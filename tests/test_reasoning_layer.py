"""
推理層改造測試（SPEC_REASONING_LAYER）：餵料層 P0-2/P0-3/2.1 + 裁決問責 guardrail 2.3。
全部純函式測試，不需 DB / 不需 LLM。
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.stock_selector import format_candidates_for_llm
from agent.llm_advisor import (apply_judge_guardrail, _format_debate_for_judge,
                               _parse_json, _repair_truncated_json)


def _df(**over):
    base = dict(stock_id=["6182"], stock_name=["合晶"], industry=["半導體業"],
                close=[38.4], change_pct=[9.86], rsi14=[77.2], macd_hist=[0.5],
                signal_ma_cross=[0], signal_breakout=[1],
                inst_net=[6025929.0], foreign_net=[4087068.0],
                rs20=[0.9964], stack_days=[45.0], invest_streak=[3.0],
                mom60=[3.14], rev_yoy=[31.5])
    base.update(over)
    return pd.DataFrame(base)


# ── P0-2：法人單位 股→張 ──────────────────────────────────────────
def test_inst_net_converted_from_shares_to_lots():
    out = format_candidates_for_llm(_df(), news_map={})
    assert "6,026 張" in out or "6,025 張" in out     # 6,025,929 股 ≈ 6,026 張（四捨五入）
    assert "6025929" not in out                       # 1000× 高估的舊格式不得再出現


# ── 2.1：趨勢/題材因子必須餵給 LLM ────────────────────────────────
def test_factors_are_fed_to_llm():
    out = format_candidates_for_llm(_df(), news_map={})
    assert "相對強度 RS20 全市場第 100 百分位" in out or "第 99 百分位" in out
    assert "多頭排列(MA5>20>60)連續 45 日" in out
    assert "60日動能 +314%" in out
    assert "月營收年增 +31.5%" in out
    assert "投信連買 3 日" in out


def test_news_titles_included_when_available():
    out = format_candidates_for_llm(_df(), news_map={"6182": ["2026-07-10《合晶產能滿載》"]})
    assert "題材：2026-07-10《合晶產能滿載》" in out


# ── P0-3：法人資料缺失時明確標註，不給 0 當論證 ────────────────────
def test_missing_inst_data_is_annotated_not_zero():
    df = _df()
    df.attrs["inst_data_ok"] = False
    out = format_candidates_for_llm(df, news_map={})
    assert "法人買賣超資料缺失" in out            # 頁首警告
    assert "不可據此論證" in out                  # 個股籌碼行標註
    assert "三大法人 +6,02" not in out            # 不再顯示可能是假的數字


# ── 2.3：裁決問責 guardrail ───────────────────────────────────────
def _judge_result():
    return {
        "recommendations": [
            {"rank": 1, "stock_id": "1303", "stock_name": "南亞",
             "reason": "r", "objections_addressed": []},                      # VETO 且無駁回 → 應剔除
            {"rank": 2, "stock_id": "6182", "stock_name": "合晶", "reason": "r",
             "objections_addressed": [{"objection": "RSI 過熱", "verdict": "駁回",
                                       "rebuttal": "多頭排列45日且營收年增31%，趨勢結構完好"}]},  # 有效駁回 → 保留
            {"rank": 3, "stock_id": "5483", "stock_name": "中美晶",
             "reason": "r", "objections_addressed": []},                      # 只被 caution → 不受 guardrail 影響
        ],
        "backups": [
            {"rank": 6, "stock_id": "3374", "stock_name": "精材", "reason": "b",
             "objections_addressed": []},
        ],
    }


def _bear():
    return {"vetoes": [
        {"stock_id": "1303", "severity": "veto", "reason": "外資巨額賣超疑似出貨"},
        {"stock_id": "6182", "severity": "veto", "reason": "RSI 過熱"},
        {"stock_id": "5483", "severity": "caution", "reason": "RSI 偏高"},
    ]}


def test_guardrail_removes_unrebutted_veto_and_promotes_backup():
    result, actions = apply_judge_guardrail(_judge_result(), _bear())
    ids = [r["stock_id"] for r in result["recommendations"]]
    assert "1303" not in ids                     # VETO 無駁回 → 剔除
    assert "6182" in ids                         # VETO 有具體駁回 → 保留
    assert "5483" in ids                         # caution → 不動
    assert "3374" in ids                         # backup 遞補
    assert [r["rank"] for r in result["recommendations"]] == [1, 2, 3]   # 重新編號
    kinds = [a["action"] for a in actions]
    assert "剔除" in kinds and "遞補" in kinds


def test_guardrail_rejects_vetoed_backup():
    res = _judge_result()
    res["backups"] = [{"rank": 6, "stock_id": "1303", "stock_name": "南亞",
                       "reason": "b", "objections_addressed": []}]           # 遞補股同樣被 VETO
    result, actions = apply_judge_guardrail(res, _bear())
    ids = [r["stock_id"] for r in result["recommendations"]]
    assert "1303" not in ids
    assert len(ids) == 2                          # 沒得遞補 → 誠實地少一檔
    assert any(a["action"] == "遞補被拒" for a in actions)


def test_guardrail_noop_without_bear_or_vetoes():
    res = _judge_result()
    r1, a1 = apply_judge_guardrail(res, None)
    assert a1 == [] and len(r1["recommendations"]) == 3
    r2, a2 = apply_judge_guardrail(res, {"vetoes": [
        {"stock_id": "1303", "severity": "caution", "reason": "只是提醒"}]})
    assert a2 == [] and len(r2["recommendations"]) == 3


def test_guardrail_ignores_short_rebuttal():
    res = _judge_result()
    res["recommendations"][0]["objections_addressed"] = [
        {"objection": "外資賣超", "verdict": "駁回", "rebuttal": "沒事"}]     # <10字 → 不算駁回
    result, actions = apply_judge_guardrail(res, _bear())
    assert "1303" not in [r["stock_id"] for r in result["recommendations"]]


# ── 2.2：裁決輸入格式器的三種模式 ─────────────────────────────────
def test_debate_formatter_structured_and_fallback():
    bull = {"raw": "x", "data": {"picks": [{"stock_id": "6182", "stock_name": "合晶",
            "thesis": "多頭排列45日", "preempt_rebuttal": "RSI高但趨勢完好"}]}}
    bear = {"raw": "y", "data": {"vetoes": [{"stock_id": "1303", "stock_name": "南亞",
            "severity": "veto", "reason": "外資賣超"}], "market_concerns": ["族群集中"]}}
    out = _format_debate_for_judge(bull, bear)
    assert "VETO" in out and "1303" in out and "多方自辯" in out and "族群集中" in out
    # 散文 fallback
    out2 = _format_debate_for_judge({"raw": "多方散文", "data": None},
                                    {"raw": "空方散文", "data": None})
    assert "多方散文" in out2 and "空方散文" in out2 and "原文" in out2
    # 全失敗 → 單次模式
    assert _format_debate_for_judge({"raw": None, "data": None},
                                    {"raw": None, "data": None}) == ""


# ── 截斷 JSON 救援（2026-07-14 修：空方被 max_tokens 切斷 → guardrail 靜默失效）──
def test_repair_recovers_truncated_bear_json_unterminated_string():
    """模擬實際故障：空方 JSON 在 market_concerns 陣列中途被切斷（未結束的字串）。"""
    truncated = (
        '{\n "vetoes": [\n'
        '  {"stock_id": "8096", "stock_name": "擎亞", "severity": "veto", "reason": "外資賣超疑似出貨"},\n'
        '  {"stock_id": "1303", "stock_name": "南亞", "severity": "caution", "reason": "RSI偏高"}\n'
        ' ],\n "market_concerns": [\n'
        '  "多數候選來自半導體，族群過度集中",\n'
        '  "部分股票出現外資賣超，如南亞、南電，需留意是否'   # ← 截斷在字串中途
    )
    parsed = _parse_json(truncated)
    assert parsed.get("vetoes"), "應救回 vetoes"
    assert len(parsed["vetoes"]) == 2
    assert parsed["vetoes"][0]["stock_id"] == "8096"
    assert parsed["vetoes"][0]["severity"] == "veto"


def test_repair_drops_incomplete_trailing_element_but_keeps_complete():
    """截斷在最後一個 veto 物件中途 → 丟掉那筆殘缺的，保住前面完整的。"""
    truncated = (
        '{"vetoes": ['
        '{"stock_id": "1111", "severity": "veto", "reason": "跌破月線"},'
        '{"stock_id": "2222", "severity": "veto", "reas'   # ← 截斷在第二筆中途
    )
    parsed = _repair_truncated_json(truncated)
    assert parsed is not None
    ids = [v["stock_id"] for v in parsed.get("vetoes", [])]
    assert "1111" in ids            # 完整的保住


def test_repair_returns_none_for_garbage():
    assert _repair_truncated_json("這不是 JSON，只是一段中文") is None


def test_full_json_still_parses_normally():
    good = '{"vetoes": [{"stock_id": "6182", "severity": "veto", "reason": "x"}], "market_concerns": []}'
    parsed = _parse_json(good)
    assert len(parsed["vetoes"]) == 1


def test_recommendations_salvage_still_works():
    """舊的推薦專用救援不能被新救援搞壞（回歸）。"""
    truncated = ('{"recommendations": ['
                 '{"rank": 1, "stock_id": "2330", "stock_name": "台積電", "reason": "強"},'
                 '{"rank": 2, "stock_id": "2454", "stock_name": "聯發科", "rea')  # 截斷
    parsed = _parse_json(truncated)
    ids = [r["stock_id"] for r in parsed.get("recommendations", [])]
    assert "2330" in ids


def test_recovered_bear_data_actually_drives_guardrail():
    """端對端縮影：截斷的空方 → 救回 → guardrail 真的用救回的 VETO 攔截（這是本次 bug 的核心）。"""
    truncated_bear = (
        '{"vetoes": ['
        '{"stock_id": "1303", "stock_name": "南亞", "severity": "veto", "reason": "外資大額賣超疑似出貨"},'
        '{"stock_id": "6182", "stock_name": "合晶", "severity": "caution", "reason": "RSI偏高但趨勢完整"}'
        '], "market_concerns": ["族群集中在半導體，需留意系統性回檔風'  # ← 截斷
    )
    bear_data = _parse_json(truncated_bear)
    assert bear_data.get("vetoes")                       # 先確認救得回來

    judge_result = {"recommendations": [
        {"rank": 1, "stock_id": "1303", "stock_name": "南亞", "reason": "多頭",
         "objections_addressed": []},                    # 被 VETO 且無駁回 → 應被攔
        {"rank": 2, "stock_id": "6182", "stock_name": "合晶", "reason": "強勢",
         "objections_addressed": []},                    # 只是 caution → 保留
    ], "backups": [
        {"rank": 6, "stock_id": "2330", "stock_name": "台積電", "reason": "備",
         "objections_addressed": []},
    ]}
    result, actions = apply_judge_guardrail(judge_result, bear_data)
    ids = [r["stock_id"] for r in result["recommendations"]]
    assert "1303" not in ids                             # VETO 被攔（修復前完全不會發生）
    assert "6182" in ids
    assert "2330" in ids                                 # backup 遞補
    assert any(a["action"] == "剔除" for a in actions)
