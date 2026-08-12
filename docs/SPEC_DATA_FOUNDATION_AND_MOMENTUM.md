# 規格：研究資料補強與 MOM-1 中期動能策略

> 版本：v1.0
> 日期：2026-08-11
> 狀態：**規格已凍結，尚未開始下載新資料或回測 MOM-1**
> 範圍：研究資料版本化、歷史資料補充、現行策略最終診斷、下一套獨立動能策略
> 非目標：本文件不修改現行 `STRATEGY`、不部署新策略、不用 2015~2026 再挑參數

---

## 0. 決策摘要

依下列順序執行，前一階段未通過不得進入下一階段：

1. **凍結現況**：保留目前可重現 snapshot、程式版本與正式回測數字。
2. **建立版本化資料管線**：新資料不得直接覆蓋 `data/research`。
3. **先補 TWSE 價量／公司行動／歷史 universe**：目標從 2005 開始，讓 2008~2014
   成為尚未看過的一次性 backward holdout。
4. **完成資料品質閘門與重現測試**：未通過前禁止看 MOM-1 績效。
5. **實作 MOM-1A／MOM-1B**：兩個事前固定版本，總試驗次數固定為 2。
6. **只開封一次 2008~2014 backward holdout**：結果不論好壞都寫入實驗登記簿。
7. **通過才進 forward paper trading**：2026-08-12 之後才是最終乾淨證據。
8. **再決定是否研究 MOM-2 或另一類策略**：不得在 MOM-1 失敗後原地微調救績效。

核心決策：

- 現行法人／營收趨勢策略繼續凍結，暫不再調參。
- 現行策略跨年代的正式比較優先採 **TWSE-only**；全市場一致比較只能從 2018 開始。
- 下一套策略採獨立的 **中期價格動能**，不把 `w_momentum` 直接加回現行排名器。
- MOM-1 第一版只使用價量、歷史 universe、公司行動與產業資料；不使用法人與月營收，
  以降低和現行策略的邏輯重疊。

---

## 1. 已知事實與問題定義

### 1.1 現行策略最終診斷基準

以下數字使用 2026-08-11 還原的同一份 snapshot、30 萬元資本、處置股排除、目前成本與
整數股 sizing：

| 2015-01~2026-07 | 全市場現行策略 | TWSE-only 現行策略 | 0050 含息 |
|---|---:|---:|---:|
| 總報酬 | +671.6% | +659.5% | +767.6% |
| Sharpe | 0.919 | 1.002 | 1.041 |
| 最大回撤 | -42.7% | -33.4% | -34.0% |
| Calmar | 0.452 | 0.574 | 0.605 |
| 2015~2024 累積 | +138.2% | +251.8% | +302.5% |
| 2025~2026 累積 | +223.9% | +115.9% | 約 +115.6% |

判讀：

- 全市場 2025~2026 的超額在 TWSE-only 後幾乎消失。
- 全市場版本的 2025 入場交易貢獻約 80.7% 全期淨利。
- 最大四筆（6274、2383、6223、2368）合計約占全期淨利 67.2%，都在 2025 年 5~6 月
  進場並持有 261~283 個交易日。
- 6274、6223 為 TPEX；早期 TPEX 法人資料缺漏會讓跨年代比較失真。
- 目前沒有證據顯示上述集中是股／張 1000 倍錯誤；巨額損益同時反映在每股報酬率。

### 1.2 動能不是單純的負因子

目前 `mom60` 是 `close[t] / close[t-60] - 1`。舊結論用它預測未來約 5~20 日，因此看到
短期 IC 偏負；這不能外推成「動能交易無效」。

2026-08-11 固定定義診斷，TWSE 普通股結果：

| 因子 | 未來 20 日 IC | 未來 60 日 IC | 未來 120 日 IC |
|---|---:|---:|---:|
| `mom60` | -0.0029 | +0.0247 | +0.0378 |
| `mom60_skip5` | +0.0026 | +0.0275 | +0.0384 |
| `mom120_skip20` | +0.0110 | +0.0294 | **+0.0479** |
| `mom252_skip20` | +0.0145 | +0.0138 | -0.0018 |

補充判讀：

- `mom60` 最高十分位相對最低十分位，未來 20 日平均多約 1.23pp；整體 IC 接近零，
  代表關係可能是非線性，不能用「全市場每一名都線性加分」處理。
