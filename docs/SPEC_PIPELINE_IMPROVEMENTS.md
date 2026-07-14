# 規格:決策透明化 × 資料品質 × 效能(五項改進)

> 狀態:**已定稿(2026-07-13 使用者確認四個決策點+補充儲存限制),照此實作**。
> 起因:使用者提出五個改進方向;本規格逐項給出「採用/修正/暫緩」
> 的立場、設計、驗收標準。已確認決策見文末。
> 原則:**可觀測先於優化、驗證先於學習、預設不增加 LLM 成本**。

## 0. 現況事實(2026-07-13 查證)

- `pipeline_logs` 表存在(task_name/started_at/finished_at)**但是空的**——沒有任何程式寫它。
  「20分鐘花在哪」目前零數據。
- `daily_recommendations` 只存最終裁決(rank/score/reason/signals)。**多方/空方辯論文本
  生成後即丟棄**,未持久化——黑盒子感的直接原因。
- 因子分數(score_candidates 明細)、市場濾網狀態、候選池篩選過程:都有算,都沒留。
- 實際踩過的資料坑(0050分割、invest_net單位、投信連買雜訊、smallint溢位)全部是
  **規則檢查可抓的結構性錯誤**,無一是「來源可信度加權」能防的。

---

## Phase A:Execution Log(決策軌跡)+ 逐段計時 —— 地基,第一個做

### 目標
回答「AI 為什麼選這檔」與「20分鐘花在哪」。兩者共用同一套儀表。

### 設計
新表 `execution_log`(migration 15):

| 欄位 | 型別 | 說明 |
|---|---|---|
| run_id | UUID | 一次 pipeline 執行一個 id |
| stage | VARCHAR | 見下方階段列表 |
| seq | SMALLINT | 階段順序 |
| started_at / finished_at | TIMESTAMPTZ | **逐段計時(第5點的量測來源)** |
| model_calls / tokens_in / tokens_out | INT | LLM 用量(非 LLM 段為 0) |
| sources | JSONB | 本段用到的資料來源+信心分數(Phase B 後有值) |
| summary | TEXT | 人話小總結(1~3句,供 UI 直接顯示) |
| payload | JSONB | 完整中間產物(辯論全文、因子分數明細、候選清單…) |
| status / error_msg | | 成功/失敗/略過 |

階段(對應現有 pipeline,不重構流程,只加記錄):
```
data_ingest → quality_gate(Phase B 加入) → sector_momentum → factor_screen
→ debate_bull → debate_bear → judge → risk_gate(濾網/部位上限/張數)
→ orders(掛單) → fills(隔日回帳) → notify
```

- `factor_screen` 的 payload 存**每檔候選的因子分數明細**(rs20/stack_days/invest_streak/
  mom60/rev_yoy 各得幾分)——「為什麼是這20檔」從此可查。
- `debate_*` 的 payload 存**辯論全文**;summary 用同一次 LLM 呼叫的結構化輸出附帶產生
  (prompt 加一個 `summary` 欄位要求),**不多花一次呼叫**。
- `judge` 存裁決全文 + 每檔入選理由 + 被空方否決的股票與原因。

### 呈現
- Streamlit 新頁「🔍 決策軌跡」:選日期 → 依 seq 渲染流程(每段:小總結 + 耗時 + 來源
  信心分數,可展開看 payload)。🔶 決策點1:獨立新頁(預設) vs 掛在既有推薦頁下方?
- Telegram 日報末尾附一行:「決策軌跡:{streamlit url}」不塞長文。

### 儲存預算與保留策略(使用者補充的第5點:Neon 已 0.36/0.5GB)
- **payload 紀律**:factor_screen 只存前 20 檔候選的因子明細+池統計(不存全市場 1855 檔);
  辯論/裁決存全文(每篇數 KB)。估算:**~20KB/run → 180 天 ≈ 3.6MB**,佔剩餘空間 2.6%,可控。
