from __future__ import annotations

import json
from pathlib import Path

from agent.cockpit import (current_strategy_status, load_forward_status,
                           present_regime)


def test_forward_status_counts_observations_and_exposes_freshness(tmp_path: Path):
    path = tmp_path / "forward.json"
    path.write_text(json.dumps({
        "forward_start": "2026-08-14",
        "observations": [{"month": 1}, {"month": 2}],
        "snapshot_freshness": {
            "last_trade_date": "2026-08-18", "lag_days": 0,
            "threshold_days": 45, "is_stale": False,
        },
    }), encoding="utf-8")
    status = load_forward_status(path)
    assert status == {
        "ok": True,
        "forward_start": "2026-08-14",
        "periods": 2,
        "last_trade_date": "2026-08-18",
        "lag_days": 0,
        "is_stale": False,
        "threshold_days": 45,
    }


def test_forward_status_never_turns_a_broken_journal_into_zero_periods(tmp_path: Path):
    path = tmp_path / "broken.json"
    path.write_text("{}", encoding="utf-8")
    status = load_forward_status(path)
    assert status["ok"] is False
    assert "periods" not in status


def test_failed_market_query_is_not_presented_as_risk_on():
    shown = present_regime({"ok": False, "bull": True, "state": "risk_on"})
    assert shown["ok"] is False
    assert "多頭" not in shown["label"]
    assert "bull=True" in shown["caption"]


def test_regime_presentation_keeps_state_and_diagnostics():
    shown = present_regime({
        "ok": True, "state": "risk_off", "close": 98, "ma60": 100,
        "breadth": 0.32, "exposure_scale": 0.5,
    })
    assert shown["label"] == "空頭／Risk-off"
    assert "市場寬度 32%" in shown["caption"]
    assert "曝險倍率 50%" in shown["caption"]


def test_legacy_two_state_helper_is_shown_truthfully_not_as_neutral():
    bull = present_regime({"ok": True, "bull": True, "close": 101, "ma60": 100})
    bear = present_regime({"ok": True, "bull": False, "close": 99, "ma60": 100})
    assert bull["label"] == "多頭／Risk-on"
    assert bear["label"] == "空頭／Risk-off"
    assert "bull 二態" in bull["caption"]


def test_strategy_status_comes_from_strategy_eras():
    status = current_strategy_status()
    assert status["ok"] is True
    assert status["key"] == "v3"
    assert str(status["live_from"]) == "2026-07-24"