- 最近一個月含在形成期時容易混入短期反轉；跳過最近 5~20 日後方向改善。
- 動能訊號需要與 60~120 日持有期對齊，不能用中期訊號搭配短線出場後再宣告無效。
- 2025~2026 是異常有利的動能 regime；`mom60` 對未來 60 日的 TWSE IC 約 +0.1009，
  2021~2022 則約 -0.0076。

### 1.3 目前因子報告不能直接作部署證據

既有 `factor_lab` 仍有下列限制，MOM-1 研究必須修正：

1. 因子在收盤後才完整可知，舊報告卻從同日收盤計 forward return；正式績效必須改為
   **T+1 可成交價格**。
2. 20／60／120 日 forward return 大量重疊，將每日 IC 當獨立樣本計 t-stat 會高估顯著性；
   必須使用 Newey-West、非重疊抽樣或 block bootstrap。
3. `stock_universe_history.parquet` 目前只有 2026-07-31 一個 snapshot，不能證明歷史時點
   的成分資格。
4. 因子 IC 不含完整交易成本、漲跌停無法成交、容量與換手率。
5. 全市場 IC 不等於通過流動性／產業／風控後仍有組合 edge。

---

## 2. 資料來源與不可跨越的限制

### 2.1 已實測的歷史可用起點

| 資料 | 已知最早可用 | 決策 |
|---|---|---|
| TWSE 日價量 | 2005 以前後 | MOM-1 第一階段主資料 |
| TPEX 日價量 | 約 2008 | 第二階段再納入 |
| TWSE 法人 T86 | 2012-05 左右 | 現行策略可用 |
| TPEX 法人官方資料 | 2018-01 左右 | 2018 前不可假設為 0 |
| 月營收 | 約 2010 | MOM-1 不使用 |
| 現有歷史 universe | 僅 2026-07-31 | 必須補強 |
| TPEX 處置／注意／下櫃 | 現有檔案不完整 | 全市場部署前必補 |

### 2.2 資料來源政策

1. 優先使用 TWSE、TPEX、MOPS 等官方來源。
2. 同一欄位跨期間不得拼接兩個不一致來源。
3. 替代／付費來源只能採以下兩種方式之一：
   - 完整取代該欄位的整段歷史；或
   - 在充分重疊期逐股票日驗證單位、方向、缺值與誤差後才拼接。
4. 已驗證 FinMind 早期 TPEX 投信資料與官方重疊期不一致，不得拿來補 2015~2017 洞。
5. 缺資料不等於 0；所有因子必須區分 `missing`、`zero`、`not_applicable`。
6. 原始回應必須保存，正規化 parquet 不能是唯一副本。

### 2.3 研究宇宙決策

- **MOM-1：TWSE 普通股**。
- **現行策略長期比較：TWSE-only**。
- **全市場現行策略：只可從 2018 起作一致覆蓋比較**。
- TPEX 納入 MOM-2 前，必須完成 TPEX 歷史 universe、下櫃、公司行動及交易限制資料。

---

## 3. 資料版本化與目錄契約

### 3.1 不覆蓋原則

目前 `data/research` 是已驗證 snapshot。補資料時不得直接覆蓋，先產生新版本：

```text
data/
  raw/
    twse/<dataset>/<request_date>/...
    tpex/<dataset>/<request_date>/...
  research_versions/
    research_vYYYYMMDD_HHMMSS_<short-hash>/
      prices.parquet
      dividend_events.parquet
      stocks.parquet
      delisted_stocks.parquet
      stock_universe_history.parquet
      industries.parquet
      stock_industry_map.parquet
      manifest.json
      quality_report.json
      README.md
```

只有 quality gate 全通過、重疊期對帳完成且使用者確認後，才能將新版本「promote」為正式
`data/research`。promotion 必須可回復，不得刪除舊版本。

### 3.2 `manifest.json` 必填欄位

- `snapshot_id`
- `created_at`、時區
- Git commit SHA、dirty worktree 清單
- Python 版本、lock／requirements SHA256
- 每個來源的 URL／endpoint、查詢參數、抓取時間
- 每個檔案 SHA256、bytes、row count、schema
- 日期最小／最大值、股票數、交易日數
- 主鍵重複數、null 統計、單位
- 原始資料目錄與原始回應 hash
- 任何已知缺口、修正與排除原因

### 3.3 標準單位

| 欄位 | 標準單位 |
|---|---|
| 價格 | 新台幣／股 |
| `volume` | 股，不是張 |
| `turnover` | 新台幣 |
| 法人買賣超 | 股，不是張 |
| 流通股數 | 股 |
| 市值 | 新台幣 |
| 回測／paper／broker 的 `shares` | 股，正整數 |
| 一般整張委託 | `shares // 1000` 張 |
| 零股委託 | `shares % 1000` 股 |

