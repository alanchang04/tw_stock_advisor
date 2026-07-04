"""解析函式單元測試（不需 DB、不需網路）。跑法：py -3.12 -m pytest tests/ -v"""
import sys, os
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.scrapers.news_scraper import _parse_analysis_line
from data_pipeline.fetchers.revenue_fetcher import _num, latest_published_month
from agent.backtest import _available_rev_month, net_return


# ── 新聞分析行解析 ────────────────────────────────────────────────
def test_parse_full_line():
    r = _parse_analysis_line(
        "[1] 重點：台積電釋出樂觀展望，AI需求強勁。 | 情緒：正面 | 股票：2330,2454",
        "預設")
    assert r["summary"].startswith("台積電")
    assert r["sentiment"] == "positive"
    assert r["stocks"] == ["2330", "2454"]

def test_parse_negative_no_stocks():
    r = _parse_analysis_line("[2] 重點：運價下跌。 | 情緒：負面 | 股票：無", "預設")
    assert r["sentiment"] == "negative"
    assert r["stocks"] == []

def test_parse_empty_line_uses_default():
    r = _parse_analysis_line("", "預設摘要")
    assert r["summary"] == "預設摘要"
    assert r["sentiment"] == "neutral"

def test_parse_garbage_line():
    r = _parse_analysis_line("完全不符合格式的一行", "預設")
    assert r["summary"] == "預設"


# ── 月營收工具 ────────────────────────────────────────────────────
def test_num_parses_commas():
    assert _num("416,975,163") == 416975163.0
    assert _num("-11.87") == -11.87
    assert _num("-") is None
    assert _num("") is None

def test_latest_published_month_after_10th():
    assert latest_published_month(date(2026, 7, 15)) == (2026, 6)

def test_latest_published_month_before_10th():
    assert latest_published_month(date(2026, 7, 4)) == (2026, 5)

def test_latest_published_month_year_rollover():
    assert latest_published_month(date(2026, 1, 5)) == (2025, 11)


# ── 回測 point-in-time 月營收 ─────────────────────────────────────
def test_available_rev_month_mid_month():
    assert _available_rev_month(date(2026, 7, 15)) == "2026-06"

def test_available_rev_month_early_month():
    assert _available_rev_month(date(2026, 7, 4)) == "2026-05"


# ── 交易成本 ──────────────────────────────────────────────────────
def test_net_return_costs_roughly_half_percent():
    gross = 0.0
    net = net_return(100, 100)
    assert -0.006 < net < -0.004   # 一買一賣成本約 0.47%

def test_net_return_positive_trade():
    assert net_return(100, 110) < 0.10   # 淨報酬必小於毛報酬
