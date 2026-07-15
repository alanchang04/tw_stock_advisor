"""
data_pipeline/fetchers/financials_fetcher.py

季度財報擷取（SPEC_STRATEGY_MIDCAP 決策5：先補完整財報，讓基本面判讀不只靠月營收）。

來源：TWSE / TPEX OpenAPI（免 key，全市場一次抓完）——只涵蓋「一般業」
（金融/保險/證券等特殊報表格式的公司不在此範圍，欄位不同，之後有需要再補）。
  上市 綜合損益表：https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci
  上市 資產負債表：https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci
  上櫃 綜合損益表：https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci
  上櫃 資產負債表：https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci

**誠實限制**：這四個 OpenAPI 端點只回傳「當期最新一季」的快照，不支援用參數查歷史
季度（MOPS 正式查詢頁 t164sb04 有歷史，但有機器人防護會擋直接爬蟲，見開發時的
403「安全性考量」測試紀錄）。所以本抓取器等同月營收抓取器的「抓最新」模式：
每季公布後跑一次，逐季 upsert 累積歷史，做不到一次回補過去很多季。這代表短期內
`financials` 表只會有「起跑後」的季度，起跑前的歷史季度是真空——這點要對使用者
誠實揭露，不要假裝資料完整。

用法：
  run_financials_fetch()   — 抓當期最新一季（上市+上櫃），upsert 進 financials
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from loguru import logger
from sqlalchemy import text

from database.connection import get_session

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

URL_INCOME = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci",
}
URL_BALANCE = {
    "TWSE": "https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci",
    "TPEX": "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci",
}


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("-", "N/A", "不適用"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clamp_pct(v: float | None, limit: float = 9999.0) -> float | None:
    """NUMERIC(8,4) 欄位範圍有限；營收/淨值趨近 0 或為負時算出的比率會爆量，超界視為異常值捨棄。"""
    if v is None:
        return None
    return v if abs(v) < limit else None


def _get(row: dict, *keys):
    """兩個市場的 JSON 欄位名不完全一致（上市中文欄名／上櫃英中混用），依序嘗試。"""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _fetch_json(url: str) -> list[dict]:
    import requests
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_financials() -> int:
    """抓上市+上櫃當期最新一季綜合損益表+資產負債表，合併算衍生指標，upsert 進 financials。回傳筆數。"""
    rows_by_sid: dict[str, dict] = {}

    for market in ("TWSE", "TPEX"):
        try:
            income = _fetch_json(URL_INCOME[market])
        except Exception as e:
            logger.warning(f"  {market} 綜合損益表抓取失敗: {e}")
            income = []
        try:
            balance = _fetch_json(URL_BALANCE[market])
        except Exception as e:
            logger.warning(f"  {market} 資產負債表抓取失敗: {e}")
            balance = []

        balance_map = {}
        for b in balance:
            sid = _get(b, "公司代號", "SecuritiesCompanyCode")
            if sid:
                balance_map[sid] = b

        for i in income:
            sid = _get(i, "公司代號", "SecuritiesCompanyCode")
            if not sid:
                continue
            year_roc = _num(_get(i, "年度", "Year"))
            season = _num(_get(i, "季別", "Season"))
            if year_roc is None or season is None:
                continue

            revenue = _num(_get(i, "營業收入"))
            gross_profit = _num(_get(i, "營業毛利（毛損）淨額", "營業毛利（毛損）"))
            operating_income = _num(_get(i, "營業利益（損失）"))
            net_income = _num(_get(i, "淨利（淨損）歸屬於母公司業主", "本期淨利（淨損）"))
            eps = _num(_get(i, "基本每股盈餘（元）", "基本每股盈餘(元)"))

            b = balance_map.get(sid, {})
            total_assets = _num(_get(b, "資產總額", "資產總計"))
            total_liab = _num(_get(b, "負債總額", "負債總計"))
            equity = _num(_get(b, "歸屬於母公司業主之權益合計"))

            gross_margin = (gross_profit / revenue * 100) if gross_profit is not None and revenue else None
            operating_margin = (operating_income / revenue * 100) if operating_income is not None and revenue else None
            debt_ratio = (total_liab / total_assets * 100) if total_liab is not None and total_assets else None
            roa = (net_income / total_assets * 100) if net_income is not None and total_assets else None
            roe = (net_income / equity * 100) if net_income is not None and equity else None

            rows_by_sid[sid] = {
                "sid": sid,
                "year": int(year_roc + 1911),
                "quarter": int(season),
                # MOPS 原始數值已是「千元」單位，financials 欄位註解同單位，不需再乘 1000
                "revenue": int(revenue) if revenue is not None else None,
                "gross_profit": int(gross_profit) if gross_profit is not None else None,
                "operating_income": int(operating_income) if operating_income is not None else None,
                "net_income": int(net_income) if net_income is not None else None,
                "eps": _clamp_pct(eps), "roe": _clamp_pct(roe), "roa": _clamp_pct(roa),
                "gross_margin": _clamp_pct(gross_margin), "operating_margin": _clamp_pct(operating_margin),
                "debt_ratio": _clamp_pct(debt_ratio),
            }
        logger.info(f"  {market}：綜合損益表 {len(income)} 家、資產負債表 {len(balance)} 家 → 合併 {len(rows_by_sid)} 家（累計）")

    if not rows_by_sid:
        return 0

    with get_session() as s:
        known_sids = {r[0] for r in s.execute(text("SELECT stock_id FROM stocks")).fetchall()}
        skipped = [sid for sid in rows_by_sid if sid not in known_sids]
        for sid in skipped:
            del rows_by_sid[sid]
        if skipped:
            logger.info(f"  略過 {len(skipped)} 檔（不在 stocks 表，如特別股/已下市）")
        if not rows_by_sid:
            return 0
        s.execute(text("""
            INSERT INTO financials (stock_id, year, quarter, revenue, gross_profit,
                operating_income, net_income, eps, roe, roa, gross_margin, operating_margin, debt_ratio)
            VALUES (:sid, :year, :quarter, :revenue, :gross_profit,
                :operating_income, :net_income, :eps, :roe, :roa, :gross_margin, :operating_margin, :debt_ratio)
            ON CONFLICT (stock_id, year, quarter) DO UPDATE SET
                revenue = EXCLUDED.revenue, gross_profit = EXCLUDED.gross_profit,
                operating_income = EXCLUDED.operating_income, net_income = EXCLUDED.net_income,
                eps = EXCLUDED.eps, roe = EXCLUDED.roe, roa = EXCLUDED.roa,
                gross_margin = EXCLUDED.gross_margin, operating_margin = EXCLUDED.operating_margin,
                debt_ratio = EXCLUDED.debt_ratio
        """), list(rows_by_sid.values()))

    logger.info(f"  財報：寫入 {len(rows_by_sid)} 家")
    return len(rows_by_sid)


def run_financials_fetch() -> int:
    """pipeline/排程用：抓當期最新一季（重複跑會 upsert 覆蓋，不會重複累積）。"""
    logger.info("=== 抓取季度財報（上市+上櫃，僅一般業）===")
    return fetch_financials()


if __name__ == "__main__":
    run_financials_fetch()
