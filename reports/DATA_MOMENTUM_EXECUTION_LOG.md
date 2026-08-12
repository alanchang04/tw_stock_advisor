# 資料補強與 MOM-1 執行紀錄

規格來源：`docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md`
開始日期：2026-08-11（Asia/Taipei）
原則：本檔只記錄執行結果；不回頭修改已凍結的 MOM-1 規則或 backward holdout 門檻。

## D0：現況凍結與可重現基準

- 固定 snapshot：`data/research_versions/research_v20260811_current_bf58807`
- 12 個 parquet，總計 158,702,929 bytes。
- snapshot content SHA-256：`A108619835EA72A3837FC64E008BF3698A5889F17CD6C9E188A3DF2F5532D8F3`
- manifest SHA-256：`D906D614758A92C1DD5716645645DCC71BAB3D1E228C4EB625DCFB900DA29A2F`
- 結構品質通過：主鍵重複 0；非正 OHLC、負 volume／turnover、OHLC 關係錯誤皆為 0。
- 尚未達全市場 point-in-time research-ready：universe 只有單一快照、2018 前無 TPEX 法人、
  TPEX 處置／注意事件未補齊。這些限制已明列，未被靜默忽略。

固定 snapshot 連續重跑兩次，以下內容完全相同：

- 策略設定 SHA-256：`B915F0398C53D72867A1002C068107A533E29F3EB75073FC121E36660D4F2432`
- 交易表 SHA-256：`09EDEEC11D36A2370363C3D13F225119FFCEC3A9875B0F19CA33ED94CAFC37C9`
- 策略 NAV SHA-256：`136398F837D89CD6A156227F4D5AE76EE90CA27408DB16D013AB8A3CF5A765BA`
- 0050 NAV SHA-256：`FCB58223D0B569B4207BF770ED55CFEEDF30F16E555C60ADCC122F3C3CD0D7E0`
- 交易 475 筆；總報酬 671.6245%；年化報酬 19.3203%；Sharpe 0.91935；
  MDD -42.7169%；Calmar 0.45229。
- 對照 0050：總報酬 767.5928%；Sharpe 1.04118；MDD -33.9570%；Calmar 0.60475。
- 驗收檔：`reports/swing_backtest_verified_20260811_bf58807.json`、
  `reports/swing_backtest_verified_20260811_bf58807_repeat.json`。

結論：本機已能穩定重現同一組回測數字；現行策略弱於 0050 的判斷不是隨機執行差異。

## 資料版本化與下載管線

- `scripts/build_research_snapshot_v2.py`：只複製 parquet、逐檔驗 SHA、原子發布、禁止覆寫。
- `research/snapshot_manifest.py`：記錄環境、Git、資料 schema／範圍／主鍵／單位與品質閘門。
- `scripts/backfill_twse_momentum_history.py`：官方 JSON gzip 原始歸檔、SQLite 日期狀態、可續跑、
  price-only snapshot 原子匯出，且不寫入 `data/research` 或 Neon。
- price-only snapshot 只可通過結構閘門，明確標記不可直接用於全市場 point-in-time 回測。
- 自動測試：16 passed；另有一項既存 pandas deprecation warning，不影響結果。
- 股／張關鍵路徑測試：114 passed、6 skipped；position ledger、sizing、現金與成本皆以 shares
  運算，只有明示的法人門檻與顯示層除以 1,000 轉為 lots。

## D2：TWSE 2005～2014 日價量

### Smoke test

- 日期：2005-01-03～2005-01-07。
- 5 份官方 gzip、3,493 列；第二次執行下載 0、cache 命中 5。
- 結構品質通過；volume 單位為 shares；不可直接策略回測的 scope 標記正確。

### 正式進度

2005 已完成：

- 已處理平日 260；交易日 247。
- 行情 172,792 列；狀態表宣告列數與價格表完全相等。
- 738 個原始代碼；日期 2005-01-03～2005-12-30。
- 負成交量 0；非整數成交量 0；標準單位為 shares。
- 原始資料保留所有官方代碼；ETF、權證與非普通股會在 D1 point-in-time security master
  階段排除，不用容易出錯的「四位數即股票」捷徑。

2006 已完成：260 個平日、247 個交易日、172,018 列。

2005～2006 累積驗收：

- 520 個平日狀態、494 個交易日、344,810 列、756 個原始代碼。
- 日期 2005-01-03～2006-12-29。
- 狀態表宣告列數與價格表完全相等；負成交量 0；非整數成交量 0。

2007 已完成：261 個平日、243 個交易日、168,400 列。

2005～2007 warm-up 累積驗收：

- 781 個平日狀態、737 個交易日、513,210 列、791 個原始代碼。
- 日期 2005-01-03～2007-12-31。
- 513,210 筆逐日證券名稱 observation，與價格列一一對上。
- price-only snapshot：`data/research_versions/twse_prices_2005_2007_warmup_v1`。
- snapshot content SHA-256：`7E99F34230E4F66038341B17E65102B96CBE6A152C52664B3895658D37FCF228`。
- 781 份 raw response 集合 SHA-256：
  `7E0AF250EE0F275CD58ABD40078ACFBED49F528C6BC21620823D3D28A4895CE8`。
- 結構閘門通過；price-only scope 仍明確標記為不可直接執行策略回測。

2008～2014 原始價量已完成。這一段是 backward holdout，在 D1／D3 與
資料品質閘門完成前不得執行或查看 MOM-1 績效。

2005～2008 累計驗證：

