# 研究資料跨主機搬運

## 結論

另一台主機不需要重新向 TWSE 下載。Git 用來同步程式碼；雲端硬碟用來搬運官方 raw
cache。SQLite、parquet 與研究 snapshot 是可重建衍生物，可以選擇一起搬以節省時間。

## 權威資料

必帶：

- `data/raw/twse/mi_index/`：逐日官方 MI_INDEX JSON gzip。
- `data/raw/twse/security_master/`：官方公司清單、年報／異動文件及來源 manifest。
- `data/raw/twse/corporate_actions/`：2005–2014 除權息與 2011–2014 減資月報 JSON gzip。
- `reports/twse_raw_transfer_manifest_*.json`：每檔大小、SHA-256 與集合 SHA-256。

選帶：

- `data/raw/twse/mi_index.sqlite3`：正規化工作資料庫，可節省重建時間。
- `data/raw/twse/corporate_actions.sqlite3`：公司行動 staging 資料庫，可由 raw 重建。
- `data/research_versions/<snapshot_id>/`：已凍結 parquet、manifest 與品質報告。

不要依賴 `*-wal`／`*-shm` 檔作為唯一副本，也不要在下載程序仍執行時同步 SQLite。

## 來源主機

確認下載程序已停止後，在 repo root 建立 raw 搬運清單：

```powershell
.\.venv-repro\Scripts\python.exe scripts\build_data_transfer_manifest.py create `
  --root data/raw/twse/mi_index `
  --root data/raw/twse/security_master `
  --root data/raw/twse/corporate_actions `
  --output reports/twse_raw_transfer_manifest_2005_2014_d3_20260811.json
```

將上述三個目錄與 manifest 放進同一個雲端資料夾；可先壓成單一 archive，避免大量小檔
同步到一半時看似已完成。搬運期間不要修改來源 archive。

2026-08-11 的 D3 manifest 涵蓋 2,820 個檔案、338,804,487 bytes，集合 SHA-256 為
`798C9F5835E18083010AA0CB1778E721C84FEE661642B3ED1C1B616689453E44`。

## 目的主機

先 `git pull` 到相同程式版本，再把資料放回相同相對路徑，然後驗證：

```powershell
.\.venv-repro\Scripts\python.exe scripts\build_data_transfer_manifest.py verify `
  --manifest reports/twse_raw_transfer_manifest_2005_2014_d3_20260811.json
```

只有 `passed: true` 才能使用。若沒有搬 SQLite，可完全離線由 raw cache 重建；已有 raw 的
日期不會向 TWSE 發出請求：

```powershell
.\.venv-repro\Scripts\python.exe scripts\backfill_twse_momentum_history.py `
  --start 2005-01-01 --end 2014-12-31 --delay 0

.\.venv-repro\Scripts\python.exe scripts\backfill_twse_corporate_actions.py `
  --start 2005-01-01 --end 2014-12-31 --delay 0 --export
```

重建後再執行 `--status`，確認宣告列數、價格列數與 observation 列數一致，且成交量單位
仍為 `shares`。最後重建 versioned snapshot 並比對 content SHA-256。
