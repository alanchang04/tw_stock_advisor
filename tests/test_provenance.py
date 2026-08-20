"""Provenance block for research reports.

These are the fields another machine needs to tell "your data differs from mine"
apart from "your code is broken", so each one is pinned by a test.
"""
import datetime as dt

import pandas as pd

from research.provenance import data_fingerprint, environment_block


def _prices() -> pd.DataFrame:
    days = [dt.date(2020, 1, 1), dt.date(2020, 1, 2), dt.date(2020, 1, 3)]
    rows = []
    for d in days:
        # Two names on the first two days, three on the last.
        for sid in (["A", "B"] if d != days[-1] else ["A", "B", "C"]):
            rows.append({"stock_id": sid, "trade_date": d, "close": 10.0})
    return pd.DataFrame(rows)


def test_environment_block_reports_runtime_versions():
    block = environment_block()
    assert block["pandas"] == pd.__version__
    assert block["python"].count(".") >= 2
    assert block["numpy"]


def test_data_fingerprint_captures_shape_and_boundaries():
    fingerprint = data_fingerprint({"prices": _prices()})
    assert fingerprint["price_rows"] == 7
    assert fingerprint["price_stocks"] == 3
    assert fingerprint["price_days"] == 3
    assert fingerprint["price_first"] == "2020-01-01"
    assert fingerprint["price_last"] == "2020-01-03"
    assert fingerprint["median_stocks_per_day"] == 2.0


def test_last_date_moves_when_dataset_grows():
    prices = _prices()
    extended = pd.concat([prices, pd.DataFrame([
        {"stock_id": "A", "trade_date": dt.date(2020, 1, 6), "close": 11.0}
    ])], ignore_index=True)

    before = data_fingerprint({"prices": prices})
    after = data_fingerprint({"prices": extended})

    assert before["price_last"] != after["price_last"]


def test_fingerprint_is_empty_without_data():
    assert data_fingerprint(None) == {}
    assert data_fingerprint({}) == {}
    assert data_fingerprint({"prices": pd.DataFrame()}) == {}


def test_environment_block_embeds_fingerprint_and_parquet_dir():
    block = environment_block({"prices": _prices()}, parquet_dir="data/research")
    assert block["parquet_dir"] == "data/research"
    assert block["data"]["price_rows"] == 7
