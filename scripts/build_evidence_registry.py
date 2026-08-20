"""產出 `research/results/evidence_registry.json`——前端唯一要讀的證據彙總。

為什麼需要這一支
----------------
角色 D 稽核過九個 `research/results/h*.json`，**沒有一致的機器可讀判決欄位**：
`h13`／`h16` 有 `evidence_status`，`h18`／`h19` 只有 `track`，其餘沒有。
判決其實住在 `research/HYPOTHESES.md` 的散文裡。

**手寫一張表會變成第二個 README**——今天正確、兩週後漂移。
README 就是這樣壞掉的（寫著 7 頁而實際 16 頁、停損 −7% 而實際 −8%）。

因此本檔的設計是：

- **判決由人策展**（住在下面的 `HYPOTHESES` 常數，因為它本來就是判斷）
- **數字由腳本從結果 JSON 抽取**（因此不會與來源檔漂移）
- 抽不到就寫 `null` 並在 `extraction_errors` 記一筆，**不猜**

`display_policy` 的原則
-----------------------
先前否決 MOM-1 的 NAV 曲線時，我把界線畫在「粒度」。**那個界線後來被修正**：
我們自己早已在同一段 holdout 上跑過兩支事後分析腳本，所以「summary 可以、
逐日不行」不是一致的立場。

**修正後的原則：UI 上的風險是誤讀，不是污染。**

- 已執行且判決已公布的研究，**數字可以顯示**——該段資料的成本已經付掉了
- 真正必要的是**強制併陳證據等級與警語**，而不是把數字藏起來
- 唯一維持「不顯示數字」的是**密封資料**（H17），因為那裡根本還沒有結果，
  顯示任何東西都會暗示我們看過

因此每一列都有 `mandatory_caveat`：**引用該列任何數字時必須一併顯示。**
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "research/results/evidence_registry.json"

VERDICT_VOCABULARY = {
    "passed": "主要判準通過",
    "rejected": "否決",
    "primary_not_met": "主要判準未通過（不等於現象不存在）",
    "real_but_not_deployable": "現象未被否證，但扣成本後不足以部署",
    "blocked": "阻塞：缺資料或缺文獻定義，尚未執行",
    "registered_not_executed": "已事前登記，尚未執行",
    "pending_dependency": "依附其他假說，前置未過故不執行",
}

TIER_VOCABULARY = {
    "development": ("2015~2026。falsification / development evidence only，"
                    "不是 OOS 確認（M22 Discovery 軌）"),
    "backward": "2008~2014，該訊號在此段先前無 outcome exposure 的一次性集合",
    "holdout_spent": "一次性 holdout，已開封且不得重跑",
    "sealed": "資料已備妥但尚未與未來報酬 join",
    "forward": "2026-08-14 之後累積中，唯一的真確認來源",
}

DISPLAY_POLICY_VOCABULARY = {
    "full": "可顯示所有已公布的數字，但必須併陳 evidence_tier 與 mandatory_caveat",
    "summary_only": "只可顯示摘要指標，不得顯示逐日序列或逐筆交易",
    "status_only": "只可顯示狀態文字，不得顯示任何數值",
    "do_not_display": "不得顯示於面向使用者的介面",
}

DEV_CAVEAT = ("development 證據（2015~2026），**不是 OOS 確認**。"
              "唯一的真確認來源是 2026-08-14 起的 forward。")

# ── 判決：由人策展。數字不寫在這裡，由 `metric` 指向來源檔抽取 ──────
HYPOTHESES = [
    {"id": "H01", "title": "布林二次突破", "factor": "bollinger_rebreakout",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-13",
     "source": "research/results/h01_bollinger_event_study.json",
     "display_policy": "full", "caveat": DEV_CAVEAT},
    {"id": "H04", "title": "月營收年增（事件研究）", "factor": "rev_yoy",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-13",
     "source": "research/results/h04_h06_event_study.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " 去重後 CAR 大幅下降，且結論由 2025 一年撐起（M3／M4）。"},
    {"id": "H05", "title": "投信連續買超", "factor": "invest_streak",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-13",
     "source": "research/results/h04_h06_event_study.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " 120 日窗重複率 85.4%，去重後 CAR 由 +1.712% 降為 +0.360%，"
               "且拿掉 2025 一年即變號。"},
    {"id": "H06", "title": "投信新進場", "factor": "invest_new_entry",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-13",
     "source": "research/results/h04_h06_event_study.json",
     "display_policy": "full", "caveat": DEV_CAVEAT},
    {"id": "H10", "title": "布林候選之間的橫斷面可預測性", "factor": "multiple",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-13",
     "source": "research/results/h10_cross_section.json",
     "display_policy": "full", "caveat": DEV_CAVEAT},
    {"id": "H11", "title": "營收 underreaction", "factor": "rev_yoy",
     "tier": "development", "verdict": "real_but_not_deployable",
     "decided_at": "2026-08-13",
     "source": "research/results/fama_macbeth_operating_only.json",
     "metric": {"name": "6M 累積（yoy>0，控制規模）",
                "path": "results[signal=rev_yoy_positive,specification=size_controlled]"
                        ".cumulative_6m.mean_pct",
                "t_path": "results[signal=rev_yoy_positive,specification=size_controlled]"
                          ".cumulative_6m.t_stat"},
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " **因子未被否證，但只做多扣成本後僅 +0.46%，"
               "低於 2.185% 經濟門檻——不可部署。**"},
    {"id": "H11b", "title": "營收訊號的資訊時點複製", "factor": "rev_yoy",
     "tier": "development", "verdict": "real_but_not_deployable",
     "decided_at": "2026-08-14",
     "source": "research/results/h11b_revenue_timing.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " 提前至法定可得時點回復約 0.81pp，"
               "但**事前登記的主規格（yoy>0）只做多經濟性仍未達門檻**。"
               "`yoy>20%` 僅以 0.07pp 勉強過線，不升格。"},
    {"id": "H12", "title": "法人確認的交互作用", "factor": "invest_streak",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-14",
     "source": "research/results/h12_institutional_confirmation.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " Δ_signal +0.172%（t=0.43），規模吸收 81%。"},
    {"id": "H13", "title": "布林／量能作為進場時機", "factor": "bollinger_timing",
     "tier": "development", "verdict": "rejected", "decided_at": "2026-08-14",
     "source": "research/results/h13_bollinger_timing.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " **方向與假說相反**：三項主要指標皆顯著較差，"
               "B1 平均在 +10.229% 的漲幅之後才出現，參與率僅 20.1%。"},
    {"id": "H16", "title": "基本面條件下的價格動能", "factor": "mom_6_1",
     "tier": "development", "verdict": "primary_not_met", "decided_at": "2026-08-14",
     "source": "research/results/h16_momentum_confirmation.json",
     "display_policy": "full",
     "caveat": DEV_CAVEAT + " 🟡 歷史 inconclusive：+1.864%（t=1.693），CI 含 0。"},
    {"id": "H17", "title": "營收成長的持續性", "factor": "revenue_persistence",
     "tier": "sealed", "verdict": "blocked", "decided_at": None, "source": None,
     "display_policy": "status_only",
     "caveat": "**阻塞**：persistence 的精確定義取自論文，尚未取得，禁止自行猜測。"
               "2010–2014 營收資料已回補但**尚未與未來報酬 join**"
               "（`reports/revenue_2010_2014_sealed.json`）。**不得顯示任何數值。**"},
    {"id": "H18", "title": "多頭排列持續天數的 backward 複製",
     "factor": "stack_days", "tier": "backward", "verdict": "primary_not_met",
     "decided_at": "2026-08-17",
     "source": "research/results/h18_trend_stack_backward.json",
     "metric": {"name": "CUM3（控制規模）",
                "path": "size_controlled.primary_estimand_CUM3.mean_pct",
                "t_path": "size_controlled.primary_estimand_CUM3.t_stat"},
     "display_policy": "full",
     "caveat": "**主要估計量未通過（t=0.20）**，但事前列為描述性的同口徑 rank IC "
               "為 +0.0354（t=2.94），與 development 的 +0.0329 幾乎相同。"
               "矛盾指向估計式（線性迴歸跑在 64% 為 0 的計數上），不是訊號。"
               "**描述性統計不得升格為判決**（M25）。此段對此訊號已用掉。"},
    {"id": "H19", "title": "短期反轉的 backward 複製", "factor": "rs20",
     "tier": "backward", "verdict": "passed", "decided_at": "2026-08-17",
     "source": "research/results/h19_reversal_backward.json",
     "metric": {"name": "20 日 rank IC",
                "path": "primary_estimand_rank_ic_20d.mean",
                "t_path": "primary_estimand_rank_ic_20d.t_stat"},
     "display_policy": "full",
     "caveat": "**本專案第一次乾淨的 backward 複製成功**：該段對此訊號先前無 "
               "outcome exposure、horizon 有外部文獻先驗（Jegadeesh 1990／"
               "Lehmann 1990）、主檢定形式與選規格證據一致（M25）。"
               "**但不是可部署策略**：D10−D1 僅 −0.4495%／月，"
               "而完整換手成本 1.185%，扣成本後為負。"
               "這是方法論上的成功，不是可部署性的成功。"},
    {"id": "MOM-1", "title": "月頻動能組合策略", "factor": "mom_6_1",
     "tier": "holdout_spent", "verdict": "rejected", "decided_at": "2026-08-14",
     "source": "reports/mom1_f1_backward_holdout.json",
     "display_policy": "summary_only",
     "caveat": "F1 一次性 holdout（2008–2014）已開封，**不得重跑**。"
               "六項判準過五項，未過的是「排除最佳 5 筆交易後仍為正」"
               "（211 筆交易中，另外 206 筆合計為虧損）。"
               "**不得顯示逐日 NAV 曲線**：該執行未持久化任何 NAV 序列，"
               "產生它需要重新執行一次 holdout。年度報酬已公布，可以顯示。"},
]

# ── 作廢清單 ────────────────────────────────────────────────────
INVALIDATED = [
    {"artifact": "reports/mom1_f1_backward_holdout_run1_INVALID.json",
     "invalidated_at": "2026-08-14",
     "reason": ("引擎把「未成交單逐日重試」寫成「每日重算目標股數」，變成每日再平衡"
                "而非 SPEC §7.3 的每月再平衡。成交 6,644 筆／84 個月／最多 10 檔，"
                "成本吃掉 18.8~23.3% 資本。觸發點是成交筆數這個診斷數字明顯不合理，"
                "不是看了報酬去調參數。"),
     "superseded_by": "reports/mom1_f1_backward_holdout.json"},
    {"artifact": "舊首頁績效數字 +328% 與 0050 +745.5%",
     "invalidated_at": "2026-08-11",
     "reason": "未經凍結快照驗證的口徑，且基準計算有誤。",
     "superseded_by": "reports/swing_backtest_verified_20260811_bf58807.json"},
    {"artifact": "data/research_versions/twse_security_master_2005_2007_staging_v2",
     "invalidated_at": None,
     "reason": "該目錄自帶 INVALID_DO_NOT_USE.md。",
     "superseded_by": "後續 staging 版本"},
    {"artifact": "docs/STRATEGY_STATUS_2026-08-13.md §3.5 的結論",
     "invalidated_at": "2026-08-14",
     "reason": ("M13 存活者偏差：前瞻報酬被未來合格條件過濾。修正後月營收 6 個月"
                "累積由被高估的 +3.03% 降為 +2.02%，投信連買由 −1.19% 變 +1.86%"
                "（連符號都反了）。"),
     "superseded_by": "research/results/fama_macbeth_operating_only.json"},
    {"artifact": "動能族 2008-2021/2022-2026.8 的探索/封存切分",
     "invalidated_at": "2026-08-16",
     "reason": ("與本專案自己的 SPEC_DATA_FOUNDATION_AND_MOMENTUM.md §9.2.4 牴觸："
                "mom_6_1 的形成窗是看著 2015~2026 的 IC 表從四個候選挑出來的，"
                "因此該段對動能族並不乾淨。乾淨歷史 holdout = 0。"),
     "superseded_by": "research/data_splits.py MOMENTUM_DEVELOPMENT（無 sealed）"},
]


def dig(data: dict, path: str):
    """支援 `a.b.c` 與 `results[k=v,k2=v2].x` 兩種取值。"""
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if "[" in part and part.endswith("]"):
            name, _, filters = part.partition("[")
            rows = current.get(name) if isinstance(current, dict) else None
            if not isinstance(rows, list):
                return None
            wanted = dict(pair.split("=", 1)
                          for pair in filters[:-1].split(","))
            current = next((r for r in rows
                            if all(str(r.get(k)) == v for k, v in wanted.items())),
                           None)
        else:
            current = current.get(part) if isinstance(current, dict) else None
    return current


def build() -> dict:
    rows, errors = [], []
    for spec in HYPOTHESES:
        entry = {
            "id": spec["id"], "title": spec["title"], "factor": spec["factor"],
            "evidence_tier": spec["tier"], "verdict": spec["verdict"],
            "verdict_label": VERDICT_VOCABULARY[spec["verdict"]],
            "decided_at": spec["decided_at"],
            "source_report": spec["source"],
            "display_policy": spec["display_policy"],
            "mandatory_caveat": spec["caveat"],
            "primary_metric": None,
        }
        metric = spec.get("metric")
        if metric and spec["source"]:
            path = ROOT / spec["source"]
            if not path.exists():
                errors.append({"id": spec["id"], "error": f"missing {spec['source']}"})
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                value = dig(data, metric["path"])
                t_stat = dig(data, metric["t_path"]) if metric.get("t_path") else None
                if value is None:
                    errors.append({"id": spec["id"],
                                   "error": f"path not found: {metric['path']}"})
                entry["primary_metric"] = {"name": metric["name"],
                                           "value": value, "t_stat": t_stat}
        rows.append(entry)

    by_tier: dict[str, int] = {}
    for row in rows:
        by_tier[row["evidence_tier"]] = by_tier.get(row["evidence_tier"], 0) + 1

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "scripts/build_evidence_registry.py",
        "how_to_read": (
            "verdicts are curated (they live in research/HYPOTHESES.md prose and are "
            "judgements, not data); NUMBERS are extracted from the source reports at "
            "build time so they cannot drift. Anything unextractable is null and "
            "listed in extraction_errors — never guessed."),
        "tier_vocabulary": TIER_VOCABULARY,
        "verdict_vocabulary": VERDICT_VOCABULARY,
        "display_policy_vocabulary": DISPLAY_POLICY_VOCABULARY,
        "display_rule": (
            "ANY number taken from a row MUST be shown together with that row's "
            "evidence_tier and mandatory_caveat. The risk on a user-facing surface "
            "is misreading, not contamination — so the fix is compulsory labelling, "
            "not hiding numbers. The one exception is tier 'sealed', where no "
            "outcome numbers exist and showing anything would imply we looked."),
        "summary": {
            "hypotheses": len(rows),
            "by_tier": by_tier,
            "by_verdict": {v: sum(1 for r in rows if r["verdict"] == v)
                           for v in VERDICT_VOCABULARY},
            "clean_backward_candidates_remaining": 0,
            "clean_backward_note": ("stack_days (H18) and rs20 (H19) both consumed "
                                    "2026-08-17. No price factor has an unexposed "
                                    "2008-2014 segment left. Remaining clean "
                                    "resources: the sealed 2010-2014 revenue, "
                                    "and forward."),
            "forward_start": "2026-08-14",
        },
        "hypotheses": rows,
        "invalidated_artifacts": INVALIDATED,
        "extraction_errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    report = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    for row in report["hypotheses"]:
        metric = row["primary_metric"]
        shown = (f"{metric['value']}" if metric and metric["value"] is not None
                 else "—")
        print(f"  {row['id']:6s} {row['evidence_tier']:14s} "
              f"{row['verdict']:26s} {row['display_policy']:14s} {shown}")
    if report["extraction_errors"]:
        print("\n**抽取失敗（已寫成 null，未猜測）**：")
        for err in report["extraction_errors"]:
            print(f"  {err['id']}: {err['error']}")
    print(f"\nwritten: {args.output}")


if __name__ == "__main__":
    main()
