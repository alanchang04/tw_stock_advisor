"""
agent/citation_check.py

Phase C1（docs/SPEC_PIPELINE_IMPROVEMENTS.md）：引用驗證，防幻覺主力的第一步。
2026-07-21新增。純規則檢查，不呼叫LLM、不重新生成——只標記、不攔截（攔截需要
重跑裁決，屬於之後C2/best-of-N才要做的範疇；這裡先做「量測」那一半，跟C2/D
同樣的「先measure再決定要不要投資更複雜的機制」邏輯）。

原理：agent.stock_selector.format_candidates_for_llm() 把每檔候選股票的數字用
固定精度格式化餵給LLM（RSI/RS20百分位/60日動能%/月營收年增%/法人張數/投信連買
日數...），理論上裁決/辯論理由裡複述的數字都應該能在這個集合裡找到（容差內）。
抽出理由文字裡「帶單位」的數字，跟這個集合比對，抓不到對應的視為可疑——
提示使用者去看payload原文，不是自動判死刑（也不足以自動判死刑：見下方限制）。

**誠實聲明（v1已知限制）**：
1. 不做「數字對應到哪個欄位」的精確歸屬，只檢查「這個數字有沒有出現在這檔股票
   任何一個合法欄位值附近」——所以無法抓到「把A股票的數字安到B欄位」這種同數值
   但欄位張冠李戴的錯誤，只能抓「這個數字在候選資料裡完全找不到出處」的情況。
2. 只檢查帶明確單位（%/百分位/日/張）的數字，不含股價、RSI裸數字、題材類金額
   （億/萬/元——這些來自新聞內容，不是結構化候選欄位，本來就不該被要求逐字對應
   候選表）。範圍刻意保守，寧可漏抓也不要抓一堆假警報。
3. 提示語模板裡「60日動能」這個窗口長度標籤本身含「60」+「日」，容易被誤判成
   候選資料裡的天數欄位，已用字串排除處理；如果之後改了模板措辭要記得同步檢查。
"""
from __future__ import annotations
import re
import pandas as pd

# 只認帶這幾種單位的數字——都對應候選資料裡的結構化欄位（見下方 expected_numbers）
_NUM_UNIT = re.compile(r"([+\-−]?\d[\d,]*\.?\d*)\s*(%|百分位|日|張)")

# 提示模板固定窗口長度標籤，"60"+"日"會被regex誤判成天數類欄位，抽數字前先拿掉。
# 真實資料驗證抓到LLM有時寫「60日動能」有時寫「60 日動能」（中間帶空格），
# 字串完全比對會漏抓後者——真跑過一次歷史資料才發現，改用正則涵蓋空格變體。
_FIXED_WINDOW_RE = re.compile(r"60\s*日動能")


def expected_numbers(row: pd.Series) -> set[float]:
    """這檔股票在 format_candidates_for_llm() 裡合法出現過的數字集合（同樣精度）。"""
    vals: set[float] = set()

    def add(v, ndigits=1):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v != v:  # NaN
            return
        vals.add(round(v, ndigits))

    add(row.get("rsi14"), 1)
    add(row.get("change_pct"), 2)
    rs = row.get("rs20")
    if rs is not None and pd.notna(rs):
        add(float(rs) * 100, 0)
    add(row.get("stack_days"), 0)
    mom = row.get("mom60")
    if mom is not None and pd.notna(mom):
        add(float(mom) * 100, 0)
    add(row.get("rev_yoy"), 1)
    inst = row.get("inst_net")
    if inst is not None and pd.notna(inst):
        add(float(inst) / 1000, 0)
    frn = row.get("foreign_net")
    if frn is not None and pd.notna(frn):
        add(float(frn) / 1000, 0)
    add(row.get("invest_streak"), 0)
    return vals


def extract_cited_numbers(text: str) -> list[float]:
    """抽出理由文字裡帶單位（%/百分位/日/張）的數字。"""
    if not text:
        return []
    cleaned = _FIXED_WINDOW_RE.sub("", text)
    out = []
    for m in _NUM_UNIT.finditer(cleaned):
        raw = m.group(1).replace(",", "").replace("−", "-")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def check_grounding(row: pd.Series, text: str, tolerance: float = 1.5) -> list[float]:
    """回傳文字裡「候選資料完全找不到對應」的數字（容差內都算通過）。
    用絕對值比對——LLM常用「買超/賣超」等中文詞表達方向，數字本身不帶負號，
    跟資料庫裡帶正負號的張數直接比對會誤判，改比較量級。"""
    expected = expected_numbers(row)
    if not expected:
        return []
    expected_abs = {abs(e) for e in expected}
    flagged = []
    for v in extract_cited_numbers(text):
        if any(abs(abs(v) - e) <= tolerance for e in expected_abs):
            continue
        flagged.append(v)
    return flagged


def annotate_recommendations(result: dict, candidates: pd.DataFrame | None) -> dict:
    """幫 result['recommendations']/['backups'] 每筆加上 grounding_flags 欄位——
    不攔截、不改變裁決結果，只是多一個標記供 UI 顯示⚠️與未來統計可疑率用。"""
    if not result or candidates is None or candidates.empty:
        return result
    by_id = {str(r["stock_id"]): r for _, r in candidates.iterrows()}
    for key in ("recommendations", "backups"):
        for item in result.get(key) or []:
            row = by_id.get(str(item.get("stock_id")))
            if row is None:
                continue
            item["grounding_flags"] = check_grounding(row, item.get("reason", ""))
    return result


def annotate_debate_coverage(result: dict, bull_pack: dict | None) -> dict:
    """幫 result['recommendations'] 每筆標記是否曾被多方研究員主張過
    （not_debated=True 代表沒有）。

    2026-07-21 真實案例：裁決結果裡出現玉山金，使用者一度以為是幻覺——追查後
    數字全部真實存在於候選資料（check_grounding驗證通過），只是：(1) 多方只從
    20檔候選挑5檔主張，玉山金沒被選中；(2) 空方也沒對它提異議（沉默，不代表
    「沒看到」）；(3) 決策軌跡頁UI只顯示候選池前8名，玉山金排在候選池外沒被
    顯示。裁決本來就看得到完整候選資料、被系統提示要求「從候選名單挑最值得
    關注的5支」，不是只能選多方主張過的股票，所以這是設計允許的行為，不是bug——
    但代表這筆推薦少了一層辯論雙方的檢視。這裡不攔截、只標記，讓使用者自己判斷
    要不要對「未經辯論」的推薦更謹慎。backups不標記（backups本來就是裁決自己
    找的候補，多方不太可能主張過，標記了也沒有額外資訊量）。
    """
    if not result or not bull_pack:
        return result
    bull_picks = {str(p.get("stock_id")) for p in (bull_pack.get("data") or {}).get("picks", [])}
    if not bull_picks:
        return result
    for item in result.get("recommendations") or []:
        item["not_debated"] = str(item.get("stock_id")) not in bull_picks
    return result
