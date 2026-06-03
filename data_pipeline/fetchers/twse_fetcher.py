"""
data_pipeline/fetchers/twse_fetcher.py

每日更新用的「全市場單日」抓取器：直接打證交所(TWSE) / 櫃買(TPEX) 官方 OpenAPI，
一次回傳全市場最近交易日的資料，免 token、無 FinMind 每小時 600 次限制。

每日更新只需 4 次請求（上市股價 / 上櫃股價 / 上市法人 / 上櫃法人），數秒完成，
取代原本逐檔 FinMind 抓取（約 4000 次請求 / 數小時）。

歷史回補仍用 finmind_fetcher（一次性）。
"""
import re
import time
from datetime import date
from typing import Optional

import pandas as pd
import requests
from loguru import logger

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session
from sqlalchemy import text
from data_pipeline.fetchers.finmind_fetcher import (
    upsert_daily_prices, upsert_institutional,
)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TIMEOUT = 60

# OpenAPI 端點
URL_TWSE_PRICE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
URL_TWSE_INST  = "https://www.twse.com.tw/rwd/zh/fund/T86"   # 需帶 date + selectType
URL_TPEX_PRICE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
URL_TPEX_INST  = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"

# 指定日期端點（補資料用：每個交易日各一次請求）
URL_TWSE_PRICE_BYDATE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
URL_TPEX_PRICE_BYDATE = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
URL_TPEX_INST_BYDATE  = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"


