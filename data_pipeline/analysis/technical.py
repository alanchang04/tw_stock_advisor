"""
data_pipeline/analysis/technical.py

從 daily_prices 計算技術指標並寫入 technical_indicators 表
使用 ta==0.5.25（FinMind 相依版本）的 API
"""
import pandas as pd
import ta
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session


def load_prices(stock_id: str, min_rows: int = 30) -> pd.DataFrame:
    with get_session() as session:
        result = session.execute(text("""
            SELECT trade_date, open, high, low, close, volume
            FROM daily_prices
            WHERE stock_id = :sid
            ORDER BY trade_date ASC
        """), {"sid": stock_id})
        rows = result.fetchall()
        cols = list(result.keys())

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=cols)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.reset_index(drop=True) if len(df) >= min_rows else pd.DataFrame()


def _calc_kd(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9) -> tuple[pd.Series, pd.Series]:
    """
    台灣標準 KD（隨機指標）
      RSV = (Close - N日最低) / (N日最高 - N日最低) × 100
      K   = K_prev × 2/3 + RSV × 1/3   (EWM alpha=1/3 → com=2)
      D   = D_prev × 2/3 + K   × 1/3
    """
    lowest  = low.rolling(n).min()
    highest = high.rolling(n).max()
    rsv = 100.0 * (close - lowest) / (highest - lowest + 1e-9)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d


def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]

    # ── 均線（ta 0.5.25 用 n 不用 window）──────────────────────
    df["ma5"]   = ta.trend.sma_indicator(close, n=5)
    df["ma10"]  = ta.trend.sma_indicator(close, n=10)
    df["ma20"]  = ta.trend.sma_indicator(close, n=20)
    df["ma60"]  = ta.trend.sma_indicator(close, n=60)
    df["ma120"] = ta.trend.sma_indicator(close, n=120)
    df["ma240"] = ta.trend.sma_indicator(close, n=240)

    # ── RSI ────────────────────────────────────────────────────
    df["rsi14"] = ta.momentum.rsi(close, n=14)

    # ── MACD ───────────────────────────────────────────────────
    df["macd"]        = ta.trend.macd(close, n_slow=26, n_fast=12)
    df["macd_signal"] = ta.trend.macd_signal(close, n_slow=26, n_fast=12, n_sign=9)
    df["macd_hist"]   = ta.trend.macd_diff(close, n_slow=26, n_fast=12, n_sign=9)

    # ── 布林通道 ────────────────────────────────────────────────
    df["bb_upper"]  = ta.volatility.bollinger_hband(close, n=20, ndev=2)
    df["bb_middle"] = ta.volatility.bollinger_mavg(close, n=20)
    df["bb_lower"]  = ta.volatility.bollinger_lband(close, n=20, ndev=2)

    # ── KD 隨機指標 ─────────────────────────────────────────────
    df["k_value"], df["d_value"] = _calc_kd(high, low, close)

    # ── 訊號偵測 ────────────────────────────────────────────────
    # MA5/MA20 黃金交叉(1) / 死亡交叉(-1)
    ma5, ma20 = df["ma5"], df["ma20"]
    signal_cross = pd.Series(0, index=df.index)
    signal_cross[(ma5 > ma20) & (ma5.shift(1) <= ma20.shift(1))] =  1
    signal_cross[(ma5 < ma20) & (ma5.shift(1) >= ma20.shift(1))] = -1
    df["signal_ma_cross"] = signal_cross

    # 突破近 20 日高點(1) / 跌破近 20 日低點(-1)
    rolling_high = high.rolling(20).max().shift(1)
    rolling_low  = low.rolling(20).min().shift(1)
    signal_break = pd.Series(0, index=df.index)
    signal_break[close > rolling_high] =  1
    signal_break[close < rolling_low]  = -1
    df["signal_breakout"] = signal_break

    return df


