"""
agent/llm_advisor.py

用 LiteLLM 接 Gemini 2.5 Flash，產生每日選股推薦報告
支援未來切換到 Claude 或其他模型（只需改 MODEL 設定）
"""
import json
import re
import time
from datetime import date
from loguru import logger
import litellm
from litellm import completion
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config.settings import APIConfig
from database.connection import get_session

# 不支援的參數（如某些模型不吃 response_format）自動忽略，不丟例外
litellm.drop_params = True

# ── 模型設定（改這裡就能切換模型）────────────────────────────────
MODEL = "gemini/gemini-2.5-flash"

# 多空辯論模式（TradingAgents 概念輕量版）：
#   多方研究員 → 空方研究員 → 裁決（原本的選股呼叫附上兩方觀點）
#   每天多 2 次 Gemini 呼叫（約 5K token），失敗自動退回單次呼叫模式
DEBATE_MODE = os.getenv("LLM_DEBATE", "1") == "1"

# 設定 API Key
os.environ["GEMINI_API_KEY"] = APIConfig.GEMINI_KEY

SYSTEM_PROMPT = """你是首席投資長（CIO），要在內部多空辯論後做最終裁決。

你的任務：從候選名單挑出最值得關注的 5 支股票。裁決紀律（SPEC_REASONING_LAYER 2.3）：
1. 空方對某檔提出的每一條異議，你都必須明確回應：「接受」（該檔不選/降級）或
   「駁回」（必須引用候選資料中的具體數據說明為何異議不成立）。
2. 空方標記 VETO（絕對不該碰）的股票，除非你能以具體數據駁回，否則不得入選。
3. 只照抄多方名單而不逐條回應空方異議＝無效輸出。系統會用程式檢查：被 VETO 且
   無具體駁回的入選股會被自動剔除，由 backups 遞補——所以請認真提供 backups。
4. 論證多樣性：理由須綜合趨勢結構（多頭排列天數）、基本面（營收）、
   籌碼（法人/投信）、題材（新聞），不可只複述當日漲跌幅與 RSI。
   注意：候選資料中的「相對強度 RS20」「60日動能」經統計研究證實在目前持有天數
   與後續報酬負相關（追高風險），只能當作參考背景，不可當作進場理由的主要依據。

回覆格式請嚴格按照以下 JSON 格式，不要有其他文字：
{
  "date": "YYYY-MM-DD",
  "recommendations": [
    {
      "rank": 1,
      "stock_id": "股票代號",
      "stock_name": "股票名稱",
      "reason": "推薦理由（120字以內，須含至少兩類證據：趨勢結構/基本面/籌碼/題材）",
      "key_signals": ["訊號1", "訊號2"],
      "risk_note": "注意事項（30字以內）",
      "objections_addressed": [
        {"objection": "空方對此檔的異議摘要（無異議時此列表為空）",
         "verdict": "接受 或 駁回",
         "rebuttal": "駁回時必填：引用具體數據的反駁；接受時留空"}
      ]
    }
  ],
  "backups": [
    {"rank": 6, "stock_id": "...", "stock_name": "...", "reason": "...",
     "key_signals": [], "risk_note": "...", "objections_addressed": []}
  ],
  "market_summary": "今日大盤族群輪動簡評（50字以內）"
}
backups 為遞補名單（最多 2 檔，結構同上）：當入選股因未妥善回應 VETO 被系統剔除時遞補。"""


def _ask(system: str, user: str, max_tokens: int = 900, rec=None,
         json_mode: bool = False) -> str | None:
    """單次輕量呼叫（辯論用）：2 次重試，失敗回 None 不丟例外。
    rec: exec_log.StageRec，有給就累計 LLM 用量（決策軌跡）。
    json_mode: 結構化辯論契約用，強制輸出純 JSON。"""
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    for attempt in range(2):
        try:
            resp = completion(
                model=MODEL,
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
                temperature=0.4,
                max_tokens=max_tokens,
                reasoning_effort="disable",
                **kwargs,
            )
            if rec is not None:
                rec.add_llm(getattr(resp, "usage", None))
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out
        except Exception as e:
            if rec is not None:
                rec.add_llm(None)          # 失敗的呼叫也算一次（額度有消耗）
            logger.warning(f"辯論呼叫失敗（{attempt+1}/2）: {str(e)[:100]}")
            time.sleep(10)
    return None


