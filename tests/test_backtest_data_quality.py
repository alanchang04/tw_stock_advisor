import pandas as pd

from research.data_quality import backtest_data_quality


def _data(dates):
    rows = []
    for d in dates:
        for sid in ("0050", "2330"):
            rows.append({"stock_id": sid, "trade_date": d, "open": 1, "high": 1,
                         "low": 1, "close": 1, "volume": 1})
    prices = pd.DataFrame(rows)
    inst = prices[["stock_id", "trade_date"]].assign(total_net=0)
    return {"prices": prices, "inst": inst}


def test_complete_dataset_passes_gate():
    assert backtest_data_quality(_data(pd.bdate_range("2024-01-01", periods=30)))["passed"]


def test_sparse_dataset_fails_gate():
    result = backtest_data_quality(_data(pd.to_datetime(["2024-01-01", "2024-06-28"])))
    assert not result["passed"]
    assert "date coverage" in result["errors"][0]
