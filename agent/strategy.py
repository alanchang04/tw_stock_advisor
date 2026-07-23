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
    # 2026-07-15 中型流動性股轉向（SPEC_STRATEGY_MIDCAP）：拉高門檻排除阿呆股/薄籌碼股，
    # 用「成交金額」而非市值（無市值資料）當抗操控核心——低流動性才是主力好拉抬的根源。
    "min_rsi": 45, "max_rsi": 88,
    "min_close": 15, "min_volume": 500,    # 張
    "min_turnover_avg5": 200_000_000,  # 近5日均成交金額下限（元）＝抗操控核心門檻，可調鬆緊
    "turnover_avg_days": 5,
    "min_turnover_percentile": 0.75,   # 2026-07-19：改用「當日全市場成交金額前25%」取代
                                        # 固定金額門檻（10年回測A/B驗證：跟固定門檻表現幾乎
                                        # 一樣，但不隨市場規模膨脹而鬆動，是更穩健的預設）。
                                        # None=改回沿用上面的絕對金額門檻，見apply_liquidity_gate註解
    "hot_sectors_top_n": 5, "sector_min_stocks": 10,
    "use_hot_sector_gate": False,          # False=全市場趨勢/題材選股（趨勢版，回測勝出）；True=舊的族群硬閘門
    "pick_top_n": 5,                        # 每次買進檔數

    # ── 進場：評分權重（2026-07-19 P1因子研究重配權；SPEC_QUANT_UPGRADE.md P1→P3：
    #   10年IC研究實測，horizon=10~20日（貼近現行~15.5日平均持有期）：rs20/mom60在
    #   這個時間窗統計顯著「負」相關（rs20 IC -0.16~-0.19，t值-8~-10），現行系統選出的
    #   正是短期會反向的股票，直接歸零；rev_yoy全時間窗最強且唯一四窗都顯著正向
    #   （ICIR 0.34~0.60），大幅升權；投信新進場/連買時間尺度吻合、穩定正向，升權/維持；
    #   外資買超20日後訊號迅速衰減（60日ICIR僅0.02），略降權。10年A/B回測驗證：
    #   Sharpe 0.63→0.96、回撤-32.8%→-21.4%、Calmar 0.29→0.56，三項同向大幅改善
    #   （0050同期Sharpe1.08/回撤-34%，差距從原本0.68縮小到0.12，回撤甚至優於0050）──
    "w_ma_cross":  0.0,    # 均線黃金交叉（單日事件旗標，選股重構後降為 0——只認「今天剛交叉」會漏掉整年強勢股）
    "w_breakout":  0.0,    # 突破 20 日高（單日事件旗標，同上）
    "w_macd_pos":  0.0,    # MACD 柱 > 0（單日狀態，降為 0）
    "w_inst_buy":  0.0,    # 三大法人「當日」買超（雜訊大，改用下面的投信連買 w_invest_streak）
    "w_foreign_buy": 1.0,  # 外資買超（P1：20日後訊號衰減快，從1.5降權）
    "w_rsi_sweet": 0.0,    # RSI 落在 50~65（與追強勢股矛盾，降為 0）
    "w_momentum":  0.0,    # 60日動能相對排名（P1：10~20日IC顯著負相關，歸零）
    "w_rev_yoy":   3.0,    # 月營收年增 >0（P1：全時間窗最強、唯一四窗都顯著正向，從2.0升權）
    "w_rev_accel": 1.0,    # 月營收年增 >20%（高成長加碼，升權）
    # 相對強度/多頭排列/投信連買（可回測核心因子）：
    "w_rs":           0.0,  # 相對強度（20日報酬全市場百分位）（P1：10~20日IC顯著負相關，歸零）
    "w_trend_stack":  0.8,  # 多頭排列 MA5>MA20>MA60 持續天數（P1：要60日以上才顯著，現行持有期
                             # 跑不完，從1.5降權——2026-07-20修正：這裡先前commit漏改，10年A/B
                             # 驗證的C組是0.8，之前誤留1.5，數字對不上見exit_rule_ablation重測）
    "w_invest_streak": 2.5, # 投信連續買超（含量體門檻）——「買氣」升權，追大戶的錢

    # ── 評分模式（SPEC_QUANT_UPGRADE.md §5 決策1，2026-07-23）──
    # "manual"＝上面那組手調 w_xxx（現行預設）
    # "icir"  ＝ICIR加權 × 橫斷面排序標準化（見 score_candidates_icir）
    # ⚠️ 改預設前必須先過 10 年 A/B（scripts/backtest_ab_icir.py），照 P0~P3 的紀律。
    "score_mode": "manual",
    "icir_horizon": 20,          # 用哪個持有天數的 ICIR 當權重（現行平均持有約15.5天）
    # 下面的數字直接來自 P1 的 10 年因子研究（data/research/factor_report.json），
    # 不是手填的。要更新就重跑 scripts/run_factor_report.py 再抄過來。
    # 註：rs20/mom60 在 h10~h20 的 ICIR 是「負的」（追高反而扣分），故權重為 0。
    "icir_weights": {
        "5":  {"rs20": -0.1851, "stack_days": 0.0702, "mom60": -0.1143,
               "invest_streak": 0.3445, "invest_new_entry": 0.4286,
               "foreign_net": 0.4169, "rev_yoy": 0.3375},
        "10": {"rs20": -0.1599, "stack_days": 0.1109, "mom60": -0.0805,
               "invest_streak": 0.3617, "invest_new_entry": 0.4225,
               "foreign_net": 0.3241, "rev_yoy": 0.4069},
        "20": {"rs20": -0.1608, "stack_days": 0.1476, "mom60": -0.0406,
               "invest_streak": 0.3143, "invest_new_entry": 0.3697,
               "foreign_net": 0.1675, "rev_yoy": 0.4231},
        "60": {"rs20": 0.0961, "stack_days": 0.4693, "mom60": 0.2190,
               "invest_streak": 0.3166, "invest_new_entry": 0.3398,
               "foreign_net": 0.0167, "rev_yoy": 0.6033},
    },

    # ── 2026-07-15：中小型股×投信剛開始買（使用者要求）──
    "w_invest_new_entry": 2.5,   # 投信新進場（連買1~2日+當日量體達標，P1：短中期最強因子之一，從2.0升權）——跟上面的
                                  # w_invest_streak方向相反，這裡抓「剛開始買」不是「已經買很多天」
    "new_entry_min_lots": 50,    # 新進場門檻：當日投信買超 ≥ 50 張才算數（避免雜訊）
    "w_etf_accum": 1.0,          # 近期有幾檔主動式ETF加碼/新增（輔助佐證，非硬性門檻）
    "etf_accum_lookback_days": 10,

    # ── 流動性 OR 閘門（2026-07-15）：成交金額達標，或投信新進場+較低流動性下限也放行 ──
    "allow_new_entry_alt_gate": True,   # AI 軌開啟；PRACTICE_CFG 會覆寫成 False
    "alt_min_turnover_avg5": 30_000_000,  # 替代路徑的流動性下限（3千萬/日，比主門檻低但非0）

    # ── 市場濾網（regime filter）──
    # 大盤代理（0050 收盤 vs MA60）：空頭時 (a) 不開新倉 (b) 出場加回死亡交叉保護
    # 這是對「多頭年調參、空頭無保護」的補強——參考常見趨勢跟蹤系統的 market filter
    "market_filter":            True,
    "market_filter_stock":      "0050",
    "market_filter_block_entries": True,   # 2026-07-19：10年回測A/B驗證，開啟後最大回撤從
                                            # -54.7%收斂到-35.5%（大幅改善），總報酬雖降但
                                            # Sharpe/Calmar同步改善，是四組對照中最佳配置。
                                            # False=只加出場保護，不擋新倉（舊預設）
    "bear_reenable_death_cross": True,

    # ── 資金/風險管理（參考 freqtrade money management / 1% 風險法則）──
    # 每筆交易最多虧總資金的 risk_per_trade（配合停損距離反推張數）
    "capital":            int(os.getenv("TRADING_CAPITAL", "300000")),  # 可投入總資金（元）
    "risk_per_trade":     0.01,   # 單筆風險上限 = 資金的 1%
    "max_open_positions": 10,     # 同時持倉上限（portfolio 風控守門員）
    # 族群曝險上限（2026-07-20，SPEC_QUANT_UPGRADE.md P3決策3）：量化確認過問題
    # 規模——10年回測裡最大單一族群佔當次候選比例中位數60%、90分位80%，91.9%的
    # 再平衡日≥40%單一族群，組合波動長期被單一賭注支配。10年A/B三組(None/0.4/0.6)：
    # 0.4太嚴會砍掉真正在噴的族群(Sharpe0.97→0.92，回撤反而更差)；0.6是全指標同時
    # 勝出的柏拉圖改善(總報酬+320.7%→+344.3%、Sharpe0.97→1.00、回撤-26.1%→-24.8%、
    # Calmar0.53→0.58)，已設為新預設，跟0050(Sharpe1.08)只差0.08。
    # None=不設上限；設數值＝任一族群持倉數不得超過 max_open_positions×此比例。
    "sector_exposure_cap": 0.6,

    # 訊號品質偵測+動態縮手（2026-07-20，診斷2021/2024兩個「指數被權值股拉漲、
    # 個股池卻無趨勢」的異常虧損年份後新增）：待10年A/B驗證後決定是否設為預設，
    # 見 scripts/adaptive_throttle_ab.py。False=關閉，維持原行為。
    "adaptive_throttle_enabled": False,
    "adaptive_throttle_lookback": 10,      # 看最近幾筆平倉的勝率
    "adaptive_throttle_min_trades": 10,    # 累積到這麼多筆平倉紀錄才開始判斷（避免暖身期誤觸發）
    "adaptive_throttle_win_rate": 0.20,    # 最近勝率低於這個門檻 → 暫停開新倉（已有部位不受影響）

    # ── 出場規則：基本 ──
    # 2026-07-09 出場改為「趨勢騎乘」（E4，walk-forward 勝出）：核心出場是下面的
    # 死亡交叉（MA5 跌破 MA20＝短期趨勢轉弱才走），只要 5/20 趨勢還在就抱著，
    # 讓飛天股整段大波段吃下來（實測 3491+103%/6274+97%/3026+120% 都是單次抱到）。
    # 固定停利、40 日時間停損都關閉；移動停利只留一層「寬 backstop」防拋物線崩塌。
    "stop_loss":      0.08,   # 自進場價跌 8% → 停損（趨勢還沒確立前的災難保護）
    "take_profit":    0.30,   # 固定停利門檻（僅 exit_fixed_take_profit=True 時生效，供消融對照用）
    "exit_fixed_take_profit": False,
    "trail_activate": 0.10,   # 沿用：trail_tiers 沒設定時的預設啟動門檻
    "trail_stop":     0.08,   # 沿用：trail_tiers 沒設定時的預設回落容忍度
    "trail_tiers": [
        (0.50, 0.25),   # 只有峰值獲利 ≥50% 後才啟動、回落 25% 才出場——寬 backstop，
                        # 平時不介入（交給死亡交叉判趨勢），只防「垂直噴出後急崩」把大獲利全吐回。
    ],
    "max_hold_days":  0,      # 0=關閉時間停損：趨勢還在就一直抱，不因持有天數強制平倉

    # ── 出場規則：均線（趨勢騎乘核心）──
    # 2026-07-20 P3出場規則消融（10年資料，scripts/exit_rule_ablation.py）推翻了
    # 2026-07-09的舊結論：死亡交叉「全時期開啟」實測反而扣分（Sharpe 0.73 vs 拔掉後
    # 0.71~0.95，回撤幾乎不變，證明它砍的是本來會續漲的波段，不是真的在防下檔——
    # 下檔已經由停損+移動停利守住）。三組乾淨對照：全開0.73／全關0.71／
    # 只在熊市重開(下面這個設定)0.95——熊市重開最佳，牛市讓飛天股用移動停利抱到底，
    # 熊市才用死叉當趨勢轉弱的提早示警。改成 False，靠 bear_reenable_death_cross
    # 在熊市日用 bear_cfg 動態重新打開（見 agent/backtest.py run_backtest）。
    "exit_on_death_cross": False,  # 牛市關閉（消融驗證扣分）；熊市由 bear_reenable_death_cross 重開
    "exit_below_ma20":     False,  # 收盤跌破 MA20 → 出場（關閉，交給死亡交叉）
    "exit_below_ma5":      False,  # 收盤跌破 MA5 → 出場（關閉，太敏感會剁碎飛天股）

    # ── 出場規則：KD 高檔死叉 + MACD 同步轉負 ──
    # 2026-07-20 P3消融：10年回測裡拔掉這條，結果與baseline逐位元組相同（連交易筆數
    # 都一樣）——代表這條規則的觸發條件在10年真實資料裡從未先於其他規則觸發過，
    # 是死代碼，關閉（見 exit_rule_ablation.py 結果）。
    "exit_kd_macd":   False,
    "kd_overbought":  80,     # K 值需在此閾值以上才算「高檔」

    # ── 出場規則：跌破前波低點 ──
    # 2026-07-20 P3消融：同上，10年回測裡從未觸發過，關閉。
    "exit_swing_low":      False,
    "swing_low_window":    5,    # 左右各幾根確認樞紐
    "swing_low_lookback":  30,   # 往回看幾根 K 棒找樞紐

    # ── 出場規則：跌破前 N 根 K 棒實體棒底部 ──
    # 2026-07-20 P3消融：同上，10年回測裡從未觸發過，關閉。
    "exit_body_break":     False,
    "body_break_candles":  3,    # 參考最近幾根

    # ── 出場規則：跌破最近大 K 棒底部 ──
    # 2026-07-20 P3消融：同上，10年回測裡從未觸發過，關閉。
    "exit_large_candle":      False,
    "large_candle_pct":       0.03,   # 實體漲跌幅 ≥ 3% 才算大 K 棒
    "large_candle_lookback":  20,     # 往回看幾根

    # ── 出場規則：長上引線爆量 ──
    # 2026-07-20 P3消融：10年回測裡從未觸發過，關閉（沿用同一組門檻常數給
    # hard_veto_upper_wick——那是進場前的否決規則，跟這條出場規則彼此獨立）。
    "exit_upper_wick":    False,
    "upper_wick_ratio":   0.6,    # 上引線 ≥ 振幅 60%
    "high_volume_ratio":  2.5,    # 成交量 ≥ 均量 2.5 倍
    "volume_avg_days":    20,     # 均量計算天數（由 portfolio 傳入）

    # ── 成交量天花板（2026-07-15 人類交易員練習軌規格）──
    # 單筆成交張數不得超過近5日均量的一定比例，避免回測/實盤買到「市場承載不了」的量，
    # 30萬小資金規模下通常不會真的卡到，但這是資金規模放大後的護欄，也讓回測誠實。
    "max_pct_of_avg_volume": 0.01,   # 單筆 ≤ 近5日均量 1%
    "liquidity_avg_days":    5,

    # ── 跌停鎖死模擬（回測用；即時 PaperBroker 靠開盤價存在與否已隱含近似判斷）──
    "limit_down_pct":       -0.095,  # 開盤跌幅 ≤ -9.5% 視為疑似跌停
    "limit_lock_vol_ratio":  0.10,   # 且當日量 < 近5日均量的10% → 判定鎖死無法成交

    # ── 空方硬否決規則（2026-07-15，程式層級強制，不經 LLM 裁量）──
    # 「無條件」的意思是：候選股一旦觸發，直接在進辯論前就被排除，LLM 連看都看不到，
    # 不像一般 VETO 還能被裁決引用數據駁回——避免重演空方JSON截斷讓guardrail失效的教訓
    # （不能只信任 LLM 自己遵守 System Prompt 裡的規則）。
    "hard_veto_deviation_pct": 15.0,  # 乖離 MA20 超過 ±15%
    "hard_veto_upper_wick":    True,  # 帶量長上引線（沿用 exit_upper_wick 同組門檻）

    # ── 個股除權息還原（2026-07-17，SPEC_QUANT_UPGRADE.md P0-2）──
    "total_return_adjust": True,   # 用 dividend_events 官方事件還原個股報酬；
                                    # 消融/對照用途可設 False 關閉，回到未還原行為
}


