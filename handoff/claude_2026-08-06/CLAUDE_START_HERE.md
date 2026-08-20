# Claude 技術交接：台股波段策略研究

交接日期：2026-08-06（Asia/Taipei）
專案：`tw_stock_advisor`
目前階段：回測可信度已大幅修正；正式波段策略仍未證明優於 0050；下一輪應研究「現行品質選股＋可執行突破時機＋現行出場」。

---

## 0. 先讀這段

1. **不要重設工作樹。** 目前有多個尚未提交但彼此相關的修改；禁止 `git reset --hard`、`git checkout --` 或覆蓋現有檔案。
2. `.vscode/settings.json` 是使用者原有／無關變更，請勿碰。
3. 正式 `STRATEGY` 目前仍維持固定 8% 停損、binary 市場濾網、重進場停用；最近研究都沒有改正式預設。
4. **2026 年 8 月不要再次讀 validation。** `validation=2021-01-01~2022-12-31` 已於 2026-08-05 被 P3-6 一次性批次使用。
5. `holdout=2023-01-01~2025-05-31` 已於制度建立前被間接看過並在 2026-07-24 補登；不要再次把它當乾淨 holdout。
6. `2025-06-01+` 是 hard-contaminated，只能診斷或前向觀察，不可當驗收樣本。
7. ZIP 刻意不含 `.env`、行情 parquet、DB dump、憑證或實盤資料。原 workspace 的 `data/research` 可供本機重跑。

完整資料切分規則：`research/data_splits.py`、`research/SPLIT_ACCESS_LOG.md`。

---

## 1. 已完成的基礎可信度修正

### 1.1 回測／實盤一致性與真實資金

已處理的主要問題：

- 回測與實盤共用候選評分、因子、停損與部位計算函式。
- 訊號日收盤判斷，統一於隔日開盤成交並加入滑價。
- 使用真實現金、股數與逐日 NAV，不再把每筆報酬簡單平均成投資組合報酬。
- 個股與 0050 使用還原價格／含息比較；0050 的 2025-06-18 一拆四已處理。
- 排除 ETF、處置股、未上市／下市時間錯置等不可交易標的。
- 修正設定值未生效、報表讀錯設定、法人單位與歷史資料品質問題。
- 舊 paper positions 沒有真實 `shares`，不能誠實倒推完整歷史 NAV；UI／報表只應呈現新帳本建立後的 forward NAV。

主要程式：

- `agent/backtest.py`
- `agent/strategy.py`
- `agent/stock_selector.py`
- `agent/portfolio.py`
- `agent/paper_account.py`
- `agent/broker.py`
- `agent/daily_runner.py`

### 1.2 測試狀態

最後完整測試：

```text
461 passed, 55 skipped, 1 warning
```

警告是 pandas 對常數陣列 Spearman correlation 無法定義，非本輪回歸。

重跑：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 2. 現行波段策略到底是什麼

正式 AI 軌目前偏向「品質／法人」而非純價格動能：

- `w_rev_yoy=3.0`
- `w_rev_accel=1.0`
- `w_invest_streak=2.5`
- `w_invest_new_entry=2.5`
- `w_foreign_buy=1.0`
- `w_trend_stack=0.8`
- `w_breakout=0`
- `w_rs=0`
- `w_momentum=0`

正式流程：量化候選前 20 → LLM 多空辯論／裁決 → 實際 paper order。
歷史回測不含 LLM，只測底層量化訊號；這仍是 live/backtest 分布差異，必須以前向 A/B 累積樣本，不可假裝歷史可重現 LLM。

正式出場：

- 固定 8% 災難停損。
- 牛市關閉 MA5/MA20 死叉；熊市由 market filter 重開。
- 固定停利關閉。
- 峰值獲利 ≥50% 後才啟動寬 trailing backstop（回落 25%）。
- `max_hold_days=0`，不強迫時間出場。

市場濾網：0050 還原收盤 vs MA60；binary 模式空頭禁止新倉。

---

## 3. 現行策略的實證結論

### 3.1 長期與 0050

舊全期診斷（2015-01~2026-07）：

| | 總報酬 | 年化 | Sharpe | MDD | Calmar |
|---|---:|---:|---:|---:|---:|
| 現行策略 | +328.0% | 14.0% | 0.97 | -27.4% | 0.51 |
| 0050 含息買進持有 | +745.5% | 約 21.2% | 1.08 | -34.0% | 0.62 |

解讀：策略全期 MDD 曾比 0050 淺，但報酬、Sharpe、Calmar 都輸；不能宣稱風險調整後優於 0050。

樣本外／污染診斷：

- 2015-01~2025-05：策略 +80.0%（年化約 6.0%），0050 +274.7%（年化約 14.1%）。
- 2025-06~2026-07（被調校的硬污染期）：策略 +138.7%，0050 +130.6%。
- 這是明顯的 regime／過擬合警訊。

