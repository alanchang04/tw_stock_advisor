"""
scripts/sector_concentration_report.py

SPEC_QUANT_UPGRADE.md P3 決策3：「組合層：單一族群曝險上限」——目前只是原則，
還沒量化過問題規模。這裡量測：10年回測裡，每次再平衡選出的候選股，實際的
族群集中度有多嚴重（top-5常常整批同族群 vs 分散），用來決定值不值得實作硬上限。

做法：monkeypatch agent.backtest._candidates_asof 記錄每次呼叫的候選清單，
跑一次完整 run_backtest()（產生真實交易用同一套邏輯，不是另外複製一份），
跑完後用記錄下來的候選清單 join stock_industry_map 統計族群分佈。

用法：
    python scripts/sector_concentration_report.py
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from loguru import logger

import agent.backtest as bt
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")


def main():
    logger.info("=== 載入10年本機歷史資料 ===")
    data = bt._load(parquet_dir=PARQUET_DIR)
    imap = data["imap"]
    sid_to_inds = {}
    if not imap.empty:
        for sid, g in imap.groupby("stock_id"):
            sid_to_inds[sid] = set(g["industry_code"])

    records = []   # (date, [sid,...])
    orig = bt._candidates_asof

    def _wrapped(data_, d, industry_codes, top_n=5, cfg=None):
        picks = orig(data_, d, industry_codes, top_n=top_n, cfg=cfg)
        if picks:
            records.append((d, picks))
        return picks

    bt._candidates_asof = _wrapped
    try:
        logger.info("=== 跑一次完整10年回測（記錄每次再平衡的候選族群分佈）===")
        bt.run_backtest(cfg=STRATEGY, data=data, quiet=True, parquet_dir=None)
    finally:
        bt._candidates_asof = orig

    logger.info(f"=== 共記錄 {len(records)} 個再平衡日的候選清單 ===")

    max_share = []       # 每次再平衡：最大單一族群佔比
    n_unique_sectors = []
    no_industry_days = 0
    for d, picks in records:
        sectors = []
        for sid in picks:
            inds = sid_to_inds.get(sid)
            if inds:
                sectors.extend(inds)   # 一檔可能跨多族群，全部計入（保守估計集中度）
        if not sectors:
            no_industry_days += 1
            continue
        cnt = Counter(sectors)
        top_sector, top_count = cnt.most_common(1)[0]
        max_share.append(top_count / len(picks))
        n_unique_sectors.append(len(cnt))

    s = pd.Series(max_share)
    logger.info(f"「最大單一族群佔當次候選比例」統計（{len(s)}個再平衡日，"
               f"{no_industry_days}日無族群對照資料）：")
    logger.info(f"  平均 {s.mean()*100:.1f}%  中位數 {s.median()*100:.1f}%  "
               f"90分位 {s.quantile(0.9)*100:.1f}%")
    for thresh in (0.4, 0.6, 0.8, 1.0):
        ratio = (s >= thresh).mean()
        logger.info(f"  最大族群佔比 >= {thresh*100:.0f}% 的再平衡日佔比：{ratio*100:.1f}%")

    u = pd.Series(n_unique_sectors)
    logger.info(f"每次再平衡候選涵蓋的「不重複族群數」：平均 {u.mean():.2f}（top_n={STRATEGY['pick_top_n']}，"
               f"若完全分散應接近{STRATEGY['pick_top_n']}）")


if __name__ == "__main__":
    main()
