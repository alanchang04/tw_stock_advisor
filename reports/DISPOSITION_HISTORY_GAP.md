# 資料缺口：TWSE 處置／注意事件歷史只回溯到 2014-12

> 日期：2026-08-12
> 狀態：**已確認缺口、已確認可補、尚未執行回補**
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

## 4. 回補工具

`scripts/backfill_twse_disposition_history.py`

- 只寫 `data/raw/twse/disposition/{punish,notice}/<year>/`，
  **不觸碰** `data/research/`（SPEC §0.2／§3.1 不覆蓋原則）。
- 已存在的 raw 不重抓，可離線重跑；`--status` 完全不發請求。
- 原子寫入；只凍結官方原始回應，不做正規化、不建 snapshot、不寫資料庫。

## 5. ⚠️ 跨機執行限制

gzip header 含 mtime，**兩台機器各自回補會產生不同位元組，sha256 永遠對不上**，
manifest 驗證會直接失效。

**只能在一台機器執行**，完成後：

```powershell
python scripts/build_data_transfer_manifest.py create `
  --root data/raw/twse/disposition `
  --output reports/twse_disposition_transfer_manifest_<range>_<date>.json
```

另一台以檔案傳輸取得，再 `verify`，`passed: true` 才可使用。
與 `corporate_actions` 那 168 檔同樣的協定。

## 6. 尚未決定

- 由哪一台執行回補（來源主機目前擁有 `data/raw/twse/`）。
- 是否同時回補 `notice`（注意事件）。注意事件母體遠大於處置
  （2015~2026 為 28,861 筆），但門檻較鬆、經濟意義較弱。
- 正規化與 promotion 併入哪個 D 工作包。
