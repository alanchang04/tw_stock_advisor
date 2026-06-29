"""
data_pipeline/scrapers/industry_fetcher.py

從 FinMind TaiwanStockInfo API 取得產業分類，建立：
  - industries 資料表
  - stock_industry_map 資料表
  - 同時更新 stocks 表的 industry_code 欄位
"""
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from config.settings import APIConfig
from database.connection import get_session


def run_industry_scraper():
    logger.info("=== 開始從 FinMind 建立產業分類 ===")

    from FinMind.data import DataLoader  # lazy import：只有 --mode industry 才需要 FinMind
    # 1. 拉取股票總覽（含 industry_category 欄位）
    dl = DataLoader()
    if APIConfig.FINMIND_TOKEN:
        dl.login_by_token(api_token=APIConfig.FINMIND_TOKEN)

    df = dl.taiwan_stock_info()
    if df.empty:
        logger.error("FinMind 回傳空資料")
        return

    # 只保留純數字股票代號
    df = df[df["stock_id"].str.match(r"^\d{4,6}$")]

    # 2. 取出所有不重複的產業類別
    industries = (
        df["industry_category"]
        .dropna()
        .unique()
    )
    logger.info(f"找到 {len(industries)} 個產業類別")

    # 3. 寫入 industries 表
    with get_session() as session:
        for name in industries:
            if not name or not name.strip():
                continue
            # 用產業名稱做 code（去除空白）
            code = name.strip().replace(" ", "_").replace("/", "_")
            session.execute(text("""
                INSERT INTO industries (code, name_zh, source)
                VALUES (:code, :name_zh, 'finmind')
                ON CONFLICT (code) DO UPDATE
                    SET name_zh = EXCLUDED.name_zh,
                        updated_at = NOW()
            """), {"code": code, "name_zh": name.strip()})
    logger.info("✅ industries 表更新完成")

    # 4. 建立 stock_industry_map 並更新 stocks.industry_code
    mapped = 0
    with get_session() as session:
        for _, row in df.iterrows():
            sid = row["stock_id"]
            ind = row.get("industry_category", "")
            if not ind or not str(ind).strip():
                continue
            code = str(ind).strip().replace(" ", "_").replace("/", "_")

            # 確認股票存在才做 mapping
            exists = session.execute(
                text("SELECT 1 FROM stocks WHERE stock_id = :sid"),
                {"sid": sid}
            ).fetchone()
            if not exists:
                continue

            # 更新 stocks 的 industry_code
            session.execute(text("""
                UPDATE stocks SET industry_code = :code
                WHERE stock_id = :sid
            """), {"code": code, "sid": sid})

            # 寫入 stock_industry_map
            session.execute(text("""
                INSERT INTO stock_industry_map (stock_id, industry_code)
                VALUES (:sid, :code)
                ON CONFLICT DO NOTHING
            """), {"sid": sid, "code": code})
            mapped += 1

    logger.info(f"✅ 產業對應完成：{mapped} 支股票")
    logger.info("=== 產業分類建立完成 ===")


if __name__ == "__main__":
    run_industry_scraper()