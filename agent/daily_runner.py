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
from agent.portfolio import format_positions_report
from agent.broker import get_broker
from agent import exec_log


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
    with exec_log.stage("sector_momentum") as rec:
        run_sector_momentum()
        rec.summary = "官方54類族群5日動能已更新（熱門排名供參考，趨勢版選股不設硬閘門）"

    # 交易層：預設 PaperBroker（紙上模擬，行為同現況）；BROKER=shioaji 可換券商
    broker = get_broker()

    # Step 1.5: 先把已成交的委託回帳（paper: 昨日掛單於今開盤成交）
    logger.info(f"Step 1.5 — 回帳已成交委託（broker={broker.name}）")
    with exec_log.stage("fills") as rec:
        filled = broker.sync(eval_date)
        rec.summary = (f"broker={broker.name}：開盤成交 買{len(filled.get('entries', []))} "
                       f"賣{len(filled.get('exits', []))}")
        rec.payload = filled if (filled.get("entries") or filled.get("exits")) else None

    # Step 2: 出場檢查（先做，且不依賴 LLM —— 確保賣出提醒一定會發）
    #         只掛「明日開盤賣出」委託，不當場平倉
    logger.info("Step 2 — 檢查持有部位出場訊號（掛明日賣單）")
    with exec_log.stage("orders_exits") as rec:
        exits = broker.submit_exits(eval_date)
        rec.summary = (f"掛出明日開盤賣單 {len(exits)} 檔"
                       + ("：" + ", ".join(f"{e['stock_id']}({e['reason']})" for e in exits)
                          if exits else "（全部續抱）"))
        rec.payload = {"exits": exits} if exits else None

    result, opened = {}, []

    # 市場濾網：僅在 market_filter_block_entries=True 時空頭不開新倉
    # （預設 False：空頭只加回死亡交叉出場保護，見 portfolio.exit_cfg）
    from agent.strategy import STRATEGY as _S
    with exec_log.stage("risk_gate") as rec:
        blocked = False
        if with_entries and _S.get("market_filter_block_entries"):
            from agent.stock_selector import market_is_bull
            if not market_is_bull():
                logger.warning("市場濾網觸發（空頭）：今日不開新倉")
                with_entries = False
                blocked = True
        rec.summary = ("空頭濾網擋下新倉" if blocked else
                       f"進場{'開' if with_entries else '關(週末模式)'}；"
                       f"部位上限 {_S.get('max_open_positions', 10)} 檔")
        rec.payload = {"with_entries": with_entries, "blocked_by_market_filter": blocked,
                       "market_filter_block_entries": _S.get("market_filter_block_entries", False)}

    if with_entries:
        # Step 3: 篩選候選股票（趨勢版選股不用族群硬閘門；熱門族群仍供 LLM 參考）
        logger.info("Step 3 — 篩選候選股票")
        from agent.strategy import STRATEGY as _ST
        with exec_log.stage("factor_screen") as rec:
            hot_sectors = get_hot_sectors(top_n=5, min_stocks=10)
            if _ST.get("use_hot_sector_gate", True):
                candidates = get_candidate_stocks(hot_sectors, top_n=20) if hot_sectors else None
            else:
                candidates = get_candidate_stocks(hot_sectors, top_n=20)
            n = 0 if candidates is None else len(candidates)
            rec.summary = f"熱門族群 {len(hot_sectors or [])} 個；評分後取前 {n} 檔進辯論"
            if candidates is not None and not candidates.empty:
                # payload 紀律：只存前 20 檔的因子明細（不存全市場），控制在 ~5KB
                keep = [c for c in ["stock_id", "stock_name", "industry", "close", "score",
                                    "rs20", "stack_days", "invest_streak", "mom60",
                                    "rev_yoy", "rsi14", "inst_net", "foreign_net"]
                        if c in candidates.columns]
                rec.payload = {"hot_sectors": hot_sectors,
                               "top_candidates": candidates[keep].head(20).to_dict("records")}

        if candidates is not None and not candidates.empty:
            logger.info("Step 4 — 呼叫 LLM 產生推薦")
            candidates_text = format_candidates_for_llm(candidates)
            hot_sector_names = candidates["industry"].unique().tolist()
            # debate_bull / debate_bear / judge 三段在 llm_advisor 內部各自記錄
            result = generate_recommendations(candidates_text, hot_sector_names)
            try:
                from agent.llm_ab_tracking import record_daily_picks
                record_daily_picks(eval_date, candidates, result, pick_top_n=_ST.get("pick_top_n", 5))
            except Exception as e:
                logger.warning(f"LLM A/B量測記錄失敗（不影響正式推薦流程）: {e}")
            if result:
                save_recommendations(result)
                picks = [{"stock_id": r["stock_id"], "reason": r.get("reason", "")}
                         for r in result.get("recommendations", [])]
                with exec_log.stage("orders_entries") as rec:
                    opened = broker.submit_entries(picks, eval_date)
                    rec.summary = (f"掛出明日開盤買單 {len(opened)} 檔"
                                   + ("：" + ", ".join(o["stock_id"] for o in opened)
                                      if opened else "（名額已滿或均已持有）"))
                    rec.payload = {"picks": picks, "queued": opened} if picks else None
            else:
                logger.error("LLM 未回傳有效結果，僅輸出出場提醒")
        else:
            logger.warning("無候選股票，僅輸出出場提醒")
    else:
        # 2026-07-20修正：這裡原本不分青紅皂白都印「週末模式」，但with_entries=False
        # 有兩種完全不同的成因——真正的週末(mode_auto的星期判斷)，或空頭市場濾網
        # 擋新倉(跟星期幾無關，交易日一樣會觸發)。訊息混用「週末模式」誤導使用者
        # 以為系統認錯日期，實際上tw_today()從頭到尾都是對的，只是這句話沒講清楚
        # 是市場濾網在擋，不是日期算錯。
        logger.info("（空頭濾網擋新倉：略過新進場推薦，只檢查持倉出場）" if blocked
                   else "（週末模式：略過新進場推薦，只檢查持倉出場）")

    # 合併報告（進場推薦 + 出場提醒）
    logger.info("產出報告")
    if result:
        report = format_report(result)
    elif with_entries:
        report = "📊 今日無新進場推薦（候選不足或 LLM 失敗）"
    elif blocked:
        report = "🐻 空頭濾網擋新倉：今日不產生新進場推薦，僅追蹤持倉（非週末，是市場濾網判斷空頭）"
    else:
        report = "📅 週末模式：今日不產生新進場推薦，僅追蹤持倉"
    report += "\n\n" + format_positions_report(exits, opened, filled)
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
