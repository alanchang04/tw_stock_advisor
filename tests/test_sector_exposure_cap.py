"""
族群曝險上限測試（agent/backtest.py run_backtest 的 sector_exposure_cap，
SPEC_QUANT_UPGRADE.md P3決策3）。用小規模合成資料跑一次完整 run_backtest()，
逐日重建「當時同時持有幾檔」驗證任何時間點都沒有單一族群超過上限。
"""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from agent.backtest import run_backtest, _compute_tech_from_prices
from agent.strategy import STRATEGY


def _dates(n, start=dt.date(2024, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _make_data(n_days=140, n_per_sector=4):
    """6檔股票分兩個族群(SEC_A/SEC_B)，價格緩步上漲讓大家都能通過候選篩選，
    用隨機小雜訊區分排名，避免完全同分讓結果對實作細節太敏感。"""
    rng = np.random.default_rng(7)
    dates = _dates(n_days)
    stocks = [f"S{i}" for i in range(n_per_sector * 2)]
    sectors = {sid: ("SEC_A" if i < n_per_sector else "SEC_B") for i, sid in enumerate(stocks)}

    rows = []
    for sid in stocks:
        price = 100.0
        drift = rng.uniform(0.001, 0.004)
        for d in dates:
            price *= (1 + drift + rng.normal(0, 0.01))
            rows.append({"stock_id": sid, "trade_date": d, "open": price, "high": price * 1.01,
                        "low": price * 0.99, "close": price, "volume": 5000.0,
                        "turnover": price * 5000.0, "change_pct": drift * 100})
    prices = pd.DataFrame(rows)
    tech = _compute_tech_from_prices(prices)

    imap = pd.DataFrame([{"stock_id": sid, "industry_code": sectors[sid]} for sid in stocks])
    inds = pd.DataFrame({"industry_code": ["SEC_A", "SEC_B"], "name_zh": ["產業A", "產業B"]})
    # inst 給跟 prices 同樣的日期範圍（全 0，只是為了讓 run_backtest 算 effective_start
    # 不會因為空表 min() 回傳 NaN 而炸掉——真實資料本來就一定有法人表，這裡補真實一點）。
    inst = pd.DataFrame({"stock_id": [stocks[0]] * len(dates), "trade_date": dates,
                         "total_net": [0.0] * len(dates), "foreign_net": [0.0] * len(dates),
                         "invest_net": [0.0] * len(dates)})
    div = pd.DataFrame(columns=["stock_id", "ex_date", "pre_close", "ref_price"])

    return {"prices": prices, "tech": tech, "inst": inst, "imap": imap, "inds": inds,
           "rev_map": {}, "dividends": div}


def _reconstruct_open_counts(tdf, sid_to_sector):
    """從交易紀錄(entry_date/exit_date)重建逐日持倉，回傳每天各族群持有數的最大值。"""
    if tdf is None or tdf.empty:
        return 0
    all_dates = sorted(set(tdf["entry_date"]) | set(tdf["exit_date"]))
    worst = 0
    for d in all_dates:
        open_now = tdf[(tdf["entry_date"] <= d) & (tdf["exit_date"] >= d)]
        if open_now.empty:
            continue
        counts = {}
        for sid in open_now["stock_id"]:
            sec = sid_to_sector.get(sid)
            if sec:
                counts[sec] = counts.get(sec, 0) + 1
        if counts:
            worst = max(worst, max(counts.values()))
    return worst


def _cfg(**overrides):
    return {
        **STRATEGY,
        "market_filter": False, "min_turnover_percentile": None,
        "min_turnover_avg5": 0, "min_close": 1, "min_volume": 1,
        "min_rsi": 0, "max_rsi": 100, "use_hot_sector_gate": False,
        "pick_top_n": 4, "max_open_positions": 5,
        **overrides,
    }


def test_sector_cap_never_exceeded_when_enabled():
    data = _make_data()
    sid_to_sector = dict(zip(data["imap"]["stock_id"], data["imap"]["industry_code"]))
    cfg = _cfg(sector_exposure_cap=0.4)   # max_open=5 → 上限 round(5*0.4)=2
    tdf = run_backtest(cfg=cfg, data=data, quiet=True)
    worst = _reconstruct_open_counts(tdf, sid_to_sector)
    assert worst <= 2


def test_sector_cap_enabled_by_default_at_point_six():
    # 2026-07-20 10年A/B驗證：0.6是全指標同時勝出的柏拉圖改善，已設為新預設
    assert STRATEGY.get("sector_exposure_cap") == 0.6


def test_sector_cap_none_preserves_old_behavior_no_crash():
    data = _make_data()
    cfg = _cfg(sector_exposure_cap=None)
    tdf = run_backtest(cfg=cfg, data=data, quiet=True)
    assert tdf is not None
