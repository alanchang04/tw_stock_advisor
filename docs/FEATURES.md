# 功能總覽與未來展望

> 台股個人投資顧問系統 — 目前功能盤點（2026-07）與後續規劃。
> 部署細節請看 [README](../README.md)、[Neon 部署指南](neon_deployment_guide.md)、[Shioaji 設定](shioaji_setup.md)。

---

## 一、系統概觀

一套**全自動、免開電腦**的台股投資輔助系統：

```
每日 21:00（GitHub Actions 雲端排程）
   │
   ├─ 抓全市場股價 + 三大法人（TWSE/TPEX OpenAPI）
   ├─ 算技術指標（MA/RSI/MACD/KD/布林）
   ├─ 族群輪動熱度
   ├─ 出場檢查（持倉）→ 選股 → Gemini 推薦 → 開新部位
   ├─ 市場情報（ETF換股 / 財經新聞 / YouTube，全部 AI 摘要 + 每日彙整）
   ├─ 聰明資金（投信連買 × 統一ETF成分股）
   │
   ├─→ Telegram 推播（進場推薦 + 出場提醒）
   └─→ Streamlit 網頁（任何裝置可看）
```

三條主線：
1. **選股 Agent**：LLM 選股 → 開部位 → 每日追蹤 → 規則出場提醒（核心，最成熟）
2. **市場情報**：新聞 / YouTube / ETF 換股，AI 濃縮成每日重點
3. **聰明資金**：跟著投信與統一主動型 ETF 做波段

---

## 二、核心功能

### 1. 每日資料管線
- **每日更新**走證交所/櫃買官方 OpenAPI，一次抓回全市場單日股價與三大法人（約 4 次請求、數秒完成，**免 token、不受 FinMind 限流**）。
- **自動補洞**：`backfill` 從 DB 最後一天接續補到今天，某天漏跑下次自動補回。
- **歷史回補**：FinMind 只在首次拉長歷史或長缺口時使用。
- 相關：[twse_fetcher.py](../data_pipeline/fetchers/twse_fetcher.py)、[finmind_fetcher.py](../data_pipeline/fetchers/finmind_fetcher.py)

### 2. 技術分析與族群輪動
- 技術指標：均線、RSI、MACD、KD、布林通道（`ta>=0.9`），增量計算只寫最近數日。
- 族群輪動熱度：計算各產業近期相對強度，找輪動中的族群。
- 相關：[technical.py](../data_pipeline/analysis/technical.py)、[sector_momentum.py](../data_pipeline/analysis/sector_momentum.py)

### 3. 選股 Agent（核心）
- **進場**：候選池過濾（RSI/股價/成交量）+ 多因子評分（均線交叉、突破、MACD、法人買超、RSI 甜蜜帶）。
- **LLM 推薦**：Gemini 2.5 Flash 產生 Top 5 選股，強制 JSON 輸出、含重試容錯、關閉 thinking 以省 token。
- **部位追蹤**：推薦的股票寫入 `positions` 表，記進場日/價，每日更新損益。
- **出場（12 條規則、可個別開關）**：停損 -7%、停利 +20%、移動停利回撤 -10%、KD 高檔死叉+MACD 轉負、均線死亡交叉、跌破 MA20/MA5、跌破前波低點、長上引線爆量、持有到期 30 日等。
- **出場獨立於 LLM**：即使 LLM 當機，賣出提醒照發。
- 買賣參數全部集中在 [strategy.py](../agent/strategy.py) 的 `STRATEGY` dict，改完可直接回測驗證。
- 相關：[daily_runner.py](../agent/daily_runner.py)、[stock_selector.py](../agent/stock_selector.py)、[llm_advisor.py](../agent/llm_advisor.py)、[portfolio.py](../agent/portfolio.py)

### 4. 回測
- Round-trip 回測（完整買進→賣出交易），輸出勝率、平均報酬、平均持有天數，並對比大盤。
- 用於驗證策略調整前後優劣。
- 相關：[backtest.py](../agent/backtest.py)

### 5. 市場情報（AI 摘要）
每日自動蒐集三類情報，**每一則都用 Gemini 產生精華重點**（非只放連結），最後再生成一份「每日彙整」：
- **財經新聞**：Google News RSS（台股/半導體/ETF/費半），批次 Gemini 分析出重點+情緒+相關個股。
- **YouTube**：追蹤財經頻道（錢線百分百等），抓字幕後 AI 摘要（Shorts 不處理）。
- **ETF 換股**：偵測追蹤名單 ETF 的成分股新增/加碼/減碼/移除。
- **每日彙整**：把當日所有情報濃縮成【被看好的族群】【風險/利空】【值得關注個股】【市場氛圍】。
- 相關：[news_scraper.py](../data_pipeline/scrapers/news_scraper.py)、[youtube_scraper.py](../data_pipeline/scrapers/youtube_scraper.py)、[daily_digest.py](../data_pipeline/analysis/daily_digest.py)

