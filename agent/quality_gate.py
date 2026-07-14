"""
agent/quality_gate.py

資料品質驗證器（規格 SPEC_PIPELINE_IMPROVEMENTS.md Phase B）。

立場（規格明確定調，避免過度設計）：核心行情/法人資料的來源就是證交所官方本身，
沒有「多來源競爭選誰可信」的問題；真正需要品質管理的是我們自己的轉換管線
——歷史上踩過的坑（0050分割未還原污染市場濾網、invest_net股/張單位、
07-13並發race讀到全0）全部是這裡的規則檢查抓得到的結構性錯誤。

三條規則檢查（皆為純函式，吃 DataFrame/純量、不碰 DB，方便單元測試）：
  1. detect_price_discontinuity  單日|漲跌|>20%（遠超台股±10%限制）→ 疑似分割/資料錯
  2. detect_institutional_lag    法人資料日期落後股價 → 當日籌碼面因子不可信
  3. detect_row_count_anomaly    當日資料筆數明顯低於近期均值 → 疑似抓取不完整

觸發都寫進 discrepancy_log（累積訓練資料，現在不學習——規格明確暫緩，
觸發條件＝累積≥300筆含人工裁決才重開此議題）。近30日觸發次數 → 信心分數
（固定公式，非模型），餵進 execution_log.sources 顯示於決策軌跡頁。
"""
from __future__ import annotations

import pandas as pd
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session

PRICE_JUMP_THRESHOLD = 0.20     # 與 agent/strategy.py SPLIT_JUMP_THRESHOLD 同一門檻
ROW_COUNT_DROP_RATIO = 0.85     # 當日筆數 < 近期均值 85% 視為異常
SCORECARD_WINDOW_DAYS = 30
SCORECARD_CAP = 10              # 近30日觸發達此次數，信心分數探底至0

# 來源註冊表（靜態文件化，非資料庫；供 discrepancy 訊息與記分卡標籤引用）
SOURCE_REGISTRY = {
    "daily_prices": {
        "origin": "TWSE/TPEX OpenAPI",
        "freq": "daily",
        "known_weakness": "上櫃(TPEX)本機曾遇SSL憑證問題；除權息/股票分割未還原",
    },
    "institutional_trading": {
        "origin": "TWSE T86 / TPEX 三大法人",
        "freq": "daily",
        "known_weakness": "偶爾較股價慢一天更新（2026-07-13 並發事故實例）",
    },
    "etf_holdings": {
        "origin": "統一官網(主動式ETF) / MoneyDJ(其他ETF)",
        "freq": "統一=daily，MoneyDJ≈月更",
        "known_weakness": "MoneyDJ 月更資料可能為近似值/過期",
    },
    "monthly_revenue": {
        "origin": "公開資訊觀測站 MOPS",
        "freq": "monthly",
        "known_weakness": "月初數日可能尚未公布最新月份",
    },
}


def ensure_discrepancy_log_table():
    """冪等建表（migration 17 同內容，現有 DB 自動補上）。"""
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS discrepancy_log (
                id          BIGSERIAL    PRIMARY KEY,
                detected_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
                check_name  VARCHAR(40)  NOT NULL,
                source      VARCHAR(40)  NOT NULL,
                stock_id    VARCHAR(10),
                field       VARCHAR(40),
                expected    TEXT,
                actual      TEXT,
                severity    VARCHAR(10)  NOT NULL DEFAULT 'warn' CHECK (severity IN ('warn', 'error')),
                note        TEXT,
                resolution  VARCHAR(10)  CHECK (resolution IN ('confirmed_bad', 'false_positive', NULL))
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_discrepancy_log_time ON discrepancy_log (detected_at)"))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_discrepancy_log_source ON discrepancy_log (source, detected_at)"))


# ══════════════════════════════════════════════════════════════════
#  規則檢查（純函式：不碰 DB，輸入輸出明確，單元測試不需要資料庫）
# ══════════════════════════════════════════════════════════════════
def detect_price_discontinuity(df: pd.DataFrame, threshold: float = PRICE_JUMP_THRESHOLD) -> list[dict]:
    """
    df 需含 stock_id/trade_date/close（可多檔多日）；只檢查「最新交易日 vs 前一日」。
    台股漲跌限制 ±10%，任何超過的單日變動只可能是分割/減資/資料錯誤
    （見 0050 2025-06-18 一分四實例：未還原污染市場濾網達 2 個月）。
    """
    if df.empty:
        return []
    d = df[["stock_id", "trade_date", "close"]].dropna().sort_values(["stock_id", "trade_date"])
    d["prev_close"] = d.groupby("stock_id")["close"].shift(1)
    d["pct"] = d["close"] / d["prev_close"] - 1
    last_date = d["trade_date"].max()
    latest = d[(d["trade_date"] == last_date) & d["prev_close"].notna() & (d["prev_close"] > 0)]
    hits = latest[latest["pct"].abs() > threshold]
    return [{
        "check_name": "price_discontinuity", "source": "daily_prices",
        "stock_id": r["stock_id"], "field": "close",
        "expected": f"|單日漲跌|<={threshold*100:.0f}%（台股漲跌限制±10%）",
        "actual": f"{r['pct']*100:+.1f}%（{r['prev_close']:.2f}→{r['close']:.2f}）",
        "severity": "error",
        "note": "疑似分割/減資/資料錯誤，非正常交易可達成的單日變動",
    } for _, r in hits.iterrows()]


