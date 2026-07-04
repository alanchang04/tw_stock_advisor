"""
scripts/ablation_test.py — 出場規則消融測試

逐一關閉/調整出場規則跑回測，找出「過度出場、扼殺勝率」的規則。
資料只載入一次，所有情境共用。結果存 scripts/ablation_result.md。

執行：py -3.12 scripts/ablation_test.py
"""
import sys, os, io, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import pandas as pd
from agent.backtest import run_backtest, _load
from agent.strategy import STRATEGY

# ── 測試情境：每項只改一件事（combo 除外）───────────────────────
SCENARIOS = {
    "baseline(現行)":        {},
    "關 跌破MA5":            {"exit_below_ma5": False},
    "關 跌破MA20":           {"exit_below_ma20": False},
    "關 均線死亡交叉":       {"exit_on_death_cross": False},
    "關 跌破實體棒底":       {"exit_body_break": False},
    "關 跌破大K棒底":        {"exit_large_candle": False},
    "關 跌破前波低點":       {"exit_swing_low": False},
    "關 KD死叉+MACD":        {"exit_kd_macd": False},
    "關 長上引線爆量":       {"exit_upper_wick": False},
    "停損放寬到10%":         {"stop_loss": 0.10},
    "持有上限60日":          {"max_hold_days": 60},
    # 組合情境
    "combo:關均線三規則":    {"exit_below_ma5": False, "exit_below_ma20": False,
                              "exit_on_death_cross": False},
    "combo:關K棒四規則":     {"exit_body_break": False, "exit_large_candle": False,
                              "exit_swing_low": False, "exit_upper_wick": False},
    "combo:只留停損停利移動": {"exit_below_ma5": False, "exit_below_ma20": False,
                              "exit_on_death_cross": False, "exit_body_break": False,
                              "exit_large_candle": False, "exit_swing_low": False,
                              "exit_upper_wick": False, "exit_kd_macd": False},
    "combo:寬鬆波段(建議候選)": {"exit_below_ma5": False, "exit_body_break": False,
                              "exit_large_candle": False, "exit_upper_wick": False,
                              "stop_loss": 0.10, "max_hold_days": 60},
}


def stats(tdf: pd.DataFrame) -> dict:
    nets = tdf["net_ret"]
    return {
        "trades":   len(tdf),
        "win_pct":  (nets > 0).mean() * 100,
        "avg_net":  nets.mean() * 100,
        "med_net":  nets.median() * 100,
        "sum_net":  nets.sum() * 100,          # 期望值×筆數的粗略累計
        "avg_hold": tdf["hold"].mean(),
    }


def main():
    print("載入回測資料（一次，全情境共用）...")
    t0 = time.time()
    data = _load()
    print(f"  完成（{time.time()-t0:.0f}s）\n")

    rows = []
    for name, overrides in SCENARIOS.items():
        cfg = {**STRATEGY, **overrides}
        t0 = time.time()
        tdf = run_backtest(cfg=cfg, data=data, quiet=True)
        if tdf is None or tdf.empty:
            print(f"[{name}] 無交易，跳過")
            continue
        st = stats(tdf)
        st["name"] = name
        rows.append(st)
        print(f"[{name:<22}] 交易 {st['trades']:>3} 筆  勝率 {st['win_pct']:5.1f}%  "
              f"平均淨 {st['avg_net']:+6.2f}%  中位 {st['med_net']:+6.2f}%  "
              f"累計 {st['sum_net']:+8.1f}%  持有 {st['avg_hold']:4.1f} 日  ({time.time()-t0:.0f}s)")

    df = pd.DataFrame(rows).set_index("name")
    df = df.sort_values("sum_net", ascending=False)

    # ── 輸出 markdown ────────────────────────────────────────────
    lines = [
        "# 出場規則消融測試結果",
        "",
        f"> 產生時間：{pd.Timestamp.now():%Y-%m-%d %H:%M}；"
        f"每情境獨立回測（進場邏輯固定，只動出場），依「累計淨報酬」排序。",
        "",
        "| 情境 | 交易數 | 勝率% | 平均淨% | 中位淨% | 累計淨% | 平均持有日 |",
        "|------|-------:|------:|--------:|--------:|--------:|-----------:|",
    ]
    for name, r in df.iterrows():
        lines.append(f"| {name} | {r['trades']:.0f} | {r['win_pct']:.1f} "
                     f"| {r['avg_net']:+.2f} | {r['med_net']:+.2f} "
                     f"| {r['sum_net']:+.1f} | {r['avg_hold']:.1f} |")

    base = df.loc["baseline(現行)"] if "baseline(現行)" in df.index else None
    best = df.iloc[0]
    lines += [
        "",
        "## 結論",
        "",
        f"- 最佳情境：**{df.index[0]}**（累計淨 {best['sum_net']:+.1f}%、"
        f"勝率 {best['win_pct']:.1f}%、平均持有 {best['avg_hold']:.1f} 日）",
    ]
    if base is not None:
        lines.append(f"- 對比 baseline：累計淨 {base['sum_net']:+.1f}% → {best['sum_net']:+.1f}%，"
                     f"勝率 {base['win_pct']:.1f}% → {best['win_pct']:.1f}%")
    lines += [
        "- 個別規則的累計淨報酬若明顯高於 baseline，代表該規則「關掉更好」（過度出場）。",
        "- 是否採用由使用者決定；採用方式：改 agent/strategy.py 的 STRATEGY 後重跑 --mode backtest 確認。",
    ]

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ablation_result.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n結果已存 {out}")
    print(df.to_string())


if __name__ == "__main__":
    main()
