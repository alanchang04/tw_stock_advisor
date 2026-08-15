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

### 不是角色 — Streamlit Cloud

託管服務。從 GitHub 部署，經 secret 連 Neon。
**沒有本機需要為它做任何事**，除了 push 程式碼。
**釋出包不得上傳到 Streamlit Cloud**（`usage_policy.blocked`）。

---

## 2. 誰負責什麼

| 工作 | 跑在 | 頻率 | 現況 |
|---|---|---|---|
| `run_pipeline.py` | **角色 A** | 每日 | ✅ 正常 |
| `db_to_parquet.py --out data/research` | **角色 B** | 每月 | ❌ **沒有排程 ← 唯一缺口** |
| `run_forward_journal.py` | **角色 B** | 每月 | ✅ 已排程 |

**角色 B 需要能連 Neon**（`db_to_parquet` 是從 DB 匯出），
但**不需要**跑 `run_pipeline.py`——那是角色 A 的事，兩者不要重複跑。

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

### 補上這一條（在角色 B 上，不是角色 A）

```powershell
schtasks /create /tn "TWStockResearchExport" /tr ^
  "cmd /c cd /d C:\Users\User\Desktop\tw_stock_advisor && python scripts\db_to_parquet.py --out data\research" ^
  /sc monthly /d 5 /st 08:30
```

排 08:30、journal 09:00，中間留 30 分鐘。**順序顛倒等於沒跑。**

### 三個很容易搞混的腳本，只有第一個產生新資料

| 腳本 | 做什麼 |
|---|---|
| `scripts/db_to_parquet.py` | **從 DB 匯出 parquet** ← 要的是這個 |
| `scripts/build_research_snapshot_v2.py` | 把現有 parquet **凍結**成版本化不可變快照 |
| `scripts/package_data_release.ps1` | 把凍結快照**打包**成釋出 zip |

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