- 1,043 個平日 raw response、986 個交易日、692,798 筆價格與同數量證券名稱 observation。
- 官方宣告列數、價格列數、security observation 列數皆為 692,798，完全一致。
- 826 個證券代號；主鍵重複 0；OHLC 不一致 0；負成交量 0；非整數成交量 0。
- 成交量單位為 `shares`，未在儲存層除以 1,000；下單層仍須依規格顯式換算整張／零股。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2008_backfill_v1`。
- snapshot content SHA-256：
  `77075160C9FEB62745F0B75B71569BF04FBBBDE811D28F0594EEAA89645ACE0C`。
- 1,043 份 raw response 集合 SHA-256：
  `E8F20A28F6DC1871B0F3D3318326404E6D556D852A06DCF2DD05C17BF378B384`。
- 由相同 raw／SQLite 再建 `twse_prices_2005_2008_backfill_v1_repeat`，content SHA-256
  完全相同；結構閘門通過，仍明確標記為 price-only、不可做策略回測。

2005～2009 年度 checkpoint：

- 1,304 個平日 raw response、878,154 筆價格、865 個證券代號；日期止於 2009-12-31。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2009_backfill_v1`。
- snapshot content SHA-256：
  `29C507D863EAA32FC7FBCED9F4D633D028BF111C0169D77EF299936BBCB47288`。
- raw response 集合 SHA-256：
  `389C6C22DDE7F5ED817F34BDA61DE86AB565799DD256A4A2378A0971C52D4F98`。
- 相同來源重建 `twse_prices_2005_2009_backfill_v1_repeat`，content SHA-256 完全相同；
  主鍵重複 0、OHLC 不一致 0，結構閘門通過。
- exporter 新增 requested `start/end` SQL 邊界與 manifest 欄位；即使共享 SQLite 已有
  2010-01-12 資料，2009 snapshot 仍不會混入 2010，並有回歸測試鎖定。

2005～2010 年度 checkpoint：

- 1,565 個平日 raw response、1,484 個交易日、1,073,561 筆價格與同數量 observation。
- 908 個證券代號；負成交量 0、非整數成交量 0、重複主鍵 0、OHLC 不一致 0。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2010_backfill_v1`。
- snapshot content SHA-256：
  `80AB6C49B9AA82A5D6426F8D9CF55682C89CA854AF9B9A2C9DA5E93C26D28B37`。
- raw response 集合 SHA-256：
  `DA52E198E1366A2A0D56AC201C8E7E15EEA44231DD2A083F79A88613965C4D19`。
- 重建 `twse_prices_2005_2010_backfill_v1_repeat` 後 content SHA-256 完全相同，
  結構閘門通過。
- 曾測試三個互斥年份 shard；官方端點回傳非 JSON 後即停止並行，改回單路、較長節流。
  已完成 raw 可續用，無部分檔案。另有 2012-02-08 與 2014-02-04 以前的 raw cache，
  尚未匯入正式主 SQLite／snapshot，後續單路續跑時才正規化。

2005～2011 年度 checkpoint：

- 1,825 個平日 raw response、1,731 個交易日、1,275,809 筆價格與同數量 observation。
- 959 個證券代號；負成交量 0、非整數成交量 0、重複主鍵 0、OHLC 不一致 0。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2011_backfill_v1`。
- snapshot content SHA-256：
  `9FB99A746FEC779C3870E1F17347C75DA48BC1266C1A56CD0D2F5EA52080A7F0`。
- raw response 集合 SHA-256：
  `4811D5FCB5B215D65312BF4ABDBA2DB17DD418898285B0244893D38B8268130E`。
- 重建 `twse_prices_2005_2011_backfill_v1_repeat` 後 content SHA-256 完全相同，
  結構閘門通過。

2005～2012 年度 checkpoint：