# ══════════════════════════════════════════════════════════════════
#  人類交易員練習軌（2026-07-15）：純量化、不進 LLM，每日輸出前 20 檔給使用者
#  自己用純線圖（20MA+成交量，不看新聞籌碼）練手動判斷進出場。
#  跟 AI 軌（STRATEGY）完全獨立、互不影響——只是共用同一套候選篩選/評分機制，
#  換一份權重與門檻。目的：讓使用者對「波段操作40%勝率、小賠大賺」的量化心態
#  建立信心，而不是用 AI 軌的即時損益去驗證這個理論（AI 軌樣本太少、雜訊太多）。
# ══════════════════════════════════════════════════════════════════
PRACTICE_CFG = {
    **STRATEGY,
    # 評分只看三個純量化因子（籌碼優勢、趨勢優勢、基本面優勢），技術面/動能/題材全關閉
    "w_ma_cross": 0.0, "w_breakout": 0.0, "w_macd_pos": 0.0, "w_inst_buy": 0.0,
    "w_foreign_buy": 0.0, "w_rsi_sweet": 0.0, "w_momentum": 0.0, "w_rev_yoy": 0.0,
    "w_rs": 0.0,
    "w_trend_stack":   1.5,   # 多頭排列天數（趨勢優勢）
    "w_invest_streak": 2.5,   # 投信連續買超（籌碼優勢）
    "w_rev_accel":     2.0,   # 月營收年增 >20%（基本面優勢）
    "w_invest_new_entry": 0.0,  # 練習軌不用「新進場」因子，維持單純機械（2026-07-15決策）
    "w_etf_accum": 0.0,
    "above_ma20_only": True,  # 硬門檻：股價必須站上月線
    "allow_new_entry_alt_gate": False,  # 練習軌不開流動性OR閘門，只用單一嚴格門檻
    "min_turnover_percentile": None,    # 練習軌維持單純，固定金額門檻即可，不用百分位模式
    "pick_top_n": 20,
}


