"""
SPEC_QUANT_UPGRADE §5-5 成本意識 + §5.0 P3 第一題（15pp 歸因分解）的成本那一塊。

§5-5 原話：
    **成本意識**：回測報告新增「年化摩擦成本占比」，換手率成為一級指標。

**為什麼現在做這個**：§4.6 判決後 P3 的第一個題目是「核心 edge 的 CAR 是 +8.5%/年，
組合實際交出來是 -6.91%/年，中間 15pp 跑去哪」。成本是四個嫌疑犯裡最容易量、
也最可能是大宗的一個——39 天持有 × 來回 1.07% ≈ 7%/年，光這一項就佔落差的一半。

**摩擦成本的三個來源**（常數在 `agent/strategy.py`，回測與實盤帳本共用）：
    手續費 FEE_RATE  = 14.25bp × 58折 = 0.8265bp × 2（買賣各一次）
    證交稅 TAX_RATE  = 30bp（僅賣出）
    滑價   SLIPPAGE  = 30bp × 2（買賣各一次，⚠️這是假設不是量測）

**一個重要的區分**：滑價已經內含在成交價裡（buy_fill/sell_fill），手續費與證交稅
則由 net_return() 扣除。所以「淨報酬」已經扣掉全部成本了——本模組不是要重扣，
而是要把**成本的絕對量與佔比顯性化**，因為 §5-5 的重點是「換手率成為一級指標」：
同樣的毛報酬，換手兩倍就等於多付一倍過路費，而現行報告完全看不到這件事。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agent.strategy import FEE_RATE, SLIPPAGE, TAX_RATE

#: 一次完整來回（買+賣）的摩擦成本率。買賣各一次手續費與滑價，證交稅只在賣出收。
ROUND_TRIP_COST = 2 * FEE_RATE + TAX_RATE + 2 * SLIPPAGE

TRADING_DAYS_PER_YEAR = 252


def round_trip_cost_breakdown() -> dict:
    """一次來回的成本拆解（bp）。單一事實來源在 strategy.py，這裡只做呈現。"""
    return {
        "手續費(買+賣)": 2 * FEE_RATE,
        "證交稅(賣)": TAX_RATE,
        "滑價(買+賣)": 2 * SLIPPAGE,
        "合計": ROUND_TRIP_COST,
    }


def cost_metrics(trades: pd.DataFrame, n_days: int,
                 max_open: int = 10) -> dict:
    """
    換手率與年化摩擦成本。

    trades：回測交易表，需有 hold（持有交易日數）與 net_ret。
    n_days：回測期間的交易日總數（用來年化）。
    max_open：同時持倉上限——換手率要用「部位格數」當分母才有意義，
              10 格各周轉 6 次 ≠ 1 格周轉 60 次。

    **年化摩擦成本占比**的定義：每年每一格部位付出的成本 ÷ 1，
    也就是「一整年下來，摩擦成本吃掉本金的百分之幾」。
    """
    if trades is None or len(trades) == 0 or n_days <= 0:
        return {}
    years = n_days / TRADING_DAYS_PER_YEAR
    n_trades = len(trades)
    hold = pd.to_numeric(trades["hold"], errors="coerce").dropna()

    # 每格部位每年周轉幾次：總交易數 ÷ 格數 ÷ 年數
    turnover_per_slot_year = n_trades / max(1, max_open) / years
    # 年化摩擦成本 = 每格每年周轉次數 × 每次來回成本（每格資金佔比 1/max_open，
    # 但成本也是按該格資金計，兩者相抵 → 直接乘即為佔總資金比例）
    ann_cost = turnover_per_slot_year * ROUND_TRIP_COST

    gross = pd.to_numeric(trades["net_ret"], errors="coerce").dropna()
    return {
        "交易筆數": n_trades,
        "年數": years,
        "每年交易筆數": n_trades / years,
        "每格每年周轉次數": turnover_per_slot_year,
        "平均持有交易日": float(hold.mean()) if len(hold) else np.nan,
        "來回成本率": ROUND_TRIP_COST,
        "年化摩擦成本占比": ann_cost,
        "全期摩擦成本占比": ann_cost * years,
        "平均每筆淨報酬": float(gross.mean()) if len(gross) else np.nan,
        # 成本佔每筆毛報酬的比例——這是「值不值得做這筆交易」的直觀指標
        "成本佔每筆毛報酬": (ROUND_TRIP_COST / (gross.mean() + ROUND_TRIP_COST)
                            if len(gross) and (gross.mean() + ROUND_TRIP_COST) > 0
                            else np.nan),
    }


def edge_gap_attribution(car_annual: float, actual_excess_annual: float,
                         cost_metrics_out: dict) -> dict:
    """
    §5.0 第一題的骨架：把「CAR 宣稱的 edge」到「組合實際超額」的落差拆開。

    **目前只有成本這一項是量出來的**，其餘三項（集中度／市場濾網／停損砍斷）
    尚未量化，一律列為「未解釋」——**不准把未解釋的部分算到任何一項頭上**，
    那就變成用敘事填補數字，正是這份規格書要避免的事。
    """
    cost = cost_metrics_out.get("年化摩擦成本占比", np.nan)
    gap = car_annual - actual_excess_annual
    explained = cost if not np.isnan(cost) else 0.0
    return {
        "CAR宣稱(年化)": car_annual,
        "實際超額(年化)": actual_excess_annual,
        "落差": gap,
        "已解釋:摩擦成本": cost,
        "未解釋": gap - explained,
        "已解釋佔比": explained / gap if gap else np.nan,
    }


def format_report(cm: dict, attribution: dict | None = None) -> str:
    """§5-5 要求的報告區塊（納入回測輸出）。"""
    if not cm:
        return ""
    b = round_trip_cost_breakdown()
    L = ["─" * 66, "成本與換手率（SPEC §5-5：換手率是一級指標）", "─" * 66]
    L.append(f"  來回成本 {b['合計']*100:.3f}%　＝ 手續費 {b['手續費(買+賣)']*100:.3f}%"
             f" + 證交稅 {b['證交稅(賣)']*100:.3f}% + 滑價 {b['滑價(買+賣)']*100:.3f}%")
    L.append(f"  ⚠️ 滑價 30bp 是**假設**不是量測，待實盤成交回報校準")
    L.append("")
    L.append(f"  平均持有 {cm['平均持有交易日']:.1f} 個交易日　"
             f"每年 {cm['每年交易筆數']:.1f} 筆　"
             f"每格部位每年周轉 {cm['每格每年周轉次數']:.2f} 次")
    L.append(f"  **年化摩擦成本占比 {cm['年化摩擦成本占比']*100:.2f}%**"
             f"（全期 {cm['全期摩擦成本占比']*100:.1f}%）")
    L.append(f"  成本佔每筆毛報酬 {cm['成本佔每筆毛報酬']*100:.1f}%"
             f"（平均每筆淨報酬 {cm['平均每筆淨報酬']*100:+.2f}%）")
    if attribution:
        a = attribution
        L.append("")
        L.append("  【edge 落差歸因（§5.0 P3 第一題，進行中）】")
        L.append(f"    核心 edge CAR 宣稱　 {a['CAR宣稱(年化)']*100:>+7.2f}%/年")
        L.append(f"    組合實際超額　　　　 {a['實際超額(年化)']*100:>+7.2f}%/年")
        L.append(f"    落差　　　　　　　　 {a['落差']*100:>7.2f}pp")
        L.append(f"      已解釋：摩擦成本　 {a['已解釋:摩擦成本']*100:>7.2f}pp"
                 f"（佔落差 {a['已解釋佔比']*100:.0f}%）")
        L.append(f"      **未解釋　　　　　 {a['未解釋']*100:>7.2f}pp**"
                 f" ← 集中度／市場濾網／停損砍斷，尚未量化")
        L.append("    ⚠️ 未解釋的部分不得歸因到任何一項——那是用敘事填補數字。")
    L.append("─" * 66)
    return "\n".join(L)