對 0050 超額報酬檢定：年化 -6.91%，`p=0.2187`；沒有證據支持策略優於 0050。

### 3.2 Regime 歸因

| 市況 | 佔比 | 策略歸因 | 0050 歸因 | 結論 |
|---|---:|---:|---:|---|
| 多頭 | 59.6% | +666.9% | +1901.6% | 策略嚴重漏掉大多頭右尾 |
| 空頭 | 23.5% | -36.7% | -25.4% | 並未證明熊市保護較好 |
| 盤整 | 16.8% | -11.6% | -43.4% | 唯一相對占優，多半來自低曝險／空手 |

主要問題不是單一 8% 停損，而是：

1. benchmark-relative entry edge 很薄。
2. 多頭時市場濾網／現金曝險造成巨大機會成本。
3. 現行品質選股未明確限制「何時買」，股票可位於任何圖形階段。
4. live 還有 LLM 子選擇，歷史回測未覆蓋。

### 3.3 候選訊號的相對優勢

- 20 日平均超額約 +1.1%，60 日約 +1.4%。
- 只有約 38%~45% 候選打敗同期 0050。
- 完整來回摩擦約 1.065%，短持有期 edge 幾乎被成本吃掉。
- 交易成本年化約 4.64%，只解釋總落差約 30%，不是唯一主犯。

---

## 4. 停損、重進場、曝險研究

### 4.1 8% 停損不是主要主犯

歷史 stop sensitivity：5%／6%／7%／8%／10%／12% 沒有單調改善；放寬到 20% 也沒有接回超額報酬。

173 筆歷史停損診斷：

- 隔日開盤站回停損線：29/173（16.8%）。
- 隔日開盤直接站回原始成本：1/173（0.6%，緯創特例）。
- 5／10／20／40 日內收復原成本：約 5.8%／17.5%／27.1%／42.9%。
- 約 25.9% 在停損後 20 日內再跌至少 10%。

所以神達／緯創式 false stop 存在，但不能據此全面取消固定停損。

### 4.2 P3-6 預先登記風控

Development（2015~2020）：

| 版本 | 年化 | Sharpe | MDD |
|---|---:|---:|---:|
| baseline | 13.55% | 0.81 | -26.64% |
| 隔日收復停損取消 | 13.84% | 0.82 | -27.28% |
| ATR 等風險 | 15.03% | 0.88 | -24.88% |
| 重進場狀態機 | 12.56% | 0.77 | -26.92% |
| 三級曝險 | 14.03% | 1.09 | -22.00% |

Validation（2021~2022）：所有版本約 -16.1%~-16.8% 年化、MDD -40.3%~-43.1%，同期 0050 總報酬 -6.40%。全部否決部署。

注意：重進場 development 實際觸發 **0 次**；差異來自觀察期暫停一般重買。只能說此規格無樣本，不能說所有重進場概念無效。

報告：`research/results/swing_risk_experiments_2026-08-05.md/json`。

---

## 5. Qullamaggie／Muninn 研究

### 5.1 為什麼不能直接套出場

現行策略主力是營收與法人品質；Qullamaggie 則需要先前大漲、領先強度、緊密 base、突破當日低風險成本。兩者進場分布完全不同。

### 5.2 P3-7：收盤確認後隔日開盤進場

同一研究引擎精確重現現行 development baseline `13.55% / Sharpe 0.81 / MDD -26.64% / 236筆`，排除引擎差異。

| 進場／出場 | 年化 | Sharpe | MDD |
|---|---:|---:|---:|
| 現行／現行 | 13.55% | 0.81 | -26.64% |
| 現行／Q 全倉 10MA | 5.06% | 0.42 | -27.39% |
| 現行／Q 第5日半倉＋10MA | 0.85% | 0.13 | -24.89% |
| 現行／Q 第5日半倉＋20MA | -3.69% | -0.42 | -30.08% |
| Q／現行 | 6.21% | 0.54 | -26.97% |
| 完整 Q 三版 | +0.79% 至 -2.11% | 0.15 至 -0.49 | 約 -11.6% 至 -13.8% |

修正存活者偏誤後，Q 日線進場所有交易無條件追蹤：Day3 約 -1.00%、Day5 約 -0.99%、Day10 約 +1.29%；Day5 僅約 37% 為正。因此 Muninn 的「第5日減半」不適用此訊號分布。

重要方法：即使交易 Day2 停損，事件研究仍必須繼續追蹤原股票到 Day3／5／10；不可只統計仍存活部位。

### 5.3 P3-8：前一日 watchlist、次日 stop-buy

可執行時序：T 日收盤建立 watchlist／箱頂；T+1 未觸價取消，跳空越過按開盤；初始停損只用 T 日 ADR。日 K 不知道先高後低，同時跑 conservative／relaxed。

