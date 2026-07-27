"""
agent/pick_journal.py

前向計分板 / 選股日誌（2026-07-26，方向 B）——量測「你的判斷有沒有在排名之上加分」。

回測會被過擬合污染，唯一乾淨的驗證是**向前記錄真實決定**。這裡記你從每日排行（或
你自己的判斷）挑出的股票 + 理由，然後持續對照三條線：
  1. 你的挑選（子集）
  2. 前20整體（若你只是無腦買整份清單會怎樣）——現成，存在 ranked_watchlist_history
  3. 0050（大盤）

若「你的挑選」報酬 > 「前20整體」→ 你的判斷（供應鏈/讀報告/大戶/避開出貨）真的加分；
若 ≈ 或 < → 你的過濾沒加分甚至扣分。這是幾個月後才有意義的長期量測，不是今天的答案。

**一律用「挑選日收盤 → 現在收盤」的 close-to-close**，三條線同一慣例才公平（這是
判斷歸因，不是實際成交損益；實際成交是隔日開盤，另在持倉頁看）。

pick_journal 純函式（summarize）不碰 DB，好測；DB 膠水另包。多使用者：全部帶 user_id。
"""
from __future__ import annotations

from datetime import date

from loguru import logger
from sqlalchemy import text

from database.connection import get_session


def ensure_pick_journal_table():
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS pick_journal (
                id              BIGSERIAL   PRIMARY KEY,
                user_id         INTEGER     NOT NULL,
                pick_date       DATE        NOT NULL,     -- 決策依據的交易日
                stock_id        VARCHAR(10) NOT NULL,
                stock_name      VARCHAR(50),
                entry_ref_price NUMERIC(12,4) NOT NULL,   -- 挑選日收盤（參考基準，非成交價）
                rank_at_pick    INTEGER,                  -- 當日在前20的排名（NULL＝榜外，純你的判斷）
                note            VARCHAR(200),             -- 你的理由/用到的邏輯
                status          VARCHAR(10) NOT NULL DEFAULT 'open',
                exit_date       DATE,
                exit_price      NUMERIC(12,4),
                created_at      TIMESTAMPTZ DEFAULT now()
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_pick_journal_user "
                       "ON pick_journal (user_id, status)"))
        s.commit()


# ── 純函式：計分彙總（好測，不碰 DB）──────────────────────────────
def summarize_scoreboard(rows: list[dict]) -> dict:
    """
    rows：每筆含 pick_ret / pool_ret / mkt_ret（同一窗的 close-to-close 報酬，None 可缺）。
    回傳彙總：各自平均、你相對前20/0050 的加分、以及「打敗」比例。

    edge_vs_pool＝在「兩者都有值」的筆數上，平均(pick_ret − pool_ret)。這是核心指標：
    >0 代表你的挑選贏過「無腦買整份前20」。
    """
    n = len(rows)
    def _avg(key, paired=None):
        vals = [r[key] for r in rows if r.get(key) is not None
                and (paired is None or r.get(paired) is not None)]
        return sum(vals) / len(vals) if vals else None

    def _edge(a, b):
        diffs = [r[a] - r[b] for r in rows if r.get(a) is not None and r.get(b) is not None]
        return (sum(diffs) / len(diffs), sum(1 for d in diffs if d > 0), len(diffs)) \
            if diffs else (None, 0, 0)

    edge_pool, win_pool, np_ = _edge("pick_ret", "pool_ret")
    edge_mkt, win_mkt, nm = _edge("pick_ret", "mkt_ret")
    return {
        "n": n,
        "avg_pick": _avg("pick_ret"),
        "avg_pool": _avg("pool_ret"),
        "avg_mkt": _avg("mkt_ret"),
        "edge_vs_pool": edge_pool, "win_vs_pool": win_pool, "n_vs_pool": np_,
        "edge_vs_mkt": edge_mkt, "win_vs_mkt": win_mkt, "n_vs_mkt": nm,
    }


# ── DB 操作 ──────────────────────────────────────────────────────
def _latest_trade_date(s):
    return s.execute(text("SELECT MAX(trade_date) FROM daily_prices")).scalar()


def add_pick(user_id: int, stock_id: str, note: str = "") -> tuple[bool, str]:
    """記一筆挑選：以最新交易日收盤為參考基準，順帶記當日在前20的排名（榜外＝NULL）。"""
    ensure_pick_journal_table()
    stock_id = str(stock_id).strip()
    try:
        with get_session() as s:
            d = _latest_trade_date(s)
            px = s.execute(text(
                "SELECT close FROM daily_prices WHERE stock_id=:sid AND trade_date=:d"
            ), {"sid": stock_id, "d": d}).scalar()
            if px is None or float(px) <= 0:
                return False, f"找不到 {stock_id} 在 {d} 的收盤價"
            name = s.execute(text(
                "SELECT stock_name FROM stocks WHERE stock_id=:sid"), {"sid": stock_id}).scalar()
            rk = s.execute(text(
                "SELECT rank FROM ranked_watchlist_history WHERE rec_date=:d AND stock_id=:sid"
            ), {"d": d, "sid": stock_id}).scalar()
            # 同 user 同 pick_date 同股票，未平倉的不重複記
            dup = s.execute(text(
                "SELECT 1 FROM pick_journal WHERE user_id=:u AND stock_id=:sid "
                "AND pick_date=:d AND status='open'"), {"u": user_id, "sid": stock_id, "d": d}).scalar()
            if dup:
                return False, f"{stock_id} 今天已經在日誌裡了"
            s.execute(text("""
                INSERT INTO pick_journal
                    (user_id, pick_date, stock_id, stock_name, entry_ref_price, rank_at_pick, note)
                VALUES (:u, :d, :sid, :nm, :px, :rk, :note)
            """), {"u": user_id, "d": d, "sid": stock_id, "nm": name,
                   "px": float(px), "rk": rk, "note": (note or "")[:200]})
            s.commit()
        return True, f"已記錄 {stock_id} {name or ''}（參考價 {float(px):.2f}"\
                     + (f"，當日排名 #{rk}）" if rk else "，榜外）")
    except Exception as e:
        logger.warning(f"add_pick 失敗: {e}")
        return False, f"記錄失敗：{e}"


