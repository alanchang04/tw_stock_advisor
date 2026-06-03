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

# 設定 API Key
os.environ["GEMINI_API_KEY"] = APIConfig.GEMINI_KEY

SYSTEM_PROMPT = """你是一位專業的台股投資顧問，具備技術分析和籌碼分析的專業知識。

你的任務是根據提供的股票資料，從候選名單中挑選出最值得關注的 5 支股票，
並為每支股票提供簡潔且具體的推薦理由。

分析時請考量：
1. 技術面：均線多頭排列、黃金交叉、突破壓力區、MACD 轉正
2. 籌碼面：三大法人買超、外資持續買進
3. 族群題材：所屬產業是否為當前熱門族群
4. RSI 是否在合理區間（避免追高）

回覆格式請嚴格按照以下 JSON 格式，不要有其他文字：
{
  "date": "YYYY-MM-DD",
  "recommendations": [
    {
      "rank": 1,
      "stock_id": "股票代號",
      "stock_name": "股票名稱",
      "reason": "推薦理由（100字以內，具體說明技術和籌碼面的依據）",
      "key_signals": ["訊號1", "訊號2"],
      "risk_note": "注意事項（30字以內）"
    }
  ],
  "market_summary": "今日大盤族群輪動簡評（50字以內）"
}"""


def generate_recommendations(candidates_text: str, hot_sectors: list[str]) -> dict:
    """
    呼叫 LLM 產生推薦報告
    回傳解析後的 dict
    """
    if not candidates_text:
        logger.error("沒有候選股票資料")
        return {}

    today = date.today().strftime("%Y-%m-%d")

    user_prompt = f"""今天日期：{today}

今日熱門產業族群：{", ".join(hot_sectors)}

{candidates_text}

請從以上候選股票中，挑選最值得關注的 5 支，按照指定 JSON 格式回覆。"""

    logger.info(f"呼叫 LLM 產生推薦（模型：{MODEL}）")

    # 重試涵蓋兩類問題：
    #   1. 503/429 等暫時性錯誤（Gemini 免費版常見）
    #   2. 回傳的 JSON 解析不出有效推薦（非決定性，重生一次常就好）
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
            )
            content = response.choices[0].message.content
            result = _parse_json(content)
            if result.get("recommendations"):
                logger.info(f"LLM 回覆成功（解析到 {len(result['recommendations'])} 檔）")
                return result
            last_err = "回傳內容解析不到有效 recommendations"
            logger.warning(f"LLM 輸出無法解析（第 {attempt}/{max_retries} 次）")

        except Exception as e:
            last_err = str(e)
            transient = any(k in last_err for k in ("503", "UNAVAILABLE", "429",
                                                    "RateLimit", "overloaded", "high demand"))
            if not (transient and attempt < max_retries):
                logger.error(f"LLM 呼叫失敗: {e}")
                return {}
            logger.warning(f"LLM 暫時性錯誤（第 {attempt}/{max_retries} 次）：{last_err[:120]}")

        if attempt < max_retries:
            time.sleep(min(15 * attempt, 45))   # 退避：15s, 30s, 45s

    logger.error(f"LLM 多次嘗試仍失敗：{last_err}")
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

    # 截斷救援：推薦物件內無巢狀大括號（key_signals 是陣列），可逐一抽出
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
            "",
        ]

    return "\n".join(lines)