def _run_debate(candidates_text: str, hot_sectors: list[str]) -> tuple[dict, dict]:
    """
    多空辯論（SPEC_REASONING_LAYER 2.2：結構化 JSON 契約，取代散文）。
    回傳 (bull_pack, bear_pack)，各為 {"raw": 原文|None, "data": 解析後dict|None}。
    JSON 解析失敗時 data=None、raw 保留 → 裁決退化為舊的散文模式（優雅降級）。

    SPEC_STRATEGY_MIDCAP §3 決策3（2026-07-15）：候選池已縮到中型流動性股+成交金額門檻，
    暫採方案A（維持現有單輪、20檔一次的結構，不逐檔多輪辯論）——先觀察中型池+新聞真的
    有料之後的辯論品質，成本也低（Gemini 免費版每日每專案僅20次呼叫，逐檔多輪會爆量）。
    保留方案B（TradingAgents 式：粗選~8檔 → 逐檔多輪來回、空方回應多方再駁）的概念：
    若之後要拉高辯論強度，改法是把這個函式拆成「單股辯論」+ 外層迴圈跑候選池，每輪把
    對方上一輪發言餵回去要求正面回應，直到雙方沒有新論點或達到輪數上限；需要付費 key
    或換一個沒有每日20次硬限制的模型才能承受呼叫量。
    """
    from agent import exec_log
    bull_pack = {"raw": None, "data": None}
    bear_pack = {"raw": None, "data": None}

    with exec_log.stage("debate_bull") as rec:
        raw = _ask(
            "你是多方研究員。只根據提供的數據論證，禁止編造數據。只輸出 JSON。",
            f"熱門族群：{', '.join(hot_sectors)}\n\n{candidates_text}\n\n"
            "從候選中挑出你最看好的 5 檔。規則：\n"
            "1. thesis（買進論點，2-4句）必須引用至少兩類證據：趨勢結構（多頭排列天數）、"
            "基本面（營收年增）、籌碼（法人/投信連買）、題材（新聞）。"
            "**禁止只複述當日漲跌幅與 RSI**——那不是論點，是複讀資料。"
            "候選資料裡的「相對強度 RS20」「60日動能」經統計研究證實在目前持有天數與後續"
            "報酬負相關，只能當背景參考，不可當作論點的主要依據。\n"
            "2. preempt_rebuttal：誠實寫出「反對這檔最強的理由是什麼、為何仍值得買」。\n"
            "3. evidence_fields：列出你論點實際引用的資料欄位名。\n"
            '輸出 JSON：{"picks":[{"stock_id":"","stock_name":"","thesis":"",'
            '"evidence_fields":["rs20","rev_yoy"],"preempt_rebuttal":""}]}',
            max_tokens=2600, rec=rec, json_mode=True,
        )
        bull_pack["raw"] = raw
        bull_pack["data"] = _parse_json(raw) if raw else None
        if bull_pack["data"] and not bull_pack["data"].get("picks"):
            bull_pack["data"] = None            # 有 JSON 但不合契約 → 視為降級
        _n = len((bull_pack["data"] or {}).get("picks", []))
        rec.summary = (f"結構化論證 {_n} 檔：" +
                       ", ".join(p.get("stock_id", "?") for p in bull_pack["data"]["picks"])
                       if bull_pack["data"] else
                       ("JSON 解析失敗，退化為散文模式" if raw else "呼叫失敗（裁決將退化為單次模式）"))
        rec.payload = {"text": raw, "parsed": bull_pack["data"]} if raw else None

    with exec_log.stage("debate_bear") as rec:
        raw = _ask(
            "你是空方研究員，專門用數據拆解多頭故事的漏洞。只根據提供的數據，禁止編造。只輸出 JSON。",
            f"熱門族群：{', '.join(hot_sectors)}\n\n{candidates_text}\n\n"
            "對這批候選股提出風險審查。規則：\n"
            "1. vetoes：對個股的具體異議。severity 分兩級——"
            "「veto」＝基於數據有具體理由絕對不該碰（例：外資大額賣超疑似出貨、跌勢未止、"
            "資料異常）；「caution」＝有風險但可控（例：RSI 偏高但趨勢完整）。"
            "不要把所有 RSI>70 一律打成 veto——動能股本來就常在高檔，要看趨勢結構是否完好。\n"
            "2. 每條異議必須引用具體數據，reason 精簡在 40 字內。\n"
            "3. market_concerns：族群集中度/整體市場疑慮，最多 3 條、每條一句話。\n"
            '輸出 JSON：{"vetoes":[{"stock_id":"","stock_name":"","severity":"veto|caution",'
            '"reason":"引用具體數據的異議"}],"market_concerns":["..."]}',
            max_tokens=3200, rec=rec, json_mode=True,
        )
        bear_pack["raw"] = raw
        bear_pack["data"] = _parse_json(raw) if raw else None
        if bear_pack["data"] is not None and "vetoes" not in bear_pack["data"]:
            bear_pack["data"] = None
        if bear_pack["data"]:
            _v = [v for v in bear_pack["data"].get("vetoes", []) if v.get("severity") == "veto"]
            _c = [v for v in bear_pack["data"].get("vetoes", []) if v.get("severity") != "veto"]
            rec.summary = (f"VETO {len(_v)} 檔（{', '.join(x.get('stock_id', '?') for x in _v) or '無'}）"
                           f"＋提醒 {len(_c)} 檔")
        elif raw:
            # 有輸出但解析不出結構 → guardrail 會失效，這件事必須顯性化，不能默默降級
            rec.summary = "⚠️ 空方 JSON 解析失敗 → 裁決問責 guardrail 本次未啟用（VETO 不會被強制）"
            logger.warning("⚠️ 空方 JSON 無法解析，apply_judge_guardrail 本次不會攔截任何 VETO")
        else:
            rec.summary = "呼叫失敗（裁決將退化為單次模式）"
        rec.payload = {"text": raw, "parsed": bear_pack["data"]} if raw else None

    return bull_pack, bear_pack


