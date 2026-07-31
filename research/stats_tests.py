"""
SPEC_QUANT_UPGRADE §4.4 統計檢定 + §4.2 的 deflated Sharpe。

規格書 §4.4 原話：
    - 交易級 bootstrap：平均報酬的 95% 信賴區間（現在連標準誤都沒報）
    - 蒙地卡羅重排交易序列 → 回撤分佈的 95 分位（倉位上限應該由**最壞回撤分佈**
      決定，不是由歷史單一路徑的 -26.7% 決定）
    - 對 0050 含息的超額報酬 t 檢定
§4.2 原話（登記簿的目的）：
    - 誠實面對總嘗試次數，對最終 Sharpe 打折（deflated Sharpe 概念）

**為什麼這是整份規格書裡最關鍵的一項**：目前所有結論都建立在「+328% 這個點估計」
上，但 483 筆交易裡有 4 筆扛著約 67% 的毛獲利。厚尾 + 高度集中的分布下，點估計
幾乎沒有資訊量——真正該問的是「這個數字的信賴區間有沒有跨過 0」。

所有函式都是純函式（吃 array-like、回 dict），方便測試也方便被回測報告引用。
"""
from __future__ import annotations

import math

import numpy as np
from scipy import stats

#: Euler–Mascheroni 常數，算「N 次試驗下期望最大 Sharpe」用
_EULER = 0.5772156649015329


def _business_daily_nav(values):
    """Normalize irregular dated NAV observations before daily statistics."""
    import pandas as pd
    s = pd.Series(values).dropna().sort_index()
    if len(s) < 2:
        return s
    try:
        s.index = pd.to_datetime(s.index)
        return s.reindex(pd.bdate_range(s.index.min(), s.index.max())).ffill()
    except (TypeError, ValueError, OverflowError):
        return s


# ══════════════════════════════════════════════════════════════════
#  1. 交易級 bootstrap：平均報酬的信賴區間
# ══════════════════════════════════════════════════════════════════
def bootstrap_mean_ci(returns, n_boot: int = 10000, alpha: float = 0.05,
                      seed: int = 42) -> dict:
    """
    對逐筆交易報酬做有放回重抽，回傳平均報酬的 (1-alpha) 信賴區間。

    為什麼不用常態近似的標準誤：交易報酬嚴重右偏（少數幾筆 +100%~+700%），
    常態假設下的 t 區間會嚴重低估不確定性。bootstrap 不假設分布形狀。
    """
    r = np.asarray([x for x in returns if x is not None and not np.isnan(x)], dtype=float)
    n = len(r)
    if n < 2:
        return {"n": n, "mean": float(r[0]) if n else float("nan"),
                "ci_low": float("nan"), "ci_high": float("nan"),
                "p_mean_le_zero": float("nan"), "significant": False}
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n_boot, n), replace=True).mean(axis=1)
    lo, hi = np.percentile(means, [alpha / 2 * 100, (1 - alpha / 2) * 100])
    return {
        "n": n,
        "mean": float(r.mean()),
        "ci_low": float(lo),
        "ci_high": float(hi),
        # 重抽分布中「平均報酬 ≤ 0」的比例＝單尾 p 值的 bootstrap 版
        "p_mean_le_zero": float((means <= 0).mean()),
        "significant": bool(lo > 0),      # 信賴區間整段在 0 以上才算顯著
    }


