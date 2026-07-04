# 開發待辦清單（給執行者的詳細規格）

> 本文件是**可直接照做的任務規格書**。每個任務含：目標、要動的檔案、具體步驟、驗收標準。
> 依序做 P0 → P1 → P2。新功能三個 Epic（A/B/C）互相獨立，可穿插進行。

## 進度（2026-07-04）

- ✅ 已完成：P0-1（回測交易成本）、**P0-2（消融測試 → 已採用 G 方案：關均線三規則+停利30%，勝率 32%→43.6%、持有 3.7→19.8 日，詳見 scripts/ablation_result.md）**、P0-3（彙整去雜訊）、P0-4（workflow 依賴收斂，FinMind 依使用者要求保留）、**Epic A（我的持倉）**、**Epic B（追蹤清單）**、**Epic C（族群輪動）**。Migration 07~09 已直接在 Neon 執行完畢。
- ⬜ 待做：P1-1~P1-5、P2 全部。另注意：現行參數以多頭年調校，市況轉空時優先回測開回 exit_on_death_cross（見 ablation_result.md 注意事項）。

---

## ⚠️ 執行須知（動手前必讀）

以下是本專案已踩過的坑，違反任何一條都會重蹈覆轍：

1. **本機 Python**：用 `py -3.12` 執行（`python` 指到 Windows Store 殼層會失敗）。
2. **主控台中文亂碼**：loguru 輸出在 PowerShell 顯示亂碼是正常的（cp950），不影響功能；自己寫測試腳本時開頭加：
   ```python
   import sys, io
   sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
   ```
3. **DB migration**：Neon 不會自動跑 migration。每次新增/修改資料表，要在 `database/migrations/` 建 `NN_xxx.sql`（編號接續，目前到 05），**並提醒使用者去 Neon SQL Editor 手動執行**。`market_signals.signal_type` 有 CHECK 約束，新增類型要先改約束。
4. **三大法人單位是「股」**：`institutional_trading` 所有 `*_net/*_buy/*_sell` 欄位單位是股，顯示成「張」要 ÷1000，門檻比較要 ×1000。
5. **Gemini 呼叫規則**：模型用 `APIConfig.GEMINI_MODEL`（勿寫死字串）、金鑰用 `APIConfig.GEMINI_API_KEY`（是 `GEMINI_KEY` 的別名，兩個都不能刪）、**必加 `reasoning_effort="disable"`**（gemini-2.5 是思考模型，不關會吃光 max_tokens 導致回應截斷）。
6. **pandas 3.0**：`Styler.applymap` 已移除（用 `.map`）；`DataFrame` 欄位可能是 `Decimal`（SQL ROUND 回傳），顯示前 `astype("int64")` 或 `float()`。
7. **Streamlit 顯示 NaN**：從 DB 讀出的欄位可能是 `None`/`NaN`，直接 f-string 會印出 "nan"。一律用 `app.py` 現有的 `_clean_val()` 清洗。
8. **`.env` 絕不可上傳 GitHub**（含 API Key，已在 .gitignore）。commit 前 `git status` 檢查。
9. **驗證方式**：改完先 `py -3.12 -c "import ast; ast.parse(open('檔案',encoding='utf-8').read())"` 檢查語法，再實際跑一次相關函式對真實 DB 驗證（本機 `.env` 可直連 Neon），最後才 commit。
10. **策略改動**：買賣邏輯只改 `agent/strategy.py`，改完必跑 `py -3.12 run_pipeline.py --mode backtest` 對比前後，勿無驗證亂調。

---

## P0 — 高價值改進（先做）

### P0-1 回測加入交易成本
- **目標**：回測目前用收盤價對收盤價、零成本，績效高估。加入台股實際成本後才能真實評估策略。
- **檔案**：`agent/backtest.py`
- **步驟**：
  1. 在檔案頂部加參數：`FEE_RATE = 0.001425 * 0.6`（券商手續費 14.25bp，一般電子下單 6 折）、`TAX_RATE = 0.003`（賣出證交稅 30bp）。
  2. 每筆 round-trip 報酬改為：`net_ret = (exit_price * (1 - FEE_RATE - TAX_RATE)) / (entry_price * (1 + FEE_RATE)) - 1`。
  3. 輸出同時顯示「毛報酬」與「淨報酬」兩組統計（勝率、平均報酬）。
  4. 更新第 12 行與第 276 行的註解文字。