# ══════════════════════════════════════════════════════════════════
#  交易成本與成交假設（回測與即時紙上帳本共用，單一事實來源）
#
#  成交時序：pipeline 於收盤後 20:00~22:30 才跑，當日收盤價早已成交完畢、買不到。
#  故一律「今日收盤算訊號 → 隔日開盤成交」，買 ×(1+SLIPPAGE)、賣 ×(1−SLIPPAGE)。
#  SLIPPAGE 目前是「假設」不是「量測」——待階段3 用實盤成交回報校準
#  （回測敏感度：每 10bp 滑價約吃掉 3~4 個百分點總報酬）。
# ══════════════════════════════════════════════════════════════════
FEE_RATE = 0.001425 * 0.58   # 券商手續費 14.25bp × 58折（買賣各收一次，用實測成交反推）
TAX_RATE = 0.003             # 證交稅 30bp（僅賣出時收）
SLIPPAGE = 0.003             # 單邊滑價假設 30bp（2026-07-15 從10bp調保守；回測/PaperBroker共用同一常數）


def net_return(entry_price: float, exit_price: float) -> float:
    """一買一賣扣掉手續費+證交稅後的淨報酬（entry/exit 皆為已含滑價的成交價）。"""
    cost_in  = entry_price * (1 + FEE_RATE)
    cash_out = exit_price * (1 - FEE_RATE - TAX_RATE)
    return cash_out / cost_in - 1