- 2,086 個平日 raw response、1,978 個交易日、1,486,017 筆價格與同數量 observation。
- 984 個證券代號；負成交量 0、非整數成交量 0、重複主鍵 0、OHLC 不一致 0。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2012_backfill_v1`。
- snapshot content SHA-256：
  `AC094A780FB897BB74A7C67192B5AE5E1A8731473AA942172749C49E5CA68038`。
- raw response 集合 SHA-256：
  `11F164C1BC86CD3243054F9EFB897E6D8274CF2D3C1712BA5A7F0526AE6661D2`。
- 重建 `twse_prices_2005_2012_backfill_v1_repeat` 後 content SHA-256 完全相同，
  結構閘門通過。

2005～2013 年度 checkpoint：

- 2,347 個平日 raw response、2,222 個交易日、1,697,488 筆價格與同數量 observation。
- 1,017 個證券代號；負成交量 0、非整數成交量 0、重複主鍵 0、OHLC 不一致 0。
- immutable price-only snapshot：
  `data/research_versions/twse_prices_2005_2013_backfill_v1`。
- snapshot content SHA-256：
  `F4F476B1ABF1A7FE247D785DDEE404A846E99414AAD3FD43B11D1AA0014794F7`。
- raw response 集合 SHA-256：
  `BEC452C24723132C28F3022B79C474063CDACCD91CE656D5845361971FF1E6CB`。
- 重建 `twse_prices_2005_2013_backfill_v1_repeat` 後 content SHA-256 完全相同，
  結構閘門通過。

2005～2014 D2 最終 checkpoint：

- 2,608 個平日 raw response、2,469 個交易日、1,917,768 筆價格與同數量 observation。
- 1,042 個證券代號；負成交量 0、非整數成交量 0、重複主鍵 0、OHLC 不一致 0。
- immutable price-only snapshot：`data/research_versions/twse_prices_2005_2014_v1`。
- snapshot content SHA-256：
  `6E04C1E32BC93899E439D5DE2161C7F77170C227F03CB753207464BCFE2CBE59`。
- raw response 集合 SHA-256：
  `364F523DB3AA84B430F955E7FB66ACCBCCEE3D5C5C2CA34166370DB84297F496`。
- 重建 `twse_prices_2005_2014_v1_repeat` 後 content SHA-256 完全相同，結構閘門通過。
- `research_ready_for_point_in_time_all_market=false`：D2 價量已完成，但 D3 公司行動與
  PIT universe／產業尚未 promotion，因此仍禁止執行 MOM-1。

跨主機 raw 搬運清單：

- manifest：`reports/twse_raw_transfer_manifest_2005_2014_20260811.json`。
- 2,652 個檔案、338,365,921 bytes。
- collection SHA-256：
  `2B4B876A5311F4BF9A5D91A4D132F8F73ED8B488B6C4EF41EB7F5221F085518B`。
- 建立後在來源主機自我驗證：2,652 checked、missing 0、mismatched 0、passed=true。

## 尚未開封的研究階段

- D1 security master／歷史 universe、D3 公司行動、D4 流通股數／市值與產業仍未完成。
- 資料品質與重疊期對帳未全部通過前，不執行 MOM-1A／MOM-1B，也不查看 2008～2014
  backward holdout 績效。

## D1：TWSE security master staging

官方來源已封存：

- 2005～2014 TWSE 年報 10 份完整 PDF。
- 官網可用的上市異動分章 PDF 9 份；沒有文字層的 95／98／99 年頁面另渲染 PNG 核對。
- 2026-08-11 上市公司基本資料 1,094 筆。
- 完整終止上市公司資料 264 筆，最早可追溯至民國 90 年。
- 每個官方檔案、derived text／image 與來源 URL 均記錄在
  `data/raw/twse/security_master/source_manifest_2026-08-11.json`。

staging snapshot：`data/research_versions/twse_security_master_2005_2007_staging_v3`

- content SHA-256：`3C61D7437BC9F4FD2A02787BE684288F0416C179BC32C3468B87AFBB2F6F080F`。
- source manifest SHA-256：`CDA3D33B82701330C2541CBFFD131B0F880664C4F130A635B7B1C22FC980D914`；
  使用本地封存來源重建後保持一致。
- v3 嚴格限制 observation 日期為 2005-01-01～2007-12-31，內容雜湊與 v1 完全相同。
  `staging_v2` 曾因建構器未限制結束日而混入下載中的部分 2008 observation，已標記
  `INVALID_DO_NOT_USE.md`，從未用於策略或績效計算。
- 791 個曾出現在官方行情的證券：普通股 751、TDR 5、其他非公司證券 35。
- 756 個 issuer 由上市公司／終止上市公司官方資料直接分類；ETF、REIT、特別股、
  可轉債與基金因不屬公司 issuer 資料集而保守排除。
- 普通股 effective-from 覆蓋 100%；623 檔有官方精確上市日，其餘使用首次官方成交日，
  並明確標記非精確 listing date。
- 36 個月末 snapshot、24,759 列；`(snapshot_date, stock_id)` 重複 0。
- 2005／2006／2007 年末「有官方成交觀察的普通股」為 689／686／696；相對年報法定
  掛牌公司數 691／688／698 固定少 2，初步判斷為無行情的停止交易公司。兩者語意未混用。

D1 尚未 promotion，原因：

- 歷史 `industry_code_asof` PIT 覆蓋為 0%；不能用 2026 產業分類回填。
- D2 已有 2005～2014 價格／名稱 observation；D1 staging 仍凍結於 2005～2007，待 PIT
  產業資料與公司行動完成後才建立 promotion candidate。
- D3 公司行動與 total-return 還原尚未完成。

因此目前只能建立保守歷史 universe，仍禁止執行 MOM-1A／B。

## 測試與工作區隔離

- 新增 `pytest.ini`，正式測試只從 `tests/` 收集，並排除 handoff、repro snapshot、
  虛擬環境與資料目錄；保留另一台電腦帶回的備份也不會再造成同名模組衝突。
- 2026-08-11 完整正式測試：521 passed、55 skipped、0 failed、4 warnings。
- D1／D2／manifest／transfer 定向測試：13 passed；包含「staging 結束日後 observation 必須排除」
  與「price snapshot requested 結束日後資料必須排除」的回歸測試。

## D3：TWSE 公司行動 staging 與價格跳動稽核

- 已封存 2005～2014 除權息月報 120 份，以及 2011～2014 減資月報 48 份。
- 官方 source rows 6,277 筆完整保留；去除兩組公司更名／公告別名後為 6,275 個 canonical
  經濟事件，並非資料遺失。
- 前收、參考價與調整因子均為正值；可跨主機重建的 staging v2 與 repeat content
  SHA-256 均為 `C1C488EC4F1D74C8979D94C0D1F0864F9D28C7F43A7FD8D2DE78BB864C1DE647`。
  v2 將 raw 路徑改為 repo-relative，避免不同磁碟／使用者目錄造成 snapshot 差異。
- 168 份 raw response 的集合 SHA-256 為
  `3F1306EC2B6C1AE774BC020584E8F3305BE77B400637FAE831919862FAF4621D`。
- 1,917,768 筆價格中共有 324 筆相鄰觀察跳動超過 20%；138 筆對到公司行動，
  2 筆屬非普通股，2 筆屬新上市價格發現，短空窗未解釋為 0。
- 官方 archived row 採 first／middle／last 分層抽樣 42 筆：TWT49U 30 筆、TWTAUU 12 筆；
  raw SHA mismatch 0、17 個正規化欄位 mismatch 0、`passed=true`。
- 另有 182 筆發生在超過 7 天沒有成交觀察之後；D3 仍為 `promotion_ready=false`。阻礙為
  2005～2010 減資缺口、合併／分割完整性、停牌 stale-price eligibility 規則，以及 10 筆
  官方前收與 MI_INDEX 前收差異（其中 3 筆為普通股）。不得據此開封 MOM-1。
- 現有 `total_return_adjust` 的因子方向與多事件複利數學正確，但舊回測同時把 adjusted
  open 用於成交與股數 sizing；這會讓 ledger 的 `shares` 成為復權後合成單位，而非可逐筆
  對帳的實際股數。MOM-1 實作前必須拆成「adjusted close 只供訊號／績效、raw open 供成交」
  並另建股利現金與股數變動 ledger，否則不符合本規格第 248 行。

跨主機 D3 raw manifest：
`reports/twse_raw_transfer_manifest_2005_2014_d3_20260811.json`，共 2,820 個檔案、
338,804,487 bytes；集合 SHA-256 為
`798C9F5835E18083010AA0CB1778E721C84FEE661642B3ED1C1B616689453E44`，本機驗證結果為
missing 0、mismatched 0、passed=true。

2026-08-11 D3 更新後完整正式測試：528 passed、55 skipped、0 failed、4 warnings。

### D3 第二輪：參考價重設與實際股數 ledger 閘門

- MI_INDEX 的 `change_pct` 是相對當日交易所參考價；不能一律拿最後一次有成交的收盤價
  當分母。182 筆長觀察空窗的大跳動已全部反推出官方當日參考價：參考價相對上次收盤
  重設超過 20%，但恢復交易當日相對新參考價的市場漲跌均在 10% 內。因此改列
  `official_reference_reset_unresolved_cause`，不再描述成單日市場暴漲跌。
- 182 筆只證實「參考價曾重設」，尚未證實一定是減資；在未配對減資／合併／分割等
  官方原因前，`total_return_eligible=false`，本輪未擅自調整任何一筆。
- MOPS 官網目前仍列出「公司增減資表」`IRB160`，前端查詢條件為市場、民國年、月份；
  但官方 `redirectToIRB` 對不同市場、年度、月份及同系列新公司表都回覆查無相符資料。
  因此 2005～2010 減資原因仍是外部來源缺口，不能把空回覆當成完整資料。
- 搜尋索引仍可找到舊 `TWT49UDetail?STK_NO=...&T1=...` 公開明細網址，但 TWSE 現站所有
  新舊路徑都已回 404／首頁，不能作可重現來源。TWSE 資訊商店另有自 2009-10-14 起、
  含無償配股率／現增配股率／認購價／現金股利的付費資料產品；未取得授權資料前，
  相關事件繼續阻擋而不猜值。
- TWT49U 在部分年度只提供「權值＋息值」。parser v4 僅在純除息／純除權時依事件種類
  無歧義補回 cash／rights value；除權息合併事件維持缺值，沒有任意拆分。
- 獨立重建時發現 v3 的 `source_status.fetched_at` 錯記成本次重建時間，造成事件 parquet
  雖相同、snapshot content hash 卻不同；v3 與 repeat 已標記 `INVALID_DO_NOT_USE.md`。
  v4 改讀 gzip 原始檔內建 acquisition timestamp，兩個獨立 SQLite 重建結果完全相同：
  `7F0322EDD5FA6B67D8061204C48F7FA585EC75D3CBB4A552C1D0133682344780`。
- 實際股數／現金分解閘門共檢查 6,275 個 canonical 事件：4,979 筆可分解、1,296 筆阻擋。
  阻擋原因為 cash／stock split 缺失 972、現金增資認購條件缺失 288、退還股款與換股率
  缺失 36。138 筆大跳動已配對公司行動中，仍有 30 筆缺少可執行 ledger 條件。
- 內部數量契約固定為整數 `shares`；券商委託前明確拆成 `common_lots=shares//1000` 與
  `odd_lot_shares=shares%1000`。公司行動產生的畸零權利另存
  `fractional_share_entitlement`，取得官方折現價格前不得塞回 shares 或現金。
