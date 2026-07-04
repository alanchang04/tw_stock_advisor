"""
agent/stock_selector.py

從資料庫篩選出今日候選股票：
  1. 取族群輪動熱度前 N 產業
  2. 從這些產業撈出技術訊號正面的股票
  3. 加入籌碼面過濾
  4. 回傳候選股票清單供 LLM 分析
"""
import pandas as pd
from datetime import date, timedelta
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database.connection import get_session
from agent.strategy import STRATEGY, score_candidates


# 排除非個股的產業類別（ETF、指數等）
EXCLUDE_INDUSTRIES = {
    "ETF", "ETN", "上櫃ETF", "指數投資證券(ETN)",
    "上櫃指數股票型基金(ETF)", "受益憑證", "存託憑證",
}

# 技術面篩選條件（集中在 strategy.py，方便調整）
MIN_RSI    = STRATEGY["min_rsi"]
MAX_RSI    = STRATEGY["max_rsi"]
MIN_CLOSE  = STRATEGY["min_close"]
MIN_VOLUME = STRATEGY["min_volume"]


def market_is_bull() -> bool:
    """
    市場濾網：大盤代理（預設 0050）收盤 >= MA60 視為多頭。
    空頭時 daily_runner 不開新倉、出場加回死亡交叉（見 STRATEGY market_filter 區塊）。
    查無資料或未啟用時回傳 True（不阻擋）。
    """
    if not STRATEGY.get("market_filter"):
        return True
    sid = STRATEGY.get("market_filter_stock", "0050")
    try:
        with get_session() as s:
            r = s.execute(text("""
                SELECT p.close, t.ma60
                FROM daily_prices p
                JOIN technical_indicators t
                  ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
                WHERE p.stock_id = :sid AND t.ma60 IS NOT NULL
                ORDER BY p.trade_date DESC LIMIT 1
            """), {"sid": sid}).fetchone()
        if r is None:
            return True
        bull = float(r[0]) >= float(r[1])
        if not bull:
            logger.warning(f"市場濾網：{sid} 收盤 {float(r[0]):.2f} < MA60 {float(r[1]):.2f} → 空頭模式")
        return bull
    except Exception as e:
        logger.warning(f"市場濾網查詢失敗（視為多頭）: {e}")
        return True


def get_hot_sectors(top_n: int = 5, min_stocks: int = 10) -> list[str]:
    """
    取今日熱度前 N 個產業（排除 ETF 類、且族群股票數要夠）
    """
    today = date.today()
    # 往前找最近一筆有資料的日期
    with get_session() as session:
        result = session.execute(text("""
            SELECT sm.industry_code, i.name_zh,
                   sm.avg_change_pct, sm.momentum_score, sm.total_count
            FROM sector_momentum sm
            JOIN industries i ON i.code = sm.industry_code
            WHERE sm.calc_date = (
                SELECT MAX(calc_date) FROM sector_momentum
            )
            AND sm.total_count >= :min_stocks
            ORDER BY sm.momentum_score DESC
            LIMIT 20
        """), {"min_stocks": min_stocks})
        rows = result.fetchall()
        cols = list(result.keys())

    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        logger.warning("sector_momentum 沒有資料，請先跑 --mode sector")
        return []

    # 過濾 ETF 類
    df = df[~df["name_zh"].isin(EXCLUDE_INDUSTRIES)]
    df = df[~df["industry_code"].isin(EXCLUDE_INDUSTRIES)]

    hot = df.head(top_n)
    logger.info(f"熱門產業 Top {top_n}：")
    for _, row in hot.iterrows():
        logger.info(
            f"  {row['name_zh']} | 漲幅 {row['avg_change_pct']:.2f}% "
            f"| 熱度 {row['momentum_score']:.3f} | {row['total_count']} 支"
        )

    return hot["industry_code"].tolist()