def buy_fill(open_price: float, slippage: float = SLIPPAGE) -> float:
    return open_price * (1 + slippage)


def sell_fill(open_price: float, slippage: float = SLIPPAGE) -> float:
    return open_price * (1 - slippage)


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
#  個股除權息還原（2026-07-17，SPEC_QUANT_UPGRADE.md P0-2）
#
#  split_adjust() 只用在市場濾網代理股，靠「單日漲跌>20%」猜測分割事件——
#  這對「還原個股報酬」不夠用：(a) 正常除權息常在門檻之下(<20%)完全偵測不到，
#  股息就這樣從回測報酬裡消失；(b) 沒有事件對照，猜法在真實大跌日會誤判成分割。
#  這裡改用 dividend_events 表的官方公告事件（真實除權息日+前收盤+參考價），
#  不是用門檻猜的。同一套「後復權」數學（乘上調整係數讓序列跟現在的股價連續），
#  但輸入是 ground truth 不是統計推測——這也是為什麼除息會被「還原」進報酬：
#  事件前的價格被按 ref_price/pre_close 的比例向下調整，讓事件前後的報酬率
#  等於「有領到息」的真實報酬，而不是除息當天那筆虛假的下跌。
# ══════════════════════════════════════════════════════════════════
def total_return_adjust(closes: pd.Series, dividend_events: pd.DataFrame) -> pd.Series:
    """
    closes：單一股票、日期排序的收盤價 Series（index 為 date）。
    dividend_events：該股票的除權息事件，至少含 ex_date/pre_close/ref_price 三欄
                     （可為 None 或空 DataFrame，此時原樣回傳，不報錯）。
    回傳：後復權還原後的收盤價序列。
    """
    s = closes.dropna().sort_index()
    if len(s) < 2 or dividend_events is None or dividend_events.empty:
        return closes

    events = dividend_events.dropna(subset=["ex_date", "pre_close", "ref_price"])
    events = events[(events["pre_close"] > 0) & (events["ref_price"] > 0)]
    if events.empty:
        return closes

    adj = pd.Series(1.0, index=s.index)
    for _, ev in events.sort_values("ex_date").iterrows():
        ex_date = ev["ex_date"]
        ratio = float(ev["ref_price"]) / float(ev["pre_close"])
        if ratio <= 0:
            continue
        adj.loc[adj.index < ex_date] *= ratio
    return (closes * adj.reindex(closes.index).ffill().bfill()).where(closes.notna())


