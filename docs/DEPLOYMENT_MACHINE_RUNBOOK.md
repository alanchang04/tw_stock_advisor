# 部署機作業手冊

> 建立 2026-08-16。
> 相關文件：`docs/DEPLOYMENT_MACHINE_DATA_RELEASE.md`（釋出包的取得與驗證）、
> `docs/DATA_TRANSFER_BETWEEN_MACHINES.md`（raw cache 搬運）。
> **本文件補的是那兩份沒有涵蓋的東西：日常該跑什麼、順序為何、
> 以及一個目前存在的靜默失效缺口。**

---

## 0. 先講結論：目前唯一真正缺的東西

**`data/research/*.parquet` 沒有任何排程在更新它。**

`schtasks` 上目前只有一個工作：

```
\TWStockForwardJournal    每月 5 號 09:00    Ready
```

而 forward journal 讀的就是 `data/research`。於是會發生這件事：

```
沒有人重建 parquet
        ↓
journal 每月準時跑、exit code 0
        ↓
印出「尚無 forward 觀測。這是正確的——forward 需要時間累積」
        ↓
看起來完全正常，但其實永遠不會有數字
```

**那句話現在是對的，在管線已經死掉時也長得一模一樣。**
2026-08-16 已在 `scripts/run_forward_journal.py` 加入新鮮度檢查，
超過 45 天會明確警告；但**警告不等於修好**，還是要把重建排進排程。

---

## 1. 部署機的三個角色

| 角色 | 跑什麼 | 頻率 |
|---|---|---|
| **每日生產** | `run_pipeline.py` | 每日收盤後 |
| **研究資料匯出** | `db_to_parquet.py` | 每月，**在 journal 之前** |
| **forward 監看** | `run_forward_journal.py` | 每月 |

Streamlit 網站本身**不在這台機器上**——它從 GitHub 部署程式碼，
經 `DATABASE_URL` 連 PostgreSQL。**釋出包不得上傳到 Streamlit Cloud**
（`usage_policy.blocked`）。

---

## 2. 每日：生產管線

```powershell
python run_pipeline.py --mode daily      # 單次
python run_pipeline.py --mode schedule   # 常駐（apscheduler，Asia/Taipei）
```

`--mode schedule` 內建兩個工作，**不需要另外設 Windows 排程**：

| 工作 | 時間 |
|---|---|
| `daily_auto` | 每天 `DAILY_FETCH_HOUR`:00（預設 **18:00**，環境變數可覆寫） |
| `weekly_industry` | 每週一 08:00 更新產業分類 |

這條管線做的是：抓價量與法人 → 技術指標 → 產業動能 →
每日推薦 → 市場情報與 Telegram 推播。**它寫的是資料庫，不是 parquet。**

---

## 3. 每月：把資料庫匯出成研究 parquet ← **目前缺這一步**

```powershell
python scripts/db_to_parquet.py --out data/research
```

**這一步必須排在 forward journal 之前。** 順序錯了，journal 讀到的
仍是舊資料，等於沒跑。

三個容易搞混的腳本，**只有第一個會產生新資料**：

| 腳本 | 做什麼 |
|---|---|
| `scripts/db_to_parquet.py` | **從 DB 匯出 parquet** ← 要的是這個 |
| `scripts/build_research_snapshot_v2.py` | 把現有 parquet **凍結**成版本化不可變快照（內容定址、SHA-256、品質報告） |
| `scripts/package_data_release.ps1` | 把凍結快照**打包**成可搬運的釋出 zip |

建議排程（每月 5 號，接在 journal 之前）：

```powershell
schtasks /create /tn "TWStockResearchExport" /tr ^
  "cmd /c cd /d C:\Users\User\Desktop\tw_stock_advisor && python scripts\db_to_parquet.py --out data\research" ^
  /sc monthly /d 5 /st 08:30
```

排 08:30、journal 排 09:00，中間留 30 分鐘。

---

## 4. 每月：forward journal

已排程，不用手動：

```
\TWStockForwardJournal    每月 5 號 09:00
→ scripts\forward_journal_monthly.cmd
→ 日誌 reports\forward_journal_run.log（UTF-8）
```

