"""
agent/citation_check.py 測試（Phase C1引用驗證，2026-07-21新增）。

純函式，不需要DB/LLM——驗證「把候選資料格式化給LLM的數字」跟「裁決/辯論理由文字
裡複述的數字」比對邏輯本身是對的。真正的價值要等實際pipeline跑一陣子、累積
grounding_flags出現頻率之後才看得出來（跟C2/D同樣的「先量測」邏輯），這裡先確保
比對機制本身沒有明顯漏洞（尤其是之前發現的「60日動能」這個固定窗口標籤誤判成
天數欄位、以及法人買賣超中文措辭不帶負號的假警報這兩個坑）。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.citation_check import (
    annotate_debate_coverage,
    annotate_recommendations,
    check_grounding,
    expected_numbers,
    extract_cited_numbers,
)


def _row(**kwargs):
    base = {
        "stock_id": "5434", "rsi14": 71.4, "change_pct": 1.39,
        "rs20": 0.96, "stack_days": 16, "mom60": 0.29,
        "rev_yoy": 32.9, "inst_net": 5_000_000, "foreign_net": -1_017_000,
        "invest_streak": 11,
    }
    base.update(kwargs)
    return pd.Series(base)


def test_expected_numbers_converts_units_correctly():
    exp = expected_numbers(_row())
    assert 96 in exp          # rs20 0.96 -> *100
    assert 16 in exp          # stack_days as-is
    assert 29 in exp          # mom60 0.29 -> *100
    assert 32.9 in exp        # rev_yoy as-is
    assert 5000 in exp        # inst_net 股 -> 張 (÷1000)
    assert -1017 in exp       # foreign_net 股 -> 張，保留負號
    assert 11 in exp          # invest_streak


def test_extract_ignores_fixed_window_label():
    # "60日動能"是提示模板固定窗口長度標籤，"60"不該被當成候選資料裡的天數欄位
    text = "其60日動能高達+131%，展現強勁成長"
    cited = extract_cited_numbers(text)
    assert 60 not in cited
    assert 131 in cited


def test_extract_ignores_fixed_window_label_with_space():
    # 真實資料驗證抓到：LLM有時寫「60 日動能」中間帶空格，字串完全比對會漏抓
    text = "60 日動能 +129%，且三大法人持續買超"
    cited = extract_cited_numbers(text)
    assert 60 not in cited
    assert 129 in cited


def test_extract_ignores_numbers_without_recognized_unit():
    # 裸數字（股價、RSI不帶單位）不在檢查範圍內，避免大量假警報
    text = "RSI已達71.4，股價來到2600.0元"
    assert extract_cited_numbers(text) == []


def test_check_grounding_passes_legit_numbers():
    row = _row()
    text = "相對強度RS20位於全市場前96百分位，已連續16日多頭排列，投信連買11日，月營收年增32.9%"
    assert check_grounding(row, text) == []


def test_check_grounding_flags_fabricated_number():
    row = _row()
    text = "投信已連續買超40日，展現高度認同"   # 實際 invest_streak=11，40明顯查無對應
    flagged = check_grounding(row, text)
    assert 40 in flagged


def test_check_grounding_handles_direction_word_without_sign():
    # 中文用「賣超」表達方向，數字本身常不帶負號；DB裡foreign_net是負值，
    # 用絕對值比對才不會誤判成幻覺
    row = _row()
    text = "外資賣超1,017張，法人分歧"
    assert check_grounding(row, text) == []


def test_annotate_recommendations_adds_flags_by_stock_id():
    candidates = pd.DataFrame([_row().to_dict(), _row(stock_id="6414", invest_streak=2).to_dict()])
    result = {
        "recommendations": [
            {"stock_id": "5434", "reason": "投信連買11日，表現穩健"},
            {"stock_id": "6414", "reason": "投信連買50日，展現長期買盤"},
        ],
        "backups": [{"stock_id": "9999", "reason": "候選資料裡沒有這檔，不該爆炸"}],
    }
    out = annotate_recommendations(result, candidates)
    assert out["recommendations"][0]["grounding_flags"] == []
    assert 50 in out["recommendations"][1]["grounding_flags"]
    assert "grounding_flags" not in out["backups"][0]


def test_annotate_recommendations_noop_when_candidates_none():
    result = {"recommendations": [{"stock_id": "5434", "reason": "x"}]}
    out = annotate_recommendations(result, None)
    assert "grounding_flags" not in out["recommendations"][0]


def test_annotate_debate_coverage_flags_pick_bull_never_argued_for():
    # 2026-07-21 真實案例重演：玉山金(2884)在候選資料裡、裁決選中了，但多方只
    # 主張了另外5檔，玉山金不在其中，裁決自己的backups(台積電/神達)也完全沒用——
    # 應該被標記not_debated=True且更嚴重的bypassed_backups=True
    result = {
        "recommendations": [
            {"stock_id": "2059", "reason": "..."},
            {"stock_id": "2884", "reason": "..."},
        ],
        "backups": [{"stock_id": "2330"}, {"stock_id": "3706"}],
    }
    bull_pack = {"data": {"picks": [{"stock_id": "2059"}, {"stock_id": "1303"},
                                     {"stock_id": "2615"}, {"stock_id": "3479"},
                                     {"stock_id": "2395"}]}}
    out = annotate_debate_coverage(result, bull_pack)
    assert out["recommendations"][0]["not_debated"] is False
    assert out["recommendations"][0]["bypassed_backups"] is False
    assert out["recommendations"][1]["not_debated"] is True
    assert out["recommendations"][1]["bypassed_backups"] is True


def test_annotate_debate_coverage_not_bypassed_when_pick_is_in_own_backups():
    # 沒被多方主張，但裁決至少是從自己列的backups遞補的——不算「跳過backups」
    result = {
        "recommendations": [{"stock_id": "2330", "reason": "..."}],
        "backups": [{"stock_id": "2330"}, {"stock_id": "3706"}],
    }
    bull_pack = {"data": {"picks": [{"stock_id": "2059"}]}}
    out = annotate_debate_coverage(result, bull_pack)
    assert out["recommendations"][0]["not_debated"] is True
    assert out["recommendations"][0]["bypassed_backups"] is False


def test_annotate_debate_coverage_not_bypassed_when_backups_empty():
    # backups本身是空的（沒東西可以跳過），不該誤標bypassed_backups
    result = {
        "recommendations": [{"stock_id": "2884", "reason": "..."}],
        "backups": [],
    }
    bull_pack = {"data": {"picks": [{"stock_id": "2059"}]}}
    out = annotate_debate_coverage(result, bull_pack)
    assert out["recommendations"][0]["not_debated"] is True
    assert out["recommendations"][0]["bypassed_backups"] is False


def test_annotate_debate_coverage_noop_when_bull_pack_missing():
    result = {"recommendations": [{"stock_id": "2884", "reason": "..."}]}
    out = annotate_debate_coverage(result, None)
    assert "not_debated" not in out["recommendations"][0]
    out2 = annotate_debate_coverage(result, {"data": None})
    assert "not_debated" not in out2["recommendations"][0]


def test_annotate_debate_coverage_does_not_touch_backups():
    result = {
        "recommendations": [{"stock_id": "2059", "reason": "..."}],
        "backups": [{"stock_id": "2330", "reason": "..."}],
    }
    bull_pack = {"data": {"picks": [{"stock_id": "2059"}]}}
    out = annotate_debate_coverage(result, bull_pack)
    assert "not_debated" not in out["backups"][0]
