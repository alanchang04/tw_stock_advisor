# 遷移 DB 到 Neon.tech + 部署 Streamlit Community Cloud

## 為什麼要做這件事

| 現況 | 遷移後 |
|------|--------|
| PostgreSQL 跑在本機 Docker 裡 | PostgreSQL 在雲端（Neon.tech），免費 |
| Streamlit 只有本機能開 | Streamlit Community Cloud 有永久公開 URL |
| 教授需要你的電腦開著才能看 | 任何時間、任何裝置都能看 |
| Docker 要佔記憶體 | 完全不需要 Docker（也不需要開著電腦） |

---

## 第一步：建立 Neon 免費資料庫

1. 開瀏覽器到 **https://neon.tech**，點「Sign Up」
2. 用 GitHub 帳號登入（最方便）
3. 建立新 Project（名稱自取，例如 `taiwan-stock`）
4. Region 選 **AWS Asia Pacific (Tokyo)**（最近）
5. 建立完成後，點左側選單 **Dashboard → Connection Details**
6. 找到 **Connection string**，格式長這樣：
   ```
   postgresql://alanchang:密碼@ep-xxx-xxx.ap-southeast-1.aws.neon.tech/taiwan_stock?sslmode=require
   ```
   複製這一整行，等一下要用。

---

## 第二步：把本機資料匯出

開啟 PowerShell，在 `taiwan_stock_advisor` 資料夾執行：

```powershell
# 把整個 DB 匯出成 SQL 檔案（包含結構和資料）
docker exec stock_advisor_db pg_dump -U stock_user taiwan_stock > db_export.sql
```

這會在專案資料夾產生 `db_export.sql`，大小視資料量而定（幾十 MB 正常）。

---

## 第三步：把資料匯入 Neon

需要安裝 **psql** 工具（如果沒有）：
- 到 https://www.postgresql.org/download/windows/ 下載，安裝時勾選「Command Line Tools」

安裝完執行（把連線字串換成你剛才複製的）：

```powershell
# 匯入到 Neon（會要求輸入密碼，從連線字串裡找）
psql "postgresql://alanchang:密碼@ep-xxx.ap-southeast-1.aws.neon.tech/taiwan_stock?sslmode=require" -f db_export.sql
```

這可能需要 5-20 分鐘，看資料量。

---

## 第四步：更新 .env 連線設定

打開 `.env`，把 `DATABASE_URL` 改成 Neon 的連線字串：

```env
# 原本（本機 Docker）
DATABASE_URL=postgresql://stock_user:stock_pass@localhost:5432/taiwan_stock

# 改成（Neon 雲端）
DATABASE_URL=postgresql://alanchang:密碼@ep-xxx.ap-southeast-1.aws.neon.tech/taiwan_stock?sslmode=require
```

其他欄位（API Key、Telegram 等）不用動。

---

## 第五步：本機測試

```powershell
python -c "from database.connection import test_connection; test_connection()"
```

看到 `✅ 資料庫連線成功` 就代表切換完成。  
這時候 **Docker 可以完全關掉**，不再需要了。

---

## 第六步：部署到 Streamlit Community Cloud

1. 確認 `app.py` 已經 push 到 GitHub（你說已經推了✅）

2. 開瀏覽器到 **https://share.streamlit.io**，用 GitHub 帳號登入

3. 點「New app」→ 選你的 repo → 主程式選 `app.py` → Branch 選 `main`

4. **重要：設定 Secrets（取代 .env）**  
   點「Advanced settings」→「Secrets」，貼入以下內容：
   ```toml
   DATABASE_URL = "postgresql://alanchang:密碼@ep-xxx.aws.neon.tech/taiwan_stock?sslmode=require"
   GEMINI_API_KEY = "你的 Gemini API Key"
   TELEGRAM_BOT_TOKEN = "你的 Bot Token"
   TELEGRAM_CHAT_ID = "你的 Chat ID"
   FINMIND_TOKEN = "你的 FinMind Token"
   ```
   格式是 TOML，字串要加引號。

5. 點「Deploy」，等 2-3 分鐘部署完成

6. Streamlit 會給你一個網址，類似：  
   `https://你的帳號-taiwan-stock-advisor-app-xxxxx.streamlit.app`  
   這個網址是永久的，任何地方都能開。

---

## 部署後還需要 Docker 嗎？

| 用途 | 需要 Docker？ |
|------|-------------|
| 跑 Streamlit 網頁 | ❌ 不需要（用 Neon） |
| 每日 21:00 自動更新 | ❌ 不需要（daily_update.bat 直接連 Neon） |
| 本機開發測試 | ❌ 不需要（.env 指向 Neon） |
| 完全不需要了 | ✅ 可以解除安裝或只是不啟動 |

---

## Neon 免費方案限制

| 項目 | 免費方案 |
|------|---------|
| 儲存空間 | 0.5 GB |
| 計算時數 | 191.9 小時/月（足夠） |
| 閒置自動休眠 | 5 分鐘後休眠，首次連線 <1 秒喚醒 |
| 並發連線 | 足夠（此系統單用戶） |

目前系統資料大小估計：1964 支 × 250 天 × 2 張表 ≈ 約 150-200 MB，在免費方案內。
