# 永豐金 Shioaji API 設定指南

## 什麼是 Shioaji？

永豐金證券提供的 Python 官方 API，開立帳戶後免費使用。  
可以做到：
- 訂閱即時 Tick 報價（每筆成交都能收到）
- 查詢歷史盤中分鐘資料
- 下單買賣（自動化交易用）
- 查詢帳戶餘額、部位

官方文件：https://sinotrade.github.io/Shioaji/

---

## 第一步：開立永豐金證券帳戶

1. 到 **https://www.sinotrade.com.tw** 點「開戶」
2. 準備文件：身分證、第二證件（健保卡或駕照）、銀行存摺
3. 選擇「網路開戶」，可線上完成（約 5-10 分鐘填表）
4. 審核通過後（通常 1-2 個工作天），會收到帳號

---

## 第二步：申請 API 憑證

帳戶開立後，需要額外申請 API 權限：

1. 登入永豐金網路銀行/證券系統
2. 到「API 服務申請」（或聯絡客服申請開通 API 功能）
3. 申請完成後會取得：
   - **API Key**：你的身分識別
   - **API Secret Key**（或稱 Secret）
   - **憑證檔案**（`.pfx` 或類似格式，用於下單簽章）

> ⚠️ 注意：**查詢報價不需要憑證，只需要帳號密碼**。下單才需要憑證。

---

## 第三步：安裝 Shioaji

```powershell
pip install shioaji
```

---

## 第四步：設定 .env

在 `.env` 裡加入以下欄位：

```env
SHIOAJI_API_KEY=你的API_KEY
SHIOAJI_SECRET_KEY=你的SECRET_KEY
SHIOAJI_ACCOUNT_ID=你的帳號（身分證字號或帳號）
SHIOAJI_PASSWORD=你的密碼
# 下單用（選填，先做報價不需要）
SHIOAJI_CERT_PATH=C:/path/to/your/cert.pfx
SHIOAJI_CERT_PASSWORD=憑證密碼
```

---

## 第五步：加入系統設定

在 `config/settings.py` 裡新增（參考現有格式）：

```python
class ShioajiConfig:
    API_KEY     = os.getenv("SHIOAJI_API_KEY", "")
    SECRET_KEY  = os.getenv("SHIOAJI_SECRET_KEY", "")
    ACCOUNT_ID  = os.getenv("SHIOAJI_ACCOUNT_ID", "")
    PASSWORD    = os.getenv("SHIOAJI_PASSWORD", "")
    CERT_PATH   = os.getenv("SHIOAJI_CERT_PATH", "")
    CERT_PASSWORD = os.getenv("SHIOAJI_CERT_PASSWORD", "")
```

---

## 第六步：即時報價範例

以下是最基本的即時報價訂閱，可以直接測試：

```python
import shioaji as sj
from config.settings import ShioajiConfig

# 登入
api = sj.Shioaji()
api.login(
    api_key=ShioajiConfig.API_KEY,
    secret_key=ShioajiConfig.SECRET_KEY,
)

# 訂閱 2884 玉山金即時 Tick
@api.on_tick_stk_v1()
def on_tick(exchange, tick):
    print(f"{tick.code} 成交價={tick.close} 量={tick.volume} 時間={tick.datetime}")

contract = api.Contracts.Stocks["2884"]
api.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)

# 讓程式持續運行
import time
time.sleep(60)  # 訂閱 60 秒

api.logout()
```

---

## 整合到本系統的規劃

取得帳戶後，分兩階段整合：

### 階段 A：盤中即時報價（替代 TWSE 快照）

新增 `data_pipeline/fetchers/shioaji_fetcher.py`：
- 登入 Shioaji
- 訂閱持倉中股票的即時 Tick
- 每 15 分鐘把最新價格寫入 DB
- Streamlit 按鈕改為呼叫此 fetcher

### 階段 B：自動化下單（未來）

新增 `agent/trader.py`：
- 呼叫 `decide_exit()` 確認出場訊號
- 若確認出場 → `api.place_order(...)` 掛市價單賣出
- 若確認進場 → `api.place_order(...)` 掛市價單買進
- 紀錄所有委託單狀態

下單的部分需要憑證（.pfx 檔）和額外的風控邏輯（資金管理、倉位上限等），
建議在回測驗證策略穩定後再實作。

---

## 注意事項

- Shioaji 只在台灣交易時間（09:00-13:30）有即時 Tick，盤後只能查歷史
- 免費帳戶有連線數量限制（同時訂閱的股票數）
- 使用 API 下單仍屬合法零售交易，不需要特殊執照
- 沙盒（模擬）環境可以先測試下單邏輯，不影響真實帳戶
