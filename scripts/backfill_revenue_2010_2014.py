"""回補 2010-01 ~ 2014-12 的月營收（**只碰營收，不碰價量法人**）。

為什麼要單獨寫一支
------------------
`scripts/historical_backfill_local.py --start-year 2010` 會把**價量、法人、
除權息、營收全部**從 2010 重抓——那是數千個請求、數小時的工作，
而且我們只缺營收（見 `research/EVIDENCE_MATRIX.md` §1）。

本腳本只跑營收那一段：**每月 2 個請求**（上市 sii ＋ 上櫃 otc），
2010-01 ~ 2014-12 共 60 個月 ≈ 120 個請求。

**這是資料工程，不是研究**
--------------------------
**下載本身不會污染 2010–2014。** 真正會消耗它的是把營收與**未來報酬**
接起來看結果。因此本腳本：

- 寫進本機 SQLite `data/research/research.db`（**不碰 Neon**）
- 匯出到**獨立檔案** `monthly_revenue_2010_2014_sealed.parquet`，
  **不覆蓋**現行的 `monthly_revenue.parquet`
- 產出一份 `_SEALED.json` 描述檔，狀態明寫
  **`DATA AVAILABLE / OUTCOME LINK NOT OPENED`**

**在取得 persistence 論文的精確定義、寫進規格、凍結程式碼之前，
不得把這批資料與未來報酬 join。**

安全性（執行前已驗證）
----------------------
- `research.db` 與現行 parquet 完全同步（各 4,840,274 列、日期一致），
  因此不存在「重新匯出會掉資料」的風險
- `upsert_df` 以 PK upsert，重跑安全、不重複
- 進度寫入 `backfill_progress`，可中斷續跑
- 單月探測已確認 2010-01／2012-06／2014-12 皆可取得
"""
from __future__ import annotations

import argparse
from calendar import monthrange
from datetime import date
import json
from pathlib import Path
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_pipeline.fetchers.revenue_fetcher import fetch_month_rows  # noqa: E402
from data_pipeline.local_research_db import (  # noqa: E402
    ensure_local_tables, get_local_conn, set_progress, upsert_df,
)

MIN_COMPLETENESS_RATIO = 0.75      # 低於中位數這個比例即視為部分回傳
START = (2010, 1)
END = (2014, 12)
TASK = "revenue_2010_2014_sealed"
SEALED_PARQUET = ROOT / "data/research/monthly_revenue_2010_2014_sealed.parquet"
SEALED_DESCRIPTOR = ROOT / "reports/revenue_2010_2014_sealed.json"