- **驗收**：`--mode backtest` 輸出兩組數字，淨報酬 < 毛報酬，差距約每筆 0.47%。

### P0-2 出場規則消融測試（找出跑輸大盤的元兇）
- **目標**：現行 12 條出場規則全開，其中 `exit_below_ma5: True`（跌破 5 日線就出場）極度激進，很可能是「多頭期跑輸大盤」主因。逐一關閉規則跑回測，找出最佳組合。
- **檔案**：`agent/strategy.py`（只讀）、新增 `scripts/ablation_test.py`
- **步驟**：
  1. 建 `scripts/` 資料夾，寫 `ablation_test.py`：
     - import `agent.backtest.run_backtest` 與 `agent.strategy.STRATEGY`。
     - 定義基準：現行設定跑一次，記錄（交易數、勝率、平均淨報酬、平均持有日）。
     - 逐一測試：`exit_below_ma5=False`、`exit_below_ma20=False`、`exit_on_death_cross=False`、`exit_body_break=False`、`exit_large_candle=False`、`stop_loss=0.10`、`max_hold_days=60`——每次只改一項（用 `STRATEGY.copy()` 改副本傳入；若 run_backtest 不接受 cfg 參數，先小幅重構讓它接受）。
     - 結果整理成表格印出 + 存 `scripts/ablation_result.md`。
  2. 把最佳組合（平均淨報酬×勝率綜合最好）寫進 `ablation_result.md` 結論，**但不要直接改 STRATEGY**——由使用者決定是否採用。
- **驗收**：`ablation_result.md` 有 8+ 組對照數據與結論建議。

### P0-3 每日彙整排除 smart_money 且去除 AI 開場白
- **目標**：(a) `daily_digest.py` 目前撈當日全部 signals，smart_money 一天 60 筆會塞爆 prompt 稀釋新聞重點；(b) Gemini 回應常以「好的，根據您提供的…」開頭，是廢話。
- **檔案**：`data_pipeline/analysis/daily_digest.py`
- **步驟**：
  1. 查詢的 WHERE 改為 `signal_type NOT IN ('digest', 'smart_money')`。
  2. Gemini 回應後處理：`digest_text = re.sub(r'^(好的|以下|根據)[^【]*?(?=【)', '', digest_text.strip())`（把第一個【之前的開場白刪掉；若結果為空則保留原文）。
  3. prompt 最後加一句「直接以【被看好的族群】開頭，不要任何開場白」。
- **驗收**：手動跑 `generate_daily_digest(昨日日期)`（先在 DB 刪掉該日 digest），輸出以【被看好的族群】開頭。

### P0-4 workflow 依賴收斂進 requirements.txt
- **目標**：GitHub Actions 目前在 workflow 內另外 pip install `litellm google-generativeai FinMind`，本機與雲端環境不一致，之前 ta 版本衝突就是這樣發生的。
- **檔案**：`requirements.txt`、`.github/workflows/daily_update.yml`
- **步驟**：
  1. 把 `litellm`、`FinMind` 加進 `requirements.txt`（版本用本機實測可跑的：`pip show litellm finmind` 查版本後鎖 `>=`）。`google-generativeai` 若程式碼沒 import 就不加。
  2. workflow 刪掉那兩行額外 install，保留驗證 print。
  3. 本機重跑 `--mode market` 確認無 import error。
- **驗收**：workflow 只有一行 `pip install -r requirements.txt`；手動觸發 Actions 全綠。

---

## P1 — 穩健性改進

### P1-1 market_signals 防重複（UNIQUE 索引）
- **目標**：pipeline 同日重跑時，各 scraper 靠應用層防重，但無 DB 層保證。
- **步驟**：建 `database/migrations/06_market_signals_unique.sql`：
  ```sql
  -- 先清掉既有重複（保留 id 最小者）
  DELETE FROM market_signals a USING market_signals b
  WHERE a.id > b.id AND a.signal_type = b.signal_type
    AND a.title = b.title AND a.signal_date = b.signal_date;
  CREATE UNIQUE INDEX IF NOT EXISTS uq_market_signals_type_title_date
      ON market_signals (signal_type, title, signal_date);
  ```
  各 scraper 的 INSERT 已有 `ON CONFLICT DO NOTHING` 的保留，沒有的補上。
- **驗收**：連跑兩次 `--mode market`，`market_signals` 當日筆數不變。

