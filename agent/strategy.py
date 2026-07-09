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
import math
import os
import pandas as pd


# ══════════════════════════════════════════════════════════════════
#  可調參數（教授主要動這裡）
# ══════════════════════════════════════════════════════════════════
STRATEGY = {
    # ── 進場：候選池過濾 ──
    # 2026-07-09 選股重構（趨勢版）：RSI 上限放寬到 90（動能股最強時 RSI 常 >75，
    # 舊上限反而在最強勢時把它踢出），且關掉熱門族群硬閘門讓全市場都是候選。
    "min_rsi": 40, "max_rsi": 90,
    "min_close": 10, "min_volume": 500,    # 張
    "hot_sectors_top_n": 5, "sector_min_stocks": 10,
    "use_hot_sector_gate": False,          # False=全市場趨勢/題材選股（趨勢版，回測勝出）；True=舊的族群硬閘門
    "pick_top_n": 5,                        # 每次買進檔數

    # ── 進場：評分權重（2026-07-09 趨勢版；walk-forward 回測全期 +71.8% vs 舊版 +16.8%）──
    # 核心＝趨勢/相對強度，題材（投信連買）為輔；技術面單日事件旗標降為 0（只用於進出場）。
    "w_ma_cross":  0.0,    # 均線黃金交叉（單日事件旗標，選股重構後降為 0——只認「今天剛交叉」會漏掉整年強勢股）
    "w_breakout":  0.0,    # 突破 20 日高（單日事件旗標，同上）
    "w_macd_pos":  0.0,    # MACD 柱 > 0（單日狀態，降為 0）
    "w_inst_buy":  0.0,    # 三大法人「當日」買超（雜訊大，改用下面的投信連買 w_invest_streak）
    "w_foreign_buy": 1.0,  # 外資買超
    "w_rsi_sweet": 0.0,    # RSI 落在 50~65（與追強勢股矛盾，降為 0）
    "w_momentum":  1.5,    # 60日動能相對排名（0~1 百分位）——強者恆強因子
    "w_rev_yoy":   1.0,    # 月營收年增 >0（FinLab 式營收動能，來源 MOPS）
    "w_rev_accel": 0.5,    # 月營收年增 >20%（高成長加碼）
    # 2026-07-09 選股重構新增的可回測因子（趨勢版核心）：
    "w_rs":           3.0,  # 相對強度（20 日報酬全市場百分位）——趨勢核心，抓「強勢股」
    "w_trend_stack":  2.0,  # 多頭排列 MA5>MA20>MA60 持續天數——抓「已確認的趨勢」非今天剛交叉
    "w_invest_streak": 1.5, # 投信連續買超（含量體門檻）——「題材代理」，追大戶的錢

    # ── 市場濾網（regime filter）──
    # 大盤代理（0050 收盤 vs MA60）：空頭時 (a) 不開新倉 (b) 出場加回死亡交叉保護
    # 這是對「多頭年調參、空頭無保護」的補強——參考常見趨勢跟蹤系統的 market filter
    "market_filter":            True,
    "market_filter_stock":      "0050",
    "market_filter_block_entries": False,  # True=空頭完全不開新倉（保守）；False=只加出場保護
    "bear_reenable_death_cross": True,

    # ── 資金/風險管理（參考 freqtrade money management / 1% 風險法則）──
    # 每筆交易最多虧總資金的 risk_per_trade（配合停損距離反推張數）
    "capital":            int(os.getenv("TRADING_CAPITAL", "300000")),  # 可投入總資金（元）
    "risk_per_trade":     0.01,   # 單筆風險上限 = 資金的 1%
    "max_open_positions": 10,     # 同時持倉上限（portfolio 風控守門員）

    # ── 出場規則：基本 ──
    "stop_loss":      0.08,   # 自進場價跌 8% → 停損
    "take_profit":    0.30,   # 固定停利門檻（僅 exit_fixed_take_profit=True 時生效，供消融對照用）
    "exit_fixed_take_profit": False,
    # 2026-07-09 使用者要求關閉：台股常見「噴出→整理→再噴出」（如南亞科/欣興同期
    # +530~640%），固定停利到價就出場會錯過後面的大波段。改用下面的分級移動停利，
    # 讓真正的大行情抱得住，同時獲利越大、保護的獲利也越多（回落容忍度隨峰值獲利放寬）。
    "trail_activate": 0.10,   # 沿用：分級移動停利第一層的啟動門檻（trail_tiers 沒設定時的預設值）
    "trail_stop":     0.08,   # 沿用：第一層的回落容忍度
    "trail_tiers": [
        (0.10, 0.08),   # 峰值獲利 ≥10% → 回落 8% 出場（原本行為，小賺先保護本金）
        (0.40, 0.15),   # 峰值獲利 ≥40% → 回落 15%（波段整理常見拉回，別被洗出場）
        (1.00, 0.20),   # 峰值獲利 ≥100% → 回落 20%
        (2.00, 0.25),   # 峰值獲利 ≥200% → 回落 25%（飆股級別，留更大空間抱住整理）
    ],
    "max_hold_days":  40,     # 持有超過 40 個交易日 → 到期出場

    # ── 出場規則：均線 ──
    # 2026-07 消融測試（scripts/ablation_result.md）：三條均線規則讓平均持有僅 3.7 日、
    # 勝率 32%（MA5 幾乎每次搶先出場，波段被洗掉）。全關後勝率 43.6%、持有 19.8 日、
    # 平均淨報酬 +2.04%→+2.89%。注意：測試區間為多頭年，保護交給停損+移動停利。
    "exit_on_death_cross": False,  # MA5 跌破 MA20（死亡交叉）→ 出場
    "exit_below_ma20":     False,  # 收盤跌破 MA20 → 出場
    "exit_below_ma5":      False,  # 收盤跌破 MA5 → 出場

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
#  市場濾網用：股票分割/合併還原
#  台股單日漲跌幅限制 ±10%，任何單日變動超過此值只可能是分割/減資等公司行動
#  或資料錯誤，不可能是真實交易。market_filter_stock（預設0050）若曾分割，
#  原始收盤價會出現假崩盤，汙染 MA60 導致誤判空頭（見 2025-06-18 0050 一分四實例）。
#  這裡只還原「濾網代理股」自己的序列，不動 daily_prices 原始資料，也不影響
#  一般個股的技術指標（那是更大範圍的資料品質工程，此處不處理）。
# ══════════════════════════════════════════════════════════════════
SPLIT_JUMP_THRESHOLD = 0.20   # 單日變動超過 20%（遠高於漲跌限制）視為分割/資料異常

def split_adjust(closes: pd.Series) -> pd.Series:
    """
    依日期排序的收盤價序列，偵測單日 |漲跌幅| > SPLIT_JUMP_THRESHOLD 的斷點，
    將斷點前的價格整批乘上調整係數，讓序列在分割前後可比（後復權）。
    單一離群的一日錯誤（隔天就跳回）也會被同一機制吸收成一段極短的整段調整，
    不影響鄰近正常區間。
    """
    s = closes.dropna().sort_index()
    if len(s) < 2:
        return closes
    ratio = s / s.shift(1)
    jumps = ratio[(ratio < 1 - SPLIT_JUMP_THRESHOLD) | (ratio > 1 + SPLIT_JUMP_THRESHOLD)]
    if jumps.empty:
        return closes
    adj = pd.Series(1.0, index=s.index)
    for jump_date, r in jumps.items():
        adj.loc[:jump_date] *= r
        adj.loc[jump_date] = 1.0   # 斷點當天本身已是新基準，不重複調整
    return (closes * adj.reindex(closes.index).ffill().bfill()).where(closes.notna())


# ══════════════════════════════════════════════════════════════════
#  趨勢/題材因子（可回測）——回測與即時選股共用同一份計算，確保線上=回測
#  輸入都是「日期 × 股票」的 pivot（index=日期、columns=股票代號）
# ══════════════════════════════════════════════════════════════════
MIN_INVEST_STREAK_LOTS = 100   # 投信連買至少累計 100 張才算數（沿用 smart_money 的量體下限）

def _consecutive_true(df_bool: pd.DataFrame) -> pd.DataFrame:
    """逐欄計算「至當列為止連續 True 的天數」（向量化，供多頭排列/投信連買用）。"""
    csum = df_bool.cumsum()
    reset = csum.where(~df_bool).ffill().fillna(0)
    return (csum - reset).where(df_bool, 0)


def compute_factor_matrices(closes, ma5p, ma20p, ma60p, invest):
    """
    回傳 (rs20, stack_days, inv_streak) 三個「日期 × 股票」矩陣：
      rs20        — 20 日報酬在全市場當日的百分位（0~1，越強越高）
      stack_days  — MA5>MA20>MA60 多頭排列連續成立天數
      inv_streak  — 投信連續淨買超天數（需累計量體 ≥ MIN_INVEST_STREAK_LOTS 才算）
    所有輸入對齊到 closes 的 index/columns。
    """
    idx, cols = closes.index, closes.columns
    ma5p  = ma5p.reindex(index=idx, columns=cols)
    ma20p = ma20p.reindex(index=idx, columns=cols)
    ma60p = ma60p.reindex(index=idx, columns=cols)
    invest = invest.reindex(index=idx, columns=cols)

    ret20 = closes / closes.shift(20) - 1
    rs20 = ret20.rank(axis=1, pct=True)

    stack = ((ma5p > ma20p) & (ma20p > ma60p)).fillna(False)
    stack_days = _consecutive_true(stack)

    is_pos = invest > 0
    streak = _consecutive_true(is_pos.fillna(False))
    pos_only = invest.where(is_pos, 0.0)
    csum = pos_only.cumsum()
    reset_val = csum.where(~is_pos.fillna(False)).ffill().fillna(0.0)
    streak_lots = ((csum - reset_val).where(is_pos, 0.0)) / 1000.0   # 股→張
    inv_streak = streak.where(streak_lots >= MIN_INVEST_STREAK_LOTS, 0)
    return rs20, stack_days, inv_streak


# ══════════════════════════════════════════════════════════════════
#  進場評分
# ══════════════════════════════════════════════════════════════════
def score_candidates(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.Series:
    """
    輸入：含 signal_ma_cross, signal_breakout, macd_hist, inst_net,
          foreign_net, rsi14 欄位的 DataFrame（趨勢/題材因子 rs20/stack_days/
          invest_streak 為選配，有欄位才計分）。
    輸出：每列的分數 Series。

    2026-07-09 選股重構：新增「趨勢」（相對強度 rs20、多頭排列持續 stack_days）
    與「題材代理」（投信連買 invest_streak）三個可回測因子，用來取代原本主導
    選股、卻只認「今天剛發生」的單日技術面 event flags（w_ma_cross/w_breakout）。
    所有新因子與降權都由 cfg 權重控制，STRATEGY 預設維持舊值以免影響尚未升級的
    即時路徑；實驗用的 cfg_trend/cfg_flow 才把新因子開起來、把 event flags 降為 0。
    """
    s = pd.Series(0.0, index=df.index)
    # ── 技術面單日事件旗標（選股重構後在新 cfg 中降權為 0，只保留給進出場/舊設定）──
    s += df["signal_ma_cross"].clip(0, 1).astype(float) * cfg.get("w_ma_cross", 0)
    s += df["signal_breakout"].clip(0, 1).astype(float) * cfg.get("w_breakout", 0)
    s += (df["macd_hist"] > 0).astype(float) * cfg.get("w_macd_pos", 0)
    s += (df["inst_net"] > 0).astype(float) * cfg.get("w_inst_buy", 0)
    s += (df["foreign_net"] > 0).astype(float) * cfg.get("w_foreign_buy", 0)
    s += ((df["rsi14"] >= 50) & (df["rsi14"] <= 65)).astype(float) * cfg.get("w_rsi_sweet", 0)

    # ── 趨勢：相對強度（20 日報酬全市場百分位，0~1，已預算好直接用）──
    if "rs20" in df.columns and cfg.get("w_rs", 0) > 0:
        rs = pd.to_numeric(df["rs20"], errors="coerce")
        s += rs.fillna(0.0) * cfg["w_rs"]
    # ── 趨勢：多頭排列持續天數（MA5>MA20>MA60 連續幾天，20 日封頂做 0~1 飽和）──
    if "stack_days" in df.columns and cfg.get("w_trend_stack", 0) > 0:
        sd = pd.to_numeric(df["stack_days"], errors="coerce").fillna(0)
        s += (sd.clip(0, 20) / 20.0) * cfg["w_trend_stack"]
    # ── 題材代理：投信連買（連續買超天數，已含量體門檻，5 日封頂做 0~1 飽和）──
    if "invest_streak" in df.columns and cfg.get("w_invest_streak", 0) > 0:
        st = pd.to_numeric(df["invest_streak"], errors="coerce").fillna(0)
        s += (st.clip(0, 5) / 5.0) * cfg["w_invest_streak"]

    # ── 60 日動能：候選池內相對排名（0~1），有欄位才計（回測與正式選股都會提供）──
    if "mom60" in df.columns and cfg.get("w_momentum", 0) > 0:
        mom = pd.to_numeric(df["mom60"], errors="coerce")
        if mom.notna().sum() >= 2:
            s += mom.rank(pct=True).fillna(0.5) * cfg["w_momentum"]
    # ── 月營收年增（缺資料 = 0 分，不懲罰）──
    if "rev_yoy" in df.columns:
        yoy = pd.to_numeric(df["rev_yoy"], errors="coerce")
        s += (yoy > 0).fillna(False).astype(float) * cfg.get("w_rev_yoy", 0)
        s += (yoy > 20).fillna(False).astype(float) * cfg.get("w_rev_accel", 0)
    return s


# ══════════════════════════════════════════════════════════════════
#  資金管理：建議張數
# ══════════════════════════════════════════════════════════════════
def suggest_shares(price: float, cfg: dict = STRATEGY) -> int:
    """
    1% 風險法則（股為單位，支援零股）：
      單筆最大虧損 = capital × risk_per_trade；停損打到每股虧 price × stop_loss
      → 建議股數 = 風險額度 ÷ 每股風險。
    另設集中度天花板：單一部位市值 ≤ 資金 ÷ pick_top_n。
    """
    if not price or price <= 0:
        return 0
    risk_budget     = cfg["capital"] * cfg["risk_per_trade"]
    shares_by_risk  = risk_budget / (price * cfg["stop_loss"])
    cap_value       = cfg["capital"] / max(cfg.get("pick_top_n", 5), 1)
    shares_by_cap   = cap_value / price
    return max(math.floor(min(shares_by_risk, shares_by_cap)), 0)


def format_size(shares: int) -> str:
    """股數 → 人話：1000 股以上顯示張（+零股），不足顯示零股。"""
    if shares <= 0:
        return "資金不足（跳過或縮小停損）"
    lots, odd = divmod(shares, 1000)
    if lots and odd:
        return f"{lots} 張 + {odd} 股"
    if lots:
        return f"{lots} 張"
    return f"{odd} 股（零股）"


# ══════════════════════════════════════════════════════════════════
#  出場輔助函式
# ══════════════════════════════════════════════════════════════════
def active_trail_giveback(peak_gain: float, cfg: dict = STRATEGY) -> float | None:
    """
    依「峰值獲利」查目前生效的移動停利回落容忍度（分級：獲利越大，容忍拉回越寬）。
    peak_gain 未達最低一層門檻時回傳 None（移動停利尚未啟動）。
    """
    tiers = cfg.get("trail_tiers") or [(cfg.get("trail_activate", 0.10), cfg.get("trail_stop", 0.08))]
    active = None
    for threshold, giveback in sorted(tiers, key=lambda t: t[0]):
        if peak_gain >= threshold:
            active = giveback
    return active


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

    # ── 2. 固定停利（預設關閉，見 STRATEGY["exit_fixed_take_profit"] 說明）────
    if cfg.get("exit_fixed_take_profit") and gain >= cfg["take_profit"]:
        return True, f"停利(+{cfg['take_profit']*100:.0f}%)"

    # ── 3. 分級移動停利：峰值獲利越大，容忍的拉回越寬，才抱得住噴出後整理的大波段
    peak_gain = (peak_price / entry_price - 1) if entry_price else 0.0
    giveback = active_trail_giveback(peak_gain, cfg)
    if giveback is not None and peak_price and close <= peak_price * (1 - giveback):
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

    # ── 12. 持有到期（max_hold_days 設 0 或 None → 關閉，讓趨勢股靠移動停利自然出場）──
    mhd = cfg.get("max_hold_days")
    if mhd and holding_days >= mhd:
        return True, f"持有到期({mhd}日)"

    return False, None
