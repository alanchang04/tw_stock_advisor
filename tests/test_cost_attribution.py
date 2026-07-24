"""SPEC §5-5 成本與換手率的測試。

換手率算錯會直接誤導 P3 的第一個結論（15pp 落差有多少是成本造成的），
所以分母定義（部位格數 vs 總資金）特別要釘住。
"""
import numpy as np
import pandas as pd
import pytest

from agent.strategy import FEE_RATE, SLIPPAGE, TAX_RATE
from research.cost_attribution import (ROUND_TRIP_COST, cost_metrics,
                                       edge_gap_attribution, format_report,
                                       round_trip_cost_breakdown)


def _trades(n=100, hold=39, ret=0.069):
    return pd.DataFrame({"hold": [hold] * n, "net_ret": [ret] * n})


# ── 成本常數 ─────────────────────────────────────────────────────
def test_round_trip_cost_matches_strategy_constants():
    """單一事實來源是 strategy.py——這裡若跟它對不上，兩邊會各說各話。"""
    assert ROUND_TRIP_COST == pytest.approx(2 * FEE_RATE + TAX_RATE + 2 * SLIPPAGE)


def test_breakdown_components_sum_to_total():
    b = round_trip_cost_breakdown()
    assert b["手續費(買+賣)"] + b["證交稅(賣)"] + b["滑價(買+賣)"] == pytest.approx(b["合計"])


def test_round_trip_cost_is_around_107bp():
    """規格書多處引用「來回摩擦成本約 1.07%」，變動時要有人注意到。"""
    assert 0.010 < ROUND_TRIP_COST < 0.011


# ── 換手率 ───────────────────────────────────────────────────────
def test_turnover_uses_slot_count_as_denominator():
    """10 格各周轉 6 次 ≠ 1 格周轉 60 次——分母搞錯，成本會差 10 倍。"""
    cm = cost_metrics(_trades(n=600), n_days=2520, max_open=10)   # 10年、600筆
    assert cm["每格每年周轉次數"] == pytest.approx(600 / 10 / 10)
    cm1 = cost_metrics(_trades(n=600), n_days=2520, max_open=1)
    assert cm1["每格每年周轉次數"] == pytest.approx(600 / 1 / 10)


def test_annual_cost_scales_linearly_with_trade_count():
    a = cost_metrics(_trades(n=100), n_days=2520)["年化摩擦成本占比"]
    b = cost_metrics(_trades(n=200), n_days=2520)["年化摩擦成本占比"]
    assert b == pytest.approx(2 * a)


def test_annual_cost_matches_hand_calculation():
    """10年、484筆、10格 → 每格每年 4.84 次 × 1.07% ≈ 5.2%/年。"""
    cm = cost_metrics(_trades(n=484), n_days=2520, max_open=10)
    assert cm["年化摩擦成本占比"] == pytest.approx(4.84 * ROUND_TRIP_COST)
    assert 0.04 < cm["年化摩擦成本占比"] < 0.06


def test_full_period_cost_is_annual_times_years():
    cm = cost_metrics(_trades(n=484), n_days=2520)
    assert cm["全期摩擦成本占比"] == pytest.approx(cm["年化摩擦成本占比"] * cm["年數"])


def test_cost_share_of_gross_return():
    """每筆淨賺 6.9%、成本 1.07% → 成本約佔毛報酬的 13%。"""
    cm = cost_metrics(_trades(ret=0.069), n_days=2520)
    assert cm["成本佔每筆毛報酬"] == pytest.approx(
        ROUND_TRIP_COST / (0.069 + ROUND_TRIP_COST))


def test_empty_input_returns_empty_dict():
    assert cost_metrics(pd.DataFrame(), n_days=2520) == {}
    assert cost_metrics(_trades(), n_days=0) == {}


# ── 落差歸因 ─────────────────────────────────────────────────────
def test_attribution_leaves_unexplained_explicit():
    """未解釋的部分必須留在「未解釋」欄，不能被塞進任何一項。"""
    cm = cost_metrics(_trades(n=484), n_days=2520)
    a = edge_gap_attribution(0.085, -0.0691, cm)
    assert a["落差"] == pytest.approx(0.085 + 0.0691)
    assert a["已解釋:摩擦成本"] == cm["年化摩擦成本占比"]
    assert a["未解釋"] == pytest.approx(a["落差"] - a["已解釋:摩擦成本"])
    assert a["未解釋"] > 0          # 成本解釋不完，這件事本身就是結論


def test_attribution_handles_missing_cost():
    a = edge_gap_attribution(0.085, -0.0691, {})
    assert a["未解釋"] == pytest.approx(a["落差"])


# ── 報告 ─────────────────────────────────────────────────────────
def test_report_flags_slippage_is_an_assumption():
    """滑價 30bp 是假設不是量測，這個警語不可以消失。"""
    txt = format_report(cost_metrics(_trades(n=484), n_days=2520))
    assert "假設" in txt and "年化摩擦成本占比" in txt


def test_report_with_attribution_warns_against_narrative_filling():
    cm = cost_metrics(_trades(n=484), n_days=2520)
    txt = format_report(cm, edge_gap_attribution(0.085, -0.0691, cm))
    assert "未解釋" in txt and "敘事填補" in txt


def test_report_empty_when_no_data():
    assert format_report({}) == ""
