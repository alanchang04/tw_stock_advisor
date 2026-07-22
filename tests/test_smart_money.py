"""
聰明資金「投信買超」過濾測試（data_pipeline/analysis/smart_money.py）。

2026-07-22 修真bug：原本 _invest_buying 只看「買超天數 + 正日累計張數」，沒有要求
投信在回看窗內是「淨買」——實測全市場268檔命中裡73檔(27%)其實淨賣，最誇張的群創
顯示「投信買超6天」但淨賣75811張，國巨「投信買超10天」但淨賣14322張。這種散落幾天
綠K的淨賣股被貼上「主力建倉/聰明資金」是嚴重誤導。加 SUM(invest_net)>0 要求整段淨買。

用真實DB交易內回滾（同 test_stock_analysis.py 手法），只驗 _invest_buying 的過濾邏輯。
"""
import os
import sys
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_pipeline.analysis.smart_money as sm


@pytest.fixture
def tx(monkeypatch):
    try:
        from database.connection import get_session_factory
        session = get_session_factory()()
        session.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"DB 無法連線，跳過整合測試：{e}")
    yield session
    session.rollback()
    session.close()


def _seed(tx, sid, rows):
    """rows: list of (days_ago, invest_net_股)。塞近期的投信買賣超資料。"""
    tx.execute(text("INSERT INTO stocks (stock_id, stock_name, market) VALUES (:s, :s, 'TWSE') "
                    "ON CONFLICT DO NOTHING"), {"s": sid})
    tx.execute(text("DELETE FROM institutional_trading WHERE stock_id = :s"), {"s": sid})
    today = date.today()
    for days_ago, inv in rows:
        d = today - timedelta(days=days_ago)
        tx.execute(text("""
            INSERT INTO institutional_trading (stock_id, trade_date, invest_net, foreign_net, total_net)
            VALUES (:s, :d, :inv, 0, :inv)
            ON CONFLICT (stock_id, trade_date) DO UPDATE SET invest_net = EXCLUDED.invest_net
        """), {"s": sid, "d": d, "inv": inv})


def test_net_seller_with_scattered_buy_days_is_excluded(tx):
    # 國巨型態：5天綠K累計正日2670張達門檻，但整段淨賣（大額紅K蓋過）→ 不該入選
    sid = "SMNETSELL"
    _seed(tx, sid, [
        (10, 1_745_000),   # +1745張
        (8, 260_000),      # +260張
        (7, 48_000),       # +48張
        (6, 400_000),      # +400張
        (5, 217_000),      # +217張（正日共5天，累計2670張 > 100張門檻）
        (9, -5_710_000),   # 大額紅K -5710張
        (4, -4_365_000),   # 大額紅K -4365張 → 整段淨賣
    ])
    hits = {r["stock_id"] for r in sm._invest_buying(tx)}
    assert sid not in hits


def test_big_single_buy_day_but_net_sell_is_excluded(tx):
    # 單日爆量買超達500張門檻，但整段仍淨賣 → 也不該入選（大額單日買不能蓋過淨賣事實）
    sid = "SMBIGSELL"
    _seed(tx, sid, [(5, 600_000), (4, -2_000_000)])   # +600張一天，但-2000張淨賣
    hits = {r["stock_id"] for r in sm._invest_buying(tx)}
    assert sid not in hits


def test_all_results_are_net_buyers(tx):
    # 核心不變式：修正後回傳的每一檔，近窗內投信一定是淨買（net_lots>0）——
    # 這條對真實DB也成立，是這次bug修正的本質保證，不受 LIMIT 60 排名影響。
    for r in sm._invest_buying(tx):
        assert (r.get("net_lots") or 0) > 0, f"{r['stock_id']} net_lots={r.get('net_lots')} 不應入選"


def test_genuine_net_buyer_is_included(tx):
    # 真的大額連買 → 入選（用夠高的天數+量體確保排在 LIMIT 60 內，不被真實資料擠掉）
    sid = "SMBIGBUY"
    _seed(tx, sid, [(d, 50_000_000) for d in range(11, 0, -1)])  # 連買11天各5萬張
    rows = {r["stock_id"]: r for r in sm._invest_buying(tx)}
    assert sid in rows
    assert rows[sid]["net_lots"] > 0
    assert rows[sid]["buy_days"] == 11
