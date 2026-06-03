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


def mode_init(lookback_days: int = 365, limit: int = None):
    """
    首次初始化：拉最近 N 天的歷史資料
    建議先跑一次，之後改用 daily mode

    limit: 只抓前幾支股票（測試用）。None=全部。
    """
    logger.info(f"=== 初始化模式：拉取最近 {lookback_days} 天資料 ===")

    if not test_connection():
        logger.error("無法連接資料庫，請確認 Docker 已啟動")
        return

    # Step 1: 更新股票清單
    logger.info("Step 1/3 — 更新股票清單")
    df_stocks = fetch_stock_list()
    upsert_stock_list(df_stocks)

    # Step 2: 抓取歷史股價（預設全部；--limit 可只抓前幾支做測試）
    all_stocks = df_stocks["stock_id"].tolist()
    target_stocks = all_stocks[:limit] if limit else all_stocks
    logger.info(f"Step 2/3 — 抓取 {len(target_stocks)} 支股票的歷史股價")

    start = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    batch_fetch_prices(target_stocks, start_date=start, delay=1.2)

    # Step 3: 籌碼資料（近 90 天，FinMind 免費版限制）
    logger.info("Step 3/3 — 抓取籌碼資料（近 90 天）")
    start_inst = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    batch_fetch_institutional(target_stocks, start_date=start_inst, delay=1.2)

    logger.info("=== 初始化完成 ===")


def mode_daily(source: str = "openapi"):
    """
    每日例行更新：抓最近交易日的資料

    source:
      openapi = 證交所/櫃買官方 API，全市場一次抓完（約 4 次請求、數秒），預設
      finmind = 逐檔 FinMind（受 600 次/hr 限制，較慢，僅後備用）
    """
    logger.info(f"=== 每日更新模式（source={source}）===")

    if not test_connection():
        logger.error("無法連接資料庫")
        return

    if source == "openapi":
        from data_pipeline.fetchers.twse_fetcher import update_daily_via_openapi
        update_daily_via_openapi()
        return

    # ── 以下為 FinMind 後備路徑（逐檔抓）──────────────────────────
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


def mode_pipeline(source: str = "openapi"):
    """
    每日完整流程（這是每天要自動跑的）：
        補齊缺漏交易日 → 算技術指標 → 產生推薦（推薦會自動先算族群熱度）

    第 1 步用 backfill 而非單抓今天，因此即使某天漏跑，下次一跑就會自動補回，
    具自我修復能力。source=finmind 時改走 FinMind 逐檔後備路徑。
    """
    from agent.notifier import notify_success, notify_failure

    if not test_connection():
        logger.error("無法連接資料庫，流程中止")
        notify_failure("資料庫連線", "無法連接資料庫（請確認 Docker 是否啟動）")
        return

    logger.info("########## 每日完整流程開始 ##########")
    try:
        if source == "openapi":
            from data_pipeline.fetchers.twse_fetcher import backfill
            backfill()                   # 1. 自動補齊 DB 最後一天 ~ 今天（含今天）
        else:
            mode_daily(source="finmind")  # 後備：FinMind 抓近 3 天
        run_technical_analysis()         # 2. 重算技術指標
        result = run_daily_recommendation()  # 3. 出場檢查 + 族群熱度 + 候選篩選 + LLM 推薦 + 開部位
        notify_success(result.get("report_text") if result else None)
        logger.info("########## 每日完整流程結束 ##########")
    except Exception as e:
        logger.exception("每日完整流程失敗")
        notify_failure("每日流程", str(e))
        raise


def mode_backfill(days: int = None):
    """
    補抓資料：把 DB 缺的交易日補齊（用證交所/櫃買「指定日期」端點，每日約 4 次請求）。
    預設自動從 DB 最後一天接續補到今天；給 --days N 則改補最近 N 天。
    """
    if not test_connection():
        return
    from datetime import date, timedelta
    from data_pipeline.fetchers.twse_fetcher import backfill
    start = (date.today() - timedelta(days=days)) if days else None
    backfill(start=start)


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

    # 每天 18:00 跑完整流程（抓資料 → 技術指標 → 推薦）
    scheduler.add_job(
        mode_pipeline,
        "cron",
        hour=ScheduleConfig.DAILY_FETCH_HOUR,
        minute=0,
        id="daily_pipeline",
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
        choices=["init", "daily", "pipeline", "backfill", "industry", "technical", "sector", "recommend", "backtest", "schedule"],
        default="daily",
        help="執行模式（pipeline=每日完整流程；backfill=補齊缺漏交易日；backtest=回測選股績效）",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="init：拉幾天歷史（預設 365）；backfill：補最近幾天（預設自動接續 DB 最後一天）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="init 模式只抓前幾支股票（測試用，預設全部）",
    )
    parser.add_argument(
        "--source",
        choices=["openapi", "finmind"],
        default="openapi",
        help="daily 模式資料來源：openapi=證交所/櫃買官方(快、免限流)，finmind=逐檔抓",
    )
    args = parser.parse_args()

    if args.mode == "init":
        mode_init(lookback_days=args.days or 365, limit=args.limit)
    elif args.mode == "daily":
        mode_daily(source=args.source)
    elif args.mode == "pipeline":
        mode_pipeline(source=args.source)
    elif args.mode == "backfill":
        mode_backfill(days=args.days)
    elif args.mode == "industry":
        mode_industry()
    elif args.mode == "technical":
        run_technical_analysis()
    elif args.mode == "sector":
        run_sector_momentum()
    elif args.mode == "recommend":
        run_daily_recommendation()
    elif args.mode == "backtest":
        from agent.backtest import run_backtest
        run_backtest()
    elif args.mode == "schedule":
        mode_schedule()