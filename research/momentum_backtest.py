"""MOM-1 組合模擬引擎（SPEC `docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.4~§7.6）。

**這個模組在 F1 開封前必須先提交。** 它是把已凍結規則變成實際部位帳的最後一塊；
`research/momentum.py` 只到訊號與選股，`research/momentum_execution.py` 只到單筆
委託的定價與可成交判定，兩者都刻意不含績效。

三個實作決定，全部由規格條文推得，不是自由選擇
------------------------------------------------

1. **目標股數在每次再平衡凍結，未成交的部分才逐日重試。** 規格 §7.3 是
   **每月**再平衡、§7.5 要求「未成交單逐一實際交易日重試」。兩者合起來的意思是：
   目標股數於該次再平衡的首個成交嘗試日決定後就固定，之後每個交易日只補未成交的差額。

   **第一版寫錯成「每個交易日以當日開盤價重新推導目標股數」**，
   於是目標股數隨價格逐日漂移，每天都產生一筆小額買賣——那是每日再平衡。
   實測後果：每個決策月成交 38 筆（上限應為約 20），
   holdout 成本被灌到本金的 18.8%~23.3%。單元測試沒抓到，因為 fixture 價格全程持平，
   目標股數剛好不會漂移。**扁平價格的 fixture 無法偵測周轉率缺陷。**

2. **sizing 用決策日收盤的 NAV 與決策日的 20 日均量，整月固定。**
   否則同一次再平衡會因為當日淨值波動而算出不同股數，變成沒有登記過的自由度。

3. **MOM-1B 濾網每日檢查，但恢復只在月頻決策日。** 規格 §7.3 寫的是
   「低於 200 日均線 → 下一交易日開盤降為 0；重新站回後，於**下一個月頻訊號日**
   恢復選股」。降曝險是每日事件，回補不是。

不含停損、trailing、波動 sizing——§7.6 明文把出場條件列舉完畢，
多加一條就是規格外的自由度。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from research.momentum import (
    eligible_universe, month_end_sessions, pit_common_stock_mask,
)
from research.momentum_corporate_actions import apply_corporate_action
from research.momentum_execution import (
    build_orders_from_target_shares, buy_fill, equal_weight_target_shares,
    pit_industry_map, select_holdings_with_industry_cap,
)

CAPITAL_TWD = 300_000.0            # §7.4
MARKET_FILTER_TICKER = "0050"      # §7.3 MOM-1B
MARKET_FILTER_WINDOW = 200
LIQUIDITY_WINDOW = 20              # §7.1.5


@dataclass
class Position:
    """單一持股的實際股數帳。成本基礎含手續費，現金股利記在 ``corporate_cash``。"""

    shares: int = 0
    cost_basis: float = 0.0        # 累計買入成本（含手續費）
    proceeds: float = 0.0          # 累計賣出淨額（扣手續費與證交稅）
    corporate_cash: float = 0.0    # 持有期間公司行動配發的現金
    entry_date: pd.Timestamp | None = None


@dataclass
class BacktestResult:
    variant: str
    nav: pd.Series
    cash: pd.Series
    position_count: pd.Series
    trades: pd.DataFrame
    monthly: pd.DataFrame
    diagnostics: dict = field(default_factory=dict)


def _market_filter_risk_off(adjusted_close: pd.DataFrame) -> pd.Series:
    """0050 含息收盤低於其 200 日均線 → 該日收盤後為 risk-off（§7.3 MOM-1B）。

    均線用**還原**收盤價，與規格所寫的 "0050 total-return close" 一致；
    用原始價會在每次配息斷點產生假訊號。
    """
    if MARKET_FILTER_TICKER not in adjusted_close.columns:
        raise ValueError(
            f"MOM-1B 需要 {MARKET_FILTER_TICKER} 的還原收盤價，release 中不存在")
    series = adjusted_close[MARKET_FILTER_TICKER]
    moving_average = series.rolling(MARKET_FILTER_WINDOW, min_periods=MARKET_FILTER_WINDOW).mean()
    # 均線尚未成形的期間視為 risk-on：規格沒有授權在此提前空手，
    # 而 2005 起算到 2008 開始模擬時，200 日均線早已成形。
    return (series < moving_average).fillna(False)


def _fillable(
    *, side: str, stock_id: str, session: pd.Timestamp,
    raw_open: pd.DataFrame, locked_limit: pd.DataFrame,
) -> tuple[bool, str]:
    """能不能在這個交易日的開盤成交（§7.5）。回傳 (可成交, 原因)。"""
    if stock_id not in raw_open.columns:
        return False, "no_price_series"
    price = raw_open.at[session, stock_id]
    if pd.isna(price) or not np.isfinite(price) or price <= 0:
        return False, "no_open_price"
    if stock_id in locked_limit.columns:
        lock = locked_limit.at[session, stock_id]
        # 漲停鎖死時買方沒有對手盤；跌停鎖死時賣方沒有對手盤
        if side == "buy" and lock == "up":
            return False, "locked_limit_up"
        if side == "sell" and lock == "down":
            return False, "locked_limit_down"
    return True, "ok"


def _apply_corporate_actions(
    *, session: pd.Timestamp, positions: dict[str, Position], cash: float,
    ledger_by_date: dict, raw_close: pd.DataFrame, sessions: pd.DatetimeIndex,
    audit: list,
) -> float:
    """套用當日除權息事件到持股（§7.5.2 凍結政策 A／B）。"""
    events = ledger_by_date.get(session)
    if not events:
        return cash
    position_index = sessions.get_loc(session)
    for event in events:
        stock_id = str(event["stock_id"])
        position = positions.get(stock_id)
        if position is None or position.shares <= 0:
            continue
        pre_event_close = None
        if position_index > 0:
            previous = raw_close.iloc[position_index - 1].get(stock_id, np.nan)
            if np.isfinite(previous) and previous > 0:
                pre_event_close = float(previous)
        adjustment = apply_corporate_action(
            shares=position.shares, event=event, pre_event_close=pre_event_close)
        position.shares = adjustment.shares
        position.corporate_cash += adjustment.cash_delta
        cash += adjustment.cash_delta
        audit.append({
            "session": session, "stock_id": stock_id,
            "outcome": adjustment.outcome, "reason": adjustment.reason,
        })
    return cash


def simulate(
    inputs,
    *,
    variant: str,
    signal: pd.DataFrame,
    ledger: pd.DataFrame,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    capital: float = CAPITAL_TWD,
) -> BacktestResult:
    """跑一個 MOM-1 變體，回傳日頻 NAV、交易明細與月頻診斷。

    ``variant`` 只接受 ``"MOM-1A"``（純動能）與 ``"MOM-1B"``（加 0050 MA200 濾網）。
    本輪變體總數固定為 2（§7.3），不得新增。
    """
    if variant not in {"MOM-1A", "MOM-1B"}:
        raise ValueError(f"未登記的變體：{variant}；本輪只有 MOM-1A 與 MOM-1B")

    sessions = pd.DatetimeIndex(inputs.trading_days)
    window = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    if len(window) == 0:
        raise ValueError("模擬期間內沒有交易日")
    decisions = set(month_end_sessions(sessions)) & set(window)

    risk_off_series = (_market_filter_risk_off(inputs.adjusted_close)
                       if variant == "MOM-1B"
                       else pd.Series(False, index=sessions))

    ledger_by_date: dict = {}
    for row in ledger.to_dict("records"):
        ledger_by_date.setdefault(pd.Timestamp(row["event_date"]), []).append(row)

    positions: dict[str, Position] = {}
    cash = float(capital)
    target_holdings: list[str] = []
    # 本次再平衡凍結的目標股數；None 代表決策已下但尚未有可定價的成交嘗試日
    frozen_target_shares: dict[str, int] | None = None
    sizing_nav = float(capital)
    average_volumes = pd.Series(dtype=float)

    nav_rows, cash_rows, count_rows = [], [], []
    trades: list[dict] = []
    monthly: list[dict] = []
    corporate_audit: list[dict] = []
    fills = {"buy": 0, "sell": 0}
    blocked = {"locked_limit_up": 0, "locked_limit_down": 0,
               "no_open_price": 0, "no_price_series": 0}
    total_commission = 0.0
    total_tax = 0.0
    total_buy_notional = 0.0
    total_sell_notional = 0.0
    target_without_volume = 0

    for session in window:
        # ── 1. 公司行動（除權息在開盤前生效）─────────────────────────
        cash = _apply_corporate_actions(
            session=session, positions=positions, cash=cash,
            ledger_by_date=ledger_by_date, raw_close=inputs.raw_close,
            sessions=sessions, audit=corporate_audit)

        # ── 2. 以當日開盤執行目標持股（未成交者留待隔日自動重試）──────
        held = {sid: position.shares for sid, position in positions.items()
                if position.shares > 0}
        if target_holdings or held:
            prices = inputs.raw_open.loc[session]
            candidates = sorted(set(target_holdings) | set(held))
            # **只以「當日有沒有開盤價」篩選，不得以「能不能買」篩選目標。**
            # 若把漲停鎖死的目標剔出 target，已持有的那一檔會被誤判成
            # 「不在目標內」而產生賣單——那是規格沒有的賣出理由。
            priced = [sid for sid in candidates
                      if sid in prices.index
                      and np.isfinite(prices.get(sid, np.nan))
                      and prices.get(sid, 0) > 0]
            # 當日沒有開盤價的持股不進委託推導，部位維持不變（不得假成交）
            orders = []
            if priced:
                price_slice = prices.reindex(priced).astype(float)
                if frozen_target_shares is None:
                    # 本次再平衡的第一個可定價交易日：決定目標股數並**凍結**
                    volume_slice = average_volumes.reindex(priced)
                    missing_volume = [sid for sid in target_holdings
                                      if sid in priced and not np.isfinite(
                                          volume_slice.get(sid, np.nan))]
                    target_without_volume += len(missing_volume)
                    frozen_target_shares = {
                        sid: equal_weight_target_shares(
                            executable_price=buy_fill(float(price_slice[sid])),
                            nav=sizing_nav,
                            average_volume_shares=float(
                                volume_slice.get(sid, 0.0)
                                if np.isfinite(volume_slice.get(sid, np.nan)) else 0.0),
                        )
                        for sid in target_holdings if sid in priced
                    }
                # 已凍結的目標股數只在下一次月頻決策時才會被取代
                orders = build_orders_from_target_shares(
                    target_shares={s: n for s, n in frozen_target_shares.items()
                                   if s in priced},
                    current_shares={s: held[s] for s in priced if s in held},
                    raw_open_prices=price_slice)
            for order in orders:
                ok, reason = _fillable(
                    side=order.side, stock_id=order.stock_id, session=session,
                    raw_open=inputs.raw_open, locked_limit=inputs.locked_limit)
                if not ok:
                    blocked[reason] = blocked.get(reason, 0) + 1
                    continue
                if order.side == "buy" and cash + order.cash_delta_twd < 0:
                    continue           # 現金不足即不成交，不得透支
                position = positions.setdefault(order.stock_id, Position())
                cash += order.cash_delta_twd
                total_commission += order.commission_twd
                total_tax += order.transaction_tax_twd
                fills[order.side] += 1
                if order.side == "buy":
                    if position.shares == 0:
                        position.entry_date = session
                    position.shares += order.shares
                    position.cost_basis += order.gross_notional_twd + order.commission_twd
                    total_buy_notional += order.gross_notional_twd
                else:
                    position.shares -= order.shares
                    position.proceeds += (order.gross_notional_twd
                                          - order.commission_twd
                                          - order.transaction_tax_twd)
                    total_sell_notional += order.gross_notional_twd
                    if position.shares == 0:
                        gross = position.proceeds - position.cost_basis
                        trades.append({
                            "stock_id": order.stock_id,
                            "entry_date": position.entry_date,
                            "exit_date": session,
                            "cost_basis": position.cost_basis,
                            "proceeds": position.proceeds,
                            "corporate_cash": position.corporate_cash,
                            "net_pnl": gross + position.corporate_cash,
                        })
                        positions.pop(order.stock_id, None)

        # ── 3. 收盤標記淨值 ──────────────────────────────────────────
        closes = inputs.raw_close.loc[session]
        market_value = 0.0
        for stock_id, position in positions.items():
            price = closes.get(stock_id, np.nan)
            if np.isfinite(price) and price > 0:
                market_value += position.shares * float(price)
        nav = cash + market_value
        nav_rows.append(nav)
        cash_rows.append(cash)
        count_rows.append(sum(1 for p in positions.values() if p.shares > 0))

        # ── 4. 收盤後決定下一個交易日的目標持股 ──────────────────────
        risk_off = bool(risk_off_series.get(session, False))
        eligible = pd.Index([])
        if risk_off:
            # §7.3：下一交易日開盤將目標曝險降為 0。回補要等下一個月頻決策日。
            if target_holdings:
                frozen_target_shares = None
            target_holdings = []
        elif session in decisions:
            pit_mask = pit_common_stock_mask(
                inputs.pit_master, session, inputs.adjusted_close.columns)
            eligible = eligible_universe(
                as_of=session, adjusted_close=inputs.adjusted_close,
                raw_close=inputs.raw_close, turnover=inputs.turnover,
                signal=signal, pit_mask=pit_mask, restricted=inputs.restricted)
            if len(eligible):
                values = signal.loc[session, eligible]
                industries = pit_industry_map(inputs.market_structure, session, eligible)
                previous = [sid for sid, p in positions.items() if p.shares > 0]
                target_holdings = select_holdings_with_industry_cap(
                    values, industries, previous)
            else:
                target_holdings = []
            sizing_nav = nav
            volume_window = inputs.volume_shares.loc[:session].tail(LIQUIDITY_WINDOW)
            average_volumes = volume_window.mean(skipna=False)
            # 新的月頻目標取代舊的，目標股數重新凍結（§7.5）
            frozen_target_shares = None

        if session in decisions:
            monthly.append({
                "decision_date": session,
                "eligible_count": int(len(eligible)),
                "target_count": len(target_holdings),
                "held_count": count_rows[-1],
                "nav": nav,
                "cash": cash,
                "risk_off": risk_off,
            })

    # ── 期末：按最後可交易價格標記，未平倉另列（§7.6.4）──────────────
    final_session = window[-1]
    open_positions = []
    for stock_id, position in positions.items():
        if position.shares <= 0:
            continue
        price = inputs.raw_close.loc[:final_session, stock_id].dropna()
        mark = float(price.iloc[-1]) if len(price) else 0.0
        open_positions.append({
            "stock_id": stock_id, "shares": position.shares,
            "cost_basis": position.cost_basis, "mark_value": position.shares * mark,
            "unrealised_pnl": position.shares * mark - position.cost_basis
                              + position.proceeds + position.corporate_cash,
        })

    nav_series = pd.Series(nav_rows, index=window, dtype=float)
    return BacktestResult(
        variant=variant,
        nav=nav_series,
        cash=pd.Series(cash_rows, index=window, dtype=float),
        position_count=pd.Series(count_rows, index=window, dtype=int),
        trades=pd.DataFrame(trades),
        monthly=pd.DataFrame(monthly),
        diagnostics={
            "capital_twd": capital,
            "sessions": int(len(window)),
            "decision_months": int(len(monthly)),
            "fills": fills,
            "blocked_attempts": blocked,
            "total_commission_twd": total_commission,
            "total_transaction_tax_twd": total_tax,
            "total_buy_notional_twd": total_buy_notional,
            "total_sell_notional_twd": total_sell_notional,
            "target_months_without_average_volume": target_without_volume,
            "corporate_action_events_applied": len(corporate_audit),
            "corporate_action_outcomes": pd.DataFrame(corporate_audit)["outcome"]
            .value_counts().to_dict() if corporate_audit else {},
            "open_positions_at_end": open_positions,
        },
    )


def benchmark_nav(inputs, *, start, end, capital: float = CAPITAL_TWD) -> pd.Series:
    """0050 含息買進持有（§8.1 基準 1）。

    以還原收盤價建構，並在期初收一次買進手續費——買進持有只有一次進場成本，
    忽略它會讓基準被高估約 8bp。
    """
    from agent.strategy import FEE_RATE

    sessions = pd.DatetimeIndex(inputs.trading_days)
    window = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    series = inputs.adjusted_close.loc[window, MARKET_FILTER_TICKER].astype(float)
    if series.isna().any():
        series = series.ffill()
    invested = capital / (1.0 + FEE_RATE)
    return invested * series / float(series.iloc[0])
