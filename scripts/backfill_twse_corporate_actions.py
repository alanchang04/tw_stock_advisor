"""Archive and normalize official TWSE corporate-action reference-price reports.

This is a D3 staging builder.  It intentionally keeps raw prices separate and
does not run strategy code or expose backward-holdout performance.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_pipeline.fetchers.twse_fetcher import _get_json
from research.snapshot_manifest import (
    build_manifest,
    build_quality_report,
    canonical_json_sha256,
    write_json,
)


URL = "https://www.twse.com.tw/exchangeReport/{report}"
REPORT_EX_RIGHT = "TWT49U"
REPORT_REDUCTION = "TWTAUU"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def portable_path(path: str | Path) -> str:
    """Prefer a repository-relative path so snapshots reproduce across machines."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _num(value) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "--", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _roc_date(value) -> date | None:
    text = str(value or "").strip().replace("年", "/").replace("月", "/").replace("日", "")
    parts = text.split("/")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _row_mapping(fields: list, values: list) -> dict:
    return {str(field).strip(): values[index] if index < len(values) else None
            for index, field in enumerate(fields)}


def parse_twt49u(payload: dict) -> pd.DataFrame:
    records = []
    fields = payload.get("fields") or []
    for values in payload.get("data") or []:
        row = _row_mapping(fields, values)
        event_date = _roc_date(row.get("資料日期"))
        stock_id = str(row.get("股票代號") or "").strip()
        if not event_date or not stock_id:
            continue
        raw_type = str(row.get("權/息") or "").strip()
        event_kind = {
            "權": "ex_right",
            "息": "ex_dividend",
            "權息": "ex_right_dividend",
            "除權": "ex_right",
            "除息": "ex_dividend",
            "除權息": "ex_right_dividend",
        }.get(raw_type, "ex_right_dividend")
        pre_close = _num(row.get("除權息前收盤價"))
        reference_price = _num(row.get("除權息參考價"))
        combined_value = _num(row.get("權值+息值"))
        rights_value = _num(row.get("權值"))
        cash_value = _num(row.get("息值"))
        # 2009 起部分 TWT49U 回應只保留「權值+息值」，不再拆成兩欄。純除息／
        # 純除權事件仍可由事件種類無歧義還原；只有「除權息」合併事件維持缺值，
        # 絕不把合計數任意當成現金或股票股利。
        if cash_value is None and event_kind == "ex_dividend":
            cash_value = combined_value
        if rights_value is None and event_kind == "ex_right":
            rights_value = combined_value
        records.append({
            "source_report": REPORT_EX_RIGHT,
            "stock_id": stock_id,
            "stock_name": str(row.get("股票名稱") or "").strip(),
            "event_date": event_date.isoformat(),
            "event_kind": event_kind,
            "pre_event_close": pre_close,
            "reference_price": reference_price,
            "opening_reference_price": _num(row.get("開盤競價基準")),
            "ex_right_reference_price": _num(row.get("減除股利參考價")),
            "rights_value": rights_value,
            "cash_value": cash_value,
            "combined_value": combined_value,
            "upper_limit": _num(row.get("漲停價格")),
            "lower_limit": _num(row.get("跌停價格")),
            "reduction_reason": None,
            "detail_key": str(row.get("詳細資料") or "").strip() or None,
            "adjustment_factor": (
                reference_price / pre_close
                if pre_close and reference_price and pre_close > 0 and reference_price > 0
                else None
            ),
        })
    return pd.DataFrame(records)