禁止以欄位名稱猜單位。匯入時要有 assertion；顯示成「張」只能在 UI 層除以 1000。

---

## 4. 資料補充工作包

### D0：凍結與備份

輸入：目前 `data/research`、commit `bf58807` 附近的可重現環境。
輸出：

- 現況 manifest 與 12 個核心 parquet hash。
- 正式回測摘要 JSON。
- 現況資料只讀備份／版本 ID。
- `.venv-repro` 套件清單與 Python 3.12.9 指紋。

通過條件：從凍結 snapshot 重跑能重現 475 筆交易與既有正式指標，容許誤差不超過
浮點顯示精度。

### D1：TWSE security master 與歷史 universe

目標欄位：

- `stock_id`
- `market`
- `asset_type`
- `listing_date`
- `delisting_date`
- `is_active_asof`
- `industry_code_asof`
- `snapshot_date` 或有效區間 `valid_from`／`valid_to`

要求：

- 研究只納入普通股，ETF／ETN／權證／特別股不得靠代號猜測排除。
- 股票只能在 `listing_date <= signal_date < delisting_date` 時進入候選池。
- 下市後最後價格不可 forward-fill 成永久可交易。
- 產業分類若只有現在值，必須標記為非 PIT，不能假稱歷史正確。

### D2：TWSE 2005~2014 日價量

必要欄位：

```text
stock_id, trade_date, open, high, low, close, volume, turnover, change_pct
```

規則：

- 原始未調整價與公司行動事件分開保存。
- 主鍵 `(stock_id, trade_date)` 唯一。
- 技術指標一律由凍結的原始價量重新計算，不匯入來源端預算指標。
- 至少保留 2005~2007 作 MOM-1 formation／MA200 warm-up；2008~2014 才進 backward holdout。

### D3：公司行動與 total-return 還原

補齊：現金股利、股票股利、分割、合併、減資及交易所參考價事件。

品質要求：

- 每個疑似單日跳動超過 20% 的事件都要能對到公司行動、漲跌停規則或資料異常。
- 調整後 close 用於訊號與績效；實際成交、滑價與委託限制仍依當日可交易原價計算。
- 0050 基準使用同一套 total-return 原則。
- 隨機抽樣至少 30 個除權息事件，人工核對官方 pre-close／reference price。

### D4：流通股數／市值與產業資料

MOM-1 的 MVP 可用成交金額篩選，但部署前需要 point-in-time：

- 流通／已發行股數
- 市值
- 產業分類有效期間

若只能取得現在市值，不得回填歷史。資料未到位時，研究結果要明示「只用流動性，不做
市值中性／小型股控制」。

### D5：TPEX 第二階段補強

本階段不阻擋 MOM-1，但阻擋全市場正式部署：

- 2008 起日價量及公司行動
- 歷史上櫃／下櫃 universe
- TPEX 注意／處置事件
- 交易限制與缺值語意

TPEX 法人 2018 前若找不到整段一致來源，維持 missing；不得補 0、不得拼接已否決來源。

### D6：重疊期對帳與 promotion

新舊 snapshot 的 2015~2026 重疊期應逐檔比較：

- 主鍵與 row count
- OHLCV／turnover
- 公司行動與調整因子
- universe membership
- 0050 total-return NAV

任何差異都要分類為：來源修正、舊 bug、新 bug、或無法解釋。無法解釋的差異會阻擋
promotion。

---

## 5. 資料品質閘門

### 5.1 結構閘門

- 所有主鍵無重複。
- 日期型別與時區固定。
- `low <= min(open, close) <= max(open, close) <= high`，例外要有原因。
- 價格大於 0；volume／turnover 不得為負。
- stock ID、market、asset type 可解析。
- 每個交易日的股票數相對前一日異常跳動要告警。

### 5.2 覆蓋閘門

- 逐年、逐市場列出 price／universe／corporate-action 覆蓋。
- 不只檢查日期起訖，也檢查每日橫斷面股票數。
- 不得把只有一個 2026 snapshot 的 universe 報成 2005~2026 point-in-time 完整。
- 若某因子年度覆蓋低於 90%，該年度不得和高覆蓋年度合併估計因子效果。

### 5.3 經濟合理性閘門

