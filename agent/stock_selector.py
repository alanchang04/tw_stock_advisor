"""
agent/stock_selector.py

從資料庫篩選出今日候選股票：
  1. 取族群輪動熱度前 N 產業
  2. 從這些產業撈出技術訊號正面的股票
  3. 加入籌碼面過濾
  4. 回傳候選股票清單供 LLM 分析
"""
import pandas as pd
from datetime import date, timedelta
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.strategy import (STRATEGY, score_candidates, split_adjust,
                            compute_factor_matrices)


# 排除非個股的產業類別（ETF、指數等）
EXCLUDE_INDUSTRIES = {
    "ETF", "ETN", "上櫃ETF", "指數投資證券(ETN)",
    "上櫃指數股票型基金(ETF)", "受益憑證", "存託憑證",
}

# 技術面篩選條件（集中在 strategy.py，方便調整）
MIN_RSI    = STRATEGY["min_rsi"]
MAX_RSI    = STRATEGY["max_rsi"]
MIN_CLOSE  = STRATEGY["min_close"]
MIN_VOLUME = STRATEGY["min_volume"]
MIN_TURNOVER_AVG5 = STRATEGY["min_turnover_avg5"]
TURNOVER_AVG_DAYS = STRATEGY["turnover_avg_days"]


def market_regime_detail() -> dict:
    """
    市場濾網明細：大盤代理（預設 0050）還原後收盤 vs MA60，回傳
    {"bull": bool, "stock_id": str, "close": float|None, "ma60": float|None, "ok": bool}。
    ok=False 代表查無資料/查詢失敗（此時 bull 保守回傳 True，不阻擋）。

    自行抓收盤序列並用 split_adjust() 還原後現算 MA60，不直接信任
    technical_indicators.ma60——0050 這類 ETF 若曾分割/併股，原始收盤價會
    出現單日假崩盤（見 2025-06-18 一分四實例），未還原的 MA60 會誤判成
    連續數月空頭，錯誤觸發死亡交叉出場保護。
    """
    sid = STRATEGY.get("market_filter_stock", "0050")
    try:
        with get_session() as s:
            rows = s.execute(text("""
                SELECT trade_date, close FROM daily_prices
                WHERE stock_id = :sid AND close > 0
                ORDER BY trade_date DESC LIMIT 90
            """), {"sid": sid}).fetchall()
        if len(rows) < 30:
            return {"bull": True, "stock_id": sid, "close": None, "ma60": None, "ok": False}
        closes = pd.Series({r[0]: float(r[1]) for r in rows}).sort_index()
        adj = split_adjust(closes)
        ma60 = float(adj.rolling(60, min_periods=30).mean().iloc[-1])
        last_close = float(adj.iloc[-1])
        bull = last_close >= ma60
        if not bull:
            logger.warning(f"市場濾網：{sid} 還原後收盤 {last_close:.2f} < MA60 {ma60:.2f} → 空頭模式")
        return {"bull": bull, "stock_id": sid, "close": last_close, "ma60": ma60, "ok": True}
    except Exception as e:
        logger.warning(f"市場濾網查詢失敗（視為多頭）: {e}")
        return {"bull": True, "stock_id": sid, "close": None, "ma60": None, "ok": False}


def market_is_bull() -> bool:
    """
    市場濾網：大盤代理 收盤 >= MA60 視為多頭。
    空頭時 daily_runner 不開新倉、出場加回死亡交叉（見 STRATEGY market_filter 區塊）。
    查無資料或未啟用時回傳 True（不阻擋）。細節數字見 market_regime_detail()。
    """
    if not STRATEGY.get("market_filter"):
        return True
    return market_regime_detail()["bull"]