### P1-2 時區統一 Asia/Taipei
- **目標**：GitHub Actions 跑在 UTC，`date.today()` 在 UTC 13:00 = 台灣 21:00 當天沒問題，但任何改排程時間/本機半夜跑就會錯置日期。
- **步驟**：
  1. `config/settings.py` 加：
     ```python
     from zoneinfo import ZoneInfo
     TW_TZ = ZoneInfo("Asia/Taipei")
     def tw_today():
         from datetime import datetime
         return datetime.now(TW_TZ).date()
     ```
  2. 全域搜尋 `date.today()`，**只替換**與「訊號日期/資料日期」相關者（`daily_digest.py`、`smart_money.py`、`news_scraper.py`、`youtube_scraper.py`、`run_pipeline.py` 的 mode_auto 星期判斷）為 `tw_today()`。app.py 顯示用的可不動。
- **驗收**：`py -3.12 -c "from config.settings import tw_today; print(tw_today())"` 印出台灣今日。

### P1-3 YouTube 字幕雲端被擋的降級方案
- **目標**：`youtube-transcript-api` 在 GitHub Actions 資料中心 IP 常被 YouTube 擋（該影片就無摘要）。至少要讓「標題」也有 AI 分析，不要整筆空白。
- **檔案**：`data_pipeline/scrapers/youtube_scraper.py`
- **步驟**：`run_youtube_scraper` 中 `transcript` 為 None 時，改呼叫一個新函式 `_analyze_title_only(channel_name, title)`：用 Gemini 只根據標題推測主題與提及個股（prompt 註明「僅標題推測，簡短一句」），summary 前綴加「（無字幕，僅依標題推測）」。Gemini 呼叫規則見執行須知第 5 條。
- **驗收**：DB 中 youtube 訊號不再有 summary IS NULL 的新記錄。

### P1-4 單元測試起步
- **目標**：核心邏輯零測試。先為最關鍵且純函式的部分建立 pytest。
- **步驟**：
  1. `requirements.txt` 加 `pytest`；建 `tests/` 資料夾。
  2. `tests/test_strategy.py`：測 `decide_exit`——停損觸發、停利觸發、移動停利、`exit_below_ma5` 關閉時不觸發、`max_hold_days` 到期。每條規則至少一正一反。
  3. `tests/test_parsers.py`：測 `news_scraper` 的 Gemini 回應行解析（餵固定字串 `[1] 重點：xxx | 情緒：正面 | 股票：2330`，驗證 summary/sentiment/stocks）；把解析邏輯抽成獨立函式 `_parse_analysis_line(line)` 以便測試。
  4. 跑法：`py -3.12 -m pytest tests/ -v`。
- **驗收**：至少 12 個測試全綠；不需 DB 連線即可跑。

### P1-5 etf_fetcher 移除每次執行的 migration 重放
- **目標**：`_ensure_tables()` 每天把 `03_market_signals.sql` 整份重執行（INSERT 15 檔 ETF + DDL），靠 IF NOT EXISTS/ON CONFLICT 硬擋，脆弱且慢。
- **步驟**：改為只檢查：`SELECT 1 FROM information_schema.tables WHERE table_name='etf_watchlist'`，存在就直接 return；不存在才執行 migration 檔並 log 警告。
- **驗收**：連跑兩次 `run_etf_tracking()`，第二次 log 無 migration 相關輸出。

---

## P2 — 品質/整理（有空再做）

### P2-1 app.py 拆頁
`app.py` 已破千行。建 `ui/` 資料夾，每頁一個模組（`ui/page_home.py`…`ui/page_smart_money.py`），共用函式（`_clean_val`、`load_*`）放 `ui/common.py`，`app.py` 只留路由。**純搬移不改邏輯**，搬完每頁人工點開確認。

### P2-2 requirements 鎖版本
`pip freeze` 對照 requirements.txt，把主要套件鎖到 `~=` 級別，避免 GitHub Actions 未來又因上游升版爆炸（ta 事件重演）。

### P2-3 pipeline_logs 實際寫入
schema 有 `pipeline_logs` 表但沒人寫。在 `run_pipeline.py` 的 `mode_pipeline` 開頭/結尾/except 寫入（mode、started_at、finished_at、status、error_msg），網頁首頁可顯示最近 5 次執行狀態。

---

# 新功能 Epic