- 隨機抽 30 檔、每檔至少 5 個日期與官方來源核對。
- 股價跳動、成交量、法人數字做單位倍數檢查（1、100、1000）。
- 報酬極端值列出 top／bottom 50 筆並逐筆分類。
- delisted 股票必須留在其歷史有效期間，且下市後不可成交。

### 5.4 可重現閘門

同一 commit、環境與 manifest 連跑兩次，必須得到：

- 完全相同的輸入 hash。
- 完全相同的交易清單排序與 shares。
- 指標差異低於 `1e-10` 或明確的浮點容許範圍。
- 產出檔含 config hash、data hash、code SHA、執行時間。

---

## 6. MOM-1 研究假說

### 6.1 主假說

> 在 point-in-time 的 TWSE 普通股與可交易流動性宇宙中，排除最近一個月後的中期價格
> 動能，對未來 60~120 個交易日具有可交易的正向延續；採月頻再平衡、T+1 成交並扣除
> 完整成本後，其風險調整報酬優於 0050 或提供可觀的分散價值。

### 6.2 為何是獨立策略

MOM-1 不使用以下現行策略要素：

- 月營收年增
- 投信連買／投信新進場
- 外資買超
- 現行 5 日 cadence 排名
- 現行 50% 啟動、25% 回落的個股 trailing exit

這可以區分「價格延續 edge」與現行「基本面／法人流＋長抱」edge，避免只換名字重做同一套
策略。

### 6.3 污染標記

| 期間 | 用途 | 狀態 |
|---|---|---|
| 2015-01~2026-07 | 假說形成、診斷、實作除錯 | **已污染，不可驗收** |
| 2025-06~2026-07 | 既有策略反覆調參 | **硬污染** |
| 2005~2007 | formation／MA warm-up | 不評分 |
| 2008-01~2014-12 | MOM-1 一次性 backward holdout | **目前未看，規格凍結後才可開封** |
| 2026-08-12 起 | forward paper trading | **最終乾淨證據** |

不得因 backward holdout 結果不好就重新命名同一變體再測 2008~2014。

---

## 7. MOM-1 固定策略規則

### 7.1 共同 universe

訊號日須同時滿足：

1. TWSE `common_stock`。
2. 當日依 point-in-time master 為有效上市。
3. 至少 252 個有效交易日歷史。
4. 收盤價至少新台幣 10 元。
5. 過去 20 日平均成交金額位於當日 TWSE 普通股前 50%。
6. 非處置／停止交易／全額交割等無法按模型成交的股票。
7. 訊號所需價格與 universe 欄位無缺值；缺值不得補 0。

流動性門檻使用當日以前的資料，不得包含 T+1 成交資訊。

為避免實作間產生不同 universe，流動性規則凍結如下：20 個交易日必須全部有
非缺值成交金額，少一日即不合格；分位數母體是當日所有具完整 20 日窗的 PIT
TWSE 普通股，先套用價格、252 日歷史或其他濾網後才計算分位數是錯誤實作。

### 7.2 固定形成訊號

主要訊號只使用一個定義：

```text
mom_6_1(t) = adjusted_close[t-20] / adjusted_close[t-120] - 1
```

執行方式：

- 每月最後一個交易日收盤後計算。
- 在 eligible universe 做橫斷面百分位排名。
- 新進場只取前 10%。
- 既有部位跌出前 20% 才賣，形成 10%／20% buffer 以降低換手。
- 同分時以 `stock_id` 升冪作 deterministic tie-break，不得隨機。

10%／20% 名額均採 `floor(universe_size × fraction)`，不以四捨五入或至少一檔
放寬；名額為 0 時保留現金。最大 10 檔與 buffer 衝突時，仍在前 20% 的既有部位
優先保留，剩餘名額才由前 10% 新進者依排名補入。新進者不得擠掉尚未觸發出場條件
的既有部位。

`mom_6_1` 是用已污染的 2015~2026 診斷形成的候選，因此只能由未看過的 2008~2014 與
forward period 驗證，不能引用 2015~2026 的漂亮數字作採用理由。

### 7.3 兩個事前登記版本

#### MOM-1A：純橫斷面動能控制組

- 只使用 §7.1 universe 與 §7.2 排名。
- 每月再平衡。
- 不加市場濾網、不加個股停損、不加營收／法人條件。
- 用途：檢驗原始動能 edge，不以部署為目標。

#### MOM-1B：可部署候選

在 MOM-1A 上只增加一條事前固定規則：