def _format_debate_for_judge(bull_pack: dict, bear_pack: dict) -> str:
    """把結構化辯論組成裁決輸入；任一方降級時用原文，兩方全失敗回空字串（單次模式）。"""
    if not (bull_pack.get("raw") or bear_pack.get("raw")):
        return ""
    parts = ["\n\n═══ 內部研究員辯論（裁決時必須逐條回應空方異議）═══"]
    bd = bull_pack.get("data")
    if bd:
        parts.append("\n【多方論點】")
        for p in bd.get("picks", []):
            parts.append(f"- {p.get('stock_id')} {p.get('stock_name', '')}：{p.get('thesis', '')}")
            if p.get("preempt_rebuttal"):
                parts.append(f"  （多方自辯：{p['preempt_rebuttal']}）")
    elif bull_pack.get("raw"):
        parts.append(f"\n【多方研究員（原文）】\n{bull_pack['raw']}")
    br = bear_pack.get("data")
    if br:
        vets = [v for v in br.get("vetoes", []) if v.get("severity") == "veto"]
        cauts = [v for v in br.get("vetoes", []) if v.get("severity") != "veto"]
        if vets:
            parts.append("\n【空方 VETO——除非以具體數據駁回，否則不得入選】")
            parts += [f"- {v.get('stock_id')} {v.get('stock_name', '')}：{v.get('reason', '')}" for v in vets]
        if cauts:
            parts.append("\n【空方風險提醒（caution）】")
            parts += [f"- {v.get('stock_id')} {v.get('stock_name', '')}：{v.get('reason', '')}" for v in cauts]
        if br.get("market_concerns"):
            parts.append("\n【空方整體疑慮】")
            parts += [f"- {c}" for c in br["market_concerns"]]
    elif bear_pack.get("raw"):
        parts.append(f"\n【空方研究員（原文）】\n{bear_pack['raw']}")
    parts.append("\n請以首席投資長身分裁決：對每檔入選股逐條回應空方異議（接受→不選；"
                 "駁回→引用具體數據），並提供 backups 遞補名單。")
    return "\n".join(parts)


