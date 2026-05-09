"""
data_pipeline/fetchers/finmind_fetcher.py

透過 FinMind API 抓取：
  - 每日股價 (daily_prices)
  - 三大法人籌碼 (institutional_trading)
  - 融資融券 (margin_trading)
  - 季度財報 (financials)
  - 上市上櫃股票清單 (stocks)
"""
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from FinMind.data import DataLoader
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from config.settings import APIConfig
from database.connection import get_session


# ── 初始化 FinMind DataLoader ──────────────────────────────────
def _get_loader() -> DataLoader:
    dl = DataLoader()
    if APIConfig.FINMIND_TOKEN:
        dl.login_by_token(api_token=APIConfig.FINMIND_TOKEN)
    else:
        logger.warning("未設定 FINMIND_TOKEN，使用未登入模式（有流量限制）")
    return dl


# ── 1. 抓上市上櫃股票清單 ────────────────────────────────────────
def fetch_stock_list() -> pd.DataFrame:
    """
    回傳 DataFrame，欄位：stock_id, stock_name, market
    """
    logger.info("抓取股票清單...")
    dl = _get_loader()

    twse = dl.taiwan_stock_info()
    twse["market"] = "TWSE"

    # FinMind 同一個方法可取 TPEX，過濾 market 欄
    tpex_mask = twse["market"].str.contains("OTC|TPEX", na=False)
    twse.loc[tpex_mask, "market"] = "TPEX"
    twse.loc[~tpex_mask, "market"] = "TWSE"

    df = twse[["stock_id", "stock_name", "market"]].drop_duplicates("stock_id")

    # 只保留純數字的股票代號（過濾掉指數、ETF連結等非股票項目）
    df = df[df["stock_id"].str.match(r"^\d{4,6}$")]
    logger.info(f"取得 {len(df)} 支股票")
    return df


def upsert_stock_list(df: pd.DataFrame):
    """將股票清單寫入 stocks 資料表（upsert）"""
    with get_session() as session:
        for _, row in df.iterrows():
            session.execute(text("""
                INSERT INTO stocks (stock_id, stock_name, market)
                VALUES (:sid, :name, :market)
                ON CONFLICT (stock_id) DO UPDATE
                    SET stock_name = EXCLUDED.stock_name,
                        market     = EXCLUDED.market,
                        updated_at = NOW()
            """), {
                "sid":    row["stock_id"],
                "name":   row["stock_name"],
                "market": row["market"],
            })
    logger.info(f"✅ stocks upsert 完成：{len(df)} 筆")


