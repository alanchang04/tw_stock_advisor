"""回補 TWSE 變更交易方法／全額交割證券歷史（2005~2014），落地為不可變 raw cache。

為什麼需要這支腳本
------------------
`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6 要求 MOM-1 的 universe 排除
「非處置／停止交易／全額交割等無法按模型成交的股票」。D6 已補齊 TWSE 處置
（`twse_disposition_punish_2005_2014_v1`），但**全額交割／變更交易方法**在
`reports/mom1_f0_execution_readiness.json` 仍列為 remaining blocker：
TPEX 有 D5 的 `trading_restrictions.parquet`，TWSE 沒有等價物。

全額交割股採預收款券、且多為財務狀況惡化者；把它們留在可交易池會讓回測用
一個實務上難以按模型成交的價格建立部位。

官方來源
--------
`https://www.twse.com.tw/exchangeReport/TWT85U`（OpenAPI 目錄登記為
「集中市場證券變更交易」）。2026-08-12 實測 `date` 參數可回溯至 2005-01-03，
逐交易日各一份快照。

**跨年代語意變遷（本腳本刻意保留而不合併）**：

- 2005-01-03 起報表名稱為「全額交割證券」，欄位僅 `證券代號`、`證券名稱`。
- 2007 年內改名為「變更交易」，並新增 `分盤集合競價(以**表示)` 欄位。

因此早期沒有分盤集合競價旗標。依 SPEC §2.2.5「缺資料不等於 0」，正規化時
該欄位在早期必須是 **unknown**，不得補 False——否則會把「當時不揭露」誤述成
「當時沒有分盤」。本腳本只負責凍結原始回應，判定交給 versioned builder。

安全性質
--------
- 只寫 `data/raw/twse/altered_trading/`，**不觸碰** `data/research/`
  （SPEC §0.2／§3.1 不覆蓋原則）。
- 已存在的 raw 檔預設不重抓，可離線重跑、可中斷續跑。
- 原子寫入；中斷不會留下半個檔案。
- 不建立 research snapshot，也不寫任何資料庫。

跨機可重現性
------------
JSON 使用固定 key 順序與緊縮 separators，gzip member 的 mtime 固定為 0。
相同官方 payload 在兩台機器會得到相同位元組。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = "https://www.twse.com.tw/exchangeReport/TWT85U"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 30
DEFAULT_CALENDAR = ROOT / "data/research_versions/twse_prices_2005_2014_v1/prices.parquet"


def raw_path(raw_root: Path, day: pd.Timestamp) -> Path:
    return raw_root / f"{day.year}" / f"altered_trading_{day:%Y%m%d}.json.gz"


def write_raw_response(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    raw = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        temporary.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_raw_response(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def trading_days(calendar: Path, start: str, end: str) -> list[pd.Timestamp]:
    """交易日曆取自已凍結的 D2 價格快照，不另外猜測開休市。"""
    frame = pd.read_parquet(calendar, columns=["trade_date"])
    days = pd.DatetimeIndex(sorted(pd.to_datetime(frame["trade_date"]).unique()))
    return list(days[(days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))])


def fetch_day(day: pd.Timestamp) -> dict:
    response = requests.get(
        URL,
        params={"response": "json", "date": f"{day:%Y%m%d}"},
        headers=HEADERS, timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{day:%Y-%m-%d}: official response is not a JSON object")
    if payload.get("stat") != "OK":
        raise ValueError(f"{day:%Y-%m-%d}: official stat={payload.get('stat')!r}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--end", default="2014-12-31")
    parser.add_argument("--calendar", default=str(DEFAULT_CALENDAR),
                        help="交易日曆來源（預設為已凍結的 D2 價格快照）")
    parser.add_argument("--raw-root", default=str(ROOT / "data/raw/twse/altered_trading"))
    parser.add_argument("--delay", type=float, default=2.0,
                        help="每次請求後的間隔秒數；官方端點無明文限流，保守預設 2 秒")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--force", action="store_true",
                        help="即使 raw 已存在也重抓；官方若有歷史更正，sha256 會改變")
    parser.add_argument("--status", action="store_true",
                        help="只讀本地 raw cache，回報覆蓋狀況，不發任何請求")
    parser.add_argument("--progress-every", type=int, default=100)
    args = parser.parse_args()

    raw_root = Path(args.raw_root)
    days = trading_days(Path(args.calendar), args.start, args.end)

    downloaded = cached = missing = rows = 0
    per_year_days: dict[int, int] = {}
    per_year_rows: dict[int, int] = {}
    failures: list[dict] = []

    for index, day in enumerate(days, start=1):
        path = raw_path(raw_root, day)
        payload = None
        if path.exists() and not args.force:
            payload = read_raw_response(path)
            cached += 1
        elif args.status:
            missing += 1
        else:
            for attempt in range(1, args.retries + 1):
                try:
                    payload = fetch_day(day)
                    break
                except Exception as error:                       # noqa: BLE001
                    if attempt == args.retries:
                        failures.append({"date": f"{day:%Y-%m-%d}",
                                         "error": f"{type(error).__name__}: {error}"})
                    else:
                        time.sleep(args.delay * attempt * 2)
            if payload is not None:
                write_raw_response(path, payload)
                downloaded += 1
            if args.delay:
                time.sleep(args.delay)

        if payload is not None:
            n = len(payload.get("data") or [])
            rows += n
            per_year_rows[day.year] = per_year_rows.get(day.year, 0) + n
            per_year_days[day.year] = per_year_days.get(day.year, 0) + 1

        if args.progress_every and index % args.progress_every == 0:
            print(f"[{index}/{len(days)}] {day:%Y-%m-%d} "
                  f"downloaded={downloaded} cached={cached} rows={rows} "
                  f"failed={len(failures)}", flush=True)

    print(json.dumps({
        "raw_root": str(raw_root),
        "period": [args.start, args.end],
        "trading_days": len(days),
        "downloaded": downloaded,
        "cached": cached,
        "missing": missing,
        "observation_rows": rows,
        "days_per_year": dict(sorted(per_year_days.items())),
        "rows_per_year": dict(sorted(per_year_rows.items())),
        "failures": failures,
        "note": "raw only；正規化與 promotion 由 versioned builder 負責",
    }, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
