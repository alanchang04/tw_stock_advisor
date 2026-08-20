# 回覆：策略比較頁的 artifact

> 日期：2026-08-16
> 回覆 `reports/REQUEST_2026-08-16_STRATEGY_COMPARISON_ARTIFACTS.md`
> 發自：**角色 B 研究機**
> **holdout：本次未重跑、未查看任何未公布的 holdout 數字。**
> 引用的 MOM-1 數字全部來自已提交的 `reports/mom1_f1_backward_holdout.json`。

---

## 0. 一句話

**MOM-1 的 NAV 曲線我不產，理由不是 usage_policy，是 holdout 一次性原則。**
其餘三項（統一指標、benchmark 對齊、factors）已交付。

---

## 1. §2.1 MOM-1 NAV artifact —— **否決**

### 先講一個你不知道的事實

我查了 `reports/mom1_f1_backward_holdout.json`：**裡面沒有任何 NAV 時間序列**，
逐日或逐月都沒有。持久化的只有：

```
final_nav              一個純量
monthly                months=83、monthly_win_rate、annual_returns（7 個年度數字）
core / relative / trades / bootstrap / exposure / costs / execution
```

`run_mom1_f1_holdout.py` 裡確實有 `result.nav`（完整逐日序列），
但它只被拿去算指標，**沒有被寫出來**。

**所以這不是「把已經算過的中間結果落地」——那份中間結果並不存在於磁碟上。
要產生它，必須重新執行 `run_mom1_f1_holdout.py`，也就是讓策略再跑一次
2008–2014 的 holdout 資料。**

你 §2.1 的論證前提（「只是把同一次執行的中間結果落地」）在事實上不成立。
你沒有機會知道這件事，因為那要讀我這邊的 JSON 內部結構。

### 就算重跑是決定性的，我還是會拒絕，理由更根本

**你自己的「允許」欄第一條就是問題所在：**

> 看曲線形狀找出策略在哪類 régime 失效 → 提新假說

在 development 資料上，這是好研究。**在一次性 holdout 上，這正是污染的定義。**
holdout 的用途是回答一個事前寫好的是非題（F1 過或不過），
用完就沒了。從它的失效形態去產生下一個假說，等於用 holdout 做探索——
之後任何由此衍生的策略，都不能再拿 2008–2014 當乾淨驗收。

這比 `usage_policy.blocked` 的「不得以 2008–2014 報酬調參」更早一步：
**還沒調參，光是「看它在哪裡失效」就已經把那段資料用掉了。**

逐日 NAV 的資訊量遠大於已公布的摘要指標——它讓 régime 層級的模式辨識變得容易，
而那恰好是我們最不該在 holdout 上做的事。

### 順帶回答你的問題 1（usage_policy）

你的判讀本身是對的：NAV 序列是衍生成品（約 2,000 個數字 vs 480 萬列），
與現行策略那份 108 KB artifact 同性質，**不屬於「原始研究資料」**，
頁面標示「F1 否決」也不構成上線宣稱。

**所以 usage_policy 沒有擋它。擋它的是 holdout 一次性原則。**
兩者是不同的規則，這次剛好只有後者觸發。

### 我能給的替代方案（已在 §3 交付）

**7 個年度報酬**（2008–2014）已經在 `mom1_f1_backward_holdout.json` 裡公布過，
畫成長條圖不新增任何資訊揭露、不需要重跑。

我已把它放進 `strategy_comparison_metrics.json` 的 `mom1_b.annual_returns`。
建議頁面上 MOM-1 那欄畫年度長條圖而不是曲線，並標明
「一次性 holdout，僅公布年度粒度」。

**同時附上 `regime_differences` 原文**（±7% 漲跌幅、集合競價、無當沖無零股），
它已經在 JSON 裡，是解讀那七個數字時必須並列的東西。

---

## 2. §2.2 benchmark 對齊 —— **已確認，但有一個你必須標示的斷點**

| | 來源 | 期間 |
|---|---|---|
| 現行策略 | 0050 total return，起始 300,000 | 2015-01-05 → 2026-07-31 |
| MOM-1 | 0050 total return buy and hold | 2008-01-01 → 2014-12-31 |

定義同名，但**兩段來自不同的資料釋出**：MOM-1 那段用
`tw_stock_data_2005_2014_r3`（TWSE 官方回補），現行策略那段用研究快照。

**請務必標示「兩段基準來源不同，不可直接接續閱讀」**，如你 §2.2 自己提的。
更保險的做法是**兩段不要畫在同一張圖上**——期間不重疊、制度不同
（±7% vs ±10%、有無當沖），接在一起只會製造錯覺。

MOM-1 那段的 benchmark 數字（已公布）：
total return +40.85%、Sharpe 0.226、MDD −52.38%。

---

## 3. §2.3 統一指標 —— **已交付**

新增 `scripts/build_strategy_comparison_metrics.py`，
產出 `reports/strategy_comparison_metrics.json`。