def apply_judge_guardrail(result: dict, bear_data: dict | None) -> tuple[dict, list[dict]]:
    """
    裁決問責的決定性檢查（SPEC_REASONING_LAYER 2.3，軟模式）——不信任 LLM 自覺：
    被空方 VETO 的入選股，必須在 objections_addressed 有「駁回＋具體理由(≥10字)」，
    否則程式自動剔除、以 backups 遞補（遞補股同樣受檢）。回傳 (修正後result, 攔截紀錄)。
    2026-07-13 實錘：裁決曾 5/5 照抄多方、把空方 VETO 貼成 risk_note 照樣入選。
    """
    actions: list[dict] = []
    if not result or not result.get("recommendations") or not bear_data:
        return result, actions
    vetoes = {str(v.get("stock_id")): v for v in (bear_data.get("vetoes") or [])
              if v.get("severity") == "veto"}
    if not vetoes:
        return result, actions

    def _rebutted(rec: dict) -> bool:
        for oa in rec.get("objections_addressed") or []:
            if oa.get("verdict") == "駁回" and len((oa.get("rebuttal") or "").strip()) >= 10:
                return True
        return False

    target_n = len(result["recommendations"])
    kept = []
    for rec in result["recommendations"]:
        sid = str(rec.get("stock_id"))
        if sid in vetoes and not _rebutted(rec):
            actions.append({"action": "剔除", "stock_id": sid,
                            "why": f"空方 VETO（{str(vetoes[sid].get('reason', ''))[:80]}）且裁決未提出具體駁回"})
        else:
            kept.append(rec)

    if actions:                     # 有剔除才動用遞補
        for b in result.get("backups") or []:
            if len(kept) >= target_n:
                break
            bsid = str(b.get("stock_id"))
            if any(str(k.get("stock_id")) == bsid for k in kept):
                continue
            if bsid in vetoes and not _rebutted(b):
                actions.append({"action": "遞補被拒", "stock_id": bsid,
                                "why": "遞補股同樣被 VETO 且無具體駁回"})
                continue
            kept.append(b)
            actions.append({"action": "遞補", "stock_id": bsid, "why": "補足被剔除的名額"})
        for i, r in enumerate(kept, 1):
            r["rank"] = i
        result = {**result, "recommendations": kept}
    return result, actions