def apply_total_return_adjustment(closes: pd.DataFrame, dividend_events: pd.DataFrame) -> pd.DataFrame:
    """
    批次版：closes 為「日期×股票」的 pivot，dividend_events 為全市場除權息事件表
    （含 stock_id 欄）。回傳同形狀、已逐檔還原的 DataFrame。
    沒有除權息資料（dividend_events 為 None/空）時原樣回傳，優雅降級。
    """
    if dividend_events is None or dividend_events.empty:
        return closes
    out = closes.copy()
    by_stock = dividend_events.groupby("stock_id")
    for sid in closes.columns:
        if sid not in by_stock.groups:
            continue
        out[sid] = total_return_adjust(closes[sid], by_stock.get_group(sid))
    return out


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
#  投信「新進場」（2026-07-15，使用者要求：中小型股×投信剛開始買）
#  跟上面的 inv_streak 方向刻意相反：inv_streak 獎勵「已經連買很多天、
#  累計量體夠大」的股票（追蹤大戶已確立的部位）；這裡要抓的是「連買
#  第1~2天、單日量體就夠大」——趁還沒漲一大段時就進場，不是追上車。
# ══════════════════════════════════════════════════════════════════
NEW_ENTRY_MAX_STREAK_DAYS = 2   # 連買天數 1~2 天內才算「新進場」

