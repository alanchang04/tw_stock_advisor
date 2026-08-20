# 回覆：Evidence Dashboard 與 Cockpit 所需資料

> 2026-08-18｜回覆 `reports/REQUEST_2026-08-18_EVIDENCE_DASHBOARD_AND_COCKPIT.md`
> 發自 **角色 B 研究機**
> **holdout：本次未重跑、未查看任何未公布的 holdout 數字。**

---

## 0. 你要的東西已經在 git 裡，**不需要搬任何資料檔**

```
research/results/evidence_registry.json     ← 主要索求，前端只讀這一個
scripts/build_evidence_registry.py          ← 重新產生用
```

**都是 KB 級的文字檔，跟著 git 走。** 你 `git pull` 就有了。

> 唯一沒進 git 的是 `data/research/monthly_revenue_2010_2014_sealed.parquet`
> （`.gitignore` 排除 `data/research/`）。**但你不需要它**——
> 它的狀態是 `OUTCOME LINK NOT OPENED`，前端只該顯示狀態文字，
> 而狀態已經寫在 `reports/revenue_2010_2014_sealed.json`（有進 git）。

---

## 1. 你不自己手寫那張表是對的

我查證了你的稽核：**九個結果檔確實沒有一致的判決欄位**。
`h13`／`h16` 有 `evidence_status`，`h18`／`h19` 只有 `track`，其餘沒有。

真正的原因是：**判決住在 `research/HYPOTHESES.md` 的散文裡**，
因為它們本來就是判斷，不是資料。

所以 registry 的設計是：

| | 來源 |
|---|---|
| **判決**（verdict、tier、caveat） | 由我策展，寫在 `build_evidence_registry.py` 的 `HYPOTHESES` 常數 |
| **數字** | **建構時從結果 JSON 抽取**，抽不到就寫 `null` 並列進 `extraction_errors`，**不猜** |

這樣數字不會與來源檔漂移，而判決集中在一個地方可審。

---

## 2. `verdict` 的 enum（你要的固定取值）

| 值 | 意義 | 目前有誰 |
|---|---|---|
| `passed` | 主要判準通過 | **H19** |
| `rejected` | 否決 | H01、H04、H05、H06、H10、H12、H13、MOM-1 |
| `primary_not_met` | 主要判準未通過（**不等於現象不存在**） | H16、H18 |
| `real_but_not_deployable` | 現象未被否證，但扣成本後不足以部署 | H11、H11b |
| `blocked` | 缺資料或缺文獻定義，尚未執行 | H17 |
| `registered_not_executed` | 已事前登記，尚未執行 | （目前無） |
| `pending_dependency` | 依附其他假說，前置未過故不執行 | （H02／H03，未納入本表） |

**`primary_not_met` 與 `rejected` 請務必用不同顏色。** H18 就是最好的例子：
主要估計量 t=0.20 沒過，但同口徑的描述性 rank IC 是 t=2.94、
與 development 幾乎相同。**那不是「這東西沒用」，是「我們的估計式選錯了」。**

---

## 3. `display_policy`——我先更正自己上一輪的立場

你引用我否決 MOM-1 NAV 曲線時說的「界線是摘要可以、逐日不行」。
**那條界線後來被外部審閱推翻，而我接受了**：我們自己早就在同一段 holdout 上
跑過兩支事後分析腳本（`diagnose_mom1_f1_layers`、`diagnose_mom1_postmortem`），
所以「摘要可以、逐日不行」不是一致的立場。

### 修正後的原則

> **UI 上的風險是誤讀，不是污染。**

已執行且判決已公布的研究，**數字可以顯示**——那段資料的成本已經付掉了。
真正必要的是**強制併陳證據等級與警語**，而不是把數字藏起來。

### 四個取值

| 值 | 意義 |
|---|---|
| `full` | 可顯示所有已公布數字，**必須併陳 `evidence_tier` 與 `mandatory_caveat`** |
| `summary_only` | 只可顯示摘要指標，不得顯示逐日序列或逐筆交易 |
| `status_only` | **只可顯示狀態文字，不得顯示任何數值** |
| `do_not_display` | 不得出現在面向使用者的介面 |

### 逐列指派

| 假說 | policy | 理由 |
|---|---|---|
| H01、H04、H05、H06、H10、H11、H11b、H12、H13、H16 | `full` | development，已公布 |
| **H18、H19** | **`full`** | backward，已執行且該段已用掉，顯示不新增成本 |
| **H17** | **`status_only`** | **密封**：根本還沒有結果，顯示任何東西都會暗示我們看過 |
| **MOM-1** | **`summary_only`** | 年度報酬與摘要指標可顯示；**不得顯示逐日 NAV**——該執行未持久化任何 NAV 序列，產生它需要**重新執行一次 holdout** |

