"""替 AI 紙上部位補算股數與成本，並標記為「推算」而非「成交」。

為什麼要做
----------
`positions.shares` 為 NULL 時，app.py 的損益$欄、首頁與歷史績效兩個指標
都只能顯示空白——因為系統刻意拒絕用目前參數倒推舊股數。那個拒絕是對的：
推算值與成交值長得一樣時沒人分得出來。

2026-08-18 使用者決定補算，但同時要求標記。因此本腳本：
  1. 只處理 `source='ai'` 的紙上追蹤部位，**完全不碰使用者自有的手動持倉**
     （那些是真金白銀、未使用本系統的部位大小規則，補算會製造假紀錄）。
  2. 用 `agent.strategy.entry_share_count`，也就是 live 下單用的同一個函式，
     不另外寫一套算法。
  3. 依進場日順序遞減現金——`entry_share_count` 的 `slot_budget` 取
     `min(cash, nav/max_open)`，所以順序會影響結果，必須照實際進場順序重放。
  4. 一律寫入 `shares_source='reconstructed'`。

為什麼是 nav/max_open 而不是 capital/pick_top_n
-----------------------------------------------
1% 風險 ÷ 8% 停損 = 每檔 12.5% 資金，10 檔就是 125%，超出資金。
`entry_share_count` 的槽位上限 `nav/max_open` = 10% 會綁住它，10 檔約 94%
（差的 6% 是整股進位——5274 一股 16,675 元，30,000 槽位只夠 1.79 股）。
舊的 `suggest_shares` 用 `capital/pick_top_n` = 20%，綁不住 12.5%，才會算成 122%。

安全性質
--------
- **預設 dry-run**，要加 `--commit` 才寫入。
- 只更新 `shares IS NULL` 的列，重跑不會覆蓋已補過的。
- 單一交易，任何一列失敗就整批回滾。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.paper_account import ensure_paper_account  # noqa: E402
from agent.strategy import FEE_RATE, STRATEGY, entry_share_count  # noqa: E402
from database.connection import get_session  # noqa: E402

SELECT_TARGETS = text("""
    SELECT id, stock_id, entry_date, entry_price
    FROM positions
    WHERE status = 'open' AND COALESCE(source, 'ai') = 'ai' AND shares IS NULL
    ORDER BY entry_date, stock_id
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="真的寫入。不加就只印預覽，不碰資料庫內容。")
    parser.add_argument("--capital", type=float,
                        default=float(STRATEGY["capital"]))
    args = parser.parse_args()

    capital = args.capital
    max_open = int(STRATEGY["max_open_positions"])

    with get_session() as session:
        session.execute(text(
            "ALTER TABLE positions ADD COLUMN IF NOT EXISTS shares_source VARCHAR(20)"))

        account_id = None
        if args.commit:
            ensure_paper_account()
            row = session.execute(text(
                "SELECT id FROM paper_accounts ORDER BY id LIMIT 1")).fetchone()
            account_id = int(row[0]) if row else None

        targets = session.execute(SELECT_TARGETS).fetchall()
        if not targets:
            print("沒有需要補算的部位（source='ai' 且 shares IS NULL）。")
            return

        cash = capital
        total = 0.0
        print(f"資金 {capital:,.0f}　槽位上限 nav/max_open = {1 / max_open:.0%}"
              f"　風險 {STRATEGY['risk_per_trade']:.0%} / 停損 {STRATEGY['stop_loss']:.0%}")
        print(f"{'股號':>6} {'進場日':>12} {'進場價':>11} {'推算股數':>9} "
              f"{'成本':>10} {'佔資金':>8} {'剩餘現金':>11}")
        print("-" * 74)

        for pid, stock_id, entry_date, entry_price in targets:
            price = float(entry_price)
            shares = entry_share_count(price, cash=cash, nav=capital,
                                       max_open=max_open)
            cost = shares * price * (1 + FEE_RATE)
            cash -= cost
            total += cost
            print(f"{stock_id:>6} {str(entry_date):>12} {price:>11,.2f} "
                  f"{shares:>9} {cost:>10,.0f} {cost / capital:>7.1%} {cash:>11,.0f}")

            if args.commit and shares > 0:
                session.execute(text("""
                    UPDATE positions
                       SET shares = :s, entry_cost = :c,
                           shares_source = 'reconstructed',
                           paper_account_id = COALESCE(paper_account_id, :acct)
                     WHERE id = :i AND shares IS NULL
                """), {"s": int(shares), "c": round(cost, 2),
                       "acct": account_id, "i": pid})

        print("-" * 74)
        print(f"{'合計':>6} {'':>12} {'':>11} {'':>9} {total:>10,.0f} "
              f"{total / capital:>7.1%} {cash:>11,.0f}")
        print()
        if args.commit:
            print(f"[OK] 已寫入 {len(targets)} 列，shares_source='reconstructed'"
                  f"，paper_account_id={account_id}")
        else:
            print("[dry-run] ：未寫入任何資料。確認無誤後加 --commit。")


if __name__ == "__main__":
    main()
