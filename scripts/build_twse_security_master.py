"""Build a source-backed TWSE historical security-master staging area.

D1 deliberately separates official source acquisition from promotion.  A row
is not labelled ``common_stock`` merely because its code looks stock-like.
Official annual-report security-type tables, current company metadata and the
complete delisting list are archived first; unresolved rows remain unknown.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import gzip
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import uuid

import pandas as pd
from pypdf import PdfReader
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.snapshot_manifest import (  # noqa: E402
    build_manifest, canonical_json_sha256, sha256_file, write_json,
)


CURRENT_COMPANIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
DELISTED_URL = "https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml"
ANNUAL_REPORT_URLS = {
    roc_year: f"https://www.twse.com.tw/downloads/zh/about/company/annual_{roc_year}.pdf"
    for roc_year in range(94, 104)
}
ANNUAL_CHANGES_URLS = {
    95: "https://www.twse.com.tw/downloads/zh/about/company/Annual/95/P32.pdf",
    96: "https://www.twse.com.tw/downloads/zh/about/company/Annual/96/P32.pdf",
    97: "https://www.twse.com.tw/downloads/zh/about/company/Annual/97/P28.pdf",
    98: "https://www.twse.com.tw/downloads/zh/about/company/Annual/98/P32.pdf",
    99: "https://www.twse.com.tw/downloads/zh/about/company/Annual/99/P36.pdf",
    100: "https://www.twse.com.tw/downloads/zh/about/company/Annual/100/P38.pdf",
    101: "https://www.twse.com.tw/downloads/zh/about/company/Annual/101/P16.pdf",
    102: "https://www.twse.com.tw/downloads/zh/about/company/Annual/102/P4.pdf",
    103: "https://www.twse.com.tw/downloads/zh/about/company/Annual/103/P4.pdf",
}
HEADERS = {"User-Agent": "Mozilla/5.0 tw-stock-advisor research/1.0"}
TDR_OFFICIAL_OVERRIDES = {
    "9101": "https://www.twse.com.tw/downloads/zh/about/company/Annual/97/P28.pdf",
    "9102": "https://www.twse.com.tw/staticFiles/product/publication/twse60/html/444/",
    "9104": "https://www.twse.com.tw/staticFiles/product/publication/twse60/html/445/",
}


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _download(url: str, path: Path, timeout: int = 120) -> bool:
    """Download once; return True only when a new file was written."""
    if path.exists():
        return False
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    content = response.content
    if path.suffix.lower() == ".pdf" and not content.startswith(b"%PDF"):
        raise ValueError(f"official source did not return a PDF: {url}")
    _atomic_write(path, content)
    return True


def _download_json_gzip(url: str, path: Path, timeout: int = 120) -> bool:
    if path.exists():
        return False
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError(f"official JSON source is not a list: {url}")
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(temporary, "wb") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def extract_pdf_text(pdf_path: Path, text_path: Path) -> dict:
    if text_path.exists():
        joined = text_path.read_text(encoding="utf-8")
        sections = joined.split("--- PAGE ")[1:]
        page_bodies = [section.split("---\n", 1)[-1] for section in sections]
        return {
            "pages": len(sections),
            "pages_with_text": sum(bool(body.strip()) for body in page_bodies),
            "characters": len(joined),
            "failed_pages": [],
            "contains_listing_changes": "上市異動" in joined,
        }
    reader = PdfReader(str(pdf_path))
    page_text = []
    failed_pages = []
    for index, page in enumerate(reader.pages):
        try:
            page_text.append(page.extract_text() or "")
        except Exception as exc:  # preserve a visible quality failure per page
            failed_pages.append({"page": index + 1, "error": str(exc)})
            page_text.append("")
    joined = "\n\n".join(
        f"--- PAGE {index + 1} ---\n{text}" for index, text in enumerate(page_text)
    )
    _atomic_write(text_path, joined.encode("utf-8"))
    return {
        "pages": len(reader.pages),
        "pages_with_text": sum(bool(text.strip()) for text in page_text),
        "characters": len(joined),
        "failed_pages": failed_pages,
        "contains_listing_changes": "上市異動" in joined,
    }


def render_pdf_pages(pdf_path: Path, image_root: Path, scale: float = 2.0) -> list[str]:
    """Render source pages for visual verification when no text layer exists."""
    import pymupdf

    image_root.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(pdf_path)
    outputs = []
    for index, page in enumerate(document):
        target = image_root / f"{pdf_path.stem}_page_{index + 1}.png"
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        _atomic_write(target, pixmap.tobytes("png"))
        outputs.append(str(target))
    document.close()
    return outputs


def acquire_sources(raw_root: str | Path, as_of: date) -> dict:
    raw_root = Path(raw_root).resolve()
    pdf_root = raw_root / "official_pdf"
    text_root = raw_root / "derived_text"
    json_root = raw_root / "official_json"
    sources: list[dict] = []

    for roc_year, url in ANNUAL_REPORT_URLS.items():
        pdf_path = pdf_root / f"annual_{roc_year}.pdf"
        _download(url, pdf_path)
        text_path = text_root / f"annual_{roc_year}.txt"
        extraction = extract_pdf_text(pdf_path, text_path)
        sources.append({
            "kind": "twse_annual_report",
            "roc_year": roc_year,
            "calendar_year": roc_year + 1911,
            "url": url,
            "path": str(pdf_path.relative_to(raw_root)),
            "sha256": sha256_file(pdf_path),
            "bytes": pdf_path.stat().st_size,
            "derived_text_path": str(text_path.relative_to(raw_root)),
            "derived_text_sha256": sha256_file(text_path),
            "extraction": extraction,
        })

    for roc_year, url in ANNUAL_CHANGES_URLS.items():
        pdf_path = pdf_root / f"changes_{roc_year}.pdf"
        _download(url, pdf_path)
        text_path = text_root / f"changes_{roc_year}.txt"
        extraction = extract_pdf_text(pdf_path, text_path)
        rendered_pages = []
        if extraction["pages_with_text"] < extraction["pages"]:
            rendered_pages = render_pdf_pages(pdf_path, raw_root / "derived_images")
        sources.append({
            "kind": "twse_annual_listing_changes",
            "roc_year": roc_year,
            "calendar_year": roc_year + 1911,
            "url": url,
            "path": str(pdf_path.relative_to(raw_root)),
            "sha256": sha256_file(pdf_path),
            "bytes": pdf_path.stat().st_size,
            "derived_text_path": str(text_path.relative_to(raw_root)),
            "derived_text_sha256": sha256_file(text_path),
            "rendered_pages": [
                str(Path(path).relative_to(raw_root)) for path in rendered_pages
            ],
            "extraction": extraction,
        })

    for kind, url in (
        ("current_companies", CURRENT_COMPANIES_URL),
        ("delisted_companies", DELISTED_URL),
    ):
        path = json_root / f"{kind}_{as_of.isoformat()}.json.gz"
        _download_json_gzip(url, path)
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        sources.append({
            "kind": kind,
            "as_of": as_of.isoformat(),
            "url": url,
            "path": str(path.relative_to(raw_root)),
            "sha256": sha256_file(path),
            "canonical_content_sha256": canonical_json_sha256(payload),
            "bytes": path.stat().st_size,
            "rows": len(payload),
        })

    manifest = {
        "schema_version": 2,
        "stage": "D1_source_archive_not_promoted",
        "as_of": as_of.isoformat(),
        "path_contract": "all source paths are relative to raw_root",
        "parser_versions": {
            "pypdf": version("pypdf"),
            "pymupdf": version("pymupdf"),
        },
        "sources": sources,
    }
    write_json(raw_root / f"source_manifest_{as_of.isoformat()}.json", manifest)
    return manifest


def _parse_compact_date(value) -> date | None:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        except ValueError:
            return None
    return None


def _parse_roc_date(value) -> date | None:
    raw = str(value or "").strip()
    parts = raw.split("/")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _classify_asset(stock_id: str, current: dict | None, delisted: dict | None) -> tuple[str, str]:
    """Classify only from official dataset membership or explicit TWSE evidence."""
    if current is not None:
        short_name = str(current.get("公司簡稱", ""))
        if str(current.get("產業別", "")).strip() == "91" or "-DR" in short_name:
            return "depositary_receipt", "TWSE current company metadata: industry=91 or -DR"
        return "common_stock", "TWSE listed-company basic-data membership"
    if delisted is not None:
        company = str(delisted.get("Company", ""))
        if "-DR" in company or stock_id in TDR_OFFICIAL_OVERRIDES:
            source = TDR_OFFICIAL_OVERRIDES.get(stock_id, DELISTED_URL)
            return "depositary_receipt", f"TWSE TDR evidence: {source}"
        return "common_stock", "TWSE terminated-listed-company membership"
    return "non_company_security", "absent from both official company issuer datasets"


def _load_json_gzip(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def build_staging_snapshot(
    db_path: str | Path,
    raw_root: str | Path,
    output_root: str | Path,
    snapshot_id: str,
    as_of: date,
    stage_start: date = date(2005, 1, 1),
    stage_end: date = date(2007, 12, 31),
) -> Path:
    raw_root = Path(raw_root).resolve()
    output_root = Path(output_root).resolve()
    target = output_root / snapshot_id
    if target.exists():
        raise FileExistsError(f"snapshot already exists and will not be overwritten: {target}")

    current_path = raw_root / "official_json" / f"current_companies_{as_of}.json.gz"
    delisted_path = raw_root / "official_json" / f"delisted_companies_{as_of}.json.gz"
    source_manifest_path = raw_root / f"source_manifest_{as_of}.json"
    for path in (current_path, delisted_path, source_manifest_path):
        if not path.exists():
            raise FileNotFoundError(path)

    current_rows = _load_json_gzip(current_path)
    delisted_rows = _load_json_gzip(delisted_path)
    current_map = {str(row.get("公司代號", "")).strip(): row for row in current_rows}
    delisted_map = {str(row.get("Code", "")).strip(): row for row in delisted_rows}

    connection = sqlite3.connect(db_path)
    try:
        observations = pd.read_sql_query(
            """SELECT stock_id, MIN(trade_date) AS first_trade_date,
                      MAX(trade_date) AS last_trade_date, COUNT(*) AS observed_days
               FROM security_observations
               WHERE trade_date BETWEEN :start AND :end
               GROUP BY stock_id ORDER BY stock_id""",
            connection,
            params={"start": stage_start.isoformat(), "end": stage_end.isoformat()},
        )
        names = pd.read_sql_query(
            """SELECT o.stock_id, o.stock_name
               FROM security_observations o
               JOIN (SELECT stock_id, MAX(trade_date) AS trade_date
                     FROM security_observations
                     WHERE trade_date BETWEEN :start AND :end
                     GROUP BY stock_id) latest
                 ON latest.stock_id=o.stock_id AND latest.trade_date=o.trade_date
               WHERE o.trade_date BETWEEN :start AND :end""",
            connection,
            params={"start": stage_start.isoformat(), "end": stage_end.isoformat()},
        ).drop_duplicates("stock_id")
        month_ends = pd.read_sql_query(
            """SELECT MAX(trade_date) AS snapshot_date
               FROM daily_prices
               WHERE trade_date BETWEEN :start AND :end
               GROUP BY substr(trade_date, 1, 7)
               ORDER BY snapshot_date""",
            connection,
            params={"start": stage_start.isoformat(), "end": stage_end.isoformat()},
        )
    finally:
        connection.close()

    observations = observations.merge(names, on="stock_id", how="left")
    records = []
    for row in observations.to_dict("records"):
        sid = str(row["stock_id"])
        current = current_map.get(sid)
        delisted = delisted_map.get(sid)
        asset_type, classification_source = _classify_asset(sid, current, delisted)
        official_listing = _parse_compact_date(current.get("上市日期")) if current else None
        official_delisting = _parse_roc_date(delisted.get("DelistingDate")) if delisted else None
        first_trade = date.fromisoformat(row["first_trade_date"])
        effective_from = official_listing or first_trade
        records.append({
            "stock_id": sid,
            "stock_name": row["stock_name"],
            "market": "TWSE",
            "asset_type": asset_type,
            "listing_date": official_listing,
            "listing_date_is_exact": official_listing is not None,
            "effective_from": effective_from,
            "effective_from_source": (
                "TWSE current company listing date" if official_listing
                else "first observed TWSE official daily quote"
            ),
            "delisting_date": official_delisting,
            "first_trade_date": first_trade,
            "last_trade_date": date.fromisoformat(row["last_trade_date"]),
            "observed_days": int(row["observed_days"]),
            "industry_code_current": (
                str(current.get("產業別", "")).strip() or None if current else None
            ),
            "industry_code_asof": None,
            "industry_is_point_in_time": False,
            "classification_source": classification_source,
        })
    master = pd.DataFrame(records)

    common = master[master["asset_type"].eq("common_stock")].copy()
    history_frames = []
    for value in month_ends["snapshot_date"]:
        snapshot_date = date.fromisoformat(value)
        eligible = common[
            (common["effective_from"] <= snapshot_date)
            & (common["delisting_date"].isna() | (common["delisting_date"] > snapshot_date))
        ].copy()
        history_frames.append(pd.DataFrame({
            "snapshot_date": snapshot_date,
            "stock_id": eligible["stock_id"],
            "market": "TWSE",
            "industry_code": None,
            "asset_type": "common_stock",
            "listing_date": eligible["listing_date"],
            "delisting_date": eligible["delisting_date"],
            "is_active": True,
        }))
    history = pd.concat(history_frames, ignore_index=True) if history_frames else pd.DataFrame()
    delisted_frame = master[master["delisting_date"].notna()][
        ["stock_id", "stock_name", "delisting_date", "market"]
    ].copy()
    stocks = master[
        ["stock_id", "stock_name", "market", "asset_type", "listing_date"]
    ].copy()
    stocks["is_active"] = master["delisting_date"].isna()

    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / f".{snapshot_id}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        master.to_parquet(temporary / "security_master_staging.parquet", index=False)
        stocks.to_parquet(temporary / "stocks.parquet", index=False)
        delisted_frame.to_parquet(temporary / "delisted_stocks.parquet", index=False)
        history.to_parquet(temporary / "stock_universe_history.parquet", index=False)
        manifest = build_manifest(temporary, ROOT)
        manifest["snapshot_id"] = snapshot_id
        manifest["snapshot_dir"] = str(target)
        manifest["source"] = {
            "source_manifest": str(source_manifest_path),
            "source_manifest_sha256": sha256_file(source_manifest_path),
            "current_companies_sha256": sha256_file(current_path),
            "delisted_companies_sha256": sha256_file(delisted_path),
            "observations_db": str(Path(db_path).resolve()),
            "observation_date_min": stage_start.isoformat(),
            "observation_date_max": stage_end.isoformat(),
        }
        asset_counts = master["asset_type"].value_counts().sort_index().to_dict()
        exact_common = int(common["listing_date_is_exact"].sum())
        blockers = [
            "historical industry_code_asof is unavailable; sector cap cannot be validated",
            "corporate-action-adjusted prices are not part of this D1 snapshot",
        ]
        if stage_end < date(2014, 12, 31):
            blockers.insert(
                1,
                f"source observations currently end at {stage_end}; 2008-2014 holdout is incomplete",
            )
        quality = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "snapshot_id": snapshot_id,
            "structural_passed": bool(
                master["stock_id"].is_unique
                and not history.duplicated(["snapshot_date", "stock_id"]).any()
            ),
            "promotion_ready": False,
            "scope": (
                f"D1_{stage_start.year}_{stage_end.year}_staging_not_strategy_ready"
            ),
            "asset_counts": asset_counts,
            "observed_security_count": len(master),
            "classified_from_official_issuer_sources": int(
                master["asset_type"].isin(["common_stock", "depositary_receipt"]).sum()
            ),
            "common_stock_count": len(common),
            "common_exact_listing_date_count": exact_common,
            "common_effective_from_coverage": (
                float(common["effective_from"].notna().mean()) if len(common) else 0.0
            ),
            "monthly_snapshot_count": int(history["snapshot_date"].nunique()),
            "industry_pit_coverage": 0.0,
            "blockers": blockers,
            "classification_contract_sha256": canonical_json_sha256({
                "current_company_membership": "common_stock_except_industry_91_or_-DR",
                "delisted_company_membership": "common_stock_except_explicit_TDR_evidence",
                "non_issuer_membership": "non_company_security_excluded",
                "tdr_overrides": TDR_OFFICIAL_OVERRIDES,
            }),
        }
        write_json(temporary / "manifest.json", manifest)
        write_json(temporary / "quality_report.json", quality)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw-root",
        default=str(ROOT / "data" / "raw" / "twse" / "security_master"),
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--build-staging", action="store_true")
    parser.add_argument(
        "--db", default=str(ROOT / "data" / "raw" / "twse" / "mi_index.sqlite3")
    )
    parser.add_argument(
        "--output-root", default=str(ROOT / "data" / "research_versions")
    )
    parser.add_argument("--snapshot-id", default="twse_security_master_2005_2007_staging_v1")
    parser.add_argument("--stage-start", type=date.fromisoformat, default=date(2005, 1, 1))
    parser.add_argument("--stage-end", type=date.fromisoformat, default=date(2007, 12, 31))
    args = parser.parse_args()
    manifest = acquire_sources(args.raw_root, args.as_of)
    print(json.dumps({
        "manifest": str(Path(args.raw_root).resolve() / f"source_manifest_{args.as_of}.json"),
        "sources": len(manifest["sources"]),
        "annual_reports": sum(s["kind"] == "twse_annual_report" for s in manifest["sources"]),
        "annual_change_documents": sum(
            s["kind"] == "twse_annual_listing_changes" for s in manifest["sources"]
        ),
        "json_rows": {s["kind"]: s.get("rows") for s in manifest["sources"] if "rows" in s},
    }, ensure_ascii=False, indent=2))
    if args.build_staging:
        if args.stage_end < args.stage_start:
            parser.error("--stage-end must not be earlier than --stage-start")
        print(build_staging_snapshot(
            args.db, args.raw_root, args.output_root, args.snapshot_id, args.as_of,
            args.stage_start, args.stage_end,
        ))


if __name__ == "__main__":
    main()
