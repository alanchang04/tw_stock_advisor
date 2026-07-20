"""agent/strategy.py 核心規則單元測試（不需 DB）。跑法：py -3.12 -m pytest tests/ -v"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from agent.strategy import (decide_exit, suggest_shares, format_size, STRATEGY,
                            PRACTICE_CFG, compute_hard_vetoes,
                            compute_new_entry_flag, apply_liquidity_gate,
                            total_return_adjust, apply_total_return_adjustment)


def cfg(**overrides):
    return {**STRATEGY, **overrides}


# ── 停損 ──────────────────────────────────────────────────────────
def test_stop_loss_triggers():
    ex, reason = decide_exit(100, 100, 91.9, None, None, 1, cfg=cfg())
    assert ex and "停損" in reason

def test_stop_loss_not_triggered_above_threshold():
    ex, _ = decide_exit(100, 100, 92.1, None, None, 1, cfg=cfg())
    assert not ex


# ── 停利 ──────────────────────────────────────────────────────────
# 2026-07-09：固定停利預設關閉（改用分級移動停利抱住大波段），
# 只在 exit_fixed_take_profit=True 時才會觸發，供消融對照。
def test_take_profit_disabled_by_default():
    ex, _ = decide_exit(100, 131, 130.5, None, None, 5, cfg=cfg())
    assert not ex   # 預設關閉，+30.5% 不會被固定停利踢出場

def test_take_profit_triggers_when_enabled():
    ex, reason = decide_exit(100, 131, 130.5, None, None, 5,
                             cfg=cfg(exit_fixed_take_profit=True))
    assert ex and "停利" in reason

def test_take_profit_not_triggered_below_when_enabled():
    ex, _ = decide_exit(100, 120, 120, None, None, 5,
                        cfg=cfg(exit_fixed_take_profit=True))
    assert not ex


# ── 分級移動停利機制（用明確的 trail_tiers 測機制，與 STRATEGY 現行預設脫鉤）──
TIERS = [(0.10, 0.08), (0.40, 0.15), (1.00, 0.20), (2.00, 0.25)]

def test_trailing_stop_triggers_after_activation():
    # 峰值+20%（落在第一層 10%~40%），回落容忍度8%：120*0.92=110.4，跌破出場
    ex, reason = decide_exit(100, 120, 110.0, None, None, 5, cfg=cfg(trail_tiers=TIERS))
    assert ex and "移動停利" in reason

def test_trailing_stop_inactive_before_activation():
    # 峰值只有 +5%，未達第一層門檻(10%)，回落不出場
    ex, _ = decide_exit(100, 105, 99.5, None, None, 5,
                        cfg=cfg(stop_loss=0.08, trail_tiers=TIERS))
    assert not ex

def test_trailing_stop_wider_giveback_after_big_gain():
    # 峰值+150%（落在第三層 100%~200%，容忍度20%）：250*0.80=200，剛好等於門檻(<=)出場
    ex, reason = decide_exit(100, 250, 200.0, None, None, 5, cfg=cfg(trail_tiers=TIERS))
    assert ex and "移動停利" in reason
    # 站在門檻之上（200.5）不出場
    ex, _ = decide_exit(100, 250, 200.5, None, None, 5, cfg=cfg(trail_tiers=TIERS))
    assert not ex

def test_trailing_stop_would_have_stopped_out_small_gain_at_same_pullback():
    # 同樣的絕對跌幅，如果峰值只有 +20%（第一層8%），110左右的回落就會出場——
    # 驗證分級確實比固定8%給大波段更多空間，不是全域放寬。
    ex, reason = decide_exit(100, 120, 108.0, None, None, 5, cfg=cfg(trail_tiers=TIERS))
    assert ex and "移動停利" in reason


# ── E4 趨勢騎乘出場（2026-07-20 P3出場規則消融後改版：死亡交叉全時期開啟在
#   10年資料上實測反而扣分——Sharpe 0.73(開)vs 0.95(只在熊市經bear_reenable_
#   death_cross重開)，回撤幾乎不變，證明砍的是本來會續漲的波段，不是真的防下檔。
#   見 scripts/exit_rule_ablation.py。牛市base預設關閉，熊市由 run_backtest 的
#   bear_cfg 動態重開，這裡只測純函式 cfg() 這層（不含 bear_cfg 那層動態邏輯）──
def test_death_cross_off_by_default():
    # 現行預設 exit_on_death_cross=False：MA5(99) < MA20(100) 但規則已關，不出場
    ex, _ = decide_exit(100, 105, 104, 99.0, 100.0, 5, cfg=cfg())
    assert not ex

def test_max_hold_disabled_by_default():
    # 現行預設 max_hold_days=0：抱再久也不因天數強制出場（趨勢還在就抱著）
    ex, _ = decide_exit(100, 105, 104, 105.0, 100.0, 999, cfg=cfg())
    assert not ex

def test_wide_backstop_ignores_small_pullback_by_default():
    # 現行預設寬 backstop [(0.50,0.25)]：峰值+20% 回落到 108 不出場（交給死亡交叉判趨勢）
    ex, _ = decide_exit(100, 120, 108.0, 110.0, 105.0, 5, cfg=cfg())
    assert not ex


# ── 均線規則 ──────────────────────────────────────────────────────
def test_ma5_exit_disabled_by_default():
    ex, _ = decide_exit(100, 105, 104, 105.0, 100.0, 5, cfg=cfg())
    assert not ex   # close < ma5 但規則已關

def test_ma5_exit_when_enabled():
    ex, reason = decide_exit(100, 105, 104, 105.0, 100.0, 5,
                             cfg=cfg(exit_below_ma5=True))
    assert ex and "MA5" in reason

def test_death_cross_when_enabled():
    ex, reason = decide_exit(100, 105, 104, 99.0, 100.0, 5,
                             cfg=cfg(exit_on_death_cross=True))
    assert ex and "死亡交叉" in reason


# ── 持有到期（現行預設關閉；用明確 max_hold_days 測機制）──────────────
def test_max_hold_days_when_enabled():
    ex, reason = decide_exit(100, 105, 104, None, None, 40, cfg=cfg(max_hold_days=40))
    assert ex and "到期" in reason

def test_hold_not_expired_when_enabled():
    ex, _ = decide_exit(100, 105, 104, None, None, 39, cfg=cfg(max_hold_days=40))
    assert not ex


# ── 資金管理 ──────────────────────────────────────────────────────
def test_suggest_shares_respects_risk_budget():
    c = cfg(capital=300_000, risk_per_trade=0.01, stop_loss=0.08, pick_top_n=5)
    # 60 元：風險法 3000/(60*0.08)=625 股；集中度 60000/60=1000 股 → 取 625
    assert suggest_shares(60, c) == 625

def test_suggest_shares_respects_concentration_cap():
    c = cfg(capital=300_000, risk_per_trade=0.05, stop_loss=0.08, pick_top_n=5)
    # 風險法 15000/(60*0.08)=3125 股；集中度 60000/60=1000 股 → 取 1000
    assert suggest_shares(60, c) == 1000

def test_suggest_shares_zero_on_bad_price():
    assert suggest_shares(0) == 0
    assert suggest_shares(None) == 0

def test_format_size():
    assert format_size(0) == "資金不足（跳過或縮小停損）"
    assert format_size(500) == "500 股（零股）"
    assert format_size(2000) == "2 張"
    assert format_size(1500) == "1 張 + 500 股"


# ── 成交量天花板（2026-07-15 人類練習軌規格）───────────────────────
def test_suggest_shares_respects_volume_cap():
    c = cfg(capital=300_000, risk_per_trade=0.05, stop_loss=0.08, pick_top_n=5,
           max_pct_of_avg_volume=0.01)
    # 風險法/集中度都遠大於量能上限：均量100,000股 * 1% = 1000股
    assert suggest_shares(60, c, avg_volume=100_000) == 1000

def test_suggest_shares_volume_cap_not_binding_when_generous():
    c = cfg(capital=300_000, risk_per_trade=0.01, stop_loss=0.08, pick_top_n=5)
    # 均量很大時量能上限不生效，回到原本的風險法規則（625股，同上面的測試）
    assert suggest_shares(60, c, avg_volume=100_000_000) == 625

def test_suggest_shares_no_avg_volume_unaffected():
    c = cfg(capital=300_000, risk_per_trade=0.01, stop_loss=0.08, pick_top_n=5)
    assert suggest_shares(60, c) == 625   # 沒給 avg_volume 時行為不變（向下相容）


# ── PRACTICE_CFG：人類交易員練習軌權重/門檻 ─────────────────────────
def test_practice_cfg_disables_momentum_and_news_adjacent_factors():
    assert PRACTICE_CFG["w_rs"] == 0
    assert PRACTICE_CFG["w_momentum"] == 0
    assert PRACTICE_CFG["w_foreign_buy"] == 0
    assert PRACTICE_CFG["w_rev_yoy"] == 0   # 只看 rev_accel(>20%)，不看單純 >0

def test_practice_cfg_only_three_quant_factors_active():
    assert PRACTICE_CFG["w_invest_streak"] == 2.5
    assert PRACTICE_CFG["w_trend_stack"] == 1.5
    assert PRACTICE_CFG["w_rev_accel"] == 2.0

def test_practice_cfg_requires_above_ma20_and_top20():
    assert PRACTICE_CFG["above_ma20_only"] is True
    assert PRACTICE_CFG["pick_top_n"] == 20

def test_practice_cfg_inherits_liquidity_floor_from_strategy():
    assert PRACTICE_CFG["min_turnover_avg5"] == STRATEGY["min_turnover_avg5"]
    assert PRACTICE_CFG["min_close"] == STRATEGY["min_close"]


# ── 空方硬否決規則（2026-07-15，程式層級強制）─────────────────────
def _hv_df(**over):
    base = dict(stock_id=["1111"], stock_name=["測試股"], close=[100.0], ma20=[100.0],
               open=[95.0], high=[100.0], low=[95.0], volume=[1000.0], avg_volume=[1000.0])
    base.update(over)
    return pd.DataFrame(base)

def test_hard_veto_triggers_on_deviation_from_ma20():
    df = _hv_df(close=[120.0], ma20=[100.0])   # 乖離 +20% > 15% 門檻
    out = compute_hard_vetoes(df)
    assert len(out) == 1 and out.iloc[0]["stock_id"] == "1111"
    assert "乖離月線" in out.iloc[0]["hard_veto_reason"]

def test_hard_veto_not_triggered_within_deviation_threshold():
    df = _hv_df(close=[110.0], ma20=[100.0])   # 乖離 +10% < 15% 門檻
    assert compute_hard_vetoes(df).empty

def test_hard_veto_triggers_on_upper_wick_with_volume():
    # 開95收96，高100 → 上影線(100-96)/(100-95)=80% ≥ 60%；量2500 ≥ 均量1000*2.5
    df = _hv_df(close=[96.0], ma20=[90.0], open=[95.0], high=[100.0], low=[95.0],
               volume=[2500.0], avg_volume=[1000.0])
    out = compute_hard_vetoes(df)
    assert len(out) == 1
    assert "上引線" in out.iloc[0]["hard_veto_reason"]

def test_hard_veto_upper_wick_needs_both_ratio_and_volume():
    # 上引線比例夠，但量不夠(只有均量的1.2倍) → 不觸發
    df = _hv_df(close=[96.0], ma20=[90.0], open=[95.0], high=[100.0], low=[95.0],
               volume=[1200.0], avg_volume=[1000.0])
    assert compute_hard_vetoes(df).empty

def test_hard_veto_missing_ohlc_columns_skips_wick_rule_gracefully():
    df = pd.DataFrame(dict(stock_id=["1111"], close=[100.0], ma20=[100.0]))
    assert compute_hard_vetoes(df).empty   # 沒 OHLC/量欄位也不會噴例外

def test_hard_veto_empty_df_returns_empty():
    assert compute_hard_vetoes(pd.DataFrame()).empty


# ── 個股除權息還原（2026-07-17，SPEC_QUANT_UPGRADE.md P0-2）─────────
import datetime as _dt

def _dates(n):
    return [_dt.date(2025, 1, 1) + _dt.timedelta(days=i) for i in range(n)]

def test_total_return_adjust_no_events_returns_unchanged():
    idx = _dates(3)
    closes = pd.Series([100.0, 101.0, 102.0], index=idx)
    out = total_return_adjust(closes, pd.DataFrame())
    assert list(out) == [100.0, 101.0, 102.0]
    assert total_return_adjust(closes, None) is closes or list(total_return_adjust(closes, None)) == [100.0, 101.0, 102.0]

def test_total_return_adjust_removes_false_ex_dividend_drop():
    idx = _dates(5)
    # d1收盤100(除息前一日)，d2除息後真實收盤92（相對ref_price=90只小幅波動，
    # 不是真正暴跌——但原始序列 100→92 看起來像跌8%，還原後才知道其實是正常的）
    closes = pd.Series([100.0, 100.0, 92.0, 93.0, 95.0], index=idx)
    events = pd.DataFrame([{"ex_date": idx[2], "pre_close": 100.0, "ref_price": 90.0}])
    out = total_return_adjust(closes, events)
    # ratio = 90/100 = 0.9；ex_date(d2)之前的日期都要乘上這個比例
    assert out[idx[0]] == pytest.approx(90.0)
    assert out[idx[1]] == pytest.approx(90.0)
    # ex_date當天及之後不動（它已經是新的定價基準）
    assert out[idx[2]] == 92.0
    assert out[idx[3]] == 93.0
    assert out[idx[4]] == 95.0

def test_total_return_adjust_compounds_multiple_events():
    idx = _dates(6)
    closes = pd.Series([100.0, 100.0, 90.0, 90.0, 81.0, 82.0], index=idx)
    events = pd.DataFrame([
        {"ex_date": idx[2], "pre_close": 100.0, "ref_price": 90.0},   # ratio 0.9
        {"ex_date": idx[4], "pre_close": 90.0, "ref_price": 81.0},    # ratio 0.9
    ])
    out = total_return_adjust(closes, events)
    # d0,d1 早於兩個事件 → 兩個 ratio 都要乘（複利）：100*0.9*0.9=81
    assert out[idx[0]] == pytest.approx(81.0)
    assert out[idx[1]] == pytest.approx(81.0)
    # d2,d3 只早於第二個事件 → 只乘一次：90*0.9=81
    assert out[idx[2]] == pytest.approx(81.0)
    assert out[idx[3]] == pytest.approx(81.0)
    # d4之後不再調整
    assert out[idx[4]] == 81.0
    assert out[idx[5]] == 82.0

def test_total_return_adjust_short_series_unchanged():
    idx = _dates(1)
    closes = pd.Series([100.0], index=idx)
    out = total_return_adjust(closes, pd.DataFrame([{"ex_date": idx[0], "pre_close": 100.0, "ref_price": 90.0}]))
    assert list(out) == [100.0]

def test_total_return_adjust_ignores_invalid_events():
    idx = _dates(3)
    closes = pd.Series([100.0, 101.0, 102.0], index=idx)
    bad_events = pd.DataFrame([{"ex_date": idx[1], "pre_close": 0.0, "ref_price": 90.0}])  # pre_close=0 不合理，跳過
    out = total_return_adjust(closes, bad_events)
    assert list(out) == [100.0, 101.0, 102.0]

def test_apply_total_return_adjustment_only_touches_stocks_with_events():
    idx = _dates(3)
    piv = pd.DataFrame({"1111": [100.0, 100.0, 92.0], "2222": [50.0, 51.0, 52.0]}, index=idx)
    events = pd.DataFrame([{"stock_id": "1111", "ex_date": idx[2], "pre_close": 100.0, "ref_price": 90.0}])
    out = apply_total_return_adjustment(piv, events)
    assert out["1111"][idx[0]] == pytest.approx(90.0)      # 有事件的股票被還原
    assert list(out["2222"]) == [50.0, 51.0, 52.0]          # 沒事件的股票原樣不動

def test_apply_total_return_adjustment_empty_events_returns_unchanged():
    idx = _dates(2)
    piv = pd.DataFrame({"1111": [100.0, 101.0]}, index=idx)
    out = apply_total_return_adjustment(piv, pd.DataFrame())
    assert list(out["1111"]) == [100.0, 101.0]



# ── 投信新進場（2026-07-15，中小型股×投信剛開始買）───────────────────
def _invest_series(*vals):
    """依日期序列(股為單位)建一欄的 DataFrame，模擬 compute_new_entry_flag 的輸入。"""
    idx = pd.date_range("2026-01-01", periods=len(vals))
    return pd.DataFrame({"1111": vals}, index=idx)

def test_new_entry_true_on_day1_with_enough_lots():
    # 連買第1日，當日買超60張(60000股) ≥ 50張門檻
    df = _invest_series(60_000)
    out = compute_new_entry_flag(df, min_lots=50)
    assert out.iloc[-1]["1111"] == True

def test_new_entry_true_on_day2():
    df = _invest_series(60_000, 60_000)   # 連買第2日
    out = compute_new_entry_flag(df, min_lots=50)
    assert out.iloc[-1]["1111"] == True

def test_new_entry_false_on_day3_already_established():
    df = _invest_series(60_000, 60_000, 60_000)   # 連買第3日，不算「剛開始」
    out = compute_new_entry_flag(df, min_lots=50)
    assert out.iloc[-1]["1111"] == False

def test_new_entry_false_when_lots_too_small():
    df = _invest_series(10_000)   # 只有10張，< 50張門檻，雜訊排除
    out = compute_new_entry_flag(df, min_lots=50)
    assert out.iloc[-1]["1111"] == False

def test_new_entry_false_when_not_buying():
    df = _invest_series(60_000, -5_000)   # 昨天連買今天轉賣，streak斷了
    out = compute_new_entry_flag(df, min_lots=50)
    assert out.iloc[-1]["1111"] == False


# ── 流動性 OR 閘門（2026-07-15）───────────────────────────────────
def _liq_df(**over):
    base = dict(stock_id=["1111", "2222"], avg_turnover=[100_000_000.0, 300_000_000.0],
               invest_new_entry=[False, False])
    base.update(over)
    return pd.DataFrame(base)

def test_gate_passes_main_turnover_threshold():
    # 2222 成交金額3億 ≥ 2億主門檻 → 通過；1111 只有1億且無新進場 → 剔除
    out = apply_liquidity_gate(_liq_df(), cfg(allow_new_entry_alt_gate=True))
    assert list(out["stock_id"]) == ["2222"]

def test_gate_alt_path_admits_new_entry_small_cap():
    # 1111 成交金額1億(<2億但≥3千萬alt下限)+投信新進場 → OR閘門放行
    out = apply_liquidity_gate(_liq_df(invest_new_entry=[True, False]),
                               cfg(allow_new_entry_alt_gate=True))
    assert set(out["stock_id"]) == {"1111", "2222"}

def test_gate_alt_path_rejects_below_alt_floor_even_with_new_entry():
    # 投信新進場但成交金額只有2千萬，連替代下限(3千萬)都不到 → 仍剔除
    df = _liq_df(avg_turnover=[20_000_000.0, 300_000_000.0], invest_new_entry=[True, False])
    out = apply_liquidity_gate(df, cfg(allow_new_entry_alt_gate=True))
    assert list(out["stock_id"]) == ["2222"]

def test_gate_disabled_falls_back_to_single_threshold():
    # OR閘門關閉時（如PRACTICE_CFG），新進場不能當替代路徑
    out = apply_liquidity_gate(_liq_df(invest_new_entry=[True, False]),
                               cfg(allow_new_entry_alt_gate=False))
    assert list(out["stock_id"]) == ["2222"]

def test_gate_practice_cfg_has_or_gate_disabled():
    out = apply_liquidity_gate(_liq_df(invest_new_entry=[True, False]), PRACTICE_CFG)
    assert list(out["stock_id"]) == ["2222"]


# ── 百分位數流動性門檻（2026-07-19，10年回測發現固定金額門檻的疑點）───
def test_gate_percentile_mode_uses_percentile_not_absolute():
    df = pd.DataFrame(dict(stock_id=["1111", "2222", "3333"],
                           avg_turnover=[1_000_000.0, 2_000_000.0, 3_000_000.0],   # 遠低於絕對門檻2億
                           turnover_percentile=[0.2, 0.5, 0.9]))
    out = apply_liquidity_gate(df, cfg(min_turnover_percentile=0.6, allow_new_entry_alt_gate=False))
    assert list(out["stock_id"]) == ["3333"]   # 只有百分位>=0.6的通過，絕對金額完全不看

def test_gate_percentile_mode_falls_back_when_column_missing():
    # 設了門檻但df沒算turnover_percentile欄位 → 優雅退回絕對金額門檻，不報錯
    df = pd.DataFrame(dict(stock_id=["1111", "2222"],
                           avg_turnover=[100_000_000.0, 300_000_000.0]))
    out = apply_liquidity_gate(df, cfg(min_turnover_percentile=0.6, allow_new_entry_alt_gate=False))
    assert list(out["stock_id"]) == ["2222"]   # 用絕對門檻2億判斷

def test_gate_percentile_none_uses_absolute_threshold_unchanged():
    df = pd.DataFrame(dict(stock_id=["1111", "2222"],
                           avg_turnover=[100_000_000.0, 300_000_000.0],
                           turnover_percentile=[0.9, 0.1]))
    out = apply_liquidity_gate(df, cfg(min_turnover_percentile=None, allow_new_entry_alt_gate=False))
    assert list(out["stock_id"]) == ["2222"]   # 即使有turnover_percentile欄位，None時忽略它

def test_gate_empty_df():
    assert apply_liquidity_gate(pd.DataFrame(), cfg()).empty


# ── score_candidates：新增的兩個因子 ───────────────────────────────
def test_score_rewards_invest_new_entry():
    from agent.strategy import score_candidates
    df = pd.DataFrame(dict(
        stock_id=["1111", "2222"], signal_ma_cross=[0, 0], signal_breakout=[0, 0],
        macd_hist=[0.0, 0.0], inst_net=[0, 0], foreign_net=[0, 0], rsi14=[60, 60],
        invest_new_entry=[True, False],
    ))
    s = score_candidates(df, cfg(w_invest_new_entry=2.0))
    assert s.iloc[0] > s.iloc[1]

def test_score_rewards_etf_accum_count_capped():
    from agent.strategy import score_candidates
    df = pd.DataFrame(dict(
        stock_id=["1111", "2222", "3333"], signal_ma_cross=[0, 0, 0], signal_breakout=[0, 0, 0],
        macd_hist=[0.0, 0.0, 0.0], inst_net=[0, 0, 0], foreign_net=[0, 0, 0], rsi14=[60, 60, 60],
        etf_accum_count=[0, 1, 5],   # 5檔應被封頂在2檔的滿分
    ))
    s = score_candidates(df, cfg(w_etf_accum=1.0))
    assert s.iloc[1] > s.iloc[0]
    assert s.iloc[2] == s.iloc[1] * 2   # 5檔封頂=2檔滿分，剛好是1檔的兩倍