def upsert_indicators(stock_id: str, df: pd.DataFrame, recent_days: int = None):
    df_valid = df.dropna(subset=["ma20"]).copy()
    if df_valid.empty:
        return

    # 增量模式：指標用完整歷史算（MA 才正確），但只寫回最近 recent_days 筆，
    # 避免每天重寫整段歷史（~40 萬筆 upsert）造成耗時與高 DB 負載
    if recent_days:
        df_valid = df_valid.tail(recent_days)

    df_valid = df_valid.where(pd.notnull(df_valid), None)

    cols = [
        "ma5","ma10","ma20","ma60","ma120","ma240",
        "rsi14","macd","macd_signal","macd_hist",
        "bb_upper","bb_middle","bb_lower",
        "signal_ma_cross","signal_breakout",
        "k_value","d_value",
    ]

    with get_session() as session:
        for _, row in df_valid.iterrows():
            session.execute(text("""
                INSERT INTO technical_indicators
                    (stock_id, trade_date,
                     ma5, ma10, ma20, ma60, ma120, ma240,
                     rsi14, macd, macd_signal, macd_hist,
                     bb_upper, bb_middle, bb_lower,
                     signal_ma_cross, signal_breakout,
                     k_value, d_value)
                VALUES
                    (:stock_id, :trade_date,
                     :ma5, :ma10, :ma20, :ma60, :ma120, :ma240,
                     :rsi14, :macd, :macd_signal, :macd_hist,
                     :bb_upper, :bb_middle, :bb_lower,
                     :signal_ma_cross, :signal_breakout,
                     :k_value, :d_value)
                ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                    ma5             = EXCLUDED.ma5,
                    ma10            = EXCLUDED.ma10,
                    ma20            = EXCLUDED.ma20,
                    ma60            = EXCLUDED.ma60,
                    rsi14           = EXCLUDED.rsi14,
                    macd            = EXCLUDED.macd,
                    macd_signal     = EXCLUDED.macd_signal,
                    macd_hist       = EXCLUDED.macd_hist,
                    bb_upper        = EXCLUDED.bb_upper,
                    bb_middle       = EXCLUDED.bb_middle,
                    bb_lower        = EXCLUDED.bb_lower,
                    signal_ma_cross = EXCLUDED.signal_ma_cross,
                    signal_breakout = EXCLUDED.signal_breakout,
                    k_value         = EXCLUDED.k_value,
                    d_value         = EXCLUDED.d_value
            """), {
                "stock_id":   stock_id,
                "trade_date": row["trade_date"].date(),
                **{c: row.get(c) for c in cols},
            })


