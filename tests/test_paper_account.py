from agent.paper_account import live_entry_share_count
from agent.strategy import FEE_RATE


def test_live_size_is_capped_by_cash_and_nav_slot():
    shares = live_entry_share_count(100, cash=30_000, nav=1_000_000,
                                    max_open=10, risk_shares=10_000,
                                    avg_volume=None, max_pct_of_avg_volume=0.01)
    assert shares == int(30_000 // (100 * (1 + FEE_RATE)))


def test_live_size_uses_tightest_risk_and_liquidity_cap():
    assert live_entry_share_count(100, 1_000_000, 1_000_000, 10,
                                  risk_shares=800, avg_volume=5_000,
                                  max_pct_of_avg_volume=0.01) == 50
    assert live_entry_share_count(100, 1_000_000, 1_000_000, 10,
                                  risk_shares=20, avg_volume=5_000,
                                  max_pct_of_avg_volume=0.01) == 20