# ── 共用工具 ─────────────────────────────────────────────────────
def _clean_num(v) -> Optional[float]:
    """把官方欄位的字串轉成數字：去逗號/空白，處理 ''、'--'、'X' 等。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace(" ", "")
    if s in ("", "--", "---", "X", "x", "N/A"):
        return None
    s = s.lstrip("X")            # 部分欄位有 'X' 前綴（除息等註記）
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None


def _clean_int(v) -> int:
    n = _clean_num(v)
    return int(n) if n is not None else 0


def _roc_to_date(s: str) -> Optional[date]:
    """民國日期字串 '1150602' → date(2026, 6, 2)。"""
    s = str(s).strip()
    if not s.isdigit() or len(s) < 7:
        return None
    year = int(s[:-4]) + 1911
    month = int(s[-4:-2])
    day = int(s[-2:])
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_stock_code(code: str) -> bool:
    """只保留 4 位數字的普通股代號。
       台股普通股皆為 4 位；5~6 位數字代號幾乎都是權證、債券ETF、ETN 等，
       不在選股範圍，一律排除（避免權證污染股票池）。"""
    return bool(re.fullmatch(r"\d{4}", str(code).strip()))


def _db_stock_ids() -> set:
    """DB 已登錄的股票代號（用來過濾，避免外鍵違反）。"""
    with get_session() as session:
        rows = session.execute(text("SELECT stock_id FROM stocks")).fetchall()
    return {r[0] for r in rows}


def _get_json(url: str, params: dict = None, retries: int = 3) -> list:
    """GET + 解析 JSON，含重試（官方端點偶爾會連線中斷 IncompleteRead）。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            logger.warning(f"請求失敗（第 {attempt}/{retries} 次）：{url} — {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_err


# ── 1. 股價（全市場最近交易日）──────────────────────────────────
def _change_pct(close: Optional[float], change: Optional[float]) -> Optional[float]:
    """由收盤價與漲跌額回推漲跌幅 %。prev = close - change。"""
    if close is None or change is None:
        return None
    prev = close - change
    if prev == 0:
        return None
    return round(change / prev * 100, 4)


def fetch_prices_twse() -> pd.DataFrame:
    data = _get_json(URL_TWSE_PRICE)
    rows = []
    for d in data:
        close = _clean_num(d.get("ClosingPrice"))
        if close is None:           # 無成交/暫停交易
            continue
        change = _clean_num(d.get("Change"))
        rows.append({
            "stock_id":   str(d["Code"]).strip(),
            "trade_date": _roc_to_date(d["Date"]),
            "open":   _clean_num(d.get("OpeningPrice")),
            "high":   _clean_num(d.get("HighestPrice")),
            "low":    _clean_num(d.get("LowestPrice")),
            "close":  close,
            "volume": _clean_int(d.get("TradeVolume")),
            "turnover": _clean_int(d.get("TradeValue")),
            "change_pct": _change_pct(close, change),
        })
    return pd.DataFrame(rows)


def fetch_prices_tpex() -> pd.DataFrame:
    data = _get_json(URL_TPEX_PRICE)
    rows = []
    for d in data:
        close = _clean_num(d.get("Close"))
        if close is None:
            continue
        change = _clean_num(d.get("Change"))
        rows.append({
            "stock_id":   str(d["SecuritiesCompanyCode"]).strip(),
            "trade_date": _roc_to_date(d["Date"]),
            "open":   _clean_num(d.get("Open")),
            "high":   _clean_num(d.get("High")),
            "low":    _clean_num(d.get("Low")),
            "close":  close,
            "volume": _clean_int(d.get("TradingShares")),
            "turnover": _clean_int(d.get("TransactionAmount")),
            "change_pct": _change_pct(close, change),
        })
    return pd.DataFrame(rows)


# ── 2. 三大法人（全市場最近交易日）─────────────────────────────
def fetch_institutional_twse(trade_date: date) -> pd.DataFrame:
    """證交所 T86：需指定日期(AD)。selectType=ALLBUT0999（全部，不含權證牛熊證）。"""
    # TWSE RWD 端點短時間內密集呼叫會被軟性限流（回「沒有符合條件的資料」），
    # 故空資料時退避重試；正式每日只呼叫一次，通常一次就成功
    payload = {}
    for attempt in range(1, 4):
        payload = _get_json(URL_TWSE_INST, params={
            "date": trade_date.strftime("%Y%m%d"),
            "selectType": "ALLBUT0999",
            "response": "json",
        })
        if payload.get("stat") == "OK" and payload.get("data"):
            break
        logger.warning(f"T86 暫無資料（第 {attempt}/3 次）：{payload.get('stat')}")
        if attempt < 3:
            time.sleep(5 * attempt)
    if payload.get("stat") != "OK" or not payload.get("data"):
        logger.error("T86 多次重試仍無資料，跳過上市法人更新")
        return pd.DataFrame()

    fields = payload["fields"]

    def col(*keywords):
        """回傳第一個「包含全部關鍵字」的欄位索引，找不到回 None。"""
        for i, name in enumerate(fields):
            if all(k in name for k in keywords):
                return i
        return None

    i_fb = col("外陸資買進", "不含外資自營商")
    i_fs = col("外陸資賣出", "不含外資自營商")
    i_ib = col("投信買進")
    i_is = col("投信賣出")
    i_db_self = col("自營商買進股數", "自行買賣")
    i_ds_self = col("自營商賣出股數", "自行買賣")
    i_db_hed  = col("自營商買進股數", "避險")
    i_ds_hed  = col("自營商賣出股數", "避險")
    i_total   = col("三大法人買賣超股數")

    rows = []
    for r in payload["data"]:
        # 少數股票（特殊交易狀態）官方少給欄位，需防越界
        g = lambda i: _clean_int(r[i]) if (i is not None and i < len(r)) else 0
        fb, fs = g(i_fb), g(i_fs)
        ib, is_ = g(i_ib), g(i_is)
        db = g(i_db_self) + g(i_db_hed)
        ds = g(i_ds_self) + g(i_ds_hed)
        rows.append({
            "stock_id":   str(r[0]).strip(),
            "trade_date": trade_date,
            "foreign_buy": fb, "foreign_sell": fs, "foreign_net": fb - fs,
            "invest_buy": ib, "invest_sell": is_, "invest_net": ib - is_,
            "dealer_buy": db, "dealer_sell": ds, "dealer_net": db - ds,
            "total_net": g(i_total),
        })
    return pd.DataFrame(rows)


def fetch_institutional_tpex(trade_date: date) -> pd.DataFrame:
    """櫃買 三大法人買賣明細（OpenAPI，最近交易日）。"""
    data = _get_json(URL_TPEX_INST)
    if not data:
        return pd.DataFrame()

    # 欄位名稱含不規則空格，正規化後用子字串比對
    norm = {re.sub(r"\s+", "", k).lower(): k for k in data[0].keys()}

    def key(*musts, without=()):
        for nk, orig in norm.items():
            if all(m in nk for m in musts) and not any(w in nk for w in without):
                return orig
        return None

    k_fb = key("foreigndealersexcluded", "totalbuy")
    k_fs = key("foreigndealersexcluded", "totalsell")
    k_ib = key("securitiesinvestmenttrust", "totalbuy")
    k_is = key("securitiesinvestmenttrust", "totalsell")
    k_db = key("dealers", "totalbuy", without=("foreign", "securities"))
    k_ds = key("dealers", "totalsell", without=("foreign", "securities"))
    k_total = key("totaldifference")

    rows = []
    for d in data:
        gv = lambda k: _clean_int(d.get(k)) if k else 0
        fb, fs = gv(k_fb), gv(k_fs)
        ib, is_ = gv(k_ib), gv(k_is)
        db, ds = gv(k_db), gv(k_ds)
        td = _roc_to_date(d.get("Date")) or trade_date
        rows.append({
            "stock_id":   str(d["SecuritiesCompanyCode"]).strip(),
            "trade_date": td,
            "foreign_buy": fb, "foreign_sell": fs, "foreign_net": fb - fs,
            "invest_buy": ib, "invest_sell": is_, "invest_net": ib - is_,
            "dealer_buy": db, "dealer_sell": ds, "dealer_net": db - ds,
            "total_net": gv(k_total),
        })
    return pd.DataFrame(rows)


# ── 3. 對外主流程 ───────────────────────────────────────────────
def _filter_known(df: pd.DataFrame, known: set) -> pd.DataFrame:
    if df.empty:
        return df
    df = df[df["stock_id"].apply(_is_stock_code)]
    df = df[df["stock_id"].isin(known)]
    df = df[df["trade_date"].notna()]
    return df.reset_index(drop=True)


def update_daily_via_openapi():
    """每日更新主流程：抓全市場最近交易日的股價 + 三大法人，寫入 DB。"""
    logger.info("=== 每日更新（TWSE/TPEX OpenAPI）===")
    known = _db_stock_ids()
    if not known:
        logger.warning("stocks 資料表是空的，請先執行 --mode init 建立股票清單")
        return

    # 股價
    df_price = pd.concat([fetch_prices_twse(), fetch_prices_tpex()], ignore_index=True)
    df_price = _filter_known(df_price, known)
    trade_date = df_price["trade_date"].max() if not df_price.empty else date.today()
    upsert_daily_prices(df_price)
    logger.info(f"✅ 股價更新 {len(df_price)} 檔（交易日 {trade_date}）")

    # 三大法人（用股價的交易日對齊 T86 日期）
    df_inst = pd.concat([
        fetch_institutional_twse(trade_date),
        fetch_institutional_tpex(trade_date),
    ], ignore_index=True)
    df_inst = _filter_known(df_inst, known)
    upsert_institutional(df_inst)
    logger.info(f"✅ 三大法人更新 {len(df_inst)} 檔")

    logger.info("=== 每日更新完成 ===")


# ── 4. 指定日期抓取（補資料 / 補跑漏掉的日子）────────────────────
def _twse_signed_change(sign_html: str, spread) -> Optional[float]:
    """MI_INDEX 的漲跌方向放在 HTML 欄（綠=跌、紅=漲），價差在另一欄。"""
    sp = _clean_num(spread)
    if sp is None:
        return None
    s = str(sign_html)
    if "green" in s:                 # 綠色 = 下跌
        return -sp
    return sp                        # 紅色 = 上漲；平盤時 sp=0，正負不影響


def fetch_prices_twse_by_date(d: date) -> pd.DataFrame:
    payload = _get_json(URL_TWSE_PRICE_BYDATE, params={
        "date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json",
    })
    if payload.get("stat") != "OK":
        return pd.DataFrame()         # 非交易日 / 無資料
    tbl = next((t for t in payload.get("tables", [])
                if "每日收盤行情" in t.get("title", "")), None)
    if not tbl or not tbl.get("data"):
        return pd.DataFrame()

    rows = []
    for r in tbl["data"]:
        close = _clean_num(r[8])
        if close is None:
            continue
        change = _twse_signed_change(r[9], r[10])
        rows.append({
            "stock_id": str(r[0]).strip(), "trade_date": d,
            "open": _clean_num(r[5]), "high": _clean_num(r[6]), "low": _clean_num(r[7]),
            "close": close, "volume": _clean_int(r[2]), "turnover": _clean_int(r[4]),
            "change_pct": _change_pct(close, change),
        })
    return pd.DataFrame(rows)


def fetch_prices_tpex_by_date(d: date) -> pd.DataFrame:
    payload = _get_json(URL_TPEX_PRICE_BYDATE, params={
        "date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json",
    })
    tables = payload.get("tables") or []
    if str(payload.get("stat", "")).lower() != "ok" or not tables:
        return pd.DataFrame()
    tbl = tables[0]                   # 上櫃股票行情：代號0 名稱1 收盤2 漲跌3 開盤4 最高5 最低6 均價7 成交股數8 成交金額9
    rows = []
    for r in tbl.get("data", []):
        close = _clean_num(r[2])
        if close is None:
            continue
        change = _clean_num(r[3])
        rows.append({
            "stock_id": str(r[0]).strip(), "trade_date": d,
            "open": _clean_num(r[4]), "high": _clean_num(r[5]), "low": _clean_num(r[6]),
            "close": close, "volume": _clean_int(r[8]), "turnover": _clean_int(r[9]),
            "change_pct": _change_pct(close, change),
        })
    return pd.DataFrame(rows)


def fetch_institutional_tpex_by_date(d: date) -> pd.DataFrame:
    """櫃買 三大法人 by-date（24 欄，標準欄序：每 3 欄為一組 買/賣/買賣超）。
       欄序：外陸資(不含自營商)2-4、外資自營商5-7、外資合計8-10、投信11-13、
             自營(自行)14-16、自營(避險)17-19、自營合計20-22、三大法人合計23。"""
    payload = _get_json(URL_TPEX_INST_BYDATE, params={
        "date": d.strftime("%Y/%m/%d"), "type": "Daily", "response": "json", "sect": "EW",
    })
    tables = payload.get("tables") or []
    if str(payload.get("stat", "")).lower() != "ok" or not tables:
        return pd.DataFrame()
    rows = []
    for r in tables[0].get("data", []):
        if len(r) < 24:
            continue
        g = lambda i: _clean_int(r[i])
        rows.append({
            "stock_id": str(r[0]).strip(), "trade_date": d,
            "foreign_buy": g(2), "foreign_sell": g(3), "foreign_net": g(2) - g(3),
            "invest_buy": g(11), "invest_sell": g(12), "invest_net": g(11) - g(12),
            "dealer_buy": g(20), "dealer_sell": g(21), "dealer_net": g(20) - g(21),
            "total_net": g(23),
        })
    return pd.DataFrame(rows)


def _last_price_date() -> Optional[date]:
    with get_session() as session:
        row = session.execute(text("SELECT MAX(trade_date) FROM daily_prices")).fetchone()
    return row[0] if row and row[0] else None


def fetch_stock_list_openapi() -> pd.DataFrame:
    """用 OpenAPI 最近交易日的股價資料建立股票清單（stock_id / stock_name / market）。
       免 FinMind，純官方來源；只保留 4~6 位純數字代號。"""
    rows = []
    for d in _get_json(URL_TWSE_PRICE):
        code = str(d.get("Code", "")).strip()
        if _is_stock_code(code) and _clean_num(d.get("ClosingPrice")) is not None:
            rows.append({"stock_id": code, "stock_name": str(d.get("Name", "")).strip(), "market": "TWSE"})
    for d in _get_json(URL_TPEX_PRICE):
        code = str(d.get("SecuritiesCompanyCode", "")).strip()
        if _is_stock_code(code) and _clean_num(d.get("Close")) is not None:
            rows.append({"stock_id": code, "stock_name": str(d.get("CompanyName", "")).strip(), "market": "TPEX"})
    df = pd.DataFrame(rows).drop_duplicates("stock_id").reset_index(drop=True)
    return df


def ensure_stock_list() -> set:
    """確保 stocks 表有資料；空的話用 OpenAPI 自建。回傳目前的股票代號集合。"""
    known = _db_stock_ids()
    if known:
        return known
    logger.info("stocks 表是空的，改用 OpenAPI 自動建立股票清單...")
    from data_pipeline.fetchers.finmind_fetcher import upsert_stock_list
    df = fetch_stock_list_openapi()
    if df.empty:
        logger.error("OpenAPI 取不到股票清單")
        return set()
    upsert_stock_list(df)
    logger.info(f"✅ 已建立 {len(df)} 支股票")
    return _db_stock_ids()


def _fetch_prices_day(d: date, known: set):
    """回傳 (twse_df, tpex_df)，已過濾為 DB 已知股票。"""
    twse = _filter_known(fetch_prices_twse_by_date(d), known)
    tpex = _filter_known(fetch_prices_tpex_by_date(d), known)
    return twse, tpex


def backfill(start: date = None, end: date = None):
    """
    補抓 [start, end] 之間每個交易日的全市場股價 + 三大法人（用指定日期端點）。

    - start 省略時，自動從 DB 現有最後一天的「隔天」開始（接續補洞）；DB 全空時補近 30 天。
    - stocks 表為空會自動用 OpenAPI 建立。
    - 區分「假日」(兩市場皆無資料) 與「被限流」(只有一邊空)，後者會重試，避免靜默缺資料。
    """
    from datetime import timedelta
    known = ensure_stock_list()
    if not known:
        logger.error("無股票清單，無法補抓")
        return

    end = end or date.today()
    if start is None:
        last = _last_price_date()
        start = (last + timedelta(days=1)) if last else (end - timedelta(days=30))
    if start > end:
        logger.info(f"資料已是最新（DB 最後交易日 {start - timedelta(days=1)}），無需補抓")
        return

    logger.info(f"=== 補抓資料：{start} ~ {end} ===")
    d = start
    filled, warned = 0, []
    while d <= end:
        if d.weekday() >= 5:          # 六日直接跳過
            d += timedelta(days=1)
            continue

        twse_p, tpex_p = _fetch_prices_day(d, known)

        # 兩邊都空 → 可能假日；再確認一次避免限流誤判
        if twse_p.empty and tpex_p.empty:
            time.sleep(5)
            twse_p, tpex_p = _fetch_prices_day(d, known)
            if twse_p.empty and tpex_p.empty:
                logger.info(f"  {d} 無資料（假日）跳過")
                d += timedelta(days=1)
                continue

        # 只有一邊空 → 多半是被限流，重試該邊
        for _ in range(3):
            if not twse_p.empty and not tpex_p.empty:
                break
            time.sleep(5)
            if twse_p.empty:
                twse_p = _filter_known(fetch_prices_twse_by_date(d), known)
            if tpex_p.empty:
                tpex_p = _filter_known(fetch_prices_tpex_by_date(d), known)
        if twse_p.empty or tpex_p.empty:
            warned.append(d)
            logger.warning(f"  ⚠️ {d} 有一邊持續抓不到（可能限流），稍後重跑 backfill 可補回此日")

        df_p = pd.concat([twse_p, tpex_p], ignore_index=True)
        upsert_daily_prices(df_p)
        df_i = _filter_known(pd.concat(
            [fetch_institutional_twse(d), fetch_institutional_tpex_by_date(d)],
            ignore_index=True), known)
        upsert_institutional(df_i)
        logger.info(f"  ✅ {d}：股價 {len(df_p)} 檔、法人 {len(df_i)} 檔")
        filled += 1

        time.sleep(3)                 # 對官方站台禮貌間隔
        d += timedelta(days=1)

    msg = f"=== 補抓完成，共補 {filled} 個交易日 ==="
    if warned:
        msg += f"（{len(warned)} 天可能不完整：{', '.join(str(x) for x in warned)}）"
    logger.info(msg)


# ── 自我測試：只抓取與解析，不寫 DB ────────────────────────────
# 欄位語意：
#   foreign_net = 外陸資「不含外資自營商」淨買賣（與 TWSE T86 / 原 FinMind 定義一致）
#   total_net   = 官方「三大法人合計」欄位（最權威，直接採用）
# TWSE 合計 = 外陸資(不含自營商) + 投信 + 自營，三分項即可重現合計；
# TPEX 合計另含「外資自營商」子類，故三分項不一定等於合計——但 foreign_net 與
# total_net 兩個欄位各自正確（評分只用到這兩者）。
if __name__ == "__main__":
    print(">> 測試股價抓取")
    tw = fetch_prices_twse()
    tp = fetch_prices_tpex()
    print(f"   上市 {len(tw)} 檔, 上櫃 {len(tp)} 檔")
    s = tw[tw["stock_id"] == "2330"]
    if not s.empty:
        print("   2330 台積電:", s.iloc[0].to_dict())

    td = tw["trade_date"].max()
    print(f">> 測試三大法人抓取（交易日 {td}）")
    itw = fetch_institutional_twse(td)
    itp = fetch_institutional_tpex(td)
    print(f"   上市 {len(itw)} 檔, 上櫃 {len(itp)} 檔")

    # TWSE：只看真正個股，驗證 外資+投信+自營 == 官方合計
    if not itw.empty:
        real = itw[itw["stock_id"].apply(_is_stock_code)].copy()
        real["s3"] = real["foreign_net"] + real["invest_net"] + real["dealer_net"]
        bad = (real["s3"] - real["total_net"]).abs()
        print(f"   [TWSE] {len(real)} 檔個股，三分項!=官方合計者 {int((bad>0).sum())} 檔（應為 0）")
        s = itw[itw["stock_id"] == "2330"]
        if not s.empty:
            print("   [TWSE] 2330:", s.iloc[0].to_dict())
    else:
        print("   [TWSE] 此次無法人資料（多為測試期間被限流，每日單次呼叫不受影響）")
    if not itp.empty:
        print("   [TPEX] 範例:", itp.iloc[0].to_dict())
