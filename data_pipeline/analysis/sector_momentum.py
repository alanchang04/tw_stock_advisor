"""
data_pipeline/analysis/sector_momentum.py

計算各產業族群的輪動熱度，寫入 sector_momentum 表。

熱度分數邏輯：
  - 族群平均漲幅（權重 50%）
  - 上漲股票比例（權重 30%）
  - 三大法人淨買超強度（權重 20%）
"""
import pandas as pd
from datetime import date, timedelta
from loguru import logger
from sqlalchemy import text

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from database.connection import get_session


def calc_sector_momentum(calc_date: date = None):
    if calc_date is None:
        calc_date = date.today()

    # 用最近 5 個交易日的資料計算（避免單日異常）
    start = calc_date - timedelta(days=7)

    logger.info(f"計算族群輪動熱度：{calc_date}")

    with get_session() as session:

        # 1. 取各股近期平均漲幅 + 法人淨買超
        #    法人資料先在子查詢內聚合，避免和 daily_prices 多天對多天的
        #    笛卡兒乘積（fan-out）把 inst_net 灌水成 N 倍
        rows = session.execute(text("""
            SELECT
                m.industry_code,
                p.stock_id,
                AVG(p.change_pct)        AS avg_change,
                COUNT(*)                 AS total_days,
                COALESCE(inst.net_sum, 0) AS inst_net
            FROM stock_industry_map m
            JOIN daily_prices p
                ON p.stock_id = m.stock_id
                AND p.trade_date BETWEEN :start AND :end
                AND p.change_pct IS NOT NULL
            LEFT JOIN (
                SELECT stock_id, SUM(total_net) AS net_sum
                FROM institutional_trading
                WHERE trade_date BETWEEN :start AND :end
                GROUP BY stock_id
            ) inst ON inst.stock_id = m.stock_id
            GROUP BY m.industry_code, p.stock_id, inst.net_sum
        """), {"start": start, "end": calc_date})

        df = pd.DataFrame(rows.fetchall(), columns=list(rows.keys()))

    if df.empty:
        logger.warning("沒有足夠資料計算族群熱度")
        return

    # DB 的 NUMERIC 欄位在 Python 是 Decimal，需轉成 float，
    # 否則後面與 0.5 等 float 權重相乘會 TypeError
    for col in ("avg_change", "inst_net"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

    # 2. 以產業為單位聚合
    #    rising_count = 期間「平均上漲」的股票數（avg_change > 0），
    #    才能反映族群真正的上漲廣度；原本用 up_days>0 幾乎每檔都符合，沒有鑑別度
    sector = df.groupby("industry_code").agg(
        avg_change_pct = ("avg_change", "mean"),
        rising_count   = ("avg_change", lambda x: (x > 0).sum()),
        total_count    = ("stock_id",   "count"),
        inst_net_sum   = ("inst_net",   "sum"),
    ).reset_index()

    # 3. 計算熱度分數（各指標正規化後加權）
    def normalize(s):
        rng = s.max() - s.min()
        return (s - s.min()) / rng if rng != 0 else pd.Series(0.5, index=s.index)

    sector["rising_ratio"] = sector["rising_count"] / sector["total_count"].clip(lower=1)

    score  = normalize(sector["avg_change_pct"]) * 0.5
    score += normalize(sector["rising_ratio"])   * 0.3
    score += normalize(sector["inst_net_sum"])   * 0.2
    sector["momentum_score"] = score.round(4).astype(float)

    # 4. 寫入 sector_momentum
    with get_session() as session:
        for _, row in sector.iterrows():
            session.execute(text("""
                INSERT INTO sector_momentum
                    (industry_code, calc_date, avg_change_pct,
                     rising_count, total_count, momentum_score)
                VALUES
                    (:code, :date, :avg_chg,
                     :rising, :total, :score)
                ON CONFLICT (industry_code, calc_date) DO UPDATE SET
                    avg_change_pct = EXCLUDED.avg_change_pct,
                    rising_count   = EXCLUDED.rising_count,
                    total_count    = EXCLUDED.total_count,
                    momentum_score = EXCLUDED.momentum_score
            """), {
                "code":    row["industry_code"],
                "date":    calc_date,
                "avg_chg": round(float(row["avg_change_pct"]), 4),
                "rising":  int(row["rising_count"]),
                "total":   int(row["total_count"]),
                "score":   float(row["momentum_score"]),
            })

    logger.info(f"✅ 族群輪動計算完成：{len(sector)} 個產業")

    # 5. 印出當日熱度前 10 名
    top10 = sector.nlargest(10, "momentum_score")[
        ["industry_code", "avg_change_pct", "rising_count", "total_count", "momentum_score"]
    ]
    logger.info(f"\n{'='*55}\n熱度前 10 產業（{calc_date}）\n{'='*55}\n{top10.to_string(index=False)}\n{'='*55}")


def run_sector_momentum():
    calc_sector_momentum(date.today())


if __name__ == "__main__":
    run_sector_momentum()