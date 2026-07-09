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
from agent.strategy import STRATEGY, score_candidates, decide_exit, split_adjust

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
def _load() -> dict:
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


def _consecutive_true(df_bool: pd.DataFrame) -> pd.DataFrame:
    """逐欄計算「至當列為止連續 True 的天數」（向量化，供多頭排列/投信連買用）。"""
    csum = df_bool.cumsum()
    reset = csum.where(~df_bool).ffill().fillna(0)
    return (csum - reset).where(df_bool, 0)


# ── 趨勢/題材因子矩陣（一次算好，供每個再平衡日 O(1) 查表）──────────
#   這些是 2026-07-09 選股重構新增的「可回測」因子：
#   相對強度、多頭排列持續、投信連買——取代原本只認「今天剛發生訊號」的
#   單日技術面 event flags（見 docs/TRADING_LOGIC.md 第十、十一節）。
MIN_INVEST_STREAK_LOTS = 100   # 投信連買至少累計 100 張才算數（沿用 smart_money 的量體下限）

def _precompute_factors(data: dict) -> None:
    closes = data["_closes"]
    idx, cols = closes.index, closes.columns
    tech = data["tech"]
    def _piv(v):   # 對齊到 closes 的 index/columns，避免三個 MA 欄位集不同無法比較
        return tech.pivot_table(index="trade_date", columns="stock_id", values=v) \
                   .reindex(index=idx, columns=cols)
    ma5p, ma20p, ma60p = _piv("ma5"), _piv("ma20"), _piv("ma60")

    # 相對強度：20 日報酬在「全市場當日」的百分位排名（0~1，越強越高）
    ret20 = closes / closes.shift(20) - 1
    data["_rs20"] = ret20.rank(axis=1, pct=True)

    # 多頭排列持續天數：MA5>MA20>MA60 連續成立幾天（抓「已確認的強勢趨勢」，非今天剛交叉）
    stack = ((ma5p > ma20p) & (ma20p > ma60p)).fillna(False)
    data["_stack_days"] = _consecutive_true(stack)

    # 投信連買：連續淨買超天數（需累計量體達門檻才算，濾掉一天只買 2~3 張的雜訊）
    invest = data["inst"].pivot_table(index="trade_date", columns="stock_id", values="invest_net")
    invest = invest.reindex(index=idx, columns=cols)
    is_pos = invest > 0
    streak = _consecutive_true(is_pos.fillna(False))
    pos_only = invest.where(is_pos, 0.0)
    csum = pos_only.cumsum()
    reset_val = csum.where(~is_pos.fillna(False)).ffill().fillna(0.0)
    streak_lots = ((csum - reset_val).where(is_pos, 0.0)) / 1000.0   # 股→張
    data["_inv_streak"] = streak.where(streak_lots >= MIN_INVEST_STREAK_LOTS, 0)


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
                 start_date=None, end_date=None):
    """
    模擬實際操作：再平衡日依評分進場，每日依 strategy 出場規則平倉，
    計算每筆完整交易(round-trip)的報酬、勝率、平均持有天數，並與買進持有比較。

    cfg:   出場規則參數 dict（預設 STRATEGY）——消融測試用
    data:  預載資料（_load() 的回傳）——多次回測共用，避免重複查 DB
    quiet: True 時不印報告，只回傳交易 DataFrame
    start_date/end_date: 限制回測區間（walk-forward 分段用；datetime.date）
    """
    cfg = cfg or STRATEGY
    top_n = top_n or cfg["pick_top_n"]
    if not quiet:
        logger.info("=== 回測開始（進場評分 + strategy 出場規則，不含 LLM）===")
    if data is None:
        data = _load()
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
    if mf_sid in closes.columns:
        mkt_adj = split_adjust(closes[mf_sid])
        p0, p1 = mkt_adj.get(sim_dates[0]), mkt_adj.get(last)
        if p0 and p1 and p0 > 0:
            bench_0050 = p1 / p0 - 1
    nav = pd.Series({d: v for d, v in nav_curve}).sort_index()
    # 摘要指標掛在 df.attrs，供比較腳本程式化讀取（不必解析印出的文字）
    tdf.attrs.update({
        "nav_total_ret": nav.iloc[-1] / cfg["capital"] - 1,
        "nav_mdd": ((nav - nav.cummax()) / nav.cummax()).min(),
        "bench_0050": bench_0050,
        "bench_eqw": bench.mean(),
        "net_win": net_win_rate(tdf),
        "start": sim_dates[0], "end": last,
    })
    if not quiet:
        _report_roundtrip(tdf, bench, bench_0050, nav, cfg["capital"], sim_dates, top_n, rebalance)
    return tdf


def net_win_rate(tdf) -> float:
    nets = tdf["net_ret"] if "net_ret" in tdf.columns else tdf["ret"]
    return float((nets > 0).mean())


def _report_roundtrip(tdf, bench, bench_0050, nav, capital, sim_dates, top_n, rebalance):
    rets = tdf["ret"]
    nets = tdf["net_ret"] if "net_ret" in tdf.columns else rets
    win = (rets > 0).mean()
    net_win = (nets > 0).mean()

    gross_win  = nets[nets > 0].sum()
    gross_loss = abs(nets[nets <= 0].sum())
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    sharpe_pt = nets.mean() / nets.std() if nets.std() > 0 else 0.0   # 每筆 Sharpe

    # 真實資金曲線（受 max_open_positions 與每格等額資金限制，可與買進持有做同期間比較）
    port_total_ret = nav.iloc[-1] / capital - 1
    mdd = ((nav - nav.cummax()) / nav.cummax()).min() * 100   # 真實最大回撤（% of NAV，不會超過-100%）

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
        "  【整個回測期間的資金成長（同一時間基準，才能公平跟大盤比較）】",
        f"  策略總報酬（{capital:,.0f} 元本金，同時持倉上限受限，逐日試算 NAV）："
        f"{port_total_ret*100:+.2f}%",
        f"  策略最大回撤（NAV 相對前高，非逐筆加總）：{mdd:.1f}%",
        f"  0050 同期實際報酬（已還原分割/併股，這才是一般講的「大盤」）："
        f"{bench_0050*100:+.2f}%" if bench_0050 is not None else "  0050 同期實際報酬：無資料",
        f"  策略總報酬 − 0050：{(port_total_ret - bench_0050)*100:+.2f}%" if bench_0050 is not None else "",
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