def run_technical_analysis(stock_ids: list = None, recent_days: int = None):
    """
    recent_days=None：重算並寫回完整歷史（首次建置 / --mode technical 用）
    recent_days=N   ：只寫回最近 N 筆（每日增量）

    效能優化：一次讀入所有股票的歷史價格（1 次 SELECT 取代 ~2000 次），
    純記憶體計算後再用 executemany 批次寫入（1 次取代 ~10000 次 INSERT）。
    """
    mode = "完整" if not recent_days else f"增量(最近{recent_days}日)"
    logger.info(f"=== 開始計算技術指標（{mode}）===")

    # ── Step 1：一次讀入全部需要的價格（1 次 DB 請求）────────────
    with get_session() as session:
        if stock_ids is not None:
            result = session.execute(text("""
                SELECT stock_id, trade_date, open, high, low, close, volume
                FROM daily_prices
                WHERE stock_id = ANY(:ids)
                ORDER BY stock_id, trade_date ASC
            """), {"ids": list(stock_ids)})
        else:
            result = session.execute(text("""
                SELECT stock_id, trade_date, open, high, low, close, volume
                FROM daily_prices
                ORDER BY stock_id, trade_date ASC
            """))
        all_rows = result.fetchall()
        col_names = list(result.keys())

    if not all_rows:
        logger.warning("daily_prices 無資料")
        return

    all_prices = pd.DataFrame(all_rows, columns=col_names)
    all_prices["trade_date"] = pd.to_datetime(all_prices["trade_date"])
    for col in ["open", "high", "low", "close", "volume"]:
        all_prices[col] = pd.to_numeric(all_prices[col], errors="coerce")

    # ── Step 2：按股票分組計算指標（純記憶體，無 DB 請求）────────
    indicator_cols = [
        "ma5","ma10","ma20","ma60","ma120","ma240",
        "rsi14","macd","macd_signal","macd_hist",
        "bb_upper","bb_middle","bb_lower",
        "signal_ma_cross","signal_breakout",
        "k_value","d_value",
    ]
    indicator_rows = []
    success, skipped = 0, 0
    groups = list(all_prices.groupby("stock_id"))
    total = len(groups)

    for i, (sid, df) in enumerate(groups, 1):
        if i % 200 == 0:
            logger.info(f"  技術指標進度 {i}/{total}")
        df = df.reset_index(drop=True)
        if len(df) < 30:
            skipped += 1
            continue
        try:
            df = calc_indicators(df)
            df_valid = df.dropna(subset=["ma20"]).copy()
            if df_valid.empty:
                skipped += 1
                continue
            if recent_days:
                df_valid = df_valid.tail(recent_days)
            df_valid = df_valid.where(pd.notnull(df_valid), None)
            for _, row in df_valid.iterrows():
                indicator_rows.append({
                    "stock_id":   sid,
                    "trade_date": row["trade_date"].date(),
                    **{c: row.get(c) for c in indicator_cols},
                })
            success += 1
        except Exception as e:
            logger.error(f"  ❌ {sid} 失敗: {e}")
            skipped += 1

    logger.info(f"  計算完成：{success} 支成功，{skipped} 支跳過，共 {len(indicator_rows)} 筆待寫入")

    # ── Step 3：批次寫入（1 次 executemany 取代 ~10000 次 INSERT）─
    if indicator_rows:
        upsert_sql = text("""
            INSERT INTO technical_indicators
                (stock_id, trade_date,
                 ma5, ma10, ma20, ma60, ma120, ma240,
                 rsi14, macd, macd_signal, macd_hist,
                 bb_upper, bb_middle, bb_lower,
                 signal_ma_cross, signal_breakout,
                 k_value, d_value)
            VALUES
                (:stock_id, :trade_date,
                 :ma5, :ma10, :ma20, :ma60, :ma120, :ma240,
                 :rsi14, :macd, :macd_signal, :macd_hist,
                 :bb_upper, :bb_middle, :bb_lower,
                 :signal_ma_cross, :signal_breakout,
                 :k_value, :d_value)
            ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                ma5             = EXCLUDED.ma5,
                ma10            = EXCLUDED.ma10,
                ma20            = EXCLUDED.ma20,
                ma60            = EXCLUDED.ma60,
                ma120           = EXCLUDED.ma120,
                ma240           = EXCLUDED.ma240,
                rsi14           = EXCLUDED.rsi14,
                macd            = EXCLUDED.macd,
                macd_signal     = EXCLUDED.macd_signal,
                macd_hist       = EXCLUDED.macd_hist,
                bb_upper        = EXCLUDED.bb_upper,
                bb_middle       = EXCLUDED.bb_middle,
                bb_lower        = EXCLUDED.bb_lower,
                signal_ma_cross = EXCLUDED.signal_ma_cross,
                signal_breakout = EXCLUDED.signal_breakout,
                k_value         = EXCLUDED.k_value,
                d_value         = EXCLUDED.d_value
        """)
        with get_session() as session:
            session.execute(upsert_sql, indicator_rows)
        logger.info(f"  批次寫入 {len(indicator_rows)} 筆指標完成")

    logger.info(f"=== 技術指標計算完成：成功 {success}，跳過 {skipped} ===")


if __name__ == "__main__":
    run_technical_analysis()