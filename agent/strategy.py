"""
agent/strategy.py

策略中樞 —— 把「進場選股」與「出場規則」的所有可調參數與邏輯集中在這裡。
教授／你要調整買賣邏輯，原則上只改這個檔案，其他模組(選股、回測、部位追蹤)都會跟著變，
不必動到資料流程。

兩個可替換的核心：
  - score_candidates(df): 進場評分（分數越高越值得買）
  - decide_exit(...): 出場判斷（回傳 (是否出場, 原因)）
"""
from __future__ import annotations
import pandas as pd


# ══════════════════════════════════════════════════════════════════
#  可調參數（教授主要動這裡）
# ══════════════════════════════════════════════════════════════════
STRATEGY = {
    # ── 進場：候選池過濾 ──
    "min_rsi": 40, "max_rsi": 75,
    "min_close": 10, "min_volume": 500,    # 張
    "hot_sectors_top_n": 5, "sector_min_stocks": 10,
    "pick_top_n": 5,                        # 每次買進檔數

    # ── 進場：評分權重 ──
    "w_ma_cross":  2.0,    # 均線黃金交叉
    "w_breakout":  2.0,    # 突破 20 日高
    "w_macd_pos":  1.0,    # MACD 柱 > 0
    "w_inst_buy":  1.5,    # 三大法人買超
    "w_foreign_buy": 1.0,  # 外資買超
    "w_rsi_sweet": 1.0,    # RSI 落在 50~65

    # ── 出場規則：基本 ──
    "stop_loss":      0.08,   # 自進場價跌 8% → 停損
    "take_profit":    0.25,   # 獲利達 25% → 停利
    "trail_activate": 0.10,   # 漲超過 10% 後啟動移動停利
    "trail_stop":     0.08,   # 從最高點回落 8% → 出場
    "max_hold_days":  40,     # 持有超過 40 個交易日 → 到期出場

    # ── 出場規則：均線 ──
    "exit_on_death_cross": True,   # MA5 跌破 MA20（死亡交叉）→ 出場
    "exit_below_ma20":     True,   # 收盤跌破 MA20 → 出場
    "exit_below_ma5":      True,   # 收盤跌破 MA5 → 出場

    # ── 出場規則：KD 高檔死叉 + MACD 同步轉負 ──
    "exit_kd_macd":   True,   # 啟用此規則
    "kd_overbought":  80,     # K 值需在此閾值以上才算「高檔」

    # ── 出場規則：跌破前波低點 ──
    "exit_swing_low":      True,
    "swing_low_window":    5,    # 左右各幾根確認樞紐
    "swing_low_lookback":  30,   # 往回看幾根 K 棒找樞紐

    # ── 出場規則：跌破前 N 根 K 棒實體棒底部 ──
    "exit_body_break":     True,
    "body_break_candles":  3,    # 參考最近幾根

    # ── 出場規則：跌破最近大 K 棒底部 ──
    "exit_large_candle":      True,
    "large_candle_pct":       0.03,   # 實體漲跌幅 ≥ 3% 才算大 K 棒
    "large_candle_lookback":  20,     # 往回看幾根

    # ── 出場規則：長上引線爆量 ──
    "exit_upper_wick":    True,
    "upper_wick_ratio":   0.6,    # 上引線 ≥ 振幅 60%
    "high_volume_ratio":  2.5,    # 成交量 ≥ 均量 2.5 倍
    "volume_avg_days":    20,     # 均量計算天數（由 portfolio 傳入）
}


