"""修復舊倉補算後未同步扣除的紙上帳戶現金。

預設只做 dry-run。加 ``--commit`` 才會鎖定 swing 帳戶、寫入一筆可稽核的
adjustment 流水並同步 cash/last_nav。已記錄的 debit 由固定 marker 辨識，
所以可安全重跑，也能在未來新增 reconstructed 部位後只補差額。
"""
from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import text

from agent.paper_account import (RECONSTRUCTED_CASH_MARKER, SWING_ACCOUNT_KEY,
                                 marked_nav)
from config.settings import tw_today
from database.connection import get_session


CENT = Decimal("0.01")


def missing_cash_debit(total_reconstructed_cost: Decimal,
                       previously_recorded_debit: Decimal) -> Decimal:
    """Return the unrecorded reconstructed cost; reject an over-debited ledger."""
    missing = total_reconstructed_cost - previously_recorded_debit
    if missing < -CENT:
        raise ValueError(
            f"已記錄扣款 {previously_recorded_debit} 大於 reconstructed 成本 "
            f"{total_reconstructed_cost}")
    return max(Decimal("0.00"), missing).quantize(CENT)


def load_plan(session, *, lock: bool) -> dict:
    lock_clause = " FOR UPDATE" if lock else ""
    account = session.execute(text(f"""
        SELECT id, initial_capital, cash
        FROM paper_accounts WHERE strategy_key=:key{lock_clause}
    """), {"key": SWING_ACCOUNT_KEY}).fetchone()
    if account is None:
        raise RuntimeError("找不到 swing paper account")

    account_id = int(account[0])
    rows = session.execute(text("""
        SELECT id, stock_id, entry_cost
        FROM positions
        WHERE paper_account_id=:aid
          AND shares_source='reconstructed'
          AND entry_cost IS NOT NULL
        ORDER BY id
    """), {"aid": account_id}).fetchall()
    total_cost = sum((Decimal(str(row[2])) for row in rows), Decimal("0.00"))
    recorded = session.execute(text("""
        SELECT COALESCE(-SUM(amount), 0)
        FROM paper_account_transactions
        WHERE account_id=:aid AND kind='adjustment' AND amount < 0
          AND note LIKE :marker
    """), {"aid": account_id, "marker": f"{RECONSTRUCTED_CASH_MARKER}%"}).scalar()
    recorded_debit = Decimal(str(recorded or 0))
    missing = missing_cash_debit(total_cost, recorded_debit)
    current_cash = Decimal(str(account[2]))
    corrected_cash = current_cash - missing
    if corrected_cash < -CENT:
        raise RuntimeError(
            f"缺漏扣款 {missing:,.2f} 大於目前現金 {current_cash:,.2f}；拒絕修復")

    return {
        "account_id": account_id,
        "initial_capital": Decimal(str(account[1])),
        "current_cash": current_cash,
        "position_ids": [int(row[0]) for row in rows],
        "stock_ids": [str(row[1]) for row in rows],
        "total_reconstructed_cost": total_cost.quantize(CENT),
        "previously_recorded_debit": recorded_debit.quantize(CENT),
        "missing_debit": missing,
        "corrected_cash": max(Decimal("0.00"), corrected_cash).quantize(CENT),
    }


def print_plan(plan: dict) -> None:
    print(f"account_id: {plan['account_id']}")
    print(f"initial capital: {plan['initial_capital']:,.2f}")
    print(f"current cash: {plan['current_cash']:,.2f}")
    print(f"reconstructed cost: {plan['total_reconstructed_cost']:,.2f}")
    print(f"already recorded debit: {plan['previously_recorded_debit']:,.2f}")
    print(f"missing debit: {plan['missing_debit']:,.2f}")
    print(f"corrected cash: {plan['corrected_cash']:,.2f}")
    print("positions: " + ",".join(str(pid) for pid in plan["position_ids"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="寫入 adjustment 並更新帳戶；預設只預覽")
    args = parser.parse_args()

    with get_session() as session:
        plan = load_plan(session, lock=args.commit)
        print_plan(plan)
        if not args.commit:
            print("[dry-run] no database changes")
            return
        if plan["missing_debit"] == 0:
            print("[OK] 帳本已平衡，無需修改")
            return

        note = (f"{RECONSTRUCTED_CASH_MARKER}; reconcile "
                f"positions={','.join(str(pid) for pid in plan['position_ids'])}")
        session.execute(text("""
            INSERT INTO paper_account_transactions
                (account_id, position_id, trade_date, kind, amount, cash_after, note)
            VALUES (:aid, NULL, :d, 'adjustment', :amount, :cash, :note)
        """), {
            "aid": plan["account_id"], "d": tw_today(),
            "amount": -plan["missing_debit"], "cash": plan["corrected_cash"],
            "note": note,
        })
        nav = Decimal(str(marked_nav(
            session, float(plan["corrected_cash"]), plan["account_id"]
        ))).quantize(CENT)
        session.execute(text("""
            UPDATE paper_accounts
            SET cash=:cash, last_nav=:nav, updated_at=NOW()
            WHERE id=:aid
        """), {"cash": plan["corrected_cash"], "nav": nav,
                 "aid": plan["account_id"]})
        print(f"[OK] cash={plan['corrected_cash']:,.2f}; marked NAV={nav:,.2f}")


if __name__ == "__main__":
    main()