# ══════════════════════════════════════════════════════════════════
#  2. 蒙地卡羅重排交易序列 → 回撤分佈
# ══════════════════════════════════════════════════════════════════
def monte_carlo_mdd(returns, n_sims: int = 10000, position_weight: float = 0.1,
                    seed: int = 42) -> dict:
    """
    把同一批交易報酬重新洗牌，重建權益曲線，看最大回撤的分布。

    回答的問題是：「同樣這些交易，只是發生順序不同，回撤可能有多糟？」
    歷史那條 -27.4% 只是眾多可能路徑中的一條，用它決定倉位上限會低估風險。

    position_weight：每筆交易佔總資金的比重（預設 0.1 ＝ max_open 10 格等額）。
    **這是近似**：真實部位會重疊、且資金會複利成長，這裡假設逐筆依序、等權投入。
    重排本身也假設交易報酬彼此獨立（同一波行情裡的多筆部位其實相關），
    所以結果偏向「低估尾部風險」——當成下界看，不是精確值。
    """
    r = np.asarray([x for x in returns if x is not None and not np.isnan(x)], dtype=float)
    n = len(r)
    if n < 2:
        return {"n": n, "mdd_median": float("nan"), "mdd_p95": float("nan"),
                "mdd_p99": float("nan"), "mdd_worst": float("nan")}
    rng = np.random.default_rng(seed)
    scaled = r * position_weight
    mdds = np.empty(n_sims)
    for i in range(n_sims):
        eq = np.cumprod(1.0 + rng.permutation(scaled))
        peak = np.maximum.accumulate(eq)
        mdds[i] = (eq / peak - 1.0).min()
    return {
        "n": n,
        "position_weight": position_weight,
        "mdd_median": float(np.percentile(mdds, 50)),
        "mdd_p95": float(np.percentile(mdds, 5)),    # 回撤是負數，最壞的 5% 在左尾
        "mdd_p99": float(np.percentile(mdds, 1)),
        "mdd_worst": float(mdds.min()),
    }


# ══════════════════════════════════════════════════════════════════
#  3. 對 0050 的超額報酬 t 檢定
# ══════════════════════════════════════════════════════════════════
def excess_return_ttest(nav_strategy, nav_bench, periods_per_year: int = 252) -> dict:
    """
    逐日超額報酬（策略 - 0050）的單樣本 t 檢定，檢定平均是否顯著不等於 0。

    nav_* 接受 dict{date: value} 或任何可轉成 pandas Series 的東西——
    run_backtest 的 attrs 存的就是 dict（存 Series 會讓 pandas concat 炸掉）。

    ⚠️ 已知限制：日報酬有自我相關與波動叢聚，標準 t 檢定會**高估**顯著性。
    這裡照規格書做標準版本，但解讀時要記得 t 值偏樂觀（真正嚴謹要用 Newey-West）。
    """
    import pandas as pd

    a = _business_daily_nav(nav_strategy)
    b = _business_daily_nav(nav_bench) if nav_bench is not None else None
    if b is None or len(a) < 3:
        return {"n": len(a), "t_stat": float("nan"), "p_value": float("nan"),
                "ann_excess": float("nan"), "significant": False}
    idx = a.index.intersection(b.index)
    ra, rb = a.loc[idx].pct_change().dropna(), b.loc[idx].pct_change().dropna()
    idx2 = ra.index.intersection(rb.index)
    d = (ra.loc[idx2] - rb.loc[idx2]).to_numpy(dtype=float)
    d = d[~np.isnan(d)]
    if len(d) < 3:
        return {"n": len(d), "t_stat": float("nan"), "p_value": float("nan"),
                "ann_excess": float("nan"), "significant": False}
    t_stat, p_value = stats.ttest_1samp(d, 0.0)
    return {
        "n": int(len(d)),
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "mean_daily_excess": float(d.mean()),
        "ann_excess": float(d.mean() * periods_per_year),
        "significant": bool(p_value < 0.05),
    }


# ══════════════════════════════════════════════════════════════════
#  4. Deflated Sharpe（§4.2：對總嘗試次數打折）
# ══════════════════════════════════════════════════════════════════
def expected_max_sharpe(n_trials: int, sharpe_std: float) -> float:
    """
    在「真實 Sharpe 全為 0」的虛無假設下，試 N 次後**期望看到的最大 Sharpe**。

    Bailey & López de Prado (2014)。直覺：試越多次，純運氣也能刷出越高的數字——
    這就是「+328% 是從 ≳58 個變體裡挑出來的」為什麼必須打折。
    """
    if n_trials < 2 or sharpe_std <= 0:
        return 0.0
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sharpe_std * ((1.0 - _EULER) * z1 + _EULER * z2))