def get_hot_sectors(top_n: int = 5, min_stocks: int = 10) -> list[str]:
    """
    取今日熱度前 N 個產業（排除 ETF 類、且族群股票數要夠）
    """
    today = date.today()
    # 往前找最近一筆有資料的日期
    with get_session() as session:
        result = session.execute(text("""
            SELECT sm.industry_code, i.name_zh,
                   sm.avg_change_pct, sm.momentum_score, sm.total_count
            FROM sector_momentum sm
            JOIN industries i ON i.code = sm.industry_code
            WHERE sm.calc_date = (
                SELECT MAX(calc_date) FROM sector_momentum
            )
            AND sm.total_count >= :min_stocks
            ORDER BY sm.momentum_score DESC
            LIMIT 20
        """), {"min_stocks": min_stocks})
        rows = result.fetchall()
        cols = list(result.keys())

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        logger.warning("sector_momentum 沒有資料，請先跑 --mode sector")
        return []

    # 過濾 ETF 類
    df = df[~df["name_zh"].isin(EXCLUDE_INDUSTRIES)]
    df = df[~df["industry_code"].isin(EXCLUDE_INDUSTRIES)]

    hot = df.head(top_n)
    logger.info(f"熱門產業 Top {top_n}：")
    for _, row in hot.iterrows():
        logger.info(
            f"  {row['name_zh']} | 漲幅 {row['avg_change_pct']:.2f}% "
            f"| 熱度 {row['momentum_score']:.3f} | {row['total_count']} 支"
        )

    return hot["industry_code"].tolist()


