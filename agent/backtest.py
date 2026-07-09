"""
agent/backtest.py

回測「族群熱度 + 候選評分」選股邏輯（不含 LLM）。

做法：在歷史每個再平衡日，用「當天為止」可得的資料重現正式流程的選股
（族群輪動 → 候選篩選 → 取分數最高的前 N 檔），再用之後 5/10/20 個交易日的
收盤價算報酬，與大盤等權報酬比較，輸出勝率、平均報酬、超額報酬。

說明 / 限制：
  - 不含 LLM：LLM 為非決定性的再排序/解說層，無法重現；回測驗證的是底層量化訊號。
  - 收盤價對收盤價、未計滑價；「淨報酬」已計手續費(14.25bp×58折,買賣各一次)與證交稅(30bp,賣出)。
  - 法人歷史資料較短（FinMind 免費版約近 90 天），早期樣本的籌碼分數可能偏弱。

用法：
    python -m agent.backtest
    python run_pipeline.py --mode backtest
"""
from datetime import date, timedelta

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.stock_selector import (
    EXCLUDE_INDUSTRIES, MIN_RSI, MAX_RSI, MIN_CLOSE, MIN_VOLUME,
)
from agent.strategy import (STRATEGY, score_candidates, decide_exit, split_adjust,
                            compute_factor_matrices)

# ── 交易成本（台股現股）──────────────────────────────────────────
# 2026-07-09：用使用者元大帳戶 4 筆真實成交手續費反推折數（見對話紀錄），
# 58% 完美吻合全部 4 筆（含捨去與四捨五入兩種進位假設皆成立）；
# 原本假設的 6折(60%) 在四捨五入規則下有 2 筆對不上，已修正為實測值。
FEE_RATE = 0.001425 * 0.58   # 券商手續費 14.25bp × 58折（買賣各收一次，實測反推）
TAX_RATE = 0.003             # 證交稅 30bp（僅賣出時收，政府統一費率與券商無關）


def net_return(entry_price: float, exit_price: float) -> float:
    """一買一賣扣除手續費+證交稅後的淨報酬。"""
    cost_in  = entry_price * (1 + FEE_RATE)
    cash_out = exit_price * (1 - FEE_RATE - TAX_RATE)
    return cash_out / cost_in - 1


# ── 載入資料（一次全載入記憶體，避免每個日期重複查 DB）───────────
def _load(parquet_dir: str | None = None) -> dict:
    """parquet_dir 給定時改讀本機 parquet（跨週期歷史回測用，見 _load_parquet）。"""
    if parquet_dir:
        return _load_parquet(parquet_dir)
    with get_session() as s:
        prices = pd.DataFrame(
            s.execute(text("""
                SELECT stock_id, trade_date, close, volume, change_pct
                FROM daily_prices
            """)).fetchall(),
            columns=["stock_id", "trade_date", "close", "volume", "change_pct"],
        )
        tech = pd.DataFrame(
            s.execute(text("""
                SELECT stock_id, trade_date, ma5, ma20, ma60, rsi14, macd_hist,
                       signal_ma_cross, signal_breakout
                FROM technical_indicators
            """)).fetchall(),
            columns=["stock_id", "trade_date", "ma5", "ma20", "ma60", "rsi14",
                     "macd_hist", "signal_ma_cross", "signal_breakout"],
        )
        inst = pd.DataFrame(
            s.execute(text("""
                SELECT stock_id, trade_date, total_net, foreign_net, invest_net
                FROM institutional_trading
            """)).fetchall(),
            columns=["stock_id", "trade_date", "total_net", "foreign_net", "invest_net"],
        )
        imap = pd.DataFrame(
            s.execute(text("SELECT stock_id, industry_code FROM stock_industry_map")).fetchall(),
            columns=["stock_id", "industry_code"],
        )
        inds = pd.DataFrame(
            s.execute(text("SELECT code, name_zh FROM industries")).fetchall(),
            columns=["industry_code", "name_zh"],
        )
        try:
            rev = pd.DataFrame(
                s.execute(text(
                    "SELECT stock_id, year_month, yoy_pct FROM monthly_revenue"
                )).fetchall(),
                columns=["stock_id", "year_month", "yoy_pct"],
            )
            rev["yoy_pct"] = pd.to_numeric(rev["yoy_pct"], errors="coerce")
        except Exception:
            rev = pd.DataFrame(columns=["stock_id", "year_month", "yoy_pct"])

    # 型別整理：DB 的 NUMERIC → float
    for df, cols in [
        (prices, ["close", "volume", "change_pct"]),
        (tech, ["ma5", "ma20", "ma60", "rsi14", "macd_hist", "signal_ma_cross", "signal_breakout"]),
        (inst, ["total_net", "foreign_net", "invest_net"]),
    ]:
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for df in (prices, tech, inst):
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    # 月營收 → point-in-time 查表 {(stock_id, 'YYYY-MM'): yoy}
    rev_map = {(r.stock_id, r.year_month): r.yoy_pct for r in rev.itertuples()}

    return {"prices": prices, "tech": tech, "inst": inst,
            "imap": imap, "inds": inds, "rev_map": rev_map}


