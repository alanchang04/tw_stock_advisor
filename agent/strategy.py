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

    # ── 出場規則 ──
    "stop_loss":      0.08,   # 自進場價跌 8% → 停損
    "take_profit":    0.25,   # 獲利達 25% → 停利
    "trail_activate": 0.10,   # 漲超過 10% 後啟動移動停利
    "trail_stop":     0.08,   # 從最高點回落 8% → 出場
    "exit_on_death_cross": True,   # MA5 跌破 MA20 → 出場
    "exit_below_ma20":     True,   # 收盤跌破 MA20 → 出場
    "max_hold_days":  40,     # 持有超過 40 個交易日 → 到期出場
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
) -> tuple[bool, str | None]:
    """
    對一個「持有中」的部位，根據當日資料判斷是否該出場。
    回傳 (是否出場, 原因字串)。多條件同時成立時，以「先保護本金」的順序回報。
    """
    if entry_price and close <= entry_price * (1 - cfg["stop_loss"]):
        return True, f"停損(-{cfg['stop_loss']*100:.0f}%)"

    gain = (close / entry_price - 1) if entry_price else 0.0
    if gain >= cfg["take_profit"]:
        return True, f"停利(+{cfg['take_profit']*100:.0f}%)"

    # 移動停利：漲幅曾超過啟動門檻後，從最高點回落超過 trail_stop
    peak_gain = (peak_price / entry_price - 1) if entry_price else 0.0
    if peak_gain >= cfg["trail_activate"] and peak_price and \
            close <= peak_price * (1 - cfg["trail_stop"]):
        return True, "移動停利(回落)"

    if cfg["exit_on_death_cross"] and ma5 is not None and ma20 is not None and ma5 < ma20:
        return True, "均線死亡交叉"

    if cfg["exit_below_ma20"] and ma20 is not None and close < ma20:
        return True, "跌破月線(MA20)"

    if holding_days >= cfg["max_hold_days"]:
        return True, f"持有到期({cfg['max_hold_days']}日)"

    return False, None
