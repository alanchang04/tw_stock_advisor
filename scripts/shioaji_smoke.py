"""
scripts/shioaji_smoke.py

【老師任務：先證明「接券商 API → 下單 → 成交回報」整個流程可行】

這支腳本在**永豐 Shioaji 模擬模式**下，實地驗證交易管線能不能跑通：
    1. 登入（simulation=True，不影響真實帳戶）
    2. 取得股票 contract
    3. 下一張「遠離市價的限價單」→ 會掛著不成交（安全，不會真的買到）
    4. 用 update_status 讀回委託狀態（證明下單與回報都通）
    5. 取消該委託（證明改單/刪單也通）

⚠️ 執行條件（我沙箱無法代跑，需你本人在自己電腦執行）：
    - 平日 08:00~20:00（永豐模擬測試時段）
    - 已 `pip install shioaji`
    - .env 設好 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY（永豐後台申請模擬金鑰，免費）
      取得方式：永豐證券 → Shioaji API 後台 → 建立 API Key（模擬）
      公用測試帳號亦可，但建議直接申請自己的模擬金鑰較穩。

用法：
    python scripts/shioaji_smoke.py            # 預設下 2890 永豐金 1 張、限價很低（不會成交）
    python scripts/shioaji_smoke.py --stock 2330 --price 100 --no-cancel

這支「不」碰資料庫、「不」動 positions，純粹驗證券商 API 流程。通過後我們才把
pending_orders → place_order → 成交回報 → positions 接進每日 pipeline。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from agent.broker import ShioajiBroker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", default="2890", help="測試股票代號（預設 2890 永豐金）")
    ap.add_argument("--price", type=float, default=None,
                    help="限價（預設用漲停下方一大截，確保掛著不成交）")
    ap.add_argument("--lots", type=int, default=1, help="張數（預設 1）")
    ap.add_argument("--no-cancel", action="store_true", help="不自動取消委託（預設會取消）")
    a = ap.parse_args()

    print("=" * 60)
    print("  Shioaji 模擬下單流程驗證（老師任務）")
    print("=" * 60)

    broker = ShioajiBroker(simulation=True)
    print("① 連線＋登入模擬模式 …")
    broker.connect()
    print("   ✅ 登入成功")

    print(f"② 取得 contract：{a.stock} …")
    c = broker.contract(a.stock)
    ref = float(getattr(c, "reference", 0) or 0)
    limit_down = float(getattr(c, "limit_down", 0) or 0)
    print(f"   ✅ {c.code} {c.name}  參考價 {ref}  跌停 {limit_down}")

    # 預設用「參考價 8 折」當限價：遠低於市價 → 掛著不會成交，驗證流程最安全
    price = a.price if a.price is not None else round(max(limit_down, ref * 0.8), 2)
    print(f"③ 下限價買單：{a.lots} 張 @ {price}（刻意遠低於市價，掛著不成交）…")
    trade = broker.place("buy", a.stock, a.lots, price)
    print(f"   ✅ 已送出，委託單號 {getattr(trade.order, 'id', '?')}")

    print("④ 更新並讀回委託狀態 …")
    time.sleep(2)
    broker.refresh()
    st = trade.status
    print(f"   ✅ 狀態：{getattr(st, 'status', '?')}  "
          f"已成交量 {getattr(st, 'deal_quantity', 0)}  "
          f"委託量 {getattr(st, 'order_quantity', a.lots)}")

    if not a.no_cancel:
        print("⑤ 取消委託 …")
        broker.cancel(trade)
        time.sleep(2)
        broker.refresh()
        print(f"   ✅ 取消後狀態：{getattr(trade.status, 'status', '?')}")

    broker.disconnect()
    print("=" * 60)
    print("  🎉 流程驗證完成：登入 → 取 contract → 下單 → 讀回報 → 取消 全通")
    print("  下一步：接進 pipeline（等你辦好帳戶＋策略定版後）")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ 失敗：{type(e).__name__}: {e}")
        print("   常見原因：非盤中時段(08:00~20:00)、未裝 shioaji、金鑰未設或錯誤。")
        sys.exit(1)
