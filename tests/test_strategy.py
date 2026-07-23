"""agent/strategy.py 核心規則單元測試（不需 DB）。跑法：py -3.12 -m pytest tests/ -v"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

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


# ── ICIR 加權標準化合成（§5決策1，2026-07-23 A/B後不採用，但程式保留為 opt-in）──
def _icir_cfg(**kw):
    from agent.strategy import STRATEGY
    return {**STRATEGY, "score_mode": "icir", **kw}


def _icir_df(**cols):
    base = dict(stock_id=["1111", "2222", "3333"],
                signal_ma_cross=[0, 0, 0], signal_breakout=[0, 0, 0],
                macd_hist=[0.0, 0.0, 0.0], inst_net=[0, 0, 0],
                foreign_net=[0, 0, 0], rsi14=[60, 60, 60])
    base.update(cols)
    return pd.DataFrame(base)


def test_default_score_mode_is_manual_not_icir():
    """A/B 沒勝出 → 預設必須維持手調權重，這條防止不小心把預設改掉。"""
    from agent.strategy import STRATEGY
    assert STRATEGY.get("score_mode") == "manual"


def test_icir_mode_keeps_magnitude_unlike_binary_flags():
    """現行手調版把 rev_yoy 二元化(>0/>20%)，+555% 與 +21% 同分；ICIR 版應區分得出來。"""
    from agent.strategy import score_candidates
    df = _icir_df(rev_yoy=[21.0, 555.0, 25.0])
    manual = score_candidates(df, cfg(w_rev_yoy=3.0, w_rev_accel=1.0))
    assert manual.iloc[0] == manual.iloc[1]          # 二元旗標：兩者同分（就是被質疑的點）
    icir = score_candidates(df, _icir_cfg())
    assert icir.iloc[1] > icir.iloc[0]               # ICIR版：量級有反映出來


def test_icir_negative_icir_factors_get_zero_weight():
    """rs20/mom60 的 ICIR 在 h20 是負的 → 權重應為 0，分數不受它們影響。"""
    from agent.strategy import score_candidates
    a = score_candidates(_icir_df(rs20=[0.99, 0.01, 0.5], mom60=[3.0, -3.0, 0.0]), _icir_cfg())
    b = score_candidates(_icir_df(rs20=[0.01, 0.99, 0.5], mom60=[-3.0, 3.0, 0.0]), _icir_cfg())
    assert list(a.round(6)) == list(b.round(6))      # 把兩者對調，分數完全不變


def test_icir_rank_norm_is_outlier_robust():
    """排序標準化：極端值只佔一個名次，不會像原始z-score那樣主導尺度。"""
    from agent.strategy import _rank_norm
    s = _rank_norm(pd.Series([1.0, 2.0, 3.0, 1_000_000.0]))
    assert s.max() <= 1.0 and s.min() > 0
    assert s.iloc[3] > s.iloc[2] > s.iloc[1]         # 順序保留
    assert s.iloc[2] - s.iloc[1] == s.iloc[3] - s.iloc[2]   # 間距均勻，不被離群值撐開


def test_icir_missing_column_is_skipped_not_crash():
    from agent.strategy import score_candidates
    s = score_candidates(_icir_df(), _icir_cfg())    # 完全沒有因子欄位
    assert len(s) == 3 and s.notna().all()


def test_icir_unknown_horizon_returns_zero_scores():
    from agent.strategy import score_candidates
    s = score_candidates(_icir_df(rev_yoy=[1.0, 2.0, 3.0]), _icir_cfg(icir_horizon=999))
    assert (s == 0).all()


# ── 策略版本沿革 STRATEGY_ERAS（2026-07-23）──────────────────────────
# 原本 🔄歷史績效頁寫死 STRATEGY_V2_DATE，策略改了好幾輪沒人更新，長期把舊策略的
# 交易標成「現行策略績效」。改成清單後這幾條測試鎖住它的結構不被寫壞。
def test_eras_sorted_newest_first_and_unique():
    from agent.strategy import STRATEGY_ERAS
    dates = [e["live_from"] for e in STRATEGY_ERAS]
    assert dates == sorted(dates, reverse=True), "必須由新到舊排序（strategy_era_for 依賴這點）"
    assert len(set(dates)) == len(dates), "生效日不可重複"
    assert len({e["key"] for e in STRATEGY_ERAS}) == len(STRATEGY_ERAS)


def test_every_era_has_required_fields():
    from agent.strategy import STRATEGY_ERAS
    for e in STRATEGY_ERAS:
        assert e.get("label") and e.get("desc"), f"{e.get('key')} 缺 label/desc"
        assert isinstance(e["live_from"], date)


def test_strategy_era_for_picks_right_version():
    from agent.strategy import STRATEGY_ERAS, strategy_era_for
    cur, prev = STRATEGY_ERAS[0], STRATEGY_ERAS[1]
    assert strategy_era_for(cur["live_from"])["key"] == cur["key"]
    assert strategy_era_for(cur["live_from"] - timedelta(days=1))["key"] == prev["key"]
    assert strategy_era_for(prev["live_from"])["key"] == prev["key"]


def test_oldest_era_is_catch_all():
    """最舊那筆要能接住任意早的日期，否則 strategy_era_for 會漏掉極早的交易。"""
    from agent.strategy import strategy_era_for
    assert strategy_era_for(date(2001, 1, 1)) is not None


# ── 券資比 short_ratio（2026-07-23，IC過關、A/B待驗，預設0不啟用）─────────
def _sr_df(ratios):
    n = len(ratios)
    return pd.DataFrame(dict(
        stock_id=[str(i) for i in range(n)], signal_ma_cross=[0]*n, signal_breakout=[0]*n,
        macd_hist=[0.0]*n, inst_net=[0]*n, foreign_net=[0]*n, rsi14=[60]*n,
        short_ratio=ratios))


def _sr_cfg(w):
    """只留券資比一個因子，其餘歸零，方便單獨驗證它的行為。"""
    return {**STRATEGY, "w_short_ratio": w, "w_rev_yoy": 0, "w_rev_accel": 0,
            "w_invest_streak": 0, "w_invest_new_entry": 0, "w_foreign_buy": 0,
            "w_trend_stack": 0, "w_etf_accum": 0}


def test_short_ratio_default_off():
    """IC 過關不等於可用，A/B 沒驗完前預設必須是 0（這條防止不小心開啟）。"""
    assert STRATEGY.get("w_short_ratio") == 0.0


def test_short_ratio_low_scores_higher():
    """IC 為負＝券資比越低後續報酬越好，所以低券資比要拿高分（方向不能搞反）。"""
    from agent.strategy import score_candidates
    s = score_candidates(_sr_df([0.001, 0.05, 0.5]), _sr_cfg(1.0))
    assert s.iloc[0] > s.iloc[1] > s.iloc[2]


def test_short_ratio_zero_weight_has_no_effect():
    from agent.strategy import score_candidates
    s = score_candidates(_sr_df([0.001, 0.05, 0.5]), _sr_cfg(0.0))
    assert (s == 0).all()


def test_short_ratio_uses_rank_not_raw_value():
    """券資比分佈極右偏，必須用排序；用原始值的話單一極端值會主導整個尺度。"""
    from agent.strategy import score_candidates
    normal = score_candidates(_sr_df([0.01, 0.02, 0.03]), _sr_cfg(1.0))
    with_outlier = score_candidates(_sr_df([0.01, 0.02, 999.0]), _sr_cfg(1.0))
    assert list(normal.round(6)) == list(with_outlier.round(6))   # 名次相同→分數相同


def test_short_ratio_missing_values_are_neutral():
    from agent.strategy import score_candidates
    s = score_candidates(_sr_df([0.01, float("nan"), 0.5]), _sr_cfg(1.0))
    assert s.notna().all()
    assert s.iloc[0] > s.iloc[2]                                   # 有值的仍照方向排


# ── 波段進場型態 compute_swing_setup（2026-07-23，練習軌用）────────────
# 構造：前20日寬幅震盪(100~110) → 近5日窄幅盤整(~100) → 第26日帶量紅K突破
_N = 26
_IDX = pd.date_range("2026-01-01", periods=_N, freq="D")
_HI = [110]*20 + [101, 101.5, 101, 101.2, 101] + [106]
_LO = [100]*20 + [99, 99.5, 99, 99.2, 99] + [100.5]
_OP = [105]*20 + [100, 100.5, 100, 100.5, 100] + [100.8]
_CL = [105]*20 + [100, 101, 100.5, 101, 100.5] + [105.8]
_VO = [5000]*20 + [1000]*5 + [3000]


def _panel(v):
    return pd.DataFrame({"X": v}, index=_IDX)


def _setup(op=None, hi=None, lo=None, cl=None, vo=None, ma20=None, ma60=None, cfg=None):
    from agent.strategy import compute_swing_setup
    return compute_swing_setup(
        _panel(op or _OP), _panel(hi or _HI), _panel(lo or _LO), _panel(cl or _CL),
        _panel(vo or _VO), ma20 if ma20 is not None else _panel([98]*_N),
        ma60 if ma60 is not None else _panel([95]*_N), cfg)["X"]


def test_swing_setup_triggers_only_on_breakout_day():
    m = _setup()
    assert m.sum() == 1 and bool(m.iloc[-1]), "應只在突破當日觸發"


def test_swing_setup_rejects_low_breakout_volume():
    assert not _setup(vo=[5000]*20 + [1000]*5 + [1100]).any()      # 僅1.1倍量


def test_swing_setup_rejects_close_inside_box():
    # 沒突破箱頂（收在盤整區間內）＝不是突破
    assert not _setup(cl=[105]*20 + [100, 101, 100.5, 101, 100.5] + [101.0]).any()


def test_swing_setup_rejects_long_upper_wick():
    # 衝高回落（長上影線）方向與型態相反，是出貨不是突破
    assert not _setup(hi=[110]*20 + [101, 101.5, 101, 101.2, 101] + [112]).any()


def test_swing_setup_rejects_black_candle():
    assert not _setup(op=[105]*20 + [100, 100.5, 100, 100.5, 100] + [106.5]).any()


def test_swing_setup_rejects_downtrend():
    """趨勢過濾：10年消融證實這是最有價值的一條（關掉後 CAR20 0.40%→0.05%）。"""
    assert not _setup(ma20=_panel([120]*_N), ma60=_panel([130]*_N)).any()


def test_swing_setup_conditions_are_toggleable():
    """每條都要能關掉——回測消融靠這個逐條驗證哪個真的有用。"""
    assert not _setup(ma20=_panel([120]*_N), ma60=_panel([130]*_N)).any()
    assert _setup(ma20=_panel([120]*_N), ma60=_panel([130]*_N),
                  cfg={"require_trend": False}).any()


def test_swing_setup_realigns_mismatched_panels():
    """欄位/日期沒對齊時要自動 reindex，不能丟 'identically-labeled' 例外——
    接進即時路徑時實際踩過，型態被靜默降級成純分數排序。"""
    from agent.strategy import compute_swing_setup
    ma_short = pd.DataFrame({"X": [98]*_N, "Y": [98]*_N}, index=_IDX)   # 多一欄
    m = compute_swing_setup(_panel(_OP), _panel(_HI), _panel(_LO), _panel(_CL),
                            _panel(_VO), ma_short, _panel([95]*_N))
    assert list(m.columns) == ["X"] and bool(m["X"].iloc[-1])


def test_swing_setup_defaults_match_ablation_evidence():
    """預設值由 10 年消融決定：量縮/相對振幅實測會讓 CAR 變差，故預設關閉。"""
    from agent.strategy import SWING_SETUP_CFG
    assert SWING_SETUP_CFG["require_vol_dryup"] is False
    assert not SWING_SETUP_CFG["contraction_ratio"]
    assert SWING_SETUP_CFG["require_trend"] is True      # 最有價值的一條，必須開