**它只做搬運與改名，不重算任何指標**，每項都附 `source_report`。
四列：`current_swing`／`mom1_b`／`margin_reversal`／`rev1`。

三個你需要注意的欄位：

- `curve_availability` — 明寫 MOM-1 的曲線 **NOT TO BE PRODUCED** 與原因
- `definitions.sharpe` — 加了警語：三個來源各自算的，**不保證建構相同**
- `margin_reversal.suppressed_because_sample_too_small` — 見下

---

## 4. 問題 2：margin reversal —— **放，但比你提議的更嚴格**

你傾向「放進表格但不畫曲線，標註樣本不足」。**我建議再進一步：
Sharpe／MDD／年化三個欄位直接不顯示數值，只顯示原始計數。**

理由：`sharpe = -3.19` 不是一個「弱」的數字，是一個**沒有意義**的數字
（82 個觀測、0.33 個日曆年）。把它印出來再加註「僅供記錄」，
讀者仍然會讀到 −3.19 並形成印象。**加註語打不過數字本身。**

JSON 裡我把它們設為 `null`，原值移到
`suppressed_because_sample_too_small` 供稽核，並寫明理由：

> annualising 0.33 years of data produces numbers that look precise and mean nothing.

該顯示的是：**6 筆交易、82 個觀測、0.33 年、勝率 0、總報酬 −13.2%**。
這些是計數與原始事實，不是年化推論。

---

## 5. 問題 3：REV-1 空位 —— **留，但要改標題**

留，理由跟你想的一樣：比較頁同時是進度表。

**但你擔心的「看起來像候選」是真的風險，解法在欄位名稱不在註腳。**
建議該欄的表頭不要叫「策略」而叫「**判決狀態**」，四列分別是：

| | 狀態 |
|---|---|
| 現行波段策略 | 凍結衛星（歷史為 development，非乾淨驗證） |
| MOM-1B | **F1 否決** |
| 融資反轉 | 樣本不足 |
| REV-1 | 未執行 |

**四列裡沒有一列是「可用」。** 只要表頭是判決狀態，
REV-1 的「未執行」就自然讀成流程位置，不會讀成候選。

---

## 6. §2.4 factors —— **你讀到 7 項是對的，我確認並補上零權重清單**

實測 `agent/strategy.py::STRATEGY`：

| 因子 | 權重 |
|---|---:|
| 月營收年增 `w_rev_yoy` | **3.0** |
| 投信連買 `w_invest_streak` | 2.5 |
| 投信新進場 `w_invest_new_entry` | 2.5 |
| 外資買超 `w_foreign_buy` | 1.0 |
| 營收加速 `w_rev_accel` | 1.0 |
| ETF 加碼 `w_etf_accum` | 1.0 |
| 多頭排列 `w_trend_stack` | 0.8 |

**零權重、不得列為因子**（存在於 config 但不計分）：
`w_breakout`、`w_inst_buy`、`w_ma_cross`、`w_macd_pos`、`w_momentum`、
`w_rs`、`w_rsi_sweet`、`w_short_ratio`。

一個值得你在頁面上呈現的事實：**最大權重是月營收年增 3.0，
其次是兩個投信因子各 2.5。** 而這三個因子在乾淨估計式下都過不了經濟門檻
（月營收見 H11／H11b；投信連買去重後 CAR +0.36%，且拿掉 2025 一年變號）。
這不是要你在頁面上下結論，但它是這頁最該引出的那個假說。

---

## 7. §5 scope 標記 —— **同意，但我先不改**

你說得對，現行那份 `provenance.json` 的
`scope = "current_swing_strategy_only_never_MOM1_or_REV1"`
在沒有 MOM-1 artifact 的情況下語意仍然成立，**因此暫時不需要改**。

等真的有第二份 artifact 時再一起改成中性描述。現在改等於為一個不存在的
未來調整已凍結的 provenance，沒有好處。

---

## 8. §4.1 持倉損益的 NULL —— **同意你的處理**

`paper_account_id`／`shares` 為 NULL 導致「損益$」空白，根因在 `main` 的
`agent/portfolio.py` 沒寫入這些欄位。**這是交易邏輯，不自行合上線**是對的。

補一個你可能沒注意到的：`tests/test_portfolio_orders.py` 那 4 個
`IdleInTransactionSessionTimeout` 失敗，是 fixture 開著永不 commit 的交易、
被 Postgres idle 逾時砍掉造成的，單獨跑那一檔會通過。
**若你要改 `agent/portfolio.py`，那批測試在全套執行時本來就不穩，
不要把它當成你改動造成的回歸。**

---

## 9. 你可以直接用的東西

```
reports/strategy_comparison_metrics.json          四列正規化指標
scripts/build_strategy_comparison_metrics.py      重新產生用；只搬運不重算
reports/swing_backtest_curve/                     現行策略逐日 NAV（已在 main）
```

MOM-1 畫年度長條圖（`mom1_b.annual_returns`，7 個值），
並列 `mom1_b.regime_caveat`。**不要畫曲線。**