def deflated_sharpe(returns, n_trials: int, sharpe_std_ann: float = 0.5,
                    periods_per_year: int = 252) -> dict:
    """
    把「試了 N 次」的選擇偏誤扣掉之後，Sharpe 仍顯著的機率。

    returns：逐期（這裡用逐日）報酬序列。
    n_trials：總共試過幾個變體——取自 research/EXPERIMENTS.md 的誠實計數。
    sharpe_std_ann：各變體**年化** Sharpe 的標準差。變體之間差異越大，純運氣能刷出
                的最大值越高，門檻越嚴。結果對這個參數敏感，所以請用
                `deflated_sharpe_sensitivity()` 報一個範圍，別只報單一數字。

    DSR = Φ( (SR - SR0)·√(n-1) / √(1 - γ3·SR + (γ4-1)/4·SR²) )

    ⚠️ **單位**：公式裡 SR 與 SR0 都必須是「每期」（這裡＝每日）單位。
    2026-07-24 第一版把年化的 sharpe_std 直接餵進去，算出「純運氣門檻 18.5」這種
    不可能的數字（年化 Sharpe 18.5），DSR 自然恆為 0。這裡先把 sharpe_std_ann
    除以 √periods_per_year 轉成每期單位再算。
    """
    r = np.asarray([x for x in returns if x is not None and not np.isnan(x)], dtype=float)
    n = len(r)
    if n < 3 or r.std(ddof=1) == 0:
        return {"n": n, "sharpe_period": float("nan"), "sr_threshold": float("nan"),
                "dsr": float("nan"), "passes": False, "n_trials": n_trials}
    ann = math.sqrt(periods_per_year)
    sr = float(r.mean() / r.std(ddof=1))                 # 每期（日）Sharpe
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))        # 非超額峰度
    sr0 = expected_max_sharpe(n_trials, sharpe_std_ann / ann)   # ← 轉成每期單位
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return {"n": n, "sharpe_period": sr, "sr_threshold": sr0,
                "dsr": float("nan"), "passes": False, "n_trials": n_trials}
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    dsr = float(stats.norm.cdf(z))
    return {
        "n": n, "sharpe_period": sr, "sharpe_ann": sr * ann,
        "skew": skew, "kurtosis": kurt,
        "n_trials": n_trials, "sharpe_std_ann": sharpe_std_ann,
        "sr_threshold": sr0, "sr_threshold_ann": sr0 * ann,
        "dsr": dsr, "passes": bool(dsr >= 0.95),
    }


def deflated_sharpe_sensitivity(returns, n_trials: int,
                                stds=(0.1, 0.2, 0.3, 0.5),
                                periods_per_year: int = 252) -> list[dict]:
    """
    DSR 對 `sharpe_std_ann` 的敏感度。

    這個參數是「各變體年化 Sharpe 的離散程度」，我們沒有完整記錄（§4.2 登記簿是
    2026-07-24 才建的，先前 42 筆是補登、多數沒留 Sharpe），所以只能給範圍。
    照 §4.3「edge 必須是高原不是尖峰」的同一種精神：**結論若隨參數翻轉，
    就不能宣稱結論成立。**
    """
    return [deflated_sharpe(returns, n_trials, s, periods_per_year) for s in stds]


