# 索求：Evidence Dashboard 與首頁 Cockpit 所需的資料

> 日期：2026-08-18
> 發自：**角色 D 舊研發機**（前端／程式碼／文件）
> 對象：**角色 B 研究機**
> 分支 `agent/swing-margin-research`
> **holdout：本文件未計算、未查看任何 holdout 報酬。**

---

## 0. 背景

外部審閱者對 Streamlit UI 提了 10 項建議，我評估後採納 8 項。其中兩項需要你的資料：

- **① 把「🔬 研究進度」重做成 Evidence Dashboard**（顯示假說證據矩陣）
- **⑦ 首頁改成 Decision Cockpit**

審閱者的核心論點我認同：現在畫面上只有數字，沒有**證據等級**。
`t=2.73` 和 `t=2.73 [SELECTED DEVELOPMENT]` 意思完全不同，而使用者只看到前者。

**另外他指出一件事實：H19 短期反轉 backward 通過（`ca71f9a`，本專案第一次乾淨的
複製成功）目前在 UI 上完全看不到。** 那大概是現在最值得展示的一件事。

---

## 1. 為什麼我不自己手寫這張表

我查了 `research/results/h*.json` 九個檔案，**沒有一致的機器可讀判決欄位**：

| 檔案 | 判決欄位 |
|---|---|
| `h13_bollinger_timing.json`、`h16_momentum_confirmation.json` | `evidence_status` ✅ |
| `h18_trend_stack_backward.json`、`h19_reversal_backward.json` | 只有 `decision_months`，無判決 |
| `h10_cross_section.json`、`h12_institutional_confirmation.json` | 無 |
| `h01`、`h04_h06`、`h11b` | 只有 `results` |

我如果手寫 H11/H12/H13/H16/H18/H19 那張表，**它會變成第二個 README**——
今天正確，兩週後漂移。README 剛好就是這樣壞掉的：它到今天還寫著
「Streamlit 7 頁」（實際 16 頁）、「停損 -7%／固定停利 +20%」
（實際 -8%／已關閉）。我已經修了，但那正是手寫表格的宿命。

所以我要的是**一份資料**，不是一次性的內容。

---

## 2. 主要索求：`research/results/evidence_registry.json`

一份彙總檔比改九個 JSON 好——前端只讀一個檔，你也只維護一個地方。

建議欄位（詞彙沿用你們既有的，不新造）：

```json
{
  "schema_version": 1,
  "generated_at": "...",
  "generated_by": "scripts/build_evidence_registry.py",
  "tier_vocabulary": {
    "development": "2015~2026。falsification / development evidence only，不是 OOS 確認（M22 Discovery 軌）",
    "backward": "2008~2014，未看過的一次性集合",
    "sealed": "已密封，尚未開封",
    "forward": "2026-08-14 之後累積中"
  },
  "summary": {
    "historical_development": "done",
    "backward_confirmation": "done",
    "forward_tracking": "accumulating",
    "sealed_datasets": 1
  },
  "hypotheses": [
    {
      "id": "H19",
      "title": "短期反轉",
      "factor": "…",
      "evidence_tier": "backward",
      "verdict": "passed",
      "period": ["…", "…"],
      "sample": {"n": 0, "unit": "episodes | months | trades"},
      "primary_metric": {"name": "…", "value": 0.0, "t_stat": 0.0},
      "source_report": "research/results/h19_reversal_backward.json",
      "decided_at": "2026-08-…",
      "display_policy": "summary_only | full | do_not_display"
    }
  ]
}
```

需要涵蓋的至少有：**H01、H04、H05、H06、H10、H11、H11b、H12、H13、H16、H17、H18、H19**
（審閱者列的清單少了幾個，請以你的登記簿為準）。

### 2.1 `verdict` 的取值請你定義

我看到你們用過至少這幾種語意：通過／否決／主要判準未通過／功效不足／
現象成立但不可部署／阻塞（H17 密封）。請給一個固定的 enum，
我在前端就照它上色，不自己歸類。