def _live_factor_maps(cfg: dict) -> dict:
    """
    即時算出「今天」的趨勢/題材因子（相對強度 rs20、多頭排列持續 stack_days、
    投信連買 invest_streak），回傳 {因子名: {stock_id: 值}}。與回測共用
    strategy.compute_factor_matrices，確保線上=回測。載入近 70 個交易日即足夠
    （rs20 需 21 天、stack/streak 有封頂，載多也只是取最後一列）。
    """
    with get_session() as session:
        px = pd.DataFrame(session.execute(text("""
            SELECT stock_id, trade_date, close FROM daily_prices
            WHERE trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM daily_prices ORDER BY trade_date DESC LIMIT 70) d)
              AND close > 0
        """)).fetchall(), columns=["stock_id", "trade_date", "close"])
        tk = pd.DataFrame(session.execute(text("""
            SELECT stock_id, trade_date, ma5, ma20, ma60 FROM technical_indicators
            WHERE trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM technical_indicators ORDER BY trade_date DESC LIMIT 70) d)
        """)).fetchall(), columns=["stock_id", "trade_date", "ma5", "ma20", "ma60"])
        it = pd.DataFrame(session.execute(text("""
            SELECT stock_id, trade_date, invest_net FROM institutional_trading
            WHERE trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM institutional_trading ORDER BY trade_date DESC LIMIT 70) d)
        """)).fetchall(), columns=["stock_id", "trade_date", "invest_net"])
    if px.empty:
        return {"rs20": {}, "stack_days": {}, "invest_streak": {}}
    for d in (px, tk, it):
        for c in d.columns:
            if c not in ("stock_id", "trade_date"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
    closes = px.pivot_table(index="trade_date", columns="stock_id", values="close").where(lambda x: x > 0)
    ma5p  = tk.pivot_table(index="trade_date", columns="stock_id", values="ma5")
    ma20p = tk.pivot_table(index="trade_date", columns="stock_id", values="ma20")
    ma60p = tk.pivot_table(index="trade_date", columns="stock_id", values="ma60")
    invest = it.pivot_table(index="trade_date", columns="stock_id", values="invest_net")
    rs20, stack_days, inv_streak = compute_factor_matrices(closes, ma5p, ma20p, ma60p, invest)
    last = closes.index.max()
    return {
        "rs20":          rs20.loc[last].to_dict() if last in rs20.index else {},
        "stack_days":    stack_days.loc[last].to_dict() if last in stack_days.index else {},
        "invest_streak": inv_streak.loc[last].to_dict() if last in inv_streak.index else {},
    }


def get_candidate_stocks(
    industry_codes: list[str],
    top_n: int = 20,
    cfg: dict = None,
) -> pd.DataFrame:
    """
    篩選候選股票。cfg["use_hot_sector_gate"] 為 True 時只從熱門族群選（舊行為），
    為 False 時全市場都是候選、讓相對強度/題材評分自己排序（2026-07-09 趨勢版選股）。
    """
    cfg = cfg or STRATEGY
    use_gate = cfg.get("use_hot_sector_gate", True)
    if use_gate and not industry_codes:
        return pd.DataFrame()

    min_rsi = cfg.get("min_rsi", MIN_RSI)
    max_rsi = cfg.get("max_rsi", MAX_RSI)

    # 候選池：有加族群硬閘門就用 stock_industry_map 過濾，否則全市場（一檔取一個產業名顯示）
    gate_clause = ""
    if use_gate:
        placeholders = ",".join([f"'{c}'" for c in industry_codes])
        gate_clause = f"AND m.industry_code IN ({placeholders})"

    min_turnover = cfg.get("min_turnover_avg5", MIN_TURNOVER_AVG5)
    turnover_days = cfg.get("turnover_avg_days", TURNOVER_AVG_DAYS)

    with get_session() as session:
        result = session.execute(text(f"""
            WITH recent_dates AS (
                SELECT DISTINCT trade_date FROM daily_prices
                ORDER BY trade_date DESC LIMIT :turnover_days
            ),
            avg_turnover AS (
                SELECT stock_id, AVG(turnover) AS avg_turnover
                FROM daily_prices
                WHERE trade_date IN (SELECT trade_date FROM recent_dates)
                GROUP BY stock_id
            )
            SELECT DISTINCT ON (s.stock_id)
                s.stock_id,
                s.stock_name,
                i.name_zh        AS industry,
                p.close,
                p.change_pct,
                p.volume,
                t.ma5, t.ma20, t.ma60, t.rsi14, t.macd_hist,
                t.signal_ma_cross, t.signal_breakout,
                COALESCE(inst.total_net, 0)   AS inst_net,
                COALESCE(inst.foreign_net, 0) AS foreign_net
            FROM stock_industry_map m
            JOIN stocks s         ON s.stock_id = m.stock_id
            JOIN industries i     ON i.code = m.industry_code
            JOIN daily_prices p   ON p.stock_id = m.stock_id
                AND p.trade_date = (
                    SELECT MAX(trade_date) FROM daily_prices
                    WHERE stock_id = m.stock_id
                )
            JOIN technical_indicators t ON t.stock_id = m.stock_id
                AND t.trade_date = p.trade_date
            JOIN avg_turnover at ON at.stock_id = m.stock_id
            LEFT JOIN institutional_trading inst ON inst.stock_id = m.stock_id
                AND inst.trade_date = p.trade_date
            WHERE p.close >= :min_close
            AND p.volume >= :min_volume
            AND at.avg_turnover >= :min_turnover
            AND t.rsi14 BETWEEN :min_rsi AND :max_rsi
            AND t.ma5 IS NOT NULL
            AND t.ma20 IS NOT NULL
            {gate_clause}
        """), {
            "min_close":  MIN_CLOSE,
            "min_volume": MIN_VOLUME,
            "min_turnover": min_turnover,
            "turnover_days": turnover_days,
            "min_rsi":    min_rsi,
            "max_rsi":    max_rsi,
        })
        rows = result.fetchall()
        cols = list(result.keys())

    if not rows:
        logger.warning("沒有符合條件的候選股票")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=cols)

    # 趨勢/題材因子（相對強度/多頭排列持續/投信連買）——與回測共用同一份計算
    try:
        fmaps = _live_factor_maps(cfg)
        for col in ("rs20", "stack_days", "invest_streak"):
            df[col] = df["stock_id"].map(fmaps[col])
    except Exception as e:
        logger.warning(f"趨勢/題材因子計算失敗（略過此因子）: {e}")

    # 60 日動能：取 60 個交易日前的收盤，算區間報酬（候選池內相對排名進評分）
    try:
        with get_session() as session:
            base_rows = session.execute(text("""
                WITH d AS (
                    SELECT DISTINCT trade_date FROM daily_prices
                    ORDER BY trade_date DESC LIMIT 61
                )
                SELECT p.stock_id, p.close
                FROM daily_prices p
                WHERE p.trade_date = (SELECT MIN(trade_date) FROM d)
                  AND p.close > 0
            """)).fetchall()
        base_map = {r[0]: float(r[1]) for r in base_rows}
        df["mom60"] = [
            (float(c) / base_map[sid] - 1) if base_map.get(sid) else None
            for sid, c in zip(df["stock_id"], df["close"])
        ]
    except Exception as e:
        logger.warning(f"動能計算失敗（略過此因子）: {e}")

    # 月營收年增（最新已公布月份）
    try:
        with get_session() as session:
            rev_rows = session.execute(text("""
                SELECT stock_id, yoy_pct FROM monthly_revenue
                WHERE year_month = (SELECT MAX(year_month) FROM monthly_revenue)
            """)).fetchall()
        rev_map = {r[0]: float(r[1]) for r in rev_rows if r[1] is not None}
        df["rev_yoy"] = df["stock_id"].map(rev_map)
    except Exception as e:
        logger.warning(f"月營收因子讀取失敗（略過）: {e}")

    # 綜合評分：統一使用 strategy.score_candidates（與回測共用同一份邏輯）
    df["score"] = score_candidates(df, cfg)

    df = df.sort_values("score", ascending=False)
    candidates = df.head(top_n).reset_index(drop=True)

    # P0-3 資料一致性檢查（SPEC_REASONING_LAYER）：法人資料日期必須跟上股價最新日，
    # 且候選不得全為 0（2026-07-13 並發事故：另一 pipeline backfill 途中讀到全 0，
    # 空方據此寫出「所有候選皆無法人支撐」的錯誤強論斷）。異常時標註而非默默餵 0。
    inst_ok = True
    try:
        with get_session() as session:
            pmax, imax = session.execute(text(
                "SELECT (SELECT MAX(trade_date) FROM daily_prices),"
                "       (SELECT MAX(trade_date) FROM institutional_trading)")).fetchone()
        inst_ok = (imax == pmax)
    except Exception as e:
        logger.warning(f"法人資料一致性檢查失敗（視為正常）: {e}")
    all_zero = bool(len(candidates) >= 5
                    and (pd.to_numeric(candidates["inst_net"], errors="coerce").fillna(0) == 0).all()
                    and (pd.to_numeric(candidates["foreign_net"], errors="coerce").fillna(0) == 0).all())
    candidates.attrs["inst_data_ok"] = inst_ok and not all_zero
    if not candidates.attrs["inst_data_ok"]:
        logger.warning("⚠️ quality gate：法人資料缺失/落後（日期未跟上或候選全0），"
                       "將在給 LLM 的資料中明確標註，禁止拿 0 當論證依據")

    logger.info(f"篩選出 {len(candidates)} 支候選股票（{'族群閘門' if use_gate else '全市場趨勢/題材'}）")
    return candidates


