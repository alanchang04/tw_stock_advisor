"""
scripts/historical_backfill_local.py

SPEC_QUANT_UPGRADE.md P0：10年+官方歷史回補，寫本機 SQLite（不碰 Neon）。

**為什麼不寫 Neon**：2026-07-17 實測過，10年規模的逐日回補 + parquet 匯出，
單次測試就把 Neon 免費專案的資料傳輸配額燒穿，連正式每日 pipeline 都連不上
（詳見 docs/SPEC_QUANT_UPGRADE.md §2.7 事故記錄）。且事後測試發現網路抓取本身
很快（單日全部抓取約5~6秒），真正拖慢到「單日3~4分鐘」的是 Neon serverless
連線的 cold-start 開銷——改寫本機 SQLite 後兩個問題（配額+速度）應該一併解決。

架構：沿用 `twse_fetcher.py`/`corporate_actions_fetcher.py` 裡「只抓取、回傳
DataFrame、不碰 DB」的既有函式（`fetch_prices_twse_by_date` 等），只把寫入端從
`get_session()`(Neon) 換成 `data_pipeline/local_research_db.py`(本機 SQLite)。
不需要先登記「已知股票清單」——本機表沒有外鍵約束，任何 4 位數股票代號都收，
下市股歷史資料不會被濾掉（這正是本來要修的倖存者偏誤問題）。

可安全中斷、重跑自動接續（`backfill_progress` 表記錄各任務進度）。

用法：
    python scripts/historical_backfill_local.py --start-year 2015
    python scripts/historical_backfill_local.py --start-year 2015 --export-only  # 只匯出parquet
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

logger.add("logs/historical_backfill_local_{time:YYYY-MM-DD}.log",
          rotation="1 day", retention="14 days", level="INFO")

from data_pipeline.fetchers.twse_fetcher import (
    fetch_prices_twse_by_date, fetch_prices_tpex_by_date,
    fetch_institutional_twse, fetch_institutional_tpex_by_date,
    _is_stock_code,
)
from data_pipeline.fetchers.corporate_actions_fetcher import (
    fetch_dividend_events_twse, fetch_dividend_events_tpex, URL_TWSE_DELISTED, _parse_roc_slash, _UA,
)
from data_pipeline.fetchers.revenue_fetcher import fetch_month_rows, latest_published_month
from data_pipeline.local_research_db import (
    get_local_conn, ensure_local_tables, upsert_df, get_progress, set_progress, export_to_parquet,
)


def _filter_valid(df, id_col="stock_id"):
    if df.empty:
        return df
    return df[df[id_col].apply(_is_stock_code)].reset_index(drop=True)


def backfill_prices_and_institutional(conn, start_year: int, delay: float = 0.8,
                                      date_workers: int = 3):
    # 用 start_year 當 task key 的一部分：避免不同起始年份的回補互相誤判「已完成」
    # （例如先小範圍測試 --start-year 2026，之後正式跑 --start-year 2015，
    # 若共用同一個進度標記，2015 那次會誤以為已經接續到2026、直接跳過整段歷史）。
    task = f"prices_institutional_from_{start_year}"
    resume = get_progress(conn, task)
    d = resume + timedelta(days=1) if resume else date(start_year, 1, 1)
    end = date.today()
    logger.info(f"=== 價量+法人回補：{d} ~ {end}（{'接續上次進度' if resume else '從頭開始'}）===")

    total_days, total_rows = 0, 0
    # 一個日期有四個互不相依的官方端點。小批次並行下載，但仍按日期
    # 依序寫入，避免續傳標記跳過尚未完成的日期。
    with ThreadPoolExecutor(max_workers=max(1, date_workers) * 4) as pool:
      while d <= end:
        batch = []
        while d <= end and len(batch) < max(1, date_workers):
            if d.weekday() < 5:
                batch.append(d)
            d += timedelta(days=1)
        jobs = {
            trade_day: {
                "px_tw": pool.submit(fetch_prices_twse_by_date, trade_day),
                "px_tp": pool.submit(fetch_prices_tpex_by_date, trade_day),
                "it_tw": pool.submit(fetch_institutional_twse, trade_day),
                "it_tp": pool.submit(fetch_institutional_tpex_by_date, trade_day),
            }
            for trade_day in batch
        }
        for trade_day in batch:
            results = {}
            for name, job in jobs[trade_day].items():
                try:
                    results[name] = _filter_valid(job.result())
                except Exception as e:
                    logger.warning(f"  {trade_day} {name} 抓取失敗：{e}")
                    results[name] = pd.DataFrame()
            tw_price_empty = results["px_tw"].empty
            tpex_price_empty = results["px_tp"].empty
            has_institutional = not (
                results["it_tw"].empty and results["it_tp"].empty
            )
            if tw_price_empty != tpex_price_empty or (
                tw_price_empty and tpex_price_empty and has_institutional
            ):
                # 交易日只取得一個市場時，保留已落庫資料但把游標退回本日；
                # 下一批會以 upsert 補齊，不會把半套資料誤認成完成。
                logger.error(f"  {trade_day} 價格資料不完整，暫停推進並重試")
                d = trade_day
                break
            px_parts = [results[k] for k in ("px_tw", "px_tp") if not results[k].empty]
            px = pd.concat(px_parts, ignore_index=True) if px_parts else pd.DataFrame()
            if px.empty:
                set_progress(conn, task, trade_day)
                continue
            it_parts = [results[k] for k in ("it_tw", "it_tp") if not results[k].empty]
            it = (pd.concat(it_parts, ignore_index=True) if it_parts else
                  pd.DataFrame(columns=["stock_id", "trade_date", "total_net",
                                        "foreign_net", "invest_net"]))
            n1 = upsert_df(conn, "daily_prices",
                           px[["stock_id", "trade_date", "open", "high", "low", "close",
                              "volume", "turnover", "change_pct"]], date_cols=("trade_date",))
            n2 = upsert_df(conn, "institutional_trading",
                           it[["stock_id", "trade_date", "total_net", "foreign_net", "invest_net"]],
                           date_cols=("trade_date",))
            set_progress(conn, task, trade_day)
            total_days += 1
            total_rows += n1 + n2
            if total_days % 20 == 0:
                logger.info(f"  進度：{trade_day}（已處理 {total_days} 個交易日，累計 {total_rows} 列）")
        time.sleep(delay)

    logger.info(f"=== 價量+法人回補完成：{total_days} 個交易日，{total_rows} 列 ===")


def backfill_dividends_local(conn, start_year: int, delay: float = 0.8):
    from calendar import monthrange
    task = f"dividends_from_{start_year}"
    resume = get_progress(conn, task)
    y, m = (resume.year, resume.month + 1) if resume else (start_year, 1)
    if m > 12:
        m, y = 1, y + 1
    end = date.today()
    logger.info(f"=== 除權息回補：{y}-{m:02d} ~ {end}（{'接續上次進度' if resume else '從頭開始'}）===")

    total = 0
    while date(y, m, 1) <= end:
        last_day = monthrange(y, m)[1]
        chunk_end = min(date(y, m, last_day), end)
        chunk_start = date(y, m, 1)
        try:
            tw = fetch_dividend_events_twse(chunk_start, chunk_end)
            time.sleep(delay)
            tp = fetch_dividend_events_tpex(chunk_start, chunk_end)
            time.sleep(delay)
        except Exception as e:
            logger.warning(f"  除權息 {chunk_start}~{chunk_end} 抓取失敗：{e}")
            tw = tp = pd.DataFrame()
        parts = [d for d in (tw, tp) if not d.empty]   # 避免全空DataFrame觸發pandas concat警告
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        if not df.empty:
            n = upsert_df(conn, "dividend_events", df[["stock_id", "ex_date", "pre_close", "ref_price"]],
                         date_cols=("ex_date",))
            total += n
        set_progress(conn, task, chunk_end)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    logger.info(f"=== 除權息回補完成：累計 {total} 筆 ===")


def backfill_revenue_local(conn, start_year: int, delay: float = 0.5):
    """月營收回補（P1因子研究缺口：w_rev_yoy/w_rev_accel 因子驗證需要）。
    來源同 revenue_fetcher.py（MOPS彙總頁），只換寫入端成本機SQLite——一個月2個
    請求（上市+上櫃），10年也才~240個請求，比逐日的價量回補快得多（分鐘級不是小時級）。"""
    task = f"revenue_from_{start_year}"
    resume = get_progress(conn, task)
    y, m = (resume.year, resume.month + 1) if resume else (start_year, 1)
    if m > 12:
        m, y = 1, y + 1
    end_y, end_m = latest_published_month()
    logger.info(f"=== 月營收回補：{y}-{m:02d} ~ {end_y}-{end_m:02d}"
               f"（{'接續上次進度' if resume else '從頭開始'}）===")

    total = 0
    while (y, m) <= (end_y, end_m):
        rows = fetch_month_rows(y, m)
        if rows:
            df = pd.DataFrame([{"stock_id": r["sid"], "year_month": r["ym"],
                               "revenue": r["rev"], "mom_pct": r["mom"], "yoy_pct": r["yoy"]}
                              for r in rows])
            n = upsert_df(conn, "monthly_revenue", df)
            total += n
        # 用「當月最後一天」當進度標記，跟 dividends 回補用同一個慣例（get_progress回傳date）
        from calendar import monthrange
        set_progress(conn, task, date(y, m, monthrange(y, m)[1]))
        m += 1
        if m > 12:
            m, y = 1, y + 1
        time.sleep(delay)
    logger.info(f"=== 月營收回補完成：累計 {total} 筆 ===")


def backfill_delisted_local(conn):
    import requests
    try:
        r = requests.get(URL_TWSE_DELISTED, headers=_UA, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error(f"下市清單抓取失敗: {e}")
        return
    rows = []
    for d in data:
        sid = str(d.get("Code", "")).strip()
        if _is_stock_code(sid):
            rows.append({"stock_id": sid, "stock_name": (d.get("Company") or "").strip(),
                        "delisting_date": str(_parse_roc_slash(d.get("DelistingDate")) or ""),
                        "market": "TWSE"})
    if rows:
        conn.executemany(
            "INSERT INTO delisted_stocks (stock_id, stock_name, delisting_date, market) VALUES (?,?,?,?) "
            "ON CONFLICT(stock_id) DO UPDATE SET delisting_date=excluded.delisting_date",
            [(r["stock_id"], r["stock_name"], r["delisting_date"], r["market"]) for r in rows])
        conn.commit()
    logger.info(f"=== 下市清單：{len(rows)} 家 ===")


def run(start_year: int, out_dir: str, export_only: bool = False,
        delay: float = 0.8):
    t0 = time.time()
    conn = get_local_conn()
    ensure_local_tables(conn)
    logger.info(f"########## 本機歷史回補開始：{start_year} ~ 今天（DB: {conn}）##########")

    if not export_only:
        backfill_delisted_local(conn)
        backfill_prices_and_institutional(conn, start_year, delay=delay)
        backfill_dividends_local(conn, start_year, delay=delay)
        backfill_revenue_local(conn, start_year, delay=delay)

    logger.info(f"=== 匯出 parquet → {out_dir} ===")
    stats = export_to_parquet(conn, out_dir)
    logger.info(f"匯出完成：{stats}")
    try:
        from scripts.db_to_parquet import export_metadata
        export_metadata(out_dir)
    except Exception as e:
        logger.warning(f"參考主檔匯出失敗；parquet 暫不可作正式回測：{e}")

    elapsed = (time.time() - t0) / 60
    logger.info(f"########## 完成，總耗時 {elapsed:.1f} 分鐘 ##########")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research"))
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--delay", type=float, default=0.8,
                    help="每個公開端點請求間隔秒數；建議至少 0.1")
    a = ap.parse_args()
    run(a.start_year, a.out, a.export_only, delay=max(0.0, a.delay))
