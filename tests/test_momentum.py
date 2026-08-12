"""MOM-1 訊號引擎的 F0 正確性測試（SPEC_DATA_FOUNDATION_AND_MOMENTUM.md §9.1）。

全部使用合成 fixtures，不讀取任何 snapshot，因此不觸及 2008~2014 backward holdout，
也不計算任何報酬。測試涵蓋：時間對齊、缺失交易日、universe 成員資格、
§7.1 濾網，以及 look-ahead 洩漏的防止。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.momentum import (
    ENTRY_TOP_FRAC,
    EXIT_BOTTOM_FRAC,
    FORMATION_LOOKBACK,
    FORMATION_SKIP,
    MIN_HISTORY_SESSIONS,
    build_pit_master,
    compute_mom_6_1,
    disposition_restriction_frame,
    eligible_universe,
    month_end_sessions,
    next_session,
    pit_common_stock_mask,
    pit_common_stock_mask_from_universe_history,
    rank_percentile,
    select_holdings,
)


# ══════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════
def make_sessions(n: int, start: str = "2006-01-02") -> pd.DatetimeIndex:
    """n 個「交易日」，刻意用 B frequency 讓週末天然缺席。"""
    return pd.bdate_range(start=start, periods=n)


@pytest.fixture
def flat_panel():
    """400 個交易日、6 檔股票，價格與成交金額皆為常數且互不相同。"""
    sessions = make_sessions(400)
    ids = [f"{1000 + i}" for i in range(6)]
    close = pd.DataFrame(
        {sid: np.linspace(50.0, 100.0, len(sessions)) * (1 + i * 0.1)
         for i, sid in enumerate(ids)},
        index=sessions,
    )
    turnover = pd.DataFrame(
        {sid: np.full(len(sessions), 1e8 * (i + 1)) for i, sid in enumerate(ids)},
        index=sessions,
    )
    return sessions, ids, close, turnover


def make_master(ids, *, asset_types=None, effective_from=None, delisting=None):
    n = len(ids)
    return build_pit_master(pd.DataFrame({
        "stock_id": ids,
        "market": ["TWSE"] * n,
        "asset_type": asset_types or ["common_stock"] * n,
        "effective_from": effective_from or ["2000-01-01"] * n,
        "delisting_date": delisting or [None] * n,
    }))


# ══════════════════════════════════════════════════════════════════
#  時間對齊：月末決策日與 T+1 成交
# ══════════════════════════════════════════════════════════════════
def test_month_end_uses_last_actual_session_not_calendar_month_end():
    # 2006-01-31 是週二；把它從交易日曆移除，月末決策日必須退到 01-30，
    # 不得產生一個沒有行情的日期。
    sessions = make_sessions(60).drop(pd.Timestamp("2006-01-31"))
    ends = month_end_sessions(sessions)
    assert pd.Timestamp("2006-01-30") in ends
    assert pd.Timestamp("2006-01-31") not in ends
    assert set(ends).issubset(set(sessions))


def test_month_end_skips_months_with_no_sessions():
    sessions = pd.DatetimeIndex(["2006-01-03", "2006-01-04", "2006-03-01"])
    ends = month_end_sessions(sessions)
    assert list(ends) == [pd.Timestamp("2006-01-04"), pd.Timestamp("2006-03-01")]
    assert not any(d.month == 2 for d in ends)


def test_next_session_is_strictly_after_and_skips_gaps():
    sessions = pd.DatetimeIndex(["2006-01-03", "2006-01-04", "2006-01-10"])
    # 決策日當天不得成交（§7.5 最早 T+1）。
    assert next_session(sessions, "2006-01-04") == pd.Timestamp("2006-01-10")
    # 中間停市 5 天也要跳到真正的下一個交易日，不是日曆隔天。
    assert next_session(sessions, "2006-01-05") == pd.Timestamp("2006-01-10")


def test_next_session_returns_none_at_sample_end():
    """樣本尾端沒有下一個交易日時必須回傳 None，讓呼叫端無法假成交。"""
    sessions = pd.DatetimeIndex(["2006-01-03", "2006-01-04"])
    assert next_session(sessions, "2006-01-04") is None


# ══════════════════════════════════════════════════════════════════
#  形成訊號：窗長與 look-ahead
# ══════════════════════════════════════════════════════════════════
def test_mom_6_1_uses_exact_skip_and_lookback_offsets(flat_panel):
    sessions, ids, close, _ = flat_panel
    signal = compute_mom_6_1(close)
    d = sessions[300]
    sid = ids[0]
    expected = (
        close[sid].iloc[300 - FORMATION_SKIP] / close[sid].iloc[300 - FORMATION_LOOKBACK] - 1.0
    )
    assert signal.loc[d, sid] == pytest.approx(expected)


def test_mom_6_1_is_null_before_lookback_window_is_full(flat_panel):
    sessions, _, close, _ = flat_panel
    signal = compute_mom_6_1(close)
    assert signal.iloc[FORMATION_LOOKBACK - 1].isna().all()
    assert signal.iloc[FORMATION_LOOKBACK].notna().all()


def test_mom_6_1_ignores_future_prices(flat_panel):
    """把決策日之後的價格全部改掉，訊號必須完全不變——這是 look-ahead 的直接反證。"""
    sessions, _, close, _ = flat_panel
    d = sessions[300]
    baseline = compute_mom_6_1(close).loc[d]

    tampered = close.copy()
    tampered.iloc[301:] *= 7.5
    assert compute_mom_6_1(tampered).loc[d].equals(baseline)


def test_mom_6_1_rejects_unsorted_index(flat_panel):
    sessions, _, close, _ = flat_panel
    with pytest.raises(ValueError, match="升冪"):
        compute_mom_6_1(close.iloc[::-1])


def test_mom_6_1_rejects_duplicate_sessions(flat_panel):
    _, _, close, _ = flat_panel
    duplicated = pd.concat([close.iloc[:1], close])
    with pytest.raises(ValueError, match="不可重複"):
        compute_mom_6_1(duplicated)


def test_mom_6_1_shift_is_by_session_not_calendar_day():
    """跨越長假時，位移必須是交易日列數，不是日曆天數。"""
    sessions = pd.DatetimeIndex(
        list(pd.bdate_range("2006-01-02", periods=130))
    )
    # 在第 100 列之後插入一段 30 天的日曆空窗，但列數不變。
    gap = pd.DateOffset(days=30)
    shifted = sessions[:100].append(pd.DatetimeIndex([d + gap for d in sessions[100:]]))
    close = pd.DataFrame({"1101": np.arange(1.0, 131.0)}, index=shifted)
    signal = compute_mom_6_1(close)
    d = shifted[125]
    assert signal.loc[d, "1101"] == pytest.approx(
        close["1101"].iloc[105] / close["1101"].iloc[5] - 1.0
    )


# ══════════════════════════════════════════════════════════════════
#  Universe 成員資格：PIT 而非代號啟發式
# ══════════════════════════════════════════════════════════════════
def test_pit_mask_excludes_non_common_stock_regardless_of_ticker():
    """核心稽核點：ETF／TDR 要靠 asset_type 排除，不是靠代號開頭。
    這裡刻意讓非普通股用 4 位數代號、普通股用 00 開頭代號，
    舊的 startswith("00") 啟發式會兩邊都判錯。"""
    ids = ["0050", "1234", "2330"]
    master = make_master(
        ids, asset_types=["common_stock", "depositary_receipt", "common_stock"]
    )
    mask = pit_common_stock_mask(master, "2006-06-30", ids)
    assert mask["0050"]      # 代號像 ETF，但 master 說是普通股
    assert not mask["1234"]  # 代號像普通股，但 master 說是 TDR
    assert mask["2330"]


def test_pit_mask_respects_listing_and_delisting_boundaries():
    ids = ["1101", "1102", "1103"]
    master = make_master(
        ids,
        effective_from=["2006-06-30", "2006-07-01", "2000-01-01"],
        delisting=[None, None, "2006-06-30"],
    )
    mask = pit_common_stock_mask(master, "2006-06-30", ids)
    assert mask["1101"]       # 生效日當天即可納入
    assert not mask["1102"]   # 生效日前一天不得納入
    assert not mask["1103"]   # 下市日當天即排除（嚴格小於）


def test_pit_mask_excludes_ids_absent_from_master():
    master = make_master(["1101"])
    mask = pit_common_stock_mask(master, "2006-06-30", ["1101", "9999"])
    assert mask["1101"]
    assert not mask["9999"]


def test_build_pit_master_refuses_to_fall_back_when_asset_type_missing():
    frame = pd.DataFrame({
        "stock_id": ["1101"], "market": ["TWSE"], "effective_from": ["2000-01-01"],
    })
    with pytest.raises(ValueError, match="asset_type"):
        build_pit_master(frame)


def test_build_pit_master_rejects_null_effective_from():
    frame = pd.DataFrame({
        "stock_id": ["1101"], "market": ["TWSE"],
        "asset_type": ["common_stock"], "effective_from": [None],
    })
    with pytest.raises(ValueError, match="effective_from"):
        build_pit_master(frame)


def test_universe_history_path_never_reads_a_future_snapshot():
    history = pd.DataFrame({
        "snapshot_date": ["2006-06-30", "2006-06-30", "2006-07-31"],
        "stock_id": ["1101", "1102", "1103"],
        "market": ["TWSE"] * 3,
        "asset_type": ["common_stock"] * 3,
        "is_active": [True, False, True],
    })
    mask = pit_common_stock_mask_from_universe_history(
        history, "2006-07-15", ["1101", "1102", "1103"]
    )
    assert mask["1101"]
    assert not mask["1102"]   # is_active=False
    assert not mask["1103"]   # 只存在於未來的 07-31 snapshot


def test_universe_history_returns_all_false_before_first_snapshot():
    history = pd.DataFrame({
        "snapshot_date": ["2006-06-30"], "stock_id": ["1101"],
        "market": ["TWSE"], "asset_type": ["common_stock"], "is_active": [True],
    })
    mask = pit_common_stock_mask_from_universe_history(history, "2005-01-31", ["1101"])
    assert not mask.any()


def test_both_pit_paths_agree_at_a_month_end():
    ids = ["1101", "1102", "0050"]
    master = make_master(
        ids,
        asset_types=["common_stock", "common_stock", "non_company_security"],
        delisting=[None, "2006-05-01", None],
    )
    history = pd.DataFrame({
        "snapshot_date": ["2006-06-30"] * 3,
        "stock_id": ids,
        "market": ["TWSE"] * 3,
        "asset_type": ["common_stock", "common_stock", "non_company_security"],
        "is_active": [True, False, True],
    })
    from_master = pit_common_stock_mask(master, "2006-06-30", ids)
    from_history = pit_common_stock_mask_from_universe_history(history, "2006-06-30", ids)
    pd.testing.assert_series_equal(from_master, from_history)


def test_disposition_restriction_frame_expands_inclusive_trading_sessions():
    sessions = pd.DatetimeIndex(["2006-01-02", "2006-01-03", "2006-01-06"])
    events = pd.DataFrame({
        "stock_id": ["1101"],
        "announce_date": ["2006-01-01"],
        "start_date": ["2006-01-03"],
        "end_date": ["2006-01-06"],
        "market": ["TWSE"],
    })
    restricted = disposition_restriction_frame(events, sessions, ["1101", "1102"])
    assert not restricted.loc["2006-01-02", "1101"]
    assert restricted.loc["2006-01-03", "1101"]
    assert restricted.loc["2006-01-06", "1101"]
    assert not restricted["1102"].any()


def test_disposition_restriction_frame_rejects_future_announcement():
    events = pd.DataFrame({
        "stock_id": ["1101"],
        "announce_date": ["2006-01-04"],
        "start_date": ["2006-01-03"],
        "end_date": ["2006-01-06"],
    })
    with pytest.raises(ValueError, match="前視"):
        disposition_restriction_frame(events, ["2006-01-03"], ["1101"])


# ══════════════════════════════════════════════════════════════════
#  §7.1 濾網
# ══════════════════════════════════════════════════════════════════
def run_universe(sessions, ids, close, turnover, as_of, **kwargs):
    signal = compute_mom_6_1(close)
    defaults = dict(
        as_of=as_of,
        adjusted_close=close,
        raw_close=close,
        turnover=turnover,
        signal=signal,
        pit_mask=make_pit_mask(ids),
        allow_missing_restrictions=True,
    )
    defaults.update(kwargs)
    return eligible_universe(**defaults)


def make_pit_mask(ids, excluded=()):
    return pd.Series([sid not in excluded for sid in ids], index=ids, dtype=bool)


def test_universe_applies_price_floor_on_raw_not_adjusted_close(flat_panel):
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    raw = close.copy()
    raw[ids[0]] = 5.0          # 實際股價低於 10 元
    result = eligible_universe(
        as_of=d, adjusted_close=close, raw_close=raw, turnover=turnover,
        signal=compute_mom_6_1(close), pit_mask=make_pit_mask(ids),
        allow_missing_restrictions=True,
    )
    assert ids[0] not in result


def test_universe_requires_minimum_history(flat_panel):
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    short = close.copy()
    short.iloc[: 300 - MIN_HISTORY_SESSIONS + 2, short.columns.get_loc(ids[1])] = np.nan
    result = run_universe(sessions, ids, short, turnover, d,
                          adjusted_close=short, raw_close=close,
                          signal=compute_mom_6_1(short))
    assert ids[1] not in result


def test_universe_keeps_only_top_half_by_liquidity(flat_panel):
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    result = run_universe(sessions, ids, close, turnover, d)
    # turnover 依序遞增，6 檔取前 50% → 最高的 3 檔。
    assert set(result) == {ids[3], ids[4], ids[5]}


def test_liquidity_percentile_denominator_is_all_pit_common_stock(flat_panel):
    """§7.1.5 的母體是「當日 TWSE 普通股」，不是已通過價格／歷史門檻的子集。

    這裡把成交金額最低的 3 檔全部壓到 10 元門檻以下。若分母錯用已過濾的子集，
    中位數會由高流動性的 3 檔重算、門檻被抬高，只剩最高的 2 檔通過；
    正確的做法是門檻仍由全體 6 檔普通股決定，因此高流動性的 3 檔全數保留。
    """
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    raw = close.copy()
    for sid in ids[:3]:
        raw[sid] = 5.0
    result = eligible_universe(
        as_of=d, adjusted_close=close, raw_close=raw, turnover=turnover,
        signal=compute_mom_6_1(close), pit_mask=make_pit_mask(ids),
        allow_missing_restrictions=True,
    )
    assert set(result) == {ids[3], ids[4], ids[5]}


def test_liquidity_denominator_excludes_non_common_stock(flat_panel):
    """非普通股不得進入分位數分母——否則一批低流動性 ETF 會壓低門檻。"""
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    # 前 3 檔改判為非普通股：分母只剩後 3 檔，門檻升為其中位數。
    result = eligible_universe(
        as_of=d, adjusted_close=close, raw_close=close, turnover=turnover,
        signal=compute_mom_6_1(close), pit_mask=make_pit_mask(ids, excluded=ids[:3]),
        allow_missing_restrictions=True,
    )
    assert set(result) == {ids[4], ids[5]}


def test_universe_drops_stock_with_incomplete_liquidity_window(flat_panel):
    """缺一天成交金額就淘汰，不改用較短窗平均——§7.1.7 缺值不得補 0。"""
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    holed = turnover.copy()
    holed.iloc[295, holed.columns.get_loc(ids[5])] = np.nan
    result = run_universe(sessions, ids, close, holed, d)
    assert ids[5] not in result


def test_universe_excludes_restricted_names(flat_panel):
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    restricted = pd.DataFrame(False, index=sessions, columns=ids)
    restricted.loc[d, ids[5]] = True
    result = run_universe(sessions, ids, close, turnover, d,
                          restricted=restricted, allow_missing_restrictions=False)
    assert ids[5] not in result
    assert ids[4] in result


def test_universe_blocks_everything_when_restriction_row_is_missing(flat_panel):
    """有 restricted 表但查無當日 → 可交易狀態未知，未知不等於可交易。"""
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    restricted = pd.DataFrame(False, index=sessions.drop(d), columns=ids)
    result = run_universe(sessions, ids, close, turnover, d,
                          restricted=restricted, allow_missing_restrictions=False)
    assert len(result) == 0


def test_universe_refuses_to_silently_skip_investability_screen(flat_panel):
    """§7.1.6 沒資料時必須顯式承認，不能靠預設值悄悄跳過。"""
    sessions, ids, close, turnover = flat_panel
    with pytest.raises(ValueError, match="§7.1.6"):
        eligible_universe(
            as_of=sessions[300], adjusted_close=close, raw_close=close,
            turnover=turnover, signal=compute_mom_6_1(close),
            pit_mask=make_pit_mask(ids),
        )


def test_universe_is_sorted_and_deterministic(flat_panel):
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    first = run_universe(sessions, ids, close, turnover, d)
    shuffled = close[list(reversed(ids))]
    second = eligible_universe(
        as_of=d, adjusted_close=shuffled, raw_close=shuffled,
        turnover=turnover[list(reversed(ids))], signal=compute_mom_6_1(shuffled),
        pit_mask=make_pit_mask(list(reversed(ids))), allow_missing_restrictions=True,
    )
    assert list(first) == sorted(first)
    assert list(first) == list(second)


def test_universe_ignores_data_after_as_of(flat_panel):
    """把決策日之後的價格與成交金額全部竄改，合格清單必須不變。"""
    sessions, ids, close, turnover = flat_panel
    d = sessions[300]
    baseline = run_universe(sessions, ids, close, turnover, d)

    future_close = close.copy()
    future_turnover = turnover.copy()
    future_close.iloc[301:] = 1.0
    future_turnover.iloc[301:] = future_turnover.iloc[301:].values[:, ::-1]
    tampered = eligible_universe(
        as_of=d, adjusted_close=future_close, raw_close=future_close,
        turnover=future_turnover, signal=compute_mom_6_1(future_close),
        pit_mask=make_pit_mask(ids), allow_missing_restrictions=True,
    )
    assert list(tampered) == list(baseline)


# ══════════════════════════════════════════════════════════════════
#  排名、buffer 與 tie-break
# ══════════════════════════════════════════════════════════════════
def test_entry_threshold_is_strict_top_decile():
    """n=10 時前 10% 只有 1 檔；用 >= 會誤收 2 檔。"""
    signal = pd.Series({f"{1000 + i}": float(i) for i in range(10)})
    assert select_holdings(signal) == ["1009"]


def test_entry_and_exit_fractions_hold_on_a_larger_universe():
    signal = pd.Series({f"{1000 + i:04d}": float(i) for i in range(100)})
    entrants = select_holdings(signal)
    assert len(entrants) == int(100 * ENTRY_TOP_FRAC)

    # 全部 100 檔都當成既有部位時，只有前 20% 會被保留。
    kept = select_holdings(signal, previous_holdings=list(signal.index))
    assert len(kept) == int(100 * EXIT_BOTTOM_FRAC)


def test_buffer_keeps_holding_between_entry_and_exit_thresholds():
    signal = pd.Series({f"{1000 + i:04d}": float(i) for i in range(100)})
    borderline = "1085"          # 名次 15：不夠格新進場（前 10），但仍在前 20
    assert borderline not in select_holdings(signal)
    assert borderline in select_holdings(signal, previous_holdings=[borderline])


def test_holding_that_falls_out_of_top_20_percent_is_dropped():
    signal = pd.Series({f"{1000 + i:04d}": float(i) for i in range(100)})
    assert "1075" not in select_holdings(signal, previous_holdings=["1075"])


def test_ties_break_by_stock_id_ascending_not_input_order():
    """20 檔中有 3 檔同分並列第一，但前 10% 只有 2 個名額——
    誰入選必須由 stock_id 升冪決定，而不是輸入欄位順序。"""
    values = {f"{2000 + i:04d}": 0.0 for i in range(17)}
    values.update({"9999": 1.0, "1101": 1.0, "2330": 1.0})
    signal = pd.Series(values)

    assert select_holdings(signal) == ["1101", "2330"]
    assert select_holdings(signal.iloc[::-1]) == ["1101", "2330"]
    assert select_holdings(signal.sample(frac=1.0, random_state=7)) == ["1101", "2330"]


def test_selection_rejects_missing_signal_values():
    signal = pd.Series({"1101": 1.0, "1102": np.nan})
    with pytest.raises(ValueError, match="缺值"):
        select_holdings(signal)


def test_selection_rejects_infinite_signal_values():
    signal = pd.Series({"1101": 1.0, "1102": np.inf})
    with pytest.raises(ValueError, match="有限數值"):
        select_holdings(signal)


def test_selection_rejects_negative_max_positions():
    signal = pd.Series({f"{1000 + i}": float(i) for i in range(10)})
    with pytest.raises(ValueError, match="非負整數"):
        select_holdings(signal, max_positions=-1)


def test_no_entrants_when_universe_too_small_for_a_full_slot():
    """floor(4 * 10%) = 0：名額不足時保留現金，不得放寬門檻硬湊一檔。"""
    signal = pd.Series({"1101": 3.0, "1102": 2.0, "1103": 1.0, "1104": 0.0})
    assert select_holdings(signal) == []


def test_max_positions_gives_priority_to_existing_holdings():
    """§7.6 只列舉了「跌出前 20%」這一個排名相關的賣出理由；
    新進場者不得把仍在前 20% 的持股擠掉。"""
    signal = pd.Series({f"{1000 + i:04d}": float(i) for i in range(100)})
    held = "1082"                                   # 百分位 0.83，在前 20% 內
    ordered = select_holdings(signal, previous_holdings=[held], max_positions=3)
    assert ordered[0] == held
    assert len(ordered) == 3


def test_rank_percentile_gives_ties_the_same_value():
    values = pd.Series({"a": 1.0, "b": 1.0, "c": 2.0})
    pct = rank_percentile(values)
    assert pct["a"] == pct["b"] < pct["c"]
