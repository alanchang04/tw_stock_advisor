"""Regression tests for live/backtest candidate-filter parity."""

import datetime as dt

import pandas as pd

from agent.backtest import _candidates_asof, _eligible_stock_ids_asof
from agent.stock_selector import get_candidate_stocks
from agent.strategy import STRATEGY, apply_pre_score_filters


def _cfg(**overrides):
    return {
        **STRATEGY,
        "use_hot_sector_gate": False,
        "min_close": 1,
        "min_volume": 1,
        "min_rsi": 0,
        "max_rsi": 100,
        "min_turnover_avg5": 0,
        "min_turnover_percentile": None,
        "allow_new_entry_alt_gate": False,
        "hard_veto_upper_wick": False,
        "hard_veto_deviation_pct": 15,
        "above_ma20_only": False,
        "require_swing_setup": False,
        **overrides,
    }


def test_shared_filter_applies_basic_ma20_hard_veto_and_disposition():
    day = dt.date(2026, 7, 30)
    frame = pd.DataFrame([
        {"stock_id": "KEEP", "close": 105, "volume": 1000, "rsi14": 50,
         "ma5": 103, "ma20": 100},
        {"stock_id": "BELOW", "close": 95, "volume": 1000, "rsi14": 50,
         "ma5": 96, "ma20": 100},
        {"stock_id": "VETO", "close": 130, "volume": 1000, "rsi14": 50,
         "ma5": 110, "ma20": 100},
        {"stock_id": "DISP", "close": 105, "volume": 1000, "rsi14": 50,
         "ma5": 103, "ma20": 100},
    ])
    disposition = {"DISP": [(day, day)]}

    kept, hard, disp = apply_pre_score_filters(
        frame,
        cfg=_cfg(above_ma20_only=True),
        as_of=day,
        disposition_idx=disposition,
    )

    assert kept["stock_id"].tolist() == ["KEEP"]
    assert hard["stock_id"].tolist() == ["VETO"]
    assert disp["stock_id"].tolist() == ["DISP"]


def test_backtest_uses_mapped_common_stock_universe_and_hard_veto():
    day = dt.date(2026, 7, 30)
    stocks = ["KEEP", "VETO", "ETF1", "UNMAPPED"]
    closes = [105.0, 130.0, 105.0, 105.0]
    prices = pd.DataFrame({
        "stock_id": stocks,
        "trade_date": [day] * 4,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [10_000.0] * 4,
        "turnover": [100_000_000.0] * 4,
        "change_pct": [0.0] * 4,
    })
    tech = pd.DataFrame({
        "stock_id": stocks,
        "trade_date": [day] * 4,
        "ma5": [103.0, 110.0, 103.0, 103.0],
        "ma20": [100.0] * 4,
        "ma60": [95.0] * 4,
        "rsi14": [50.0] * 4,
        "macd_hist": [0.0] * 4,
        "signal_ma_cross": [False] * 4,
        "signal_breakout": [False] * 4,
    })
    data = {
        "prices": prices,
        "tech": tech,
        "inst": pd.DataFrame({
            "stock_id": stocks,
            "trade_date": [day] * 4,
            "total_net": [0.0] * 4,
            "foreign_net": [0.0] * 4,
        }),
        "imap": pd.DataFrame([
            {"stock_id": "KEEP", "industry_code": "SEM"},
            {"stock_id": "VETO", "industry_code": "SEM"},
            {"stock_id": "ETF1", "industry_code": "ETF"},
        ]),
        "inds": pd.DataFrame([
            {"industry_code": "SEM", "name_zh": "半導體"},
            {"industry_code": "ETF", "name_zh": "ETF"},
        ]),
        "_avg_turnover": pd.DataFrame(
            [[100_000_000.0] * 4], index=[day], columns=stocks
        ),
        "_avg_volume_liq": pd.DataFrame(
            [[10_000.0] * 4], index=[day], columns=stocks
        ),
        "_closes": pd.DataFrame([closes], index=[day], columns=stocks),
        "_disposition_idx": {},
        "rev_map": {},
    }

    selected = _candidates_asof(
        data, day, industry_codes=None, top_n=10, cfg=_cfg()
    )

    assert selected == ["KEEP"]


class _EmptyResult:
    def fetchall(self):
        return []

    def keys(self):
        return []


class _CaptureSession:
    def __init__(self):
        self.params = []

    def execute(self, _stmt, params=None):
        self.params.append(params or {})
        return _EmptyResult()


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_args):
        return False