### 6. 聰明資金（跟主力做波段）
專門追蹤「聰明錢」的兩個訊號並找重疊：
- **投信連買排行**：近 N 日投信淨買超天數/大量（每日、即時，來源 TWSE 三大法人）。
- **統一 ETF 成分股**：追蹤統一台股增長(00981A)等主動型 ETF 的持股與換股（來源 MoneyDJ）。
- **黃金交叉**：同時被投信連買 + 統一 ETF 加碼的股票，波段勝率最高。
- 相關：[smart_money.py](../data_pipeline/analysis/smart_money.py)、[etf_fetcher.py](../data_pipeline/fetchers/etf_fetcher.py)

> ⚠️ ETF 持股來源 MoneyDJ 約**每月更新、僅前十大**；主動型 ETF 的每日換股抓不到即時。
> 因此**每日的主力訊號以投信買超為準**，ETF 持股為輔助確認。詳見「未來展望」。

### 7. 通知、網頁、排程
- **Telegram**：成功推當日「進場推薦 + 出場提醒」；失敗推錯誤訊息；支援 `/help /status /positions` 互動指令。
- **Streamlit 網頁**：公開 URL，任何裝置可看，含「立即更新」按鈕手動觸發雲端 pipeline。
- **GitHub Actions**：每日 21:00（週一至週五）自動跑，不需開電腦。
- 相關：[notifier.py](../agent/notifier.py)、[app.py](../app.py)、[.github/workflows/daily_update.yml](../.github/workflows/daily_update.yml)

---

## 三、網頁介面（Streamlit 七頁）

| 頁面 | 內容 |
|------|------|
| 📊 首頁 | 系統狀態、持倉概況、法人近 5 日買超 Top 5 |
| 📦 持倉追蹤 | 所有持倉損益、出場訊號即時提醒 |
| 🏦 法人動向 | 三大法人買賣超排行 |
| 📉 個股走勢 | 個股 K 線 + 技術指標 |
| 🔄 歷史績效 | 回測報酬分布、交易紀錄、出場原因統計 |
| 📰 市場情報 | 每日彙整 + ETF換股 / 財經新聞 / YouTube（AI 摘要）|
| 🧠 聰明資金 | 黃金交叉 / 統一ETF動態 / 投信連買排行 |

---

## 四、資料來源總表

| 資料 | 來源 | 頻率 | 備註 |
|------|------|------|------|
| 每日股價、三大法人 | TWSE/TPEX OpenAPI | 每日 | 免 token、免限流 |
| 歷史股價回補 | FinMind | 首次 | 需 token |
| 產業分類 | MoneyDJ | 每週 | 爬蟲 |
| ETF 成分股 | MoneyDJ | 約每月 | 爬蟲、前十大 |
| 財經新聞 | Google News RSS | 每日 | Gemini 摘要 |
| YouTube 情報 | 頻道 RSS + 字幕 | 每日 | Gemini 摘要 |
| AI 分析 | Gemini 2.5 Flash | 每日 | litellm |

---

## 五、未來展望

### 短期（資料完整度）
- **ETF 每日 PCF**：目前 MoneyDJ 只有月更前十大。真正的每日申購買回清單在統一投信官網（JS 動態渲染），需 Playwright/Selenium 無頭瀏覽器；或改用付費 FinMind。待月更前十大不夠用時再上。
- **YouTube 字幕雲端封鎖**：`youtube-transcript-api` 在 GitHub Actions 的資料中心 IP 常被 YouTube 擋，導致部分影片無摘要。可評估 proxy 或改用其他字幕來源。
- **每日彙整開頭贅字**：Gemini 偶爾回「好的，根據您提供的…」，可加後處理去除開場白。

### 中期（功能擴充）
- **Shioaji 接入**（永豐金 API，[設定指南](shioaji_setup.md) 已備）：即時 Tick 報價、盤中分鐘資料，可取代 FinMind 當更即時的報價來源。ETF 成分股 Shioaji 無法提供（已查證）。
- **待開發資料表**：`margin_trading`（融資融券）、`financials`（季報財務）、`announcements`（MOPS 重大訊息）——schema 已建，尚未接資料源。
- **聰明資金回測**：把「投信連買 × ETF 加碼」訊號納入回測，驗證波段勝率。

### 長期（策略與自動化）
- **選股策略優化**：現行選股在多頭期跑輸大盤，需重新檢視進場評分與出場規則（改 strategy.py 後用 backtest 驗證，勿每天亂改否則無法回測）。
- **半自動/自動下單**：Shioaji 支援下單，可在嚴格風控下做「推薦→一鍵下單」或全自動執行。
- **多元 LLM/多模型比較**：目前用 Gemini，可評估其他模型在選股/摘要品質上的差異。

---

## 六、已知限制

- MoneyDJ ETF 資料月更、前十大 → 主動 ETF 每日換股看不到即時（用投信買超補）。
- YouTube 字幕在雲端 IP 可能抓不到 → 該影片顯示「無字幕」不做摘要。
- Neon 免費方案 0.5GB、閒置休眠（首次連線 <1 秒喚醒）。
- Gemini 免費額度有限：模型固定 `gemini-2.5-flash`（2.0-flash 免費額度為 0），呼叫需 `reasoning_effort="disable"` 避免思考吃光 token。