def close_pick(user_id: int, pick_id: int) -> bool:
    try:
        with get_session() as s:
            d = _latest_trade_date(s)
            row = s.execute(text(
                "SELECT stock_id FROM pick_journal WHERE id=:i AND user_id=:u AND status='open'"
            ), {"i": pick_id, "u": user_id}).fetchone()
            if not row:
                return False
            px = s.execute(text(
                "SELECT close FROM daily_prices WHERE stock_id=:sid AND trade_date=:d"
            ), {"sid": row[0], "d": d}).scalar()
            s.execute(text(
                "UPDATE pick_journal SET status='closed', exit_date=:d, exit_price=:px "
                "WHERE id=:i AND user_id=:u"),
                {"d": d, "px": float(px) if px else None, "i": pick_id, "u": user_id})
            s.commit()
        return True
    except Exception as e:
        logger.warning(f"close_pick 失敗: {e}")
        return False


def _pool_return(s, pick_date, current_prices: dict) -> float | None:
    """該挑選日前20整體的 close-to-close 平均報酬（買整份清單的對照）。"""
    pool = s.execute(text(
        "SELECT stock_id FROM ranked_watchlist_history WHERE rec_date=:d"
    ), {"d": pick_date}).fetchall()
    if not pool:
        return None
    base = s.execute(text(
        "SELECT stock_id, close FROM daily_prices WHERE trade_date=:d "
        "AND stock_id = ANY(:ids)"),
        {"d": pick_date, "ids": [r[0] for r in pool]}).fetchall()
    rets = []
    for sid, b in base:
        cur = current_prices.get(sid)
        if b and float(b) > 0 and cur:
            rets.append(cur / float(b) - 1)
    return sum(rets) / len(rets) if rets else None


def list_picks_with_returns(user_id: int, status: str = "open") -> list[dict]:
    """讀該使用者的挑選，並算 pick/pool/mkt 三條線的 close-to-close 報酬。"""
    ensure_pick_journal_table()
    with get_session() as s:
        rows = s.execute(text("""
            SELECT id, pick_date, stock_id, stock_name, entry_ref_price, rank_at_pick,
                   note, status, exit_date, exit_price
            FROM pick_journal WHERE user_id=:u AND status=:st
            ORDER BY pick_date DESC, id DESC
        """), {"u": user_id, "st": status}).fetchall()
        if not rows:
            return []
        d_now = _latest_trade_date(s)
        # 現價（未平倉用最新收盤；已平倉用 exit_price）
        sids = list({r[2] for r in rows})
        cur_map = {r[0]: float(r[1]) for r in s.execute(text(
            "SELECT stock_id, close FROM daily_prices WHERE trade_date=:d AND stock_id = ANY(:ids)"
        ), {"d": d_now, "ids": sids}).fetchall() if r[1]}
        # 0050 各挑選日 + 今日
        mkt_dates = list({r[1] for r in rows}) + [d_now]
        mkt_map = {r[0]: float(r[1]) for r in s.execute(text(
            "SELECT trade_date, close FROM daily_prices WHERE stock_id='0050' AND trade_date = ANY(:ds)"
        ), {"ds": mkt_dates}).fetchall() if r[1]}
        mkt_now = mkt_map.get(d_now)
        # pool（依挑選日快取，避免重算）
        pool_cache: dict = {}
        out = []
        for r in rows:
            pid, pdate, sid, name, ref, rk, note, st_, xd, xp = r
            ref = float(ref) if ref else None
            end_px = float(xp) if (st_ == "closed" and xp) else cur_map.get(sid)
            pick_ret = (end_px / ref - 1) if ref and end_px else None
            if pdate not in pool_cache:
                pool_cache[pdate] = _pool_return(s, pdate, cur_map)
            pool_ret = pool_cache[pdate]
            mb = mkt_map.get(pdate)
            mkt_ret = (mkt_now / mb - 1) if mb and mkt_now else None
            out.append({
                "id": pid, "pick_date": pdate, "stock_id": sid, "stock_name": name or "",
                "entry_ref_price": ref, "rank_at_pick": rk, "note": note or "",
                "status": st_, "exit_date": xd, "current_price": end_px,
                "pick_ret": pick_ret, "pool_ret": pool_ret, "mkt_ret": mkt_ret,
            })
        return out


def scoreboard(user_id: int) -> dict:
    """未平倉 + 已平倉全部納入計分（判斷歸因看整體，不只看未實現）。"""
    rows = list_picks_with_returns(user_id, "open") + list_picks_with_returns(user_id, "closed")
    return summarize_scoreboard(rows)
