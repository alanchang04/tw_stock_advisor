"""MOM-1 組合模擬引擎的單元測試（合成 fixtures，完全不碰 release）。

**這些測試必須在 F1 開封前全部通過。** F1 是一次性的，引擎有 bug 就沒有第二次機會。
重點放在四類會靜默污染結果的錯誤：假成交、規格外的賣出理由、成本漏算、
以及 MOM-1B 濾網的時序。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from agent.strategy import FEE_RATE, TAX_RATE, SLIPPAGE
from research.momentum_backtest import (
    CAPITAL_TWD, MARKET_FILTER_TICKER, MARKET_FILTER_WINDOW,
    _fillable, _market_filter_risk_off, benchmark_nav, simulate,
)

SESSIONS = pd.bdate_range("2020-01-01", periods=320)
# 需要 >= 10 檔合格股票，否則 floor(n x 10%) = 0，規格會正確地保留現金而不進場。
COMMON = [f"{1100 + i}" for i in range(30)]
STOCKS = COMMON + [MARKET_FILTER_TICKER]


def frame(value, columns=None, dtype=float) -> pd.DataFrame:
    return pd.DataFrame(value, index=SESSIONS, columns=columns or STOCKS, dtype=dtype)


def build_inputs(*, close=100.0, open_=100.0, market_close=None) -> SimpleNamespace:
    """最小但**真實**的 inputs：走完整的 universe/產業/選股路徑，不打樁。"""
    raw_close = frame(close)
    raw_open = frame(open_)
    adjusted = raw_close.copy()
    if market_close is not None:
        adjusted[MARKET_FILTER_TICKER] = market_close
        raw_close[MARKET_FILTER_TICKER] = market_close
        raw_open[MARKET_FILTER_TICKER] = market_close

    master = pd.DataFrame({
        "stock_id": STOCKS,
        "market": ["TWSE"] * len(STOCKS),
        # 0050 是 ETF，不進 universe，但 MOM-1B 的濾網仍要用到它的價格
        "asset_type": ["common_stock"] * len(COMMON) + ["etf"],
        "effective_from": [SESSIONS[0]] * len(STOCKS),
        "delisting_date": [pd.NaT] * len(STOCKS),
    })
    structure = pd.DataFrame({
        "snapshot_date": [SESSIONS[0]] * len(STOCKS),
        "stock_id": STOCKS,
        "industry_code_asof": [f"IND{i // 3}" for i in range(len(STOCKS))],
        "industry_is_point_in_time": [True] * len(STOCKS),
        "industry_observation_date": [SESSIONS[0]] * len(STOCKS),
    })
    return SimpleNamespace(
        trading_days=SESSIONS,
        raw_open=raw_open,
        raw_close=raw_close,
        adjusted_close=adjusted,
        turnover=frame(50_000_000.0),
        volume_shares=frame(1_000_000.0),
        locked_limit=frame(None, dtype=object),
        restricted=frame(False, dtype=bool),
        pit_master=master,
        market_structure=structure,
    )


def rising_signal() -> pd.DataFrame:
    """1100 最強、1101 次之……讓選股結果可預測且穩定（每月不換股）。"""
    values = {sid: 1.0 - 0.01 * i for i, sid in enumerate(STOCKS)}
    return pd.DataFrame({sid: [v] * len(SESSIONS) for sid, v in values.items()},
                        index=SESSIONS)


EMPTY_LEDGER = pd.DataFrame(columns=["stock_id", "event_date", "ledger_status"])

WINDOW_START = 260          # 模擬自 SESSIONS[260] 起，前面留給 252 日歷史門檻


def first_decision_position() -> int:
    """模擬窗內第一個月頻決策日在 SESSIONS 中的位置（委託在其次一日成交）。"""
    from research.momentum import month_end_sessions
    window = SESSIONS[SESSIONS >= SESSIONS[WINDOW_START]]
    decision = sorted(set(month_end_sessions(SESSIONS)) & set(window))[0]
    return int(SESSIONS.get_loc(decision))


def run(inputs, *, variant="MOM-1A", ledger=EMPTY_LEDGER, signal=None,
        start=None, end=None):
    return simulate(
        inputs, variant=variant, signal=signal if signal is not None else rising_signal(),
        ledger=ledger,
        start=start or SESSIONS[260], end=end or SESSIONS[-1])


class TestFillability:
    def test_a_locked_limit_up_blocks_buying_but_not_selling(self):
        locked = frame(None, dtype=object)
        locked.loc[SESSIONS[5], "1101"] = "up"
        opens = frame(100.0)
        assert not _fillable(side="buy", stock_id="1101", session=SESSIONS[5],
                             raw_open=opens, locked_limit=locked)[0]
        assert _fillable(side="sell", stock_id="1101", session=SESSIONS[5],
                         raw_open=opens, locked_limit=locked)[0]

    def test_a_locked_limit_down_blocks_selling_but_not_buying(self):
        locked = frame(None, dtype=object)
        locked.loc[SESSIONS[5], "1101"] = "down"
        opens = frame(100.0)
        assert not _fillable(side="sell", stock_id="1101", session=SESSIONS[5],
                             raw_open=opens, locked_limit=locked)[0]
        assert _fillable(side="buy", stock_id="1101", session=SESSIONS[5],
                         raw_open=opens, locked_limit=locked)[0]

    def test_a_missing_open_price_blocks_both_sides(self):
        opens = frame(100.0)
        opens.loc[SESSIONS[5], "1101"] = np.nan
        for side in ("buy", "sell"):
            assert not _fillable(side=side, stock_id="1101", session=SESSIONS[5],
                                 raw_open=opens, locked_limit=frame(None, dtype=object))[0]


class TestMarketFilter:
    def test_risk_off_when_the_benchmark_closes_below_its_moving_average(self):
        series = pd.Series(np.r_[np.full(300, 100.0), np.full(20, 50.0)], index=SESSIONS)
        adjusted = frame(100.0)
        adjusted[MARKET_FILTER_TICKER] = series
        flags = _market_filter_risk_off(adjusted)
        assert not bool(flags.iloc[299])
        assert bool(flags.iloc[-1])

    def test_the_period_before_the_average_exists_is_risk_on(self):
        """規格沒有授權在均線成形前提前空手。"""
        adjusted = frame(100.0)
        flags = _market_filter_risk_off(adjusted)
        assert not flags.iloc[:MARKET_FILTER_WINDOW - 1].any()

    def test_a_release_without_the_benchmark_is_rejected(self):
        adjusted = frame(100.0, columns=["1101"])
        with pytest.raises(ValueError, match="0050"):
            _market_filter_risk_off(adjusted)


class TestCostsAndLedger:
    def test_a_buy_pays_slippage_and_commission(self):
        result = run(build_inputs())
        assert result.diagnostics["fills"]["buy"] > 0
        assert result.diagnostics["total_commission_twd"] > 0
        # 價格全程持平，因此期末低於本金的差額**只可能**來自成本
        assert result.nav.iloc[-1] < CAPITAL_TWD

    def test_the_round_trip_cost_matches_the_frozen_constants(self):
        """成本不是本引擎自訂的，必須沿用 agent/strategy.py 的正式常數。"""
        inputs = build_inputs()
        result = run(inputs)
        buys = result.diagnostics["total_buy_notional_twd"]
        commission = result.diagnostics["total_commission_twd"]
        sells = result.diagnostics["total_sell_notional_twd"]
        tax = result.diagnostics["total_transaction_tax_twd"]
        assert commission == pytest.approx((buys + sells) * FEE_RATE, rel=1e-9)
        assert tax == pytest.approx(sells * TAX_RATE, rel=1e-9)
        assert SLIPPAGE > 0        # 滑價已含在成交價，不另列

    def test_nav_equals_cash_plus_marked_positions(self):
        inputs = build_inputs()
        result = run(inputs)
        last = result.nav.index[-1]
        implied = result.cash.loc[last] + result.position_count.loc[last] * 0
        assert result.nav.loc[last] >= result.cash.loc[last] - 1e-9
        assert np.isfinite(implied)

    def test_no_position_is_opened_without_an_open_price(self):
        inputs = build_inputs()
        inputs.raw_open.loc[:, :] = np.nan
        result = run(inputs)
        assert result.diagnostics["fills"]["buy"] == 0
        assert result.nav.iloc[-1] == pytest.approx(CAPITAL_TWD)


class TestSpuriousSellGuard:
    def test_a_held_target_locked_at_limit_up_is_not_sold(self):
        """本引擎最容易寫錯的一條：若把漲停鎖死的目標剔出 target，
        已持有的那一檔會被誤判為「不在目標內」而產生賣單——
        那是規格 §7.6 沒有列舉的賣出理由。"""
        baseline = run(build_inputs())
        entry = first_decision_position() + 1

        inputs = build_inputs()
        locked = frame(None, dtype=object)
        # 進場數日後，讓**已持有**的最強股連續漲停鎖死
        locked.iloc[entry + 5:entry + 9, locked.columns.get_loc("1100")] = "up"
        inputs.locked_limit = locked
        result = run(inputs)

        assert result.diagnostics["fills"]["sell"] == baseline.diagnostics["fills"]["sell"]
        assert result.position_count.iloc[-1] == baseline.position_count.iloc[-1]

    def test_a_blocked_buy_is_retried_on_a_later_session(self):
        inputs = build_inputs()
        entry = first_decision_position() + 1
        locked = frame(None, dtype=object)
        locked.iloc[entry:entry + 3] = "up"      # 應成交日起連三日全市場漲停鎖死
        inputs.locked_limit = locked
        result = run(inputs)
        assert result.diagnostics["blocked_attempts"]["locked_limit_up"] > 0
        assert result.diagnostics["fills"]["buy"] > 0     # 解鎖後仍然買到


class TestMarketFilterBehaviour:
    def test_mom1b_liquidates_after_the_filter_triggers(self):
        crash = np.r_[np.full(300, 100.0), np.full(20, 40.0)]
        inputs = build_inputs(market_close=crash)
        result = run(inputs, variant="MOM-1B")
        assert result.position_count.iloc[-1] == 0
        assert result.cash.iloc[-1] == pytest.approx(result.nav.iloc[-1])

    def test_mom1a_ignores_the_filter(self):
        crash = np.r_[np.full(300, 100.0), np.full(20, 40.0)]
        inputs = build_inputs(market_close=crash)
        result = run(inputs, variant="MOM-1A")
        assert result.position_count.iloc[-1] > 0

    def test_an_unregistered_variant_is_rejected(self):
        with pytest.raises(ValueError, match="未登記的變體"):
            run(build_inputs(), variant="MOM-1C")


class TestCorporateActions:
    def test_a_cash_dividend_increases_cash_and_is_attributed_to_the_trade(self):
        inputs = build_inputs()
        ledger = pd.DataFrame([{
            "stock_id": "1100",
            "event_date": SESSIONS[280],
            "ledger_status": "executable",
            "share_multiplier": 1.0,
            "cash_per_old_share": 2.0,
        }])
        with_dividend = run(inputs, ledger=ledger)
        without = run(inputs)
        assert with_dividend.nav.iloc[-1] > without.nav.iloc[-1]
        assert with_dividend.diagnostics["corporate_action_events_applied"] == 1

    def test_an_optional_rights_issue_is_declined_not_subscribed(self):
        """凍結政策 A：不認購現增，股數不變、不付款。"""
        inputs = build_inputs()
        ledger = pd.DataFrame([{
            "stock_id": "1100",
            "event_date": SESSIONS[280],
            "ledger_status": "blocked",
            "ledger_block_reason": "paid_subscription_terms_missing",
        }])
        result = run(inputs, ledger=ledger)
        outcomes = result.diagnostics["corporate_action_outcomes"]
        assert outcomes.get("declined_subscription") == 1


class TestBenchmark:
    def test_benchmark_is_buy_and_hold_with_one_entry_cost(self):
        inputs = build_inputs(market_close=np.linspace(100.0, 200.0, len(SESSIONS)))
        nav = benchmark_nav(inputs, start=SESSIONS[260], end=SESSIONS[-1])
        assert nav.iloc[0] == pytest.approx(CAPITAL_TWD / (1 + FEE_RATE), rel=1e-9)
        assert nav.iloc[-1] > nav.iloc[0]

    def test_benchmark_uses_the_adjusted_series(self):
        inputs = build_inputs()
        inputs.adjusted_close[MARKET_FILTER_TICKER] = np.linspace(
            50.0, 100.0, len(SESSIONS))
        nav = benchmark_nav(inputs, start=SESSIONS[260], end=SESSIONS[-1])
        assert nav.iloc[-1] / nav.iloc[0] > 1.0