def detect_institutional_lag(price_max_date, inst_max_date) -> dict | None:
    """法人資料最新日期落後股價最新日期 → 當日候選股籌碼面因子不可信。"""
    if price_max_date is None or inst_max_date is None:
        return None
    if inst_max_date < price_max_date:
        return {
            "check_name": "institutional_lag", "source": "institutional_trading",
            "stock_id": None, "field": "trade_date",
            "expected": f"最新日 = {price_max_date}", "actual": f"最新日 = {inst_max_date}",
            "severity": "warn",
            "note": "法人資料落後股價，當日籌碼面因子不可信（見07-13並發事故：候選讀到全0）",
        }
    return None


def detect_row_count_anomaly(today_count: int, baseline_counts: list[int],
                             source: str = "daily_prices",
                             ratio: float = ROW_COUNT_DROP_RATIO) -> dict | None:
    """當日資料筆數明顯低於近期均值 → 疑似抓取不完整（如遇假日應排除在外再呼叫）。"""
    if not baseline_counts:
        return None
    baseline = sum(baseline_counts) / len(baseline_counts)
    if baseline <= 0 or today_count >= baseline * ratio:
        return None
    return {
        "check_name": "row_count_anomaly", "source": source,
        "stock_id": None, "field": "row_count",
        "expected": f">= {baseline*ratio:.0f}（近期均值 {baseline:.0f} 的 {ratio*100:.0f}%）",
        "actual": str(today_count),
        "severity": "warn",
        "note": "當日資料量明顯偏低，疑似抓取不完整",
    }


# ══════════════════════════════════════════════════════════════════
#  DB 查詢層：組資料 → 呼叫規則檢查 → 寫入 discrepancy_log
# ══════════════════════════════════════════════════════════════════
def run_quality_checks() -> list[dict]:
    """對目前 DB 最新資料跑三條規則檢查，觸發者寫入 discrepancy_log，回傳本次觸發清單。"""
    ensure_discrepancy_log_table()
    with get_session() as s:
        px = pd.read_sql(text("""
            SELECT stock_id, trade_date, close FROM daily_prices
            WHERE trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM daily_prices ORDER BY trade_date DESC LIMIT 2) d)
              AND close > 0
        """), s.bind)
        price_max, inst_max = s.execute(text(
            "SELECT (SELECT MAX(trade_date) FROM daily_prices),"
            "       (SELECT MAX(trade_date) FROM institutional_trading)")).fetchone()
        counts = pd.read_sql(text("""
            SELECT trade_date, COUNT(*) AS n FROM daily_prices
            WHERE trade_date >= (SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM daily_prices ORDER BY trade_date DESC LIMIT 21) d)
            GROUP BY trade_date ORDER BY trade_date
        """), s.bind)

    discs: list[dict] = []
    discs += detect_price_discontinuity(px)
    lag = detect_institutional_lag(price_max, inst_max)
    if lag:
        discs.append(lag)
    if len(counts) >= 2:
        anomaly = detect_row_count_anomaly(int(counts.iloc[-1]["n"]), counts.iloc[:-1]["n"].tolist())
        if anomaly:
            discs.append(anomaly)

    if discs:
        with get_session() as s:
            for d in discs:
                s.execute(text("""
                    INSERT INTO discrepancy_log
                        (check_name, source, stock_id, field, expected, actual, severity, note)
                    VALUES (:check_name, :source, :stock_id, :field, :expected, :actual, :severity, :note)
                """), d)
        logger.warning(f"🧪 quality_gate 觸發 {len(discs)} 項資料異常：" +
                       "、".join(f"{d['check_name']}({d.get('stock_id') or '整體'})" for d in discs))
    return discs


def source_scorecard(days: int = SCORECARD_WINDOW_DAYS, cap: int = SCORECARD_CAP) -> dict[str, float]:
    """
    近 N 日各來源觸發次數 → 信心分數 0~1（固定公式：1 - 觸發次數/cap，下限0）。
    這不是學來的模型——規格明確暫緩「模型學來源權重」，觸發條件是
    discrepancy_log 累積 ≥300 筆含人工裁決紀錄後才重開。
    """
    ensure_discrepancy_log_table()
    with get_session() as s:
        rows = s.execute(text("""
            SELECT source, COUNT(*) FROM discrepancy_log
            WHERE detected_at >= now() - make_interval(days => :d)
            GROUP BY source
        """), {"d": days}).fetchall()
    counts = {r[0]: r[1] for r in rows}
    all_sources = set(SOURCE_REGISTRY) | set(counts)
    return {src: round(max(0.0, 1 - counts.get(src, 0) / cap), 2) for src in sorted(all_sources)}