- **自動輪替**:每次 pipeline 開始時 `DELETE FROM execution_log WHERE started_at < now()-'180 days'`,
  不需人工清理。180 天為常數 `EXEC_LOG_RETENTION_DAYS`,可調。
- **為什麼存 Neon 不存本地**:Streamlit Cloud 只能讀 Neon,決策軌跡頁要能看就必須在雲端;
  本地只適合放回測用冷資料(既有的冷熱分層待辦,與本表無關)。若之後想保留 >180 天的軌跡,
  可在冷熱分層腳本裡順手把過期 execution_log 歸檔到本機 parquet 再刪(選配,不擋本期)。

### 驗收
- 跑一次 pipeline 後,`execution_log` 有完整一輪 run 的所有階段,總耗時分解可加總到
  接近實際 wall-clock;Streamlit 能從資料來源一路點到最終委託。
- 額外 LLM 呼叫數:0(只允許在既有呼叫上加結構化欄位)。
- 單次 run 寫入量 ≤ 100KB(防 payload 失控)。

---

## Phase B:資料品質層 —— 量化採用,模型學習明確暫緩

> **狀態:已實作(2026-07-14)**。`agent/quality_gate.py` 三條規則檢查 + `discrepancy_log`
> 表(migration 17)+ 來源記分卡,接進 `run_pipeline.py` 的獨立 `quality_gate` stage
> (data_ingest 之後、market_intel 之前),決策軌跡頁「01 資料與候選」欄同步顯示信心
> chips 與觸發明細。13 個新測試(純函式含真實 0050 分割數值重演 + DB 整合)。
> 對正式資料庫實跑:0 項異常、4 個來源皆信心 1.0(乾淨起始狀態,之前的坑已在
> 別處修過)。**誠實落差**:2025-12-03 那個單日異常沒有另外寫回放測試(邏輯與
> 0050 分割共用同一門檻,理論上會抓到,但沒有逐一針對那個日期驗證)。

### 立場修正(重要)
核心行情/法人資料來源是證交所官方=ground truth 本身,**沒有多來源競爭問題**;
需要品質管理的是:我們自己的轉換管線(歷史上全部的坑都在這)、以及真正多來源的
ETF持股/新聞/營收。因此本 Phase 是「驗證器+記分卡」,不是「來源加權模型」。

### 設計
1. **來源註冊表**(靜態 YAML/常數即可):每種資料類型列出來源、更新頻率、已知弱點。
2. **規則驗證器**(pipeline 的 `quality_gate` 段執行,結果寫 execution_log.sources):
   - 單日|漲跌|>20% 且非公司行動 → 疑似分割/資料錯(0050 案例的通則化)
   - 法人買賣超單位/正負號合理性;跨源抽查(ETF:MoneyDJ vs 官網;價格:TWSE vs 回補值)
   - 缺值率/停牌數異常(相對近30日基線)
3. **discrepancy_log 表**:每次驗證器觸發記一筆(來源、欄位、期望vs實際、人工裁決結果)。
   這就是未來「學習權重」的訓練資料——**現在開始累積,累積夠了才談模型**。
4. **來源記分卡**:近30日每來源的觸發次數/錯誤率 → 轉成 0~1 信心分數,餵進
   execution_log.sources 顯示(第2點要的「來源+信心分數」在這裡閉環)。

### 暫緩項(明確)
- ❌ 現在就用模型學來源權重:無標註資料,學了是幻覺。**觸發條件**:discrepancy_log
  累積 ≥300 筆含人工裁決的紀錄後,重開此議題(先從 logistic regression 這種可解釋的開始)。

### 驗收
- 對歷史資料回放驗證器,能抓到已知的 0050 分割案例與 2025-12-03 異常日。
- 記分卡在 Streamlit 決策軌跡頁的 data_ingest 段可見。

---

## Phase C:引用驗證(anti-hallucination 主力)+ 裁決穩定性量測