# ── 從本機 parquet 載入（跨週期歷史回測；資料來源＝TWSE回補 或 匯入老師的歷史檔）──
#   目錄下可放（皆選配，缺的優雅降級）：
#     prices.parquet          必要：stock_id, trade_date, close, volume[, open/high/low, change_pct]
#     institutional.parquet   選配：stock_id, trade_date, total_net, foreign_net, invest_net（單位：股）
#     technical.parquet       選配：沒有就用 technical.calc_indicators 從 prices 現算
#     stock_industry_map.parquet / industries.parquet / monthly_revenue.parquet  選配
def _load_parquet(parquet_dir: str) -> dict:
    import os
    def _p(name):
        path = os.path.join(parquet_dir, name)
        return pd.read_parquet(path) if os.path.exists(path) else None

    prices = _p("prices.parquet")
    if prices is None or prices.empty:
        raise FileNotFoundError(f"{parquet_dir} 缺 prices.parquet（回測至少需要股價）")
    for c in ["close", "volume", "change_pct", "open", "high", "low"]:
        if c in prices.columns:
            prices[c] = pd.to_numeric(prices[c], errors="coerce")
    prices["trade_date"] = pd.to_datetime(prices["trade_date"]).dt.date
    prices = prices.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    if "change_pct" not in prices.columns:   # 沒給就從收盤自算（每檔各自 pct_change）
        prices["change_pct"] = prices.groupby("stock_id")["close"].pct_change() * 100

    inst = _p("institutional.parquet")
    if inst is None:
        inst = pd.DataFrame(columns=["stock_id", "trade_date", "total_net", "foreign_net", "invest_net"])
    else:
        for c in ["total_net", "foreign_net", "invest_net"]:
            inst[c] = pd.to_numeric(inst[c], errors="coerce") if c in inst.columns else 0.0
        inst["trade_date"] = pd.to_datetime(inst["trade_date"]).dt.date

    tech = _p("technical.parquet")
    if tech is None:
        tech = _compute_tech_from_prices(prices)
    else:
        for c in ["ma5", "ma20", "ma60", "rsi14", "macd_hist", "signal_ma_cross", "signal_breakout"]:
            if c in tech.columns:
                tech[c] = pd.to_numeric(tech[c], errors="coerce")
        tech["trade_date"] = pd.to_datetime(tech["trade_date"]).dt.date

    imap = _p("stock_industry_map.parquet")
    if imap is None:
        imap = pd.DataFrame(columns=["stock_id", "industry_code"])
    inds = _p("industries.parquet")
    if inds is None:
        inds = pd.DataFrame(columns=["industry_code", "name_zh"])
    rev = _p("monthly_revenue.parquet")
    rev_map = {}
    if rev is not None and not rev.empty:
        rev["yoy_pct"] = pd.to_numeric(rev["yoy_pct"], errors="coerce")
        rev_map = {(r.stock_id, r.year_month): r.yoy_pct for r in rev.itertuples()}

    logger.info(f"從 parquet 載入：股價 {len(prices)} 列、法人 {len(inst)} 列、"
                f"技術指標 {len(tech)} 列（{'現算' if _p('technical.parquet') is None else '讀檔'}）")
    return {"prices": prices, "tech": tech, "inst": inst,
            "imap": imap, "inds": inds, "rev_map": rev_map}


