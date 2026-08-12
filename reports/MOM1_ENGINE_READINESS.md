# MOM1-0：引擎正確性與 PIT universe 稽核

> 日期：2026-08-12（Asia/Taipei）
> 指派來源：`CLAUDE.md`、`docs/AI_COLLABORATION_PLAYBOOK.md` 工作分配表 MOM1-0
> 規格來源：`docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7（固定策略規則）、§9.1（F0）
> 狀態：**F0 的訊號建構部分已可測試；F0 整體未通過，F1 未開封。**

---

## 0. 交接摘要

| 項目 | 內容 |
|---|---|
| Branch | `research/mom1-engine` |
| 基底 commit | `8c92938`（`origin/agent/swing-margin-research`） |
| Worktree | `../tw_stock_advisor_claude`，與 Codex 的 Data Authority checkout 完全分離 |
| 新增 | `research/momentum.py`、`tests/test_momentum.py` |
| 修改 | `scripts/analyze_momentum_signal_overlap.py` |
| 資料釋出 ID | **`fixtures-only`**（見 §1） |
| 是否查看 holdout 績效 | **否。** 未計算任何報酬、NAV、Sharpe、CAGR、回撤、勝率或參數組合排名。 |

本輪沒有下載任何資料，沒有寫入 `data/`，也沒有修改任何 manifest 或 snapshot。
對 `data/research_versions/` 的存取一律唯讀，衍生輸出寫在 worktree 外的暫存目錄。

---

## 1. 資料釋出識別（重要）

依 `docs/AI_COLLABORATION_PLAYBOOK.md`「資料釋出閘門」，DATA-3 尚未發佈 release
descriptor，因此**目前不存在可引用的 `data_release_id`**。

- **單元測試的資料釋出 ID 為 `fixtures-only`**。`tests/test_momentum.py` 完全使用
  合成資料，不讀取任何 snapshot，這是本輪唯一具備驗收效力的證據。
- §4 的診斷數字讀取了下列**未 promotion 的 staging snapshot**。它們存在於磁碟上
  這件事不使它們成為 released dataset，因此以下數字是**診斷**，不是研究結論：

| Snapshot | content SHA-256 | 狀態 |
|---|---|---|
| `twse_prices_2005_2014_v1` | `6E04C1E3…CBE59` | price-only，明示不可直接策略回測 |
| `twse_security_master_2005_2014_staging_v1` | `983B1B51…361ED` | `promotion_ready=false` |

兩者 manifest 記錄的建構 commit 均為 `bf58807`。

> **CLAUDE.md 的完成定義有一項現在無法滿足**：「Every diagnostic is reproducible
> from a named immutable data release」。在 DATA-3 發佈 release descriptor 之前，
> 本機 staging snapshot 只有 content hash，沒有 release ID、沒有覆蓋宣告、
> 也沒有可從第二個 worktree 執行的驗證指令。本報告因此把單元測試（`fixtures-only`，
> 完全可重現）與 staging 診斷（可用 hash 指認，但不是 release）分開陳述，
> 而不是假裝後者已滿足該條件。

---

## 2. 執行的指令與結果

```bash
# worktree 建立（依 playbook §「Create the Claude worktree」）
git worktree add ../tw_stock_advisor_claude -b research/mom1-engine \
    origin/agent/swing-margin-research

# 新增的 F0 單元測試（fixtures-only）
python -m pytest tests/test_momentum.py -q
# → 37 passed，0 warnings

# 既有測試套件（.venv-repro，即 execution log 使用的官方環境）
../tw_stock_advisor/.venv-repro/Scripts/python.exe -m pytest -q \
    --ignore=tests/test_app_pages_render.py \
    --ignore=tests/test_portfolio_orders.py