- 若 0050 total-return close 低於其 200 日均線，下一交易日開盤將策略目標曝險降為 0；
  重新站回 MA200 後，於下一個月頻訊號日恢復選股。

不得再測 MA60／MA100／MA120／MA250 或多段曝險。本輪總變體數固定為 **2**。

### 7.4 組合與 sizing

- 最大持股：10 檔。
- 目標權重：等權，每檔 10%。第一版不用最佳化權重。
- 同一產業最多 3 檔／30%。若產業資料不是 point-in-time，阻擋正式驗收。
- 候選不足時保留現金，不用排名較低股票補滿。
- 資本：300,000 元。
- `shares = floor(target_notional / executable_price)`，結果必須是非負整數股。
- 可交易量上限沿用平均成交量 1%；不得因小資金假設無限流動性。
- 回測允許整股＋零股的經濟曝險；live broker 必須拆為普通整張與盤中零股委託。
- 禁止將 shares 直接傳給以「張」為單位的 broker 欄位。

### 7.5 成交與成本

- 月末訊號只使用收盤前已公開資料。
- 訂單最早在 T+1 開盤成交。
- 無開盤價、漲跌停鎖死或停止交易時不得假成交，訂單按既有執行規則延後或取消。
- 手續費、證交稅、滑價沿用正式 backtest 常數；報告同時列 30bp 與壓力成本，但壓力成本
  只作風險報告，不用來挑版本。
- 報酬與基準都含息；訊號使用調整價，成交使用可交易原價。

### 7.6 出場

MOM-1A／B 第一版不使用個股停損、死亡交叉或 trailing stop，避免把「選股 edge」和出場
調參混在一起。出場只由以下事件觸發：

1. 月末排名跌出前 20%，T+1 開盤賣出。
2. 股票失去合法 universe 資格或進入不可持有狀態。
3. MOM-1B 市場濾網觸發全體降至現金。
4. 回測期結束，按最後可交易價格標記並另列未實現損益。

若 MOM-1 通過 edge 驗收，停損／波動 sizing 屬下一份「production risk overlay」規格，
不得回頭修改 MOM-1 holdout 結果。

---

## 8. 基準、報告與統計方法

### 8.1 必要基準

1. 0050 含息買進持有。
2. MOM universe 等權含息組合。
3. MOM-1A（控制組）。
4. MOM-1B（唯一部署候選）。

### 8.2 必報指標

- 總報酬、年化報酬、年化波動
- Sharpe、Sortino、最大回撤、Calmar
- 相對 0050 的年化超額、tracking error、information ratio、beta
- 月勝率、年度報酬、最長回撤與恢復時間
- turnover、交易成本占本金／毛利比例
- 平均持股、現金比例、產業曝險
- top 1／5／10 交易毛利占比
- 移除最佳 5 筆後的淨損益
- 單一年份占總毛利上限
- 2008 類危機、反轉、多頭與盤整 regime 切片

### 8.3 統計方法

- IC 顯著性用 Newey-West 或 block bootstrap，不再使用重疊日資料的 naive t-stat 作結論。
- 組合績效用月報酬 bootstrap，block 長度事前固定為 6 個月。
- 報告 95% confidence interval，不只報單一 Sharpe。
- MOM-1A／B 共 2 次試驗，記入 `research/EXPERIMENTS.md` 多重測試計數。
- 所有未預先登記的切片只能標示 exploratory，不得決定採用。

---

## 9. 驗收與停止條件

### 9.1 F0：實作正確性

全部必須通過：

- 合成資料下的動能排名、skip-month、10%／20% buffer 單元測試。
- 訊號日期後移一日成交，無 look-ahead。
- universe listing／delisting 邊界測試。
- 公司行動前後 total-return 連續性測試。
- shares／lots 單位測試與 broker split 測試。
- 相同輸入兩次結果完全一致。

### 9.2 F1：backward holdout edge

2008~2014 一次性開封後，MOM-1B 必須同時滿足：

- 扣完整成本後累積淨利為正。
- 相對 0050 年化超額大於 0。
- Sharpe 不低於 0050。
- 最大回撤不比 0050 差超過 5pp。
- 移除最佳 5 筆後淨損益仍為正。
- 單一年份占總毛利不超過 40%。

MOM-1A 是控制組，不要求通過部署門檻；但 A／B 結果都必須揭露。

### 9.3 F2：穩健性

只做下列事前固定檢查，不掃參數：

- 成本加倍後仍為正淨利。
- 依股票代號奇偶分半，兩半方向一致。
- 依月份奇偶分半，兩半方向一致。
- 產業中性後方向不反轉。
- 2008 壓力期的 loss／exposure 可解釋。