# ══════════════════════════════════════════════════════════════════
#  彙總 + 報告
# ══════════════════════════════════════════════════════════════════
def run_all(trade_returns, nav, nav_bench, n_trials: int,
            position_weight: float = 0.1) -> dict:
    """一次跑完 §4.4 三項 + §4.2 deflated Sharpe。"""
    import pandas as pd

    daily = _business_daily_nav(nav).pct_change().dropna().to_numpy(dtype=float) \
        if nav else np.array([])
    return {
        "bootstrap": bootstrap_mean_ci(trade_returns),
        "monte_carlo": monte_carlo_mdd(trade_returns, position_weight=position_weight),
        "ttest": excess_return_ttest(nav, nav_bench),
        "deflated_sharpe": deflated_sharpe(daily, n_trials),
        "dsr_sensitivity": deflated_sharpe_sensitivity(daily, n_trials),
    }


def format_report(res: dict, mdd_actual: float | None = None) -> str:
    """把 run_all 的結果排成報告文字（回測輸出用，§4.4「納入回測輸出」）。"""
    b, m, t, d = (res["bootstrap"], res["monte_carlo"], res["ttest"],
                  res["deflated_sharpe"])
    L = ["─" * 66, "統計檢定（SPEC §4.4 / §4.2）", "─" * 66]

    L.append(f"【交易級 bootstrap】{b['n']} 筆交易，平均淨報酬 {b['mean']*100:+.2f}%")
    L.append(f"  95% 信賴區間：[{b['ci_low']*100:+.2f}%, {b['ci_high']*100:+.2f}%]"
             f"　平均≤0 的機率 {b['p_mean_le_zero']*100:.1f}%")
    L.append(f"  → {'✅ 信賴區間整段在 0 以上' if b['significant'] else '❌ 信賴區間跨過 0：無法宣稱平均報酬顯著為正'}")

    L.append("")
    L.append(f"【蒙地卡羅回撤】重排 10000 次（每筆佔資金 {m['position_weight']*100:.0f}%）")
    L.append(f"  中位 {m['mdd_median']*100:.1f}%　95分位 {m['mdd_p95']*100:.1f}%　"
             f"99分位 {m['mdd_p99']*100:.1f}%　最壞 {m['mdd_worst']*100:.1f}%")
    if mdd_actual is not None:
        L.append(f"  歷史實際回撤 {mdd_actual*100:.1f}% —— "
                 f"倉位上限應由 95 分位而非這條單一路徑決定")

    L.append("")
    L.append(f"【對 0050 超額報酬 t 檢定】n={t['n']} 日")
    L.append(f"  年化超額 {t['ann_excess']*100:+.2f}%　t = {t['t_stat']:+.3f}　p = {t['p_value']:.4f}")
    L.append(f"  → {'✅ 顯著' if t['significant'] else '❌ 不顯著：無法拒絕「與 0050 無差異」'}"
             f"（註：日報酬自我相關會高估顯著性，t 值偏樂觀）")

    L.append("")
    L.append(f"【Deflated Sharpe】已試過 {d['n_trials']} 個變體，"
             f"實際 Sharpe(年化) {d.get('sharpe_ann', float('nan')):.3f}")
    sens = res.get("dsr_sensitivity")
    if sens:
        L.append("  變體Sharpe離散度   純運氣門檻(年化)      DSR    判決")
        for s in sens:
            L.append(f"    σ = {s['sharpe_std_ann']:.1f}          "
                     f"{s['sr_threshold_ann']:>8.3f}      {s['dsr']:>7.4f}   "
                     f"{'✅ 顯著' if s['passes'] else '❌ 不顯著'}")
        vs = {x["passes"] for x in sens}
        L.append("  → " + ("✅ 各種離散度假設下都顯著" if vs == {True}
                           else "❌ 各種假設下都不顯著" if vs == {False}
                           else "⚠️ **結論隨假設翻轉**——不能宣稱結論成立（同 §4.3 精神）"))
    else:
        L.append(f"  純運氣門檻 {d.get('sr_threshold_ann', float('nan')):.3f}　"
                 f"DSR = {d['dsr']:.4f} → "
                 f"{'✅ 顯著' if d['passes'] else '❌ 不顯著'}")
    L.append("─" * 66)
    return "\n".join(L)
