# 台股個人投資顧問系統

個人使用的台股投資 agent：每日自動抓取全市場股價與籌碼、計算技術指標、偵測族群輪動，
透過 LLM 產生 Top 5 選股推薦，**並記住推薦過的部位、持續追蹤、在符合出場規則時主動提醒離場**。
結果可推播到 Telegram，並提供回測工具驗證選股/買賣邏輯。

---

## 技術堆疊

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.12 |
| 資料庫 | PostgreSQL 16（Docker） |
| LLM | Gemini 2.5 Flash（via LiteLLM，強制 JSON 輸出 + 重試/容錯） |
| 每日資料來源 | 證交所(TWSE)/櫃買(TPEX) 官方 OpenAPI（免 token、無限流） |
| 歷史回補 | FinMind API（僅首次或長缺口） |
| 技術指標 | ta 0.5.25 |
| 通知 | Telegram Bot |
| 排程 | Windows 工作排程器（每日 21:00） |

> **資料來源說明**：每日更新走證交所/櫃買官方 API，一次抓回全市場單日股價與三大法人，約 4 次請求、數秒完成，**不受 FinMind 600 次/hr 限制**。FinMind 只在第一次拉長歷史時用得到（也可改用更快的 `--mode backfill`）。

---

## 快速開始（從零建立）

### 前置準備

