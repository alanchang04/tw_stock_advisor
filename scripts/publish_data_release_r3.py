"""發佈 release r3：解除 MOM-1 backward holdout 的績效檢視封鎖。

**這個腳本只改政策，不改資料。** 九個元件與 r2 逐位元相同（content SHA 直接沿用，
且 `verify_release_inputs` 會在載入時對實體檔案重新驗算），
改的只有 `readiness` 與 `usage_policy` 兩個欄位。

## 為什麼要有 r3

r2 自己的政策明文封鎖 "backward holdout performance inspection"。
在 r2 之下執行 F1 會違反該 release 的使用政策，因此不能「就跑一下」——
必須先用一個新的、記錄了解封理由與證據的 release 取代它。

## 解封的證據

`reports/mom1_f0_execution_readiness.json`：

- `f0_status = passed`，SPEC §9.1 六項正確性要求逐條有測試覆蓋
- `remaining_blockers = []`，兩個原始阻擋都以實測證據關閉：
  - TWSE 停止交易無獨立官方旗標 → 官方 TWTAWU 自 2011-10-03 起，
    窗內 28 筆停牌全為外國第一上市／TDR／權證，與 MOM-1 選中的 239 檔**交集為空**
  - D3 實股執行 ledger → 653 筆 blocked 中只有 10 筆落在 MOM-1 持有窗，
    9 筆是可選擇的現金增資（政策 A 不認購），剩下 1 筆（0.09% 股-月）
    以事件前收盤價強制平倉（政策 B）

## 什麼**沒有**被解封

`parameter tuning on 2008-2014 returns` **維持封鎖**。
F1 是一次性否證測試，不是調參授權。看過結果之後再改參數，
2008~2014 就從 holdout 變成 development 資料（M18 同理）。
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_ID = "tw_stock_data_2005_2014_r2"
TARGET_ID = "tw_stock_data_2005_2014_r3"
UNBLOCKED = "backward holdout performance inspection"
NEWLY_ALLOWED = (
    "one-time MOM-1 backward holdout falsification test (SPEC 9.2 F1)",
    "portfolio performance metrics on the 2008-2014 window",
)


def descriptor_path(release_id: str) -> Path:
    directory = ROOT / "reports/data_releases"
    matches = [p for p in sorted(directory.glob("*.json"))
               if json.loads(p.read_text(encoding="utf-8")).get("data_release_id") == release_id]
    if len(matches) != 1:
        raise SystemExit(f"找不到唯一的 {release_id} descriptor（找到 {len(matches)} 個）")
    return matches[0]


def build(source: dict) -> dict:
    document = copy.deepcopy(source)
    document["data_release_id"] = TARGET_ID
    document["supersedes"] = SOURCE_ID
    document["release_date"] = "2026-08-14"
    document["authority"] = "Claude (Data Authority transferred 2026-08-13)"

    readiness = document.setdefault("readiness", {})
    readiness["backward_holdout_performance_ready"] = True
    # 全市場部署仍未就緒——F1 通過只代表省下 forward paper，不代表可部署
    readiness["all_market_strategy_promotion_ready"] = False

    policy = document.setdefault("usage_policy", {})
    blocked = [item for item in policy.get("blocked", []) if item != UNBLOCKED]
    if len(blocked) == len(policy.get("blocked", [])):
        raise SystemExit(f"r2 的 blocked 清單裡沒有 {UNBLOCKED!r}，請先確認來源 descriptor")
    policy["blocked"] = blocked
    allowed = list(policy.get("allowed", []))
    for item in NEWLY_ALLOWED:
        if item not in allowed:
            allowed.append(item)
    policy["allowed"] = allowed

    document["unblock_rationale"] = {
        "unblocked": UNBLOCKED,
        "evidence_report": "reports/mom1_f0_execution_readiness.json",
        "f0_status": "passed",
        "remaining_blockers": 0,
        "still_blocked": policy["blocked"],
        "note": (
            "F1 is a one-shot falsification test. Passing it only means the forward "
            "paper-trading requirement is shortened; it is not positive evidence and "
            "it does not authorise parameter tuning on the 2008-2014 window."
        ),
    }
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = json.loads(descriptor_path(SOURCE_ID).read_text(encoding="utf-8"))
    document = build(source)
    target = ROOT / f"reports/data_releases/tw_stock_data_release_2005_2014_r3.json"

    if args.dry_run:
        print(json.dumps({"readiness": document["readiness"],
                          "usage_policy": document["usage_policy"]},
                         ensure_ascii=False, indent=2))
        return
    if target.exists():
        raise SystemExit(f"{target} 已存在；release 不可變，不得覆寫")
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"written: {target}")
    print("blocked now:", document["usage_policy"]["blocked"])


if __name__ == "__main__":
    main()