### C1. 引用驗證(grounding check)
- 裁決/辯論的結構化輸出要求:每個論點附 `cited_fields`(引用了候選表的哪些欄位值)。
- 程式驗證:輸出中出現的每個數字,必須能在輸入資料中找到對應(容差內)。
  不符 → 該論點標 ⚠️(UI 紅字),嚴重(>N個不符)→ 該次裁決重生成一次。
- **立場**:RAG 不是防幻覺的主力,驗證才是。此項優先於任何 RAG 建設。

### C2. 裁決穩定性量測(best-of-N 的前置)
- 連續 5 個交易日,每天用同一份資料**多跑 2 次裁決段**(共3次,不重跑辯論),
  記錄 top-5 重疊率與理由一致性。成本:每天 +2 次 Gemini 呼叫,+~1分鐘。
- **決策規則**(🔶 決策點2,預設如下):
  - 平均重疊 ≥ 4/5 → 穩定,**不做** best-of-N(省成本)
  - 平均重疊 < 3/5 → 不穩定警訊,採 **N=3 多數決**(只在裁決段)常態化
  - 介於中間 → 帶數據回來再討論
- 誠實聲明:LLM 層無法回測,本項可驗證的是**穩定性與引用錯誤率**,不是報酬。

### C3. RAG 檢索增強(縮小範圍後採用)
- 場景:辯論前,為每檔候選檢索 `market_signals` 近30日相關新聞/YT摘要(related_stocks
  匹配 + 標題關鍵字),前 3 筆餵進辯論 context。
- 語料只有數百筆:**先用 SQL 關鍵字檢索**,不建 embedding 基礎設施。
  🔶 決策點3:若之後語料大了(老師資料/更長新聞史),再評估 pgvector(Neon 原生支援)。

---

## Phase D:效能優化 —— 等 Phase A 的數據,不先猜

- 現況零數據;Phase A 上線一週後,自動得到每段耗時分布,屆時針對 top-2 慢段優化。
- 已備好的候選手段(依假設的嫌疑度排序,待數據證實):
  1. 新聞逐篇 LLM 摘要 → 已摘要過的 URL 不重摘(快取表)
  2. debate/judge 重試退避(15/30/45s)→ 檢討必要性與上限
  3. 獨立 fetchers(新聞/YT/美股/ETF)並行化(ThreadPool)
  4. GitHub Actions 免費 runner 本身慢 → 無解,但可把非決策關鍵段(YT/新聞)移到
     獨立 workflow 錯峰跑
- **時間預算**(驗收基準):決策關鍵路徑(data→orders)≤ 10 分鐘;全 pipeline ≤ 20 分鐘
  (含新增功能)。新增各項的預算:A +0、B +30秒、C1 +0(同呼叫)、C2 +1分鐘(僅量測週)。

---

## 實施順序與依賴

```
A(execution_log+計時) ─┬→ B(驗證器,信心分數餵進A的sources)
                        ├→ C1(引用驗證) → C2(穩定性量測) → [數據決定] best-of-N
                        ├→ C3(關鍵字RAG,獨立可平行)
                        └→ D(效能,等A的數據)
模型學來源權重:defer,觸發條件=discrepancy_log≥300筆
```

與現有工作線的關係:不影響「老師資料匯入」「券商API開通」兩條線;
Phase A 的 orders/fills 段直接記錄 broker 層(paper/shioaji 通用)。

## ✅ 已確認的決策(2026-07-13)

1. 決策軌跡:Streamlit **獨立新頁**。
2. C2 穩定性閾值:**接受**(≥4/5 不做、<3/5 做 N=3 多數決)。
3. RAG:**接受**(先 SQL 關鍵字檢索,pgvector 等語料變大再評估)。
4. Phase 順序:**接受** A→B→C→D。
5. (使用者補充)Neon 空間緊繃 → 採 payload 紀律 + 180 天自動輪替(見 Phase A 儲存段),
   估算 3.6MB 穩態,不需人工刪除、不需搬本地。
