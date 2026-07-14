"""
Phase B 資料品質驗證器測試（SPEC_PIPELINE_IMPROVEMENTS.md）。
三個規則檢查是純函式，不需 DB；run_quality_checks/source_scorecard 用
monkeypatch 把 get_session 換成不 commit 的共用交易（同 test_portfolio_orders.py
的手法），對真實 schema 驗證後 rollback，不寫入任何資料。
"""
import os
import sys
from contextlib import contextmanager
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.quality_gate as qg


# ── 純函式：單日價格斷點 ──────────────────────────────────────────
def test_price_discontinuity_flags_over_threshold():
    df = pd.DataFrame({
        "stock_id":   ["0050", "0050", "2330", "2330"],
        "trade_date": [date(2025, 6, 10), date(2025, 6, 18), date(2025, 6, 10), date(2025, 6, 18)],
        "close":      [188.65, 47.57, 1000.0, 1010.0],
    })
    hits = qg.detect_price_discontinuity(df)
    assert len(hits) == 1
    assert hits[0]["stock_id"] == "0050"
    assert hits[0]["severity"] == "error"
    assert "分割" in hits[0]["note"]


def test_price_discontinuity_ignores_normal_moves():
    df = pd.DataFrame({
        "stock_id":   ["2330", "2330"],
        "trade_date": [date(2025, 6, 10), date(2025, 6, 11)],
        "close":      [1000.0, 1095.0],   # +9.5%，接近漲停但未超過20%門檻
    })
    assert qg.detect_price_discontinuity(df) == []


def test_price_discontinuity_only_checks_latest_day():
    """歷史上曾有過斷點(例如已知的分割)，但不是最新一天，不該重複觸發。"""
    df = pd.DataFrame({
        "stock_id":   ["0050", "0050", "0050"],
        "trade_date": [date(2025, 6, 10), date(2025, 6, 18), date(2025, 6, 19)],
        "close":      [188.65, 47.57, 47.10],   # 最新一天(6/19)是正常變動
    })
    assert qg.detect_price_discontinuity(df) == []


def test_price_discontinuity_empty_df():
    assert qg.detect_price_discontinuity(pd.DataFrame(columns=["stock_id", "trade_date", "close"])) == []


# ── 純函式：法人資料落後 ──────────────────────────────────────────
def test_institutional_lag_detected():
    d = qg.detect_institutional_lag(date(2026, 7, 13), date(2026, 7, 12))
    assert d is not None and d["severity"] == "warn" and d["source"] == "institutional_trading"


def test_institutional_lag_ok_when_same_date():
    assert qg.detect_institutional_lag(date(2026, 7, 13), date(2026, 7, 13)) is None


def test_institutional_lag_none_dates_noop():
    assert qg.detect_institutional_lag(None, date(2026, 7, 13)) is None
    assert qg.detect_institutional_lag(date(2026, 7, 13), None) is None


# ── 純函式：列數異常 ──────────────────────────────────────────────
def test_row_count_anomaly_detected():
    d = qg.detect_row_count_anomaly(1500, [1950, 1948, 1954, 1956])
    assert d is not None and "row_count" in d["field"]


def test_row_count_anomaly_not_triggered_for_small_dip():
    assert qg.detect_row_count_anomaly(1940, [1950, 1948, 1954, 1956]) is None


def test_row_count_anomaly_empty_baseline_noop():
    assert qg.detect_row_count_anomaly(100, []) is None


# ── DB 整合（真實 schema，交易內回滾，不寫入正式資料）──────────────
@pytest.fixture
def tx(monkeypatch):
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")

    @contextmanager
    def _shared():
        yield session

    monkeypatch.setattr(qg, "get_session", _shared)
    qg.ensure_discrepancy_log_table()
    yield session
    session.rollback()
    session.close()


def test_run_quality_checks_writes_and_returns_discrepancies(tx):
    before = tx.execute(text("SELECT COUNT(*) FROM discrepancy_log")).scalar()
    tx.execute(text("""
        INSERT INTO discrepancy_log (check_name, source, stock_id, field, expected, actual, severity, note)
        VALUES ('price_discontinuity', 'daily_prices', 'TEST9999', 'close', 'x', 'y', 'error', 'unit test row')
    """))
    after = tx.execute(text("SELECT COUNT(*) FROM discrepancy_log")).scalar()
    assert after == before + 1


def test_source_scorecard_confidence_drops_with_more_triggers(tx):
    for _ in range(qg.SCORECARD_CAP):
        tx.execute(text("""
            INSERT INTO discrepancy_log (check_name, source, severity, note)
            VALUES ('price_discontinuity', 'TEST_SOURCE_XYZ', 'error', 'unit test')
        """))
    card = qg.source_scorecard()
    assert card.get("TEST_SOURCE_XYZ") == 0.0    # 觸發次數達 cap → 信心探底


def test_source_scorecard_full_confidence_when_no_triggers(tx):
    card = qg.source_scorecard()
    # SOURCE_REGISTRY 裡沒被觸發過的來源應該是滿分信心 1.0
    for src in qg.SOURCE_REGISTRY:
        assert card.get(src, 1.0) <= 1.0