def months(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month > 12:
            year, month = year + 1, 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.6,
                        help="每個公開端點請求間隔秒數")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出要抓的月份，不發任何請求")
    args = parser.parse_args()

    planned = list(months(START, END))
    print(f"計畫回補 {len(planned)} 個月："
          f"{planned[0][0]}-{planned[0][1]:02d} ~ {planned[-1][0]}-{planned[-1][1]:02d}")
    print(f"預估請求數 {len(planned) * 2}（每月上市＋上櫃各一）")
    if args.dry_run:
        print("dry-run，未發出任何請求")
        return

    conn = get_local_conn()
    ensure_local_tables(conn)

    collected, failures = [], []
    for index, (year, month) in enumerate(planned, start=1):
        try:
            rows = fetch_month_rows(year, month)
        except Exception as exc:                      # noqa: BLE001
            failures.append({"year": year, "month": month,
                             "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {year}-{month:02d}  失敗：{type(exc).__name__}", flush=True)
            time.sleep(args.delay)
            continue
        if rows:
            frame = pd.DataFrame([{"stock_id": r["sid"], "year_month": r["ym"],
                                   "revenue": r["rev"], "mom_pct": r["mom"],
                                   "yoy_pct": r["yoy"]} for r in rows])
            upsert_df(conn, "monthly_revenue", frame)
            collected.append(frame)
        set_progress(conn, TASK, date(year, month, monthrange(year, month)[1]))
        if index % 12 == 0 or index == len(planned):
            print(f"  {year}-{month:02d}  已完成 {index}/{len(planned)} 個月",
                  flush=True)
        time.sleep(args.delay)

    if not collected:
        raise SystemExit("沒有取得任何資料——不寫出任何檔案")

    sealed = pd.concat(collected, ignore_index=True)
    sealed = sealed.drop_duplicates(["stock_id", "year_month"])

    # ── 完整性守門：抓「沒有拋例外但只回傳一半」 ────────────────────
    # **實際發生過**：首次執行時 2014-01 與 2014-03 只回傳 633／634 筆
    # （鄰月約 1,450），而且沒有任何例外。重抓即完整。
    # 只靠 try/except 抓不到這種靜默的部分回傳。
    counts = sealed.groupby("year_month").size()
    median_count = float(counts.median())
    short = counts[counts < median_count * MIN_COMPLETENESS_RATIO]
    retried = []
    for year_month in short.index:
        year, month = int(year_month[:4]), int(year_month[5:7])
        print(f"  ⚠ {year_month} 只有 {int(counts[year_month])} 筆"
              f"（中位 {median_count:.0f}）——重抓", flush=True)
        time.sleep(args.delay)
        try:
            rows = fetch_month_rows(year, month)
        except Exception as exc:                      # noqa: BLE001
            failures.append({"year": year, "month": month,
                             "error": f"retry failed: {type(exc).__name__}: {exc}"})
            continue
        if len(rows) > int(counts[year_month]):
            frame = pd.DataFrame([{"stock_id": r["sid"], "year_month": r["ym"],
                                   "revenue": r["rev"], "mom_pct": r["mom"],
                                   "yoy_pct": r["yoy"]} for r in rows])
            upsert_df(conn, "monthly_revenue", frame)
            sealed = (pd.concat([sealed[sealed["year_month"] != year_month], frame],
                                ignore_index=True)
                      .drop_duplicates(["stock_id", "year_month"]))
            retried.append({"year_month": year_month,
                            "before": int(counts[year_month]), "after": len(rows)})
            print(f"    → 重抓得到 {len(rows)} 筆", flush=True)

    counts = sealed.groupby("year_month").size()
    still_short = counts[counts < counts.median() * MIN_COMPLETENESS_RATIO]
    SEALED_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    sealed.to_parquet(SEALED_PARQUET, index=False)

    per_month = sealed.groupby("year_month").size()
    descriptor = {
        "completeness_guard": {
            "rule": (f"a month with fewer than {MIN_COMPLETENESS_RATIO:.0%} of the "
                     "median row count is re-fetched once"),
            "why": ("the fetcher can return PARTIAL data without raising. On the "
                    "first run 2014-01 and 2014-03 came back with 633/634 rows "
                    "against a ~1,450 neighbour median, with no exception. "
                    "try/except alone does not catch silent partial returns."),
            "retried": retried,
            "still_short_after_retry": {str(k): int(v) for k, v
                                        in still_short.items()},
        },
        "dataset": "monthly_revenue_2010_2014_sealed",
        "status": "DATA AVAILABLE / OUTCOME LINK NOT OPENED",
        "meaning": ("downloading and validating this data does NOT contaminate "
                    "2010-2014. What would consume it is joining revenue to FUTURE "
                    "RETURNS. Do not do that until the persistence definition is "
                    "written into a spec and the code is frozen."),
        "window": [f"{START[0]}-{START[1]:02d}", f"{END[0]}-{END[1]:02d}"],
        "months_planned": len(planned),
        "months_returned": int(per_month.size),
        "rows": int(len(sealed)),
        "securities": int(sealed["stock_id"].nunique()),
        "rows_per_month": {str(k): int(v) for k, v in per_month.items()},
        "failures": failures,
        "written_to": str(SEALED_PARQUET.relative_to(ROOT)),
        "does_not_overwrite": "data/research/monthly_revenue.parquet (2015-2026)",
        "source": "MOPS 彙總頁（data_pipeline/fetchers/revenue_fetcher.py），sii + otc",
    }
    SEALED_DESCRIPTOR.parent.mkdir(parents=True, exist_ok=True)
    SEALED_DESCRIPTOR.write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n取得 {len(sealed):,} 列、{sealed['stock_id'].nunique()} 檔、"
          f"{per_month.size} 個月")
    if failures:
        print(f"**失敗 {len(failures)} 個月**：{[(f['year'], f['month']) for f in failures]}")
    print(f"written: {SEALED_PARQUET}")
    print(f"written: {SEALED_DESCRIPTOR}")
    print("\n狀態：DATA AVAILABLE / OUTCOME LINK NOT OPENED")
    print("**不得在取得 persistence 定義並凍結規格之前，把它與未來報酬 join。**")
    conn.close()


if __name__ == "__main__":
    main()
