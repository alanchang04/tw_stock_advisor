# 台股個人投資顧問系統

個人使用的台股投資顧問，每日自動抓取股價與籌碼資料、計算技術指標、偵測族群輪動，並透過 LLM 產生每日 Top 5 選股推薦。

---

## 技術堆疊

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.12.9 |
| 資料庫 | PostgreSQL 16（Docker） |
| LLM | Gemini 2.5 Flash（via LiteLLM） |
| 資料來源 | FinMind API |
| 技術指標 | ta 0.5.25 |
| 排程 | Windows 工作排程器 |

---

## 快速開始（從零建立）

### 前置準備

1. 安裝 [Python 3.12.9](https://www.python.org/downloads/release/python-3129/)
2. 安裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
3. 申請 [FinMind](https://finmindtrade.com/) 免費帳號，取得 API Token
4. 申請 [Google AI Studio](https://aistudio.google.com) 免費帳號，取得 Gemini API Key

### Step 1 — 安裝套件

```powershell
pip install -r requirements.txt
pip install litellm google-generativeai
```

### Step 2 — 設定環境變數

```powershell
copy .env.example .env
```

打開 `.env`，填入以下兩個必填欄位：

```
FINMIND_TOKEN=你的FinMind Token
GEMINI_API_KEY=你的Gemini API Key
```

### Step 3 — 啟動資料庫

```powershell
docker compose up -d
```

等約 15 秒。`database/init.sql` 會自動建立 13 張資料表。

確認正常：
```powershell
docker compose logs postgres | Select-Object -Last 3
# 看到 "database system is ready to accept connections" 表示成功
```

### Step 4 — 確認 DB 連線

```powershell
python -c "from database.connection import test_connection; test_connection()"
# 應顯示：✅ PostgreSQL 連線成功
```

### Step 5 — 初始化歷史資料

> 預設抓全部股票。若只想先測試少量，可加 `--limit 100`（只抓前 100 支）。

```powershell
python run_pipeline.py --mode init --days 240
```

約需 10+ 小時（FinMind 免費版 600次/hr 限制）。程式有自動等待重置機制，可以讓它跑一整天。中斷後重跑會自動從上次斷點繼續。

### Step 6 — 建立產業分類

```powershell
python run_pipeline.py --mode industry
```

### Step 7 — 計算技術指標與族群熱度

```powershell
python run_pipeline.py --mode technical
python run_pipeline.py --mode sector
```

### Step 8 — 產生第一份推薦報告

```powershell
python run_pipeline.py --mode recommend
```

### Step 9 — 設定每日自動排程

用**系統管理員身份**開啟 PowerShell：

```powershell
schtasks /create /tn "TaiwanStockAdvisor" /tr "C:\Users\alanchang\Desktop\taiwan-stock-advisor\daily_update.bat" /sc daily /st 18:30 /ru SYSTEM /f
```

確認排程建立成功：
```powershell
schtasks /query /tn "TaiwanStockAdvisor"
```

之後每天 18:30 會自動執行：抓取收盤資料 → 計算技術指標 → 產生推薦。

> 每日更新預設使用證交所(TWSE)/櫃買(TPEX)官方 OpenAPI，一次抓回全市場最近交易日的股價與三大法人，約 4 次請求、數秒完成，**不受 FinMind 600 次/hr 限制，電腦不需長時間運作**。FinMind 僅在首次歷史回補（`--mode init`）時使用。

---

## 每日操作

### 啟動系統（每次開機後）

```powershell
cd C:\Users\alanchang\Desktop\taiwan-stock-advisor
docker compose up -d
```

### 停止系統

```powershell
docker compose down
# 注意：不要加 -v，加了會刪除所有資料
```

---

## 所有指令

```powershell
# 首次建立歷史資料（只需執行一次）
python run_pipeline.py --mode init --days 240

# 每日收盤後抓取新資料（預設用證交所/櫃買官方 API，全市場一次抓完、數秒）
python run_pipeline.py --mode daily
# 後備：改用 FinMind 逐檔抓（受 600 次/hr 限制，較慢）
python run_pipeline.py --mode daily --source finmind

# 更新產業分類（建議每週一次）
python run_pipeline.py --mode industry

# 重新計算所有技術指標
python run_pipeline.py --mode technical

# 計算族群輪動熱度
python run_pipeline.py --mode sector

# 產生今日推薦報告
python run_pipeline.py --mode recommend

# 啟動背景排程（需電腦持續開著）
python run_pipeline.py --mode schedule
```

---

## 查詢資料庫

### 視覺化介面

打開瀏覽器：`http://localhost:5050`
- 帳號：`admin@stock.local`
- 密碼：`admin123`

### 常用查詢指令

**各表資料筆數：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT relname AS table_name, n_live_tup AS row_count FROM pg_stat_user_tables ORDER BY n_live_tup DESC;"
```

**某支股票股價（以 2330 為例）：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, close, change_pct, volume FROM daily_prices WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 10;"
```

**某支股票技術指標：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, ma5, ma20, ma60, rsi14, signal_ma_cross, signal_breakout FROM technical_indicators WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 5;"
```

**某支股票籌碼：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT trade_date, foreign_net, invest_net, dealer_net, total_net FROM institutional_trading WHERE stock_id='2330' ORDER BY trade_date DESC LIMIT 10;"
```

**今日族群熱度排名：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT industry_code, avg_change_pct, rising_count, total_count, momentum_score FROM sector_momentum WHERE calc_date=(SELECT MAX(calc_date) FROM sector_momentum) ORDER BY momentum_score DESC LIMIT 10;"
```

**歷史推薦紀錄：**
```powershell
docker exec -it stock_advisor_db psql -U stock_user -d taiwan_stock -c "SELECT rec_date, rank, stock_id, reason FROM daily_recommendations ORDER BY rec_date DESC, rank ASC;"
```

**查詢 FinMind API 剩餘額度：**
```powershell
python -c "
import requests, os
from dotenv import load_dotenv
load_dotenv()
resp = requests.get('https://api.web.finmindtrade.com/v2/user_info', params={'token': os.getenv('FINMIND_TOKEN')})
d = resp.json()
print(f'已用：{d[\"user_count\"]} / 上限：{d[\"api_request_limit\"]}')
"
```

---

## 資料庫 Schema

| 資料表 | 說明 | 資料來源 |
|--------|------|----------|
| stocks | 股票基本資料 | FinMind |
| industries | 產業分類（54個） | FinMind |
| stock_industry_map | 股票產業對應 | FinMind |
| daily_prices | 每日 OHLCV | FinMind |
| institutional_trading | 三大法人籌碼 | FinMind |
| margin_trading | 融資融券 | FinMind |
| financials | 季報財務資料 | FinMind |
| technical_indicators | 技術指標快取 | 本地計算 |
| sector_momentum | 族群輪動熱度 | 本地計算 |
| announcements | MOPS 重大訊息 | 待開發 |
| yt_insights | YouTube 情報 | 待開發 |
| daily_recommendations | 每日推薦結果 | LLM |
| pipeline_logs | 執行紀錄 | 系統 |

---

## 系統架構

```
資料層      FinMind API → 股價 / 籌碼 / 股票清單 / 產業分類
    ↓
儲存層      PostgreSQL（結構化資料）
    ↓
分析層      技術指標（ta）/ 族群輪動熱度評分
    ↓
Agent 層    候選股篩選 → Gemini 2.5 Flash → 推薦報告
    ↓
輸出層      Terminal 報告 / daily_recommendations 表
```

---

## 重要注意事項

- `.env` 含有 API Key，絕對不可上傳 GitHub
- `logs/` 目錄記錄已抓過的股票，不可刪除（會導致重複抓取浪費額度）
- `docker compose down -v` 會刪除所有資料，正常停止只用 `docker compose down`
- FinMind 免費版限制：股價 240 天、籌碼 90 天、600 次/hr
- 排程需要電腦在 18:30 開著且 Docker 正在執行

---

## 開發進度

詳見 [docs/progress.md](docs/progress.md)

## 完整架構規劃

詳見 [docs/roadmap.md](docs/roadmap.md)