- 目前仍未把現行 backtest 切到 raw-open execution：缺少上述 1,296 筆事件條件時強行切換，
  只會把既有的合成股數問題換成漏記股利／換股的新錯誤。D3 promotion 仍為 false。

2026-08-11 本輪完整正式測試（`.venv-repro`）：540 passed、55 skipped、0 failed、4 warnings。

### D3 第三輪：MOPS 股利條件與「不可反推股數」修正

- 找到 MOPS 官方歷史股利彙總端點 `server-java/t05st09sub`；採用
  `qryType=1`（董事會決議／擬議分配股利年度），封存民國 93～103 年共 11 份 Big5
  原始 HTML，解析為 8,515 筆公司股利決議、923 個股票代號。舊表的現金股利、盈餘
  配股、資本公積轉增資與新版表的兩種現金／兩種股票股利欄位均分開保留。
- `mops_dividend_distributions_2004_2014_v1` 與 `_repeat` 均只從同一組 raw cache
  獨立重建；兩者 snapshot content SHA-256 完全相同：
  `95632FBB3A5CDDBC08DCAB78558AF25B9DD26238322BEB54D7DBC103F01ED42C`，
  `dividend_terms.parquet` 檔案 SHA-256 同為
  `E426FD79E5F2DFDEC1118EC4410DC76D25ACE707A10D041C0E69B61A375907A2`。
- 配對先使用股票代號、決議年度與官方參考價方程式；早期員工股票紅利會稀釋參考價，
  但不是舊股東取得的股票，因此方程式不連續時，只接受同決議年度內唯一的官方配股
  條件，合併除權息另以 TWT49U 已拆出的現金股利交叉核對。沒有唯一解仍維持 blocked。
- 先前把 `(前收盤價－現金股利)/除權參考價` 當成實際配股倍率並不夠嚴謹：參考價已
  四捨五入，而且早期可能含員工紅利稀釋。現在只有 MOPS 公告的股票股利（元／股）
  可換算為 `1 + 股票股利/10` 的實際股數倍率；price-implied factor 不再標示為 shares。
  減資同理，未取得官方換股率前，原先 50 筆以價格比反推的彌補虧損事件已降回 blocked。
