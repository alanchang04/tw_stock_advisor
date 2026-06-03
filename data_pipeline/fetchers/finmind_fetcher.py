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


# ── 初始化 FinMind DataLoader（單例，避免每次抓取都重新登入）──────
_loader: Optional[DataLoader] = None


def _get_loader() -> DataLoader:
    global _loader
    if _loader is None:
        _loader = DataLoader()
        if APIConfig.FINMIND_TOKEN:
            _loader.login_by_token(api_token=APIConfig.FINMIND_TOKEN)
        else:
            logger.warning("未設定 FINMIND_TOKEN，使用未登入模式（有流量限制）")
    return _loader


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


# ── 4. API 用量查詢 ──────────────────────────────────────────────
def get_api_usage() -> tuple[int, int]:
    """回傳 (已用次數, 每小時上限)"""
    import requests as req
    token = APIConfig.FINMIND_TOKEN
    resp = req.get(
        "https://api.web.finmindtrade.com/v2/user_info",
        params={"token": token},
        timeout=10,
    )
    data = resp.json()
    return data.get("user_count", 0), data.get("api_request_limit", 600)


def _wait_if_near_limit(used: int, limit: int, buffer: int = 30):
    """
    如果剩餘額度 <= buffer，等到下一個整點重置再繼續
    buffer: 保留幾次不用，預防邊界誤差
    """
    remaining = limit - used
    if remaining <= buffer:
        import datetime
        now = datetime.datetime.now()
        # 等到下一個整點（FinMind 每小時整點重置）
        wait_min = 60 - now.minute + 1
        logger.warning(
            f"⚠️  API 額度剩餘 {remaining} 次，暫停 {wait_min} 分鐘等待重置..."
        )
        time.sleep(wait_min * 60)
        logger.info("✅ 額度已重置，繼續抓取")


import json
from pathlib import Path

FETCHED_LOG_DIR           = Path(__file__).parent.parent.parent / "logs"
FETCHED_PRICES_LOG        = FETCHED_LOG_DIR / "fetched_prices.json"
FETCHED_INSTITUTIONAL_LOG = FETCHED_LOG_DIR / "fetched_institutional.json"


def _load_fetched(log_file: Path) -> set:
    if log_file.exists():
        return set(json.loads(log_file.read_text()))
    return set()


def _save_fetched(log_file: Path, fetched: set):
    FETCHED_LOG_DIR.mkdir(exist_ok=True)
    log_file.write_text(json.dumps(sorted(fetched)))


# ── 5. 查詢 DB 已有哪些股票的資料 ────────────────────────────────
def get_stocks_with_prices(start_date: str) -> set:

    """回傳在 start_date 之後已有股價資料的股票代號集合"""
    with get_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT stock_id FROM daily_prices
            WHERE trade_date >= :start
        """), {"start": start_date})
        return {r[0] for r in result.fetchall()}


def get_stocks_with_institutional(start_date: str) -> set:
    """回傳在 start_date 之後已有籌碼資料的股票代號集合"""
    with get_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT stock_id FROM institutional_trading
            WHERE trade_date >= :start
        """), {"start": start_date})
        return {r[0] for r in result.fetchall()}


# ── 6. 批次抓取（多支股票，含速率控制和斷點續抓）────────────────
def batch_fetch_prices(
    stock_ids: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    delay: float = 1.0,
    skip_existing: bool = True,
):
    """
    批次抓多支股票的每日股價並寫入 DB
    skip_existing: True 時跳過已嘗試過的股票（含無資料的股票）
    """
    fetched = _load_fetched(FETCHED_PRICES_LOG) if skip_existing else set()
    todo = [s for s in stock_ids if s not in fetched]
    if skip_existing and len(fetched):
        logger.info(f"跳過已嘗試過的股票：{len(fetched)} 支，剩餘 {len(todo)} 支待抓")

    total = len(todo)
    for i, sid in enumerate(todo, 1):
        if i % 50 == 0:
            used, limit = get_api_usage()
            logger.info(f"API 用量：{used}/{limit}")
            _wait_if_near_limit(used, limit)

        logger.info(f"[{i}/{total}] 抓取股價: {sid}")
        try:
            df = fetch_daily_prices(sid, start_date, end_date)
            upsert_daily_prices(df)
            fetched.add(sid)
            _save_fetched(FETCHED_PRICES_LOG, fetched)
        except Exception as e:
            if "upper limit" in str(e).lower():
                logger.warning("觸達 API 上限，等待 65 分鐘後繼續...")
                time.sleep(65 * 60)
                try:
                    df = fetch_daily_prices(sid, start_date, end_date)
                    upsert_daily_prices(df)
                    fetched.add(sid)
                    _save_fetched(FETCHED_PRICES_LOG, fetched)
                except Exception as e2:
                    logger.error(f"  ❌ {sid} 重試失敗: {e2}（不標記完成，下次會重抓）")
            else:
                # 暫時性錯誤（網路、單檔異常）不標記完成，下次自動重試，
                # 避免把「抓失敗」誤當成「已抓到」而造成靜默缺資料
                logger.error(f"  ❌ {sid} 失敗: {e}（不標記完成，下次會重抓）")
        time.sleep(delay)
    logger.info("✅ 批次股價抓取完成")


def batch_fetch_institutional(
    stock_ids: list[str],
    start_date: str,
    end_date: Optional[str] = None,
    delay: float = 1.0,
    skip_existing: bool = True,
):
    fetched = _load_fetched(FETCHED_INSTITUTIONAL_LOG) if skip_existing else set()
    todo = [s for s in stock_ids if s not in fetched]
    if skip_existing and len(fetched):
        logger.info(f"跳過已嘗試過的股票：{len(fetched)} 支，剩餘 {len(todo)} 支待抓")

    total = len(todo)
    for i, sid in enumerate(todo, 1):
        if i % 50 == 0:
            used, limit = get_api_usage()
            logger.info(f"API 用量：{used}/{limit}")
            _wait_if_near_limit(used, limit)

        logger.info(f"[{i}/{total}] 抓取籌碼: {sid}")
        try:
            df = fetch_institutional(sid, start_date, end_date)
            upsert_institutional(df)
            fetched.add(sid)
            _save_fetched(FETCHED_INSTITUTIONAL_LOG, fetched)
        except Exception as e:
            if "upper limit" in str(e).lower():
                logger.warning("觸達 API 上限，等待 65 分鐘後繼續...")
                time.sleep(65 * 60)
                try:
                    df = fetch_institutional(sid, start_date, end_date)
                    upsert_institutional(df)
                    fetched.add(sid)
                    _save_fetched(FETCHED_INSTITUTIONAL_LOG, fetched)
                except Exception as e2:
                    logger.error(f"  ❌ {sid} 重試失敗: {e2}（不標記完成，下次會重抓）")
            else:
                # 暫時性錯誤不標記完成，下次自動重試，避免靜默缺資料
                logger.error(f"  ❌ {sid} 失敗: {e}（不標記完成，下次會重抓）")
        time.sleep(delay)
    logger.info("✅ 批次籌碼抓取完成")