### 9.4 F3：forward paper trading

只有 F1、F2 通過才開始：

- 至少 12 個月且至少 80 筆完成交易，兩者皆達成。
- 實際 paper fill 與模型 fill 的 tracking error 有每日紀錄。
- 不允許期間修改形成期、排名門檻、MA200 或出場規則。
- forward Sharpe／超額未達門檻時，MOM-1 判為未證實，不用回測重調掩蓋。

### 9.5 失敗後的處理

- F0 失敗：修 bug 後重跑，不計新策略變體；修正與差異必須記錄。
- F1／F2 失敗：MOM-1 否決，不在同一 holdout 調參。
- 若要研究 12-1、殘差動能、產業動能或波動調整動能，建立新規格 MOM-2，並承認
  2008~2014 已被看過，不能再當乾淨 holdout。

---

## 10. 實作介面與產出

建議新增：

```text
research/momentum.py
scripts/build_research_snapshot_v2.py
scripts/run_momentum_study.py
tests/test_momentum.py
tests/test_research_snapshot_v2.py
research/results/mom1_<snapshot_id>.json
reports/MOM1_<snapshot_id>.md
```

`run_momentum_study.py` 最少要接受：

```text
--snapshot <versioned directory>
--start YYYY-MM-DD
--end YYYY-MM-DD
--variant MOM-1A|MOM-1B
--capital 300000
--output <json path>
```

禁止提供形成期、skip 日數、排名門檻、MA 期間的任意 CLI sweep；這些值由本規格固定。

每次產出 JSON 必含：

- spec version／hash
- strategy config／hash
- code commit SHA
- data manifest／hash
- Python／package versions
- 完整交易清單與 shares
- NAV、基準 NAV、年度與 regime 指標
- concentration／remove-top-5 結果
- 所有 gate 的 pass／fail 與原因

---

## 11. 執行順序與交付物

| 階段 | 工作 | 交付物 | 進入下一階段條件 |
|---|---|---|---|
| 0 | 凍結現況 | current manifest、正式回測 JSON | 可重現 |
| 1 | 建 raw cache／versioned builder | 新資料不覆蓋舊 snapshot | 單元測試通過 |
| 2 | 補 TWSE master、2005~2014 價量 | raw＋normalized parquet | 結構／覆蓋 gate 通過 |
| 3 | 補公司行動、universe、產業 | PIT research snapshot | 經濟合理性 gate 通過 |
| 4 | 重疊期對帳 | comparison report | 無未解釋差異 |
| 5 | 登記 MOM-1A／B | EXPERIMENTS 新列 | 規格 hash 凍結 |
| 6 | 實作與 F0 | code、tests、dry run | F0 全通過 |
| 7 | 開封 2008~2014 一次 | MOM-1 JSON／報告 | F1、F2 判決完成 |
| 8 | forward paper | 每日訊號／成交追蹤 | 12 個月＋80 筆 |
| 9 | 部署決策 | 採用／否決／待驗 | 使用者確認 |

---

## 12. 完成定義

本計畫不是以「找到一個漂亮回測」為完成，而是以下事項全部有明確結果：

1. 新研究 snapshot 可由 raw source 重建且有 hash。
2. 2005~2014 TWSE 價量、公司行動與 universe 通過品質閘門。
3. 目前策略在一致 universe 上重新產出診斷，且沒有股／張錯誤。
4. MOM-1A／B 只跑事前登記的兩個版本。
5. backward holdout 不論成功或失敗都留下完整報告。
6. 成功候選進入不可調參的 forward paper；失敗候選被正式否決。
7. 下一個研究決策基於證據，而不是再次從 2025~2026 挑看起來最好的參數。

---

## 13. 本規格引用的既有專案證據

- `docs/SPEC_QUANT_UPGRADE.md`：研究污染、資料地基、現行策略風險調整績效。
- `research/EXPERIMENTS.md`：多重測試、TWSE-only 決策、FinMind TPEX 法人拼接否決。
- `research/results/factor_report_2026-07-19_with_revenue.json`：既有因子 IC／十分位結果。
- `agent/strategy.py`：現行 `mom60` 權重歸零、法人／營收權重與出場規則。
- `agent/backtest.py`：T+1 成交、成本、shares sizing、NAV 與基準計算。
- `repro_snapshot_20260811_102114/RESTORE.md`：目前可重現環境與 snapshot 還原流程。
