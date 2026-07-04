"""
agent/backtest.py

回測「族群熱度 + 候選評分」選股邏輯（不含 LLM）。

做法：在歷史每個再平衡日，用「當天為止」可得的資料重現正式流程的選股
（族群輪動 → 候選篩選 → 取分數最高的前 N 檔），再用之後 5/10/20 個交易日的
收盤價算報酬，與大盤等權報酬比較，輸出勝率、平均報酬、超額報酬。

說明 / 限制：
  - 不含 LLM：LLM 為非決定性的再排序/解說層，無法重現；回測驗證的是底層量化訊號。
  - 收盤價對收盤價、未計滑價；「淨報酬」已計手續費(14.25bp×6折,買賣各一次)與證交稅(30bp,賣出)。
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
from agent.strategy import STRATEGY, score_candidates, decide_exit

# ── 交易成本（台股現股）──────────────────────────────────────────
FEE_RATE = 0.001425 * 0.6   # 券商手續費 14.25bp，一般電子下單 6 折（買賣各收一次）
TAX_RATE = 0.003            # 證交稅 30bp（僅賣出時收）


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
                SELECT stock_id, trade_date, ma5, ma20, rsi14, macd_hist,
                       signal_ma_cross, signal_breakout
                FROM technical_indicators
            """)).fetchall(),
            columns=["stock_id", "trade_date", "ma5", "ma20", "rsi14",
                     "macd_hist", "signal_ma_cross", "signal_breakout"],
        )
        inst = pd.DataFrame(
            s.execute(text("""
                SELECT stock_id, trade_date, total_net, foreign_net
                FROM institutional_trading
            """)).fetchall(),
            columns=["stock_id", "trade_date", "total_net", "foreign_net"],
        )
        imap = pd.DataFrame(
            s.execute(text("SELECT stock_id, industry_code FROM stock_industry_map")).fetchall(),
            columns=["stock_id", "industry_code"],
        )
        inds = pd.DataFrame(
            s.execute(text("SELECT code, name_zh FROM industries")).fetchall(),
            columns=["industry_code", "name_zh"],
        )

    # 型別整理：DB 的 NUMERIC → float
    for df, cols in [
        (prices, ["close", "volume", "change_pct"]),
        (tech, ["ma5", "ma20", "rsi14", "macd_hist", "signal_ma_cross", "signal_breakout"]),
        (inst, ["total_net", "foreign_net"]),
    ]:
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for df in (prices, tech, inst):
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    return {"prices": prices, "tech": tech, "inst": inst, "imap": imap, "inds": inds}


def _normalize(s: pd.Series) -> pd.Series:
    rng = s.max() - s.min()
    return (s - s.min()) / rng if rng else pd.Series(0.5, index=s.index)


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
    if not industry_codes:
        return []
    cfg = cfg or STRATEGY
    pr = data["prices"]
    px = pr[pr["trade_date"] == d]
    tk = data["tech"][data["tech"]["trade_date"] == d]
    if px.empty or tk.empty:
        return []

    # 只取熱門族群內的股票
    sids = set(data["imap"][data["imap"]["industry_code"].isin(industry_codes)]["stock_id"])
    df = px[px["stock_id"].isin(sids)].merge(
        tk, on=["stock_id", "trade_date"], how="inner")

    it = data["inst"]
    iday = it[it["trade_date"] == d][["stock_id", "total_net", "foreign_net"]] \
        .rename(columns={"total_net": "inst_net"})
    df = df.merge(iday, on="stock_id", how="left")
    df[["inst_net", "foreign_net"]] = df[["inst_net", "foreign_net"]].fillna(0.0)

    # 與正式 get_candidate_stocks 相同的過濾條件
    df = df[(df["close"] >= MIN_CLOSE) & (df["volume"] >= MIN_VOLUME)
            & (df["rsi14"] >= MIN_RSI) & (df["rsi14"] <= MAX_RSI)
            & df["ma5"].notna() & df["ma20"].notna()]
    if df.empty:
        return []

    # 60 日動能（用預先建好的 closes pivot；與正式選股的 mom60 對應）
    piv = data.get("_closes")
    if piv is not None and d in piv.index:
        i = piv.index.get_loc(d)
        if i >= 60:
            base = piv.iloc[i - 60]
            cur  = piv.iloc[i]
            mom = (cur / base - 1)
            df = df.copy()
            df["mom60"] = df["stock_id"].map(mom)

    # 與正式選股共用 strategy.score_candidates（教授改權重，回測自動跟著變）
    df["score"] = score_candidates(df, cfg)
    return df.sort_values("score", ascending=False).head(top_n)["stock_id"].tolist()