### 怎麼判斷它是否健康

執行後看兩件事，**順序不能顛倒**：

```
快照最後交易日 2026-07-31（落後 16 天）      ← 先看這個
forward months recorded: 0                   ← 再看這個
```

| 情況 | 意思 |
|---|---|
| 落後 ≤ 45 天、觀測 0 | **正常**。forward 從 2026-08-14 起算，還沒到時候 |
| **落後 > 45 天** | **管線斷了**。先修資料，不要把「還沒到時候」當解釋 |
| 出現「定義指紋不符」 | 有人改了凍結定義。依 M18 **不得續寫同一本 journal**，要開新本並在登記簿說明 |

### 第一筆觀測什麼時候會出現

**最快 2026 年 12 月左右。** forward 自 2026-08-14 起算，
第一個 forward 決策月是 2026 年 8 月底，而主要觀察窗是 60 個交易日
（約 3 個月）。**在那之前 journal 一直是 0 是正確的**——
這正是新鮮度警告存在的理由：它是這段空窗期裡唯一能分辨
「還沒到」與「已經壞了」的東西。

---

## 5. 部署機需要拿到的資料

### 5.1 目前的釋出：`tw_stock_data_2005_2014_r3`（2026-08-14）

`reports/data_releases/tw_stock_data_release_2005_2014_r3.json`

| readiness 旗標 | 值 |
|---|---|
| `cross_machine_byte_verification_ready` | ✅ |
| `pit_engine_correctness_work_ready` | ✅ |
| `twse_disposition_filter_component_ready` | ✅ |
| `twse_altered_trading_filter_component_ready` | ✅ |
| `backward_holdout_performance_ready` | ✅ **已開封，且僅此一次** |
| `all_market_strategy_promotion_ready` | ❌ |
| `streamlit_runtime_requires_this_bundle` | ❌ |

**`usage_policy.blocked`——這三件事不准做：**

1. 以 2008–2014 的報酬調參數
2. 宣稱全市場可上線
3. 把原始研究資料上傳到 Streamlit Cloud

> **holdout 已開封，不得重跑。** `tests/test_research_status.py` 會在
> 換釋出或旗標再次變動時變紅，要求人工確認。

### 5.2 `data_release_bundles/` 目前只有 r1 的 zip

r2／r3 的 zip **尚未打包**。要給部署機 r3 的話，在資料來源機執行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\package_data_release.ps1
```

搬 `.zip` 與 `.zip.sha256` 兩個檔，**兩個都要**。

### 5.3 部署機端的驗證（先驗再解壓）

```powershell
git fetch origin
git switch agent/swing-margin-research
git pull --ff-only origin agent/swing-margin-research

$expected = (Get-Content .\<release>.zip.sha256).Split()[0]
$actual = (Get-FileHash .\<release>.zip -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw "archive SHA-256 mismatch" }

tar.exe -x -f .\<release>.zip -C .
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_data_release.ps1
```

### 5.4 需不需要 raw cache？

**日常運作不需要。** raw cache（`data/raw/twse/...`，約 2,820 檔、338 MB）
只有在要從頭重建 2005–2014 歷史時才需要，搬運方式見
`docs/DATA_TRANSFER_BETWEEN_MACHINES.md`。

---

## 6. 檢查清單

每月 5 號之後看一次 `reports/forward_journal_run.log`：

- [ ] 有這次的執行紀錄（沒有 → 排程沒觸發，查 `schtasks /query /tn TWStockForwardJournal`）
- [ ] `exit code: 0`
- [ ] **快照落後天數 ≤ 45**
- [ ] 沒有「定義指紋不符」
- [ ] `forward months recorded` 的數字**只增不減**（append-only；變少代表有人動了 journal）

換機器或換釋出時另外做：

- [ ] `.zip.sha256` 驗過才解壓
- [ ] `verify_data_release.ps1` 通過
- [ ] `python -m pytest tests/test_research_status.py` 綠

---

## 7. 一句話的現況

**生產管線是好的；forward 監看的線路是好的；但餵給監看的資料沒有人在更新。**
補上第 3 節那一個排程，整條鏈就閉合了。
