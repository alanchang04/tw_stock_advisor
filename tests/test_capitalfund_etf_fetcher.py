"""
群益投信ETF每日持股抓取測試（data_pipeline/fetchers/capitalfund_etf_fetcher.py）。
只測純函式 _parse_buyback_json（不需 Playwright/不打網路）。
真正的 Playwright 抓取邏輯已在開發時對真實網站手動驗證過（見對話紀錄），
不在自動化測試裡跑瀏覽器（太慢、且CI環境需額外裝chromium，交給整合驗證）。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.capitalfund_etf_fetcher import _parse_buyback_json, CAPITALFUND_FUNDS


def _payload(**over):
    base = {
        "code": 200,
        "data": {
            "pcf": {"date1": "2026-07-17", "date2": "2026-07-16"},
            "stocks": [
                {"stocNo": "2330", "stocName": "台積電", "weight": 9.586, "share": 1967000.0},
                {"stocNo": "5536", "stocName": "聖暉*", "weight": 9.3072, "share": 3729000.0},
            ],
        },
        "message": None,
    }
    base.update(over)
    return base


def test_parses_stocks_and_uses_date2_as_snapshot():
    df = _parse_buyback_json(_payload(), "00982A")
    assert len(df) == 2
    assert set(df["stock_id"]) == {"2330", "5536"}
    assert df.iloc[0]["snapshot_date"] == date(2026, 7, 16)


def test_weight_pct_carried_through():
    df = _parse_buyback_json(_payload(), "00982A")
    row = df[df["stock_id"] == "2330"].iloc[0]
    assert row["weight_pct"] == 9.586


def test_skips_non_numeric_stock_codes():
    payload = _payload(data={
        "pcf": {"date1": "2026-07-17", "date2": "2026-07-16"},
        "stocks": [
            {"stocNo": "2330", "stocName": "台積電", "weight": 9.586},
            {"stocNo": "CASH", "stocName": "現金", "weight": 1.2},   # 非個股列，應跳過
            {"stocNo": None, "stocName": "期貨", "weight": 0.5},
        ],
    })
    df = _parse_buyback_json(payload, "00982A")
    assert list(df["stock_id"]) == ["2330"]


def test_empty_stocks_returns_empty_df():
    df = _parse_buyback_json(_payload(data={"pcf": {}, "stocks": []}), "00982A")
    assert df.empty


def test_missing_data_key_returns_empty_df():
    assert _parse_buyback_json({}, "00982A").empty
    assert _parse_buyback_json(None, "00982A").empty


def test_malformed_date_falls_back_to_today():
    payload = _payload(data={
        "pcf": {"date1": "??", "date2": "not-a-date"},
        "stocks": [{"stocNo": "2330", "stocName": "台積電", "weight": 9.0}],
    })
    df = _parse_buyback_json(payload, "00982A")
    assert df.iloc[0]["snapshot_date"] == date.today()


def test_capitalfund_funds_mapping_has_both_tracked_etfs():
    assert CAPITALFUND_FUNDS == {"00982A": "399", "00992A": "500"}