- 重新檢查 6,275 個 canonical 事件：2,318 個除權事件找到唯一 MOPS 條件；ledger 為
  5,622 筆 executable、653 筆 blocked。阻擋原因為現增認購條件 288、官方無償配股條件
  219、cash／stock split 60、彌補虧損減資換股率 50、退還股款／換股率 36。
  限普通股範圍仍有 642 筆 blocked，因此 D3 promotion 仍為 false。
- 原本 972 筆 cash／stock split 缺失已降到 60；這一改善沒有犧牲實際股數語意。
  以 2330 在 2005-06-13 為例，現金 1.9998 元、股東配股倍率 1.049997 直接來自 MOPS；
  參考價中其餘稀釋不會誤灌成股東收到的股票。
- TWT48U 現行預告表確實公開無償配股率、現增配股率與認購價，但實測 `date` 與
  `startDate/endDate` 歷史參數均被忽略，查 2010 仍回傳民國 115 年預告資料；不能把
  124 筆現行資料誤認成歷史檔。資訊商店自 2009-10-14 起的歷史產品仍是目前已確認
  能直接補現增條件的官方來源。
- 另確認 MOPS 新站 `api/t05st01` 與 `api/t05st01_detail` 可查 2005 年歷史重大訊息
  與全文。以 2023 燁輝的現增案為 pilot，可分別讀到「每仟股認購 73.039 股」及
  發行價由 22 元調為 20 元；因此 288 筆現增 blocker 有免費官方 backfill 路徑。
  但同一案件可能有多次調價／調比率，且新股需經繳款與交付，不應在除權日直接當成
  已持有 shares；下一輪須封存候選公告、依生效時序唯一配對，並建立 subscription
  entitlement／cash contribution／delivery settlement 狀態後才可解除閘門。
- 大跳動分類維持 138 筆公司行動、182 筆未解原因參考價重設、2 筆非普通股、2 筆
  新上市，0 筆未解短空窗；因 actual-share 閘門收緊，138 筆公司行動中有 64 筆仍不可
  執行，未開啟策略績效或 backward holdout。

2026-08-11 本輪完整正式測試（`.venv-repro`）：546 passed、55 skipped、0 failed、4 warnings。

### D3 第四輪：現金增資認購權利與交付狀態

- 以 MOPS `api/t05st01`／`api/t05st01_detail` 封存 2005～2014 付現增 blocker
  對應的 356 個 stock-year 歷史重大訊息清單及 1,812 份候選全文；除事件年度外，
  另補 1～4 月事件的前一年度公告，避免跨年案件漏配。
- 正規化後保留 1,556 個官方 fact：舊股東認購率 312、每股發行／認購價 663、
  原股東占整筆新股分配比例 581。`mops_paid_subscription_announcements_2004_2014_v1`
  與 `_repeat` 兩次獨立重建 content SHA-256 均為
  `EC878498E51D4A64DDF951097602C8AEF2E496E5BB791A4BA233B3D4A98D926A`；
  `subscription_facts.parquet` SHA-256 均為
  `7D42322354BCBC1FFA15804A4CBC350D5B1EDA3F799B6FC625A06B33A2E69F7F`。
- 修正兩種容易混淆的「配股率」：舊股東每千股可認購股數是個別投資人的 entitlement；
  TWSE 參考價公式使用整筆現增相對舊股本的 dilution rate，還包含員工認購及公開承銷。
  若原股東取得新股的 75%，則公式 dilution rate = shareholder entitlement rate / 0.75，
  兩個倍率分欄保存，禁止互換。
- 288 筆付現增事件中，112 筆已由公告日不晚於除權日的官方認購率、分配比例、認購價
  找到唯一方程式解；其餘為 rate／price／allocation 缺一 90、完全沒有候選 fact 45、
  方程式無解 18、同時缺 free-share／cash 條件 13、方程式多解 10。
- 112 筆尚未改列 executable，而是把 blocker 從 `paid_subscription_terms_missing` 改為
  `paid_subscription_settlement_timing_missing`。除權日只建立認購權利：原持有 shares
  不變，另存可認購整股、畸零認購權、每股繳款金額與 total dilution rate；實際繳款及
  新股交付日期完成前，不得扣現金或增加 shares。
- 最新 6,275 筆 ledger 仍為 5,622 executable、653 blocked；blocked 分成官方無償配股
  219、現增條件仍缺 176、現增條件已齊但 settlement timing 未齊 112、cash／stock split
  60、彌補虧損減資換股率 50、退還股款／換股率 36。D3 promotion 維持 false。

2026-08-11 本輪完整正式測試（`.venv-repro`）：550 passed、55 skipped、0 failed、4 warnings。

### D3 第五輪：現增 settlement 日期與跨案件錯配防護

- 針對前一輪 112 筆已取得認購條件的案件，補抓認股基準日、原股東／員工繳款起迄、
  增資基準日與新股／股款繳納憑證上市日；除事件年度及年初事件的前一年外，也查詢
  10～12 月事件的次一年度公告，避免跨年交付漏失。v4 共保留 2,501 個官方 fact，包含
  認股基準日 443、繳款起日／迄日各 166、增資基準日 116、新股或繳納憑證上市日 32。
- 初版日期配對曾出現 8011 的反例：把 2013 年 11 月下一次現增繳款期接到 2012 年
  除權事件及 2013 年 1 月交付日，形成「先交付、後繳款」的不可能順序。正式 matcher
  現限制事件後 240 日、付款起迄必須來自同一份公告，並強制
  `除權日 <= 繳款起 <= 繳款迄 <= 新股交付日`；跨公告的付款起迄與歧義日期均不猜測。
- 抽查另發現「2/22 股款收足、2/25 憑證上市」會被舊正則誤取 2/22。v3／v3 repeat
  已標記 `INVALID_DO_NOT_USE.md`，從未 promotion 或用於策略績效；v4 改成只接受緊貼
  上市／發放／交付語句的日期。
