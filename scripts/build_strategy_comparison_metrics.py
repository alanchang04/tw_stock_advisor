"""把各策略的摘要指標正規化成同一組欄位（策略比較頁用）。

為什麼需要這支
--------------
三套策略的指標欄位名稱與定義各不相同：現行策略用 `metrics.nav_mdd`，
margin reversal 用 `strategy_metrics.mdd`，MOM-1 用 `core.max_drawdown`。
前端若各自解讀，就會產生第二個真相。

**這支只做搬運與改名，不重算任何指標。** 每一項都附 `source_report`
指回原始檔，前端顯示來源即可。

**它不讀取、不重算任何 holdout 序列。** MOM-1 的數字全部來自
`reports/mom1_f1_backward_holdout.json` 這份**已提交、已公布**的結果，
不重跑 `run_mom1_f1_holdout.py`。原因見
`reports/HANDOFF_2026-08-16_REPLY_FROM_RESEARCH_MACHINE.md` §1。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/strategy_comparison_metrics.json"

SWING = "reports/swing_backtest_verified_20260811_bf58807.json"
MOM1 = "reports/mom1_f1_backward_holdout.json"
MARGIN = "reports/margin_reversal_study.json"


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def build() -> dict:
    swing = load(SWING)
    mom1 = load(MOM1)
    margin = load(MARGIN)

    sm = swing["metrics"]
    mb = mom1["results"]["MOM-1B"]
    mbc = mb["core"]
    mm = margin["strategy_metrics"]

    return {
        "schema_version": 1,
        "generated_by": "scripts/build_strategy_comparison_metrics.py",
        "purpose": ("side-by-side comparison to GENERATE hypotheses, not to pick a "
                    "winner. Every row carries its verdict; none of these is a "
                    "menu item."),
        "definitions": {
            "sharpe": ("annualised return / annualised volatility, risk-free taken as 0. "
                       "NOTE: the three sources computed this themselves and are NOT "
                       "guaranteed identical in construction — see per-row caveats."),
            "mdd": "largest NAV drawdown from a running peak; negative",
            "ann_ret": "geometric annualised return",
            "win_rate": "share of closed trades with positive net P&L",
            "sample_unit": "trades unless stated; reversal-type strategies need episodes",
        },
        "curve_availability": {
            "current_swing": "reports/swing_backtest_curve/ (daily NAV, development data)",
            "mom1_f1": ("NONE AND NOT TO BE PRODUCED — no NAV series was persisted by the "
                        "one-shot holdout run, so producing one requires re-executing the "
                        "strategy against 2008-2014. Only the 7 published annual returns "
                        "may be charted."),
            "margin_reversal": "NONE — 82 observations over 0.33 calendar years",
        },
        "strategies": [
            {
                "key": "current_swing",
                "display_name": "現行波段策略",
                "status": "frozen_satellite",
                "verdict_note": "凍結衛星部位；歷史績效為 development 資料，非乾淨驗證",
                "period": [sm["start"], sm["end"]],
                "factors": [
                    "月營收年增 (w_rev_yoy 3.0)",
                    "投信連買 (w_invest_streak 2.5)",
                    "投信新進場 (w_invest_new_entry 2.5)",
                    "外資買超 (w_foreign_buy 1.0)",
                    "營收加速 (w_rev_accel 1.0)",
                    "ETF 加碼 (w_etf_accum 1.0)",
                    "多頭排列 (w_trend_stack 0.8)",
                ],
                "sharpe": sm["sharpe"],
                "mdd": sm["nav_mdd"],
                "ann_ret": sm["ann_ret"],
                "total_return": sm["nav_total_ret"],
                "win_rate": sm["net_win"],
                "trades": swing["trades"],
                "independent_episodes": None,
                "benchmark_key": "0050_total_return",
                "benchmark_sharpe": sm["sharpe_0050"],
                "benchmark_mdd": sm["mdd_0050"],
                "curve_dir": "reports/swing_backtest_curve",
                "source_report": SWING,
            },
            {
                "key": "mom1_b",
                "display_name": "MOM-1B（動能＋MA200 濾網）",
                "status": "rejected_at_F1",
                "verdict_note": ("F1 backward holdout 否決（6 項判準過 5 項，"
                                 "「排除最佳 5 筆交易後仍為正」未過）。"
                                 "**不得畫曲線**，見 curve_availability。"),
                "period": [mom1["holdout_window"]["start"], mom1["holdout_window"]["end"]],
                "factors": ["mom_6_1（6 個月動能、跳過最近 1 個月）", "MA200 市場濾網"],
                "sharpe": mbc["sharpe"],
                "mdd": mbc["max_drawdown"],
                "ann_ret": mbc["cagr"],
                "total_return": mbc["total_return"],
                "win_rate": None,
                "trades": mb["trades"].get("count") if isinstance(mb["trades"], dict) else None,
                "independent_episodes": None,
                "benchmark_key": "0050_total_return",
                "benchmark_sharpe": mom1["benchmark"]["core"]["sharpe"],
                "benchmark_mdd": mom1["benchmark"]["core"]["max_drawdown"],
                "curve_dir": None,
                "annual_returns": mb["monthly"]["annual_returns"],
                "regime_caveat": mom1["regime_differences"],
                "source_report": MOM1,
            },
            {
                "key": "margin_reversal",
                "display_name": "融資反轉",
                "status": "sample_insufficient",
                "verdict_note": ("6 筆交易、82 個觀測、0.33 個日曆年、勝率 0。"
                                 "**Sharpe／MDD／年化不具統計意義，前端不得顯示數值。**"),
                "period": [margin["start"], margin["end"]],
                "factors": ["融資餘額變化", "RSI14"],
                "sharpe": None,
                "mdd": None,
                "ann_ret": None,
                "total_return": mm["total"],
                "win_rate": 0.0,
                "trades": margin["trade_count"],
                "independent_episodes": None,
                "observations": mm["observations"],
                "calendar_years": mm["calendar_years"],
                "suppressed_because_sample_too_small": {
                    "sharpe": mm["sharpe"], "mdd": mm["mdd"], "ann_ret": mm["ann_ret"],
                    "why": ("kept here for auditability only. Annualising 0.33 years of "
                            "data produces numbers that look precise and mean nothing."),
                },
                "benchmark_key": "0050_total_return",
                "curve_dir": None,
                "source_report": MARGIN,
            },
            {
                "key": "rev1",
                "display_name": "REV-1",
                "status": "not_executed",
                "verdict_note": "規格 v1.1 已凍結，尚未執行。列出是為了顯示流程進度，不是候選清單。",
                "period": None, "factors": [], "sharpe": None, "mdd": None,
                "ann_ret": None, "total_return": None, "win_rate": None,
                "trades": None, "independent_episodes": None,
                "benchmark_key": None, "curve_dir": None, "source_report": None,
            },
        ],
    }


def main() -> None:
    report = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    for row in report["strategies"]:
        print(f"  {row['key']:16s} {row['status']:22s} "
              f"curve={row['curve_dir'] or '—'}")
    print(f"\nwritten: {OUTPUT}")


if __name__ == "__main__":
    main()
