"""
agent/daily_runner.py

每日推薦流程的入口：
  1. 計算族群輪動
  2. 篩選候選股票
  3. 呼叫 LLM 產生推薦
  4. 存入 DB
  5. 印出報告
"""
from loguru import logger
from datetime import date

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data_pipeline.analysis.sector_momentum import run_sector_momentum
from agent.stock_selector import (
    get_hot_sectors,
    get_candidate_stocks,
    format_candidates_for_llm,
)
from agent.llm_advisor import (
    generate_recommendations,
    save_recommendations,
    format_report,
)


def run_daily_recommendation():
    logger.info("=" * 50)
    logger.info(f"每日推薦流程開始：{date.today()}")
    logger.info("=" * 50)

    # Step 1: 更新族群輪動熱度
    logger.info("Step 1/4 — 計算族群輪動熱度")
    run_sector_momentum()

    # Step 2: 取熱門產業 + 篩選候選股票
    logger.info("Step 2/4 — 篩選候選股票")
    hot_sectors = get_hot_sectors(top_n=5, min_stocks=10)
    if not hot_sectors:
        logger.error("無法取得熱門產業，流程中止")
        return

    candidates = get_candidate_stocks(hot_sectors, top_n=20)
    if candidates.empty:
        logger.error("沒有符合條件的候選股票，流程中止")
        return

    # Step 3: 格式化資料並呼叫 LLM
    logger.info("Step 3/4 — 呼叫 LLM 產生推薦")
    candidates_text = format_candidates_for_llm(candidates)
    hot_sector_names = candidates["industry"].unique().tolist()
    result = generate_recommendations(candidates_text, hot_sector_names)

    if not result:
        logger.error("LLM 未回傳有效結果，流程中止")
        return

    # Step 4: 存入 DB 並印出報告
    logger.info("Step 4/4 — 儲存結果並產出報告")
    save_recommendations(result)

    report = format_report(result)
    print("\n" + report)

    logger.info("=" * 50)
    logger.info("每日推薦流程完成")
    logger.info("=" * 50)

    return result


if __name__ == "__main__":
    run_daily_recommendation()