def generate_recommendations(candidates_text: str, hot_sectors: list[str],
                              candidates=None) -> dict:
    """
    呼叫 LLM 產生推薦報告（DEBATE_MODE 時先跑多空辯論，裁決時附上兩方觀點）
    回傳解析後的 dict

    candidates: 選配，候選股票 DataFrame（跟 candidates_text 是同一份資料格式化前
    的版本）。給了就會跑 agent.citation_check 引用驗證，幫每筆推薦標記
    grounding_flags（Phase C1，純規則檢查，不額外呼叫 LLM，只標記不攔截）。
    """
    if not candidates_text:
        logger.error("沒有候選股票資料")
        return {}

    today = date.today().strftime("%Y-%m-%d")

    debate_section = ""
    bear_data = None            # 結構化空方輸出 → 裁決後的程式 guardrail 用
    bull_pack = None            # 結構化多方輸出 → 裁決後標記 debate_coverage 用
    if DEBATE_MODE:
        logger.info("多空辯論：徵詢多方/空方研究員觀點（結構化 JSON）...")
        bull_pack, bear_pack = _run_debate(candidates_text, hot_sectors)
        bear_data = bear_pack.get("data")
        debate_section = _format_debate_for_judge(bull_pack, bear_pack)
        if debate_section:
            logger.info("多空辯論完成，進入裁決")
        else:
            logger.warning("辯論呼叫全數失敗，退回單次模式")

    user_prompt = f"""今天日期：{today}

今日熱門產業族群：{", ".join(hot_sectors)}

{candidates_text}{debate_section}

請從以上候選股票中，挑選最值得關注的 5 支，按照指定 JSON 格式回覆。"""

    logger.info(f"呼叫 LLM 產生推薦（模型：{MODEL}）")

    # 重試涵蓋兩類問題：
    #   1. 503/429 等暫時性錯誤（Gemini 免費版常見）
    #   2. 回傳的 JSON 解析不出有效推薦（非決定性，重生一次常就好）
    from agent import exec_log
    with exec_log.stage("judge") as rec:
        max_retries = 4
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                response = completion(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=8192,
                    response_format={"type": "json_object"},  # 強制輸出純 JSON，免 markdown 包裹
                    reasoning_effort="disable",  # gemini-2.5 關閉思考：省 token/加速，避免截斷
                )
                rec.add_llm(getattr(response, "usage", None))
                content = response.choices[0].message.content
                result = _parse_json(content)
                if result.get("recommendations"):
                    # 裁決問責 guardrail（程式檢查，不信任 LLM 自覺）：
                    # 被 VETO 且無具體駁回的入選股 → 剔除並以 backups 遞補
                    result, guard_actions = apply_judge_guardrail(result, bear_data)
                    # 引用驗證（Phase C1）：標記理由裡候選資料找不到對應的數字，
                    # 只加註記不影響裁決結果
                    from agent.citation_check import annotate_recommendations, annotate_debate_coverage
                    result = annotate_recommendations(result, candidates)
                    result = annotate_debate_coverage(result, bull_pack)
                    picks = [r.get("stock_id", "?") for r in result["recommendations"]]
                    _blocked = [a for a in guard_actions if a["action"] == "剔除"]
                    _flagged = [r["stock_id"] for r in result["recommendations"] if r.get("grounding_flags")]
                    _undebated = [r["stock_id"] for r in result["recommendations"] if r.get("not_debated")]
                    rec.summary = (f"裁決選出 {len(picks)} 檔：{', '.join(picks)}"
                                   + (f"（🚫 guardrail 攔截 {len(_blocked)} 檔："
                                      f"{', '.join(a['stock_id'] for a in _blocked)}）" if _blocked else "")
                                   + (f"（🔍 引用可疑 {len(_flagged)} 檔：{', '.join(_flagged)}）" if _flagged else "")
                                   + (f"（⚡ 未經多方主張 {len(_undebated)} 檔："
                                      f"{', '.join(_undebated)}）" if _undebated else ""))
                    rec.payload = {"result": result,
                                   "debate_used": bool(debate_section),
                                   "guardrail_actions": guard_actions,
                                   "attempts": attempt}
                    if _blocked:
                        logger.warning(f"🚫 裁決 guardrail 攔截：{guard_actions}")
                    logger.info(f"LLM 回覆成功（解析到 {len(result['recommendations'])} 檔）")
                    return result
                last_err = "回傳內容解析不到有效 recommendations"
                logger.warning(f"LLM 輸出無法解析（第 {attempt}/{max_retries} 次）")

            except Exception as e:
                rec.add_llm(None)              # 失敗的呼叫也消耗額度，照記
                last_err = str(e)
                transient = any(k in last_err for k in ("503", "UNAVAILABLE", "429",
                                                        "RateLimit", "overloaded", "high demand"))
                if not (transient and attempt < max_retries):
                    logger.error(f"LLM 呼叫失敗: {e}")
                    rec.summary = f"裁決失敗：{last_err[:80]}"
                    return {}
                logger.warning(f"LLM 暫時性錯誤（第 {attempt}/{max_retries} 次）：{last_err[:120]}")

            if attempt < max_retries:
                time.sleep(min(15 * attempt, 45))   # 退避：15s, 30s, 45s

        logger.error(f"LLM 多次嘗試仍失敗：{last_err}")
        rec.summary = f"裁決多次重試仍失敗：{str(last_err)[:80]}"
        return {}


def _parse_json(text: str) -> dict:
    """
    從 LLM 回覆中穩健地擷取 JSON：
      1. 直接解析
      2. 取第一個 { 到最後一個 } 再解析
      3. 截斷救援：逐一抽出完整的推薦物件（即使整段被切斷也能救回前幾檔）
    """
    if not text:
        return {}
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            pass

    # 通用截斷救援：被 token 上限切斷的 JSON（關未結束的字串、補未閉合的括號）。
    # 2026-07-14 修：空方輸出結構是 {"vetoes":[...],"market_concerns":[...]}，
    # 舊版只認 recommendations，導致空方一截斷就整包丟掉、guardrail 靜默失效。
    repaired = _repair_truncated_json(text)
    if isinstance(repaired, dict) and repaired:
        logger.warning("JSON 被截斷，已補齊救回（建議檢查 max_tokens 是否過小）")
        return repaired

    # 舊的推薦專用救援（保底）：推薦物件內無巢狀大括號，可逐一抽出
    recs = []
    for m in re.finditer(r"\{[^{}]*?\"stock_id\"[^{}]*?\}", text, re.DOTALL):
        try:
            recs.append(json.loads(m.group()))
        except Exception:
            pass
    if recs:
        logger.warning(f"JSON 不完整，救回 {len(recs)} 檔推薦")
        return {"recommendations": recs}

    logger.error(f"JSON 解析失敗，原始內容：\n{text[:500]}")
    return {}