## Epic A — 我的持倉（手動建倉 + AI 波段判斷）

> 使用者手動輸入實際持有的股票（買價、股數），系統每日用波段邏輯判斷：續抱 / 建議加碼 / 建議賣出，並在網頁顯示、Telegram 提醒。**系統只建議，不自動平倉**（與 AI 部位不同）。

### A-1 資料表擴充
- 建 `database/migrations/07_manual_positions.sql`：
  ```sql
  ALTER TABLE positions ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'ai'
      CHECK (source IN ('ai','manual'));
  ALTER TABLE positions ADD COLUMN IF NOT EXISTS shares INTEGER;            -- 股數（手動倉用）
  ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_advice TEXT;          -- 最近一次 AI 建議
  ALTER TABLE positions ADD COLUMN IF NOT EXISTS advice_date DATE;          -- 建議日期
  -- 手動倉可能同股票分批買，放寬唯一約束：
  ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_stock_id_entry_date_key;
  CREATE UNIQUE INDEX IF NOT EXISTS uq_positions_ai
      ON positions (stock_id, entry_date) WHERE source = 'ai';
  ```
- 既有資料不動（default 'ai'）。提醒使用者去 Neon 執行。

### A-2 建倉/平倉介面（app.py 持倉追蹤頁改造）
- 「📦 持倉追蹤」頁改為兩個 tab：`🤖 AI 部位`（現有內容原樣搬入）與 `👤 我的持倉`。
- 「我的持倉」tab：
  1. `st.form` 新增持倉：股票代號（文字輸入，送出時查 `stocks` 表驗證存在，不存在顯示錯誤）、買入日期（date_input，預設今天）、買入價格（number_input, step=0.01）、股數（number_input, step=1000, 顯示提示「1張=1000股」）、備註（選填）。寫入 positions（source='manual', entry_reason=備註）。
  2. 持倉表格：代號/名稱/買入日/買入價/股數/**現價**（daily_prices 最新收盤）/損益%（含金額 =(現價-買價)×股數）/**AI 建議**（last_advice, 高亮：含「賣出」紅、「加碼」綠）/建議日期。
  3. 每列提供「已賣出」按鈕：點擊彈出賣出價格輸入 → 更新 status='closed', exit_date, exit_price, return_pct, exit_reason='手動平倉'。
  4. 所有值經 `_clean_val` 清洗；金額千分位。

### A-3 每日 AI 判斷（核心邏輯）
- 新檔 `agent/manual_advisor.py`，函式 `advise_manual_positions(target_date=None)`：
  1. 撈 `positions WHERE source='manual' AND status='open'`。無則 return。
  2. 對每檔：用 `agent/portfolio.py` 的 `_recent_rows()` 取近 35 日 OHLCV+指標（該函式已存在，直接 import 用）。
  3. **賣出判斷**：呼叫 `strategy.decide_exit()`（與 AI 部位同一套 12 條規則，波段參數不變）。回傳 (True, 原因) → 建議 = `f"⚠️ 建議賣出：{原因}"`。
  4. **加碼判斷**（僅在不建議賣出時）：滿足全部三條 → 建議 = `"➕ 可考慮加碼：{說明}"`：
     - 現價 > 買入均價（賺錢中才加碼）
     - 收盤價回踩 MA20 附近（`abs(close-ma20)/ma20 <= 0.03`）
     - MACD 柱 > 0 且 MA5 > MA20（趨勢未壞）
  5. 都不滿足 → 建議 = `"✅ 續抱"`（附當前 RSI 與距停損/停利百分比，例：`續抱（RSI 58，距停損 -5.2%，距停利 +12%）`）。
  6. 寫回該筆 positions 的 `last_advice`、`advice_date`。
  7. 回傳彙總文字（給 Telegram）：只包含「賣出」與「加碼」建議的股票，全部續抱則回傳 None。
- **不要**在此檔重寫出場邏輯——一律 import strategy，維持單一策略中樞原則。

### A-4 接入 pipeline 與 Telegram
- `run_pipeline.py` 的 `mode_pipeline`：在 `run_daily_recommendation` 之後加：
  ```python
  try:
      from agent.manual_advisor import advise_manual_positions
      manual_msg = advise_manual_positions()
      if manual_msg:
          msg = (msg or "") + "\n\n📦 我的持倉提醒\n" + manual_msg
  except Exception as e:
      logger.error(f"手動持倉判斷失敗: {e}")
  ```
- **驗收**：手動建一筆真實持倉 → 跑 `py -3.12 -c "from agent.manual_advisor import advise_manual_positions; print(advise_manual_positions())"` → DB `last_advice` 有值、網頁表格顯示建議。

## Epic B — 追蹤清單（自選股 + AI 買賣點判斷）

> 使用者維護一份感興趣的股票清單，系統每日判斷每檔是否出現**波段買點**，出現時網頁高亮 + Telegram 通知。

### B-1 資料表
- 建 `database/migrations/08_watchlist.sql`：
  ```sql
  CREATE TABLE IF NOT EXISTS user_watchlist (
      stock_id     VARCHAR(10) PRIMARY KEY REFERENCES stocks(stock_id),
      added_date   DATE NOT NULL DEFAULT CURRENT_DATE,
      note         TEXT,
      target_price NUMERIC(12,2),          -- 選填：理想買入價
      last_signal  TEXT,                   -- 最近一次買點判斷
      signal_date  DATE
  );
  ```

### B-2 每日買點判斷
- 新檔 `data_pipeline/analysis/watchlist_advisor.py`，函式 `evaluate_watchlist(target_date=None)`：
  1. 撈 watchlist 全部股票，對每檔取最新技術指標與近 5 日法人（含投信）。
  2. 逐項檢查訊號並累計分數（沿用 `STRATEGY` 權重，勿另發明一套）：
     - 均線黃金交叉（MA5 上穿 MA20，本日成立）→ `w_ma_cross`
     - 突破 20 日高 → `w_breakout`
     - MACD 柱翻正 → `w_macd_pos`
     - 近 5 日三大法人淨買超 → `w_inst_buy`；投信連 3 日買超 → 額外 +1.5
     - RSI 在 50~65 → `w_rsi_sweet`
     - 若 target_price 有值且現價 ≤ target_price → 額外標註「已到目標價」
  3. 分數 ≥ 4.0 → `last_signal = "🟢 買點浮現：{成立的訊號列表}"`；2.0~4.0 → `"🟡 接近買點：{訊號}"`；否則 `"⚪ 觀望（{現價/RSI}）"`。寫回 last_signal/signal_date。
  4. 回傳含 🟢 的彙總文字（Telegram 用），無則 None。
- 接入 `run_pipeline.py`（與 A-4 相同模式，訊息標題「🔖 追蹤清單買點」）。

### B-3 網頁頁面
- 新增 sidebar 頁「🔖 追蹤清單」：
  1. 新增表單：代號（驗證存在）、備註、目標價（選填）。
  2. 表格：代號/名稱/加入日/現價/加入後漲跌%/目標價/**買點判斷**（🟢綠底、🟡黃底）/判斷日。
  3. 每列「移除」按鈕（DELETE）。
  4. 側欄「重新整理」按鈕清 cache（參考聰明資金頁做法）。
- **驗收**：加入 2330 → 跑 evaluate_watchlist → 表格顯示判斷結果；訊號分數手算與程式一致。

## Epic C — 族群輪動儀表板（細分族群 + 龍頭股）

> 現有產業分類太粗（「半導體業」一包 210 支，無記憶體/IC設計等細分）。建立細分族群 + 龍頭股結構，一眼看出「今天哪個族群在漲、龍頭是誰」。

### C-1 細分族群資料表 + 種子資料
- 建 `database/migrations/09_stock_groups.sql`：
  ```sql
  CREATE TABLE IF NOT EXISTS stock_groups (
      group_code  VARCHAR(30) PRIMARY KEY,     -- e.g. 'memory'
      group_name  VARCHAR(50) NOT NULL,        -- 記憶體
      description TEXT
  );
  CREATE TABLE IF NOT EXISTS stock_group_members (
      group_code  VARCHAR(30) REFERENCES stock_groups(group_code),
      stock_id    VARCHAR(10) REFERENCES stocks(stock_id),
      is_leader   BOOLEAN DEFAULT FALSE,       -- 龍頭股標記
      PRIMARY KEY (group_code, stock_id)
  );
  ```
- 種子資料（同檔 INSERT，龍頭標 is_leader=TRUE）至少涵蓋：
  - `memory` 記憶體：**2344華邦電**、**2408南亞科**、3006晶豪科、2337旺宏、8299群聯
  - `foundry` 晶圓代工：**2330台積電**、2303聯電、6770力積電、5347世界先進
  - `ic_design` IC設計：**2454聯發科**、3034聯詠、2379瑞昱、3443創意、3529力旺
  - `packaging` 封測：**3711日月光投控**、6239力成、2449京元電子
  - `pcb` PCB/載板：**3037欣興**、8046南電、2383台光電、6213聯茂
  - `ai_server` AI伺服器：**2382廣達**、2317鴻海、3231緯創、6669緯穎、2376技嘉
  - `cooling` 散熱：**3017奇鋐**、2421建準、3324雙鴻
  - `passive` 被動元件：**2327國巨**、2492華新科、2375凱美
  - `optical` 光學/CCL：**3008大立光**、3406玉晶光
  - `networking` 網通：**2345智邦**、5388中磊、3596智易
  - `financial` 金控：**2886兆豐金**、2891中信金、2884玉山金、2882國泰金
  - `shipping` 航運：**2603長榮**、2609陽明、2615萬海
  - `defense_drone` 軍工/無人機：**2634漢翔**、8033雷虎、3162精確
  - （執行時可再補；INSERT 前先 `SELECT` 確認代號存在於 stocks 表，不存在的跳過並記錄）
- 提醒使用者去 Neon 執行。

### C-2 每日族群熱度計算
- 新檔 `data_pipeline/analysis/group_momentum.py`，函式 `calc_group_momentum(target_date=None)`：
  1. 對每個 group：join `stock_group_members` × 當日 `daily_prices`，算：
     - `avg_change_pct`（成員當日平均漲跌）
     - `rising_ratio`（上漲家數/總家數）
     - `leader_change_pct`（龍頭股當日平均漲跌）
     - `inst_net_lots`（成員當日三大法人合計淨買超，**÷1000 轉張**）
  2. 熱度分 = normalize(avg_change)×0.4 + normalize(rising_ratio)×0.2 + normalize(leader_change)×0.3 + normalize(inst_net)×0.1（**龍頭權重高**——龍頭先動是輪動起點，正是使用者要的訊號）。normalize 參考 `sector_momentum.py` 的做法。
  3. 結果直接回傳 DataFrame（不落地新表；網頁即時算即可，成員少速度快）。
- 接入 `run_pipeline.py` 不需要（即時算），但在 `--mode sector` 順帶印出前 5 名族群供 log 觀察。

### C-3 網頁頁面「🔥 族群輪動」
- 新增 sidebar 頁「🔥 族群輪動」：
  1. **頂部總覽**：當日族群熱度排行（橫向 bar chart，plotly，紅漲綠跌台股慣例：漲用紅色系），每列顯示：族群名、平均漲跌%、上漲家數/總家數、龍頭漲跌%。
  2. **族群明細**（點排行任一列或用 selectbox 選族群）：該族群全部成員表格——代號/名稱/👑龍頭標記/當日漲跌%/收盤價/成交量/近5日法人買超(張)/RSI。依當日漲跌排序。
  3. **輪動提示區**：規則——若某族群「龍頭當日漲 >2% 且 族群 rising_ratio >0.6」→ 顯示 `"🔥 {族群}輪動中：龍頭 {名} +{x}%，{m}/{n} 家上漲"`。無則顯示「今日無明顯族群輪動」。
  4. 資料日期用 daily_prices 最新交易日（非 today，避免假日空白）。
- **驗收**：頁面能顯示排行與明細；手選一個當天大漲的族群，明細數字與看盤軟體一致。

### C-4 族群資訊融入每日彙整（選做）
- `daily_digest.py` 的 prompt 內容加一段「今日族群輪動：{前3名族群與龍頭表現}」（呼叫 C-2 函式取前 3），讓每日彙整的【被看好的族群】有量化佐證。

---

## 建議執行順序

```
P0-3（10分鐘）→ P0-1（30分鐘）→ P0-4（30分鐘）→ Epic A（半天）
→ Epic B（半天）→ Epic C（1天）→ P0-2（半天，跑回測耗時）
→ P1 各項 → P2
```

每完成一項：語法檢查 → 本機實跑驗證 → commit（訊息格式照 git log 慣例：`fix:`/`feat:`/`docs:` + 中文說明）→ push。涉及 migration 的，在回覆中**明確提醒使用者去 Neon SQL Editor 執行對應 SQL**。