def compute_new_entry_flag(invest: pd.DataFrame, min_lots: float = 50) -> pd.DataFrame:
    """
    輸入：invest（日期×股票，投信單日淨買超，股為單位）。
    回傳：布林矩陣，True＝當日是投信連續買超的第1或第2天、且當日單日買超
          量體 ≥ min_lots 張（避免雜訊：買100股也連續2天不該算「新進場」）。
    """
    is_pos = invest > 0
    streak = _consecutive_true(is_pos.fillna(False))
    today_lots = (invest.where(is_pos, 0.0) / 1000.0)   # 股→張，當日量體
    return (streak >= 1) & (streak <= NEW_ENTRY_MAX_STREAK_DAYS) & (today_lots >= min_lots)


# ══════════════════════════════════════════════════════════════════
#  流動性門檻（OR 邏輯，2026-07-15）：成交金額≥門檻，或「投信新進場＋
#  夠低的流動性下限」也放行——投信有揭露義務、不是隨便的主力，多方機構
#  同時確認的訊號強度不輸「單純成交金額大」，讓真正被機構買進的中小型
#  股也有入場券，同時仍保留最低流動性下限，不是完全不設防。
#  只用於 AI 軌（STRATEGY），練習軌（PRACTICE_CFG）刻意不啟用，維持單純。
# ══════════════════════════════════════════════════════════════════
def apply_liquidity_gate(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.DataFrame:
    """
    輸入：含 avg_turnover 欄位（近N日均成交金額）的候選 DataFrame，
          若已算出 invest_new_entry 欄位（bool）則納入 OR 判斷，沒有就只看主門檻。
    回傳：通過門檻的子集。

    2026-07-19（SPEC_QUANT_UPGRADE.md，10年回測發現的固定門檻疑點）：主門檻可切換
    成「當日全市場成交金額百分位」（`min_turnover_percentile`，需先算好
    `turnover_percentile` 欄位——見 backtest._candidates_asof）而不是固定NT$金額。
    固定 2億/日這種絕對值是照 2025~2026 年的市場規模校準的，10年前全市場成交量
    級別不同，同一個絕對門檻在早期年份可能過鬆或過嚴，百分位門檻才能在不同市場
    規模的年份間保持一致的「相對流動性排名」意義。沒設定 `min_turnover_percentile`
    時完全比照舊行為（絕對金額門檻），不影響任何既有結果。
    """
    if df.empty:
        return df
    pct_threshold = cfg.get("min_turnover_percentile")
    turnover = pd.to_numeric(df.get("avg_turnover", 0), errors="coerce").fillna(0)
    if pct_threshold is not None and "turnover_percentile" in df.columns:
        main_ok = pd.to_numeric(df["turnover_percentile"], errors="coerce").fillna(0) >= pct_threshold
    else:
        min_turnover = cfg.get("min_turnover_avg5", STRATEGY["min_turnover_avg5"])
        main_ok = turnover >= min_turnover

    if not cfg.get("allow_new_entry_alt_gate"):
        return df[main_ok]

    alt_turnover = cfg.get("alt_min_turnover_avg5", 30_000_000)
    if "invest_new_entry" in df.columns:
        alt_ok = df["invest_new_entry"].fillna(False).astype(bool) & (turnover >= alt_turnover)
    else:
        alt_ok = pd.Series(False, index=df.index)
    return df[main_ok | alt_ok]


# ══════════════════════════════════════════════════════════════════
#  進場評分
# ══════════════════════════════════════════════════════════════════
def _rank_norm(s: pd.Series) -> pd.Series:
    """橫斷面排序標準化 → 0~1（缺值填 0.5＝中性，不獎不罰）。

    為什麼用排序而不是原始 z-score：我們量 IC 用的是 Spearman（排序相關），
    標準化方式跟量測方式一致才合理；而且台股因子有肥尾（實例：月營收年增 +555%），
    原始 z-score 會被極端值主導，一檔怪股就能吃掉整個分數尺度。
    """
    v = pd.to_numeric(s, errors="coerce")
    if v.notna().sum() < 2:
        return pd.Series(0.5, index=s.index)
    return v.rank(pct=True).fillna(0.5)


def score_candidates_icir(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.Series:
    """
    ICIR 加權的標準化合成分數（SPEC_QUANT_UPGRADE.md §5 決策1，2026-07-23 實作）。

    跟現行手調權重版（score_candidates）的兩個關鍵差異：
    1. **權重由 ICIR 決定，不是人挑的**：權重 ∝ 該因子在目標持有期的 ICIR
       （來自 P1 的 10 年因子研究 data/research/factor_report.json），ICIR ≤ 0 的
       因子權重為 0（rs20/mom60 實測是負的，本來就已歸零）。
    2. **保留量級資訊**：現行版把最強的因子做成二元旗標——rev_yoy(ICIR 0.42，最強)
       只用 `>0` 和 `>20%` 兩個 0/1 flag，等於「營收年增 +555% 跟 +21% 拿一樣的分」；
       foreign_buy、invest_new_entry 也都是二元。這裡改用橫斷面排序標準化，把
       IC 研究說有預測力的量級差異真的用進去。

    ⚠️ 這是 opt-in（cfg["score_mode"]=="icir" 才啟用），預設仍走手調權重版——
    必須先過 10 年 A/B 驗證勝出才談改預設，照 P0~P3 一路的紀律。
    """
    horizon = str(cfg.get("icir_horizon", 20))
    table = (cfg.get("icir_weights") or {}).get(horizon) or {}
    # 只留正 ICIR 的因子；權重正規化成總和 1，讓分數尺度與因子數無關
    pos = {k: v for k, v in table.items() if v and v > 0}
    total = sum(pos.values())
    if not total:
        return pd.Series(0.0, index=df.index)

    s = pd.Series(0.0, index=df.index)
    for col, icir in pos.items():
        if col not in df.columns:
            continue
        w = icir / total
        if col == "invest_new_entry":            # 本來就是布林事件旗標，直接用
            s += df[col].fillna(False).astype(float) * w
        else:
            s += _rank_norm(df[col]) * w
    return s


def score_candidates(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.Series:
    """
    輸入：含 signal_ma_cross, signal_breakout, macd_hist, inst_net,
          foreign_net, rsi14 欄位的 DataFrame（趨勢/題材因子 rs20/stack_days/
          invest_streak 為選配，有欄位才計分）。
    輸出：每列的分數 Series。

    cfg["score_mode"]=="icir" 時改走 score_candidates_icir()（ICIR加權標準化合成）。

    2026-07-09 選股重構：新增「趨勢」（相對強度 rs20、多頭排列持續 stack_days）
    與「題材代理」（投信連買 invest_streak）三個可回測因子，用來取代原本主導
    選股、卻只認「今天剛發生」的單日技術面 event flags（w_ma_cross/w_breakout）。
    所有新因子與降權都由 cfg 權重控制，STRATEGY 預設維持舊值以免影響尚未升級的
    即時路徑；實驗用的 cfg_trend/cfg_flow 才把新因子開起來、把 event flags 降為 0。
    """
    if cfg.get("score_mode") == "icir":
        return score_candidates_icir(df, cfg)

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

    # ── 投信新進場（2026-07-15，中小型股×投信剛開始買）──
    if "invest_new_entry" in df.columns and cfg.get("w_invest_new_entry", 0) > 0:
        s += df["invest_new_entry"].fillna(False).astype(float) * cfg["w_invest_new_entry"]
    # ── 主動式ETF近期增碼檔數（輔助佐證，2日封頂做0~1飽和）──
    if "etf_accum_count" in df.columns and cfg.get("w_etf_accum", 0) > 0:
        ec = pd.to_numeric(df["etf_accum_count"], errors="coerce").fillna(0)
        s += (ec.clip(0, 2) / 2.0) * cfg["w_etf_accum"]
    return s


# ══════════════════════════════════════════════════════════════════
#  空方硬否決規則（2026-07-15，程式層級強制，見 STRATEGY 上方註解）
#  純函式，在候選股進辯論「之前」跑，被判定的股票直接排除、LLM 不會看到，
#  不透過 guardrail 的「駁回」機制（那個給裁決反駁空間，這裡不給——「無條件」）。
# ══════════════════════════════════════════════════════════════════
def compute_hard_vetoes(df: pd.DataFrame, cfg: dict = STRATEGY) -> pd.DataFrame:
    """
    輸入：候選 DataFrame，需含 stock_id, close, ma20 欄位；
          open/high/low/volume/avg_volume 缺的話該項規則自動跳過（不誤判）。
    回傳：只含被觸發列的子集 DataFrame，多兩欄 hard_veto_reason（str）。
    """
    if df.empty:
        return df.iloc[0:0]

    triggered = pd.Series(False, index=df.index)
    reasons = pd.Series([""] * len(df), index=df.index)

    dev_limit = cfg.get("hard_veto_deviation_pct", 15.0)
    if "ma20" in df.columns and "close" in df.columns:
        close = pd.to_numeric(df["close"], errors="coerce")
        ma20 = pd.to_numeric(df["ma20"], errors="coerce")
        dev = ((close - ma20) / ma20 * 100).where(ma20 > 0)
        hit = dev.abs() > dev_limit
        triggered |= hit.fillna(False)
        reasons = reasons.where(~hit.fillna(False), reasons + f"乖離月線{dev_limit:.0f}%以上；")

    if cfg.get("hard_veto_upper_wick") and all(
            c in df.columns for c in ("open", "high", "low", "close", "volume", "avg_volume")):
        o = pd.to_numeric(df["open"], errors="coerce")
        h = pd.to_numeric(df["high"], errors="coerce")
        l = pd.to_numeric(df["low"], errors="coerce")
        c = pd.to_numeric(df["close"], errors="coerce")
        vol = pd.to_numeric(df["volume"], errors="coerce")
        avg = pd.to_numeric(df["avg_volume"], errors="coerce")
        body_top = pd.concat([o, c], axis=1).max(axis=1)
        upper_wick = h - body_top
        candle_range = (h - l).where(lambda x: x > 0)
        wick_ratio = upper_wick / candle_range
        wick_hit = ((wick_ratio >= cfg.get("upper_wick_ratio", 0.6))
                    & (vol >= avg * cfg.get("high_volume_ratio", 2.5))
                    & (avg > 0)).fillna(False)
        triggered |= wick_hit
        reasons = reasons.where(~wick_hit, reasons + "帶量長上引線（疑似高檔出貨）；")

    out = df[triggered].copy()
    out["hard_veto_reason"] = reasons[triggered]
    return out


# ══════════════════════════════════════════════════════════════════
#  資金管理：建議張數
# ══════════════════════════════════════════════════════════════════
def suggest_shares(price: float, cfg: dict = STRATEGY, avg_volume: float | None = None) -> int:
    """
    1% 風險法則（股為單位，支援零股）：
      單筆最大虧損 = capital × risk_per_trade；停損打到每股虧 price × stop_loss
      → 建議股數 = 風險額度 ÷ 每股風險。
    另設集中度天花板：單一部位市值 ≤ 資金 ÷ pick_top_n。
    avg_volume 有給（近N日均量，股為單位）時，再加一道流動性天花板：單筆
    ≤ 均量 × max_pct_of_avg_volume（2026-07-15，避免買到市場承載不了的量；
    30萬小資金規模下通常不會真的卡到，是給未來資金放大用的護欄）。
    """
    if not price or price <= 0:
        return 0
    risk_budget     = cfg["capital"] * cfg["risk_per_trade"]
    shares_by_risk  = risk_budget / (price * cfg["stop_loss"])
    cap_value       = cfg["capital"] / max(cfg.get("pick_top_n", 5), 1)
    shares_by_cap   = cap_value / price
    candidates      = [shares_by_risk, shares_by_cap]
    if avg_volume:
        candidates.append(avg_volume * cfg.get("max_pct_of_avg_volume", 0.01))
    return max(math.floor(min(candidates)), 0)


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
