"""SPEC §4.5 régime 切片的測試。

用建構出來的行情驗「該被標成什麼就標成什麼」——régime 判錯的話，整份歸因報告
會把多頭段的失血算到空頭段頭上，比沒有報告更糟。
"""
import numpy as np
import pandas as pd
import pytest

from research.regime_report import (BEAR, BULL, RANGE, classify_regimes,
                                    format_report, regime_metrics)


def _series(vals, start="2015-01-01"):
    return pd.Series(vals, index=pd.date_range(start, periods=len(vals)).date)


# ── régime 判定 ──────────────────────────────────────────────────
def test_steady_uptrend_is_bull():
    s = _series(np.linspace(100, 300, 400))
    reg = classify_regimes(s)
    assert (reg.iloc[100:] == BULL).all()


def test_steady_downtrend_is_bear():
    s = _series(np.linspace(300, 100, 400))
    reg = classify_regimes(s)
    assert (reg.iloc[100:] == BEAR).all()


def test_flat_market_is_never_bull_or_bear():
    """完全走平時斜率為 0，不該被判成任何方向——寧可說不知道。"""
    reg = classify_regimes(_series(np.full(300, 100.0)))
    assert (reg == RANGE).all()


def test_warmup_period_defaults_to_range():
    """資料不足算不出 MA60 時要標盤整，不能假裝知道。"""
    reg = classify_regimes(_series(np.linspace(100, 200, 200)))
    assert (reg.iloc[:29] == RANGE).all()


def test_price_above_ma_but_ma_falling_is_range():
    """跌深反彈初期：價格已站上均線，但 MA60 仍下彎——這是盤整不是多頭。

    反彈天數要夠短（25 天）MA60 才還沒轉正；反彈滿 60 天後 MA60 本來就該翻多，
    那時判成多頭是對的（第一版測試用 60 天，是測試寫錯不是程式錯）。
    """
    s = _series(np.concatenate([np.linspace(300, 100, 200), np.linspace(100, 150, 25)]))
    reg = classify_regimes(s)
    assert reg.iloc[-1] == RANGE


def test_all_labels_are_valid():
    rng = np.random.default_rng(0)
    reg = classify_regimes(_series(100 * np.cumprod(1 + rng.normal(0, 0.02, 500))))
    assert set(reg.unique()) <= {BULL, BEAR, RANGE}


# ── 指標計算 ─────────────────────────────────────────────────────
def _nav(rets, start=100.0):
    eq = start * np.cumprod(1 + np.asarray(rets))
    return dict(zip(pd.date_range("2015-01-01", periods=len(eq)).date, eq))


def test_metrics_rows_cover_three_regimes():
    nav = _nav(np.random.default_rng(1).normal(0.0005, 0.01, 300))
    reg = classify_regimes(_series(np.linspace(100, 200, 300)))
    df = regime_metrics(nav, reg)
    assert list(df["régime"]) == [BULL, RANGE, BEAR]
    assert df["天數佔比"].sum() == pytest.approx(1.0, abs=1e-9)


def test_metrics_attributes_returns_to_the_right_regime():
    """多頭段全賺、空頭段全賠，指標必須把它們分開——分錯就整份報告失效。"""
    n = 400
    mkt = np.concatenate([np.linspace(100, 200, n // 2), np.linspace(200, 100, n // 2)])
    reg = classify_regimes(_series(mkt))
    rets = np.where(np.array(reg == BULL), 0.01, -0.01)
    df = regime_metrics(_nav(rets), reg).set_index("régime")
    assert df.loc[BULL, "累積報酬"] > 0
    assert df.loc[BEAR, "累積報酬"] < 0


def test_metrics_with_trades_computes_winrate():
    reg = classify_regimes(_series(np.linspace(100, 200, 300)))
    idx = list(reg.index)
    trades = pd.DataFrame({"entry_date": [idx[150], idx[160], idx[170]],
                           "net_ret": [0.1, -0.05, 0.2]})
    df = regime_metrics(_nav(np.full(300, 0.001)), reg, trades=trades).set_index("régime")
    assert df.loc[BULL, "進場筆數"] == 3
    assert df.loc[BULL, "勝率"] == pytest.approx(2 / 3)


def test_metrics_with_benchmark_reports_excess():
    reg = classify_regimes(_series(np.linspace(100, 200, 300)))
    df = regime_metrics(_nav(np.full(300, 0.002)), reg,
                        nav_bench=_nav(np.full(300, 0.001))).set_index("régime")
    assert df.loc[BULL, "超額"] > 0


def test_format_report_names_best_and_worst_regime():
    reg = classify_regimes(_series(np.concatenate(
        [np.linspace(100, 200, 200), np.linspace(200, 100, 200)])))
    rets = np.where(np.array(reg == BULL), -0.002, 0.002)   # 刻意做成多頭段最弱
    df = regime_metrics(_nav(rets), reg, nav_bench=_nav(np.full(400, 0.001)))
    txt = format_report(df)
    assert "régime 切片" in txt and "多頭" in txt and "空頭" in txt
    assert "最強" in txt and "最弱" in txt


def test_format_report_without_optional_columns():
    """沒有 benchmark/trades 時也要能印，不能因為缺欄位就整份報告掛掉。"""
    reg = classify_regimes(_series(np.linspace(100, 200, 200)))
    txt = format_report(regime_metrics(_nav(np.full(200, 0.001)), reg))
    assert "régime 切片" in txt
