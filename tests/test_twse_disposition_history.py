import gzip

from scripts.backfill_twse_disposition_history import (
    read_raw_response,
    raw_path,
    write_raw_response,
)
from scripts.build_twse_disposition_snapshot import build_snapshot_data


def punish_row(stock_id, announce, period, reason="reason", measure="measure"):
    return [1, announce, stock_id, "name", 1, reason, period, measure]


def test_raw_response_is_byte_deterministic_and_has_zero_gzip_mtime(tmp_path):
    payload = {
        "stat": "OK",
        "fields": ["a", "b"],
        "data": [["中", 1]],
    }
    first = tmp_path / "first.json.gz"
    second = tmp_path / "second.json.gz"

    write_raw_response(first, payload)
    write_raw_response(second, payload)

    assert first.read_bytes() == second.read_bytes()
    with gzip.open(first, "rb") as handle:
        assert handle.read()
        assert handle.mtime == 0
    assert read_raw_response(first) == payload


def test_snapshot_data_requires_every_year_and_preserves_raw_lineage(tmp_path):
    raw_root = tmp_path / "data" / "raw" / "twse" / "disposition"
    first = raw_path(raw_root, "punish", 2005)
    second = raw_path(raw_root, "punish", 2006)
    write_raw_response(first, {
        "stat": "OK",
        "data": [
            punish_row("2330", "94/01/03", "94/01/03～94/01/14"),
            punish_row("00632R", "94/01/03", "94/01/03～94/01/14"),
        ],
    })
    write_raw_response(second, {
        "stat": "OK",
        "data": [punish_row("2317", "95/02/01", "95/02/02～95/02/15")],
    })

    events, status = build_snapshot_data(raw_root, 2005, 2006, tmp_path)

    assert events["stock_id"].tolist() == ["2330", "2317"]
    assert events["source_year"].tolist() == [2005, 2006]
    assert events["raw_path"].str.startswith("data/raw/twse/disposition/").all()
    assert events["raw_sha256"].str.fullmatch(r"[0-9A-F]{64}").all()
    assert status["raw_rows"].tolist() == [2, 1]
    assert status["normalized_common_stock_rows"].tolist() == [1, 1]


def test_snapshot_data_fails_closed_when_a_required_year_is_missing(tmp_path):
    raw_root = tmp_path / "data" / "raw" / "twse" / "disposition"
    write_raw_response(raw_path(raw_root, "punish", 2005), {
        "stat": "OK", "data": [],
    })

    try:
        build_snapshot_data(raw_root, 2005, 2006, tmp_path)
    except FileNotFoundError as exc:
        assert "2006" in str(exc)
    else:
        raise AssertionError("missing annual archive must fail closed")
