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


def mode_market_signals():
    """
    ETF 換股偵測 + 財經新聞 + YouTube 財經摘要
    每日 pipeline 結尾呼叫，結果寫入 market_signals
    """
    logger.info("=== 市場情報模組開始 ===")
    try:
        from data_pipeline.fetchers.etf_fetcher import run_etf_tracking
        run_etf_tracking()
    except Exception as e:
        logger.error(f"ETF 換股偵測失敗: {e}")

    try:
        from data_pipeline.scrapers.news_scraper import run_news_scraper
        run_news_scraper()
    except Exception as e:
        logger.error(f"財經新聞爬取失敗: {e}")

    try:
        from data_pipeline.scrapers.youtube_scraper import run_youtube_scraper
        run_youtube_scraper()
    except Exception as e:
        logger.error(f"YouTube 分析失敗: {e}")

    logger.info("=== 市場情報模組完成 ===")

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


def _weekly_review_text() -> str:
    """週末策略回顧：跑回測，回傳精簡摘要文字（供 Telegram）。"""
    import io, contextlib
    try:
        from agent.backtest import run_backtest
        with contextlib.redirect_stdout(io.StringIO()):
            tdf = run_backtest()
        if tdf is None or len(tdf) == 0:
            return "📈 週末策略回顧：回測資料不足"
        win = (tdf["ret"] > 0).mean() * 100
        avg = tdf["ret"].mean() * 100
        hold = tdf["hold"].mean()
        return ("📈 週末策略回顧（回測 {} 筆完整交易）\n"
                "  勝率 {:.0f}%、平均報酬 {:+.2f}%/筆、平均持有 {:.1f} 日\n"
                "  ※ 策略維持固定；要調買賣邏輯請改 agent/strategy.py 後重跑 --mode backtest 驗證，"
                "不要每天自動改（否則無法回測）。").format(len(tdf), win, avg, hold)
    except Exception as e:
        return f"📈 週末策略回顧失敗：{e}"


def mode_pipeline(source: str = "openapi", with_entries: bool = True, review: bool = False):
    """
    每日完整流程：補齊缺漏交易日 → 算技術指標 → 出場檢查（+選股推薦）→ Telegram。

    - with_entries=True：產生隔日進場推薦並開部位（交易日前夜）
    - with_entries=False：只更新資料 + 檢查持倉出場（週末）
    - review=True：附上回測策略回顧（週末）
    第 1 步用 backfill，故某天漏跑下次會自動補回。source=finmind 走後備路徑。
    """
    from agent.notifier import notify_success, notify_failure, check_and_respond
    check_and_respond()   # 處理排隊中的 Bot 互動指令

    if not test_connection():
        logger.error("無法連接資料庫，流程中止")
        notify_failure("資料庫連線", "無法連接資料庫（請確認 Docker 是否啟動）")
        return

    logger.info("########## 每日完整流程開始 ##########")
    try:
        if source == "openapi":
            from data_pipeline.fetchers.twse_fetcher import backfill
            backfill()                   # 1. 自動補齊 DB 最後一天 ~ 今天
        else:
            mode_daily(source="finmind")
        run_technical_analysis(recent_days=5)    # 2. 技術指標（增量：只寫最近 5 天，日常更新夠用）
        mode_market_signals()                    # 3. ETF換股 + 新聞 + YouTube
        result = run_daily_recommendation(with_entries=with_entries)  # 4. 出場檢查(+進場推薦)
        msg = result.get("report_text") if result else None
        if review:
            msg = (msg or "") + "\n\n" + _weekly_review_text()
        notify_success(msg)
        logger.info("########## 每日完整流程結束 ##########")
    except Exception as e:
        logger.exception("每日完整流程失敗")
        notify_failure("每日流程", str(e))
        raise


def mode_auto(source: str = "openapi"):
    """
    依星期自動切換（給每日排程用）：
      週日~週四(隔天是交易日) → 產生隔日進場推薦
      週五、週六            → 不進場，只追蹤持倉 + 週末策略回顧
    """
    wd = date.today().weekday()          # Mon=0 ... Sun=6
    is_weekend_review = wd in (4, 5)     # 週五、週六
    mode_pipeline(source=source,
                  with_entries=not is_weekend_review,
                  review=is_weekend_review)


def mode_quick_update(top_n: int = 200):
    """
    快速盤中更新（目標 <1 分鐘）：
      1. TWSE/TPEX OpenAPI 一次拉全市場最新價格（~10 秒）
      2. 只針對「持倉中 + 成交量前 top_n 熱門股」重算最近 5 日技術指標（~40 秒）
    不做 LLM 推薦，不發 Telegram，純資料更新。
    """
    if not test_connection():
        logger.error("quick_update: DB 連線失敗")
        return

    logger.info(f"=== 快速更新開始（持倉 + 成交量前 {top_n} 支）===")

    # 1. 更新全市場今日價格（4 次請求，約 10 秒）
    from data_pipeline.fetchers.twse_fetcher import update_daily_via_openapi
    update_daily_via_openapi()

    # 2. 決定要重算指標的股票：持倉 + 近30日成交量最大的前 top_n 支
    from database.connection import get_session
    from sqlalchemy import text
    with get_session() as s:
        open_ids = [r[0] for r in s.execute(text(
            "SELECT stock_id FROM positions WHERE status='open'"
        )).fetchall()]
        hot_ids = [r[0] for r in s.execute(text(f"""
            SELECT stock_id FROM (
                SELECT stock_id, AVG(volume) AS avg_vol
                FROM daily_prices
                WHERE trade_date >= CURRENT_DATE - INTERVAL '30 days'
                GROUP BY stock_id
                ORDER BY avg_vol DESC
                LIMIT {top_n}
            ) t
        """)).fetchall()]

    target = list(set(open_ids) | set(hot_ids))
    logger.info(f"  重算指標：{len(target)} 支（持倉 {len(open_ids)} + 熱門 {len(hot_ids)}）")

    # 3. 只重算這些股票最近 5 天的指標
    run_technical_analysis(stock_ids=target, recent_days=5)
    logger.info("=== 快速更新完成 ===")


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

    # 每天 21:00 依星期自動切換（交易日前夜推薦 / 週末回顧）
    scheduler.add_job(
        mode_auto,
        "cron",
        hour=ScheduleConfig.DAILY_FETCH_HOUR,
        minute=0,
        id="daily_auto",
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
        choices=["init", "daily", "pipeline", "auto", "backfill", "industry", "technical", "sector", "recommend", "backtest", "schedule", "bot", "quick", "market"],
        default="daily",
        help="執行模式（auto=排程用，依星期自動切換；pipeline=完整流程；backfill=補洞；backtest=回測）",
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

    if args.mode == "bot":
        from agent.notifier import check_and_respond
        check_and_respond()
    elif args.mode == "quick":
        mode_quick_update()
    elif args.mode == "init":
        mode_init(lookback_days=args.days or 365, limit=args.limit)
    elif args.mode == "daily":
        mode_daily(source=args.source)
    elif args.mode == "pipeline":
        mode_pipeline(source=args.source)
    elif args.mode == "auto":
        mode_auto(source=args.source)
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
    elif args.mode == "market":
        mode_market_signals()
    elif args.mode == "schedule":
        mode_schedule()