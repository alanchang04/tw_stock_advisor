from scripts.research_data_audit import audit


def test_audit_empty_temporary_database(monkeypatch, tmp_path):
    import scripts.research_data_audit as module
    from data_pipeline.local_research_db import get_local_conn
    monkeypatch.setattr(module, "get_local_conn", lambda: get_local_conn(str(tmp_path / "r.db")))
    result = audit("2024-01-01", "2024-01-31")
    assert result["tables"]["daily_prices"]["rows"] == 0
    assert result["price_date_coverage"] == 0
