"""agent/strategy.py 核心規則單元測試（不需 DB）。跑法：py -3.12 -m pytest tests/ -v"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.strategy import decide_exit, suggest_shares, format_size, STRATEGY


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


# ── 分級移動停利 ────────────────────────────────────────────────────
def test_trailing_stop_triggers_after_activation():
    # 峰值+20%（落在第一層 10%~40%），回落容忍度8%：120*0.92=110.4，跌破出場
    ex, reason = decide_exit(100, 120, 110.0, None, None, 5, cfg=cfg())
    assert ex and "移動停利" in reason

def test_trailing_stop_inactive_before_activation():
    # 峰值只有 +5%，未達第一層門檻(10%)，回落不出場
    ex, _ = decide_exit(100, 105, 99.5, None, None, 5,
                        cfg=cfg(stop_loss=0.08))
    assert not ex

def test_trailing_stop_wider_giveback_after_big_gain():
    # 峰值+150%（落在第三層 100%~200%，容忍度20%）：250*0.80=200，剛好等於門檻(<=)出場
    ex, reason = decide_exit(100, 250, 200.0, None, None, 5, cfg=cfg())
    assert ex and "移動停利" in reason
    # 站在門檻之上（200.5）不出場
    ex, _ = decide_exit(100, 250, 200.5, None, None, 5, cfg=cfg())
    assert not ex

def test_trailing_stop_would_have_stopped_out_small_gain_at_same_pullback():
    # 同樣的絕對跌幅，如果峰值只有 +20%（第一層8%），110左右的回落就會出場——
    # 驗證分級確實比固定8%給大波段更多空間，不是全域放寬。
    ex, reason = decide_exit(100, 120, 108.0, None, None, 5, cfg=cfg())
    assert ex and "移動停利" in reason


# ── 均線規則（現行預設關閉）────────────────────────────────────────
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


# ── 持有到期 ──────────────────────────────────────────────────────
def test_max_hold_days():
    ex, reason = decide_exit(100, 105, 104, None, None, 40, cfg=cfg())
    assert ex and "到期" in reason

def test_hold_not_expired():
    ex, _ = decide_exit(100, 105, 104, None, None, 39, cfg=cfg())
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