def _compute_tech_from_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """
    沒有現成技術指標時，用 technical.calc_indicators（與正式 pipeline 同一份）從股價現算，
    確保線上/DB回測/parquet回測三邊指標一致。只回傳回測用到的欄位。
    """
    from data_pipeline.analysis.technical import calc_indicators
    need = ["ma5", "ma20", "ma60", "rsi14", "macd_hist", "signal_ma_cross", "signal_breakout"]
    out = []
    for sid, g in prices.groupby("stock_id"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) < 30:
            continue
        d = g[["trade_date"]].copy()
        d["close"] = g["close"]
        # calc_indicators 需要 OHLC；缺開高低就用收盤代（MA/RSI/MACD 只靠收盤，仍正確）
        d["open"] = g["open"] if "open" in g.columns else g["close"]
        d["high"] = g["high"] if "high" in g.columns else g["close"]
        d["low"]  = g["low"]  if "low"  in g.columns else g["close"]
        d = calc_indicators(d)
        d["stock_id"] = sid
        out.append(d[["stock_id", "trade_date"] + need])
    tech = pd.concat(out, ignore_index=True) if out else \
        pd.DataFrame(columns=["stock_id", "trade_date"] + need)
    tech["trade_date"] = pd.to_datetime(tech["trade_date"]).dt.date
    return tech


def _available_rev_month(d) -> str:
    """日期 d 當下「已公布」的最新營收月份（M 月營收於 M+1 月 10 日公布）。"""
    y, m = d.year, d.month - (1 if d.day >= 11 else 2)
    while m <= 0:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def _normalize(s: pd.Series) -> pd.Series:
    rng = s.max() - s.min()
    return (s - s.min()) / rng if rng else pd.Series(0.5, index=s.index)


# ── 趨勢/題材因子矩陣（一次算好，供每個再平衡日 O(1) 查表）──────────
#   2026-07-09 選股重構新增的「可回測」因子：相對強度、多頭排列持續、投信連買，
#   取代原本只認「今天剛發生訊號」的單日技術面 event flags。
#   實際計算在 strategy.compute_factor_matrices（回測與即時選股共用同一份）。
def _precompute_factors(data: dict) -> None:
    closes = data["_closes"]
    tech = data["tech"]
    ma5p  = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5")
    ma20p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20")
    ma60p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma60")
    invest = data["inst"].pivot_table(index="trade_date", columns="stock_id", values="invest_net")
    data["_rs20"], data["_stack_days"], data["_inv_streak"] = \
        compute_factor_matrices(closes, ma5p, ma20p, ma60p, invest)


