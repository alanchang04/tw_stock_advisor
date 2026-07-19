"""
research/factor_lab.py

SPEC_QUANT_UPGRADE.md P1：逐因子 IC / 十分位數分析框架。

核心立場（跟現行系統的根本方法論差異）：現行系統只量測過「一整套8因子加權公式」的
回測總報酬，從沒單獨驗證過任何一個因子本身有沒有預測力——回測贏了不知道為什麼贏，
輸了不知道為什麼輸。這裡反過來做：對每個因子單獨問「這個因子的值，能不能預測未來
報酬？」用兩個業界標準工具：

  IC（資訊係數）：因子橫斷面排名 vs 未來N日報酬的 Spearman 相關係數，逐日計算。
    IC均值/IC標準差 = ICIR，是「因子有沒有穩定預測力」的核心指標（不是回測報酬）。
  十分位數分析：每天依因子值分10組，看第10組(最高)-第1組(最低)的未來報酬價差
    是否單調、穩定——比IC更直觀，能看出因子是「線性有效」還是「只有極端值有效」。

全部函式操作「日期×股票」的 pivot 矩陣（跟 agent/strategy.py 的既有慣例一致），
用 `agent.backtest._load_parquet()` 讀本機10年歷史資料，不需要 DB。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════════
#  核心：未來報酬矩陣
# ══════════════════════════════════════════════════════════════════
def forward_returns(closes: pd.DataFrame, n: int) -> pd.DataFrame:
    """closes（日期×股票，建議先用 total_return_adjust 還原過）→ 未來n個交易日報酬矩陣。"""
    return closes.shift(-n) / closes - 1


# ══════════════════════════════════════════════════════════════════
#  IC（資訊係數）
# ══════════════════════════════════════════════════════════════════
def compute_ic(factor: pd.DataFrame, fwd_ret: pd.DataFrame, method: str = "spearman") -> pd.Series:
    """
    逐日橫斷面相關係數（factor 當天的值 vs fwd_ret 當天的未來報酬）。
    回傳日期索引的 IC 序列（NaN 的日期代表當天有效樣本不足或無重疊股票）。
    """
    idx = factor.index.intersection(fwd_ret.index)
    cols = factor.columns.intersection(fwd_ret.columns)
    f = factor.loc[idx, cols]
    r = fwd_ret.loc[idx, cols]
    return f.corrwith(r, axis=1, method=method)


def ic_summary(ic: pd.Series, min_days: int = 30) -> dict:
    """
    IC 序列 → 摘要統計。ic_mean/ic_std/icir 是核心；icir 業界經驗法則：
    >0.5 已算不錯的因子，>1.0 是很強的因子（注意這是「日頻ICIR」不是年化，
    不同研究對這個門檻的定義常有出入，這裡只提供原始數字，判斷交給人）。
    """
    ic = ic.dropna()
    n = len(ic)
    if n < min_days:
        return {"n_days": n, "ic_mean": None, "ic_std": None, "icir": None,
               "hit_rate": None, "t_stat": None, "note": f"樣本天數<{min_days}，不足以下結論"}
    ic_mean, ic_std = float(ic.mean()), float(ic.std())
    icir = ic_mean / ic_std if ic_std > 0 else None
    hit_rate = float((ic > 0).mean()) if ic_mean >= 0 else float((ic < 0).mean())
    t_stat = ic_mean / (ic_std / np.sqrt(n)) if ic_std > 0 else None
    return {"n_days": n, "ic_mean": round(ic_mean, 4), "ic_std": round(ic_std, 4),
           "icir": round(icir, 4) if icir is not None else None,
           "hit_rate": round(hit_rate, 4), "t_stat": round(t_stat, 2) if t_stat is not None else None}


# ══════════════════════════════════════════════════════════════════
#  十分位數分析
# ══════════════════════════════════════════════════════════════════
def decile_returns(factor: pd.DataFrame, fwd_ret: pd.DataFrame, n_deciles: int = 10,
                   min_per_day: int = 30) -> pd.DataFrame:
    """
    每天依因子值分 n_deciles 組（0=最低，n_deciles-1=最高），逐日算各組平均未來報酬，
    最後對所有日期取平均。回傳 DataFrame(index=decile, columns=['mean_ret','n_obs'])。
    當天有效樣本 < min_per_day 時跳過該日（分組會太粗，統計沒意義）。
    """
    idx = factor.index.intersection(fwd_ret.index)
    records = []
    for d in idx:
        f = factor.loc[d].dropna()
        if len(f) < min_per_day:
            continue
        r = fwd_ret.loc[d]
        common = f.index.intersection(r.dropna().index)
        f = f[common]
        if len(f) < min_per_day:
            continue
        try:
            deciles = pd.qcut(f.rank(method="first"), n_deciles, labels=False, duplicates="drop")
        except ValueError:
            continue
        for dec, sids in f.groupby(deciles).groups.items():
            records.append({"decile": int(dec), "ret": r.loc[sids].mean(), "n": len(sids)})
    if not records:
        return pd.DataFrame(columns=["mean_ret", "n_obs"])
    df = pd.DataFrame(records)
    out = df.groupby("decile").agg(mean_ret=("ret", "mean"), n_obs=("n", "sum"))
    return out


def decile_spread(decile_df: pd.DataFrame) -> float | None:
    """最高組 - 最低組 平均未來報酬價差（因子有效性最直觀的單一數字）。"""
    if decile_df.empty:
        return None
    lo, hi = decile_df.index.min(), decile_df.index.max()
    if lo == hi:
        return None
    return float(decile_df.loc[hi, "mean_ret"] - decile_df.loc[lo, "mean_ret"])


def decile_monotonic(decile_df: pd.DataFrame) -> bool:
    """組別報酬是否單調遞增（因子「線性有效」的判準；只在極端組有效則不會單調）。"""
    if len(decile_df) < 3:
        return False
    vals = decile_df.sort_index()["mean_ret"].values
    return bool(np.all(np.diff(vals) >= -1e-9))   # 容忍極小浮點誤差


# ══════════════════════════════════════════════════════════════════
#  事件研究：累積異常報酬（CAR）曲線
# ══════════════════════════════════════════════════════════════════
def event_car(closes: pd.DataFrame, event_mask: pd.DataFrame, bench_ret: pd.Series,
             horizons: list[int] = None) -> pd.DataFrame:
    """
    事件研究：event_mask（日期×股票的布林矩陣，True=該日該股觸發事件，如投信新進場）
    發生後 +1~+N 日的累積異常報酬（相對 bench_ret 大盤日報酬）。
    回傳 DataFrame(index=horizon, columns=['mean_car','n_events'])。
    """
    horizons = horizons or [1, 3, 5, 10, 20, 40, 60]
    dates = closes.index
    daily_ret = closes.pct_change()
    excess = daily_ret.sub(bench_ret.reindex(dates), axis=0)

    event_days = []
    for d in dates:
        if d not in event_mask.index:
            continue
        hit = event_mask.loc[d]
        for sid in hit[hit].index:
            event_days.append((d, sid))

    if not event_days:
        return pd.DataFrame(columns=["mean_car", "n_events"])

    pos = {d: i for i, d in enumerate(dates)}
    results = {h: [] for h in horizons}
    for d, sid in event_days:
        if sid not in excess.columns or d not in pos:
            continue
        i0 = pos[d]
        for h in horizons:
            i1 = i0 + h
            if i1 >= len(dates):
                continue
            car = excess[sid].iloc[i0 + 1:i1 + 1].sum()   # +1日起算，事件日當天不算（訊號收盤後才知道）
            if pd.notna(car):
                results[h].append(car)

    rows = []
    for h in horizons:
        vals = results[h]
        rows.append({"horizon": h, "mean_car": float(np.mean(vals)) if vals else None,
                    "n_events": len(vals)})
    return pd.DataFrame(rows).set_index("horizon")


# ══════════════════════════════════════════════════════════════════
#  單因子完整報告（IC + 十分位數，多個時間窗一次跑）
# ══════════════════════════════════════════════════════════════════
def factor_report(name: str, factor: pd.DataFrame, closes: pd.DataFrame,
                  horizons: list[int] = None) -> dict:
    horizons = horizons or [5, 10, 20, 60]
    out = {"factor": name, "horizons": {}}
    for h in horizons:
        fwd = forward_returns(closes, h)
        ic = compute_ic(factor, fwd)
        dec = decile_returns(factor, fwd)
        out["horizons"][h] = {
            "ic": ic_summary(ic),
            "decile_spread": decile_spread(dec),
            "decile_monotonic": decile_monotonic(dec) if not dec.empty else None,
            "decile_table": dec.to_dict("index") if not dec.empty else {},
        }
    return out