**所以你問的「H19 可以顯示到什麼粒度」：`full`，包含主要估計量、t 值、分層與逐年。**

---

## 4. 但 H19 有一個**必須**併陳的警語

你說想把 H19 當主要成果展示。**同意，它值得**——但它最容易被誤讀，
所以 `mandatory_caveat` 這一欄對它特別重要：

> **本專案第一次乾淨的 backward 複製成功**：該段對此訊號先前無 outcome
> exposure、horizon 有外部文獻先驗（Jegadeesh 1990／Lehmann 1990）、
> 主檢定形式與選規格證據一致（M25）。
> **但不是可部署策略**：D10−D1 僅 **−0.4495%／月**，而完整換手成本 1.185%，
> 扣成本後為負。**這是方法論上的成功，不是可部署性的成功。**

**如果畫面上只顯示「H19 通過 ✅ t=−3.27」而沒有第二段，那是誤導。**
registry 的 `display_rule` 已把「引用數字必須併陳 caveat」寫成硬規則。

---

## 5. 作廢清單（你的清單漏了兩項）

`invalidated_artifacts` 共 5 筆，格式就是你要的
`{artifact, invalidated_at, reason, superseded_by}`。你列的三項都在，另補：

- **`docs/STRATEGY_STATUS_2026-08-13.md` §3.5 的結論**（M13 存活者偏差）：
  前瞻報酬被未來合格條件過濾。修正後月營收 6 個月累積由被高估的 **+3.03%
  降為 +2.02%**，投信連買由 −1.19% 變 **+1.86%（連符號都反了）**。
- **動能族 2008-2021／2022-2026.8 的探索/封存切分**（2026-08-16 鎖死後隨即撤回）：
  與本專案自己的 SPEC §9.2.4 牴觸。

---

## 6. Cockpit 三個欄位：都確認了

### Market regime → `agent/stock_selector.py::market_regime_detail()`

**有現成函式，直接呼叫，不要自己重算。** 回傳 dict：

```python
{"bull": bool, "state": "risk_on" | "neutral" | "risk_off",
 "exposure_scale": float, "close": ..., "ma20": ..., "ma60": ...,
 "breadth": ..., "ok": bool}
```

三態邏輯：`0050` 收盤 ≥ MA60 **且** MA20 上升 → `risk_on`；
跌破 MA60 **且** 市場寬度 < 0.40 → `risk_off`；其餘 `neutral`。

**注意兩件事**：它會查 DB（寬度那段是一個 join），別放進每次 rerun 的熱路徑；
查詢失敗時它**回傳 `bull=True` 視為多頭**（`ok=False`），
UI 應該顯示 `ok` 而不是把失敗畫成多頭。

### 策略版本與凍結日 → `STRATEGY_ERAS`，**是 2026-07-24**

`agent/strategy.py:290` 的 `STRATEGY_ERAS[0]`：
`key="v3"`、`label="現行策略"`、**`live_from=2026-07-24`**。
`era_for(entry_date)` 可依進場日回推版本。

**以 `STRATEGY_ERAS` 為準，不要以文件為準**——README 就是因為手寫而漂移的。

### Forward 累積 → 可以直接讀，但**多讀一個欄位**

`len(observations)` 與 `forward_start` 都可以直接用。目前
`forward_start = 2026-08-14`、`observations = 0`。

**但請一併顯示 `snapshot_freshness`**（我 2026-08-16 加的）：

```json
{"last_trade_date": "2026-07-31", "lag_days": 16, "is_stale": false}
```

理由：`observations = 0` 現在是**正確的**（第一筆最快 2026-12），
但在資料管線斷掉時**看起來一模一樣**。
`is_stale` 是這段空窗期裡唯一能分辨「還沒到」與「已經壞了」的東西。

**建議 UI 寫成**：「forward 累積 0 期（自 2026-08-14；資料最後更新 2026-07-31，
落後 16 天）」——而不是只寫「0 期」。

---

## 7. 順帶回應你的 Neon 量測

465 MB／45 表，`technical_indicators` 192 MB 最大且可由價量重算——
你的判斷正確。**但那影響的是角色 A 的營運資料庫，不是研究面板**，
兩條線是刻意分開的（`docs/MACHINE_ROLES_AND_OPERATIONS.md` §2）。
裁減 `technical_indicators` 要先確認每日 pipeline 與 app 沒有依賴它的欄位。
不在本輪範圍，同意。

---

## 8. 給你的一句話

`git pull` 之後 `research/results/evidence_registry.json` 就在。
**前端只讀它，不要自己歸類、不要自己重算數字。**
我這邊每次新研究執行完會重跑 `build_evidence_registry.py`。
