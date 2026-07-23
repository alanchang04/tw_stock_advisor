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
from collections import Counter
from datetime import date, timedelta

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.stock_selector import (
    EXCLUDE_INDUSTRIES, MIN_RSI, MAX_RSI, MIN_CLOSE, MIN_VOLUME,
    TURNOVER_AVG_DAYS,
)
from agent.strategy import (STRATEGY, score_candidates, decide_exit, split_adjust,
                            compute_factor_matrices, compute_new_entry_flag, apply_liquidity_gate,
                            apply_total_return_adjustment,
                            # 交易成本/成交假設：單一事實來源在 strategy.py（回測與即時帳本共用）
                            FEE_RATE, TAX_RATE, SLIPPAGE, net_return)


# ── 載入資料（一次全載入記憶體，避免每個日期重複查 DB）───────────
def _load(parquet_dir: str | None = None, since=None) -> dict:
    """parquet_dir 給定時改讀本機 parquet（跨週期歷史回測用，見 _load_parquet）。

    since（date，選配，只影響直連 Neon 這條路徑）：只查這天以後的價量/技術指標/
    法人資料。2026-07-20修正一個真實事故：`run_pipeline.py _weekly_review_text()`
    每週五排程呼叫 `run_backtest()` 沒帶這個參數，對 daily_prices/technical_indicators/
    institutional_trading 三張表都是無 WHERE、無 LIMIT 的全表查詢——production DB
    的網路傳輸配額(Neon免費層5GB/月)因此被按週推播的小功能吃光，見
    docs/SPEC_QUANT_UPGRADE.md §2.8。近期摘要用途不需要全歷史，帶 since 大幅縮小
    傳輸量；不帶（None）維持原行為，跨週期研究/回測仍讀全表。
    """
    if parquet_dir:
        return _load_parquet(parquet_dir)
    date_filter = " WHERE trade_date >= :since" if since else ""
    params = {"since": since} if since else {}
    with get_session() as s:
        prices = pd.DataFrame(
            s.execute(text(f"""
                SELECT stock_id, trade_date, open, close, volume, turnover, change_pct
                FROM daily_prices{date_filter}
            """), params).fetchall(),
            columns=["stock_id", "trade_date", "open", "close", "volume", "turnover", "change_pct"],
        )
        tech = pd.DataFrame(
            s.execute(text(f"""
                SELECT stock_id, trade_date, ma5, ma20, ma60, rsi14, macd_hist,
                       signal_ma_cross, signal_breakout
                FROM technical_indicators{date_filter}
            """), params).fetchall(),
            columns=["stock_id", "trade_date", "ma5", "ma20", "ma60", "rsi14",
                     "macd_hist", "signal_ma_cross", "signal_breakout"],
        )
        inst = pd.DataFrame(
            s.execute(text(f"""
                SELECT stock_id, trade_date, total_net, foreign_net, invest_net
                FROM institutional_trading{date_filter}
            """), params).fetchall(),
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
        try:
            # 除權息事件（SPEC_QUANT_UPGRADE.md P0-2）：個股後復權還原用，表可能還沒
            # backfill（見 corporate_actions_fetcher.py），查不到就優雅降級為空。
            div = pd.DataFrame(
                s.execute(text(
                    "SELECT stock_id, ex_date, pre_close, ref_price FROM dividend_events"
                )).fetchall(),
                columns=["stock_id", "ex_date", "pre_close", "ref_price"],
            )
            div["pre_close"] = pd.to_numeric(div["pre_close"], errors="coerce")
            div["ref_price"] = pd.to_numeric(div["ref_price"], errors="coerce")
            div["ex_date"] = pd.to_datetime(div["ex_date"]).dt.date
        except Exception:
            div = pd.DataFrame(columns=["stock_id", "ex_date", "pre_close", "ref_price"])

    # 型別整理：DB 的 NUMERIC → float
    for df, cols in [
        (prices, ["open", "close", "volume", "turnover", "change_pct"]),
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
            "imap": imap, "inds": inds, "rev_map": rev_map, "dividends": div}


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
    for c in ["close", "volume", "turnover", "change_pct", "open", "high", "low"]:
        if c in prices.columns:
            prices[c] = pd.to_numeric(prices[c], errors="coerce")
    prices["trade_date"] = pd.to_datetime(prices["trade_date"]).dt.date
    prices = prices.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    if "change_pct" not in prices.columns:   # 沒給就從收盤自算（每檔各自 pct_change）
        prices["change_pct"] = prices.groupby("stock_id")["close"].pct_change() * 100
    if "turnover" not in prices.columns:
        # 舊資料（如老師的歷史檔）可能沒有成交金額欄位——優雅降級，流動性門檻在
        # _candidates_asof 會偵測全 NaN 並自動跳過該篩選，不擋整個回測。
        prices["turnover"] = pd.NA

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

    # 除權息事件（選配，P0-2）：老師的歷史檔多半不含，優雅降級為空——
    # 此時個股報酬不做除權息還原，跟現行行為一致，不會讓舊資料跑不動。
    div = _p("dividend_events.parquet")
    if div is None:
        div = pd.DataFrame(columns=["stock_id", "ex_date", "pre_close", "ref_price"])
    else:
        for c in ["pre_close", "ref_price"]:
            if c in div.columns:
                div[c] = pd.to_numeric(div[c], errors="coerce")
        div["ex_date"] = pd.to_datetime(div["ex_date"]).dt.date

    # 融資融券（選配，2026-07-23）：只有 short_ratio(券資比) 通過 IC 檢驗
    # （見 scripts/run_margin_factor_report.py 與 SPEC_QUANT_UPGRADE.md §5.5）。
    # 檔案不存在時優雅降級為 None，w_short_ratio 自然不生效，舊資料照跑。
    margin = _p("margin.parquet")
    if margin is not None and not margin.empty:
        for c in ["margin_balance", "short_balance"]:
            if c in margin.columns:
                margin[c] = pd.to_numeric(margin[c], errors="coerce")
        margin["trade_date"] = pd.to_datetime(margin["trade_date"]).dt.date

    logger.info(f"從 parquet 載入：股價 {len(prices)} 列、法人 {len(inst)} 列、"
                f"技術指標 {len(tech)} 列、除權息 {len(div)} 筆"
                f"、融資融券 {0 if margin is None else len(margin)} 列"
                f"（{'現算' if _p('technical.parquet') is None else '讀檔'}）")
    return {"prices": prices, "tech": tech, "inst": inst, "margin": margin,
            "imap": imap, "inds": inds, "rev_map": rev_map, "dividends": div}


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
def _precompute_factors(data: dict, cfg: dict = STRATEGY) -> None:
    closes = data["_closes"]
    tech = data["tech"]
    ma5p  = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5")
    ma20p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20")
    ma60p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma60")
    invest = data["inst"].pivot_table(index="trade_date", columns="stock_id", values="invest_net")
    data["_rs20"], data["_stack_days"], data["_inv_streak"] = \
        compute_factor_matrices(closes, ma5p, ma20p, ma60p, invest)
    # 投信新進場（2026-07-15）：可回測，用同一份法人歷史資料算——跟ETF增碼不同，
    # 這個因子完全靠 institutional_trading，沒有ETF訊號那種資料量稀薄的問題。
    invest_r = invest.reindex(index=closes.index, columns=closes.columns)
    data["_new_entry"] = compute_new_entry_flag(invest_r, cfg.get("new_entry_min_lots", 50))

    # 券資比（融券餘額/融資餘額，2026-07-23）：5個融資融券衍生因子裡唯一通過 IC 檢驗的
    # （h20 |ICIR| 0.252、h60 0.372，優於現行仍在用的 foreign_buy/stack_days；IC 為負
    # ＝低券資比後續報酬較好）。資料缺就不建這個矩陣，w_short_ratio 自然不生效。
    # 波段進場型態（2026-07-23，練習軌用；cfg["require_swing_setup"]=True 時當進場濾網）
    try:
        from agent.strategy import compute_swing_setup
        tech_p = lambda c: tech.pivot_table(index="trade_date", columns="stock_id", values=c)
        px_p = lambda c: data["prices"].pivot_table(index="trade_date", columns="stock_id", values=c)
        data["_swing_setup"] = compute_swing_setup(
            px_p("open"), px_p("high"), px_p("low"), closes, px_p("volume"),
            tech_p("ma20"), tech_p("ma60"), cfg.get("swing_setup"))
    except Exception as e:
        logger.warning(f"波段型態矩陣計算失敗（require_swing_setup 將無效）: {e}")

    margin = data.get("margin")
    if margin is not None and not margin.empty:
        m_bal = margin.pivot_table(index="trade_date", columns="stock_id", values="margin_balance")
        s_bal = margin.pivot_table(index="trade_date", columns="stock_id", values="short_balance")
        m_bal = m_bal.reindex(index=closes.index, columns=closes.columns)
        s_bal = s_bal.reindex(index=closes.index, columns=closes.columns)
        data["_short_ratio"] = s_bal / m_bal.where(m_bal > 0)


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
    min_close = cfg.get("min_close", MIN_CLOSE)
    min_volume = cfg.get("min_volume", MIN_VOLUME)
    df = df[(df["close"] >= min_close) & (df["volume"] >= min_volume)
            & (df["rsi14"] >= cfg.get("min_rsi", MIN_RSI)) & (df["rsi14"] <= max_rsi)
            & df["ma5"].notna() & df["ma20"].notna()]
    if df.empty:
        return []
    df = df.copy()

    # 趨勢/題材新因子：相對強度、多頭排列持續、投信連買、投信新進場
    # （O(1) 查預算矩陣的當日列；要先算好才能給下面的流動性OR閘門用 invest_new_entry）
    for col, key in [("rs20", "_rs20"), ("stack_days", "_stack_days"),
                     ("invest_streak", "_inv_streak"), ("invest_new_entry", "_new_entry"),
                     ("short_ratio", "_short_ratio")]:
        mat = data.get(key)
        if mat is not None and d in mat.index:
            df[col] = df["stock_id"].map(mat.loc[d])

    # 波段進場型態濾網（2026-07-23）：cfg["require_swing_setup"]=True 時，只留「今天剛好
    # 走到進場位置」的股票。這是練習軌的邏輯，拿來做「跟AI主軌可比的組合回測」用。
    if cfg.get("require_swing_setup"):
        sw = data.get("_swing_setup")
        if sw is not None and d in sw.index:
            row = sw.loc[d]
            df = df[df["stock_id"].map(lambda s: bool(row.get(s, False)))]
            if df.empty:
                return []

    # 成交金額（流動性/抗操控）門檻：舊資料（無 turnover 欄位，如老師歷史檔）全 NaN 時優雅跳過。
    # 2026-07-15 起改用 apply_liquidity_gate（OR邏輯：成交金額達標 OR 投信新進場+較低下限）；
    # ETF增碼訊號資料量太稀薄不進回測（同新聞的限制），backtest 沒有 etf_accum_count 欄位，
    # score_candidates 會自動跳過那個因子，不影響其他分數。
    avg_to = data.get("_avg_turnover")
    if avg_to is not None and d in avg_to.index:
        to_row = avg_to.loc[d]
        if to_row.notna().any():
            df["avg_turnover"] = df["stock_id"].map(to_row)
            # 百分位排名要用「當日全市場」分佈算，不能只用已經被RSI/收盤價等其他
            # 條件篩過的candidate子集（子集分佈會偏，百分位失去「相對排名」的意義）。
            if cfg.get("min_turnover_percentile") is not None:
                df["turnover_percentile"] = df["stock_id"].map(to_row.rank(pct=True))
            df = apply_liquidity_gate(df, cfg)
            if df.empty:
                return []

    # 60 日動能（用預先建好的 closes pivot；與正式選股的 mom60 對應）
    piv = data.get("_closes")
    if piv is not None and d in piv.index:
        i = piv.index.get_loc(d)
        if i >= 60:
            mom = (piv.iloc[i] / piv.iloc[i - 60] - 1)
            df["mom60"] = df["stock_id"].map(mom)

    # 月營收年增（point-in-time：只用當日已公布的月份，避免前視偏差）
    rev_map = data.get("rev_map")
    if rev_map:
        ym = _available_rev_month(d)
        df["rev_yoy"] = [rev_map.get((sid, ym)) for sid in df["stock_id"]]

    # 與正式選股共用 strategy.score_candidates（改權重，回測自動跟著變）
    df["score"] = score_candidates(df, cfg)
    return df.sort_values("score", ascending=False).head(top_n)["stock_id"].tolist()


def is_limit_locked(change_pct: float | None, volume: float | None,
                    avg_volume: float | None, cfg: dict = STRATEGY) -> bool:
    """
    跌停鎖死模擬（2026-07-15）：開盤跌幅≤門檻且量遠低於近5日均量 → 判定無法成交
    （真實市場跌停鎖死時排隊賣不掉，回測若照樣強制成交會低估回撤/高估可實現報酬）。
    change_pct 為百分比數值（如 -9.6 代表跌9.6%），純函式方便單獨測試。
    """
    limit_down_pct = cfg.get("limit_down_pct", -0.095)
    lock_vol_ratio = cfg.get("limit_lock_vol_ratio", 0.10)
    if change_pct is None or change_pct / 100 > limit_down_pct:
        return False
    if volume is None or not avg_volume:
        return False
    return volume < avg_volume * lock_vol_ratio


def _adaptive_throttle_blocked(recent_wins: list[bool], cfg: dict = STRATEGY) -> bool:
    """
    訊號品質偵測+動態縮手（2026-07-20，SPEC_QUANT_UPGRADE.md：診斷2021/2024兩個
    「0050被少數權值股拉漲、但策略實際交易的個股池當年沒有明確趨勢」的異常虧損
    年份後新增）：死亡交叉/停損這類趨勢跟隨出場規則，在無趨勢/巴來巴去的環境裡
    會被反覆巴（那兩年勝率掉到25~27%，遠低於10年平均34%）。這種「個股池無趨勢」
    沒辦法只看0050自己的漲跌判斷（0050那兩年都還是正的），只能從策略自己「最近
    實際打得怎樣」偵測——recent_wins 是逐筆平倉勝負紀錄（True=贏），累積到
    min_trades 筆之前一律不擋（避免暖身期資料不足誤觸發），之後看最近 lookback
    筆的勝率，低於門檻就回傳 True（擋新倉；已有部位的出場規則不受影響）。
    純函式方便單獨測試，不用跑整個 run_backtest。
    """
    if not cfg.get("adaptive_throttle_enabled"):
        return False
    min_trades = cfg.get("adaptive_throttle_min_trades", 10)
    if len(recent_wins) < min_trades:
        return False
    lookback = cfg.get("adaptive_throttle_lookback", 10)
    recent = recent_wins[-lookback:]
    return (sum(recent) / len(recent)) < cfg.get("adaptive_throttle_win_rate", 0.20)


# ── 主回測：真實進出場（round-trip）─────────────────────────────
def run_backtest(top_n=None, rebalance=5, cfg=None, data=None, quiet=False,
                 start_date=None, end_date=None, parquet_dir=None, slippage=SLIPPAGE):
    """
    模擬實際操作：再平衡日依評分進場，每日依 strategy 出場規則平倉，
    計算每筆完整交易(round-trip)的報酬、勝率、平均持有天數，並與買進持有比較。

    cfg:   出場規則參數 dict（預設 STRATEGY）——消融測試用
    data:  預載資料（_load() 的回傳）——多次回測共用，避免重複查 DB
    quiet: True 時不印報告，只回傳交易 DataFrame
    start_date/end_date: 限制回測區間（walk-forward 分段用；datetime.date）
    parquet_dir: 給定時改讀本機 parquet 歷史資料（跨週期回測），不查 DB
    slippage: 單邊滑價假設（預設 SLIPPAGE）；成交價＝隔日開盤 ×(1±slippage)

    成交時序：今日收盤算訊號 → 隔日開盤成交（pipeline 收盤後才跑，當日收盤價買不到）。
    """
    cfg = cfg or STRATEGY
    top_n = top_n or cfg["pick_top_n"]
    if not quiet:
        logger.info("=== 回測開始（進場評分 + strategy 出場規則，不含 LLM）===")
    if data is None:
        # 直連 Neon 時，若呼叫端只在乎 start_date 之後的區間（如週報摘要），帶
        # since 讓 _load() 只查這段+120天緩衝（MA60/60日動能等指標需要的暖身期），
        # 大幅縮小網路傳輸量。parquet_dir 給定時走本機檔案，不受影響。
        since = (start_date - timedelta(days=120)) if (start_date and not parquet_dir) else None
        data = _load(parquet_dir=parquet_dir, since=since)
    closes = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)   # close<=0 為資料瑕疵(停牌/無成交)，視為缺值
    div_events = data.get("dividends")
    if cfg.get("total_return_adjust", True) and div_events is not None and not div_events.empty:
        # 個股除權息還原（SPEC_QUANT_UPGRADE.md P0-2）：用官方除權息事件（ground truth，
        # 不是像 split_adjust 那樣用單日跌幅門檻猜）把股息還原進報酬，同時消除除息日
        # 假跳空誤觸停損/跌破實體底等出場規則。opens 建立時會用同一批事件再做一次，
        # 確保同一天 open/close 落在一致的復權基準上。
        closes = apply_total_return_adjustment(closes, div_events)
    data["_closes"] = closes            # 供 _candidates_asof 算 60 日動能
    if "_rs20" not in data:             # 趨勢/題材因子矩陣（相對強度/多頭排列/投信連買/新進場）
        _precompute_factors(data, cfg)
    if "_avg_turnover" not in data:     # 近N日均成交金額（流動性/抗操控門檻，SPEC_STRATEGY_MIDCAP）
        turnover_piv = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="turnover")
        turnover_piv = turnover_piv.reindex(index=closes.index, columns=closes.columns)
        days = cfg.get("turnover_avg_days", TURNOVER_AVG_DAYS)
        data["_avg_turnover"] = turnover_piv.rolling(days, min_periods=1).mean()
    if "_volume" not in data:           # 跌停鎖死偵測 + 成交量天花板（2026-07-15 人類練習軌規格）
        vol_piv = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="volume")
        vol_piv = vol_piv.reindex(index=closes.index, columns=closes.columns)
        data["_volume"] = vol_piv
        chg_piv = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="change_pct")
        data["_change_pct"] = chg_piv.reindex(index=closes.index, columns=closes.columns)
        liq_days = cfg.get("liquidity_avg_days", 5)
        # shift(1)：baseline 用「當天以前」的量，避免鎖死當天的量把自己的基準拉低
        data["_avg_volume_liq"] = vol_piv.shift(1).rolling(liq_days, min_periods=1).mean()
    if "_sid_to_inds" not in data:      # 族群曝險上限用（SPEC_QUANT_UPGRADE.md P3決策3）
        d2 = {}
        imap = data.get("imap")
        if imap is not None and not imap.empty:
            for sid, g in imap.groupby("stock_id"):
                d2[sid] = set(g["industry_code"])   # 一檔可能跨多族群
        data["_sid_to_inds"] = d2

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

    # 隔日開盤成交用的開盤價矩陣（對齊 closes 的 index/columns）
    opens = data["prices"].pivot_table(index="trade_date", columns="stock_id", values="open")
    opens = opens.where(opens > 0).reindex(index=closes.index, columns=closes.columns)
    if cfg.get("total_return_adjust", True) and div_events is not None and not div_events.empty:
        opens = apply_total_return_adjustment(opens, div_events)

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

    def _limit_locked(d, sid) -> bool:
        return is_limit_locked(val(data["_change_pct"], d, sid), val(data["_volume"], d, sid),
                               val(data["_avg_volume_liq"], d, sid), cfg)

    max_open = cfg.get("max_open_positions", 10)
    capital_per_slot = cfg["capital"] / max_open   # 假設每格等額資金（近似 suggest_shares 的風控上限）

    open_pos = {}     # stock_id -> {entry_date, entry_price, peak, shares}
    trades = []       # 完整交易紀錄
    cash = cfg["capital"]
    nav_curve = []    # (date, cash+持股市值) —— 真實資金受限的權益曲線，供公平期間比較用
    # 掛單簿：今日收盤決定 → 隔日開盤成交（真實可執行的時序，見 SLIPPAGE 上方說明）
    pending_entries: list[str] = []
    pending_exits: list[tuple[str, str]] = []   # (stock_id, 出場原因)
    recent_wins: list[bool] = []   # 訊號品質偵測用（見下方 adaptive_throttle）：逐筆平倉勝負紀錄

    def _record_exit(sid, p, fill, d_exit, i_exit, reason):
        hold = i_exit - p["entry_i"]
        net_ret = net_return(p["entry_price"], fill)
        trades.append({"stock_id": sid, "entry_date": p["entry_date"], "exit_date": d_exit,
                       "ret": fill / p["entry_price"] - 1, "net_ret": net_ret,
                       "hold": hold, "reason": reason})
        recent_wins.append(net_ret > 0)

    for i, d in enumerate(sim_dates):
        bull = True if regime_bull is None else bool(regime_bull.get(d, True))
        day_cfg = cfg if bull else bear_cfg

        # 1) 執行昨日決定的出場 —— 今日開盤價成交（扣單邊滑價）
        unfilled_exits = []
        for sid, reason in pending_exits:
            p = open_pos.get(sid)
            if p is None:
                continue
            op = val(opens, d, sid)
            if op is None or _limit_locked(d, sid):   # 停牌/無開盤，或跌停鎖死賣不掉 → 明日再試
                unfilled_exits.append((sid, reason))
                continue
            fill = op * (1 - slippage)
            _record_exit(sid, p, fill, d, i, reason)
            cash += p["shares"] * fill * (1 - FEE_RATE - TAX_RATE)
            del open_pos[sid]
        pending_exits = unfilled_exits

        # 2) 執行昨日決定的進場 —— 今日開盤價成交（加單邊滑價）；未成交即取消不追價
        for sid in pending_entries:
            if len(open_pos) >= max_open or sid in open_pos:
                continue
            op = val(opens, d, sid)
            if not op or _limit_locked(d, sid):       # 跌停鎖死同樣沒有真實對手盤成交
                continue
            fill = op * (1 + slippage)
            shares_by_cash = int(capital_per_slot // (fill * (1 + FEE_RATE)))
            avg_vol = val(data["_avg_volume_liq"], d, sid)
            shares_by_liq = (int(avg_vol * cfg.get("max_pct_of_avg_volume", 0.01))
                             if avg_vol else shares_by_cash)
            shares = min(shares_by_cash, shares_by_liq)
            if shares <= 0 or cash < shares * fill * (1 + FEE_RATE):
                continue
            cash -= shares * fill * (1 + FEE_RATE)
            open_pos[sid] = {"entry_date": d, "entry_price": fill, "peak": fill,
                             "entry_i": i, "shares": shares}
        pending_entries = []

        # 3) 依今日收盤評估出場規則 → 掛到明日開盤成交
        for sid, p in open_pos.items():
            close = val(closes, d, sid)
            if close is None:
                continue
            p["peak"] = max(p["peak"], close)
            ex, reason = decide_exit(p["entry_price"], p["peak"], close,
                                     val(ma5p, d, sid), val(ma20p, d, sid), i - p["entry_i"],
                                     cfg=day_cfg)
            if ex and not any(s == sid for s, _ in pending_exits):
                pending_exits.append((sid, reason))

        # 4) 再平衡日：依今日收盤評分選股 → 掛到明日開盤進場
        #    （持倉上限與 portfolio.record_entries() 的風控守門員一致）
        gi = pos_idx[d]
        market_ok = bull or not cfg.get("market_filter_block_entries", False)
        # 訊號品質偵測+動態縮手（見 _adaptive_throttle_blocked 註解）：出場規則不受
        # 影響，已有部位照樣正常出場，只是暫停再加碼新倉。
        entry_ok = market_ok and not _adaptive_throttle_blocked(recent_wins, cfg)
        if entry_ok and (gi - start_i) % rebalance == 0:
            free_slots = max_open - len(open_pos) - len(pending_entries) + len(pending_exits)
            if free_slots > 0:
                hot = (_hot_sectors_asof(data, d, top_n=cfg["hot_sectors_top_n"])
                       if cfg.get("use_hot_sector_gate", True) else None)
                cap = cfg.get("sector_exposure_cap")
                sid_to_inds = data.get("_sid_to_inds") or {}
                # 族群曝險上限（P3決策3）：cap給定時候選池要比 top_n 寬，被上限擋掉
                # 的名額才有下一名可以遞補，不然只是少買，不是真的分散。
                pool_n = max(top_n * 4, 20) if (cap and sid_to_inds) else top_n
                candidates = _candidates_asof(data, d, hot, top_n=pool_n, cfg=cfg)

                if cap and sid_to_inds:
                    max_per_sector = max(1, round(max_open * cap))
                    sector_counts = Counter()
                    for s2 in list(open_pos.keys()) + pending_entries:
                        for ind in sid_to_inds.get(s2, ()):
                            sector_counts[ind] += 1
                    picked, n_picked = [], 0
                    for sid in candidates:
                        if n_picked >= top_n:
                            break
                        if sid in open_pos or sid in pending_entries:
                            continue
                        inds = sid_to_inds.get(sid)
                        if inds and any(sector_counts[i] >= max_per_sector for i in inds):
                            continue   # 這檔會讓某個族群超過上限，跳過換下一名
                        picked.append(sid)
                        n_picked += 1
                        for i in (inds or ()):
                            sector_counts[i] += 1
                    candidates = picked
                else:
                    candidates = candidates[:top_n]

                for sid in candidates:
                    if free_slots <= 0:
                        break
                    if sid in open_pos or sid in pending_entries:
                        continue
                    pending_entries.append(sid)
                    free_slots -= 1

        mkt_val = sum(p["shares"] * (val(closes, d, sid) or p["peak"]) for sid, p in open_pos.items())
        nav_curve.append((d, cash + mkt_val))

    # 期末仍持有者，以最後一天收盤平倉計入（無隔日開盤可用）
    last = sim_dates[-1]
    for sid, p in open_pos.items():
        close = val(closes, last, sid)
        if close:
            _record_exit(sid, p, close * (1 - slippage), last, len(sim_dates) - 1, "回測結束平倉")

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
        f"  註：今日收盤算訊號 → 隔日開盤成交（單邊滑價 {SLIPPAGE*100:.2f}%）；",
        "      淨報酬另含手續費(買賣)與證交稅(賣出)。滑價為假設值，待實盤成交回報校準。",
        "  調 agent/strategy.py 的參數後重跑此回測，即可比較買賣邏輯優劣。",
        "=" * 66,
    ]
    print("\n".join(l for l in lines if l))
    logger.info("回測完成")


if __name__ == "__main__":
    run_backtest()