1. 安裝 [Python 3.12](https://www.python.org/downloads/)（用 `py -3.12` 呼叫）
2. 安裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
3. 申請 [Google AI Studio](https://aistudio.google.com) 免費帳號，取得 Gemini API Key
4.（選配）申請 [FinMind](https://finmindtrade.com/) Token；只在用 FinMind 拉超長歷史時需要

### Step 1 — 安裝套件

```powershell
pip install -r requirements.txt
pip install litellm google-generativeai
```

### Step 2 — 設定環境變數

```powershell
copy .env.example .env
```

打開 `.env` 填入：

```
GEMINI_API_KEY=你的Gemini API Key
FINMIND_TOKEN=你的FinMind Token        # 選配
TELEGRAM_BOT_TOKEN=你的bot_token        # 選配，見下方 Telegram 設定
TELEGRAM_CHAT_ID=你的chat_id            # 選配
```

### Step 3 — 啟動資料庫

```powershell
docker compose up -d
```

等約 15 秒。`database/init.sql` 會自動建立 14 張資料表。確認正常：

```powershell
docker compose logs postgres | Select-Object -Last 3
# 看到 "database system is ready to accept connections" 表示成功
```

### Step 4 — 確認 DB 連線

```powershell
py -3.12 -c "from database.connection import test_connection; test_connection()"
# 應顯示：✅ PostgreSQL 連線成功
```

### Step 5 — 建立歷史資料（兩種方式擇一）

**方式 A（推薦，快）— 官方 OpenAPI 回補：** 自動建立股票清單 + 約一年歷史，約 20~30 分鐘。

```powershell
py -3.12 run_pipeline.py --mode backfill --days 240
```

**方式 B — FinMind 逐檔拉取：** 較慢（數小時，受 600 次/hr 限制），需 `FINMIND_TOKEN`。

```powershell
py -3.12 run_pipeline.py --mode init --days 240   # 可加 --limit 100 先測試少量
```

### Step 6 — 建立產業分類

```powershell
py -3.12 run_pipeline.py --mode industry
```

### Step 7 — 算技術指標 + 產生第一份推薦

```powershell
py -3.12 run_pipeline.py --mode pipeline
```

`pipeline` 會一次做完：補齊缺漏交易日 → 算技術指標 → 檢查出場 → 選股 → LLM 推薦 → 開部位 →（有設定的話）推 Telegram。

### Step 8 — 設定每日自動排程

用**系統管理員身份**開 PowerShell（注意路徑為底線 `taiwan_stock_advisor`；不要加 `/ru SYSTEM`，Docker Desktop 跑在你的使用者帳號下）：

```powershell
schtasks /create /tn "TaiwanStockAdvisor" /tr "C:\Users\alanchang\Desktop\taiwan_stock_advisor\daily_update.bat" /sc daily /st 21:00 /f
```

確認建立成功：

```powershell
schtasks /query /tn "TaiwanStockAdvisor"
```

之後每天 21:00（電腦開機、已登入、Docker Desktop 運行中）會自動執行完整流程並推播推薦。
[daily_update.bat](daily_update.bat) 會自動啟動 Docker、等資料庫就緒、再跑 `--mode pipeline`。**某天漏跑也沒關係——下次會自動補齊缺漏的交易日。**

---

## Telegram 通知設定（選配但推薦）

1. Telegram 搜尋 `@BotFather` → 傳 `/newbot` → 取得 **bot token**
2. 對你的新 bot 傳一則訊息（一定要先傳，否則拿不到 chat_id）
3. 自動抓 chat_id：

```powershell
py -3.12 -c "import os,requests; from dotenv import load_dotenv; load_dotenv(); tok=os.getenv('TELEGRAM_BOT_TOKEN'); print([u['message']['chat']['id'] for u in requests.get(f'https://api.telegram.org/bot{tok}/getUpdates').json()['result'] if 'message' in u])"
```

4. 把 token 與 chat_id 填入 `.env`，測試：

```powershell
py -3.12 agent/notifier.py    # 手機收到測試訊息即成功
```

設定後：成功 → 推當日「進場推薦 + 出場提醒」；失敗 → 推錯誤訊息。

---

## 每日操作

```powershell
# 開機後啟動
docker compose up -d

# 手動跑完整流程（等同排程做的事）
py -3.12 run_pipeline.py --mode pipeline

# 停止（注意：不要加 -v，會刪資料）
docker compose down
```

---

## 所有指令

```powershell
# ── 每日 ──
py -3.12 run_pipeline.py --mode pipeline      # 完整流程：補資料+技術指標+出場檢查+選股+推薦+開部位
py -3.12 run_pipeline.py --mode daily         # 只抓最近交易日資料（OpenAPI，數秒）
py -3.12 run_pipeline.py --mode backfill      # 補齊 DB 缺漏的交易日（自動偵測；漏跑救援）

# ── 分析/推薦（pipeline 內含，也可單獨跑）──
py -3.12 run_pipeline.py --mode technical     # 重算技術指標
py -3.12 run_pipeline.py --mode sector        # 計算族群輪動熱度
py -3.12 run_pipeline.py --mode recommend     # 出場檢查 + 選股 + LLM 推薦 + 開部位

# ── 建置/維護 ──
py -3.12 run_pipeline.py --mode init --days 240   # FinMind 拉歷史（首次，慢）
py -3.12 run_pipeline.py --mode industry          # 更新產業分類（建議每週）

# ── 回測 ──
py -3.12 run_pipeline.py --mode backtest      # 回測選股+買賣邏輯績效
```

---

## 策略調整與回測

所有買賣參數集中在 **[agent/strategy.py](agent/strategy.py)** 的 `STRATEGY` dict：

- **進場**：候選池過濾（RSI/股價/成交量）、評分權重（均線交叉、突破、MACD、法人/外資買超、RSI 甜蜜帶）
- **出場**：停損、停利、移動停利、跌破月線(MA20)、均線死亡交叉、持有上限

改完直接重跑回測比較優劣：

```powershell
py -3.12 run_pipeline.py --mode backtest
```

回測會模擬真實進出場（進場用評分、出場用 strategy 規則），輸出**勝率、平均報酬、平均持有天數、出場原因分布**，並與大盤等權買進持有比較。`stock_selector`（正式選股）與 `backtest` 共用同一份 `score_candidates`，所以調 `strategy.py` 兩邊一起變。

> 註：回測為理想化估計（收盤價、未計手續費/滑價）；法人歷史資料較短（FinMind 約近 90 天），樣本越累積越可信。

---

## 部位追蹤（agent 的核心）

- 每次推薦買進的股票會寫入 `positions` 表開始追蹤（記進場日、進場價）。
- 每日流程會先檢查所有持有部位，符合 `strategy.py` 出場規則就標記平倉並發出「賣出提醒」。
- 出場檢查**獨立於 LLM**：即使 LLM 當機，賣出提醒照樣會發。

查目前持倉：

```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT stock_id, entry_date, entry_price, status, exit_reason, return_pct FROM positions ORDER BY status, entry_date DESC;"
```

---

## 查詢資料庫

### 視覺化介面

瀏覽器開 `http://localhost:5050`，帳號 `admin@admin.com`、密碼 `admin123`。

### 常用查詢

```powershell
# 各表資料筆數
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT relname AS table_name, n_live_tup AS row_count FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"

# 某股股價（2330）
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, close, change_pct, volume FROM daily_prices WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 10;"

# 某股技術指標
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, ma5, ma20, ma60, rsi14, signal_ma_cross, signal_breakout FROM technical_indicators WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 5;"

# 某股籌碼
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, foreign_net, invest_net, dealer_net, total_net FROM institutional_trading WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 10;"

# 今日族群熱度
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT industry_code, avg_change_pct, rising_count, total_count, momentum_score FROM sector_momentum WHERE calc_date=(SELECT MAX(calc_date) FROM sector_momentum) ORDER BY momentum_score DESC LIMIT 10;"

# 歷史推薦
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT rec_date, rank, stock_id, reason FROM daily_recommendations ORDER BY rec_date DESC, rank ASC;"
```

---

## 資料庫 Schema（14 張表）

| 資料表 | 說明 | 資料來源 |
|--------|------|----------|
| stocks | 股票基本資料 | OpenAPI / FinMind |
| industries | 產業分類 | MoneyDJ |
| stock_industry_map | 股票產業對應 | MoneyDJ |
| daily_prices | 每日 OHLCV | TWSE/TPEX OpenAPI（init 時 FinMind） |
| institutional_trading | 三大法人籌碼 | TWSE/TPEX OpenAPI（init 時 FinMind） |
| margin_trading | 融資融券 | 待開發 |
| financials | 季報財務資料 | FinMind |
| technical_indicators | 技術指標快取 | 本地計算 |
| sector_momentum | 族群輪動熱度 | 本地計算 |
| **positions** | **部位追蹤（進場/出場/報酬）** | **系統** |
| daily_recommendations | 每日推薦結果 | LLM |
| announcements | MOPS 重大訊息 | 待開發 |
| yt_insights | YouTube 情報 | 待開發 |
| pipeline_logs | 執行紀錄 | 系統 |

---

## 系統架構

```
資料層    TWSE/TPEX OpenAPI（每日，全市場一次）/ FinMind（首次歷史）
   ↓
儲存層    PostgreSQL（Docker 具名 volume，已釘死防改名遺失）
   ↓
分析層    技術指標（ta）/ 族群輪動熱度
   ↓
策略層    strategy.py（進場評分 + 出場規則，集中可調）
   ↓
Agent層   出場檢查(持倉) → 候選篩選 → Gemini 2.5 Flash 推薦 → 開新部位
   ↓
輸出層    Terminal 報告 / Telegram 推播 / DB（daily_recommendations、positions）
   ↓
驗證      backtest（round-trip 回測，對比大盤）
```

模組對應：`data_pipeline/fetchers/twse_fetcher.py`（OpenAPI 抓取/補資料）、
`agent/strategy.py`（買賣邏輯）、`agent/stock_selector.py`（選股）、
`agent/llm_advisor.py`（LLM）、`agent/portfolio.py`（部位追蹤/出場）、
`agent/backtest.py`（回測）、`agent/notifier.py`（Telegram）。

---

## 重要注意事項

- `.env` 含 API Key，**絕不可上傳 GitHub**（已被 `.gitignore` 排除）。
- **DB 資料存在 Docker volume `taiwan-stock-advisor_pgdata`**，不在專案資料夾。
  docker-compose.yml 已把 volume 名稱**釘死**，即使專案資料夾改名也不會遺失資料。
- **永遠不要 `docker compose down -v`**（`-v` 會刪 volume = 刪光資料）。正常停止只用 `docker compose down`。
- 排程需電腦在 21:00 開機、已登入、且 Docker Desktop 運行中（建議在 Docker Desktop 開啟「登入時自動啟動」）。
- 股票池只收 4 位數普通股（排除權證/債券ETF）。

---

## 開發進度與規劃

詳見 [docs/progress.md](docs/progress.md) 與 [docs/roadmap.md](docs/roadmap.md)。
