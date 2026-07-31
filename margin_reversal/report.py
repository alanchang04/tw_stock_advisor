"""Text/JSON-safe reporting for the independent strategy."""
from __future__ import annotations


def format_event_study(result: dict) -> str:
    n = result.get("treated_n", 0)
    if not n:
        return "事件研究尚無可配對樣本；不得判定融資清洗有效或無效。"
    verdict = ("顯著優於對照，通過" if result.get("direction") == "better" else
               "顯著劣於對照，否決" if result.get("direction") == "worse" else
               "差異不顯著，未通過")
    return (f"融資清洗事件研究（{result['horizon']}日）：配對 {n} 組，"
            f"清洗組 {result['treated_mean']:.2%}、對照組 {result['control_mean']:.2%}，"
            f"差異 {result['lift']:+.2%}，95% CI "
            f"[{result['ci_low']:+.2%}, {result['ci_high']:+.2%}]；{verdict}。")