def parse_twtauu(payload: dict) -> pd.DataFrame:
    records = []
    fields = payload.get("fields") or []
    for values in payload.get("data") or []:
        row = _row_mapping(fields, values)
        event_date = _roc_date(row.get("恢復買賣日期"))
        stock_id = str(row.get("股票代號") or "").strip()
        if not event_date or not stock_id:
            continue
        pre_close = _num(row.get("停止買賣前收盤價格"))
        reference_price = _num(row.get("恢復買賣參考價"))
        records.append({
            "source_report": REPORT_REDUCTION,
            "stock_id": stock_id,
            "stock_name": str(row.get("名稱") or "").strip(),
            "event_date": event_date.isoformat(),
            "event_kind": "capital_reduction",
            "pre_event_close": pre_close,
            "reference_price": reference_price,
            "opening_reference_price": _num(row.get("開盤競價基準")),
            "ex_right_reference_price": _num(row.get("除權參考價")),
            "rights_value": None,
            "cash_value": None,
            "combined_value": None,
            "upper_limit": _num(row.get("漲停價格")),
            "lower_limit": _num(row.get("跌停價格")),
            "reduction_reason": str(row.get("減資原因") or "").strip() or None,
            "detail_key": str(row.get("詳細資料") or "").strip() or None,
            "adjustment_factor": (
                reference_price / pre_close
                if pre_close and reference_price and pre_close > 0 and reference_price > 0
                else None
            ),
        })
    return pd.DataFrame(records)


def raw_path(raw_root: str | Path, report: str, period_start: date) -> Path:
    folder = "ex_right_dividend" if report == REPORT_EX_RIGHT else "capital_reduction"
    return Path(raw_root) / folder / str(period_start.year) / f"{report}_{period_start:%Y%m}.json.gz"


