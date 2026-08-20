"""步驟 0：2014/2015 資料接縫稽核（**純資料一致性，不碰任何報酬預測**）。

要回答的問題
------------
2008~2026 的動能研究需要把**三份不同管線產生的價格資料**接起來：

    2008–2014 TWSE   data/research_versions/twse_prices_2005_2014_v1
    2008–2014 TPEx   data/research/tpex_history_2008_2014
    2015–2026 兩市    data/research

接縫在 **2015-01**。`mom_12_1` 的形成窗是 252 個交易日，因此 2015 年整年
到 2016 年初的決策日都會**跨接縫取形成期報酬**。若兩邊的還原慣例、單位或
證券代號對應不一致，那些月份的動能訊號就是垃圾——而且**看起來會很正常**。

判準只有一個：資料一致性
------------------------
**絕對不得**因為「某種接縫處理方式讓動能 IC 比較漂亮」而選擇它。
那會讓接縫清理本身變成模型選擇。本腳本因此**完全不計算未來報酬、
不計算任何動能訊號、不做任何預測性統計**。

門檻怎麼來
----------
不憑空設定。每一項比較都對照**同源基準線**：接縫日的統計量，
與同一份資料內部正常交易日的統計量相比。接縫若看起來像平常的一天，
就是乾淨的。

三級判決（事前寫死）
--------------------
🟢 ``clean``       無系統性 還原／單位／對應 問題 → 允許 2008–2026 統一面板
🟡 ``usable``      整體一致但接縫附近不可信 → 排除形成窗跨接縫的決策日
🔴 ``incompatible`` 系統性問題且無法隔離 → 不硬拼，分段描述
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OLD_TWSE = ROOT / "data/research_versions/twse_prices_2005_2014_v1/prices.parquet"
OLD_TPEX = ROOT / "data/research/tpex_history_2008_2014/prices.parquet"
NEW = ROOT / "data/research/prices.parquet"

# 含 capital_reduction／ex_right 的 TWSE 公司行動——**這是本稽核最重要的來源**。
# 第一版把路徑寫成 `data/corporate_actions.parquet`（不存在），又用
# `if exists()` 包住，於是整份被**靜默跳過**，稽核照樣印出 clean。
# 現在改為缺檔即拋錯（見 `require`）。九個 staging 版本內容相同（6,275 列）。
TWSE_ACTIONS = (ROOT / "data/research_versions/twse_corporate_actions_2005_2014"
                       "_staging_v4/corporate_actions.parquet")
TPEX_DIVIDENDS = ROOT / "data/research/tpex_history_2008_2014/dividend_events.parquet"
NEW_DIVIDENDS = ROOT / "data/research/dividend_events.parquet"

# mom_12_1 的形成窗：跳過最近 20 個交易日、回看 252 個交易日。
# 🟡 判決的排除長度必須用**這個**算，不能沿用 mom_6_1 的 120 日。
MOM_12_1_SKIP = 20
MOM_12_1_LOOKBACK = 252

PERCENTILES = (1, 5, 25, 50, 75, 95, 99)


def load_prices(path: Path, label: str) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame["stock_id"] = frame["stock_id"].astype(str)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["source"] = label
    return frame


def describe(series: pd.Series) -> dict:
    clean = series.dropna()
    if clean.empty:
        return {"n": 0}
    return {
        "n": int(clean.size),
        "mean_pct": round(float(clean.mean()) * 100, 4),
        "std_pct": round(float(clean.std()) * 100, 4),
        **{f"p{q}_pct": round(float(np.percentile(clean, q)) * 100, 4)
           for q in PERCENTILES},
        "abs_gt_20pct": int((clean.abs() > 0.20).sum()),
        "abs_gt_50pct": int((clean.abs() > 0.50).sum()),
        "share_abs_gt_20pct": round(float((clean.abs() > 0.20).mean()), 5),
    }


def one_step_returns(frame: pd.DataFrame, first: pd.Timestamp,
                     second: pd.Timestamp) -> pd.Series:
    """兩個交易日之間的原始收盤報酬，index 為 stock_id。"""
    a = frame[frame["trade_date"] == first].set_index("stock_id")["close"]
    b = frame[frame["trade_date"] == second].set_index("stock_id")["close"]
    shared = a.index.intersection(b.index)
    a, b = a.loc[shared], b.loc[shared]
    return (b / a.where(a > 0) - 1.0).dropna()


# ── 檢查 1：跨接縫報酬 vs 同源基準線 ────────────────────────────
def check_seam_returns(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    old_last = old["trade_date"].max()
    old_prev = sorted(old.loc[old["trade_date"] < old_last, "trade_date"].unique())[-1]
    new_first = new["trade_date"].min()
    new_next = sorted(new.loc[new["trade_date"] > new_first, "trade_date"].unique())[0]

    old_close = old[old["trade_date"] == old_last].set_index("stock_id")["close"]
    new_close = new[new["trade_date"] == new_first].set_index("stock_id")["close"]
    shared = old_close.index.intersection(new_close.index)
    seam = (new_close.loc[shared] / old_close.loc[shared].where(old_close > 0) - 1.0)
    seam = seam.dropna()

    # 同一批股票的成交量／成交值跨接縫比值。價格對得上不代表量的單位對得上，
    # 而流動性篩選與規模控制都吃這兩個欄位——它們歪掉一樣會毀掉研究。
    volume_ratio = {}
    for column in ("volume", "turnover"):
        if column in old.columns and column in new.columns:
            a = old[old["trade_date"] == old_last].set_index("stock_id")[column]
            b = new[new["trade_date"] == new_first].set_index("stock_id")[column]
            keys = a.index.intersection(b.index)
            ratio = (b.loc[keys] / a.loc[keys].where(a > 0)).replace(
                [np.inf, -np.inf], np.nan).dropna()
            volume_ratio[column] = {
                "median_new_over_old": round(float(ratio.median()), 4),
                "p5": round(float(np.percentile(ratio, 5)), 4),
                "p95": round(float(np.percentile(ratio, 95)), 4),
                "n": int(ratio.size),
            }

    extremes = (seam[seam.abs() > 0.20].sort_values(key=np.abs, ascending=False))
    return {
        "seam_from": str(old_last.date()),
        "seam_to": str(new_first.date()),
        "shared_securities": int(shared.size),
        "cross_seam_volume_ratio": volume_ratio,
        "cross_seam_volume_note": ("median ratio should sit near 1 in ORDER OF "
                                   "MAGNITUDE. A factor of 1000 here is the classic "
                                   "'numbers look fine but the unit changed' failure."),
        "price_limit_caveat": ("Taiwan had +/-7% daily limits in 2014 and +/-7% "
                               "then +/-10% from June 2015, so a single-day |R|>20% "
                               "is impossible without an adjustment artefact. "
                               "That makes the >20% count informative on its own "
                               "but makes comparing its RATE against a baseline "
                               "near-vacuous — read the percentiles instead."),
        "seam_step": describe(seam),
        "baseline_within_old": describe(one_step_returns(old, old_prev, old_last)),
        "baseline_within_new": describe(one_step_returns(new, new_first, new_next)),
        "note": ("the seam step is compared against ordinary one-day steps inside "
                 "each source. If the seam looks like an ordinary day, the price "
                 "level convention is consistent. Thresholds are NOT set a priori."),
        "extremes_abs_gt_20pct": [
            {"stock_id": sid,
             "return_pct": round(float(val) * 100, 3),
             "close_2014": round(float(old_close.loc[sid]), 3),
             "close_2015": round(float(new_close.loc[sid]), 3)}
            for sid, val in extremes.head(40).items()
        ],
    }


# ── 檢查 2：universe 連續性 ─────────────────────────────────────
def check_universe(old: pd.DataFrame, new: pd.DataFrame, sessions: int = 20) -> dict:
    old_days = sorted(old["trade_date"].unique())[-sessions:]
    new_days = sorted(new["trade_date"].unique())[:sessions]
    old_ids = set(old.loc[old["trade_date"].isin(old_days), "stock_id"])
    new_ids = set(new.loc[new["trade_date"].isin(new_days), "stock_id"])
    both = old_ids & new_ids
    return {
        "window_sessions": sessions,
        "old_only_count": len(old_ids - new_ids),
        "new_only_count": len(new_ids - old_ids),
        "intersection_count": len(both),
        "intersection_share_of_old": round(len(both) / len(old_ids), 4) if old_ids else None,
        "intersection_share_of_new": round(len(both) / len(new_ids), 4) if new_ids else None,
        "old_only_sample": sorted(old_ids - new_ids)[:30],
        "new_only_sample": sorted(new_ids - old_ids)[:30],
        "note": ("a real delisting/listing produces one-sided IDs legitimately; "
                 "a security-master mapping problem produces them in bulk. "
                 "The counts here do not distinguish the two on their own — "
                 "the ticker-format check below is what separates them."),
    }


# ── 檢查 3：schema 與單位 ───────────────────────────────────────
def check_schema(sources: dict[str, pd.DataFrame]) -> dict:
    report = {}
    for label, frame in sources.items():
        ids = frame["stock_id"]
        by_year = frame.groupby(frame["trade_date"].dt.year)["trade_date"].nunique()
        # **最有力的單位檢查**：turnover / volume 應該等於價格。
        # 比「中位數看起來差不多」強得多——它同時鎖住兩個欄位的單位，
        # 而且「數字都正常但倍率差 1000」正是它抓得到的東西。
        implied = None
        if {"volume", "turnover"} <= set(frame.columns):
            usable = frame[(frame["volume"] > 0) & (frame["turnover"] > 0)]
            implied = float((usable["turnover"] / usable["volume"]).median())
        report[label] = {
            "implied_price_from_turnover_over_volume": (
                round(implied, 4) if implied is not None else None),
            "implied_vs_median_close_ratio": (
                round(implied / float(frame["close"].median()), 4)
                if implied is not None else None),
            "non_four_digit_ticker_share": round(
                float((~ids.str.fullmatch(r"\d{4}")).mean()), 5),
            "columns": sorted(c for c in frame.columns if c != "source"),
            "dtypes": {c: str(frame[c].dtype) for c in ("close", "volume", "turnover")
                       if c in frame.columns},
            "median_close": round(float(frame["close"].median()), 4),
            "median_volume": (round(float(frame["volume"].median()), 1)
                              if "volume" in frame else None),
            "median_turnover": (round(float(frame["turnover"].median()), 1)
                                if "turnover" in frame else None),
            "ticker_lengths": {int(k): int(v) for k, v
                               in ids.str.len().value_counts().head(6).items()},
            "ticker_all_digits_share": round(float(ids.str.fullmatch(r"\d+").mean()), 4),
            "sessions_per_year": {int(k): int(v) for k, v in by_year.items()},
        }
    return report


# ── 檢查 4：公司行動邊界稽核 ────────────────────────────────────
def check_corporate_actions(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    """2014Q4 / 2015Q1 有公司行動的股票，其跨接縫原始報酬是否異常。

    **這一項是重點**：一般股票沒問題不代表 adjustment pipeline 沒問題。
    """
    def require(path: Path, label: str) -> pd.DataFrame:
        """缺檔就拋錯。**不得靜默跳過**——那正是第一版的失敗方式。"""
        if not path.exists():
            raise FileNotFoundError(
                f"公司行動來源缺失：{label} → {path}\n"
                "本稽核不得在缺少來源的情況下給出判決；請修正路徑或補上資料。")
        return pd.read_parquet(path)

    act = require(TWSE_ACTIONS, "twse_corporate_actions")
    act["stock_id"] = act["stock_id"].astype(str)
    act["event_date"] = pd.to_datetime(act["event_date"])

    tpex = require(TPEX_DIVIDENDS, "tpex_dividends")
    tpex["stock_id"] = tpex["stock_id"].astype(str)
    tpex["ex_date"] = pd.to_datetime(tpex["ex_date"])

    nd = require(NEW_DIVIDENDS, "research_dividends")
    nd["stock_id"] = nd["stock_id"].astype(str)
    nd["ex_date"] = pd.to_datetime(nd["ex_date"])

    events = [
        act.loc[act["event_date"] >= "2014-10-01",
                ["stock_id", "event_date", "event_kind"]]
           .assign(source="twse_corporate_actions"),
        tpex.loc[tpex["ex_date"] >= "2014-10-01"]
            .rename(columns={"ex_date": "event_date", "event_type": "event_kind"})
            [["stock_id", "event_date", "event_kind"]]
            .assign(source="tpex_dividends"),
        nd.loc[nd["ex_date"] <= "2015-03-31"]
          .rename(columns={"ex_date": "event_date"})
          .assign(event_kind="dividend", source="research_dividends")
          [["stock_id", "event_date", "event_kind", "source"]],
    ]
    boundary = pd.concat(events, ignore_index=True)
    affected = sorted(set(boundary["stock_id"]))

    # ── 涵蓋密度：本稽核唯一能觸及「還原管線一致性」的檢查 ──────────
    # 前面所有檢查測的都是**單日**原始價格連續性。但 mom_12_1 在 2015 年中
    # 的決策日，會用到 2014 年中起的**還原**價——2014 那段的還原來自舊事件檔，
    # 2015 那段來自新事件檔。若舊事件檔漏記事件，形成期報酬就會系統性偏差，
    # 而單日接縫檢查完全看不到。
    #
    # 台股配息接近普及，因此「每檔每年事件數」應該平滑。**在來源交界出現
    # 階梯式跳動 = 涵蓋不全；緩慢趨勢 = 真實變化。**
    all_events = pd.concat([
        act.rename(columns={"event_date": "d"})[["stock_id", "d"]].assign(
            src="twse_2005_2014"),
        tpex.rename(columns={"ex_date": "d"})[["stock_id", "d"]].assign(
            src="tpex_2008_2014"),
        nd.rename(columns={"ex_date": "d"})[["stock_id", "d"]].assign(
            src="research_2015_2026"),
    ], ignore_index=True)
    all_events = all_events[all_events["stock_id"].str.fullmatch(r"\d{4}")]
    all_events["year"] = all_events["d"].dt.year

    listed = pd.concat([old, new], ignore_index=True)
    listed = listed[listed["stock_id"].str.fullmatch(r"\d{4}")]
    per_year = listed.groupby(listed["trade_date"].dt.year)["stock_id"].nunique()
    counts = all_events.groupby("year").size()

    density = {}
    for year in sorted(set(per_year.index) & set(range(2008, 2027))):
        n = int(per_year.loc[year])
        c = int(counts.get(year, 0))
        density[int(year)] = {"events": c, "listed": n,
                              "events_per_listed": round(c / n, 4) if n else None}

    old_last = old["trade_date"].max()
    new_first = new["trade_date"].min()
    old_close = old[old["trade_date"] == old_last].set_index("stock_id")["close"]
    new_close = new[new["trade_date"] == new_first].set_index("stock_id")["close"]
    shared = old_close.index.intersection(new_close.index)
    step = (new_close.loc[shared] / old_close.loc[shared].where(old_close > 0) - 1.0)

    hit = sorted(set(affected) & set(shared))
    clean = sorted(set(shared) - set(affected))
    return {
        "available": True,
        "window": "2014-10-01 .. 2015-03-31",
        "events": int(len(boundary)),
        "event_kinds": {str(k): int(v) for k, v
                        in boundary["event_kind"].value_counts().head(12).items()},
        "securities_with_boundary_events": len(affected),
        "event_coverage_density_by_year": density,
        "density_note": ("Taiwan dividend payout is near-universal, so events per "
                         "listed security should be smooth. A STEP at a source "
                         "boundary means incomplete coverage; a gradual trend is "
                         "real. This is the only check here that can touch "
                         "adjustment-pipeline consistency over a formation window "
                         "— every other check is single-day RAW price continuity."),
        "with_events_seam_step": describe(step.loc[hit]),
        "without_events_seam_step": describe(step.loc[clean]),
        "note": ("if the two distributions differ materially, the adjustment "
                 "pipelines disagree on corporate actions even when ordinary "
                 "stocks look fine — this is the failure mode a distribution-wide "
                 "check hides in the tail."),
        "worst_with_events": [
            {"stock_id": sid, "return_pct": round(float(step.loc[sid]) * 100, 3)}
            for sid in step.loc[hit].abs().sort_values(ascending=False).head(25).index
        ],
    }


# ── 檢查 5：雙管線重疊驗證 ──────────────────────────────────────
def check_overlap(old: pd.DataFrame, new: pd.DataFrame) -> dict:
    old_days = set(old["trade_date"].unique())
    new_days = set(new["trade_date"].unique())
    shared_days = sorted(old_days & new_days)
    if not shared_days:
        return {
            "available": False,
            "reason": ("the two pipelines share no trading day "
                       f"(old ends {max(old_days).date()}, "
                       f"new starts {min(new_days).date()})"),
            "verdict_note": ("overlap validation unavailable — do NOT infer "
                             "consistency from its absence"),
        }
    lo, hi = shared_days[0], shared_days[-1]
    a = (old[old["trade_date"].isin(shared_days)]
         .set_index(["stock_id", "trade_date"])["close"])
    b = (new[new["trade_date"].isin(shared_days)]
         .set_index(["stock_id", "trade_date"])["close"])
    common = a.index.intersection(b.index)
    diff = (b.loc[common] / a.loc[common].where(a > 0) - 1.0).dropna()
    return {
        "available": True,
        "overlap_from": str(lo.date()), "overlap_to": str(hi.date()),
        "overlapping_observations": int(common.size),
        "price_ratio_minus_one": describe(diff),
    }


# ── 判決 ────────────────────────────────────────────────────────
def decide(seam: dict, universe: dict, schema: dict, actions: dict) -> dict:
    reasons: list[str] = []
    verdict = "clean"

    tickers = {k: v["ticker_all_digits_share"] for k, v in schema.items()}
    if min(tickers.values()) < 0.95:
        verdict = "incompatible"
        reasons.append(f"代號格式不一致，無法可靠 join：{tickers}")

    closes = [v["median_close"] for v in schema.values()]
    if max(closes) / max(min(closes), 1e-9) > 10:
        verdict = "incompatible"
        reasons.append(f"收盤價中位數量級差異過大，疑似單位不同：{closes}")

    # turnover/volume 應該等於價格。這比中位數比較強得多。
    implied = {k: v.get("implied_vs_median_close_ratio") for k, v in schema.items()}
    bad_units = {k: r for k, r in implied.items()
                 if r is not None and not (0.5 <= r <= 2.0)}
    if bad_units:
        verdict = "incompatible"
        reasons.append(
            f"turnover/volume 推得的價格與收盤價不符，疑似量的單位不同：{bad_units}")

    # 跨接縫的量比值——抓「倍率差 1000」這一類
    for column, stats in seam.get("cross_seam_volume_ratio", {}).items():
        ratio = stats["median_new_over_old"]
        if not (0.2 <= ratio <= 5.0):
            verdict = "incompatible"
            reasons.append(f"{column} 跨接縫中位比值 {ratio}，疑似單位改變")

    # 代號涵蓋範圍差異：舊 TWSE 含權證／ETN 等非四位數證券，新資料沒有。
    # 這不是不相容，但**必須統一過濾**，否則兩段的母體組成不同。
    non_four = {k: v.get("non_four_digit_ticker_share") for k, v in schema.items()}
    if any(s and s > 0.001 for s in non_four.values()):
        reasons.append(
            f"代號涵蓋範圍不同（非四位數證券佔比 {non_four}）——"
            "舊 TWSE 含權證／ETN 等，新資料沒有。"
            "**必須全程限制為四位數普通股**，否則兩段母體組成不同。")

    share = universe.get("intersection_share_of_old") or 0.0
    if share < 0.50:
        verdict = "incompatible"
        reasons.append(f"universe 交集僅 {share:.1%}，疑似證券主檔對應問題")

    # 接縫是否看起來像平常的一天——與同源基準線比，不設絕對門檻
    seam_tail = seam["seam_step"].get("share_abs_gt_20pct", 0.0)
    base_tail = max(seam["baseline_within_old"].get("share_abs_gt_20pct", 0.0),
                    seam["baseline_within_new"].get("share_abs_gt_20pct", 0.0))
    if seam_tail > max(base_tail * 3, base_tail + 0.01) and verdict != "incompatible":
        verdict = "usable_with_exclusion"
        reasons.append(
            f"接縫日極端值比例 {seam_tail:.3%} 明顯高於同源基準 {base_tail:.3%}")

    if actions.get("available"):
        with_ev = actions["with_events_seam_step"].get("share_abs_gt_20pct", 0.0)
        without_ev = actions["without_events_seam_step"].get("share_abs_gt_20pct", 0.0)
        if with_ev > max(without_ev * 3, without_ev + 0.02) and verdict == "clean":
            verdict = "usable_with_exclusion"
            reasons.append(
                f"有公司行動者接縫極端值比例 {with_ev:.3%} 遠高於無事件者 "
                f"{without_ev:.3%}——還原管線在公司行動上不一致")

    return {
        "verdict": verdict,
        "reasons": reasons or ["未偵測到系統性不一致"],
        "mandatory_preprocessing": [
            "限制為四位數代號（排除權證／ETN／TDR 等，僅舊 TWSE 才有）",
            "沿用既有的流動性篩選與 ETF 排除規則，兩段套用同一套",
        ],
        "criterion": ("data consistency ONLY. This audit never computes forward "
                      "returns, momentum signals, or any predictability statistic — "
                      "otherwise seam cleaning would itself become model selection."),
        "if_usable_with_exclusion": {
            "rule": ("排除所有形成窗跨越接縫的決策日。長度必須用 mom_12_1 的實際"
                     "形成窗計算，不得沿用 mom_6_1 的 120 日估計。"),
            "mom_12_1_formation_sessions": MOM_12_1_LOOKBACK + MOM_12_1_SKIP,
            "approx_calendar_months_lost": round(
                (MOM_12_1_LOOKBACK + MOM_12_1_SKIP) / 21.0, 1),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports/price_pipeline_seam_audit.json")
    args = parser.parse_args()

    twse = load_prices(OLD_TWSE, "twse_2005_2014")
    tpex = load_prices(OLD_TPEX, "tpex_2008_2014")
    new = load_prices(NEW, "research_2015_2026")
    old = pd.concat([twse[twse["trade_date"] >= "2008-01-01"], tpex],
                    ignore_index=True)

    seam = check_seam_returns(old, new)
    universe = check_universe(old, new)
    schema = check_schema({"twse_2005_2014": twse, "tpex_2008_2014": tpex,
                           "research_2015_2026": new})
    actions = check_corporate_actions(old, new)
    overlap = check_overlap(old, new)
    verdict = decide(seam, universe, schema, actions)

    report = {
        "study": "step 0 — 2014/2015 price pipeline seam audit",
        "status": "DIAGNOSTIC ONLY — no forward returns, no momentum, no new trials",
        "sources": {"old_twse": str(OLD_TWSE.relative_to(ROOT)),
                    "old_tpex": str(OLD_TPEX.relative_to(ROOT)),
                    "new": str(NEW.relative_to(ROOT))},
        "verdict": verdict,
        "check_1_seam_returns": seam,
        "check_2_universe_continuity": universe,
        "check_3_schema_and_units": schema,
        "check_4_corporate_action_boundary": actions,
        "check_5_dual_pipeline_overlap": overlap,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    print(f"判決：{verdict['verdict']}")
    for r in verdict["reasons"]:
        print(f"  - {r}")
    s = seam["seam_step"]
    print(f"\n接縫 {seam['seam_from']} → {seam['seam_to']}，共同證券 "
          f"{seam['shared_securities']}")
    print(f"  接縫日   中位 {s['p50_pct']:+.3f}%  p1 {s['p1_pct']:+.2f}%  "
          f"p99 {s['p99_pct']:+.2f}%  |R|>20% {s['abs_gt_20pct']} 檔")
    for key, label in (("baseline_within_old", "舊源平常日"),
                       ("baseline_within_new", "新源平常日")):
        b = seam[key]
        print(f"  {label} 中位 {b['p50_pct']:+.3f}%  p1 {b['p1_pct']:+.2f}%  "
              f"p99 {b['p99_pct']:+.2f}%  |R|>20% {b['abs_gt_20pct']} 檔")
    u = universe
    print(f"\nuniverse 交集 {u['intersection_count']}／舊 "
          f"{u['intersection_share_of_old']:.1%}／新 "
          f"{u['intersection_share_of_new']:.1%}"
          f"  僅舊 {u['old_only_count']}  僅新 {u['new_only_count']}")
    if actions.get("available"):
        print(f"\n公司行動邊界：{actions['securities_with_boundary_events']} 檔有事件")
        print(f"  有事件 |R|>20% 比例 "
              f"{actions['with_events_seam_step'].get('share_abs_gt_20pct'):.3%}")
        print(f"  無事件 |R|>20% 比例 "
              f"{actions['without_events_seam_step'].get('share_abs_gt_20pct'):.3%}")
    print(f"\n重疊驗證：{'可用' if overlap['available'] else overlap['reason']}")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
