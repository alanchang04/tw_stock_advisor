"""
data_pipeline/fetchers/tdcc_fetcher.py

TDCC 集保股權分散——大戶持股集中度（2026-07-26，SPEC_QUANT_UPGRADE §2.6）。

**為什麼做這個**：規格書 §2.6 把「集保股權分散（TDCC 週更、免費）」標為「大戶持股
集中度變化是中小型股『聰明錢』偵測的正宗資料」。使用者獨立想到同一件事（觀察大戶
是否默默進場）。

**但這不是驗證過的因子——是決策支援訊號。** 誠實限制（2026-07-26 探測確認）：
  - OpenData（免費）只給**當週**；smWeb 免費歷史只到 ~51 週（~1 年）；
    10 年歷史要 FinMind 付費層。
  - 1 年 = 單一 régime = §0.1 警告的「13 個月污染窗」→ **現在無法做嚴謹回測因子研究。**
  → 策略：**向前累積**（每週存一次，慢慢建乾淨歷史，1~2 年後才夠做研究）+
    同時把當前值當「未驗證訊號」給人看。用而不信，信要等資料。

資料源：https://opendata.tdcc.com.tw/getOD.ashx?id=1-5（CSV，全市場約 4000 檔）
  欄位：資料日期, 證券代號, 持股分級(1~17), 人數, 股數, 占集保庫存數比例%
  持股分級：15＝1,000,001股以上（千張大戶）；12~15＝400,001股以上（400張以上）；
            17＝合計。大戶集中度＝級15 占比%。

⚠️ SSL：opendata.tdcc.com.tw 憑證缺 Subject Key Identifier，Python 3.14 嚴格驗證
會拒（同 TPEX）。此端點為**公開、無憑證、無敏感資料**，故用 verify=False 繞過；
不傳任何帳密，研究資料用途下風險可接受。
"""
from __future__ import annotations
import csv
import io
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from datetime import date

import requests
import urllib3
from loguru import logger
from sqlalchemy import text

from database.connection import get_session

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

URL_TDCC_OD = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TIMEOUT = 60

_BIG_LEVEL = 15          # 千張以上大戶
_GT400_LEVELS = {12, 13, 14, 15}   # 400張以上（含大戶）


def _num(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def parse_tdcc_csv(csv_text: str) -> list[dict]:
    """TDCC OpenData CSV → 每檔一列的大戶集中度彙總（純函式，好測）。

    只留 4 位數個股代號（權證/6位數不在候選池）。回傳欄位：
      data_date, stock_id, big_holder_pct(級15占比), big_holder_count(級15人數),
      gt400_pct(級12~15占比合計), total_holders(級1~15人數合計).
    """
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        return []
    # 欄位名可能因 BOM/全半形略異，用「包含」比對抓
    keys = rows[0].keys()
    k_date = next((k for k in keys if "日期" in k), None)
    k_code = next((k for k in keys if "代號" in k), None)
    k_lvl = next((k for k in keys if "分級" in k), None)
    k_cnt = next((k for k in keys if "人數" in k), None)
    k_pct = next((k for k in keys if "比例" in k), None)
    if not all([k_date, k_code, k_lvl, k_cnt, k_pct]):
        logger.warning(f"TDCC CSV 欄位對不上：{list(keys)}")
        return []

    agg: dict[str, dict] = {}
    for r in rows:
        sid = str(r[k_code]).strip()
        if not (sid.isdigit() and len(sid) == 4):
            continue
        try:
            lvl = int(str(r[k_lvl]).strip())
        except (TypeError, ValueError):
            continue
        d = agg.setdefault(sid, {
            "data_date": _roc_or_ad(r[k_date]), "stock_id": sid,
            "big_holder_pct": None, "big_holder_count": None,
            "gt400_pct": 0.0, "total_holders": 0})
        pct, cnt = _num(r[k_pct]), _num(r[k_cnt])
        if lvl == _BIG_LEVEL:
            d["big_holder_pct"] = pct
            d["big_holder_count"] = int(cnt) if cnt is not None else None
        if lvl in _GT400_LEVELS and pct is not None:
            d["gt400_pct"] += pct
        if 1 <= lvl <= 15 and cnt is not None:
            d["total_holders"] += int(cnt)
    # gt400_pct 四捨五入、total_holders 為 0 視為缺
    out = []
    for d in agg.values():
        d["gt400_pct"] = round(d["gt400_pct"], 2)
        if d["total_holders"] == 0:
            d["total_holders"] = None
        out.append(d)
    return out


def _roc_or_ad(s) -> date | None:
    """TDCC 日期是西元 YYYYMMDD（例 20260724）。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def fetch_tdcc() -> list[dict]:
    """抓當週 TDCC 股權分散並彙總（網路 + 解析）。"""
    r = requests.get(URL_TDCC_OD, headers=_UA, timeout=_TIMEOUT, verify=False)
    r.raise_for_status()
    return parse_tdcc_csv(r.content.decode("utf-8-sig"))


def ensure_tdcc_table():
    with get_session() as s:
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS tdcc_holdings (
                data_date        DATE        NOT NULL,
                stock_id         VARCHAR(10) NOT NULL,
                big_holder_pct   NUMERIC(6,2),   -- 級15 千張大戶 占比%
                big_holder_count INTEGER,        -- 級15 人數
                gt400_pct        NUMERIC(6,2),   -- 級12~15（400張以上）占比合計%
                total_holders    INTEGER,        -- 總股東人數（級1~15 人數合計）
                created_at       TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (data_date, stock_id)
            )
        """))
        s.execute(text("CREATE INDEX IF NOT EXISTS idx_tdcc_stock "
                       "ON tdcc_holdings (stock_id, data_date)"))
        s.commit()