# → 595 passed、0 failed、4 warnings，41.76 秒（含本輪新增的 37 筆）
```

### 2.1 既有測試套件：結果與兩個環境限制

**595 passed、0 failed、4 warnings**（4 個 warning 為 execution log 已記錄的既存項目，
本輪沒有新增任何 warning）。`tests/test_momentum.py` 的 37 筆已包含在這個數字內。

有兩個模組（合計 25 筆）被排除，兩者都是**環境限制，與本輪變更無關**：

- `tests/test_app_pages_render.py`、`tests/test_portfolio_orders.py` 會嘗試建立
  資料庫連線。本次執行環境沒有對外網路，這些測試不會失敗，而是**阻塞在連線逾時**：
  含它們時套件跑 25 分鐘仍停在 46%，排除後全套只要 **41.76 秒**。

另一個值得 Data Authority 知道的分歧：主 checkout 的 `.venv` 缺少 `pypdf`，
使 `tests/test_twse_security_master.py` 在 collection 階段即 ImportError 並中斷整個
套件；`.venv-repro` 有該套件。上述結果一律以 `.venv-repro` 執行。

**變更影響半徑可證明為零**：全 repo 搜尋確認除了新增的 `tests/test_momentum.py`
之外，沒有任何模組 import `research.momentum`；`scripts/analyze_momentum_signal_overlap.py`
也沒有任何 importer 或既有測試。因此被排除的 25 筆不可能受本輪變更影響。

---

## 3. 程式碼缺陷（本輪發現並修正）

### 3.1 ETF 判定使用代號前綴啟發式（CLAUDE.md 指派第 1 項）

`scripts/analyze_momentum_signal_overlap.py` 原本以

```python
& ~adj.columns.str.startswith("00")        # 排除 ETF 代號段（近似，非 PIT）
```

判斷普通股。這違反 SPEC §D1「ETF／ETN／權證／特別股不得靠代號猜測排除」與
§7.1.1。已改為 D1 security master 的 `asset_type` 官方分類欄位，並以
`effective_from <= d < delisting_date` 做 point-in-time 掛牌判定。

**實測影響**（唯讀診斷，只算成員數，不算報酬）：

| 判定分歧 | 檔數 |
|---|---|
| 啟發式判為普通股、PIT 判為非普通股 | **50**（存託憑證 24、非公司證券 26） |
| 啟發式判為非普通股、PIT 判為普通股 | 0 |

120 個月末**每一個月**的候選池都被污染，中位數多收 22 檔（範圍 11～33）。
存託憑證尤其危險：它的價格由境外母股與匯率驅動，混進中期動能排名等於在測
一個不同的資產類別。

**但結論方向不變。** 套用 §7.1 其餘濾網（價格、252 日歷史、流動性前 50%）後，
多數非普通股本來就被流動性門檻擋掉，因此 `docs/SPEC_REVERSAL_AND_MULTI_STRATEGY.md`
§1.3 的正交性結論**維持成立**：

| 對照因子 | Spearman 均值（啟發式 → PIT） | 前 10% 重疊率（啟發式 → PIT） |
|---|---|---|
| `rs20` | -0.0058 → **-0.0052** | 14.92% → **15.12%** |
| `mom60` | +0.4833 → **+0.4834** | 39.74% → **39.74%** |
| `stack_days` | +0.1626 → **+0.1628** | 18.04% → **18.02%** |

期間與月份數不變（2006-01-25～2014-12-31、108 個月），中位數 universe
由 299 降為 294。

換句話說：**方法是錯的，但它支撐的那個結論剛好穩健。**這兩件事必須分開陳述——
如果只看結論沒變就保留舊寫法，下一個沒這麼幸運的因子就會出事。

> 本輪**未**重新產生 `reports/momentum_signal_overlap_2005_2014.json`。
> 依 AGENTS.md「只有當前指派明確列為交付物時才提交產出報告」，該檔不在 MOM1-0
> 交付清單內，其內容歸 Data Authority 決定何時重跑。上表的 PIT 欄位是本輪
> 唯讀診斷的結果，供 Codex 判斷是否值得重新發佈該檔。

### 3.2 百分位切門檻會讓 §7.2 的 tie-break 永遠不生效

實作 §7.2 時先用了 `rank(pct=True, method="average")` 再切 `> 0.9`。單元測試立刻
反證：同分股票會拿到**相同**百分位，於是一整團同分只能整團進、整團出，
規格明文要求的「同分時以 `stock_id` 升冪 deterministic tie-break」永遠沒有機會執行。
改用 `method="first"` 也不行——那會讓名次取決於 DataFrame 的欄位排列順序。

已改為建立 `(訊號降冪, stock_id 升冪)` 的顯式全序後取名次
（`research/momentum.py` 的 `deterministic_rank`），使 tie-break 真正決定同分時誰入選，
且與輸入順序無關。`rank_percentile` 保留為 §7.2 字面意義的診斷用函式，但不參與選股。

### 3.3 門檻邊界的系統性超收

以 `pct >= 0.90` 取「前 10%」在小 universe 會系統性多收：n=10 時會選出 2 檔，
但前 10% 只有 1 檔。已改為 `floor(n × 10%)` 名額制，並以 1e-9 epsilon 吸收
二進位浮點誤差（例如 `n × 0.1` 落在整數下方時 `int()` 會少算一個名額）。

### 3.4 流動性分位數用錯了母體 — **影響 universe 大小約 12%**

SPEC §7.1.5 的文字是「過去 20 日平均成交金額位於當日 **TWSE 普通股** 前 50%」，
母體是當日全體普通股。但原 overlap 腳本（以及我的第一版引擎）是**先**套用
價格 >= 10 元、252 日歷史等門檻，**再**在剩下的子集裡取中位數。

這不是等價的：低價股與短歷史股的成交金額通常也偏低，先把它們剔除會讓剩餘分布
整體右移，中位數被抬高，於是流動性門檻比規格更嚴、universe 比規格更窄。

**實測影響**（唯讀診斷，只算成員數）：

| 分位數母體 | 月末 universe 中位數 |
|---|---|
| 規格字面（當日全體 PIT 普通股） | **326.5** |
| 先過濾再取分位數（原寫法） | 291.0 |

每月中位數相差 **36 檔**（範圍 0～71），120 個月中有 108 個月不同。
前 10% 名額因此由 29 個變成 32 個，**會選到不同的股票**。

引擎已改為規格字面的母體（`research/momentum.py::eligible_universe`），並有兩個
測試釘住語意：被價格門檻剔除的股票**仍算在分母內**，非普通股**不算在分母內**。

> ⚠️ `scripts/analyze_momentum_signal_overlap.py` 仍沿用原本較窄的母體。
> 本輪只依指派修正該腳本的 ETF 判定，未改動它的流動性語意，以免同時變動兩個
> 因素而無法歸因。**`research/momentum.py` 是 MOM-1 universe 的唯一權威實作**，
> 該腳本是診斷工具。若 Codex 決定重跑 §1.3，建議一併改用引擎的 `eligible_universe`。

---

## 4. 資料缺陷（回報 Data Authority，本輪未自行修補）

依 CLAUDE.md 邊界，以下一律回報而不代理、不猜值。

### 4.1 §7.1.6「不可交易排除」在 holdout 期間完全無資料 — **阻擋 F0**

SPEC §7.1.6 要求排除處置、停止交易、全額交割等無法按模型成交的股票。實測：

- TWSE 處置事件最早為 **2014-12-23**（`research_v20260811_current_bf58807`），
  2008～2014 holdout 期間幾乎沒有樣本。
- **TWSE 完全沒有 `trading_restrictions.parquet`**。唯一存在的是 TPEX 的
  2008～2014 版本（60,289 列），而 MOM-1 第一版的 universe 是 TWSE 普通股。

因此 MOM-1 目前**無法**套用 §7.1.6。這與 `docs/SPEC_REVERSAL_AND_MULTI_STRATEGY.md`
§2 記載的處置歷史缺口是同一個根因，但影響範圍更大：它不只擋住 REV-1A 的事件母體，
也擋住 MOM-1 的 universe 契約。

引擎的處理方式：`eligible_universe()` 在未取得 `restricted` 時**強制** raise，
呼叫端必須顯式傳 `allow_missing_restrictions=True` 才能繼續。這讓「這條規則還沒被
套用」永遠是一個刻意的、可稽核的決定，而不是預設值悄悄跳過。

### 4.2 產業 PIT 覆蓋為 0% — 阻擋 §7.4 正式驗收

`twse_security_master_2005_2014_staging_v1` 的 `quality_report.json` 記載
`industry_pit_coverage: 0.0`；實測 `industry_code_asof` 1,042 列**全部為空**、
`industry_is_point_in_time` 全為 False、`stock_universe_history.industry_code` 亦全空。

SPEC §7.4 明定「同一產業最多 3 檔／30%。若產業資料不是 point-in-time，
阻擋正式驗收」。D4 staging v2 已把 issued shares／market cap 補到月頻且
`exact_industry_asof_ready=true`，但那是 `twse_market_structure_2005_2014_staging_v2`
的欄位，尚未併入 D1 security master，MOM-1 引擎目前拿不到。
**建議：D4 的月頻產業欄位併入 D1 或另外發佈成 release 欄位。**

### 4.3 `effective_from` 有 239 檔來自「首次官方成交日」而非官方上市日

D1 保證普通股 `effective_from` 覆蓋 100%，但 `effective_from_source` 顯示
803 檔來自「TWSE current company listing date」、**239 檔**來自
「first observed TWSE official daily quote」。

本引擎採用 `effective_from` 而非 `listing_date`（後者 964 檔普通股中有 171 檔為空）。
理由：拿有缺值的 `listing_date` 當閘門會把有官方行情的股票整段剔除，等於用資料缺口
自製倖存者偏誤。`effective_from` 的退回方向是保守的（不會讓股票比實際更早進入
universe），且來源逐列可稽核。**這是一個已知的精確度折衷，不是缺陷隱藏。**

---

## 5. 待決研究選擇（需要使用者或 Codex 裁決，本輪未自行決定）

### 5.1 持股上限與 buffer 相衝時，誰讓位？

§7.2 給出 10%／20% buffer，§7.4 給出最大 10 檔，但**沒有規定兩者衝突時的優先序**。
以中位數 294 檔的 universe 計算，前 10% 有 29 檔候選，遠超過 10 個名額，
所以這個衝突**每個月都會發生**，不是邊角案例。

本引擎目前的判讀：**先保留仍在前 20% 的既有部位，剩餘名額才給新進場者。**
依據是 §7.6 已把賣出觸發條件列舉完畢，「排名跌出前 20%」是唯一與排名有關的賣出理由；
若讓新進場者擠掉一檔仍在前 20% 的持股，等於新增一條規格沒有的賣出規則，
也抵銷 buffer 降低換手的目的。

**但另一種判讀（永遠持有動能最強的 10 檔）同樣說得通，且會產生不同的交易序列。**
這必須在 F1 開封前定案並寫入規格，否則就是事後選擇。

### 5.2 流動性窗缺值的處理

§7.1.5 要求「過去 20 日平均成交金額」，§7.1.7 要求缺值不得補 0，但沒說停牌幾天的
股票該怎麼算。本引擎要求 20 個 session **全部有值**，否則淘汰。理由是對缺值改用
較短窗平均等於默默放寬流動性門檻。代價是停牌一天的股票會被排除 20 個交易日。
這個嚴格度需要確認。

### 5.3 名額取整方向

`floor(n × 10%)` 是「只取前 10%」的保守讀法（不超收）。n 較小時名額可能為 0，
此時依 §7.4「候選不足時保留現金」不補滿。舊 overlap 腳本用的是
`max(1, round(n × 10%))`，兩者在 n=294 時分別為 29 與 29，目前無差異，
但語意不同，應統一。

---

## 6. F0 覆蓋對照（SPEC §9.1）

| §9.1 要求 | 狀態 | 證據 |
|---|---|---|
| 動能排名、skip-month、10%／20% buffer 單元測試 | ✅ | `test_momentum.py` 排名與 buffer 段 |
| 訊號日期後移一日成交，無 look-ahead | ⚠️ 部分 | `next_session` 已測（含樣本尾端回傳 `None`）；**成交價與成本尚未實作** |
| universe listing／delisting 邊界測試 | ✅ | `test_pit_mask_respects_listing_and_delisting_boundaries` |
| 公司行動前後 total-return 連續性測試 | ❌ | 屬 D3，未 promotion，本輪不做 |
| shares／lots 單位測試與 broker split 測試 | ❌ | 屬 sizing／執行層，本輪不在範圍 |
| 相同輸入兩次結果完全一致 | ✅ | `test_universe_is_sorted_and_deterministic`、tie-break 三種輸入順序測試 |

**F0 未通過。** 缺口為：公司行動還原連續性（等 D3）、成交與 sizing 層（尚未實作）、
以及 §4.1 的不可交易排除（等資料）。

新增測試涵蓋 CLAUDE.md 指派第 3 項的五個面向：

- **時間對齊**：月末取實際交易日而非日曆月底；位移以交易日列數而非日曆天數
  （跨 30 天空窗仍正確）。
- **缺失交易日**：月末日被移除時退到前一個交易日；沒有交易日的月份不產生決策日；
  `next_session` 跨越停市空窗。
- **universe 成員資格**：`asset_type` 勝過代號（刻意用 `0050`＝普通股、
  `1234`＝TDR 的反例，舊啟發式兩邊都判錯）；掛牌與下市邊界；master 查無者排除；
  兩條 PIT 路徑（interval master 與 monthly universe history）在月末互相對帳。
- **濾網**：價格門檻用未還原價；252 日歷史；流動性前 50%（含分位數母體語意的
  兩個反向測試，見 §3.4）；流動性窗缺值淘汰；不可交易排除；
  缺 restricted 表時強制 raise。
- **look-ahead 洩漏**：竄改決策日之後的所有價格與成交金額後，訊號與合格清單
  必須逐位元不變；universe history 不得讀未來 snapshot。

---

## 7. 本輪明確**沒有**做的事

- 沒有計算任何報酬、NAV、Sharpe、CAGR、回撤、勝率或基準比較。
- 沒有開封 2008～2014 backward holdout。
- 沒有實作成交、滑價、成本、sizing 或出場——`research/momentum.py` 刻意不含績效函式，
  避免「一次 import 就意外開封」。
- 沒有下載、修改或修補任何原始資料、manifest 或 snapshot。
- 沒有動 Codex 在 Data Authority checkout 的未提交工作。

---

## 8. 建議的下一步

| 優先序 | 工作 | 擁有者 |
|---|---|---|
| 1 | 裁決 §5.1 持股上限 vs buffer 的優先序，寫回規格 | 使用者／Codex |
| 2 | 補 TWSE 2005～2014 處置／停止交易／全額交割歷史（§4.1） | Codex（DATA-2） |
| 3 | 把 D4 月頻產業欄位併入 D1 或發佈為 release 欄位（§4.2） | Codex |
| 4 | 發佈 DATA-3 release descriptor，讓 `fixtures-only` 可升級為具名釋出 | Codex |
| 5 | 在 1～4 完成後實作成交／sizing 層並補完 F0 剩餘項目 | Claude |

在第 4 項完成前，MOM-1 只能繼續以 fixtures 驗證邏輯，不得把本機 staging 當研究結果。
