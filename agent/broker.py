"""
agent/broker.py

交易層抽象——pipeline 只跟「Broker 介面」講話，底下可無痛替換：
    PaperBroker      紙上模擬：委託寫進 pending_orders，隔日開盤價±滑價成交（＝階段0b）
    ShioajiBroker    永豐 Shioaji：模擬（現在）→ 實盤（之後），同一份上層程式碼

每日流程對應介面三個方法（順序固定）：
    1. sync(on_date)              先把「已成交」的回帳（paper: 昨日掛單於今開盤成交；
                                  live: 讀券商成交回報），更新 positions 帳本
    2. submit_exits(on_date)      依出場規則掛/送賣單
    3. submit_entries(picks, d)   依推薦掛/送買單

用環境變數 BROKER 選擇（預設 paper，行為與現況完全相同）：
    BROKER=paper     紙上模擬（預設）
    BROKER=shioaji   永豐（需 shioaji 套件 + 金鑰；submit/sync 尚未接 pipeline，
                     先用 scripts/shioaji_smoke.py 驗證流程，通過後才接）
"""
from __future__ import annotations
import os
from datetime import date
from loguru import logger


class Broker:
    name = "base"

    def connect(self):
        pass

    def sync(self, on_date: date) -> dict:
        """回帳已成交的委託，回傳 {"entries": [...], "exits": [...]}。"""
        raise NotImplementedError

    def submit_exits(self, on_date: date) -> list[dict]:
        raise NotImplementedError

    def submit_entries(self, picks: list[dict], on_date: date) -> list[dict]:
        raise NotImplementedError

    def disconnect(self):
        pass


class PaperBroker(Broker):
    """紙上模擬：完全沿用 portfolio 的階段0b 邏輯（隔日開盤價±滑價成交）。"""
    name = "paper"

    def sync(self, on_date: date) -> dict:
        from agent.portfolio import fill_pending_orders
        return fill_pending_orders(on_date)

    def submit_exits(self, on_date: date) -> list[dict]:
        from agent.portfolio import queue_exits
        return queue_exits(on_date)

    def submit_entries(self, picks: list[dict], on_date: date) -> list[dict]:
        from agent.portfolio import queue_entries
        return queue_entries(picks, on_date)


class ShioajiBroker(Broker):
    """
    永豐 Shioaji 券商介面（模擬/實盤同一份程式）。

    現階段（老師任務：先證明流程可行）只實作可獨立驗證的「低階原語」——
    連線、取 contract、下單、查成交——由 scripts/shioaji_smoke.py 在盤中呼叫驗證。
    submit_*/sync 這些「接進每日 pipeline 的高階流程」刻意先不接，等：
      (1) smoke 驗證通過  (2) 你辦好永豐帳戶  (3) 策略跨週期驗證定版
    三者到齊，才把 pending_orders → place_order → 成交回報 → positions 串起來，
    避免在還沒準備好時就對（即使是模擬）帳戶自動送單。

    金鑰從環境變數讀（勿寫進程式碼／勿進版控）：
      SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY
    模擬模式免真實下單風險；模擬測試時段為平日 08:00~20:00。
    """
    name = "shioaji"

    def __init__(self, simulation: bool = True):
        self.simulation = simulation
        self.api = None

    def connect(self):
        try:
            import shioaji as sj
        except ImportError as e:
            raise RuntimeError(
                "未安裝 shioaji 套件。請先 `pip install shioaji`，"
                "並確認在平日 08:00~20:00 的模擬測試時段執行。") from e

        api_key = os.getenv("SHIOAJI_API_KEY", "")
        secret  = os.getenv("SHIOAJI_SECRET_KEY", "")
        if not api_key or not secret:
            raise RuntimeError(
                "缺 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY 環境變數。"
                "到永豐 Shioaji 後台申請模擬金鑰後設進 .env（勿進版控）。")

        self.api = sj.Shioaji(simulation=self.simulation)
        self.api.login(api_key=api_key, secret_key=secret)
        mode = "模擬" if self.simulation else "⚠️實盤"
        logger.info(f"Shioaji 已登入（{mode}）")
        return self.api

    # ── 低階原語（smoke 驗證用；之後高階流程也會呼叫）────────────────
    def contract(self, stock_id: str):
        return self.api.Contracts.Stocks[stock_id]

    def place(self, side: str, stock_id: str, qty_lots: int, price: float,
              price_type: str = "LMT", order_type: str = "ROD"):
        """下一張限價股票單（qty_lots=張數）。回傳 shioaji Trade 物件。"""
        import shioaji as sj
        action = sj.Action.Buy if side == "buy" else sj.Action.Sell
        order = sj.order.StockOrder(
            action=action,
            price=price,
            quantity=qty_lots,
            price_type=getattr(sj.constant.StockPriceType, price_type),
            order_type=getattr(sj.constant.OrderType, order_type),
            order_lot=sj.constant.StockOrderLot.Common,
            order_cond=sj.constant.StockOrderCond.Cash,
        )
        return self.api.place_order(self.contract(stock_id), order)

    def refresh(self):
        """向券商更新所有委託/成交狀態（回填 trade.status）。"""
        self.api.update_status(self.api.stock_account)
        return self.api.list_trades()

    def cancel(self, trade):
        return self.api.cancel_order(trade)

    def disconnect(self):
        if self.api is not None:
            try:
                self.api.logout()
            except Exception as e:
                logger.warning(f"Shioaji 登出失敗（忽略）: {e}")

    # ── 高階流程（尚未接 pipeline，見 class docstring）─────────────
    def sync(self, on_date: date) -> dict:
        raise NotImplementedError(
            "ShioajiBroker 高階流程尚未接 pipeline——先用 scripts/shioaji_smoke.py 驗證")

    def submit_exits(self, on_date: date) -> list[dict]:
        raise NotImplementedError("同上，尚未接 pipeline")

    def submit_entries(self, picks: list[dict], on_date: date) -> list[dict]:
        raise NotImplementedError("同上，尚未接 pipeline")


def get_broker(name: str | None = None) -> Broker:
    """依 BROKER 環境變數（或參數）建 broker，預設 paper（行為同現況）。"""
    name = (name or os.getenv("BROKER", "paper")).lower()
    if name == "paper":
        return PaperBroker()
    if name == "shioaji":
        return ShioajiBroker(simulation=os.getenv("SHIOAJI_SIMULATION", "1") != "0")
    raise ValueError(f"未知的 BROKER：{name}（可用：paper / shioaji）")
