"""
data_pipeline/analysis/smart_money.py

聰明資金追蹤：投信連買 × 統一主動式ETF換股
- 投信（信託投資公司）連續或大量買超 → 主力建倉訊號
- 統一旗下主動式 ETF（00981A 主動統一台股增長、00403A 主動統一升級50）
  新增/加碼成分股 → 統一集團操盤手看好（每日全持股，見 uni_etf_fetcher.py）
- 兩者同時發生 = 雙重確認，波段勝率最高
結果存入 market_signals (signal_type='smart_money')，供 app 顯示與 Telegram 推播
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date
from loguru import logger
from sqlalchemy import text

from database.connection import get_session
from config.settings import tw_today
from data_pipeline.fetchers.uni_etf_fetcher import UNI_ACTIVE_FUNDS

# ── 可調參數 ──────────────────────────────────────────────────────
# 注意：institutional_trading.invest_net 單位為「股」，門檻以張(=1000股)表示，比較時 ×1000
TRACK_ETFS = list(UNI_ACTIVE_FUNDS.keys())   # ["00981A", "00403A"]——統一旗下追蹤的主動式 ETF
MIN_BUY_DAYS   = 3     # 投信至少連買幾天（在 LOOKBACK_DAYS 內）
MIN_SINGLE_BUY = 500   # 單日大量門檻（張）—— 達到此值即使只買 1 天也入選
# 2026-07-08 實測：「連買天數」門檻原本沒有量體下限，319 檔通過門檻的股票裡
# 177 檔（55%）單日最小買超不到10張，極端案例(帝寶/全科/志聖/萬潤/均華)3天
# 總共只買3張——純雜訊卻被當成主力訊號。加這個下限只擋掉這種假訊號，
# 不影響真訊號（兆豐金等日均數千~數萬張的案例遠高於此門檻）。
MIN_TOTAL_LOTS = 100   # 連買天數門檻分支另外要求：累計買超 ≥ 此值（張）才算數
LOOKBACK_DAYS  = 15    # 投信買超回看自然日數
ETF_LOOKBACK   = 45    # ETF 換股回看天數（主動型可能任意日換股）
_SHARES_PER_LOT = 1000  # 1 張 = 1000 股


# ── 查詢函式 ──────────────────────────────────────────────────────
def _invest_buying(session) -> list[dict]:
    """
    回傳近 LOOKBACK_DAYS 日投信淨買超的股票。
    條件：買超天數 >= MIN_BUY_DAYS，或單日買超 >= MIN_SINGLE_BUY 張。
    """
    rows = session.execute(text("""
        WITH window_ AS (
            SELECT it.stock_id,
                   it.trade_date,
                   it.invest_net,
                   s.stock_name
            FROM institutional_trading it
            JOIN stocks s ON it.stock_id = s.stock_id
            WHERE it.trade_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        )
        SELECT
            stock_id,
            MAX(stock_name)                                                  AS stock_name,
            COUNT(*)  FILTER (WHERE invest_net > 0)                          AS buy_days,
            COUNT(*)  FILTER (WHERE invest_net < 0)                          AS sell_days,
            -- invest_net 單位為股，÷1000 轉成張
            ROUND(SUM(invest_net) FILTER (WHERE invest_net > 0) / 1000.0)    AS total_bought,
            ROUND(SUM(invest_net) / 1000.0)                                  AS net_lots,
            ROUND(MAX(invest_net) / 1000.0)                                  AS peak_day,
            MAX(trade_date) FILTER (WHERE invest_net > 0)                    AS last_buy_date
        FROM window_
        GROUP BY stock_id
        HAVING
            -- 2026-07-22 修真bug：原本只看「買超天數/正日累計」，沒有要求投信在這段
            -- 期間是「淨買」——實測全市場268檔命中裡有73檔(27%)其實是淨賣，最誇張的
            -- 群創(3481)顯示「投信買超6天1539張」但實際淨賣75811張，國巨(2327)也是
            -- 「投信買超10天」但淨賣14322張。這種散落幾天綠K的淨賣股被貼上「主力建倉」
            -- 是嚴重誤導。加 SUM(invest_net) > 0 要求整段淨買，才符合「聰明資金建倉」原意。
            SUM(invest_net) > 0
            AND (
                (COUNT(*) FILTER (WHERE invest_net > 0) >= :min_days
                 AND SUM(invest_net) FILTER (WHERE invest_net > 0) >= :min_total_shares)
                OR MAX(invest_net) >= :min_single_shares
            )
        ORDER BY buy_days DESC, total_bought DESC NULLS LAST
        LIMIT 60
    """), {
        "days":              LOOKBACK_DAYS,
        "min_days":          MIN_BUY_DAYS,
        # 門檻先在 Python 端算好（張→股）再綁單一參數：兩個小整數參數相乘
        # 會被 psycopg3 推斷成 smallint，Postgres 用原生 int2*int2 相乘導致溢位
        # （500*1000=500,000 > smallint 上限 32767）。
        "min_single_shares": MIN_SINGLE_BUY * _SHARES_PER_LOT,
        "min_total_shares":  MIN_TOTAL_LOTS * _SHARES_PER_LOT,
    }).fetchall()

    return [dict(r._mapping) for r in rows]


def _etf_additions(session, etf_id: str) -> list[dict]:
    """回傳指定 ETF 近 ETF_LOOKBACK 日新增或加碼的成分股（含 etf_id/etf_name 供多檔彙整用）。"""
    rows = session.execute(text("""
        SELECT
            ec.stock_id,
            ec.stock_name,
            ec.change_type,
            ec.old_weight,
            ec.new_weight,
            ec.detected_date,
            ec.etf_id,
            ec.etf_name
        FROM etf_changes ec
        WHERE ec.etf_id    = :etf_id
          AND ec.change_type IN ('added', 'increased')
          AND ec.detected_date >= CURRENT_DATE - :days * INTERVAL '1 day'
        ORDER BY ec.detected_date DESC,
                 (COALESCE(ec.new_weight,0) - COALESCE(ec.old_weight,0)) DESC
        LIMIT 30
    """), {"etf_id": etf_id, "days": ETF_LOOKBACK}).fetchall()

    return [dict(r._mapping) for r in rows]


def get_todays_highlights(limit: int = 8, target_date: date = None,
                          gold_cross_only: bool = False) -> str | None:
    """
    今日聰明資金重點，供 Telegram 每日報告與 /smartmoney 指令共用。
    不管訊號是這次 pipeline 剛算出來還是先前已存在，都能撈到（讀 DB，非重算）。

    gold_cross_only=True：只回傳「雙重確認」（投信連買×統一ETF加碼同時成立）。
    每日推播用這個模式——純投信買超清單資訊密度低且天天都有一堆，不值得每天推播
    佔版面；純統一ETF加碼也已經在「🔀 ETF換股偵測」區塊出現過，重複列在這裡只是
    洗版。要看完整清單改用 /smartmoney 指令隨時查。
    """
    if target_date is None:
        target_date = tw_today()
    source_filter = "AND source = '雙重確認'" if gold_cross_only else ""
    with get_session() as session:
        rows = session.execute(text(f"""
            SELECT title FROM market_signals
            WHERE signal_type = 'smart_money' AND signal_date = :dt
            {source_filter}
            ORDER BY CASE source
                WHEN '雙重確認' THEN 0
                WHEN '統一ETF'  THEN 1
                ELSE 2
            END, id
            LIMIT :n
        """), {"dt": target_date, "n": limit}).fetchall()
    if not rows:
        return None
    return "\n".join(f"• {r[0]}" for r in rows)


def _all_etf_additions(session) -> list[dict]:
    """彙整 TRACK_ETFS 全部 ETF 的加碼/新增（同一股票被多檔 ETF 選中時，取最新一筆）。"""
    by_stock: dict[str, dict] = {}
    for etf_id in TRACK_ETFS:
        for row in _etf_additions(session, etf_id):
            sid = row["stock_id"]
            if sid not in by_stock or row["detected_date"] > by_stock[sid]["detected_date"]:
                by_stock[sid] = row
    return list(by_stock.values())


# ── 主入口 ────────────────────────────────────────────────────────
def run_smart_money_analysis() -> int:
    """
    計算聰明資金訊號並寫入 market_signals。
    已有今日訊號則跳過（idempotent）。
    回傳新增訊號數。
    """
    today = tw_today()
    logger.info("=== 聰明資金分析開始 ===")

    with get_session() as session:
        existing = session.execute(text("""
            SELECT COUNT(*) FROM market_signals
            WHERE signal_type = 'smart_money' AND signal_date = :dt
        """), {"dt": today}).scalar()

    if existing > 0:
        logger.info("  今日聰明資金訊號已存在，跳過")
        return 0

    with get_session() as session:
        invest_list = _invest_buying(session)
        etf_list    = _all_etf_additions(session)

    if not invest_list and not etf_list:
        logger.info("  今日無聰明資金訊號（institutional_trading / etf_changes 資料可能尚未更新）")
        return 0

    etf_ids     = {r["stock_id"] for r in etf_list}
    invest_ids  = {r["stock_id"] for r in invest_list}
    overlap_ids = etf_ids & invest_ids

    logger.info(f"  投信買超: {len(invest_list)} 支 | ETF 加碼: {len(etf_list)} 支 | 重疊: {len(overlap_ids)} 支")

    rows_to_save = []

    # ── 雙重確認（投信 + 統一ETF）──────────────────────────────
    for inv in invest_list:
        if inv["stock_id"] not in overlap_ids:
            continue
        etf = next(e for e in etf_list if e["stock_id"] == inv["stock_id"])
        action = "新納入" if etf["change_type"] == "added" else "加碼"
        weight_str = (f"{etf['old_weight'] or 0:.2f}% → {etf['new_weight'] or 0:.2f}%"
                      if etf.get("new_weight") else "")
        rows_to_save.append({
            "source":  "雙重確認",
            "title":   (f"⭐【雙確認】{inv['stock_id']} {inv['stock_name']} "
                        f"— 投信連買{inv['buy_days']}日 + {etf['etf_name']}{action}"),
            "summary": (f"投信近{LOOKBACK_DAYS}日買超 {inv['buy_days']} 天，"
                        f"淨買 {(inv.get('net_lots') or 0):,.0f} 張（正日累計 {(inv['total_bought'] or 0):,.0f} 張，"
                        f"峰值 {(inv['peak_day'] or 0):,.0f} 張）；"
                        f"{etf['etf_name']}({etf['etf_id']}) {action} {etf['detected_date']}"
                        + (f"，權重 {weight_str}" if weight_str else "")),
            "stocks":  [inv["stock_id"]],
        })

    # ── 純投信買超 ────────────────────────────────────────────
    for inv in invest_list:
        if inv["stock_id"] in overlap_ids:
            continue
        rows_to_save.append({
            "source":  "投信買超",
            "title":   (f"【投信】{inv['stock_id']} {inv['stock_name']} "
                        f"— 近{LOOKBACK_DAYS}日買超 {inv['buy_days']} 天"),
            "summary": (f"近{LOOKBACK_DAYS}日淨買 {(inv.get('net_lots') or 0):,.0f} 張"
                        f"（正日累計 {(inv['total_bought'] or 0):,.0f} 張，單日峰值 {(inv['peak_day'] or 0):,.0f} 張），"
                        f"最後買超日 {inv['last_buy_date']}"),
            "stocks":  [inv["stock_id"]],
        })

    # ── 純統一ETF換股 ─────────────────────────────────────────
    for etf in etf_list:
        if etf["stock_id"] in invest_ids:
            continue
        action = "新納入成分股" if etf["change_type"] == "added" else "加碼增持"
        weight_str = (f"{etf['old_weight'] or 0:.2f}% → {etf['new_weight'] or 0:.2f}%"
                      if etf.get("new_weight") else "")
        rows_to_save.append({
            "source":  "統一ETF",
            "title":   (f"【{etf['etf_name']}】{etf['stock_id']} {etf['stock_name']} "
                        f"— {action}（{etf['detected_date']}）"),
            "summary": (f"{etf['etf_name']}({etf['etf_id']}) {action}"
                        + (f"，權重 {weight_str}" if weight_str else "")),
            "stocks":  [etf["stock_id"]],
        })

    saved = 0
    with get_session() as session:
        for r in rows_to_save:
            session.execute(text("""
                INSERT INTO market_signals
                    (signal_type, source, title, summary,
                     related_stocks, sentiment, signal_date)
                VALUES
                    ('smart_money', :source, :title, :summary,
                     :stocks, 'positive', :dt)
                ON CONFLICT DO NOTHING
            """), {**r, "dt": today})
            saved += 1

    logger.info(f"=== 聰明資金分析完成：新增 {saved} 個訊號 ===")
    return saved
