# 機器角色與日常作業

> 建立 2026-08-16（原名 `DEPLOYMENT_MACHINE_RUNBOOK.md`，**已改名並修正**）。
> 相關文件：`docs/DEPLOYMENT_MACHINE_DATA_RELEASE.md`（釋出包取得與驗證）、
> `docs/DATA_TRANSFER_BETWEEN_MACHINES.md`（raw cache 搬運）。

---

## 0. 「部署機」這個詞已經壞掉了，不要再用

它同時被用來指三個不同的東西，而其中一個**根本不存在**：

| 曾經被叫做「部署機」 | 實際上 |
|---|---|
| 「維持 Streamlit 的那台」 | **不存在。** Streamlit Cloud 從 GitHub 拉程式碼、用 secret 直連 Neon，沒有任何本機在維持它 |
| 「每天餵資料的那台」 | 存在，而且是**真正撐住網站的那台**。但 repo 從來沒有為它命名 |
| repo 文件裡寫的 deployment machine | **接收研究鏡像釋出包的那台**——一個唯讀的研究副本，與網站營運無關 |

難怪那台機器不認為自己是部署機：**按 repo 的定義，它可能真的不是。**

**以下改用能力定義，不用名字。角色由「能不能通過測試」決定，不由稱呼決定。**

---

## 1. 三個角色與它們的判定測試

### 角色 A — 生產餵食機（Production Feeder）

**判定測試**：不做任何事，隔天再查一次，數字自己前進：

```sql
SELECT MAX(trade_date) FROM daily_prices;
```

**通過 = 你就是這台。** 它跑 `run_pipeline.py`，把價量、法人、技術指標、
每日推薦寫進 Neon，並推 Telegram。

> **這是唯一會影響網站的角色。** 它停了，Streamlit 上的數字就不動。
> 2026-08-16 實測：`daily_prices` 最新 2026-08-14（8/15–16 為週末），
> **餵食正常運作中**。

### 角色 B — 研究機（Research Machine）

**判定測試**：

```powershell
Test-Path data\raw\twse\mi_index      # 幾千個官方 JSON gzip
Test-Path data_release_bundles        # 產出釋出 zip
Test-Path data\research_versions      # 凍結快照
```

三個都在 = 你就是這台。它持有原始檔案庫、產生資料釋出、跑 forward journal、
做歷史回補。**它不需要每天開機。**

> 2026-08-16 實測：本 checkout 三項齊全（raw 2,652 檔、
> research_versions 270 檔），且 `\TWStockForwardJournal` 排在這台。
> **這台是研究機。**

### 角色 C — 研究鏡像（Research Mirror）

**判定測試**：有解壓過的釋出包，但**沒有** `data/raw/twse/`。

唯讀副本，用來重現研究結果。就是舊文件裡講的那個 deployment machine。
**它不參與營運，也不需要參與。**

### 角色 D — 停用的舊研發機（Legacy R&D）

**判定測試**：有 `data/raw/twse/`（因此不是角色 C），但**缺** `data_release_bundles`
且 `data/research_versions` 只有零星幾個快照，也沒有 forward journal 排程。

2026-08-16 由第一台研發機自己依測試判定出來
（`reports/HANDOFF_2026-08-16_LEGACY_RD_MACHINE.md`）。
它持有一份**舊的**研究資料，後續資料沒有拿到。

**明文禁止在角色 D 執行：**

- `scripts/historical_backfill_local.py`
- `scripts/db_to_parquet.py`
- `scripts/build_research_snapshot_v2.py`
- `scripts/run_forward_journal.py`（寫入正式 journal；寫 scratch 供診斷可以）

理由：任何一個都會**產生第二份分岔的研究狀態**，而沒有人知道該信哪一份。
角色 D 可以做的是前端、程式碼修正、文件、以及唯讀診斷。

> 這條規則是因為那台機器被要求「建立 forward 快照」，
> 它自檢後拒絕執行並提出質疑——**而那次質疑直接救回了研究資料集**
> （見 §3 的撤回）。這個判斷是對的，寫進文件讓下一個 agent 不必重來一次。

### 不是角色 — Streamlit Cloud

託管服務。從 GitHub 部署，經 secret 連 Neon。
**沒有本機需要為它做任何事**，除了 push 程式碼。
**釋出包不得上傳到 Streamlit Cloud**（`usage_policy.blocked`）。