- `mops_paid_subscription_announcements_2004_2015_v4` 與 `_repeat` 由同一批 raw cache
  獨立重建，content SHA-256 均為
  `4CA57CD97146F538D2DD070AF38B15ED45A9489A561DBCE8EE574BB39DB7203C`。
- 288 筆付現增事件中，官方認購條件唯一配對由 112 增為 113；settlement 分類為日期全缺
  63、付款完整但交付缺失 27、日期歧義 16、交付存在但付款缺失 7。沒有任何一筆同時
  通過可信付款期與交付日，因此沒有把認購股數灌入持股，也沒有扣除認購款。
- 最新 ledger 維持 6,275 筆事件、5,622 executable、653 blocked；現增條件缺失 175、
  條件已齊但 settlement／認購執行語意未齊 113。內部數量仍固定為整數 shares，
  `common_lots=shares//1000`、`odd_lot_shares=shares%1000`，本輪沒有股／張混用。
- 大跳動稽核維持 138 筆公司行動、182 筆未解原因參考價重設、2 筆非普通股、2 筆
  新上市；短空窗未解釋仍為 0。這是資料缺口的負結果，不以放寬配對或反推股數解除閘門，
  D3 `promotion_ready=false`，MOM-1／backward holdout 仍未開封。

2026-08-11 本輪完整正式測試（`.venv-repro`）：553 passed、55 skipped、0 failed、4 warnings。

## D4：TWSE 已發行股數、市值與歷史產業 staging

### D4 第一輪：月末 issued shares／market cap 與年度 PIT 產業觀察

- 確認 TWSE 官方 `MI_QFIIS`「外資及陸資投資持股統計」自 2004-02-11 起提供；
  2005-01-03 歷史回應已含逐檔 `發行股數`，且 `hints` 明示單位為「股」。D4 parser
  只接受正整數股，禁止除以 1,000 或把欄位改稱張；空產業類別可無 unit hint，但非空
  回應缺「股」會直接失敗。
- 以 2005～2014 每月最後交易日建立 120 個 point-in-time snapshot。已發行股數每月查詢
  `ALLBUT0999`，市場價值只以同日未復權 `close * issued_shares` 計算；停牌而缺同日 close
  者保留 null，不以前次價格靜默 forward-fill。
- 產業分類來自同一份官方報表的類別查詢，不使用 FinMind 現值或 2026 公司基本資料回填。
  官方同時回傳 07「化學生技醫療」／13「電子工業」母類及 21／22、24～31 細類；matcher
  採細類優先，母類只補沒有細分類者，多重不相干細類會直接報錯。第一版為降低官方站
  負載，每年 1 月觀察一次並只向未來沿用，逐列保存 `industry_observation_date` 與
  `industry_stale_days`；沒有任何 observation 晚於 snapshot，但最長可陳舊 344 日，
  因此不能宣稱為每日精確 industry-as-of。
- staging 共 90,281 列、963 個普通股代號、120 個月。逐月相對 D1 active common-stock
  universe 的 issued-share 覆蓋最低 100%；非正股數 0、非整數股數 0。產業 PIT 覆蓋
  95.7200%，同日市值覆蓋 99.0197%，市值方程式最大誤差 0。
- `twse_market_structure_2005_2014_staging_v1` 與 `_repeat` 從 raw cache 獨立重建，
  content SHA-256 均為
  `1CEC86D976BDBA2FE5545CF8D10A8A64D62828546BEAE708058285B1B5E16F48`。
  `issued_shares_market_cap_ready=true`；因產業只年度觀察，`exact_industry_asof_ready=false`，
  整體 `promotion_ready=false`。
- 以 TWSE Fact Book 2010 所列 2005～2009 年底總市值做 1／1,000 倍單位稽核，逐年差異
  絕對值最大 0.8134%，通過 1% 門檻，排除把股誤當張。Fact Book 的「上市股數」與
  MI_QFIIS 的「發行股數」語意不同，股數總和差異不當作 parser 錯誤或強制調平。
- D4 跨主機 manifest：
  `reports/twse_market_structure_transfer_manifest_2005_2014_d4_20260811.json`，涵蓋 raw
  cache 與 v1 snapshot 共 1,435 個檔案、8,278,428 bytes；集合 SHA-256 為
  `21AE01A4CE9E6A8DB2E6FDC2D32A1E4520E6EF94DD755039A01AB35288DEC9D6`，本機驗證
  missing 0、mismatched 0、passed=true。
- 本輪只完成 D4 第一版資料與品質閘門，沒有打開 MOM-1／2008～2014 backward holdout。
  下一輪優先把產業觀察由年度升為月度（沿用已封存的部分月度 raw），並決定停牌股票
  市值是否以明確、可稽核的 last-observed 規則另建欄位；D3 未 promotion 仍是總閘門。

2026-08-11 本輪完整正式測試（`.venv-repro`）：557 passed、55 skipped、0 failed、4 warnings。

### D4 第二輪：月頻 industry-as-of 與可重現性完成

- 把 `MI_QFIIS` 產業查詢由每年 1 月提升為每個月末，2005～2014 共封存 3,840 份
  官方 JSON（120 個月 × `ALLBUT0999` 與 31 個產業代碼）。正規化後仍為 90,281 列、
  963 個普通股代號；逐月 issued-share universe 覆蓋最低 100%，非正／非整數股數均為 0。
- 90,281 列的 `industry_observation_date` 現在全部等於該月 snapshot date，最大 stale
  days 由 344 降為 0，`exact_industry_asof_ready=true`。官方產業欄位覆蓋由 95.7200%
  提升至 96.7446%；仍無官方類別者保留 missing，沒有用今天分類回填歷史。
- 同日未復權 close × issued shares 的市值覆蓋維持 99.0197%；停牌缺 close 的列繼續
  保留 null，不把舊價冒充同日市值。`issued_shares_market_cap_ready=true`、
  `d4_component_ready=true`，但 D3 未 promotion，因此整體 `promotion_ready=false`。
