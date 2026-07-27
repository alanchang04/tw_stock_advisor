"""TDCC 集保大戶集中度——parse_tdcc_csv 純函式測試（不碰網路/DB）。

用真實抓下來的欄位格式當樣本：BOM 已在呼叫端剝掉、代號右補空白到 6 碼、
持股分級 1~17、占比欄名是「占」不是「佔」。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_pipeline.fetchers.tdcc_fetcher import parse_tdcc_csv

# 一檔股票（2330）的簡化 15+2 級樣本；代號故意帶尾端空白（真實格式）
_HEADER = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%"


def _csv(*lines):
    return _HEADER + "\n" + "\n".join(lines) + "\n"


def _stock_15(sid="2330  ", d="20260724"):
    """級1~11 各 1%、級12~14 各 2%、級15 大戶 79%、級16 差異 0、級17 合計 100。"""
    lines = []
    for lvl in range(1, 12):
        lines.append(f"{d},{sid},{lvl},100,1000,1.00")
    for lvl in range(12, 15):
        lines.append(f"{d},{sid},{lvl},10,2000,2.00")
    lines.append(f"{d},{sid},15,3,790000,79.00")       # 大戶
    lines.append(f"{d},{sid},16,0,0,0.00")             # 差異數
    lines.append(f"{d},{sid},17,1103,1000000,100.00")  # 合計
    return lines


def test_big_holder_pct_is_level_15():
    out = parse_tdcc_csv(_csv(*_stock_15()))
    assert len(out) == 1
    r = out[0]
    assert r["stock_id"] == "2330"                 # 尾端空白已剝除
    assert r["big_holder_pct"] == 79.00            # 級15 占比
    assert r["big_holder_count"] == 3              # 級15 人數


def test_gt400_sums_levels_12_to_15():
    r = parse_tdcc_csv(_csv(*_stock_15()))[0]
    # 級12~14 各 2% + 級15 79% = 85%
    assert r["gt400_pct"] == 85.00


def test_total_holders_sums_levels_1_to_15_only():
    """合計人數＝級1~15 之和（不含級16差異/級17合計，避免重複計）。"""
    r = parse_tdcc_csv(_csv(*_stock_15()))[0]
    # 級1~11：11×100=1100；級12~14：3×10=30；級15：3 → 1133
    assert r["total_holders"] == 1133


def test_date_parsed_as_ad():
    r = parse_tdcc_csv(_csv(*_stock_15()))[0]
    assert r["data_date"] == date(2026, 7, 24)


def test_only_4digit_stock_codes_kept():
    """權證/ETN 6 位數、ETF 以外的非 4 碼一律剔除。"""
    lines = _stock_15("2330  ") + _stock_15("083764", "20260724")  # 後者 6 碼權證
    out = parse_tdcc_csv(_csv(*lines))
    assert {r["stock_id"] for r in out} == {"2330"}


def test_column_name_uses_zhan_not_jian():
    """占集保庫存數比例%（占）——欄名比對用『包含比例』抓，不寫死。"""
    header_variant = "資料日期,證券代號,持股分級,人數,股數,佔集保庫存數比例%"  # 佔
    txt = header_variant + "\n" + "\n".join(_stock_15()) + "\n"
    r = parse_tdcc_csv(txt)[0]
    assert r["big_holder_pct"] == 79.00


def test_empty_input():
    assert parse_tdcc_csv("") == []
    assert parse_tdcc_csv(_HEADER + "\n") == []


def test_missing_level_15_gives_none():
    """沒有級15 資料的股票，大戶占比為 None（不要當成 0 誤導）。"""
    lines = [f"20260724,9999  ,{lvl},100,1000,10.00" for lvl in range(1, 11)]
    r = parse_tdcc_csv(_csv(*lines))[0]
    assert r["big_holder_pct"] is None
    assert r["big_holder_count"] is None
