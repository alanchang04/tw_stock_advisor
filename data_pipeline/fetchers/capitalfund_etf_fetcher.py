"""
data_pipeline/fetchers/capitalfund_etf_fetcher.py

群益投信主動式ETF每日完整持股（00982A 主動群益台灣強棒 / 00992A 主動群益科技創新）。

來源：群益投信官網 https://www.capitalfund.com.tw/etf/product/detail/{fundNo}/buyback
（fundNo：00982A=399、00992A=500，站內部代號）。前端是 Angular SPA，持股資料
由頁面載入後呼叫 POST /CFWeb/api/etf/buyback（body: {"fundId": fundNo, "date": null}）
取得，回傳「當日完整持股」（不是前10大）——這是主動式ETF依法規每日揭露的PCF資料，
比 `etf_fetcher.py` 走的 MoneyDJ 月更前10大路徑新鮮得多。

**為什麼不能用 requests 直接打**：這個 API 前面架了 Incapsula 防機器人 WAF，純
`requests.post` 會被判定為非瀏覽器連線直接吊住連線逾時（已實測），必須用真的瀏覽器
（Playwright headless chromium）先跑過 JS 挑戰才放行。這是本專案第一個需要瀏覽器
自動化的爬蟲（其餘都是輕量 requests），CI/本機首次需另外 `playwright install
chromium`（見 requirements.txt 與 .github/workflows/daily_update.yml）。

用法：
  run_capitalfund_fetch()   — 抓兩檔的當日持股，upsert 進 etf_holdings，偵測換股
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date, datetime

import pandas as pd
from loguru import logger

# 群益ETF代號 → 群益投信站內部 fundNo（找法：Playwright 攔截頁面的 POST /CFWeb/api/etf/buyback
# 網路請求，body 裡的 fundId 就是這個值；其他基金要加，一樣用瀏覽器開發者工具找一次）
CAPITALFUND_FUNDS = {
    "00982A": "399",   # 主動群益台灣強棒
    "00992A": "500",   # 主動群益科技創新
}

_BASE_URL = "https://www.capitalfund.com.tw/etf/product/detail/{fund_no}/buyback"
_API_URL_FRAGMENT = "capitalfund.com.tw/CFWeb/api/etf/buyback"


def _parse_buyback_json(payload: dict, etf_id: str) -> pd.DataFrame:
    """
    把 /CFWeb/api/etf/buyback 回傳的 JSON 轉成跟 etf_fetcher.fetch_etf_holdings() 一致的
    欄位：stock_id, stock_name, weight_pct, snapshot_date。純函式，不碰瀏覽器，方便測試。
    """
    data = (payload or {}).get("data") or {}
    stocks = data.get("stocks") or []
    pcf = data.get("pcf") or {}

    # date2 是 PCF 基準日（今天已收盤的資料），date1 是效力日（通常是隔一個交易日）——
    # 我們要的是「當日持股狀態」，用 date2。格式 "2026-07-15"。
    snap_raw = pcf.get("date2")
    try:
        snapshot_date = datetime.strptime(snap_raw, "%Y-%m-%d").date() if snap_raw else date.today()
    except ValueError:
        snapshot_date = date.today()

    rows = []
    for s in stocks:
        sid = s.get("stocNo")
        if not sid or not str(sid).isdigit():
            continue   # 跳過非個股列（現金/期貨等 stocNo 可能是文字或空）
        rows.append({
            "stock_id": sid,
            "stock_name": s.get("stocName") or sid,
            "weight_pct": s.get("weight"),
            "snapshot_date": snapshot_date,
        })
    if not rows:
        logger.warning(f"  {etf_id}：buyback API 沒解析到個股持股（回應格式可能改版）")
        return pd.DataFrame()
    return pd.DataFrame(rows)


def fetch_capitalfund_holdings(fund_ids: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """
    用 Playwright 開一個瀏覽器，依序訪問每檔基金的持股頁，攔截 buyback API 回應。
    一個瀏覽器實例跑完全部 fund_ids（省開瀏覽器的固定成本）。
    回傳 {etf_id: DataFrame}，抓取失敗的 etf_id 對應空 DataFrame（不中斷其他檔）。
    """
    fund_ids = fund_ids or list(CAPITALFUND_FUNDS.keys())
    results: dict[str, pd.DataFrame] = {etf_id: pd.DataFrame() for etf_id in fund_ids}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("  群益ETF抓取失敗：未安裝 playwright（pip install playwright && "
                     "playwright install chromium）")
        return results

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            for etf_id in fund_ids:
                fund_no = CAPITALFUND_FUNDS.get(etf_id)
                if not fund_no:
                    logger.warning(f"  {etf_id}：未知的群益 fundNo，略過")
                    continue
                captured = {}

                def _on_response(resp, _etf_id=etf_id):
                    if _API_URL_FRAGMENT in resp.url and resp.request.method == "POST":
                        try:
                            captured["payload"] = resp.json()
                        except Exception as e:
                            logger.warning(f"  {_etf_id}：API 回應解析失敗: {e}")

                page.on("response", _on_response)
                try:
                    page.goto(_BASE_URL.format(fund_no=fund_no),
                             wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    logger.error(f"  {etf_id}：頁面載入失敗: {e}")
                finally:
                    page.remove_listener("response", _on_response)

                if "payload" in captured:
                    results[etf_id] = _parse_buyback_json(captured["payload"], etf_id)
                else:
                    logger.warning(f"  {etf_id}：沒攔截到 buyback API 回應（可能被 Incapsula 擋下）")
            browser.close()
    except Exception as e:
        logger.error(f"  群益ETF Playwright 抓取失敗: {e}")

    return results


def run_capitalfund_fetch() -> list[tuple[str, str, dict]]:
    """pipeline用：抓群益兩檔當日持股，偵測換股並寫入 DB。回傳 [(etf_id, etf_name, change), ...]。"""
    from data_pipeline.fetchers.etf_fetcher import detect_and_save_changes

    names = {"00982A": "主動群益台灣強棒", "00992A": "主動群益科技創新"}
    logger.info("=== 群益ETF每日持股抓取（Playwright）===")
    holdings = fetch_capitalfund_holdings()
    all_changes = []
    for etf_id, df in holdings.items():
        if df.empty:
            continue
        changes = detect_and_save_changes(etf_id, names.get(etf_id, etf_id), df)
        all_changes.extend([(etf_id, names.get(etf_id, etf_id), c) for c in changes])
    logger.info(f"=== 群益ETF抓取完成：{sum(1 for df in holdings.values() if not df.empty)}/"
               f"{len(holdings)} 檔成功，{len(all_changes)} 筆異動 ===")
    return all_changes


if __name__ == "__main__":
    run_capitalfund_fetch()