def _latest_stored_date():
    with get_session() as s:
        return s.execute(text("SELECT MAX(data_date) FROM tdcc_holdings")).scalar()


def update_tdcc(force: bool = False) -> int:
    """抓當週 → 若該週尚未入庫則存（週更，每日 pipeline 呼叫也只會存一次）。

    回傳新寫入的筆數（0＝本週已存或無資料）。失敗不拋出，回 0 + log。
    """
    try:
        ensure_tdcc_table()
        recs = fetch_tdcc()
        if not recs:
            logger.info("TDCC：本次無資料")
            return 0
        this_week = recs[0]["data_date"]
        if not force and _latest_stored_date() == this_week:
            logger.info(f"TDCC：{this_week} 已入庫，略過")
            return 0
        stmt = text("""
            INSERT INTO tdcc_holdings
                (data_date, stock_id, big_holder_pct, big_holder_count, gt400_pct, total_holders)
            VALUES (:data_date, :stock_id, :big_holder_pct, :big_holder_count,
                    :gt400_pct, :total_holders)
            ON CONFLICT (data_date, stock_id) DO UPDATE SET
                big_holder_pct = EXCLUDED.big_holder_pct,
                big_holder_count = EXCLUDED.big_holder_count,
                gt400_pct = EXCLUDED.gt400_pct,
                total_holders = EXCLUDED.total_holders
        """)
        with get_session() as s:
            s.execute(stmt, recs)
            s.commit()
        logger.info(f"TDCC：{this_week} 存入 {len(recs)} 檔大戶集中度")
        return len(recs)
    except Exception as e:
        logger.warning(f"TDCC 更新失敗（不擋流程）: {e}")
        return 0


def load_big_holder_trend(stock_id: str, weeks: int = 8) -> list[dict]:
    """讀單檔近 N 週的大戶集中度（App SOP 面板用）。最新在前。"""
    with get_session() as s:
        rows = s.execute(text("""
            SELECT data_date, big_holder_pct, big_holder_count, gt400_pct, total_holders
            FROM tdcc_holdings WHERE stock_id = :sid
            ORDER BY data_date DESC LIMIT :n
        """), {"sid": stock_id, "n": weeks}).fetchall()
    return [{"data_date": r[0], "big_holder_pct": _num(r[1]), "big_holder_count": r[2],
             "gt400_pct": _num(r[3]), "total_holders": r[4]} for r in rows]


if __name__ == "__main__":
    update_tdcc()