---

## 2. 誰負責什麼

| 工作 | 跑在 | 頻率 | 現況 |
|---|---|---|---|
| `run_pipeline.py` | **角色 A** | 每日 | ✅ 正常 |
| `historical_backfill_local.py` | **角色 B** | 每月，人工 | ❌ **沒做 ← 唯一缺口** |
| `run_forward_journal.py` | **角色 B** | 每月 | ✅ 已排程 |

**角色 B 不需要 Neon 也不需要跑 `run_pipeline.py`。** 它有自己的
本機 SQLite 歷史庫，資料直接從 TWSE 回補。兩條資料線是**刻意分開的**：

```
角色 A：TWSE → Neon（14 個月營運視窗）→ Streamlit 網站
角色 B：TWSE → 本機 SQLite（11.5 年）→ parquet → forward journal
```

**不要把兩條線接起來。** 2026-07-17 有過事故記錄：10 年規模的回補寫 Neon
把免費配額燒穿，連每日 pipeline 都連不上。

---

## 3. 唯一的缺口：研究 parquet 沒有人更新

`data/research/*.parquet` 是從 Neon 匯出的衍生物，而 forward journal 讀的就是它。
目前角色 B 上只有一個排程：

```
\TWStockForwardJournal    每月 5 號 09:00
```

於是：

```
沒有人重新匯出 parquet
        ↓
journal 每月準時跑、exit code 0
        ↓
印出「尚無 forward 觀測。這是正確的——forward 需要時間累積」
        ↓
看起來完全正常，但永遠不會有數字
```

**那句話現在是對的，在管線已經死掉時也長得一模一樣。**

2026-08-16 已在 `scripts/run_forward_journal.py` 加入新鮮度檢查，
超過 45 天會明確警告。**但警告不等於修好。**

### ⚠️ 撤回：本文件 2026-08-16 稍早版本給了一個會摧毀研究資料的指令

原本寫的是排程 `scripts/db_to_parquet.py --out data/research`。**那是錯的，
而且後果不可逆。** 感謝另一台機器在交接文件 §5 問題 1 提出質疑後查出來。

`db_to_parquet.py` 讀的是 **Neon**，而 Neon 只保留約 14 個月的營運視窗：

| 年 | Neon `daily_prices` 交易日 |
|---|---:|
| 2015 | 13（殘留碎片） |
| **2016–2024** | **0** |
| 2025 | 142 |
| 2026 | 148 |

而 `data/research/prices.parquet` 是 **11.5 年、4,840,274 列、2,140 檔**。

**跑下去會把 11.5 年的研究母體覆蓋成 14 個月**，
H11／H11b／H12／H13／H16／MOM-1 的資料基礎全部消失。

> 附帶澄清另一台機器的算法：它由 571,092 列 ÷ 約 2,830 個交易日推得
> 「每日約 200 檔」，但 Neon 其實只有 **303 個交易日**，
> 每日約 1,885 檔。**寬度沒問題，問題在歷史深度。**
> 結論方向正確，嚴重性其實更高。

### 正確的更新方式

`data/research` 的來源是**本機 SQLite** `data/research/research.db`（724 MB），
由 `historical_backfill_local.py` 直接從 TWSE 回補——該腳本開宗明義寫著
**「寫本機 SQLite，不碰 Neon」**（2026-07-17 曾把 Neon 免費配額燒穿）。

```powershell
python scripts\historical_backfill_local.py --start-year 2015   # 回補 + 匯出
python scripts\historical_backfill_local.py --start-year 2015 --export-only   # 只重匯出
```

可安全中斷、重跑自動接續（`backfill_progress` 表記錄進度）。

**這支會從 TWSE 下載資料，因此要由人執行**，不由 agent 代跑
（`CLAUDE.md`：Do not download or modify raw data）。

### 四個很容易搞混的腳本

| 腳本 | 做什麼 | 讀哪裡 |
|---|---|---|
| **`historical_backfill_local.py`** | **回補歷史 + 匯出 parquet** ← 要的是這個 | TWSE → 本機 SQLite |
| `db_to_parquet.py` | 匯出 parquet | **Neon（只有 14 個月）** ☠️ |
| `build_research_snapshot_v2.py` | 把現有 parquet **凍結**成不可變快照 | 本機 parquet |
| `package_data_release.ps1` | 把凍結快照**打包**成釋出 zip | 凍結快照 |