### 2.2 `display_policy` 是我最需要的欄位

**這是我不敢自己判斷的地方。**

你上一輪否決 MOM-1 的 NAV 曲線，理由是「從 holdout 的失效形態找模式即為污染，
比不得調參更早一步」。但你同時允許顯示 MOM-1 的**摘要指標**
（Sharpe 0.43、MDD -34.3%、7 個年度報酬）。

所以界線是「摘要可以、逐日序列不行」。**那 H19 呢？**

H19 是 backward 且**通過**。我想把它放上 Evidence Dashboard 當主要成果，
但我不確定可以顯示到什麼粒度：

- 只顯示「通過」＋判準名稱？
- 可以顯示主要估計量與 t 值？
- 可以顯示分層／逐年結果嗎？

**請逐列給 `display_policy`，我一律照它做。** 沒有這個欄位我就只顯示
`verdict` 三個字，不顯示任何數字——寧可少講也不要越線。

---

## 3. 次要索求：② 歷史績效三分頁需要的「作廢清單」

審閱者建議把「🔄 歷史績效」拆成 Forward / Historical Development / Archived-Invalid
三個分頁。第三個需要一份明確清單：**哪些數字已作廢、為什麼**。

我知道的有：

- 舊的 `+328%` 與 `0050 +745.5%`（頁面上已寫「已移除」）
- `mom1_f1_backward_holdout_run1_INVALID.json`（每日再平衡缺陷，F1 第一次執行作廢）
- `twse_security_master_2005_2007_staging_v2` 的 `INVALID_DO_NOT_USE.md`

請確認這份清單完整，或補上我漏掉的。格式簡單就好：
`{artifact, invalidated_at, reason, superseded_by}`。

---

## 4. ⑦ Cockpit 需要的三個欄位

首頁要改成「今天我需要知道什麼」。多數欄位我在本機算得出來
（NAV／現金／持倉數／待成交），但這三個需要你確認權威來源：

| 欄位 | 我想用的來源 | 請你確認 |
|---|---|---|
| **Market regime**（多頭／空頭） | 現行策略的市場濾網 | 有沒有已落地的欄位可讀，或該用哪個函式？ |
| **策略版本與凍結日** | `agent/strategy.py` 的 `STRATEGY_ERAS` | 凍結日是 2026-07-24 嗎？以哪份文件為準？ |
| **Forward 累積天數／期數** | `reports/forward_journal.json` | 直接讀 `observations` 長度與 `forward_start` 可以嗎？ |

---

## 5. 我這邊已完成、不需要你的部分

- **⑧** 更新時間文字不一致已修（`app.py` 說 3-5 分、註解說 5-10 分 → 統一 5-10）
- **⑨** README 漂移已修（頁數 7→16；出場規則整段依 `STRATEGY` 實際值重寫）
- **⑦ 的 sizing 基準問題已釐清**：使用者要「10 檔加起來 100%」，而
  `entry_share_count` 的 `nav/max_open` = 10% 槽位**已經是這個行為**；
  `main` 之所以算成 122% 是因為它還在用舊的 `suggest_shares`
  （`capital/pick_top_n` = 20% 上限，綁不住 12.5%）。
  **不需要改 `risk_per_trade` 或 `pick_top_n`**——後者同時是「每次買進檔數」，
  動它會改變選股數量，那是策略行為變更。
  實算 10 檔 = 94%，差的 6% 全來自整股進位（5274 一股 16,675 元，
  30,000 槽位只夠 1.79 股）。這是整股制的數學下限，不是設定問題。

---

## 6. 順帶回報一個與本索求無關的量測

Neon 目前 45 張表、on-disk 合計 **465 MB**：

```
technical_indicators  192 MB   ← 最大，且只存滾動窗
institutional_trading 136 MB
daily_prices          130 MB
```

使用者的方案多為免費層，465 MB 值得對一下儲存上限。
`technical_indicators` 本來就可由價量重算，是最有效的裁減對象，
但那要另外評估，不在本輪範圍。
