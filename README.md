# 台股投資顧問系統 — Phase 1 資料 Pipeline

## 快速啟動（5 分鐘）

### 1. 安裝依賴
```bash
cd taiwan-stock-advisor
pip install -r requirements.txt
```

### 2. 設定環境變數
```bash
cp .env.example .env
# 編輯 .env，填入你的 FinMind Token
# FinMind 免費帳號申請：https://finmindtrade.com/
```

### 3. 啟動 PostgreSQL（Docker）
```bash
docker compose up -d
# 等待約 10 秒讓 DB 初始化完成
# pgAdmin 管理介面：http://localhost:5050
#   帳號：admin@stock.local
#   密碼：admin123
```

### 4. 確認資料庫已就緒
```bash
docker compose logs postgres | tail -5
# 看到 "database system is ready to accept connections" 表示成功
```

### 5. 初始化歷史資料（首次執行）
```bash
# 拉取最近 365 天歷史資料（前 100 支股票）
python run_pipeline.py --mode init

# 如果只想拉近 90 天（較快）
python run_pipeline.py --mode init --days 90
```

### 6. 建立產業分類
```bash
python run_pipeline.py --mode industry
```

### 7. 啟動每日排程
```bash
python run_pipeline.py --mode schedule
# 每天 18:00 自動抓收盤資料
# Ctrl+C 停止
```

---

## 目錄結構

```
taiwan-stock-advisor/
├── docker-compose.yml          # PostgreSQL + pgAdmin
├── requirements.txt
├── .env.example                # 環境變數範本
├── run_pipeline.py             # 主入口
│
├── config/
│   └── settings.py             # 集中設定管理
│
├── database/
│   ├── init.sql                # DB schema（Docker 啟動時自動執行）
│   └── connection.py           # SQLAlchemy 連線管理
│
├── data_pipeline/
│   ├── fetchers/
│   │   └── finmind_fetcher.py  # FinMind API 抓取
│   └── scrapers/
│       └── moneydj_scraper.py  # MoneyDJ 產業分類爬蟲
│
└── logs/                       # 自動建立的 log 目錄
```

---

## 資料庫 Schema 說明

| 資料表 | 說明 | 資料來源 |
|--------|------|----------|
| `stocks` | 股票基本資料 | FinMind |
| `industries` | 產業/族群分類 | MoneyDJ |
| `stock_industry_map` | 股票↔產業對應 | MoneyDJ |
| `daily_prices` | 每日 OHLCV | FinMind / TWSE |
| `institutional_trading` | 三大法人籌碼 | FinMind |
| `margin_trading` | 融資融券 | FinMind |
| `financials` | 季報財務資料 | FinMind |
| `technical_indicators` | 技術指標快取 | 本地計算 |
| `sector_momentum` | 族群輪動熱度 | 本地計算 |
| `announcements` | MOPS 重大訊息 | 爬蟲 |
| `yt_insights` | YouTube 情報 | 爬蟲 + LLM |
| `daily_recommendations` | 每日選股推薦 | LLM Agent |
| `pipeline_logs` | 執行紀錄 | 系統 |

---

## 常見問題

**Q: FinMind 免費版有什麼限制？**
A: 未登入每天約 300 次請求，登入後每天約 600 次。初始化時建議分批執行。

**Q: Docker 啟動後 DB 連不上？**
A: 等 10~15 秒讓 PostgreSQL 初始化完成，或執行 `docker compose logs postgres` 確認。

**Q: MoneyDJ 爬蟲爬不到資料？**
A: MoneyDJ 網站結構偶爾會改版，爬蟲的 CSS selector 可能需要更新。

---

## 下一步（Phase 2）

- [ ] `technical_indicators` 計算模組（pandas_ta）
- [ ] 族群輪動偵測邏輯
- [ ] LLM 財報解讀整合
- [ ] Telegram Bot 推播