---

## 4. 角色 A 的日常

```powershell
python run_pipeline.py --mode daily      # 單次
python run_pipeline.py --mode schedule   # 常駐（apscheduler，Asia/Taipei）
```

`--mode schedule` 內建兩個工作，**不需要另外設 Windows 排程**：

| 工作 | 時間 |
|---|---|
| `daily_auto` | 每天 `DAILY_FETCH_HOUR`:00（預設 **18:00**，環境變數可覆寫） |
| `weekly_industry` | 每週一 08:00 |

它是**前景阻塞行程**——關掉終端機就停了。要長期運作應包成服務或工作排程。

---

## 5. 角色 B：forward journal 怎麼判讀

執行後看兩件事，**順序不能顛倒**：

```
快照最後交易日 2026-07-31（落後 16 天）      ← 先看這個
forward months recorded: 0                   ← 再看這個
```

| 情況 | 意思 |
|---|---|
| 落後 ≤ 45 天、觀測 0 | **正常**。forward 自 2026-08-14 起算，還沒到時候 |
| **落後 > 45 天** | **管線斷了**。先修資料，不要把「還沒到時候」當解釋 |
| 「定義指紋不符」 | 有人改了凍結定義。依 M18 **不得續寫同一本 journal** |

**第一筆觀測最快 2026 年 12 月左右**——forward 起算 2026-08-14，
第一個 forward 決策月是 8 月底，主要觀察窗 60 個交易日（約 3 個月）。
在那之前一直是 0 是正確的，這正是新鮮度警告存在的理由：
它是這段空窗期裡**唯一**能分辨「還沒到」與「已經壞了」的東西。

---

## 6. 資料釋出：`tw_stock_data_2005_2014_r3`（2026-08-14）

`reports/data_releases/tw_stock_data_release_2005_2014_r3.json`

| readiness | 值 |
|---|---|
| `cross_machine_byte_verification_ready` | ✅ |
| `pit_engine_correctness_work_ready` | ✅ |
| `twse_disposition_filter_component_ready` | ✅ |
| `twse_altered_trading_filter_component_ready` | ✅ |
| `backward_holdout_performance_ready` | ✅ **已開封，且僅此一次** |
| `all_market_strategy_promotion_ready` | ❌ |
| `streamlit_runtime_requires_this_bundle` | ❌ |

**`usage_policy.blocked`——三件不准做的事：**

1. 以 2008–2014 的報酬調參數
2. 宣稱全市場可上線
3. 把原始研究資料上傳到 Streamlit Cloud

> holdout 已開封，**不得重跑**。`tests/test_research_status.py` 會在
> 換釋出或旗標再次變動時變紅，要求人工確認。

**`data_release_bundles/` 目前只有 r1 的 zip；r2／r3 尚未打包。**
要給角色 C 的話，在角色 B 執行 `scripts\package_data_release.ps1`，
搬 `.zip` 與 `.zip.sha256`**兩個都要**，收到後先驗 SHA-256 再解壓，
再跑 `scripts\verify_data_release.ps1`。詳見
`docs/DEPLOYMENT_MACHINE_DATA_RELEASE.md`。

raw cache（約 2,820 檔、338 MB）**日常不需要**，只有要從頭重建
2005–2014 歷史時才需要。

---

## 7. 檢查清單

**角色 A**，每週看一次：

- [ ] `SELECT MAX(trade_date) FROM daily_prices` 跟得上最近的交易日
- [ ] `run_pipeline.py --mode schedule` 的行程還活著（前景行程，關終端機就停）

**角色 B**，每月 5 號之後看 `reports/forward_journal_run.log`：

- [ ] 有這次的執行紀錄（沒有 → 查 `schtasks /query /tn TWStockForwardJournal`）
- [ ] `exit code: 0`
- [ ] **快照落後天數 ≤ 45**
- [ ] 沒有「定義指紋不符」
- [ ] `forward months recorded` **只增不減**（append-only；變少代表有人動過 journal）

---

## 8. 一句話的現況

**生產餵食正常；forward 監看的線路正常；但餵給監看的研究 parquet
沒有人在更新。** 缺口在角色 B，補上第 3 節那一個排程就閉合了。