- `twse_market_structure_2005_2014_staging_v2` 與 `_repeat` 從同一組 raw cache 獨立重建，
  content SHA-256 均為
  `9327DD0F63DB1BCA440341BA270227EDA003A5A7834811C36AD9DA1AF796AC31`。
- D4 v2 跨主機 manifest：
  `reports/twse_market_structure_transfer_manifest_2005_2014_d4_v2_20260811.json`，
  3,845 個檔案、14,688,400 bytes，集合 SHA-256
  `BAA1DC6DE97F6D19C6C2F031325E679BCA6E907947600ECD141C0D56BC461698`；本機驗證
  missing 0、mismatched 0、passed=true。

## D5：TPEX 2008～2014 官方歷史 staging

### D5 第一輪：價量、universe、公司行動與交易限制

- 確認官方 `afterTrading/dailyQuotes` 可回溯至 2008，而不是只能從 2009 開始。以已封存
  臺灣交易日曆查 1,732 日，保留 1,015,339 個標準上櫃股票行情列、762 個四位數股票
  代號；逐日 quote presence 直接形成 PIT universe，沒有把 2026 現存清單回填到歷史。
- 官方早期回應會以 `0.00` 表示沒有有效成交價；v1 品質閘門抓出 10,896 個零價欄位後，
  v2 起一律改為 null，連同原始 `----` 共 23,085 個 missing-price／停牌候選列。列仍留在
  universe，價格不 forward-fill；OHLC 非正值、負成交股數、負成交金額均為 0。
- 價量單位固定為股與元。992,254 個有成交列的 `turnover / volume / close` 中位數為
  1.000546，最小 0.6595、最大 1.1475；若把股誤當張，中位數會接近 1,000。加上所有
  volume 與 issued shares 皆為整數，`share_volume_unit_sanity_passed=true`，本輪沒有
  股／張 1,000 倍錯置。少數隱含均價超出 regular-session OHLC，保留為包含其他交易
  時段／交易類型的稽核差異，不用改單位硬調平。
- `bulletin/exDailyQ` 逐月封存 2,855 筆除權息事件。TPEX 的「權值」是每股參考價扣減額，
  不是持股增加比例；新 schema 改存 `stock_dividend_value`，單位明示
  `TWD_per_share_reference_deduction`。`pre_close - ref_price - 權值 - 息值` 最大誤差
  0.005 元，沒有超過 0.011 元的列。
- 官方 `company/deListed` 年度端點補到 62 個下櫃代號；下櫃日後仍出現在標準行情的列為
  0。注意事件 8,102 筆、處置事件 564 筆；另封存 60,289 個逐日交易限制列，包含變更
  交易 46,877、分盤 23,578、管理股 7,600、停止交易 6,327 個日－證券觀察。
- 2410、5207、5414 的注意事件不在標準行情 universe，但逐日限制表明確標示為管理股，
  因此保留事件並排除可交易 universe，不為了通過檢查硬塞成普通上櫃股；未解釋事件代號
  為 0。
- 2008-04-09 起完整限制旗標來自 `afterTrading/chtm`；更早 62 個交易日使用櫃買官方
  `hist.tpex.org.tw` 的 Big5 `CHTM_YYMMDD.HTML`。舊檔只提供變更交易名單，因此 1,645
  列的其他旗標維持 unknown，不補 false。
- 2008～2014 每年都用已證實的第一個交易日 probe `insti/dailyTrade`，7 日均為 0 筆；
  schema 明示 `missing/unknown_not_zero`，不把不存在的法人觀察補 0，也不接回已否決來源。
- v1 因把官方零價當價格而 structural fail；v2 修正後通過；v3 再固定 nullable boolean／
  integer schema 並加入股／張 sanity gate。`tpex_history_2008_2014_staging_v3` 與
  `_repeat` 獨立 cached rebuild 的 content SHA-256 均為
  `CD5275B703A04DFD1B7B5D626E48BD1C08D3AB5B60563766292C9DB98332EB0A`。
- D5 v3 為 `structural_passed=true`、`d5_staging_complete=true`；仍因 2008 年初部分限制
  旗標 unknown、2018 前 TPEX 法人 missing、D3 execution ledger 未 promotion，維持
  `all_market_deployment_ready=false` 與 `promotion_ready=false`。本輪沒有開啟 MOM-1、
  全市場績效或 backward holdout。
- D5 跨主機 manifest：`reports/tpex_history_transfer_manifest_2008_2014_d5_20260811.json`，
  3,594 個檔案、143,492,042 bytes，集合 SHA-256
  `CA7B0C2F7D8A8856A274974918D7AA9B6FDF4A438D5D167CA2CC7309778F0701`；本機驗證
  missing 0、mismatched 0、passed=true。

2026-08-11 D4 v2／D5 v3 完成後完整正式測試（`.venv-repro`）：562 passed、55 skipped、
0 failed、4 個既有 warnings。

## D6：TWSE 處置歷史與跨機資料 release

- TWSE 官方 `punish` 端點 2005～2014 共封存 10 個年度回應、681 個 raw rows；固定
  JSON 編碼與 gzip `mtime=0`，相同 payload 可產生相同位元組。
- raw transfer manifest 共 10 檔、37,738 bytes，collection SHA-256：
  `0B49730946A78D9F8709A7C9F9D0B90B68F22DF87BCAF650EE3B425BF871C9FA`；逐檔驗證
  missing 0、mismatched 0、passed=true。
- 只保留可解析處置期間的四位數普通股，年度內 parser 後 620 source rows；跨年更正／
  重覆依 `(stock_id, start_date)` 留最後一筆後為 604 事件、194 個股票代號。