def _close_json(prefix: str) -> str:
    """把一段（可能被截斷的）JSON 前綴補成語法完整：關未結束的字串、補未閉合的括號、去尾逗號。"""
    stack, in_str, esc = [], False, False
    for ch in prefix:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    out = prefix + ('"' if in_str else "")
    out = out.rstrip().rstrip(",").rstrip()
    for ch in reversed(stack):
        out += "}" if ch == "{" else "]"
    return out


def _repair_truncated_json(text: str) -> dict | None:
    """
    救回尾端被截斷的 JSON（LLM 寫到一半沒 token）。策略：從第一個 { 起，先整段補齊；
    失敗則逐次砍掉最後一個逗號片段再補（丟掉最後那筆不完整的元素，保住前面完整的）。
    """
    if "{" not in text:
        return None
    s = text[text.find("{"):]
    cand = s
    for _ in range(6):
        try:
            obj = json.loads(_close_json(cand))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        cut = cand.rstrip().rstrip(",").rfind(",")
        if cut <= 0:
            break
        cand = cand[:cut]
    return None


def save_recommendations(result: dict):
    """將推薦結果存入 daily_recommendations 表"""
    if not result or "recommendations" not in result:
        return

    rec_date = date.today()
    recs = result["recommendations"]

    with get_session() as session:
        for rec in recs:
            # 確認股票存在
            exists = session.execute(
                text("SELECT 1 FROM stocks WHERE stock_id = :sid"),
                {"sid": rec["stock_id"]}
            ).fetchone()
            if not exists:
                continue

            session.execute(text("""
                INSERT INTO daily_recommendations
                    (rec_date, stock_id, rank, reason, tech_signals)
                VALUES
                    (:date, :sid, :rank, :reason, :signals)
                ON CONFLICT (rec_date, stock_id) DO UPDATE SET
                    rank        = EXCLUDED.rank,
                    reason      = EXCLUDED.reason,
                    tech_signals = EXCLUDED.tech_signals
            """), {
                "date":    rec_date,
                "sid":     rec["stock_id"],
                "rank":    rec["rank"],
                "reason":  rec.get("reason", ""),
                "signals": json.dumps(rec.get("key_signals", []), ensure_ascii=False),
            })

    logger.info(f"✅ {len(recs)} 支推薦已存入 daily_recommendations")


def format_report(result: dict) -> str:
    """將推薦結果格式化成可讀的報告文字"""
    if not result or "recommendations" not in result:
        return "今日無推薦資料"

    lines = [
        f"📊 台股每日推薦 {result.get('date', date.today())}",
        "=" * 40,
    ]

    if "market_summary" in result:
        lines.append(f"📈 大盤簡評：{result['market_summary']}")
        lines.append("")

    for rec in result["recommendations"]:
        signals = " | ".join(rec.get("key_signals", []))
        lines += [
            f"#{rec['rank']} 【{rec['stock_id']} {rec['stock_name']}】",
            f"   {rec.get('reason', '')}",
            f"   🔑 {signals}",
            f"   ⚠️  {rec.get('risk_note', '')}",
        ]
        if rec.get("grounding_flags"):
            flags_txt = "、".join(str(v) for v in rec["grounding_flags"])
            lines.append(f"   🔍 理由中有數字候選資料查無對應（可能算錯/幻覺，僅供留意）：{flags_txt}")
        if rec.get("not_debated"):
            lines.append("   ⚡ 此檔未經多方研究員主張，由裁決直接從候選資料選出（少一層辯論檢視）")
        lines.append("")

    return "\n".join(lines)