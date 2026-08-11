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