def write_raw_response(path: str | Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_raw_response(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def raw_acquired_at(path: str | Path) -> str:
    """Return the gzip member timestamp so cached rebuilds keep source metadata stable."""
    with gzip.open(path, "rb") as handle:
        handle.peek(1)  # force gzip header parsing; GzipFile.mtime is then available
        timestamp = handle.mtime
    if timestamp is None:
        raise ValueError(f"gzip source has no acquisition timestamp: {path}")
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS corporate_action_events (
            source_report TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            pre_event_close REAL,
            reference_price REAL,
            opening_reference_price REAL,
            ex_right_reference_price REAL,
            rights_value REAL,
            cash_value REAL,
            combined_value REAL,
            upper_limit REAL,
            lower_limit REAL,
            reduction_reason TEXT,
            detail_key TEXT,
            adjustment_factor REAL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            PRIMARY KEY (source_report, stock_id, event_date)
        );
        CREATE TABLE IF NOT EXISTS corporate_action_fetch_status (
            source_report TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            source_status TEXT NOT NULL,
            rows INTEGER NOT NULL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (source_report, period_start, period_end)
        );
        CREATE TABLE IF NOT EXISTS corporate_action_source_rows (
            source_report TEXT NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            stock_id TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            pre_event_close REAL,
            reference_price REAL,
            opening_reference_price REAL,
            ex_right_reference_price REAL,
            rights_value REAL,
            cash_value REAL,
            combined_value REAL,
            upper_limit REAL,
            lower_limit REAL,
            reduction_reason TEXT,
            detail_key TEXT,
            adjustment_factor REAL,
            raw_path TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            PRIMARY KEY (source_report, period_start, period_end, source_row_number)
        );
    """)
    connection.commit()
    return connection


def upsert_period(connection: sqlite3.Connection, report: str, start: date, end: date,
                  payload: dict, frame: pd.DataFrame, archive: Path) -> None:
    raw_sha = sha256_file(archive)
    if not frame.empty:
        normalized = frame.copy()
        normalized["raw_path"] = portable_path(archive)
        normalized["raw_sha256"] = raw_sha
        canonical_columns = list(normalized.columns)
        columns = canonical_columns
        placeholders = ",".join("?" for _ in columns)
        assignments = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"source_report", "stock_id", "event_date"}
        )
        connection.executemany(
            f"INSERT INTO corporate_action_events ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT(source_report, stock_id, event_date) "
            f"DO UPDATE SET {assignments}",
            normalized.itertuples(index=False, name=None),
        )
        source_rows = normalized.copy()
        source_rows.insert(1, "period_start", start.isoformat())
        source_rows.insert(2, "period_end", end.isoformat())
        source_rows.insert(3, "source_row_number", range(1, len(source_rows) + 1))
        source_columns = list(source_rows.columns)
        source_placeholders = ",".join("?" for _ in source_columns)
        connection.execute(
            "DELETE FROM corporate_action_source_rows "
            "WHERE source_report=? AND period_start=? AND period_end=?",
            (report, start.isoformat(), end.isoformat()),
        )
        connection.executemany(
            f"INSERT INTO corporate_action_source_rows ({','.join(source_columns)}) "
            f"VALUES ({source_placeholders})",
            source_rows.itertuples(index=False, name=None),
        )
    else:
        connection.execute(
            "DELETE FROM corporate_action_source_rows "
            "WHERE source_report=? AND period_start=? AND period_end=?",
            (report, start.isoformat(), end.isoformat()),
        )
    connection.execute(
        """INSERT INTO corporate_action_fetch_status
           (source_report, period_start, period_end, source_status, rows,
            raw_path, raw_sha256, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_report, period_start, period_end) DO UPDATE SET
             source_status=excluded.source_status, rows=excluded.rows,
             raw_path=excluded.raw_path, raw_sha256=excluded.raw_sha256,
             fetched_at=excluded.fetched_at""",
        (report, start.isoformat(), end.isoformat(), str(payload.get("stat", "UNKNOWN")),
         len(frame), portable_path(archive), raw_sha, raw_acquired_at(archive)),
    )
    connection.commit()


def month_ranges(start: date, end: date):
    current = date(start.year, start.month, 1)
    while current <= end:
        final = date(current.year, current.month, monthrange(current.year, current.month)[1])
        yield max(start, current), min(end, final)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)


def fetch_range(start: date, end: date, raw_root: str | Path,
                db_path: str | Path, delay: float = 1.5) -> dict:
    connection = connect(db_path)
    downloaded = cached = rows = 0
    try:
        completed = {
            (row[0], row[1], row[2])
            for row in connection.execute(
                """SELECT s.source_report, s.period_start, s.period_end
                   FROM corporate_action_fetch_status s
                   LEFT JOIN (
                     SELECT source_report, period_start, period_end, COUNT(*) AS rows
                     FROM corporate_action_source_rows
                     GROUP BY source_report, period_start, period_end
                   ) r ON r.source_report=s.source_report
                      AND r.period_start=s.period_start AND r.period_end=s.period_end
                   WHERE COALESCE(r.rows, 0)=s.rows"""
            )
        }
        jobs = [(REPORT_EX_RIGHT, period_start, period_end)
                for period_start, period_end in month_ranges(start, end)]
        reduction_start = max(start, date(2011, 1, 1))
        if reduction_start <= end:
            jobs += [(REPORT_REDUCTION, period_start, period_end)
                     for period_start, period_end in month_ranges(reduction_start, end)]
        for index, (report, period_start, period_end) in enumerate(jobs, start=1):
            key = (report, period_start.isoformat(), period_end.isoformat())
            archive = raw_path(raw_root, report, period_start)
            if key in completed and archive.exists():
                cached += 1
                continue
            if archive.exists():
                payload = read_raw_response(archive)
                cached += 1
            else:
                payload = _get_json(URL.format(report=report), params={
                    "response": "json",
                    "startDate": period_start.strftime("%Y%m%d"),
                    "endDate": period_end.strftime("%Y%m%d"),
                })
                write_raw_response(archive, payload)
                downloaded += 1
                if delay:
                    time.sleep(delay)
            frame = parse_twt49u(payload) if report == REPORT_EX_RIGHT else parse_twtauu(payload)
            upsert_period(connection, report, period_start, period_end, payload, frame, archive)
            rows += len(frame)
            if index % 12 == 0:
                print(f"progress report={report} period={period_start:%Y-%m} "
                      f"downloaded={downloaded} cached={cached} rows={rows}", flush=True)
    finally:
        connection.close()
    return {"start": start.isoformat(), "end": end.isoformat(), "downloaded": downloaded,
            "cached": cached, "normalized_rows_this_run": rows}


def summarize_database(db_path: str | Path) -> dict:
    connection = connect(db_path)
    try:
        status = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(rows),0), MIN(period_start), MAX(period_end) "
            "FROM corporate_action_fetch_status"
        ).fetchone()
        source_rows = connection.execute(
            "SELECT COUNT(*) FROM corporate_action_source_rows"
        ).fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id), MIN(event_date), MAX(event_date), "
            "SUM(pre_event_close <= 0), SUM(reference_price <= 0), "
            "SUM(adjustment_factor <= 0) FROM corporate_action_events"
        ).fetchone()
        by_report = dict(connection.execute(
            "SELECT source_report, COUNT(*) FROM corporate_action_events GROUP BY source_report"
        ).fetchall())
        by_kind = dict(connection.execute(
            "SELECT event_kind, COUNT(*) FROM corporate_action_events GROUP BY event_kind"
        ).fetchall())
    finally:
        connection.close()
    return {
        "db": str(Path(db_path).resolve()),
        "completed_month_reports": status[0],
        "status_declared_rows": status[1],
        "official_source_rows": source_rows,
        "canonical_event_rows": events[0],
        "stock_ids": events[1],
        "period_min": status[2],
        "period_max": status[3],
        "event_date_min": events[2],
        "event_date_max": events[3],
        "nonpositive_pre_close": events[4] or 0,
        "nonpositive_reference_price": events[5] or 0,
        "nonpositive_adjustment_factor": events[6] or 0,
        "rows_match": status[1] == source_rows,
        "duplicate_economic_alias_rows": source_rows - events[0],
        "by_report": by_report,
        "by_kind": by_kind,
    }


def export_snapshot(db_path: str | Path, output_root: str | Path, snapshot_id: str,
                    start: date, end: date,
                    jump_audit_path: str | Path | None = None,
                    sample_audit_path: str | Path | None = None) -> Path:
    output_root = Path(output_root).resolve()
    target = output_root / snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".{snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        connection = sqlite3.connect(db_path)
        try:
            events = pd.read_sql_query(
                "SELECT * FROM corporate_action_events WHERE event_date BETWEEN :start AND :end "
                "ORDER BY event_date, source_report, stock_id", connection,
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
            status = pd.read_sql_query(
                "SELECT * FROM corporate_action_fetch_status "
                "WHERE period_start >= :start AND period_end <= :end "
                "ORDER BY period_start, source_report", connection,
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
            source_rows = pd.read_sql_query(
                "SELECT * FROM corporate_action_source_rows "
                "WHERE event_date BETWEEN :start AND :end "
                "ORDER BY period_start, source_report, source_row_number", connection,
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
        finally:
            connection.close()
        # Older local databases may contain absolute paths.  Do not let a
        # workstation-specific prefix leak into an immutable snapshot.
        for frame in (events, status, source_rows):
            if "raw_path" in frame:
                frame["raw_path"] = frame["raw_path"].map(portable_path)
        events.to_parquet(temporary / "corporate_actions.parquet", index=False)
        source_rows.to_parquet(temporary / "corporate_action_source_rows.parquet", index=False)
        status.to_parquet(temporary / "source_status.parquet", index=False)
        manifest = build_manifest(temporary, ROOT)
        manifest["snapshot_id"] = snapshot_id
        manifest["snapshot_dir"] = portable_path(target)
        manifest["source"] = {
            "provider": "TWSE",
            "reports": [REPORT_EX_RIGHT, REPORT_REDUCTION],
            "requested_date_start": start.isoformat(),
            "requested_date_end": end.isoformat(),
            "raw_response_count": len(status),
            "raw_responses_sha256": canonical_json_sha256(
                status[["source_report", "period_start", "period_end", "raw_sha256"]]
                .to_dict("records")
            ),
        }
        duplicate_events = int(events.duplicated(
            ["source_report", "stock_id", "event_date"]
        ).sum())
        quality = build_quality_report(
            temporary, manifest,
            required_files={"corporate_actions.parquet", "corporate_action_source_rows.parquet",
                            "source_status.parquet"},
        )
        validation = {}
        for name, audit_path in (
            ("large_jump_audit", jump_audit_path),
            ("official_sample_audit", sample_audit_path),
        ):
            if audit_path is None:
                continue
            path = Path(audit_path)
            if not path.is_file():
                raise FileNotFoundError(path)
            validation[name] = {
                "path": portable_path(path),
                "sha256": sha256_file(path),
                "result": json.loads(path.read_text(encoding="utf-8")),
            }
        jump_passed = bool(
            validation.get("large_jump_audit", {}).get("result", {})
            .get("passed_no_unexplained_short_gap", False)
        )
        sample_passed = bool(
            validation.get("official_sample_audit", {}).get("result", {}).get("passed", False)
        )
        blockers = [
            "TWSE capital-reduction report begins 2011-01-01; 2005-2010 reduction history is absent",
            "merger and split-reduction completeness is not yet reconciled",
            "long observation gaps require an explicit stale-price/suspension eligibility rule",
            "10 official-vs-MI_INDEX prior-close differences require classification; 3 are common stocks",
        ]
        if not jump_passed:
            blockers.append("greater-than-20-percent short-gap jump reconciliation is pending")
        if not sample_passed:
            blockers.append("30-event official archived-row sample audit is pending")
        quality.update({
            "snapshot_id": snapshot_id,
            "scope": "D3_corporate_actions_staging_not_strategy_ready",
            "structural_passed": bool(
                quality.get("structural_passed") and duplicate_events == 0
                and events["pre_event_close"].gt(0).all()
                and events["reference_price"].gt(0).all()
                and events["adjustment_factor"].gt(0).all()
            ),
            "promotion_ready": False,
            "event_rows": len(events),
            "official_source_rows": len(source_rows),
            "official_rows_match_status": int(status["rows"].sum()) == len(source_rows),
            "duplicate_economic_alias_rows": len(source_rows) - len(events),
            "event_counts": events["event_kind"].value_counts().sort_index().to_dict(),
            "source_counts": events["source_report"].value_counts().sort_index().to_dict(),
            "duplicate_event_keys": duplicate_events,
            "validation": validation,
            "jump_reconciliation_passed": jump_passed,
            "official_sample_audit_passed": sample_passed,
            "blockers": blockers,
        })
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2005, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2014, 12, 31))
    parser.add_argument("--raw-root", default=str(ROOT / "data" / "raw" / "twse" / "corporate_actions"))
    parser.add_argument("--db", default=str(ROOT / "data" / "raw" / "twse" / "corporate_actions.sqlite3"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--output-root", default=str(ROOT / "data" / "research_versions"))
    parser.add_argument("--snapshot-id", default="twse_corporate_actions_2005_2014_staging_v1")
    parser.add_argument(
        "--jump-audit",
        type=Path,
        default=ROOT / "reports" / "twse_corporate_action_jump_audit_2005_2014.json",
    )
    parser.add_argument(
        "--sample-audit",
        type=Path,
        default=ROOT / "reports" / "twse_corporate_action_official_sample_2005_2014.json",
    )
    args = parser.parse_args()
    if args.status:
        print(json.dumps(summarize_database(args.db), ensure_ascii=False, indent=2))
        return
    if args.end < args.start:
        parser.error("--end must not be earlier than --start")
    result = fetch_range(args.start, args.end, args.raw_root, args.db, max(0.0, args.delay))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.export:
        print(export_snapshot(
            args.db, args.output_root, args.snapshot_id, args.start, args.end,
            args.jump_audit, args.sample_audit,
        ))


if __name__ == "__main__":
    main()