def _recent_news_map(stock_ids: list[str], days: int = 30, per_stock: int = 3) -> dict:
    """近 N 日與各候選股相關的新聞/YT 標題（market_signals.related_stocks 陣列匹配）。
    題材證據餵給辯論用（SPEC_REASONING_LAYER 2.1）；查詢失敗回空 dict 不擋流程。"""
    try:
        with get_session() as s:
            rows = s.execute(text("""
                SELECT related_stocks, title, signal_date FROM market_signals
                WHERE signal_type IN ('news', 'youtube')
                  AND related_stocks IS NOT NULL
                  AND signal_date >= CURRENT_DATE - CAST(:d AS int)
                ORDER BY signal_date DESC
            """), {"d": days}).fetchall()
    except Exception as e:
        logger.warning(f"相關新聞查詢失敗（辯論將沒有題材證據）: {e}")
        return {}
    m: dict[str, list[str]] = {}
    sidset = set(stock_ids)
    for rel, title, d in rows:
        for sid in (rel or []):
            if sid in sidset and len(m.setdefault(sid, [])) < per_stock:
                m[sid].append(f"{d}《{title}》")
    return m


def format_candidates_for_llm(df: pd.DataFrame, news_map: dict | None = None) -> str:
    """
    把候選股票資料格式化成給 LLM 分析的文字。

    2026-07-13 重寫（SPEC_REASONING_LAYER 2.1 + P0-2/P0-3）：
      - 加入真正選中這些股票的趨勢/題材因子（RS20/多頭排列/動能/營收YoY/投信連買）
        ——舊版只給當日快照，辯論者只能複述漲幅與 RSI，論證薄弱是必然
      - 法人買賣超 DB 單位是「股」，÷1000 轉張再給 LLM（舊版股當張印，1000×高估）
      - 法人資料缺失/落後時明確標註，禁止 LLM 拿 0 當「無法人支撐」的論證依據
      - 附近 30 日相關新聞標題（題材證據）；news_map 可注入（測試用），None 則自查
    """
    if df.empty:
        return ""

    inst_ok = df.attrs.get("inst_data_ok", True)
    if news_map is None:
        news_map = _recent_news_map(df["stock_id"].tolist())

    def _f(v):
        try:
            return None if v is None or pd.isna(v) else float(v)
        except (TypeError, ValueError):
            return None

    lines = ["以下是今日候選股票資料（依綜合評分排序）。"]
    if not inst_ok:
        lines.append("⚠️ 注意：今日法人買賣超資料缺失或尚未更新，下列法人數字不可作為論證依據。")
    lines.append("")

    for _, row in df.iterrows():
        ma_cross = {1: "黃金交叉", -1: "死亡交叉", 0: "無"}.get(int(row.get("signal_ma_cross") or 0), "無")
        breakout = {1: "突破壓力", -1: "跌破支撐", 0: "無"}.get(int(row.get("signal_breakout") or 0), "無")
        blk = [f"【{row['stock_id']} {row['stock_name']}】產業：{row['industry']}",
               f"  收盤：{_f(row.get('close')) or 0:.1f}｜當日漲跌：{_f(row.get('change_pct')) or 0:+.2f}%"
               f"｜RSI：{_f(row.get('rsi14')) or 0:.1f}｜MACD柱：{'正' if (_f(row.get('macd_hist')) or 0) > 0 else '負'}"]

        trend = []
        rs = _f(row.get("rs20"))
        if rs is not None:
            trend.append(f"相對強度 RS20 全市場第 {rs*100:.0f} 百分位")
        sd = _f(row.get("stack_days"))
        if sd:
            trend.append(f"多頭排列(MA5>20>60)連續 {sd:.0f} 日")
        mom = _f(row.get("mom60"))
        if mom is not None:
            trend.append(f"60日動能 {mom*100:+.0f}%")
        if trend:
            blk.append("  趨勢結構：" + "｜".join(trend))

        yoy = _f(row.get("rev_yoy"))
        if yoy is not None:
            blk.append(f"  基本面：月營收年增 {yoy:+.1f}%")

        if inst_ok:
            inst_lots = (_f(row.get("inst_net")) or 0) / 1000     # DB 單位是股 → 張
            frn_lots = (_f(row.get("foreign_net")) or 0) / 1000
            chips = [f"三大法人 {inst_lots:+,.0f} 張", f"外資 {frn_lots:+,.0f} 張"]
            ivs = _f(row.get("invest_streak"))
            if ivs:
                chips.append(f"投信連買 {ivs:.0f} 日")
            blk.append("  籌碼：" + "｜".join(chips))
        else:
            blk.append("  籌碼：法人資料缺失（今日尚未更新），不可據此論證")

        blk.append(f"  單日技術訊號（僅供進出場時機參考）：均線 {ma_cross}｜突破 {breakout}")

        for t in (news_map or {}).get(str(row["stock_id"]), []):
            blk.append(f"  題材：{t}")
        lines.append("\n".join(blk) + "\n")

    return "\n".join(lines)