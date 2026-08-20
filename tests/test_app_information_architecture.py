"""UI 資訊層級的回歸測試。

這些斷言刻意鎖住 2026-08-18 外部審閱的 ①②⑤⑥⑦：證據矩陣與首頁要由
權威來源驅動、三種績效不可混回同一條長頁、個股分析必須 decision-first，
無預測力的技術指標不可回到第一層。
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


def test_research_page_is_an_evidence_dashboard_with_foundation_retained():
    source = _source()
    assert 'st.title("🔬 Evidence Dashboard")' in source
    assert 'st.tabs(["🧭 假說證據矩陣", "🧱 資料地基與閘門"])' in source
    assert "dashboard_rows as _dashboard_rows" in source
    assert '_h19["mandatory_caveat"]' in source


def test_home_is_a_decision_cockpit_using_authoritative_sources():
    source = _source()
    assert 'st.title("📈 Decision Cockpit")' in source
    assert "market_regime_detail" in source
    assert "current_strategy_status" in source
    assert "load_forward_status" in source
    assert "pending_order_status" in source
    assert 'd1.metric("Market regime"' in source
    assert 'a4.metric("待成交"' in source


def test_sidebar_groups_cover_every_page_exactly_once():
    """分組不得漏頁或重複——漏了那頁就進不去，重複則 radio 會有兩個相同選項。"""
    import re

    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    block = source[source.index("_PAGE_GROUPS = ["):source.index("_pages = [p for")]
    grouped = re.findall(r'"([^"]+)"\s*(?=[,\]])', block)
    pages = [p for p in grouped if any(ch in p for ch in "📊📋📓📦🔖🔎🎯🔥🏦📉🔄📰🧠🔍⚖🔬")]

    assert len(pages) == len(set(pages)), f"分組有重複頁面：{pages}"
    # 每個 elif page == "..." 的頁面都要出現在分組裡
    handled = set(re.findall(r'^\s*(?:el)?if page == "([^"]+)"', source, re.M))
    missing = handled - set(pages) - {"👤 帳號管理"}
    assert not missing, f"這些頁面有處理區塊但不在導覽分組裡：{missing}"


def test_sidebar_option_strings_are_unchanged_by_grouping():
    """選項字串是 page== 比對鍵、nav_goto 與渲染測試的識別碼，分組不得改動它們。"""
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "captions=_page_captions" in source, "分組靠 captions 呈現，不靠改選項字串"
    assert 'key="nav"' in source, "仍須是同一個 radio，否則 nav_goto 會失效"