| 版本 | 年化 | Sharpe | MDD |
|---|---:|---:|---:|
| Q stop-buy／現行出場 | +4.19% | 0.30 | -44.52% |
| Q＋全倉10MA conservative | -6.28% | -0.54 | -44.97% |
| Q＋全倉10MA relaxed | -2.25% | -0.15 | -33.42% |
| 其他 Q 半倉版本 | -4.38% 至 -9.22% | 全負 | 約 -33% 至 -44% |

預掛使 Day3／Day5 從約 -1.00%／-0.99% 改善至約 -0.45%／-0.52%，但仍未轉正；Day10 僅約 +0.6%~+0.7%。同日停損：最佳 conservative 111 次，relaxed 38 次。兩邊界方向一致。

判決：否決機械 Q 版本；目前不值得優先取得 5 分鐘資料。這不證明 Qullamaggie 本人的裁量交易無效，只證明目前可機械化的條件在台股 development 沒有形成優勢。

程式／報告：

- `research/qullamaggie.py`
- `scripts/run_qullamaggie_factorial.py`
- `scripts/run_q_stop_order_study.py`
- `tests/test_qullamaggie.py`
- `research/results/qullamaggie_factorial_2026-08-05.*`
- `research/results/q_stop_order_study_2026-08-05.*`

---

## 6. 融資反轉獨立策略

策略位於獨立 `margin_reversal/`，不與正式波段設定混用，且預設停用。

目前只有 2026-04-01~2026-07-30 的短樣本：

- 85,453 data rows。
- 12 個 selected signals、6 筆實際交易。
- 6 筆全數停損，平均淨報酬 -11.23%，策略總報酬 -13.18%。
- 同期間 benchmark +23.07%。
- matched event study 有效 treated/control 各僅 3 筆；Day5 treated 比 control 更差約 -4.74pp，但樣本不足。
- 多數交易在進場後繼續下跌，不是「停損後兩三天立即反彈」。
- 聯電 2303：有 1 次 margin wash event，但 0 次完整 signal；不能為聯電事後表現放寬規則。

結論：只證明「目前定義＋短樣本」無效，不能宣告融資清洗概念永遠無效。下一步必須先擴充 point-in-time 融資歷史至至少 2020、2022、2025 壓力期，處理 right-censoring，再做事件研究；未通過前不可做參數掃描或上線。

報告在原 workspace `reports/margin_reversal_study.*`、`reports/margin_reversal_path_audit.json`；交接 ZIP 有包含報告，但沒有 DB／行情資料。

---

## 7. 最後一次 live/paper audit（2026-08-05；接手後先重跑）

此段是當時資料快照，不是長期統計：

- 正式 v3 `live_from=2026-07-24`。
- v3 乾淨已平倉僅 2 筆：緯創 3231 約 -0.49% 淨損後反彈；神達 3706 約 -8.43% 後回到接近成本。
- 含跨版本受影響的 7 筆已平倉，平均約 -10.54%、0 勝；但其中有些停損確實保護後續大跌，不能只看緯創／神達。
- 當時 0050 還原收盤 100.65 < MA60 102.55，breadth 約 23.5%，`risk_off`，binary market filter 阻擋新倉。
- 正式前向樣本太少，不能用 live MDD 驗證長期「回撤較淺」宣稱；但虧損聚集是真實問題。

接手後若 DB 已更新，請重跑 read-only live audit，不要直接沿用這些價格。

---

## 8. 下一個應做的主實驗（最高優先）

### 研究問題

現行策略是否「品質選股有一些 edge，但買在錯誤圖形階段」？
測試：**現行營收／法人排名＋前一日可執行突破時機＋現行出場**。

### 預先固定的四組（只跑 development）

必須使用同一個 portfolio engine、相同成本、現金、股數、market filter、sector cap、現行出場：

1. `C0_formal_5d`：正式 5 日 cadence、隔日開盤，作為已知 baseline。
2. `C1_daily_next_open`：現行品質排名每日檢查、隔日開盤；隔離「每日 cadence」本身影響。
3. `H1_quality_stop_buy`：T 日現行品質 top20；若距 20 日箱頂 ≤3%，T+1 預掛箱頂 stop-buy，未觸價取消。
4. `H2_quality_tight_stop_buy`：H1 再要求 10 日箱體幅度 ÷ 前 20 日箱體幅度 ≤0.75。
5. `H3_quality_tight_dryup`：H2 再要求最近 5 日均量 < 前 20 日均量。

說明：雖寫「四組」，實際含 baseline 共五列；命名保持如上以免混淆。不要在看到結果後改 3%、0.75 或 5/20 日。

### 成交與停損

