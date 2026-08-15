# 交接：舊研發機 → 研究機

> 日期：2026-08-16
> 發起方：**第一台研發機**（`C:\Users\alanchang\Desktop\taiwan_stock_advisor`）
> 對象：**角色 B 研究機**（`docs/MACHINE_ROLES_AND_OPERATIONS.md` §1 定義）
> 分支 `agent/swing-margin-research`，HEAD `44c2ad0`，工作區乾淨
> 資料釋出：**未使用**。本次全部診斷皆為唯讀，未讀取任何釋出包
> **holdout 確認：本次未計算、未查看任何 holdout 報酬指標。**（細節見 §6）

---

## 0. 這份文件要什麼

我（舊研發機）被要求「建立 forward 快照」。照 `MACHINE_ROLES_AND_OPERATIONS.md`
的能力測試自檢後，**我認為這件事不該由我做**，理由在 §2。

這份文件列出：我查到的事實、我認為該做的三件事、以及**五個需要你回答的問題**。
回答後我才動手，或由你動手。

---

## 1. 我是誰：能力測試結果

依 §1 的判定測試，在本機實測：

| 測試 | 本機 | 你（文件 §1 實測值） |
|---|---|---|
| `data/raw/twse/mi_index` | ✅ 2,608 檔 | ✅ 2,652 檔 |
| `data_release_bundles` | ❌ **不存在** | ✅ |
| `data/research_versions` | ⚠️ **9 檔／2 個快照** | ✅ 270 檔 |
| `\TWStockForwardJournal` 排程 | ❌ **不在本機** | ✅ |

`data/raw/twse/` 底下只有 `mi_index` 與 `security_master` 兩個目錄。
**沒有** `corporate_actions`、`disposition`、`altered_trading`、
`market_structure`、`tpex`。

**判定：本機不是角色 B，也不是角色 C**（角色 C 的定義是「有解壓過的釋出包、
沒有 `data/raw/twse/`」，本機剛好相反）。本機是使用者所稱的
「第一台研發機，後續資料沒有拿到」，現行文件的三個角色都涵蓋不到它。

---

## 2. 我為什麼不做 forward 快照

不是做不到，是**做了會製造第二份分岔的研究狀態**——那正是機器角色文件要防止的事。

本機 `data/research/prices.parquet` 為 **4,840,274 列、2015-01-05 → 2026-07-31**，
來源是本機 `data/research/research.db`（SQLite，819 MB，
`daily_prices` 4,820,692 列、2,137 檔、至 2026-07-17）。
這是一份**舊的**研究資料；你那邊的 `data/research` 是什麼狀態我看不到。

我若在此匯出或凍結，結果是多一份沒人知道該信哪個的快照。
`db_to_parquet` 與 `run_forward_journal` 都是角色 B 的工作（文件 §2 表格已明列）。

---

## 3. 我對自己先前兩個錯誤判斷的更正

寫在這裡是因為**我先前把這兩點講給使用者聽過**，若你從對話上下文接手，
請以本節為準。

### 3.1 「Neon 覆蓋率只有 11% 是缺陷」——**錯**

Neon 只保留營運所需尺寸是**刻意的設計**（Streamlit 只需要那麼多）。
我當成資料被截斷，是錯的。

實測數字仍附上供參：

| 表 | 列數 | 期間 |
|---|---:|---|
| `daily_prices` | 571,092 | 2015-01-05 → 2026-08-14 |
| `institutional_trading` | 533,744 | 2015-01-05 → 2026-08-14 |
| `technical_indicators` | 514,829 | **2025-07-07** → 2026-08-14 |

十張表 on-disk 合計約 465 MB（`pg_total_relation_size`，含索引與 TOAST）。

### 3.2 「指紋不符會讓每月排程硬失敗」——**誇大了**

寫入 `reports/forward_journal.json` 的是你那台，你自己重算應相符，
**每月排程不會因此失敗**。這個問題只在第二台機器驗證時才浮現。
嚴重度從「阻塞」降為「跨機不可攜」。但仍是真的問題，見 §4.1。

