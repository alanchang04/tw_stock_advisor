"""
scripts/benchmark_ex_tsmc.py

使用者假設：策略 vs 0050 的總報酬落差，主要是台積電集中度效應（0050是市值加權，
台積電過去10年一直是最大權重成分股），不是選股能力差距。用「0050扣掉台積電」
當第二個對照組驗證這個假設。

**方法（誠實聲明是近似法，不是官方成分股權重逐日重建）**：沒有0050歷年官方
成分股權重的逐日歷史（那需要另外的資料源），改用迴歸法剝離：對10年期間
0050日報酬 對 台積電(2330)日報酬 做OLS，取得的beta可粗略理解為「台積電對0050
報酬的平均線性敏感度」，殘差(0050報酬 - beta×台積電報酬)複利起來當作「扣除
台積電線性貢獻後的0050」近似曲線。這不是精確的「假設從沒買過台積電的0050」，
是統計上剝離台積電解釋力後的近似——用來判斷落差方向/量級，不是精確歸因。

用法：
    python scripts/benchmark_ex_tsmc.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from loguru import logger

from agent.backtest import _load, split_adjust, apply_total_return_adjustment, run_backtest, perf_metrics
from agent.strategy import STRATEGY

PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "research")
TSMC = "2330"


def _fmt(s: dict, label: str) -> str:
    return (f"{label}：總報酬{s['total']*100:+.1f}%  年化報酬{s['ann_ret']*100:+.1f}%  "
           f"Sharpe{s['sharpe']:.2f}  回撤{s['mdd']*100:.1f}%  Calmar{s['calmar']:.2f}")


def main():
    logger.info("=== 載入10年本機歷史資料 ===")
    data = _load(parquet_dir=PARQUET_DIR)
    prices = data["prices"]
    closes = prices.pivot_table(index="trade_date", columns="stock_id", values="close")
    closes = closes.where(closes > 0)
    div = data.get("dividends")
    if div is not None and not div.empty:
        closes = apply_total_return_adjustment(closes, div)

    if "0050" not in closes.columns or TSMC not in closes.columns:
        logger.error("缺 0050 或台積電(2330)價格資料，無法比較")
        return

    mkt = split_adjust(closes["0050"]).dropna()
    tsmc = closes[TSMC].dropna()
    idx = mkt.index.intersection(tsmc.index)
    mkt, tsmc = mkt.loc[idx], tsmc.loc[idx]

    r_mkt = mkt.pct_change().dropna()
    r_tsmc = tsmc.pct_change().dropna()
    idx2 = r_mkt.index.intersection(r_tsmc.index)
    r_mkt, r_tsmc = r_mkt.loc[idx2], r_tsmc.loc[idx2]

    beta, alpha = np.polyfit(r_tsmc.values, r_mkt.values, 1)
    corr = r_mkt.corr(r_tsmc)
    logger.info(f"=== 迴歸：0050日報酬 ~ alpha({alpha:.5f}) + beta({beta:.3f}) × 台積電日報酬"
               f"（相關係數{corr:.2f}）===")
    logger.info("beta可粗略理解為台積電對0050報酬的平均線性敏感度（不是官方權重數字）")

    r_ex = r_mkt - beta * r_tsmc
    nav_mkt = (1 + r_mkt).cumprod()
    nav_ex = (1 + r_ex).cumprod()

    s_mkt = perf_metrics(nav_mkt)
    s_ex = perf_metrics(nav_ex)

    logger.info("=== 策略本身（沿用已驗證的STRATEGY，含族群曝險上限） ===")
    t = run_backtest(cfg=STRATEGY, data=data, quiet=True)
    a = t.attrs
    s_strat = dict(total=a["nav_total_ret"], ann_ret=None, sharpe=a["sharpe"],
                  mdd=a["nav_mdd"], calmar=a["calmar"])

    logger.info("=== 三方比較 ===")
    logger.info(_fmt(s_mkt, "0050(原始)"))
    logger.info(_fmt(s_ex, "0050(近似扣除台積電線性貢獻)"))
    logger.info(f"策略：總報酬{s_strat['total']*100:+.1f}%  Sharpe{s_strat['sharpe']:.2f}  "
               f"回撤{s_strat['mdd']*100:.1f}%  Calmar{s_strat['calmar']:.2f}")


if __name__ == "__main__":
    main()
