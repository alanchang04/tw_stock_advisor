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
from agent.llm_advisor import apply_judge_guardrail, _format_debate_for_judge


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
