"""
agent/daily_runner.py

每日流程的入口：
  1. 計算族群輪動
  2. 檢查持有部位是否該出場（獨立於 LLM，LLM 掛了也要發賣出提醒）
  3. 篩選候選股票 → LLM 產生進場推薦
  4. 把推薦開成新的追蹤部位
  5. 合併「進場推薦 + 出場提醒」報告
"""
from loguru import logger
from datetime import date

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from sqlalchemy import text
from database.connection import get_session
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
from agent.portfolio import evaluate_exits, record_entries, format_positions_report


def _latest_trade_date():
    with get_session() as s:
        return s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar()


def run_daily_recommendation(with_entries: bool = True):
    """
    with_entries=True ：產生隔日進場推薦並開部位（交易日前夜用）
    with_entries=False：只更新族群熱度 + 檢查持倉出場，不產生新進場（週末用）
    """
    logger.info("=" * 50)
    logger.info(f"每日流程開始：{date.today()}（進場推薦={'開' if with_entries else '關'}）")
    logger.info("=" * 50)

    eval_date = _latest_trade_date() or date.today()

    # Step 1: 更新族群輪動熱度
    logger.info("Step 1 — 計算族群輪動熱度")
    run_sector_momentum()

    # Step 2: 出場檢查（先做，且不依賴 LLM —— 確保賣出提醒一定會發）
    logger.info("Step 2 — 檢查持有部位出場訊號")
    exits = evaluate_exits(eval_date)

    result, opened = {}, []
    if with_entries:
        # Step 3: 取熱門產業 + 篩選候選股票
        logger.info("Step 3 — 篩選候選股票")
        hot_sectors = get_hot_sectors(top_n=5, min_stocks=10)
        candidates = get_candidate_stocks(hot_sectors, top_n=20) if hot_sectors else None

        if candidates is not None and not candidates.empty:
            logger.info("Step 4 — 呼叫 LLM 產生推薦")
            candidates_text = format_candidates_for_llm(candidates)
            hot_sector_names = candidates["industry"].unique().tolist()
            result = generate_recommendations(candidates_text, hot_sector_names)
            if result:
                save_recommendations(result)
                picks = [{"stock_id": r["stock_id"], "reason": r.get("reason", "")}
                         for r in result.get("recommendations", [])]
                opened = record_entries(picks, eval_date)
            else:
                logger.error("LLM 未回傳有效結果，僅輸出出場提醒")
        else:
            logger.warning("無候選股票，僅輸出出場提醒")
    else:
        logger.info("（週末模式：略過新進場推薦，只檢查持倉出場）")

    # 合併報告（進場推薦 + 出場提醒）
    logger.info("產出報告")
    if result:
        report = format_report(result)
    elif with_entries:
        report = "📊 今日無新進場推薦（候選不足或 LLM 失敗）"
    else:
        report = "📅 週末模式：今日不產生新進場推薦，僅追蹤持倉"
    report += "\n\n" + format_positions_report(exits, opened)
    try:
        print("\n" + report)
    except UnicodeEncodeError:
        print("\n" + report.encode("ascii", "replace").decode("ascii"))

    logger.info("=" * 50)
    logger.info("每日流程完成")
    logger.info("=" * 50)

    # 把完整報告掛在 result 上，供 Telegram 使用（即使無推薦也回傳含出場資訊的 dict）
    if not isinstance(result, dict):
        result = {}
    result["report_text"] = report
    return result


if __name__ == "__main__":
    run_daily_recommendation()