---

## 4. 三件我認為該做的事

### 4.1 修 `definition_fingerprint()` 的跨機不可攜性（程式碼，小改動）

**實測**（本機，HEAD `44c2ad0`，工作區乾淨）：

```
journal 記錄  : d47fb779da382e09245ddc4bd3a7745d32c26cf1b4d5695d9d4ed5304141fdbd
本機原樣(CRLF): 5a261418f06ba1490a2f44211ebc84fb6a39bc7b50432be10a40d61f0bece879
LF 正規化後   : 7ac418bdda280bb4e4124b86a46955f9c8dbe36595122056ab9d51a83cb19799
```

**三者互不相同，且已排除定義漂移**：

- 四個 `DEFINITION_FILES` 自 journal 建立的 commit `c030d06` 以來全未改動
  （`research/momentum.py` 停在 `c39c0a0`、`sequential.py` 停在 `c030d06`、
  `run_h12_*` 停在 `9322522`、`run_h16_*` 停在 `5ea480a`，皆早於或等於 `c030d06`）。
- 混入指紋的五個常數也未變（`git diff c030d06 HEAD -- scripts/run_forward_journal.py`
  只有新增新鮮度檢查，沒有動 `ENTRY_TOP_FRAC`／`FORMATION_SKIP`／`FORMATION_LOOKBACK`）。
- 本機 `core.autocrlf=true`、無 `.gitattributes`，四個檔案在磁碟上是 100% CRLF。

`definition_fingerprint()` 直接雜湊檔案原始位元組，因此換行符設定不同的機器
必然算出不同指紋。**但 LF 正規化後仍不符**，代表寫入時那台的位元組
與 HEAD 的 committed 內容也不一致（最可能是當時有未提交的本機修改）。

**為什麼值得修**：誤觸 M18 的代價是「放棄整本 journal、另開新本」，
而第一筆 forward 觀測要等到 2026 年 12 月左右。為了一個換行符差異丟掉
三個月以上的累積不划算。建議雜湊前先 `data.replace(b"\r\n", b"\n")`，
並把 `.gitattributes` 對這四個檔案釘成 LF。

**我不會自己改**——這會改變凍結定義的語意，屬於研究紀律範圍，見 §5 問題 2。

### 4.2 處理 `main` 落後（唯一影響使用者看得到什麼的事）

`origin/main` 停在 `3391eca`（2026-07-27），落後
`origin/agent/swing-margin-research` **84 個 commit**。

Streamlit Cloud 從 GitHub 部署，所以**線上跑的是 `main`**。後果：

- `e3b58a4` 的「📈 已驗證回測曲線（凍結快照）」線上看不到
- `f386960` 的「🔬 研究進度」頁線上看不到

使用者實際反映過「我沒看到這個」，原因就是這個。

### 4.3 把「本機這種機器」寫進角色文件

現行角色 A／B／C 涵蓋不到本機。建議補一個角色（暫稱**角色 D：停用的舊研發機**），
並明文寫死：**不得執行 `db_to_parquet.py`、`run_forward_journal.py`、
`build_research_snapshot_v2.py`**。

理由：沒有這條，下一個在這台開工的 agent 會重複我這次的過程——
花時間查證後才發現不該做。

---

## 5. 需要你回答的五個問題

### 問題 1：forward 與 backward 的宇宙是否可比？（我認為最重要）

`db_to_parquet.py --out data/research` 會把 `data/research` 覆蓋成 Neon 匯出。
Neon `daily_prices` 571,092 列 ÷ 約 2,830 個交易日 ≈ **每日約 200 檔**。

本機那份舊研究資料是 4,840,274 列 ≈ **每日約 1,710 檔**。

**請在你那台實測並回覆**：

```powershell
python -c "import pandas as pd; d=pd.read_parquet('data/research/prices.parquet',columns=['stock_id','trade_date']); print(len(d), d['stock_id'].nunique(), d['trade_date'].min(), d['trade_date'].max())"
```