def test_live_sql_respects_cfg_price_and_volume(monkeypatch):
    session = _CaptureSession()
    monkeypatch.setattr(
        "agent.stock_selector.get_session",
        lambda: _SessionContext(session),
    )

    out = get_candidate_stocks(
        [], top_n=5, cfg=_cfg(min_close=77, min_volume=321)
    )

    assert out.empty
    assert session.params[0]["min_close"] == 77
    assert session.params[0]["min_volume"] == 321


def test_point_in_time_universe_respects_listing_and_delisting_dates():
    day = dt.date(2024, 6, 3)
    data = {
        "imap": pd.DataFrame([
            {"stock_id": "1111", "industry_code": "SEM"},
            {"stock_id": "2222", "industry_code": "SEM"},
            {"stock_id": "3333", "industry_code": "SEM"},
        ]),
        "inds": pd.DataFrame([
            {"industry_code": "SEM", "name_zh": "半導體"},
        ]),
        "stocks": pd.DataFrame([
            {"stock_id": "1111", "market": "TWSE",
             "listing_date": dt.date(2020, 1, 1), "is_active": True},
            {"stock_id": "2222", "market": "TWSE",
             "listing_date": dt.date(2025, 1, 1), "is_active": True},
            {"stock_id": "3333", "market": "TPEX",
             "listing_date": dt.date(2020, 1, 1), "is_active": False},
        ]),
        "delisted": pd.DataFrame([
            {"stock_id": "3333", "delisting_date": dt.date(2024, 5, 31),
             "market": "TPEX"},
        ]),
        "prices": pd.DataFrame([
            {"stock_id": sid, "trade_date": dt.date(2020, 1, 2)}
            for sid in ("1111", "2222", "3333")
        ]),
    }

    assert _eligible_stock_ids_asof(data, day) == {"1111"}


def test_point_in_time_universe_uses_first_trade_when_listing_date_missing():
    before = dt.date(2023, 12, 29)
    first = dt.date(2024, 1, 2)
    data = {
        "imap": pd.DataFrame([
            {"stock_id": "4444", "industry_code": "SEM"},
        ]),
        "inds": pd.DataFrame([
            {"industry_code": "SEM", "name_zh": "半導體"},
        ]),
        "stocks": pd.DataFrame([
            {"stock_id": "4444", "market": "TWSE",
             "listing_date": pd.NaT, "is_active": True},
        ]),
        "delisted": pd.DataFrame(
            columns=["stock_id", "delisting_date", "market"]
        ),
        "prices": pd.DataFrame([
            {"stock_id": "4444", "trade_date": first},
        ]),
    }

    assert _eligible_stock_ids_asof(data, before) == set()
    assert _eligible_stock_ids_asof(data, first) == {"4444"}


def test_point_in_time_universe_prefers_snapshot_over_current_mapping():
    day = dt.date(2026, 7, 31)
    data = {
        "imap": pd.DataFrame([
            {"stock_id": "1111", "industry_code": "CURRENT"},
            {"stock_id": "2222", "industry_code": "CURRENT"},
        ]),
        "universe_history": pd.DataFrame([
            {"snapshot_date": day, "stock_id": "1111", "market": "TWSE",
             "industry_code": "HIST", "asset_type": "common_stock",
             "listing_date": dt.date(2020, 1, 1), "delisting_date": None,
             "is_active": True},
            {"snapshot_date": day, "stock_id": "2222", "market": "TWSE",
             "industry_code": "HIST", "asset_type": "common_stock",
             "listing_date": dt.date(2020, 1, 1), "delisting_date": None,
             "is_active": False},
        ]),
        "inds": pd.DataFrame([
            {"industry_code": "HIST", "name_zh": "歷史產業"},
            {"industry_code": "CURRENT", "name_zh": "目前產業"},
        ]),
        "stocks": pd.DataFrame([
            {"stock_id": "1111", "market": "TWSE",
             "listing_date": dt.date(2020, 1, 1), "is_active": True},
            {"stock_id": "2222", "market": "TWSE",
             "listing_date": dt.date(2020, 1, 1), "is_active": True},
        ]),
        "delisted": pd.DataFrame(
            columns=["stock_id", "delisting_date", "market"]
        ),
        "prices": pd.DataFrame([
            {"stock_id": "1111", "trade_date": dt.date(2020, 1, 2)},
            {"stock_id": "2222", "trade_date": dt.date(2020, 1, 2)},
        ]),
    }

    assert _eligible_stock_ids_asof(
        data, day, industry_codes=["HIST"], use_hot_sector_gate=True
    ) == {"1111"}
