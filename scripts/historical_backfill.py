"""
scripts/historical_backfill.py

SPEC_QUANT_UPGRADE.md P0：10年+官方歷史回補主控腳本，四步依序執行：
  1. 下市股票清單（TWSE，登記進 stocks 表 is_active=False——一定要先做這步，
     否則第2步的價量回補在遇到下市股 stock_id 時，`_filter_known()` 會把它們
     整批靜默濾掉，倖存者偏誤依舊修不掉）
  2. 全市場價量 + 三大法人（沿用既有 twse_fetcher.backfill()，逐日回補，可中斷續跑：
     重跑會自動從 DB 現有最後一天接續，不會重複抓已有的日子）
  3. 除權息事件（corporate_actions_fetcher.backfill_dividend_events()，逐月回補，
     upsert 不重複）
  4. 匯出成本機 parquet（scripts/db_to_parquet.py，供 run_backtest(parquet_dir=...) 用，
     不佔 Neon 空間）

**誠實限制**（開發時已實測，非猜測）：
  - TPEX 三大法人官方端點只能查到約 2019 年之後的資料，2019 年以前上櫃股的法人
    籌碼因子會是空值（TWSE 上市股不受影響，2015年起可查）。
  - TPEX 下市清單目前沒找到對應端點，只有 TWSE 的下市清單被登記，上櫃的倖存者
    偏誤修正不完整。

跑很久（10年×2市場的價量+法人逐日回補，預估數小時），設計成可安全中斷、
重跑會自動接續，不會重複做已完成的部分。

用法：
    python scripts/historical_backfill.py --start-year 2015
"""
import argparse
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from loguru import logger

logger.add("logs/historical_backfill_{time:YYYY-MM-DD}.log", rotation="1 day", retention="14 days", level="INFO")


def run(start_year: int, out_dir: str, skip_prices: bool = False):
    t0 = time.time()
    start_date = date(start_year, 1, 1)

    logger.info(f"########## 歷史回補開始：{start_year} ~ 今天 ##########")

    logger.info("=== 步驟 1/4：TWSE 下市股票清單（含登記進 stocks 表）===")
    from data_pipeline.fetchers.corporate_actions_fetcher import (
        backfill_delisted_stocks, backfill_dividend_events,
    )
    try:
        n_delisted = backfill_delisted_stocks()
        logger.info(f"下市清單完成：{n_delisted} 家")
    except Exception as e:
        logger.error(f"下市清單回補失敗（不中斷，繼續下一步）: {e}")

    if not skip_prices:
        logger.info(f"=== 步驟 2/4：全市場價量 + 三大法人，{start_date} ~ 今天（最耗時）===")
        from data_pipeline.fetchers.twse_fetcher import backfill as price_backfill
        try:
            price_backfill(start=start_date)
        except Exception as e:
            logger.error(f"價量/法人回補中途失敗：{e}（可重跑本腳本自動接續，不會重抓已完成的日子）")
    else:
        logger.info("=== 步驟 2/4：略過（--skip-prices）===")

    logger.info(f"=== 步驟 3/4：除權息事件，{start_year} ~ 今天 ===")
    try:
        backfill_dividend_events(start_year)
    except Exception as e:
        logger.error(f"除權息回補失敗（不中斷，繼續下一步）: {e}")

    logger.info(f"=== 步驟 4/4：匯出本機 parquet → {out_dir} ===")
    try:
        from scripts.db_to_parquet import export
        export(out_dir)
    except Exception as e:
        logger.error(f"parquet 匯出失敗: {e}")

    elapsed = (time.time() - t0) / 60
    logger.info(f"########## 歷史回補完成，總耗時 {elapsed:.1f} 分鐘 ##########")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research"))
    ap.add_argument("--skip-prices", action="store_true",
                    help="只做下市清單+除權息+匯出，跳過最耗時的價量回補（除錯/重跑其他步驟用）")
    a = ap.parse_args()
    run(a.start_year, a.out, a.skip_prices)