# ── 2. 每日股價 ──────────────────────────────────────────────────
def fetch_daily_prices(
    stock_id: str,
    start_date: str,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    start_date / end_date 格式：'YYYY-MM-DD'
    回傳 DataFrame 含 OHLCV + change_pct
    """
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    dl = _get_loader()
    df = dl.taiwan_stock_daily(
        stock_id=stock_id,
        start_date=start_date,
        end_date=end_date,
    )
    if df.empty:
        return df

    df = df.rename(columns={
        "date":          "trade_date",
        "open":          "open",
        "max":           "high",
        "min":           "low",
        "close":         "close",
        "Trading_Volume": "volume",
        "Trading_money":  "turnover",
        "spread":        "change_pct",
    })
    # change_pct 轉成百分比
    if "change_pct" in df.columns and "close" in df.columns:
        df["change_pct"] = df["change_pct"] / df["close"].shift(1) * 100

    cols = ["stock_id","trade_date","open","high","low","close",
            "volume","turnover","change_pct"]
    return df[[c for c in cols if c in df.columns]]


def upsert_daily_prices(df: pd.DataFrame):
    if df.empty:
        return
    # 把 inf / -inf / NaN 換成 None（DB NULL），避免 numeric overflow
    df = df.replace([float('inf'), float('-inf')], pd.NA)
    df = df.where(pd.notnull(df), None)
    with get_session() as session:
        for _, row in df.iterrows():
            session.execute(text("""
                INSERT INTO daily_prices
                    (stock_id, trade_date, open, high, low, close,
                     volume, turnover, change_pct)
                VALUES
                    (:stock_id, :trade_date, :open, :high, :low, :close,
                     :volume, :turnover, :change_pct)
                ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                    open       = EXCLUDED.open,
                    high       = EXCLUDED.high,
                    low        = EXCLUDED.low,
                    close      = EXCLUDED.close,
                    volume     = EXCLUDED.volume,
                    turnover   = EXCLUDED.turnover,
                    change_pct = EXCLUDED.change_pct
            """), row.to_dict())


# ── 3. 三大法人籌碼 ──────────────────────────────────────────────
def fetch_institutional(
    stock_id: str,
    start_date: str,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    dl = _get_loader()
    df = dl.taiwan_stock_institutional_investors(
        stock_id=stock_id,
        start_date=start_date,
        end_date=end_date,
    )
    if df.empty:
        return df

    # FinMind 回傳格式是 long（每列一個法人），需要 pivot
    pivot = df.pivot_table(
        index=["stock_id", "date"],
        columns="name",
        values=["buy", "sell"],
        aggfunc="sum",
    ).reset_index()
    pivot.columns = ["_".join(c).strip("_") for c in pivot.columns]
    pivot = pivot.rename(columns={"date": "trade_date"})

    # 統一欄位名稱（FinMind 的 name 欄位值可能因版本不同）
    col_map = {
        "buy_外陸資(不含外資自營商)":  "foreign_buy",
        "sell_外陸資(不含外資自營商)": "foreign_sell",
        "buy_投信":   "invest_buy",
        "sell_投信":  "invest_sell",
        "buy_自營商": "dealer_buy",
        "sell_自營商":"dealer_sell",
    }
    pivot = pivot.rename(columns=col_map)

    for col in ["foreign_buy","foreign_sell","invest_buy",
                "invest_sell","dealer_buy","dealer_sell"]:
        if col not in pivot.columns:
            pivot[col] = 0
        pivot[col] = pivot[col].fillna(0).astype(int)

    pivot["foreign_net"] = pivot["foreign_buy"] - pivot["foreign_sell"]
    pivot["invest_net"]  = pivot["invest_buy"]  - pivot["invest_sell"]
    pivot["dealer_net"]  = pivot["dealer_buy"]  - pivot["dealer_sell"]
    pivot["total_net"]   = (pivot["foreign_net"]
                            + pivot["invest_net"]
                            + pivot["dealer_net"])
    return pivot


def upsert_institutional(df: pd.DataFrame):
    if df.empty:
        return
    cols = ["stock_id","trade_date","foreign_buy","foreign_sell","foreign_net",
            "invest_buy","invest_sell","invest_net",
            "dealer_buy","dealer_sell","dealer_net","total_net"]
    with get_session() as session:
        for _, row in df[cols].iterrows():
            session.execute(text("""
                INSERT INTO institutional_trading
                    (stock_id, trade_date,
                     foreign_buy, foreign_sell, foreign_net,
                     invest_buy,  invest_sell,  invest_net,
                     dealer_buy,  dealer_sell,  dealer_net, total_net)
                VALUES
                    (:stock_id, :trade_date,
                     :foreign_buy, :foreign_sell, :foreign_net,
                     :invest_buy,  :invest_sell,  :invest_net,
                     :dealer_buy,  :dealer_sell,  :dealer_net, :total_net)
                ON CONFLICT (stock_id, trade_date) DO UPDATE SET
                    foreign_net = EXCLUDED.foreign_net,
                    invest_net  = EXCLUDED.invest_net,
                    dealer_net  = EXCLUDED.dealer_net,
                    total_net   = EXCLUDED.total_net
            """), row.to_dict())


# ── 4. 批次抓取（多支股票）──────────────────────────────────────
def batch_fetch_prices(
    stock_ids: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    delay: float = 1.0,
):
    """
    批次抓多支股票的每日股價並寫入 DB
    delay: 每支股票之間的等待秒數（避免 rate limit）
    """
    total = len(stock_ids)
    for i, sid in enumerate(stock_ids, 1):
        logger.info(f"[{i}/{total}] 抓取股價: {sid}")
        try:
            df = fetch_daily_prices(sid, start_date, end_date)
            upsert_daily_prices(df)
        except Exception as e:
            logger.error(f"  ❌ {sid} 失敗: {e}")
        time.sleep(delay)
    logger.info("✅ 批次股價抓取完成")


def batch_fetch_institutional(
    stock_ids: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    delay: float = 1.0,
):
    total = len(stock_ids)
    for i, sid in enumerate(stock_ids, 1):
        logger.info(f"[{i}/{total}] 抓取籌碼: {sid}")
        try:
            df = fetch_institutional(sid, start_date, end_date)
            upsert_institutional(df)
        except Exception as e:
            logger.error(f"  ❌ {sid} 失敗: {e}")
        time.sleep(delay)
    logger.info("✅ 批次籌碼抓取完成")