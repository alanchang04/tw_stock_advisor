"""
data_pipeline/fetchers/uni_etf_fetcher.py

統一投信主動式 ETF「每日完整持股」擷取。

來源：ezmoney 產品頁內嵌的投資組合 JSON（HTML-escaped，每日更新）
  https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={fundCode}
  欄位：TranDate(基準日) / DetailCode(代號) / DetailName / Share(股數) /
        NavRate(權重%) / AssetCode(ST=股票)

與 MoneyDJ（月更、前十大）的差別：這裡是**每日、全持股**——
主動式 ETF 依法每日揭露，跟著統一操盤手換股就靠這個。

首次執行建立快照，之後每日與前一次快照比對 → etf_changes + market_signals
（沿用 etf_fetcher.detect_and_save_changes，聰明資金頁自動吃到）。
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import html as html_lib
import json
import re
from datetime import datetime

import pandas as pd
import requests
from loguru import logger

# 統一台股主動式 ETF（00988A 為全球型、成分是海外股票，不納入台股追蹤）
UNI_ACTIVE_FUNDS = {
    "00981A": {"fund_code": "49YTW", "name": "主動統一台股增長"},
    "00403A": {"fund_code": "63YTW", "name": "主動統一升級50"},
}

_INFO_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode={fund_code}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _extract_json_array(text: str, anchor: str) -> list | None:
    """從 unescape 後的頁面文字中，找出包含 anchor 的第一個完整 JSON 陣列（中括號配對掃描）。"""
    idx = text.find(anchor)
    if idx == -1:
        return None
    # 往前找陣列起點 '['
    start = text.rfind("[", 0, idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, min(len(text), start + 3_000_000)):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def fetch_uni_holdings(etf_id: str) -> pd.DataFrame:
    """
    抓統一主動 ETF 的每日完整持股。
    回傳欄位：stock_id, stock_name, weight_pct, snapshot_date（與 etf_fetcher 相容）
    """
    meta = UNI_ACTIVE_FUNDS.get(etf_id)
    if not meta:
        return pd.DataFrame()

    url = _INFO_URL.format(fund_code=meta["fund_code"])
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        page = html_lib.unescape(resp.text)
    except Exception as e:
        logger.error(f"  {etf_id} ezmoney 抓取失敗: {e}")
        return pd.DataFrame()

    records = _extract_json_array(page, '"TranDate"')
    if not records:
        logger.warning(f"  {etf_id}：找不到投資組合 JSON（頁面可能改版）")
        return pd.DataFrame()

    rows = []
    for r in records:
        if not isinstance(r, dict):
            continue
        code = str(r.get("DetailCode", "")).strip()
        # 只收台股個股（4 碼數字，排除期貨/現金/附買回等）
        if r.get("AssetCode") != "ST" or not re.fullmatch(r"\d{4}", code):
            continue
        try:
            snap = datetime.fromisoformat(str(r.get("TranDate"))[:10]).date()
        except Exception:
            continue
        rows.append({
            "stock_id":      code,
            "stock_name":    (r.get("DetailName") or code).strip(),
            "weight_pct":    float(r.get("NavRate") or 0),
            "shares":        int(r.get("Share") or 0),
            "snapshot_date": snap,
        })

    if not rows:
        logger.warning(f"  {etf_id}：JSON 解析後無台股持股")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # 同一檔股票若多列（不同 Sequence），合併權重與股數
    df = df.groupby(["stock_id", "stock_name", "snapshot_date"], as_index=False).agg(
        weight_pct=("weight_pct", "sum"), shares=("shares", "sum"))
    latest = df["snapshot_date"].max()
    df = df[df["snapshot_date"] == latest].reset_index(drop=True)

    logger.info(f"  {etf_id} {meta['name']}：{latest} 全持股 {len(df)} 檔"
                f"（權重合計 {df['weight_pct'].sum():.1f}%）")
    return df[["stock_id", "stock_name", "weight_pct", "snapshot_date"]]


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    for eid in UNI_ACTIVE_FUNDS:
        d = fetch_uni_holdings(eid)
        print(f"\n[{eid}] {len(d)} 檔")
        if not d.empty:
            print(d.sort_values("weight_pct", ascending=False).head(10).to_string(index=False))
