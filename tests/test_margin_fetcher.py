"""
融資融券抓取解析測試（data_pipeline/fetchers/margin_fetcher.py，2026-07-23 新增）。

純函式測試，不打網路。重點在幾個實際會咬人的地方：
  - TWSE 回傳的第一列是「合計」（代號欄是全形空白），必須濾掉，否則會被當成一檔股票
  - 數字帶千分位逗號；停牌/無資料是 '--'
  - 餘額變化用「今日餘額 - 前日餘額」直接相減（比自行加總買賣不易受欄位定義影響）

⚠️ 這批資料目前只是**研究用**，還沒接進每日 pipeline、也還沒進評分公式——
要先用 research/factor_lab 測 10 年 IC，確認真有預測力才談納入（P1 的教訓：
rs20/動能這種「聽起來合理」的因子實測 IC 是負的）。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.margin_fetcher import _to_num, parse_margin_table

# 欄位順序：0代號 1名稱｜融資 2買進 3賣出 4現金償還 5前日餘額 6今日餘額 7限額
#          ｜融券 8買進 9賣出 10現券償還 11前日餘額 12今日餘額 13限額 14資券互抵 15註記
_TOTAL_ROW = ["　", "合計", "283,667", "248,952", "3,248", "6,804,716", "6,836,183",
              "191,794,596", "1", "2", "3", "4", "5", "6", "7", ""]
_TSMC_ROW = ["2330", "台積電", "1,000", "895", "0", "31,823", "31,928", "999,999",
             "10", "9", "0", "98", "99", "888", "0", ""]


def test_to_num_handles_commas_and_placeholders():
    assert _to_num("1,234") == 1234.0
    assert _to_num("--") is None
    assert _to_num("") is None
    assert _to_num(None) is None
    assert _to_num("0") == 0.0


def test_parse_skips_total_row():
    df = parse_margin_table([_TOTAL_ROW, _TSMC_ROW], date(2026, 7, 22))
    assert len(df) == 1
    assert df.iloc[0]["stock_id"] == "2330"


def test_parse_computes_balance_and_change():
    df = parse_margin_table([_TSMC_ROW], date(2026, 7, 22))
    r = df.iloc[0]
    assert r["margin_balance"] == 31928.0
    assert r["margin_change"] == 105.0        # 31,928 - 31,823
    assert r["short_balance"] == 99.0
    assert r["short_change"] == 1.0           # 99 - 98
    assert r["trade_date"] == date(2026, 7, 22)


def test_parse_missing_balance_gives_none_change():
    row = list(_TSMC_ROW)
    row[5] = "--"                              # 前日餘額缺 → 變化算不出來
    df = parse_margin_table([row], date(2026, 7, 22))
    assert df.iloc[0]["margin_balance"] == 31928.0
    assert df.iloc[0]["margin_change"] is None


def test_parse_empty_and_malformed_rows():
    assert parse_margin_table([], date(2026, 7, 22)).empty
    assert parse_margin_table(None, date(2026, 7, 22)).empty
    assert parse_margin_table([["2330", "台積電"]], date(2026, 7, 22)).empty   # 欄位不足
