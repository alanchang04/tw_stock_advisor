"""
agent/watchlist_alerts.py

聰明的日級通知（2026-07-25，「方向 A」）——只在「有動作可做」時叫你，不洗版。

核心是**每日排名的進榜/退榜異動**：把每天的「AI 因子排名前 N」存起來，跟昨天 diff，
推播「今天新進榜（尤其投信連買+營收正成長）」與「今天退榜（訊號轉弱）」。

為什麼這是對的通知：edge 是日級的（營收月更、投信收盤後才公布），盤中沒有已驗證
訊號可反應。所以正確的通知不是「更即時」，是「收盤後算完、只在該理你時叫你」——
貼著 edge 的時間尺度，不做時間尺度錯配（今天一整天反覆遇到的那個坑）。

純函式（diff / format）不碰 DB，方便測試；DB 膠水另外包。
"""
from __future__ import annotations

from datetime import date

from loguru import logger
from sqlalchemy import text

from database.connection import get_session

#: 進榜時要「加星標」的門檻——兩個已驗證因子同時成立才算強訊號
STAR_MIN_STREAK = 3       # 投信連買 ≥ 3 日
STAR_MIN_REV = 0.0        # 營收年增 > 0


def ensure_ranking_table():
    """冪等建表（現有 DB 不會重跑 init.sql，比照其他 fetcher）。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS ranked_watchlist_history (
                rec_date      DATE        NOT NULL,
                stock_id      VARCHAR(10) NOT NULL,
                rank          INTEGER     NOT NULL,
                score         NUMERIC(10,4),
                invest_streak INTEGER,
                rev_yoy       NUMERIC(10,2),
                created_at    TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (rec_date, stock_id)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_ranked_wl_date "
                       "ON ranked_watchlist_history (rec_date)"))
        s.commit()


def diff_rankings(today: list[dict], prev_ids: set[str]) -> dict:
    """
    純函式：今天的排名（list of dict，需含 stock_id/stock_name/rank/invest_streak/rev_yoy）
    對照昨天的 stock_id 集合，回傳進榜/退榜。

    prev_ids 為空（首次、無歷史）→ entered/dropped 都空，交由呼叫端當「首次建立基準」。
    """
    if not prev_ids:
        return {"entered": [], "dropped": [], "first_run": True}
    today_ids = {r["stock_id"] for r in today}
    entered = [r for r in today if r["stock_id"] not in prev_ids]
    dropped = sorted(prev_ids - today_ids)
    return {"entered": entered, "dropped": dropped, "first_run": False}


def _is_star(r: dict) -> bool:
    streak = r.get("invest_streak") or 0
    rev = r.get("rev_yoy")
    return streak >= STAR_MIN_STREAK and rev is not None and rev > STAR_MIN_REV


def format_ranking_alert(diff: dict, rec_date, dropped_names: dict | None = None) -> str | None:
    """把 diff 排成 Telegram 文字。沒有任何異動 → 回 None（不推播、不洗版）。"""
    if diff.get("first_run"):
        return None
    entered, dropped = diff.get("entered", []), diff.get("dropped", [])
    if not entered and not dropped:
        return None
    dropped_names = dropped_names or {}
    lines = [f"📋 {rec_date} 排名異動（AI 因子前 20）"]
    if entered:
        lines.append("\n🆕 新進榜：")
        for r in sorted(entered, key=lambda x: x.get("rank", 999)):
            star = "⭐ " if _is_star(r) else ""
            streak = int(r.get("invest_streak") or 0)
            rev = r.get("rev_yoy")
            chips = (f"投信連買{streak}日" if streak > 0 else "投信未連買")
            chips += f"｜營收{rev:+.1f}%" if rev is not None else "｜營收無資料"
            lines.append(f"  {star}#{r.get('rank','?')} {r['stock_id']} "
                         f"{r.get('stock_name','')}（{chips}）")
        if any(_is_star(r) for r in entered):
            lines.append("  （⭐＝投信連買+營收正成長，兩個已驗證因子同時成立）")
    if dropped:
        lines.append("\n📉 退榜（訊號轉弱，若持有留意）：")
        lines.append("  " + "、".join(
            f"{sid} {dropped_names.get(sid, '')}".strip() for sid in dropped))
    lines.append("\n⚠️ 這是排名異動不是買賣單，進出自己判斷（詳見 App「📋 每日排行」）。")
    return "\n".join(lines)


def _load_prev_ids(rec_date) -> set[str]:
    """取「今天以前最近一個交易日」的排名 stock_id 集合。"""
    with get_session() as s:
        prev = s.execute(text(
            "SELECT MAX(rec_date) FROM ranked_watchlist_history WHERE rec_date < :d"
        ), {"d": rec_date}).scalar()
        if prev is None:
            return set()
        rows = s.execute(text(
            "SELECT stock_id FROM ranked_watchlist_history WHERE rec_date = :d"
        ), {"d": prev}).fetchall()
    return {r[0] for r in rows}


def _save_ranking(rec_date, rows: list[dict]):
    """存今天的排名（冪等：同 rec_date 重跑會覆蓋）。"""
    if not rows:
        return
    stmt = text("""
        INSERT INTO ranked_watchlist_history
            (rec_date, stock_id, rank, score, invest_streak, rev_yoy)
        VALUES (:rec_date, :stock_id, :rank, :score, :invest_streak, :rev_yoy)
        ON CONFLICT (rec_date, stock_id) DO UPDATE SET
            rank = EXCLUDED.rank, score = EXCLUDED.score,
            invest_streak = EXCLUDED.invest_streak, rev_yoy = EXCLUDED.rev_yoy
    """)
    payload = [{"rec_date": rec_date, "stock_id": r["stock_id"], "rank": r["rank"],
                "score": r.get("score"), "invest_streak": r.get("invest_streak"),
                "rev_yoy": r.get("rev_yoy")} for r in rows]
    with get_session() as s:
        s.execute(stmt, payload)
        s.commit()


def _rows_from_ranked_df(df) -> list[dict]:
    """get_ranked_watchlist() 的 DataFrame → 存檔/diff 用的 dict list。"""
    import pandas as pd

    def _n(v):
        try:
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    out = []
    for i, r in enumerate(df.itertuples(), 1):
        streak = _n(getattr(r, "invest_streak", None))
        out.append({
            "stock_id": str(getattr(r, "stock_id")),
            "stock_name": getattr(r, "stock_name", "") or "",
            "rank": i,
            "score": _n(getattr(r, "score", None)),
            "invest_streak": int(streak) if streak is not None else 0,
            "rev_yoy": _n(getattr(r, "rev_yoy", None)),
        })
    return out


def daily_ranking_alert(top_n: int = 20, ranked_df=None) -> str | None:
    """
    每日排名異動通知的總入口（run_pipeline 呼叫）：
      算今日排名 → 對照昨日 → 存今日 → 回傳推播文字（無異動/首次 → None）。
    任一步失敗都不擋 pipeline（回 None + log）。

    ranked_df：pipeline 已算好的 get_ranked_watchlist() 結果可傳進來，避免重算。
    """
    try:
        ensure_ranking_table()
        df = ranked_df
        if df is None:
            from agent.stock_selector import get_ranked_watchlist
            df = get_ranked_watchlist(top_n=top_n)
        if df is None or df.empty:
            logger.info("排名異動：今日無候選，略過")
            return None
        with get_session() as s:
            rec_date = s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar() \
                or date.today()
        rows = _rows_from_ranked_df(df)
        prev_ids = _load_prev_ids(rec_date)
        diff = diff_rankings(rows, prev_ids)
        # 退榜股名（給推播顯示用）——從昨日紀錄補名字
        dropped_names = {}
        if diff.get("dropped"):
            with get_session() as s:
                nm = s.execute(text(
                    "SELECT stock_id, stock_name FROM stocks WHERE stock_id = ANY(:ids)"
                ), {"ids": diff["dropped"]}).fetchall()
            dropped_names = {r[0]: r[1] for r in nm}
        _save_ranking(rec_date, rows)
        msg = format_ranking_alert(diff, rec_date, dropped_names)
        if diff.get("first_run"):
            logger.info("排名異動：首次建立基準，下次起才有 diff")
        return msg
    except Exception as e:
        logger.warning(f"排名異動通知計算失敗（不擋流程）: {e}")
        return None
