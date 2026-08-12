"""回補 TWSE 處置／注意事件歷史（2005 起），落地為不可變 raw cache。

為什麼需要這支腳本
------------------
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6 要求 MOM-1 的 universe 排除
「非處置／停止交易／全額交割等無法按模型成交的股票」，但目前
`data/research/disposition_events.parquet` 起始於 2014-12-23，2015 年以前只有 3 筆。
換言之 2008~2014 backward holdout 期間，該條 universe 規則形同失效。

處置常由「爆量急漲」觸發（見 `data_pipeline/fetchers/disposition_fetcher.py`），
正是動能策略最愛選的形態，因此漏排除的偏誤方向對 MOM-1 特別不利。

2026-08-12 實測 TWSE punish 端點確實回得出 2015 年以前的資料
（2008:27、2011:71、2013:81、2014:60 筆），`disposition_fetcher.py` 註解
「實測可回溯至 2015」低估了可得範圍。

安全性質
--------
- 只寫 `data/raw/twse/disposition/`，**不觸碰** `data/research/`
  （SPEC §0.2／§3.1 不覆蓋原則）。
- 已存在的 raw 檔預設不重抓，可離線重跑。
- 原子寫入；中斷不會留下半個檔案。
- 不建立 research snapshot，也不寫任何資料庫。正規化與 promotion 交給既有的
  versioned builder，本腳本只負責把官方原始回應凍結下來。

⚠️ 跨機注意：gzip header 帶 mtime，兩台機器各自回補會產生不同位元組、
sha256 對不上。**只能在一台機器執行**，另一台用
`scripts/build_data_transfer_manifest.py` 走檔案傳輸驗證。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = {
    "punish": "https://www.twse.com.tw/rwd/zh/announcement/punish",
    "notice": "https://www.twse.com.tw/rwd/zh/announcement/notice",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 30


def raw_path(raw_root: Path, report: str, year: int) -> Path:
    return raw_root / report / str(year) / f"{report}_{year}.json.gz"


def write_raw_response(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_raw_response(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_year(report: str, year: int) -> dict:
    response = requests.get(
        URL[report],
        params={"response": "json",
                "startDate": date(year, 1, 1).strftime("%Y%m%d"),
                "endDate": date(year, 12, 31).strftime("%Y%m%d")},
        headers=HEADERS, timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{report} {year}: official response is not a JSON object")
    return payload


def row_count(payload: dict) -> int:
    return len(payload.get("data") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2005)
    parser.add_argument("--end-year", type=int, default=2014)
    parser.add_argument("--reports", nargs="+", default=["punish", "notice"],
                        choices=["punish", "notice"])
    parser.add_argument("--raw-root", default=str(ROOT / "data/raw/twse/disposition"))
    parser.add_argument("--delay", type=float, default=2.0,
                        help="每次請求後的間隔秒數；官方端點無明文限流，保守預設 2 秒")
    parser.add_argument("--force", action="store_true",
                        help="即使 raw 已存在也重抓（會改變 sha256，跨機請勿使用）")
    parser.add_argument("--status", action="store_true",
                        help="只讀本地 raw cache，回報覆蓋狀況，不發任何請求")
    args = parser.parse_args()

    raw_root = Path(args.raw_root)
    years = range(args.start_year, args.end_year + 1)
    summary: dict[str, dict] = {}

    for report in args.reports:
        downloaded = cached = rows = missing = 0
        per_year: dict[int, int | None] = {}
        for year in years:
            path = raw_path(raw_root, report, year)
            if path.exists():
                payload = read_raw_response(path)
                cached += 1
            elif args.status:
                per_year[year] = None
                missing += 1
                continue
            else:
                payload = fetch_year(report, year)
                write_raw_response(path, payload)
                downloaded += 1
                if args.delay:
                    time.sleep(args.delay)
            n = row_count(payload)
            per_year[year] = n
            rows += n
        summary[report] = {"downloaded": downloaded, "cached": cached,
                           "missing": missing, "rows": rows, "per_year": per_year}

    print(json.dumps({
        "raw_root": str(raw_root),
        "years": [args.start_year, args.end_year],
        "reports": summary,
        "note": "raw only；正規化與 promotion 由 versioned builder 負責",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
