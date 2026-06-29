"""
檢查現有持股是否符合出場條件
"""
import sys
from datetime import date
sys.path.insert(0, '.')
from database.connection import get_session
from sqlalchemy import text
from agent.strategy import decide_exit, STRATEGY

today = date.today()

with get_session() as s:
    positions = s.execute(text("""
        SELECT p.stock_id, st.stock_name, p.entry_date, p.entry_price, p.peak_price
        FROM positions p JOIN stocks st ON st.stock_id = p.stock_id
        WHERE p.status = 'open' ORDER BY p.entry_date
    """)).fetchall()

    print(f"持倉中: {len(positions)} 檔，檢查日期: {today}\n")
    print(f"{'股號':<8} {'名稱':<12} {'進場日':<12} {'成本':>8} {'現價':>8} {'漲跌%':>7} {'MA5':>8} {'MA20':>8} {'K':>6} {'D':>6} 出場訊號")
    print("-" * 120)

    for pos in positions:
        sid, name, entry_date, entry_price, peak_price = pos
        entry_price = float(entry_price)
        peak_price = float(peak_price) if peak_price else entry_price

        # 抓最近 40 筆
        rows = s.execute(text("""
            SELECT p.trade_date, p.open, p.high, p.low, p.close, p.volume,
                   t.ma5, t.ma20, t.macd_hist, t.k_value, t.d_value
            FROM daily_prices p
            LEFT JOIN technical_indicators t
                ON t.stock_id = p.stock_id AND t.trade_date = p.trade_date
            WHERE p.stock_id = :sid AND p.trade_date <= :d AND p.close > 0
            ORDER BY p.trade_date DESC LIMIT 40
        """), {"sid": sid, "d": today}).fetchall()

        if not rows:
            print(f"{sid:<8} {str(name):<12} -- 無資料")
            continue

        history = [dict(
            trade_date=r[0], open=float(r[1] or 0), high=float(r[2] or 0),
            low=float(r[3] or 0), close=float(r[4] or 0), volume=float(r[5] or 0),
            ma5=float(r[6]) if r[6] is not None else None,
            ma20=float(r[7]) if r[7] is not None else None,
            macd_hist=float(r[8]) if r[8] is not None else None,
            k_value=float(r[9]) if r[9] is not None else None,
            d_value=float(r[10]) if r[10] is not None else None,
        ) for r in reversed(rows)]

        today_row = history[-1]
        prev_row = history[-2] if len(history) >= 2 else {}
        close = today_row["close"]
        ma5 = today_row["ma5"]
        ma20 = today_row["ma20"]
        peak = max(peak_price, close)

        past = history[-(20+1):-1]
        avg_vol = sum(r["volume"] for r in past) / len(past) if past else None

        extra = dict(
            k=today_row["k_value"], d=today_row["d_value"],
            k_prev=prev_row.get("k_value"), d_prev=prev_row.get("d_value"),
            macd_hist=today_row["macd_hist"], macd_hist_prev=prev_row.get("macd_hist"),
            open=today_row["open"], high=today_row["high"],
            low=today_row["low"], volume=today_row["volume"], avg_volume=avg_vol,
        )

        hold = s.execute(text("""
            SELECT COUNT(DISTINCT trade_date) FROM daily_prices
            WHERE stock_id = :sid AND trade_date > :e AND trade_date <= :d
        """), {"sid": sid, "e": entry_date, "d": today}).scalar() or 0

        should_exit, reason = decide_exit(
            entry_price, peak, close, ma5, ma20, hold,
            cfg=STRATEGY, extra=extra, history=history,
        )

        pct = (close / entry_price - 1) * 100
        sign = "+" if pct >= 0 else ""
        k_str = f"{today_row['k_value']:.1f}" if today_row['k_value'] is not None else "--"
        d_str = f"{today_row['d_value']:.1f}" if today_row['d_value'] is not None else "--"
        ma5_str = f"{ma5:.1f}" if ma5 else "--"
        ma20_str = f"{ma20:.1f}" if ma20 else "--"

        signal = f"⚠ {reason}" if should_exit else "續抱"
        print(f"{sid:<8} {str(name):<12} {str(entry_date):<12} {entry_price:>8.2f} {close:>8.2f} {sign}{pct:>6.1f}% {ma5_str:>8} {ma20_str:>8} {k_str:>6} {d_str:>6} {signal}")
