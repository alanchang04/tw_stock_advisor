"""
run_pipeline.py — 每日資料 Pipeline 入口

執行方式：
    python run_pipeline.py --mode daily       # 每日例行更新（收盤後）
    python run_pipeline.py --mode init        # 首次初始化（拉歷史資料）
    python run_pipeline.py --mode industry    # 更新產業分類（每週一次即可）

排程（apscheduler）模式：
    python run_pipeline.py --mode schedule
"""
import argparse
from datetime import date, timedelta

from loguru import logger
from apscheduler.schedulers.blocking import BlockingScheduler

from database.connection import test_connection
from data_pipeline.fetchers.finmind_fetcher import (
    fetch_stock_list, upsert_stock_list,
    batch_fetch_prices, batch_fetch_institutional,
)
from data_pipeline.scrapers.moneydj_scraper import run_industry_scraper
from data_pipeline.analysis.technical import run_technical_analysis
from data_pipeline.analysis.sector_momentum import run_sector_momentum
from agent.daily_runner import run_daily_recommendation
from config.settings import ScheduleConfig

# 設定 log 輸出到檔案
logger.add("logs/pipeline_{time:YYYY-MM-DD}.log",
           rotation="1 day", retention="30 days", level="INFO")


def mode_init(lookback_days: int = 365):
    """
    首次初始化：拉最近 N 天的歷史資料
    建議先跑一次，之後改用 daily mode
    """
    logger.info(f"=== 初始化模式：拉取最近 {lookback_days} 天資料 ===")

    if not test_connection():
        logger.error("無法連接資料庫，請確認 Docker 已啟動")
        return

    # Step 1: 更新股票清單
    logger.info("Step 1/3 — 更新股票清單")
    df_stocks = fetch_stock_list()
    upsert_stock_list(df_stocks)

    # Step 2: 只取前 100 支主要股票做初始化（避免太久）
    # 正式使用時可移除這個限制
    all_stocks = df_stocks["stock_id"].tolist()
    target_stocks = all_stocks[:100]
    logger.info(f"Step 2/3 — 抓取 {len(target_stocks)} 支股票的歷史股價")

    start = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    batch_fetch_prices(target_stocks, start_date=start, delay=1.2)

    # Step 3: 籌碼資料（近 90 天，FinMind 免費版限制）
    logger.info("Step 3/3 — 抓取籌碼資料（近 90 天）")
    start_inst = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    batch_fetch_institutional(target_stocks, start_date=start_inst, delay=1.2)

    logger.info("=== 初始化完成 ===")


def mode_daily():
    """
    每日例行更新：只抓今天（或最近幾天）的資料
    """
    logger.info("=== 每日更新模式 ===")

    if not test_connection():
        logger.error("無法連接資料庫")
        return

    today = date.today().strftime("%Y-%m-%d")
    # 多抓3天，確保週一可以補到上週五的資料
    start = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")

    # 取 DB 中所有 active 股票
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as session:
        result = session.execute(
            text("SELECT stock_id FROM stocks WHERE is_active = TRUE ORDER BY stock_id")
        ).fetchall()
    stock_ids = [r[0] for r in result]

    if not stock_ids:
        logger.warning("stocks 資料表是空的，請先執行 --mode init")
        return

    logger.info(f"更新 {len(stock_ids)} 支股票的近期資料")
    batch_fetch_prices(stock_ids, start_date=start, delay=0.8)
    batch_fetch_institutional(stock_ids, start_date=start, delay=0.8)

    logger.info("=== 每日更新完成 ===")


def mode_industry():
    """更新產業分類（建議每週執行一次）"""
    if not test_connection():
        return
    run_industry_scraper()


def mode_schedule():
    """
    APScheduler 排程模式
    每天收盤後自動執行 daily update
    """
    scheduler = BlockingScheduler(timezone="Asia/Taipei")

    # 每天 18:00 抓收盤資料
    scheduler.add_job(
        mode_daily,
        "cron",
        hour=ScheduleConfig.DAILY_FETCH_HOUR,
        minute=0,
        id="daily_fetch",
    )

    # 每週一 08:00 更新產業分類
    scheduler.add_job(
        mode_industry,
        "cron",
        day_of_week="mon",
        hour=8,
        minute=0,
        id="weekly_industry",
    )

    logger.info(f"排程已啟動 — 每天 {ScheduleConfig.DAILY_FETCH_HOUR}:00 更新")
    logger.info("按 Ctrl+C 停止")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("排程已停止")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股資料 Pipeline")
    parser.add_argument(
        "--mode",
        choices=["init", "daily", "industry", "technical", "sector", "recommend", "schedule"],
        default="daily",
        help="執行模式",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="init 模式要拉幾天的歷史資料（預設 365）",
    )
    args = parser.parse_args()

    if args.mode == "init":
        mode_init(lookback_days=args.days)
    elif args.mode == "daily":
        mode_daily()
    elif args.mode == "industry":
        mode_industry()
    elif args.mode == "technical":
        run_technical_analysis()
    elif args.mode == "sector":
        run_sector_momentum()
    elif args.mode == "recommend":
        run_daily_recommendation()
    elif args.mode == "schedule":
        mode_schedule()