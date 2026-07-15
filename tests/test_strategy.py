"""agent/strategy.py 核心規則單元測試（不需 DB）。跑法：py -3.12 -m pytest tests/ -v"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from agent.strategy import (decide_exit, suggest_shares, format_size, STRATEGY,
                            PRACTICE_CFG, compute_hard_vetoes,
                            compute_new_entry_flag, apply_liquidity_gate)


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


# ── E4 趨勢騎乘出場的現行預設（鎖定 2026-07-09 上線設定）──────────────
def test_death_cross_on_by_default():
    # 現行預設 exit_on_death_cross=True：MA5(99) < MA20(100) → 出場
    ex, reason = decide_exit(100, 105, 104, 99.0, 100.0, 5, cfg=cfg())
    assert ex and "死亡交叉" in reason

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
