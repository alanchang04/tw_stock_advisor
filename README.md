# 台股個人投資顧問系統

個人使用的台股投資 agent：每日自動抓取全市場股價與籌碼、計算技術指標、偵測族群輪動，
透過 LLM 產生 Top 5 選股推薦，**並記住推薦過的部位、持續追蹤、在符合出場規則時主動提醒離場**。
另有兩個子系統：**市場情報**（財經新聞 / YouTube / ETF 換股，全部 AI 摘要 + 每日彙整）與
**聰明資金**（投信連買 × 統一主動型 ETF 成分股，跟主力做波段）。
結果可推播到 Telegram，並提供 Streamlit 網頁介面與回測工具驗證選股/買賣邏輯。

**線上網頁**：[twstockadvisor-eawsr2oa5u82fpablxhrhk.streamlit.app](https://twstockadvisor-eawsr2oa5u82fpablxhrhk.streamlit.app)

> 📋 完整功能盤點與未來展望見 **[docs/FEATURES.md](docs/FEATURES.md)**。

---

## 技術堆疊

| 項目 | 技術 |
|------|------|
| 語言 | Python 3.12 |
| 資料庫 | PostgreSQL（Neon.tech 雲端，免費方案） |
| LLM | Gemini 2.5 Flash（強制 JSON 輸出 + 重試/容錯） |
| 每日資料來源 | 證交所(TWSE)/櫃買(TPEX) 官方 OpenAPI（免 token、無限流） |
| 歷史回補 | FinMind API（僅首次或長缺口） |
| 情報來源 | Google News RSS（新聞）/ YouTube 頻道 RSS+字幕 / MoneyDJ（產業、ETF 成分股） |
| 技術指標 | ta ≥ 0.9（MA/RSI/MACD/布林通道/KD） |
| 通知 | Telegram Bot |
| 自動排程 | GitHub Actions（每日 21:00 台灣時間，不需開電腦） |
| 網頁介面 | Streamlit Community Cloud（公開 URL，任何裝置可看） |

> **資料來源說明**：每日更新走證交所/櫃買官方 API，一次抓回全市場單日股價與三大法人，約 4 次請求、數秒完成，**不受 FinMind 600 次/hr 限制**。FinMind 只在第一次拉長歷史時用得到。

---

## 系統架構

```
資料層    TWSE/TPEX OpenAPI（每日）/ FinMind（首次歷史）
         Google News RSS / YouTube RSS+字幕 / MoneyDJ（產業、ETF成分股）
   ↓
儲存層    PostgreSQL（Neon.tech 雲端，0.5GB 免費方案）
   ↓
分析層    技術指標（MA/RSI/MACD/KD）/ 族群輪動熱度
   ↓
策略層    strategy.py（進場評分 + 12 條出場規則，集中可調）
   ↓
Agent層   出場檢查(持倉) → 候選篩選 → Gemini 推薦 → 開新部位
   ↓
情報層    市場情報（新聞/YouTube/ETF換股，Gemini 摘要 + 每日彙整）
         聰明資金（投信連買 × 統一ETF成分股 → 黃金交叉）
   ↓
輸出層    Telegram 推播 / Streamlit 網頁（7 頁）/ DB（market_signals 等）
   ↓
排程      GitHub Actions（每日 21:00，weekdays）/ 手動觸發
   ↓
驗證      backtest（round-trip 回測，對比大盤）
```

---

## 快速開始（本機開發環境）

### 前置準備

1. 安裝 [Python 3.12](https://www.python.org/downloads/)
2. 申請 [Neon.tech](https://neon.tech) 免費帳號，建立 `taiwan_stock` 資料庫，取得連線字串
3. 申請 [Google AI Studio](https://aistudio.google.com) 免費帳號，取得 Gemini API Key
4.（選配）申請 FinMind Token；只在初次拉超長歷史時需要

### Step 1 — 安裝套件

```powershell
pip install -r requirements.txt
```

### Step 2 — 設定環境變數

```powershell
copy .env.example .env
```

打開 `.env` 填入（`.env` 絕不可上傳 GitHub，已被 `.gitignore` 排除）：

```
DATABASE_URL=postgresql://帳號:密碼@ep-xxx.aws.neon.tech/neondb?sslmode=require
GEMINI_API_KEY=你的Gemini API Key
TELEGRAM_BOT_TOKEN=你的bot_token        # 選配
TELEGRAM_CHAT_ID=你的chat_id            # 選配
FINMIND_TOKEN=你的FinMind Token         # 選配
```

### Step 3 — 確認 DB 連線

```powershell
python -c "from database.connection import test_connection; test_connection()"
# 應顯示：✅ PostgreSQL 連線成功
```

### Step 4 — 建立歷史資料

```powershell
# 用 OpenAPI 回補近一年資料（推薦，約 20-30 分鐘）
python run_pipeline.py --mode backfill --days 240

# 或用 FinMind（較慢，需 token）
python run_pipeline.py --mode init --days 240
```

### Step 5 — 建立產業分類

```powershell
python run_pipeline.py --mode industry
```

### Step 6 — 算技術指標 + 產生第一份推薦

```powershell
python run_pipeline.py --mode pipeline
```

`pipeline` 會一次做完：補齊缺漏交易日 → 算技術指標 → 檢查出場 → 選股 → LLM 推薦 → 開部位 → 推 Telegram。

### Step 7 — 本機啟動網頁

```powershell
streamlit run app.py
# 開啟 http://localhost:8501
```

---

## 雲端部署（GitHub Actions + Streamlit Cloud）

### GitHub Secrets 設定（讓 Actions 能連 DB 和發 Telegram）

1. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
2. 逐一新增以下 5 個（值直接貼，不需要引號）：

| Secret 名稱 | 說明 |
|-------------|------|
| `DATABASE_URL` | Neon 完整連線字串 |
| `GEMINI_API_KEY` | Gemini API Key |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 你的 Telegram Chat ID |
| `FINMIND_TOKEN` | FinMind Token |

### GitHub Personal Access Token（讓網頁按鈕能手動觸發 Actions）

1. `https://github.com/settings/tokens` → **Generate new token (classic)**
2. Expiration：**No expiration**
3. Scopes：只勾 **`workflow`**
4. 複製 token

### Streamlit Cloud Secrets 設定

Streamlit Cloud → App → **Settings → Secrets**，貼入（TOML 格式，值需加引號）：

```toml
DATABASE_URL       = "postgresql://帳號:密碼@ep-xxx.aws.neon.tech/neondb?sslmode=require"
GEMINI_API_KEY     = "你的key"
TELEGRAM_BOT_TOKEN = "你的token"
TELEGRAM_CHAT_ID   = "你的chat_id"
FINMIND_TOKEN      = "你的token"
GITHUB_PAT         = "github_pat_xxxx..."
```

設定完成後網頁側邊欄的「立即更新資料」按鈕即可從任何裝置觸發 GitHub Actions 更新。

---

## 自動排程

| 觸發方式 | 時間 | 執行內容 |
|---------|------|---------|
| GitHub Actions 自動 | 每日 21:00（週一至週五） | `--mode auto`（完整 pipeline + Telegram 通知） |
| 網頁按鈕手動 | 任何時間 | 觸發 GitHub Actions，約 3-5 分鐘後生效 |

**不需要開著電腦**，GitHub Actions 在雲端執行；執行紀錄可在 repo → **Actions** 頁面查看 log。

---

## Telegram 通知設定（選配但推薦）

1. Telegram 搜尋 `@BotFather` → 傳 `/newbot` → 取得 **bot token**
2. 對你的新 bot 傳一則訊息（先傳才能拿到 chat_id）
3. 取得 chat_id：

```powershell
python -c "import os,requests; from dotenv import load_dotenv; load_dotenv(); tok=os.getenv('TELEGRAM_BOT_TOKEN'); print([u['message']['chat']['id'] for u in requests.get(f'https://api.telegram.org/bot{tok}/getUpdates').json()['result'] if 'message' in u])"
```

4. 填入 `.env`，GitHub Secrets，以及 Streamlit Secrets

設定後：成功 → 推當日「進場推薦 + 出場提醒」；失敗 → 推錯誤訊息。

---

## 常用指令

```powershell
# ── 每日 ──
python run_pipeline.py --mode pipeline    # 完整流程（手動補跑）
python run_pipeline.py --mode auto        # 同 pipeline，依星期自動切換週末/平日模式
python run_pipeline.py --mode daily       # 只抓最新股價（不算指標、不推薦）
python run_pipeline.py --mode backfill    # 補齊 DB 缺漏的交易日

# ── 分析/推薦（pipeline 內含，也可單獨跑）──
python run_pipeline.py --mode technical   # 重算技術指標
python run_pipeline.py --mode sector      # 計算族群輪動熱度
python run_pipeline.py --mode recommend   # 出場檢查 + 選股 + LLM 推薦 + 開部位
python run_pipeline.py --mode market      # 市場情報 + 聰明資金（ETF換股/新聞/YT/彙整）

# ── 建置/維護 ──
python run_pipeline.py --mode init --days 240    # FinMind 拉歷史（首次，慢）
python run_pipeline.py --mode industry           # 更新產業分類（建議每週）

# ── 回測 ──
python run_pipeline.py --mode backtest    # 回測選股+買賣邏輯績效

# ── Telegram Bot 指令處理 ──
python run_pipeline.py --mode bot         # 處理一次 Bot 的 /help /status /positions
```

---

## 策略調整與回測

所有買賣參數集中在 **[agent/strategy.py](agent/strategy.py)** 的 `STRATEGY` dict：

**進場**：候選池過濾（RSI/股價/成交量）、評分權重（均線交叉、突破、MACD、法人買超、RSI 甜蜜帶）

**出場（12 條規則，可個別開關）**：
- 停損（預設 -7%）
- 固定停利（+20%）
- 移動停利（從高點回撤 -10%）
- KD 高檔死叉 + MACD 轉負
- 均線死亡交叉（MA5 < MA20）
- 跌破 MA20 / MA5
- 跌破前波低點（pivot）
- 跌破前 N 根實體棒底部
- 長上引線爆量
- 持有到期（預設 30 日）

改完直接重跑回測比較優劣：

```powershell
python run_pipeline.py --mode backtest
```

---

## 部位追蹤

- 每次 LLM 推薦的股票會寫入 `positions` 表開始追蹤（進場日、進場價）
- 每日 pipeline 先檢查所有持倉，符合任一出場規則就標記平倉並發 Telegram 提醒
- 出場檢查**獨立於 LLM**：即使 LLM 當機，賣出提醒照樣發
- 網頁「持倉追蹤」頁面即時顯示所有持倉的損益與出場訊號

---

## 資料庫 Schema

| 資料表 | 說明 | 資料來源 |
|--------|------|----------|
| stocks | 股票基本資料 | OpenAPI / FinMind |
| industries | 產業分類 | MoneyDJ |
| stock_industry_map | 股票產業對應 | MoneyDJ |
| daily_prices | 每日 OHLCV | TWSE/TPEX OpenAPI（init 時 FinMind） |
| institutional_trading | 三大法人籌碼（單位：股） | TWSE/TPEX OpenAPI（init 時 FinMind） |
| technical_indicators | 技術指標快取（MA/RSI/MACD/KD） | 本地計算 |
| sector_momentum | 族群輪動熱度 | 本地計算 |
| **positions** | **部位追蹤（進場/出場/報酬）** | **系統** |
| daily_recommendations | 每日推薦結果 | LLM |
| **market_signals** | **市場情報（新聞/YouTube/ETF換股/彙整/聰明資金）** | 多來源 + Gemini |
| etf_watchlist | ETF 追蹤名單（15 檔） | 系統 |
| etf_holdings | ETF 成分股快照 | MoneyDJ |
| etf_changes | ETF 換股記錄（新增/加碼/減碼/移除） | 本地計算 |
| pipeline_logs | 執行紀錄 | 系統 |
| margin_trading | 融資融券 | 待開發 |
| financials | 季報財務資料 | 待開發 |
| announcements | MOPS 重大訊息 | 待開發 |

> `market_signals` 的 `signal_type` 支援：`news` / `youtube` / `etf_change` / `mops` / `digest` / `smart_money`。
> 建置時依序執行 `database/migrations/` 內的 `03`~`05` migration。

---

## 重要注意事項

- `.env` 含 API Key，**絕不可上傳 GitHub**（已被 `.gitignore` 排除）
- 資料庫在 Neon.tech 雲端，**不需要 Docker**；Neon 免費方案 0.5GB，閒置 5 分鐘會自動休眠，首次連線 <1 秒喚醒
- GitHub Secrets 和 Streamlit Secrets 要分開設定（兩邊都需要 DATABASE_URL 等）
- GITHUB_PAT 只需要設在 Streamlit Secrets（不需要設在 GitHub Secrets）

---

## 功能總覽與未來展望

詳見 **[docs/FEATURES.md](docs/FEATURES.md)**（完整功能盤點、資料來源、短中長期規劃、已知限制）。

其他文件：
- [docs/TODO.md](docs/TODO.md) — 開發待辦清單（改進項目 + 新功能詳細規格）
- [docs/neon_deployment_guide.md](docs/neon_deployment_guide.md) — Neon 雲端資料庫部署
- [docs/shioaji_setup.md](docs/shioaji_setup.md) — 永豐金 Shioaji API 設定（未來擴充）
