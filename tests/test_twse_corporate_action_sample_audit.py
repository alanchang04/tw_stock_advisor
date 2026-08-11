from datetime import date

import pandas as pd

from scripts.audit_twse_corporate_action_samples import audit_samples
from scripts.backfill_twse_corporate_actions import (
    REPORT_EX_RIGHT,
    parse_twt49u,
    sha256_file,
    write_raw_response,
)


def test_official_sample_audit_reopens_raw_row_and_matches_fields(tmp_path):
    payload = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價",
                   "權值", "息值", "權值+息值", "權/息"],
        "data": [["94年01月11日", "6280", "崇貿", "33.00", "27.48", 5.52, 0,
                  "5.520000", "權"]],
    }
    raw_path = tmp_path / "TWT49U_200501.json.gz"
    write_raw_response(raw_path, payload)
    row = parse_twt49u(payload).iloc[0].to_dict()
    row.update({
        "period_start": date(2005, 1, 1).isoformat(),
        "period_end": date(2005, 1, 31).isoformat(),
        "source_row_number": 1,
        "raw_path": str(raw_path),
        "raw_sha256": sha256_file(raw_path),
    })
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    pd.DataFrame([row]).to_parquet(
        snapshot / "corporate_action_source_rows.parquet", index=False
    )

    report, samples = audit_samples(snapshot, repo_root=tmp_path, minimum_samples=1)

    assert report["passed"]
    assert report["sample_rows"] == 1
    assert samples["raw_sha256_matches"].item()
    assert samples["normalized_fields_match"].item()