# ── 某日（含當天）為止的熱門族群 ─────────────────────────────────
def _hot_sectors_asof(data, d, top_n=5, min_stocks=10, window_days=7):
    start = d - timedelta(days=window_days)
    pr = data["prices"]
    win = pr[(pr["trade_date"] > start) & (pr["trade_date"] <= d)]
    if win.empty:
        return []

    # 每檔在區間的平均漲幅
    per_stock = win.groupby("stock_id", as_index=False)["change_pct"].mean()
    per_stock = per_stock.rename(columns={"change_pct": "avg_change"})

    # 每檔在區間的法人淨買超合計
    it = data["inst"]
    iwin = it[(it["trade_date"] > start) & (it["trade_date"] <= d)]
    inet = iwin.groupby("stock_id", as_index=False)["total_net"].sum() \
        .rename(columns={"total_net": "inst_net"})
    per_stock = per_stock.merge(inet, on="stock_id", how="left")
    per_stock["inst_net"] = per_stock["inst_net"].fillna(0.0)

    # 對應到產業（一檔可屬多族群）
    m = per_stock.merge(data["imap"], on="stock_id", how="inner")
    if m.empty:
        return []

    sector = m.groupby("industry_code").agg(
        avg_change_pct=("avg_change", "mean"),
        rising_count=("avg_change", lambda x: (x > 0).sum()),
        total_count=("stock_id", "count"),
        inst_net_sum=("inst_net", "sum"),
    ).reset_index()
    sector = sector.merge(data["inds"], on="industry_code", how="left")

    # 排除 ETF 類、族群股票數需足夠
    sector = sector[~sector["name_zh"].isin(EXCLUDE_INDUSTRIES)]
    sector = sector[~sector["industry_code"].isin(EXCLUDE_INDUSTRIES)]
    sector = sector[sector["total_count"] >= min_stocks]
    if sector.empty:
        return []

    sector["rising_ratio"] = sector["rising_count"] / sector["total_count"].clip(lower=1)
    score = (_normalize(sector["avg_change_pct"]) * 0.5
             + _normalize(sector["rising_ratio"]) * 0.3
             + _normalize(sector["inst_net_sum"]) * 0.2)
    sector["momentum_score"] = score
    return sector.sort_values("momentum_score", ascending=False).head(top_n)["industry_code"].tolist()


# ── 某日（含當天）的候選股票，依正式評分排序取前 N ───────────────
def _candidates_asof(data, d, industry_codes, top_n=5, cfg=None):
    cfg = cfg or STRATEGY
    use_gate = cfg.get("use_hot_sector_gate", True)
    if use_gate and not industry_codes:
        return []
    pr = data["prices"]
    px = pr[pr["trade_date"] == d]
    tk = data["tech"][data["tech"]["trade_date"] == d]
    if px.empty or tk.empty:
        return []

    if use_gate:
        # 舊行為：只取熱門族群內的股票（硬閘門）
        sids = set(data["imap"][data["imap"]["industry_code"].isin(industry_codes)]["stock_id"])
        base_px = px[px["stock_id"].isin(sids)]
    else:
        # 新行為：不用族群硬閘門，全市場都是候選，讓相對強度/題材評分自己排序，
        # 才不會像舊版把整年強勢的南亞科（族群 60% 時間不在前 5 熱門）擋在門外。
        base_px = px
    df = base_px.merge(tk, on=["stock_id", "trade_date"], how="inner")
    if df.empty:
        return []

    it = data["inst"]
    iday = it[it["trade_date"] == d][["stock_id", "total_net", "foreign_net"]] \
        .rename(columns={"total_net": "inst_net"})
    df = df.merge(iday, on="stock_id", how="left")
    df[["inst_net", "foreign_net"]] = df[["inst_net", "foreign_net"]].fillna(0.0)

    # 流動性/RSI 初篩（RSI 上限由 cfg 控制：動能策略放寬到 ~90，才不會在最強勢時被踢出）
    max_rsi = cfg.get("max_rsi", MAX_RSI)
    df = df[(df["close"] >= MIN_CLOSE) & (df["volume"] >= MIN_VOLUME)
            & (df["rsi14"] >= cfg.get("min_rsi", MIN_RSI)) & (df["rsi14"] <= max_rsi)
            & df["ma5"].notna() & df["ma20"].notna()]
    if df.empty:
        return []
    df = df.copy()

    # 60 日動能（用預先建好的 closes pivot；與正式選股的 mom60 對應）
    piv = data.get("_closes")
    if piv is not None and d in piv.index:
        i = piv.index.get_loc(d)
        if i >= 60:
            mom = (piv.iloc[i] / piv.iloc[i - 60] - 1)
            df["mom60"] = df["stock_id"].map(mom)

    # 趨勢/題材新因子：相對強度、多頭排列持續、投信連買（O(1) 查預算矩陣的當日列）
    for col, key in [("rs20", "_rs20"), ("stack_days", "_stack_days"),
                     ("invest_streak", "_inv_streak")]:
        mat = data.get(key)
        if mat is not None and d in mat.index:
            df[col] = df["stock_id"].map(mat.loc[d])

    # 月營收年增（point-in-time：只用當日已公布的月份，避免前視偏差）
    rev_map = data.get("rev_map")
    if rev_map:
        ym = _available_rev_month(d)
        df["rev_yoy"] = [rev_map.get((sid, ym)) for sid in df["stock_id"]]

    # 與正式選股共用 strategy.score_candidates（改權重，回測自動跟著變）
    df["score"] = score_candidates(df, cfg)
    return df.sort_values("score", ascending=False).head(top_n)["stock_id"].tolist()