- `twse_disposition_punish_2005_2014_v1` 與 `_repeat` 從 clean commit `92c8480`
  獨立建置，兩者 content SHA-256 均為
  `025EEB28A2DEFE1DFC2082447A3E885211172878C808FB2462ACE04D498CAEFF`；
  `disposition_events.parquet` SHA-256 均為
  `63A04BA052C25881936B44A368B217D4E50F0E13A24EE4A11CD0ADFC6E0069A9`。
- snapshot manifest 明確為 `git.dirty=false`，raw manifest 驗證、來源年度完整性、事件期間
  方向與結構檢查全數通過，`twse_disposition_component_ready=true`。
- `notice` 不在 MOM-1 必要處置排除規則，本輪明確不下載；TPEX 處置由 D5 提供。
- 主 release `tw_stock_data_2005_2014_r1` 涵蓋 12,661 檔、543,984,330 bytes，
  collection SHA-256：
  `44EAF107359CA2754A7407A062E4ABD1A1F61A16298C6816E11994EEF604CD1E`；本機完整
  verify 與八個 component manifest／quality hash 全部通過。
- 此 release 核准跨機 identity、PIT engine correctness、signal count 與 disposition
  filter；D3 actual-share ledger 仍有 blocker，因此 backward holdout 績效仍未開封。

## D6 第二部分：TWSE 變更交易方法／全額交割（2026-08-12 晚）

`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6 的不可交易排除有兩半：處置由
D6 第一部分提供，本輪補上另一半。此項先前是 `reports/mom1_f0_execution_readiness.json`
記錄的 remaining blocker 之一（TPEX 有 D5 的 `trading_restrictions`，TWSE 沒有等價物）。

- 官方來源：`https://www.twse.com.tw/exchangeReport/TWT85U`，TWSE OpenAPI 目錄
  登記為「集中市場證券變更交易」。實測 `date` 參數可回溯至 2005-01-03，
  逐交易日一份完整名單。先前未使用此來源，並非它不存在。
- 交易日曆取自已凍結的 D2 `twse_prices_2005_2014_v1`，不另外猜測開休市。
  2,469 個交易日全部抓取成功，`downloaded=2469`、`failures=0`、`missing=0`，
  逐年日數 247／247／243／249／248／250／247／247／244／247 與 D2 完全一致。
- 原始回應 53,159 筆觀察列。raw 沿用處置回補慣例：固定 key 順序、緊縮
  separators、gzip `mtime=0`；同一日以 `--force` 重抓，位元組完全相同。
- raw transfer manifest：`reports/twse_altered_trading_transfer_manifest_2005_2014_20260812.json`，
  2,469 檔、1,897,555 bytes，collection SHA-256：
  `5F8F4AA1D2C1DBE03404F8AFE4728389C846846335B5760F62A309588326F00A`；
  逐檔驗證 missing 0、mismatched 0、passed=true。

### 跨年代 schema 變遷（保留，未合併）

官方報表在期間內換過名稱與欄位，本輪刻意保留而不抹平：

| 期間 | 標題 | 欄位 | 交易日 | 觀察列 |
|---|---|---|---:|---:|
| 2005-01-03 ～ **2007-10-31** | 全額交割證券 | 證券代號、證券名稱 | 694 | 15,754 |
| **2007-11-01** ～ 2014-12-31 | 變更交易 | 加上「分盤集合競價(以**表示)」 | 1,775 | 37,405 |

`altered_trading` 在整段期間語意一致，可直接用於 §7.1.6 排除。
但 `periodic_call_auction` **只有後期揭露**，因此早期 15,754 列一律為 `NA`
而非 `False`——依 §2.2.5「缺資料不等於 0」。補 `False` 會把「當時不揭露」
誤述成「當時沒有分盤」，且方向剛好讓回測誤以為那些股票比實際更好成交。
後期 37,405 列全部有值，其中 16,932 列為分盤集合競價。

parser 遇到未知欄位組合直接 raise，不以欄位順序猜測；官方 `stat` 非 OK 亦直接
拋出，不得視為「當日無變更交易」——查詢失敗與名單為空語意不同，混淆會讓
缺漏的日子看起來像乾淨的日子。

### 品質閘門

- `twse_altered_trading_2005_2014_v1` 與 `_repeat` 從 clean commit `003b323`
  獨立建置，兩者 content SHA-256 均為
  `AFCD5A2DDA5A42F12B96A5C97AA63F5FDC62AB07DAD9FCC890CA63EA21B18E10`。
- snapshot manifest `git.dirty=false`；53,159 列、209 個證券代號；
  `(snapshot_date, stock_id)` 重複 0；交易日覆蓋 2,469/2,469；
  官方 `stat` 全為 OK；早期分盤欄位確認全部缺值。
- `twse_altered_trading_component_ready=true`、`promotion_ready=true`。
- 品質閘門要求交易日**全覆蓋**：缺一天 raw 即 `promotion_ready=false`。
  理由是官方每個交易日都發佈完整名單，缺的那天排除規則等於失效，
  與其讓那天看起來乾淨，不如直接擋下。

### 已知缺口

- 早期不揭露分盤集合競價旗標，該段為 missing，永遠不是 False。
- TWSE「停止交易」沒有獨立官方旗標；目前間接由「當日無行情列」處理。
- TPEX 變更交易歷史由 D5 快照提供，不在本元件範圍。

### 尚未做的事

本元件**尚未納入任何 release**。`tw_stock_data_2005_2014_r1` 為
`immutable_research_component_bundle`，不得就地追加；要讓 MOM-1 實際套用
此排除規則，需發佈 r2 並更新部屬機文件的預期雜湊與 bundle。本輪沒有
修改 r1、沒有動 `data/research`、沒有查看任何 holdout 績效。

2026-08-12 本輪測試（`.venv-repro`，排除兩個需 DB 連線的模組）：
662 passed、0 failed、4 個既有 warnings。
