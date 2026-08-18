"""UI 資訊層級的回歸測試。

這些斷言刻意鎖住 2026-08-18 外部審閱的 ②⑤⑥：三種績效不可再混回同一條
長頁、個股分析必須 decision-first、無預測力的技術指標不可回到第一層。
"""
from __future__ import annotations

from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "app.py"


def _source() -> str:
    return APP.read_text(encoding="utf-8-sig")


def test_performance_page_has_three_semantically_distinct_tabs():
    source = _source()
    forward = source.index('"⏩ Forward（實際推薦）"')
    development = source.index('"🧪 Historical Development（歷史開發）"')
    invalid = source.index('"🗄️ Archived-Invalid（作廢封存）"')
    assert forward < development < invalid
    assert "invalidated_artifacts as _invalidated_artifacts" in source


def test_stock_analysis_is_decision_first():
    source = _source()
    assert "colD, colA, colB, colC = st.columns" in source
    assert "🤖 AI 綜合判讀 <span class='dt-badge'>01</span>" in source
    assert "📊 股票背景 <span class='dt-badge'>02</span>" in source


def test_low_evidence_technical_indicators_are_collapsed():
    source = _source()
    assert 'with st.expander("📐 技術指標參考（RSI / KD / MACD）' in source
    assert 'with st.expander("📐 RSI / MACD 參考（非主要證據）' in source
