"""
data_pipeline/fetchers/etf_fetcher.py

ETF 持股追蹤與換股偵測
  - 使用 FinMind REST API v4 抓各 ETF 最新持股（不依賴 SDK 方法名稱）
  - 比對前次快照，記錄 added / removed / increased / decreased
  - 結果寫入 etf_holdings + etf_changes + market_signals
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date, timedelta
import pandas as pd
from loguru import logger
from sqlalchemy import text

from config.settings import APIConfig
from database.connection import get_session


# ── 自動建立所需資料表（首次執行時）──────────────────────────────
def _ensure_tables():
    migration_sql = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "database", "migrations", "03_market_signals.sql"
    )
    if not os.path.exists(migration_sql):
        return
    with get_session() as session:
        sql = open(migration_sql, encoding="utf-8").read()
        try:
            session.execute(text(sql))
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning(f"migration warning: {e}")


# ── FinMind REST API v4（直接呼叫，不依賴 SDK 方法名稱）─────────
_FINMIND_API = "https://api.finmindtrade.com/api/v4/data"

def _finmind_get(dataset: str, data_id: str, start_date: str) -> list[dict]:
    """
    呼叫 FinMind v4 REST API，回傳 data list。
    避免依賴 DataLoader 的動態方法名稱（各版本不同）。
    """
    import requests as _req
    params = {
        "dataset":    dataset,
        "data_id":    data_id,
        "start_date": start_date,
        "token":      (APIConfig.FINMIND_TOKEN or "").strip(),
    }
    resp = _req.get(_FINMIND_API, params=params, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise ValueError(f"status={body.get('status')}: {body.get('msg', '')}")
    return body.get("data", [])


# ── 從 DB 取得某 ETF 最後一次快照的持股 ──────────────────────────
def _get_last_snapshot(session, etf_id: str) -> dict[str, float]:
    """回傳 {stock_id: weight_pct}"""
    result = session.execute(text("""
        SELECT stock_id, weight_pct
        FROM etf_holdings
        WHERE etf_id = :etf_id
          AND snapshot_date = (
              SELECT MAX(snapshot_date) FROM etf_holdings WHERE etf_id = :etf_id
          )
    """), {"etf_id": etf_id})
    return {r[0]: float(r[1]) if r[1] is not None else 0.0 for r in result.fetchall()}


# ── 抓取某 ETF 今日持股 ───────────────────────────────────────────
def fetch_etf_holdings(etf_id: str, lookback_days: int = 7) -> pd.DataFrame:
    """
    回傳最近 lookback_days 內最新一筆的持股 DataFrame。
    欄位：stock_id, stock_name, weight_pct, snapshot_date

    FinMind TaiwanETFHolding 回傳欄位（v4 REST）：
      date / stock_id (ETF本身) / hold_stock_id / hold_stock_name /
      hold_stock_volume / hold_stock_weight
    """
    start = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        records = _finmind_get("TaiwanETFHolding", etf_id, start)
        if not records:
            logger.warning(f"  {etf_id}：FinMind 無資料（可能不支援此 ETF）")
            return pd.DataFrame()

        df = pd.DataFrame(records)

        # TaiwanETFHolding 欄位 → 統一命名
        rename = {}
        for c in df.columns:
            lc = c.lower()
            if lc == "hold_stock_id":
                rename[c] = "stock_id"
            elif lc == "hold_stock_name":
                rename[c] = "stock_name"
            elif lc == "hold_stock_weight":
                rename[c] = "weight_pct"
            elif lc == "date":
                rename[c] = "snapshot_date"
        df = df.rename(columns=rename)

        if "stock_id" not in df.columns:
            logger.warning(f"  {etf_id}：找不到成分股欄位，現有欄位：{df.columns.tolist()}")
            return pd.DataFrame()
        if "weight_pct" not in df.columns:
            df["weight_pct"] = None
        if "stock_name" not in df.columns:
            df["stock_name"] = None

        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
        latest = df["snapshot_date"].max()
        df = df[df["snapshot_date"] == latest].copy()
        return df[["stock_id", "stock_name", "weight_pct", "snapshot_date"]]

    except Exception as e:
        logger.error(f"  {etf_id} FinMind 抓取失敗: {e}")
        return pd.DataFrame()


# ── 偵測換股並寫入 DB ─────────────────────────────────────────────
def detect_and_save_changes(etf_id: str, etf_name: str, df_new: pd.DataFrame) -> list[dict]:
    """
    比對新快照與 DB 中上次快照，找出換股，寫入 etf_changes + market_signals。
    回傳異動清單。
    """
    if df_new.empty:
        return []

    snapshot_date = df_new["snapshot_date"].iloc[0]
    new_holdings = {
        row["stock_id"]: {
            "weight": float(row["weight_pct"]) if pd.notna(row.get("weight_pct")) else 0.0,
            "name":   row.get("stock_name") or row["stock_id"],
        }
        for _, row in df_new.iterrows()
    }

    with get_session() as session:
        exists = session.execute(text("""
            SELECT 1 FROM etf_holdings
            WHERE etf_id = :etf_id AND snapshot_date = :dt LIMIT 1
        """), {"etf_id": etf_id, "dt": snapshot_date}).fetchone()

        if exists:
            logger.info(f"  {etf_id}：{snapshot_date} 快照已存在，跳過")
            return []

        last = _get_last_snapshot(session, etf_id)

        rows = [
            {"etf_id": etf_id, "stock_id": sid,
             "stock_name": info["name"],
             "weight_pct": info["weight"],
             "snapshot_date": snapshot_date}
            for sid, info in new_holdings.items()
        ]
        session.execute(text("""
            INSERT INTO etf_holdings (etf_id, stock_id, stock_name, weight_pct, snapshot_date)
            VALUES (:etf_id, :stock_id, :stock_name, :weight_pct, :snapshot_date)
            ON CONFLICT (etf_id, stock_id, snapshot_date) DO NOTHING
        """), rows)

        if not last:
            logger.info(f"  {etf_id}：首次快照，共 {len(new_holdings)} 支成分股")
            return []

        changes = []
        prev_set = set(last.keys())
        curr_set = set(new_holdings.keys())

        for sid in curr_set - prev_set:
            changes.append({"type": "added",    "stock_id": sid,
                            "name": new_holdings[sid]["name"],
                            "old": 0.0, "new": new_holdings[sid]["weight"]})
        for sid in prev_set - curr_set:
            changes.append({"type": "removed",  "stock_id": sid,
                            "name": sid, "old": last[sid], "new": 0.0})
        for sid in curr_set & prev_set:
            diff = new_holdings[sid]["weight"] - last[sid]
            if abs(diff) >= 0.5:
                changes.append({"type": "increased" if diff > 0 else "decreased",
                                "stock_id": sid, "name": new_holdings[sid]["name"],
                                "old": last[sid], "new": new_holdings[sid]["weight"]})

        if not changes:
            logger.info(f"  {etf_id}：{snapshot_date} 無持股異動")
            return []

        change_rows = [{
            "etf_id":        etf_id,
            "etf_name":      etf_name,
            "stock_id":      c["stock_id"],
            "stock_name":    c["name"],
            "change_type":   c["type"],
            "old_weight":    c["old"],
            "new_weight":    c["new"],
            "detected_date": snapshot_date,
        } for c in changes]

        session.execute(text("""
            INSERT INTO etf_changes
                (etf_id, etf_name, stock_id, stock_name,
                 change_type, old_weight, new_weight, detected_date)
            VALUES
                (:etf_id, :etf_name, :stock_id, :stock_name,
                 :change_type, :old_weight, :new_weight, :detected_date)
        """), change_rows)

        type_labels = {
            "added":     "新增",
            "removed":   "移除",
            "increased": "加碼",
            "decreased": "減碼",
        }
        signal_rows = [{
            "signal_type":    "etf_change",
            "source":         etf_name,
            "title":          f"【{etf_name}】{type_labels.get(c['type'], c['type'])} {c['name']}（{c['stock_id']}）",
            "summary":        f"持股從 {c['old']:.2f}% -> {c['new']:.2f}%",
            "url":            None,
            "related_stocks": [c["stock_id"]],
            "sentiment":      "positive" if c["type"] in ("added", "increased") else "negative",
            "signal_date":    snapshot_date,
        } for c in changes]

        session.execute(text("""
            INSERT INTO market_signals
                (signal_type, source, title, summary, related_stocks, sentiment, signal_date)
            VALUES
                (:signal_type, :source, :title, :summary, :related_stocks, :sentiment, :signal_date)
        """), signal_rows)

        logger.info(f"  {etf_id}：偵測到 {len(changes)} 筆換股異動")
        return changes


# ── 主入口：跑全部追蹤名單 ───────────────────────────────────────
def run_etf_tracking():
    """從 DB etf_watchlist 讀取追蹤清單，逐一偵測換股"""
    _ensure_tables()

    with get_session() as session:
        result = session.execute(text("""
            SELECT etf_id, etf_name, etf_type
            FROM etf_watchlist
            WHERE is_active = TRUE
            ORDER BY etf_type, etf_id
        """))
        etfs = result.fetchall()

    if not etfs:
        logger.warning("etf_watchlist 無追蹤清單")
        return

    logger.info(f"=== ETF 換股偵測：共 {len(etfs)} 支 ETF ===")
    all_changes = []

    for etf_id, etf_name, etf_type in etfs:
        logger.info(f"  [{etf_type}] {etf_id} {etf_name}")
        df = fetch_etf_holdings(etf_id)
        if df.empty:
            continue
        changes = detect_and_save_changes(etf_id, etf_name, df)
        all_changes.extend([(etf_id, etf_name, c) for c in changes])

    if all_changes:
        logger.info(f"=== ETF 換股偵測完成：共發現 {len(all_changes)} 筆異動 ===")
        for etf_id, etf_name, c in all_changes:
            logger.info(f"  {etf_name}({etf_id})：{c['type']} {c['name']}({c['stock_id']})")
    else:
        logger.info("=== ETF 換股偵測完成：今日無異動 ===")

    return all_changes
