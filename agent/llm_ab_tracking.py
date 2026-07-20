"""
agent/llm_ab_tracking.py

SPEC_QUANT_UPGRADE.md 決策點3（2026-07-17已同意、2026-07-20補做）：LLM層平行A/B量測。

核心立場：每日pipeline本來就會先算出「量化引擎純用分數排序會選誰」（factor_screen
階段的候選池），再讓LLM辯論+裁決選出最終推薦——這兩組資料本來就存在，只是之前
沒有額外存一份方便比對的紀錄。這裡新增的記錄動作**不多打任何一次LLM API**，
純粹是把已經算好的東西多存一份，供之後拿實際後續報酬回頭比較「量化自己選 vs
LLM裁決後選」誰的風險調整後報酬比較好——不量測就沒有證據决定要不要繼續投資
讓LLM辯論更複雜（多輪/loop engineering），這是那個決定的前提。

用法（daily_runner.py 在算完 candidates 和 LLM result 之後呼叫）：
    from agent.llm_ab_tracking import record_daily_picks
    record_daily_picks(eval_date, candidates, result, pick_top_n=5)
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database.connection import get_session


def ensure_llm_ab_tracking_table():
    """冪等建表（同 migration 20 內容，現有 DB 自動補上，不需手動跑 migration）。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS llm_ab_tracking (
                signal_date DATE         NOT NULL,
                source      VARCHAR(20)  NOT NULL,
                stock_id    VARCHAR(10)  NOT NULL,
                rank        SMALLINT,
                score       NUMERIC,
                reason      TEXT,
                PRIMARY KEY (signal_date, source, stock_id)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_llm_ab_tracking_date "
                       "ON llm_ab_tracking (signal_date)"))


def build_quant_only_rows(candidates: pd.DataFrame, pick_top_n: int) -> list[dict]:
    """
    純函式：candidates 是 stock_selector.get_candidate_stocks() 的回傳（已依score
    排序，可能已套用族群曝險上限——這裡故意直接沿用，因為那也是「量化引擎自己的
    邏輯」的一部分，不是只看裸分數，才是公平的「量化 vs LLM」對照組）。
    取前 pick_top_n 檔組成待寫入的列。
    """
    if candidates is None or candidates.empty or "score" not in candidates.columns:
        return []
    top = candidates.head(pick_top_n)
    return [{"stock_id": r.stock_id, "rank": i + 1, "score": float(r.score)}
           for i, r in enumerate(top.itertuples())]


def build_llm_rows(result: dict | None) -> list[dict]:
    """純函式：llm_advisor.generate_recommendations() 的回傳 → 待寫入的列。"""
    if not result:
        return []
    recs = result.get("recommendations") or []
    return [{"stock_id": r["stock_id"], "rank": i + 1, "reason": r.get("reason", "")}
           for i, r in enumerate(recs) if r.get("stock_id")]


def record_daily_picks(signal_date: date, candidates: pd.DataFrame, result: dict | None,
                       pick_top_n: int = 5) -> dict:
    """
    寫入當天的「量化自己選」+「LLM最終選」兩組紀錄。失敗不拋例外（不能因為記錄
    這個輔助功能失敗就打斷正式推薦流程），回傳 {"quant_only": n, "llm": n} 筆數。
    """
    quant_rows = build_quant_only_rows(candidates, pick_top_n)
    llm_rows = build_llm_rows(result)
    if not quant_rows and not llm_rows:
        return {"quant_only": 0, "llm": 0}

    try:
        ensure_llm_ab_tracking_table()
        with get_session() as s:
            for row in quant_rows:
                s.execute(text("""
                    INSERT INTO llm_ab_tracking (signal_date, source, stock_id, rank, score)
                    VALUES (:d, 'quant_only', :sid, :rank, :score)
                    ON CONFLICT (signal_date, source, stock_id) DO UPDATE SET
                        rank = EXCLUDED.rank, score = EXCLUDED.score
                """), {"d": signal_date, "sid": row["stock_id"],
                       "rank": row["rank"], "score": row["score"]})
            for row in llm_rows:
                s.execute(text("""
                    INSERT INTO llm_ab_tracking (signal_date, source, stock_id, rank, reason)
                    VALUES (:d, 'llm', :sid, :rank, :reason)
                    ON CONFLICT (signal_date, source, stock_id) DO UPDATE SET
                        rank = EXCLUDED.rank, reason = EXCLUDED.reason
                """), {"d": signal_date, "sid": row["stock_id"],
                       "rank": row["rank"], "reason": row["reason"]})
        logger.info(f"LLM A/B量測記錄：{signal_date} quant_only={len(quant_rows)}筆、llm={len(llm_rows)}筆")
    except Exception as e:
        logger.warning(f"LLM A/B量測記錄失敗（不影響正式推薦流程）: {e}")
    return {"quant_only": len(quant_rows), "llm": len(llm_rows)}