# ══════════════════════════════════════════════════════════════════
#  進場評分
# ══════════════════════════════════════════════════════════════════
def score_candidates(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.Series:
    """
    輸入：含 signal_ma_cross, signal_breakout, macd_hist, inst_net,
          foreign_net, rsi14 欄位的 DataFrame
    輸出：每列的分數 Series
    """
    s = pd.Series(0.0, index=df.index)
    s += df["signal_ma_cross"].clip(0, 1).astype(float) * cfg["w_ma_cross"]
    s += df["signal_breakout"].clip(0, 1).astype(float) * cfg["w_breakout"]
    s += (df["macd_hist"] > 0).astype(float) * cfg["w_macd_pos"]
    s += (df["inst_net"] > 0).astype(float) * cfg["w_inst_buy"]
    s += (df["foreign_net"] > 0).astype(float) * cfg["w_foreign_buy"]
    s += ((df["rsi14"] >= 50) & (df["rsi14"] <= 65)).astype(float) * cfg["w_rsi_sweet"]
    return s


# ══════════════════════════════════════════════════════════════════
#  出場輔助函式
# ══════════════════════════════════════════════════════════════════
def _find_swing_low(history: list[dict], window: int, lookback: int) -> float | None:
    """
    在 history（oldest→newest）中往回找最近一個「前波低點」樞紐。
    樞紐定義：low[i] < 左右各 window 根的所有 low。
    只看完整（左右都有足夠 bar）的樞紐，跳過最後 window 根（右邊未確認）。
    """
    # 取最近 lookback 根（不含當根，因為 history 最後一筆是今天）
    candidates = history[-(lookback + 1):-1]  # 最多 lookback 根
    if len(candidates) < 2 * window + 1:
        return None

    best = None
    for i in range(window, len(candidates) - window):
        low_i = candidates[i]["low"]
        left_ok  = all(low_i <= candidates[i - j]["low"] for j in range(1, window + 1))
        right_ok = all(low_i <= candidates[i + j]["low"] for j in range(1, window + 1))
        if left_ok and right_ok:
            # 取最近的樞紐（index 越大越近）
            if best is None or i > best[0]:
                best = (i, low_i)

    return best[1] if best else None


# ══════════════════════════════════════════════════════════════════
#  出場判斷
# ══════════════════════════════════════════════════════════════════
def decide_exit(
    entry_price: float,
    peak_price: float,
    close: float,
    ma5: float | None,
    ma20: float | None,
    holding_days: int,
    cfg: dict = STRATEGY,
    extra: dict | None = None,
    # extra 預期欄位:
    #   k, d, k_prev, d_prev         — KD 本日 / 前日
    #   macd_hist, macd_hist_prev    — MACD 柱 本日 / 前日
    #   open, high, low, volume      — 今日 OHLCV
    #   avg_volume                   — 近 volume_avg_days 日均量
    history: list | None = None,
    # history: list of dict (oldest first), 每筆含
    #   trade_date, open, high, low, close, volume
) -> tuple[bool, str | None]:
    """
    對一個「持有中」的部位，根據當日資料判斷是否該出場。
    回傳 (是否出場, 原因字串)。多條件同時成立時，以「先保護本金」的順序回報。

    extra / history 為 None 時（如舊版 backtest 呼叫）→ 只跑基本規則，不報錯。
    """
    # ── 1. 停損 ────────────────────────────────────────────────
    if entry_price and close <= entry_price * (1 - cfg["stop_loss"]):
        return True, f"停損(-{cfg['stop_loss']*100:.0f}%)"

    gain = (close / entry_price - 1) if entry_price else 0.0

    # ── 2. 固定停利 ────────────────────────────────────────────
    if gain >= cfg["take_profit"]:
        return True, f"停利(+{cfg['take_profit']*100:.0f}%)"

    # ── 3. 移動停利 ────────────────────────────────────────────
    peak_gain = (peak_price / entry_price - 1) if entry_price else 0.0
    if peak_gain >= cfg["trail_activate"] and peak_price and \
            close <= peak_price * (1 - cfg["trail_stop"]):
        return True, "移動停利(回落)"

    # ── 4. KD 高檔死叉 + MACD 同步轉負 ───────────────────────
    if cfg.get("exit_kd_macd") and extra:
        k      = extra.get("k")
        d      = extra.get("d")
        k_prev = extra.get("k_prev")
        d_prev = extra.get("d_prev")
        mh     = extra.get("macd_hist")
        if (k is not None and d is not None and
                k_prev is not None and d_prev is not None and mh is not None):
            overbought = k >= cfg["kd_overbought"] or k_prev >= cfg["kd_overbought"]
            death_cross = k < d and k_prev >= d_prev   # 本根死叉
            macd_neg = mh <= 0
            if overbought and death_cross and macd_neg:
                return True, f"KD高檔死叉+MACD轉負(K={k:.1f})"

    # ── 5. 均線死亡交叉 ────────────────────────────────────────
    if cfg.get("exit_on_death_cross") and ma5 is not None and ma20 is not None and ma5 < ma20:
        return True, "均線死亡交叉"

    # ── 6. 跌破 MA20 ───────────────────────────────────────────
    if cfg.get("exit_below_ma20") and ma20 is not None and close < ma20:
        return True, "跌破月線(MA20)"

    # ── 7. 跌破 MA5 ────────────────────────────────────────────
    if cfg.get("exit_below_ma5") and ma5 is not None and close < ma5:
        return True, "跌破週線(MA5)"

    # ── 8. 跌破前波低點 ────────────────────────────────────────
    if cfg.get("exit_swing_low") and history:
        swing_low = _find_swing_low(
            history,
            window=cfg.get("swing_low_window", 5),
            lookback=cfg.get("swing_low_lookback", 30),
        )
        if swing_low is not None and close < swing_low:
            return True, f"跌破前波低點({swing_low:.2f})"

    # ── 9. 跌破前 N 根實體棒底部 ──────────────────────────────
    if cfg.get("exit_body_break") and history:
        n = cfg.get("body_break_candles", 3)
        recent = history[-(n + 1):-1]   # 最近 n 根（不含今天）
        if len(recent) == n:
            body_bottoms = [min(r["open"], r["close"]) for r in recent]
            ref = min(body_bottoms)
            if close < ref:
                return True, f"跌破近{n}根實體底({ref:.2f})"

    # ── 10. 跌破最近大 K 棒底部 ───────────────────────────────
    if cfg.get("exit_large_candle") and history:
        lb = cfg.get("large_candle_lookback", 20)
        pct = cfg.get("large_candle_pct", 0.03)
        candidates = history[-(lb + 1):-1]
        big_candle_bottom = None
        for r in reversed(candidates):   # 最近的先找
            if r["open"] and abs(r["close"] - r["open"]) / r["open"] >= pct:
                big_candle_bottom = min(r["open"], r["close"])
                break
        if big_candle_bottom is not None and close < big_candle_bottom:
            return True, f"跌破大K棒底({big_candle_bottom:.2f})"

    # ── 11. 長上引線爆量 ───────────────────────────────────────
    if cfg.get("exit_upper_wick") and extra:
        o   = extra.get("open")
        h   = extra.get("high")
        l   = extra.get("low")
        vol = extra.get("volume")
        avg = extra.get("avg_volume")
        if all(v is not None for v in [o, h, l, vol, avg]) and avg > 0:
            body_top    = max(o, close)
            upper_wick  = h - body_top
            candle_range = h - l
            wick_ratio   = cfg.get("upper_wick_ratio", 0.6)
            vol_ratio    = cfg.get("high_volume_ratio", 2.5)
            if candle_range > 0 and upper_wick >= candle_range * wick_ratio \
                    and vol >= avg * vol_ratio:
                return True, f"長上引線爆量(上影{upper_wick/candle_range*100:.0f}%,量{vol/avg:.1f}x)"

    # ── 12. 持有到期 ───────────────────────────────────────────
    if holding_days >= cfg["max_hold_days"]:
        return True, f"持有到期({cfg['max_hold_days']}日)"

    return False, None