- 若 H16／H11 的**歷史**研究本來就是在 Neon 匯出（約 200 檔）上跑的
  → 前後一致，本問題作廢。
- 若歷史研究用的是較寬的宇宙，而 forward 之後改吃 Neon 匯出
  → **forward 與 backward 不可比**，且會在每月排程上線後才悄悄發生。

### 問題 2：指紋要重新基準化，還是依 M18 開新本？

定義**沒有**實際改變（§4.1 已證），但記錄的指紋無法從 committed 程式碼重算。

- (a) 重新產生指紋基準、續用同一本 journal，並在登記簿註明原因
- (b) 依 M18 字面開新本

我傾向 (a)，因為 M18 的立意是防「改了模型還續用 forward 資料」，
而這裡模型沒改。但這是研究紀律判斷，該由你決定。

### 問題 3：`main` 要不要合併？誰來合？

84 個 commit 裡有大量研究資產與 `.gitignore` 改動。要全部合，還是只挑
前端相關的（`app.py`、`agent/backtest_curve.py`、`agent/research_status.py`、
`reports/swing_backtest_curve/`）？

我可以做，但這會動到線上，需要明確授權。

### 問題 4：本機要不要拿 r3 釋出包？

本機缺 `data_release_bundles`，`data/raw/twse/` 也只有兩個目錄。

- 若本機之後仍要做研究 → 需要 r3 的 zip（`package_data_release.ps1` 在你那台跑）
- 若本機只做前端與程式碼 → **不需要**，我維持現狀即可

### 問題 5：本機接下來的定位？

我目前能做且不需要新資料的工作：前端、程式碼修正、文件。
需要新資料的（策略研究、forward、快照）我一律不碰。

這個分界你同意嗎？還是你希望本機補齊資料後也參與研究？

---

## 6. 本次工作紀錄（可稽核）

**執行過的指令**（全部唯讀或寫入 scratch）：

| 指令 | 結果 |
|---|---|
| `run_forward_journal.py --journal <scratch>` | 快照最後交易日 2026-07-31（落後 16 天）、`forward months recorded: 0`、資料最後決策月 2026-04-30 |
| `pg_total_relation_size` 中繼查詢 | 十表 on-disk 約 465 MB |
| `MIN/MAX/COUNT` on 3 表 | 見 §3.1 |
| SQLite `research.db` 計數 | 見 §2 |
| 指紋三種算法比對 | 見 §4.1 |
| `pytest tests/test_research_status.py` | 9 passed（較早的 session） |

**未執行**：`db_to_parquet.py`、`build_research_snapshot_v2.py`、
`package_data_release.ps1`、真正的 `run_forward_journal.py`（未寫入
`reports/forward_journal.json`）。

**未修改任何檔案**：`data/`、`reports/forward_journal.json` 皆未變動。

**holdout 確認**：本次**未計算、未查看**任何 holdout 報酬、Sharpe、CAGR、
回撤或參數組合。

兩點需揭露以求完整：

1. 我讀到 commit 標題 `c420f77 research: MOM-1 F1 backward holdout 開封結果 — 否決`，
   知道結論是否決，但**未查看任何數字**。
2. 較早的 session 中，我在 Streamlit「🔄 歷史績效」頁看到**現行策略**已驗證回測的
   數字（勝率 35.4%、前 4 筆佔淨利 67.2%、MDD -42.72%）。那是現行策略的凍結回測，
   **不是 MOM-1 的 holdout**，且該頁本就對使用者開放。

**本機此前推送的 commit**（供你追溯）：

| commit | 內容 |
|---|---|
| `07d0dfe` | 處置歷史缺口 + REV-1 規格草案 + 動能正交性實測 |
| `6a6e16b` | Codex 額度用盡後的治理文件更新 |
| `f386960` | Streamlit「🔬 研究進度」頁 |

---

## 7. 我在等什麼

問題 1～5 有答案之前，本機**不執行任何會產生或改變研究資料的動作**。
可以先做的是 §4.2（`main`，待問題 3）與 §4.3（角色文件，低風險）。