# ── 主回測：真實進出場（round-trip）─────────────────────────────
def run_backtest(top_n=None, rebalance=5, cfg=None, data=None, quiet=False,
                 start_date=None, end_date=None, parquet_dir=None):
    """
    模擬實際操作：再平衡日依評分進場，每日依 strategy 出場規則平倉，
    計算每筆完整交易(round-trip)的報酬、勝率、平均持有天數，並與買進持有比較。

    cfg:   出場規則參數 dict（預設 STRATEGY）——消融測試用
    data:  預載資料（_load() 的回傳）——多次回測共用，避免重複查 DB
    quiet: True 時不印報告，只回傳交易 DataFrame
    start_date/end_date: 限制回測區間（walk-forward 分段用；datetime.date）
    parquet_dir: 給定時改讀本機 parquet 歷史資料（跨週期回測），不查 DB
    """
    cfg = cfg or STRATEGY
    top_n = top_n or cfg["pick_top_n"]
    if not quiet:
        logger.info("=== 回測開始（進場評分 + strategy 出場規則，不含 LLM）===")
    if data is None:
        data = _load(parquet_dir=parquet_dir)
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)   # close<=0 為資料瑕疵(停牌/無成交)，視為缺值
    data["_closes"] = closes            # 供 _candidates_asof 算 60 日動能
    if "_rs20" not in data:             # 趨勢/題材因子矩陣（相對強度/多頭排列/投信連買）
        _precompute_factors(data)

    # 市場濾網：大盤代理收盤 vs 其 60 日均線（逐日 bull/bear）
    regime_bull = None
    mf_sid = cfg.get("market_filter_stock", "0050")
    if cfg.get("market_filter") and mf_sid in closes.columns:
        # split_adjust：市場濾網代理股（預設0050）若曾分割/併股，原始收盤價會出現
        # 單日假崩盤（見 2025-06-18 一分四實例），未還原會讓 MA60 誤判成連續數月
        # 空頭，錯誤觸發死亡交叉出場保護（見對話紀錄的根因分析）。
        mkt = split_adjust(closes[mf_sid])
        regime_bull = (mkt >= mkt.rolling(60, min_periods=30).mean()).fillna(True)
    bear_cfg = ({**cfg, "exit_on_death_cross": True}
                if cfg.get("bear_reenable_death_cross") else cfg)

    tech = data["tech"]
    ma5p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5")
    ma20p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20")
    dates = sorted(closes.index)
    pos_idx = {d: i for i, d in enumerate(dates)}

    inst_start = data["inst"]["trade_date"].min()
    effective_start = max(inst_start, start_date) if start_date else inst_start
    start_i = next((i for i, d in enumerate(dates) if d >= effective_start), 0)
    sim_dates = dates[start_i:]
    if end_date:
        sim_dates = [d for d in sim_dates if d <= end_date]
    if len(sim_dates) < 10:
        logger.error("可回測區間太短"); return

    def val(piv, d, sid):
        try:
            v = piv.at[d, sid]
            return None if pd.isna(v) else float(v)
        except KeyError:
            return None

    max_open = cfg.get("max_open_positions", 10)
    capital_per_slot = cfg["capital"] / max_open   # 假設每格等額資金（近似 suggest_shares 的風控上限）

    open_pos = {}     # stock_id -> {entry_date, entry_price, peak, shares}
    trades = []       # 完整交易紀錄
    cash = cfg["capital"]
    nav_curve = []    # (date, cash+持股市值) —— 真實資金受限的權益曲線，供公平期間比較用
    for i, d in enumerate(sim_dates):
        bull = True if regime_bull is None else bool(regime_bull.get(d, True))
        day_cfg = cfg if bull else bear_cfg

        # 1) 先處理出場
        for sid in list(open_pos.keys()):
            close = val(closes, d, sid)
            if close is None:
                continue
            p = open_pos[sid]
            p["peak"] = max(p["peak"], close)
            hold = i - p["entry_i"]
            ex, reason = decide_exit(p["entry_price"], p["peak"], close,
                                     val(ma5p, d, sid), val(ma20p, d, sid), hold,
                                     cfg=day_cfg)
            if ex:
                trades.append({"stock_id": sid, "entry_date": p["entry_date"], "exit_date": d,
                               "ret": close / p["entry_price"] - 1,
                               "net_ret": net_return(p["entry_price"], close),
                               "hold": hold, "reason": reason})
                cash += p["shares"] * close * (1 - FEE_RATE - TAX_RATE)
                del open_pos[sid]

        # 2) 再平衡日進場（未持有者才買；濾網設 block_entries 時空頭不開新倉；
        #    同時持倉數不得超過 max_open_positions —— 與 portfolio.record_entries()
        #    的風控守門員一致，回測才是真的在模擬「max_open_positions=10」這條規則，
        #    而不是無限制同時持倉）
        gi = pos_idx[d]
        entry_ok = bull or not cfg.get("market_filter_block_entries", False)
        if entry_ok and (gi - start_i) % rebalance == 0:
            free_slots = max_open - len(open_pos)
            if free_slots > 0:
                hot = (_hot_sectors_asof(data, d, top_n=cfg["hot_sectors_top_n"])
                       if cfg.get("use_hot_sector_gate", True) else None)
                for sid in _candidates_asof(data, d, hot, top_n=top_n, cfg=cfg):
                    if free_slots <= 0:
                        break
                    if sid in open_pos:
                        continue
                    price = val(closes, d, sid)
                    if not price:
                        continue
                    shares = int(capital_per_slot // (price * (1 + FEE_RATE)))
                    if shares <= 0:
                        continue
                    cash -= shares * price * (1 + FEE_RATE)
                    open_pos[sid] = {"entry_date": d, "entry_price": price, "peak": price,
                                      "entry_i": i, "shares": shares}
                    free_slots -= 1

        mkt_val = sum(p["shares"] * (val(closes, d, sid) or p["peak"]) for sid, p in open_pos.items())
        nav_curve.append((d, cash + mkt_val))

    # 期末仍持有者，以最後一天收盤平倉計入
    last = sim_dates[-1]
    for sid, p in open_pos.items():
        close = val(closes, last, sid)
        if close:
            hold = len(sim_dates) - 1 - p["entry_i"]
            trades.append({"stock_id": sid, "entry_date": p["entry_date"], "exit_date": last,
                           "ret": close / p["entry_price"] - 1,
                           "net_ret": net_return(p["entry_price"], close),
                           "hold": hold, "reason": "回測結束平倉"})

    if not trades:
        logger.error("回測期間沒有任何交易"); return

    tdf = pd.DataFrame(trades)
    # 大盤參考一：同區間所有股票等權買進持有（僅供參考，非可投資組合，易被少數飆股拉高）
    base, fin = closes.loc[sim_dates[0]], closes.loc[last]
    common = base.dropna().index.intersection(fin.dropna().index)
    bench = (fin[common] / base[common] - 1)
    bench = bench[base[common] > 0]
    # 大盤參考二：0050 實際同期報酬（用 split_adjust 還原分割，這才是一般人講的「大盤」）
    bench_0050 = None
    nav_0050 = None
    if mf_sid in closes.columns:
        mkt_adj = split_adjust(closes[mf_sid])
        p0, p1 = mkt_adj.get(sim_dates[0]), mkt_adj.get(last)
        if p0 and p1 and p0 > 0:
            bench_0050 = p1 / p0 - 1
            # 0050 買進持有的逐日 NAV（同本金），供風險調整後(Sharpe/回撤/Calmar)對比
            nav_0050 = (mkt_adj.reindex(sim_dates).ffill() / p0) * cfg["capital"]
    nav = pd.Series({d: v for d, v in nav_curve}).sort_index()
    m = perf_metrics(nav)
    m0050 = perf_metrics(nav_0050) if nav_0050 is not None else None
    # 摘要指標掛在 df.attrs，供比較腳本程式化讀取（不必解析印出的文字）
    tdf.attrs.update({
        "nav_total_ret": m["total"], "nav_mdd": m["mdd"],
        "sharpe": m["sharpe"], "ann_ret": m["ann_ret"], "ann_vol": m["ann_vol"], "calmar": m["calmar"],
        "bench_0050": bench_0050,
        "sharpe_0050": m0050["sharpe"] if m0050 else None,
        "mdd_0050": m0050["mdd"] if m0050 else None,
        "calmar_0050": m0050["calmar"] if m0050 else None,
        "bench_eqw": bench.mean(),
        "net_win": net_win_rate(tdf),
        "start": sim_dates[0], "end": last,
    })
    if not quiet:
        _report_roundtrip(tdf, bench, bench_0050, nav, m, m0050, cfg["capital"],
                          sim_dates, top_n, rebalance)
    return tdf


def net_win_rate(tdf) -> float:
    nets = tdf["net_ret"] if "net_ret" in tdf.columns else tdf["ret"]
    return float((nets > 0).mean())


TRADING_DAYS = 252

def perf_metrics(nav: pd.Series) -> dict:
    """
    從逐日 NAV/價格序列算風險調整後績效：總報酬、年化報酬、年化波動、
    Sharpe（rf=0）、最大回撤、Calmar（年化報酬/|最大回撤|）。
    2026-07-09 起把回測目標從「贏 0050 報酬」改為「風險調整後贏 0050」——
    大多頭年不糾結拚報酬，而是追求貼近大盤報酬、但波動與回撤更小。
    """
    nav = nav.dropna()
    if len(nav) < 3:
        return dict(total=0, ann_ret=0, ann_vol=0, sharpe=0, mdd=0, calmar=0)
    rets = nav.pct_change().dropna()
    years = len(nav) / TRADING_DAYS
    total = nav.iloc[-1] / nav.iloc[0] - 1
    ann_ret = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    ann_vol = rets.std() * (TRADING_DAYS ** 0.5)
    sharpe = (rets.mean() / rets.std()) * (TRADING_DAYS ** 0.5) if rets.std() > 0 else 0.0
    mdd = ((nav - nav.cummax()) / nav.cummax()).min()
    calmar = ann_ret / abs(mdd) if mdd < 0 else float("inf")
    return dict(total=total, ann_ret=ann_ret, ann_vol=ann_vol,
                sharpe=sharpe, mdd=mdd, calmar=calmar)


def _report_roundtrip(tdf, bench, bench_0050, nav, m, m0050, capital, sim_dates, top_n, rebalance):
    rets = tdf["ret"]
    nets = tdf["net_ret"] if "net_ret" in tdf.columns else rets
    win = (rets > 0).mean()
    net_win = (nets > 0).mean()

    gross_win  = nets[nets > 0].sum()
    gross_loss = abs(nets[nets <= 0].sum())
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    sharpe_pt = nets.mean() / nets.std() if nets.std() > 0 else 0.0   # 每筆 Sharpe

    def _cmp(a, b):   # 策略指標 vs 0050，標出誰較優
        return "✅優於0050" if a >= b else "⚠️劣於0050"

    # 出場原因分布
    by_reason = tdf.groupby("reason")["ret"].agg(["count", "mean"]).sort_values("count", ascending=False)
    lines = [
        "",
        "=" * 66,
        "  回測結果：實際進出場模擬（進場評分 + strategy 出場規則，不含 LLM）",
        "=" * 66,
        f"  區間：{sim_dates[0]} ~ {sim_dates[-1]}（每 {rebalance} 交易日再平衡，每次最多 {top_n} 檔，"
        f"同時持倉上限依 strategy.max_open_positions）",
        f"  完整交易數：{len(tdf)}",
        "",
        "  【單筆交易統計（僅供檢視個別買賣品質，時間基準是每筆平均持有天數，",
        "    不可直接拿來跟下面的整期間報酬相減比較 —— 那是兩個不同時間長度的數字）】",
        f"  毛報酬：勝率 {win*100:.1f}%   平均 {rets.mean()*100:+.2f}%   中位數 {rets.median()*100:+.2f}%",
        f"  淨報酬：勝率 {net_win*100:.1f}%   平均 {nets.mean()*100:+.2f}%   （含手續費58折+證交稅，每筆約 -{(rets.mean()-nets.mean())*100:.2f}%）",
        f"  最佳：{rets.max()*100:+.1f}%   最差：{rets.min()*100:+.1f}%",
        f"  獲利因子：{profit_factor:.2f}   每筆Sharpe：{sharpe_pt:.2f}",
        f"  平均持有天數：{tdf['hold'].mean():.1f} 交易日",
        "",
        "  【風險調整後績效 vs 0050 —— 目標：貼近大盤報酬、但波動與回撤更小】",
        f"  策略  ：總報酬 {m['total']*100:+.2f}%  年化 {m['ann_ret']*100:+.2f}%  "
        f"年化波動 {m['ann_vol']*100:.1f}%  Sharpe {m['sharpe']:.2f}  最大回撤 {m['mdd']*100:.1f}%  Calmar {m['calmar']:.2f}",
    ]
    if m0050:
        lines += [
            f"  0050 ：總報酬 {bench_0050*100:+.2f}%  年化 {m0050['ann_ret']*100:+.2f}%  "
            f"年化波動 {m0050['ann_vol']*100:.1f}%  Sharpe {m0050['sharpe']:.2f}  最大回撤 {m0050['mdd']*100:.1f}%  Calmar {m0050['calmar']:.2f}",
            f"  → Sharpe {_cmp(m['sharpe'], m0050['sharpe'])}   "
            f"最大回撤 {_cmp(m['mdd'], m0050['mdd'])}   Calmar {_cmp(m['calmar'], m0050['calmar'])}",
        ]
    else:
        lines.append("  0050 ：無資料")
    lines += [
        f"  （參考，非可投資組合）全樣本個股等權買進持有同期：{bench.mean()*100:+.2f}%"
        f"（中位數 {bench.median()*100:+.2f}%，易被少數飆股拉高平均，僅供對照）",
        "-" * 66,
        "  出場原因分布（次數 / 該原因平均毛報酬）：",
    ]
    for reason, row in by_reason.iterrows():
        lines.append(f"    {reason:<16} {int(row['count']):>3} 筆   {row['mean']*100:+.2f}%")
    lines += [
        "-" * 66,
        "  註：收盤價成交、未計滑價；淨報酬含手續費(買賣)與證交稅(賣出)。",
        "  調 agent/strategy.py 的參數後重跑此回測，即可比較買賣邏輯優劣。",
        "=" * 66,
    ]
    print("\n".join(l for l in lines if l))
    logger.info("回測完成")


if __name__ == "__main__":
    run_backtest()