def get_candidate_stocks(
    industry_codes: list[str],
    top_n: int = 20,
) -> pd.DataFrame:
    """
    從熱門產業中篩選技術面 + 籌碼面正面的候選股票
    """
    if not industry_codes:
        return pd.DataFrame()

    # 最近交易日
    recent_date = date.today() - timedelta(days=5)

    placeholders = ",".join([f"'{c}'" for c in industry_codes])

    with get_session() as session:
        result = session.execute(text(f"""
            SELECT
                s.stock_id,
                s.stock_name,
                i.name_zh        AS industry,
                p.close,
                p.change_pct,
                p.volume,
                t.ma5,
                t.ma20,
                t.ma60,
                t.rsi14,
                t.macd_hist,
                t.signal_ma_cross,
                t.signal_breakout,
                COALESCE(inst.total_net, 0)   AS inst_net,
                COALESCE(inst.foreign_net, 0) AS foreign_net
            FROM stock_industry_map m
            JOIN stocks s         ON s.stock_id = m.stock_id
            JOIN industries i     ON i.code = m.industry_code
            JOIN daily_prices p   ON p.stock_id = m.stock_id
                AND p.trade_date = (
                    SELECT MAX(trade_date) FROM daily_prices
                    WHERE stock_id = m.stock_id
                )
            JOIN technical_indicators t ON t.stock_id = m.stock_id
                AND t.trade_date = p.trade_date
            LEFT JOIN institutional_trading inst ON inst.stock_id = m.stock_id
                AND inst.trade_date = p.trade_date
            WHERE m.industry_code IN ({placeholders})
            AND p.close >= :min_close
            AND p.volume >= :min_volume
            AND t.rsi14 BETWEEN :min_rsi AND :max_rsi
            AND t.ma5 IS NOT NULL
            AND t.ma20 IS NOT NULL
        """), {
            "min_close":  MIN_CLOSE,
            "min_volume": MIN_VOLUME,
            "min_rsi":    MIN_RSI,
            "max_rsi":    MAX_RSI,
        })
        rows = result.fetchall()
        cols = list(result.keys())

    if not rows:
        logger.warning("沒有符合條件的候選股票")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=cols)

    # 60 日動能：取 60 個交易日前的收盤，算區間報酬（候選池內相對排名進評分）
    try:
        with get_session() as session:
            base_rows = session.execute(text("""
                WITH d AS (
                    SELECT DISTINCT trade_date FROM daily_prices
                    ORDER BY trade_date DESC LIMIT 61
                )
                SELECT p.stock_id, p.close
                FROM daily_prices p
                WHERE p.trade_date = (SELECT MIN(trade_date) FROM d)
                  AND p.close > 0
            """)).fetchall()
        base_map = {r[0]: float(r[1]) for r in base_rows}
        df["mom60"] = [
            (float(c) / base_map[sid] - 1) if base_map.get(sid) else None
            for sid, c in zip(df["stock_id"], df["close"])
        ]
    except Exception as e:
        logger.warning(f"動能計算失敗（略過此因子）: {e}")

    # 綜合評分：統一使用 strategy.score_candidates（與回測共用同一份邏輯）
    df["score"] = score_candidates(df)

    df = df.sort_values("score", ascending=False)
    candidates = df.head(top_n).reset_index(drop=True)

    logger.info(f"篩選出 {len(candidates)} 支候選股票")
    return candidates


def format_candidates_for_llm(df: pd.DataFrame) -> str:
    """
    把候選股票資料格式化成給 LLM 分析的文字
    """
    if df.empty:
        return ""

    lines = ["以下是今日候選股票資料，請根據這些資料推薦最值得關注的 5 支股票：\n"]

    for _, row in df.iterrows():
        ma_cross = {1: "黃金交叉", -1: "死亡交叉", 0: "無"}.get(int(row.get("signal_ma_cross", 0)), "無")
        breakout = {1: "突破壓力", -1: "跌破支撐", 0: "無"}.get(int(row.get("signal_breakout", 0)), "無")

        lines.append(
            f"【{row['stock_id']} {row['stock_name']}】產業：{row['industry']}\n"
            f"  股價：{row['close']:.1f} | 漲跌：{row.get('change_pct', 0):.2f}%\n"
            f"  MA5：{row.get('ma5', 'N/A')} | MA20：{row.get('ma20', 'N/A')} | MA60：{row.get('ma60', 'N/A')}\n"
            f"  RSI：{row.get('rsi14', 0):.1f} | MACD柱：{'正' if row.get('macd_hist', 0) > 0 else '負'}\n"
            f"  均線訊號：{ma_cross} | 突破訊號：{breakout}\n"
            f"  三大法人淨買超：{int(row.get('inst_net', 0))}張 | 外資：{int(row.get('foreign_net', 0))}張\n"
        )

    return "\n".join(lines)