# ── 主回測：真實進出場（round-trip）─────────────────────────────
def run_backtest(top_n=None, rebalance=5, cfg=None, data=None, quiet=False):
    """
    模擬實際操作：再平衡日依評分進場，每日依 strategy 出場規則平倉，
    計算每筆完整交易(round-trip)的報酬、勝率、平均持有天數，並與買進持有比較。

    cfg:   出場規則參數 dict（預設 STRATEGY）——消融測試用
    data:  預載資料（_load() 的回傳）——多次回測共用，避免重複查 DB
    quiet: True 時不印報告，只回傳交易 DataFrame
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

    # 市場濾網：大盤代理收盤 vs 其 60 日均線（逐日 bull/bear）
    regime_bull = None
    mf_sid = cfg.get("market_filter_stock", "0050")
    if cfg.get("market_filter") and mf_sid in closes.columns:
        mkt = closes[mf_sid]
        regime_bull = (mkt >= mkt.rolling(60, min_periods=30).mean()).fillna(True)
    bear_cfg = ({**cfg, "exit_on_death_cross": True}
                if cfg.get("bear_reenable_death_cross") else cfg)

    tech = data["tech"]
    ma5p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma5")
    ma20p = tech.pivot_table(index="trade_date", columns="stock_id", values="ma20")
    dates = sorted(closes.index)
    pos_idx = {d: i for i, d in enumerate(dates)}

    inst_start = data["inst"]["trade_date"].min()
    start_i = next((i for i, d in enumerate(dates) if d >= inst_start), 0)
    sim_dates = dates[start_i:]
    if len(sim_dates) < 10:
        logger.error("可回測區間太短"); return

    def val(piv, d, sid):
        try:
            v = piv.at[d, sid]
            return None if pd.isna(v) else float(v)
        except KeyError:
            return None

    open_pos = {}     # stock_id -> {entry_date, entry_price, peak}
    trades = []       # 完整交易紀錄
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
                del open_pos[sid]

        # 2) 再平衡日進場（未持有者才買；濾網設 block_entries 時空頭不開新倉）
        gi = pos_idx[d]
        entry_ok = bull or not cfg.get("market_filter_block_entries", False)
        if entry_ok and (gi - start_i) % rebalance == 0:
            hot = _hot_sectors_asof(data, d, top_n=cfg["hot_sectors_top_n"])
            for sid in _candidates_asof(data, d, hot, top_n=top_n, cfg=cfg):
                if sid in open_pos:
                    continue
                price = val(closes, d, sid)
                if price:
                    open_pos[sid] = {"entry_date": d, "entry_price": price, "peak": price, "entry_i": i}

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
    if not quiet:
        # 買進持有基準：同區間所有股票等權報酬
        base, fin = closes.loc[sim_dates[0]], closes.loc[last]
        common = base.dropna().index.intersection(fin.dropna().index)
        bench = (fin[common] / base[common] - 1)
        bench = bench[base[common] > 0]
        _report_roundtrip(tdf, bench, sim_dates, top_n, rebalance)
    return tdf


def _report_roundtrip(tdf, bench, sim_dates, top_n, rebalance):
    rets = tdf["ret"]
    nets = tdf["net_ret"] if "net_ret" in tdf.columns else rets
    win = (rets > 0).mean()
    net_win = (nets > 0).mean()
    # 出場原因分布
    by_reason = tdf.groupby("reason")["ret"].agg(["count", "mean"]).sort_values("count", ascending=False)
    lines = [
        "",
        "=" * 66,
        "  回測結果：實際進出場模擬（進場評分 + strategy 出場規則，不含 LLM）",
        "=" * 66,
        f"  區間：{sim_dates[0]} ~ {sim_dates[-1]}（每 {rebalance} 交易日再平衡，每次最多 {top_n} 檔）",
        f"  完整交易數：{len(tdf)}",
        f"  毛報酬：勝率 {win*100:.1f}%   平均 {rets.mean()*100:+.2f}%   中位數 {rets.median()*100:+.2f}%",
        f"  淨報酬：勝率 {net_win*100:.1f}%   平均 {nets.mean()*100:+.2f}%   （含手續費6折+證交稅，每筆約 -{(rets.mean()-nets.mean())*100:.2f}%）",
        f"  最佳：{rets.max()*100:+.1f}%   最差：{rets.min()*100:+.1f}%",
        f"  平均持有天數：{tdf['hold'].mean():.1f} 交易日",
        f"  大盤(等權買進持有)同期：{bench.mean()*100:+.2f}%",
        f"  選股淨報酬 − 大盤：{(nets.mean()-bench.mean())*100:+.2f}%",
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
    print("\n".join(lines))
    logger.info("回測完成")


if __name__ == "__main__":
    run_backtest()
