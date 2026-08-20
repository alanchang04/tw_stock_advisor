"""
data_pipeline/fetchers/margin_fetcher.py

融資融券（信用交易）抓取——2026-07-23 新增。

**目前定位是「研究用資料」，還沒有接進每日 pipeline，也還沒進評分公式。**
使用者問「策略是否該考慮融資融券」，照 P1 的教訓（rs20/動能這種「聽起來合理」的
因子實測 IC 是負的），流程必須是：先抓資料 → 進 research/factor_lab 測 10 年 IC →
**確認真的有預測力才接進 STRATEGY**，不是憑直覺加。

資料源：TWSE 融資融券彙總（個股）
    https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD&selectType=STOCK
    實測 10 年前（2016）的資料同樣拿得到，適合做長期 IC 研究。
    回傳表格欄位（融資6欄 + 融券6欄 + 資券互抵 + 註記）：
      代號, 名稱,
      [融資] 買進, 賣出, 現金償還, 前日餘額, 今日餘額, 次一營業日限額,
      [融券] 買進, 賣出, 現券償還, 前日餘額, 今日餘額, 次一營業日限額,
      資券互抵, 註記
    單位：交易單位（張）。

已知限制（誠實記錄）：
  - 只有上市（TWSE）。上櫃（TPEX）本機受憑證問題擋住（www.tpex.org.tw 憑證缺
    Subject Key Identifier，Python 3.14 嚴格驗證會拒絕），研究階段先做上市；
    策略候選池以上市股為主，初步 IC 判斷夠用，但這個偏誤要記在結論裡。
  - 第一列是「合計」，要濾掉（代號為全形空白）。
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd
import requests
from loguru import logger

URL_TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _to_num(v) -> float | None:
    """'1,234' → 1234.0；'--'/空字串/None → None。"""
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if not s or s in ("--", "---", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_margin_table(rows: list, trade_date: date) -> pd.DataFrame:
    """把 TWSE 回傳的個股融資融券列表轉成標準 DataFrame（純函式，方便測試）。

    欄位對應（0-based）：0代號 1名稱｜融資 2買進 3賣出 4現金償還 5前日餘額 6今日餘額
    ｜融券 8買進 9賣出 10現券償還 11前日餘額 12今日餘額
    餘額變化 = 今日餘額 - 前日餘額（直接相減，比自行加總買賣更不易受欄位定義影響）。
    """
    out = []
    for r in rows or []:
        if not r or len(r) < 13:
            continue
        sid = str(r[0]).strip()
        if not sid or not sid[0].isdigit():      # 濾掉「合計」列（代號是全形空白）
            continue
        m_prev, m_today = _to_num(r[5]), _to_num(r[6])
        s_prev, s_today = _to_num(r[11]), _to_num(r[12])
        out.append({
            "stock_id": sid,
            "trade_date": trade_date,
            "margin_balance": m_today,
            "margin_change": (m_today - m_prev) if (m_today is not None and m_prev is not None) else None,
            "short_balance": s_today,
            "short_change": (s_today - s_prev) if (s_today is not None and s_prev is not None) else None,
        })
    return pd.DataFrame(out)


def fetch_margin_twse_by_date(d: date, timeout: int = 30, retries: int = 3) -> pd.DataFrame:
    """抓指定日期全上市個股融資融券。假日/無資料回空 DataFrame（不是錯誤）。"""
    params = {"date": d.strftime("%Y%m%d"), "selectType": "STOCK", "response": "json"}
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(URL_TWSE_MARGIN, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if j.get("stat") != "OK":
                return pd.DataFrame()                 # 假日或該日無資料
            # 個股表在 tables 裡（標題含「股票」），非個股表（全市場彙總）要跳過
            for t in j.get("tables") or []:
                data = t.get("data") or []
                fields = t.get("fields") or []
                if len(fields) >= 13 and data:
                    return parse_margin_table(data, d)
            return pd.DataFrame()
        except Exception as e:
            last_err = e
            logger.warning(f"融資融券抓取失敗（第 {attempt}/{retries} 次）{d}: {str(e)[:120]}")
            time.sleep(3 * attempt)
    logger.error(f"融資融券 {d} 最終失敗: {str(last_err)[:150]}")
    return pd.DataFrame()
