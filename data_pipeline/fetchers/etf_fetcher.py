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

import re
from datetime import date
import requests
import pandas as pd
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import text

from database.connection import get_session

# MoneyDJ ETF 持股頁（免費、含代號/權重/資料日期；免付費、免瀏覽器）
# 注意：MoneyDJ 資料來自基金月報，約每月更新，僅前 10 大持股。
#       每日即時的主動買賣訊號請看「投信買超」（institutional_trading）。
_MONEYDJ_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007.xdjhtm?etfid={etf_id}.TW"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── 自動建立所需資料表（首次執行時）──────────────────────────────
def _ensure_tables():
    """表已存在（正常情況）就直接返回，不重放整份 migration。"""
    with get_session() as session:
        exists = session.execute(text("""
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'etf_watchlist' LIMIT 1
        """)).fetchone()
    if exists:
        return

    logger.warning("etf_watchlist 不存在，執行 migration 03（首次建置）")
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


# ── 抓取某 ETF 持股（MoneyDJ）─────────────────────────────────────
_HOLDING_ROW_RE = re.compile(r"(.+?)\((\d{4}[A-Z]?)\.(?:TW|TWO)\)")
_DATE_RE = re.compile(r"資料日期[：:]\s*(\d{4})/(\d{1,2})/(\d{1,2})")


def fetch_etf_holdings(etf_id: str) -> pd.DataFrame:
    """
    從 MoneyDJ 抓某 ETF 的前十大持股。
    欄位：stock_id, stock_name, weight_pct, snapshot_date

    snapshot_date 取 MoneyDJ 頁面標示的「資料日期」（基金月報基準日）——
    如此 detect_and_save_changes 只在 MoneyDJ 實際更新時才偵測換股，不會每天重複。
    """
    url = _MONEYDJ_URL.format(etf_id=etf_id)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text
    except Exception as e:
        logger.error(f"  {etf_id} MoneyDJ 抓取失敗: {e}")
        return pd.DataFrame()

    # 資料日期（找不到就用今天，避免整個流程中斷）
    m = _DATE_RE.search(html)
    if m:
        snapshot_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        snapshot_date = date.today()
        logger.warning(f"  {etf_id}：MoneyDJ 找不到資料日期，改用今日")

    # 找出持股表（表頭含「個股名稱」「投資比例」）
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        head = table.get_text(" ", strip=True)
        if "個股名稱" not in head or "投資比例" not in head:
            continue
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) < 2:
                continue
            nm = _HOLDING_ROW_RE.match(cells[0])
            if not nm:
                continue
            name, code = nm.group(1).strip(), nm.group(2)
            try:
                weight = float(cells[1])
            except ValueError:
                weight = None
            rows.append({
                "stock_id":      code,
                "stock_name":    name,
                "weight_pct":    weight,
                "snapshot_date": snapshot_date,
            })
        break

    if not rows:
        logger.warning(f"  {etf_id}：MoneyDJ 未解析到持股（頁面結構可能改版）")
        return pd.DataFrame()

    return pd.DataFrame(rows)


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
            ON CONFLICT DO NOTHING
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

    # 統一主動式 ETF 走官網每日全持股（跟大戶換股的核心）；其他走 MoneyDJ 月更
    from data_pipeline.fetchers.uni_etf_fetcher import UNI_ACTIVE_FUNDS, fetch_uni_holdings

    for etf_id, etf_name, etf_type in etfs:
        src = "統一官網(每日)" if etf_id in UNI_ACTIVE_FUNDS else "MoneyDJ(月更)"
        logger.info(f"  [{etf_type}] {etf_id} {etf_name} ← {src}")
        if etf_id in UNI_ACTIVE_FUNDS:
            df = fetch_uni_holdings(etf_id)
        else:
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