- 所有 watchlist 條件只使用 T 日收盤前資料。
- T+1 `high < trigger`：不成交、當日取消。
- `open >= trigger`：`open × (1+slippage)`。
- 否則觸價：`trigger × (1+slippage)`。
- 本輪一律沿用現行固定 8%／現行出場，**不要同時改結構停損或 Q 出場**，才能隔離進場時機。
- 跌停、停牌、流動性、處置股、ETF 排除與正式回測一致。

### 必要對照／報表

- C0 必須再次精確或近乎精確重現 `13.55% / 0.81 / -26.64% / 236筆`。
- 年化、Sharpe、MDD、Calmar、turnover、曝險率、交易數、平均持有。
- Day3／5／10／20 無條件進場路徑；提前停損者仍繼續追蹤。
- MFE／MAE、停損比例、跳空成交比例、未觸價取消比例。
- Top 1／5／10 贏家貢獻；移除最佳 5 筆後淨 PnL／年化仍需為正。
- 分年結果；不可只看合併總報酬。
- 0050 同期使用相同成本口徑。

### Development 通過門檻

候選至少同時達成：

- 年化與 Sharpe 都高於 C0；不能只用較低 MDD 換掉大部分報酬。
- MDD 不比 C0 惡化超過 3 個百分點。
- 移除最佳 5 筆後仍正 PnL。
- 改善不能只來自單一年份或單一族群。
- H1→H2→H3 若只有單點尖峰，視為不穩健；禁止再掃鄰近門檻找贏家。

### 資料紀律

- 現在只跑 development。
- 2026 年 8 月不要再碰 validation。
- 若 development 有預先登記的通過候選，最早 2026 年 9 月把所有通過候選做**一次批次 validation**，不可逐一看結果後追加。
- 不讀 holdout。

---

## 9. 第二優先研究

只有主實驗有結果後再做：

### 9.1 Core-satellite，而不是硬逼純策略贏 0050

測試 idle cash 是否改持 0050，或固定 70% 0050＋30% swing satellite。這是資產配置策略，不得與「選股 alpha」混為一談。預先固定 100/0、70/30、50/50 三組，不能掃最佳比例。

### 9.2 前向 LLM A/B

回測不含 LLM。繼續使用 `llm_ab_tracking` 記錄：量化 top5／LLM picks／未選候選的同期報酬。至少累積 30~50 個成熟事件後再判斷 LLM 是加值還是破壞底層排序。

### 9.3 重進場

現有規格零觸發，不應直接放寬門檻。若 hybrid stop-buy 成立，再將「停損後重新形成同一可交易 base 並再度觸價」定義為 reentry；這樣重進場與原進場同構，不是任意站回 MA20。

---

## 10. 明確不要做的事

- 不要因緯創／神達兩檔把 8% 停損改寬或取消。
- 不要再掃 5%~20% 停損、ATR multiple、Q prior-move、ADR、base days、Day5/10/20 出場找最佳值。
- 不要把 conditional winners 的 Day3/Day5 路徑當成所有交易的期望值。
- 不要把 Qullamaggie 的 829 筆美股裁量交易統計直接套到台股機械訊號。
- 不要把 0050 扣掉台積電後的假想指數當正式 benchmark；正式 benchmark 就是可投資的含息 0050。
- 不要把 validation／holdout 當 development 反覆使用。
- 不要啟用 `margin_reversal`，直到融資歷史與事件樣本成熟。
- 不要改正式 `STRATEGY`，除非預先登記版本通過 development、批次 validation 與前向 paper gate。

---

## 11. 常用重跑指令

```powershell
# 完整測試
.\.venv\Scripts\python.exe -m pytest -q

# P3-6 波段風控（不要在 2026-08 再跑 validation）
.\.venv\Scripts\python.exe scripts\run_swing_risk_experiments.py --split development --no-write

# P3-7 Q 因子拆解（約數分鐘）
.\.venv\Scripts\python.exe scripts\run_qullamaggie_factorial.py --split development

# P3-8 前一日 Q stop-buy（約 10 分鐘）
.\.venv\Scripts\python.exe scripts\run_q_stop_order_study.py
```

所有新實驗先追加到 `research/EXPERIMENTS.md` 再執行。

---

## 12. 建議 Claude 的第一個動作

1. 讀完本文件。
2. 讀 `research/EXPERIMENTS.md`、三份 2026-08-05 結果 MD／JSON、`agent/backtest.py`、`agent/strategy.py`、`research/qullamaggie.py`。
3. 執行完整 pytest，確認仍為 461 passed 左右。
4. 在 `research/EXPERIMENTS.md` **先登記**第 8 節五列變體。
5. 新增獨立腳本，例如 `scripts/run_quality_breakout_timing_study.py`，不要修改正式預設。
6. 只跑 development，輸出 JSON＋Markdown；報告必須包含無條件事件路徑與右尾移除測試。
