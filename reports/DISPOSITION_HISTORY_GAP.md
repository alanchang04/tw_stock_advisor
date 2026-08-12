# 資料缺口：TWSE 處置／注意事件歷史只回溯到 2014-12

> 日期：2026-08-12
> 狀態：**TWSE punish 2005～2014 已回補、已驗證；notice 明確不在 MOM-1 必要範圍**
> 影響：MOM-1 的 universe 規則 `SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6

---

## 1. 缺口

`data/research/disposition_events.parquet` 實測：

- 事件數 1,519、個股 516 檔。
- `announce_date` 範圍 2014-12-23 → 2026-07-24。
- **2015-01-01 以前只有 3 筆。**
- `market` 欄位全為 `TWSE`，上櫃 0 筆。
- 解除日（`end_date`）覆蓋 1,519 / 1,519，10 日聚合後獨立情境 42 個。

逐年事件數：

```
2014:3  2015:58  2016:42  2017:88  2018:109  2019:34  2020:226
2021:180  2022:94  2023:99  2024:162  2025:104  2026:320
```

## 2. 為什麼這會影響 MOM-1

§7.1.6 要求 MOM-1 的 universe 排除「非處置／停止交易／全額交割等無法按模型成交
的股票」。但 2008~2014 backward holdout 期間手上只有 3 筆處置紀錄，**該條規則在
holdout 期間形同失效**。

偏誤方向對動能策略特別不利：`data_pipeline/fetchers/disposition_fetcher.py` 自己
記載「處置常常是爆量急漲觸發的——那正是動能策略最愛選的形態，門檻擋不住」。
也就是說，漏掉的正是動能排名最前面那一批，而處置期間人工管制撮合＋預收款券會
使 30bp 滑價假設嚴重低估成本。

§2.1 的資料缺口表目前只列了「TPEX 處置／注意／下櫃：現有檔案不完整」，
**未涵蓋 TWSE 處置本身的歷史深度不足**。

## 3. 可補：官方端點確實回得出 2015 年以前的資料

2026-08-12 直接呼叫 `https://www.twse.com.tw/rwd/zh/announcement/punish`
（`startDate`／`endDate` 區間查詢）實測：

| 年份 | 回傳筆數 |
|---|---|
| 2008 | 27 |
| 2011 | 71 |
| 2013 | 81 |
| 2014 | 60 |

`disposition_fetcher.py` 註解寫「實測可回溯至 2015」低估了可得範圍。
依此推估 2005~2014 全期約可取得 500~600 筆處置事件。

## 4. 回補結果

2026-08-12 已由 Data Authority 執行 2005～2014 `punish` 年度回補：

| 年份 | 官方 raw rows |
|---|---:|
| 2005 | 28 |
| 2006 | 46 |
| 2007 | 79 |
| 2008 | 27 |
| 2009 | 110 |
| 2010 | 62 |
| 2011 | 77 |
| 2012 | 73 |
| 2013 | 82 |
| 2014 | 97 |

- 10 個年度 raw 檔，共 681 rows、37,738 bytes。
- raw collection SHA-256：
  `0B49730946A78D9F8709A7C9F9D0B90B68F22DF87BCAF650EE3B425BF871C9FA`。
- transfer manifest：
  `reports/twse_disposition_transfer_manifest_2005_2014_20260812.json`。
- manifest 逐檔驗證：missing 0、mismatched 0、`passed=true`。
- 只保留可解析期間的四位數普通股後，各年度合計 620 source rows；跨年更正／重覆
  依 `(stock_id, start_date)` 留最後一筆後為 604 個事件、194 個股票代號。
- 事件期間涵蓋 2004-12-29～2015-01-14；年度查詢以公告年度切分，因此邊界跨年是
  合法事件，不裁切處置起迄日。

## 5. 回補與 snapshot 工具

`scripts/backfill_twse_disposition_history.py`

- 只寫 `data/raw/twse/disposition/{punish,notice}/<year>/`，
  **不觸碰** `data/research/`（SPEC §0.2／§3.1 不覆蓋原則）。
- 已存在的 raw 不重抓，可離線重跑；`--status` 完全不發請求。
- 原子寫入；只凍結官方原始回應，不做正規化、不建 snapshot、不寫資料庫。

`scripts/build_twse_disposition_snapshot.py`

- 建置前先驗證 raw transfer manifest，任何缺檔或 SHA mismatch 都 fail closed。
- 正規化結果逐列保留 `source_year`、`raw_path`、`raw_sha256`。
- snapshot 不覆寫；品質報告分開記錄 raw、普通股 parser 過濾與跨年去重數量。

## 6. 跨機可重現性

writer 已固定 JSON 編碼及 gzip `mtime=0`。相同官方 payload 在不同機器會得到相同
位元組；但官方可能事後更正歷史回應，因此部署機仍應接收 Data Authority 封版檔並以
manifest 驗證，不應在部署時重新下載。

Data Authority 完成後：

```powershell
python scripts/build_data_transfer_manifest.py create `
  --root data/raw/twse/disposition `
  --output reports/twse_disposition_transfer_manifest_2005_2014_20260812.json
```

另一台以檔案傳輸取得，再 `verify`，`passed: true` 才可使用。
與 `corporate_actions` 那 168 檔同樣的協定。

## 7. 明確不在本輪範圍

- `notice` 注意事件不是 MOM-1 的必要排除條件，本輪不下載、不假裝已完成。
- TPEX 處置資料由 D5 官方 snapshot 提供，不與本 TWSE component 混成同一來源。
- 是否把注意股做成額外風險特徵，留待未來獨立規格，不得回頭用 holdout 績效決定。
