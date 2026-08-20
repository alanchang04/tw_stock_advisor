"""補算腳本的測試：重點在「不得碰手動持倉」與「必須標記為推算」。"""
from __future__ import annotations

import re
from pathlib import Path

from agent.strategy import STRATEGY, entry_share_count

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill_ai_position_shares.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")
#: 去掉模組 docstring，只留程式碼——docstring 裡刻意解釋了為何不用舊函式，
#: 若連它一起比對就會誤判。
CODE = SOURCE.split('"""', 2)[-1]


def test_script_defaults_to_dry_run():
    assert SCRIPT.is_file()
    assert '"--commit"' in CODE, "必須要有明確的 --commit 才寫入"
    assert not re.search(r'--commit".*default=True', CODE)


def test_only_touches_ai_paper_positions():
    """使用者的手動持倉是真金白銀、沒用本系統的規則，補算會製造假紀錄。"""
    assert "COALESCE(source, 'ai') = 'ai'" in CODE
    assert "shares IS NULL" in CODE


def test_always_marks_reconstructed():
    assert "shares_source = 'reconstructed'" in CODE


def test_update_is_idempotent():
    """重跑不得覆蓋已補過的列。"""
    update = CODE[CODE.index("UPDATE positions"):]
    assert "WHERE id = :i AND shares IS NULL" in update


def test_uses_the_live_sizing_function_not_a_reimplementation():
    """必須用 live 下單同一個函式，否則帳面與實際會用兩套算法。"""
    assert "entry_share_count(" in CODE
    assert "suggest_shares(" not in CODE, \
        "舊函式的 20% 上限綁不住 12.5%，10 檔會算成 122%"


def test_output_encodes_under_cp950():
    """實測過：emoji 會讓 cp950 終端機直接 UnicodeEncodeError，腳本跑到最後才炸。

    不用字元範圍猜，直接用 cp950 編碼——那才是實際會發生的事。
    """
    for line in CODE.splitlines():
        if line.strip().startswith("print("):
            try:
                line.encode("cp950")
            except UnicodeEncodeError as exc:
                raise AssertionError(
                    f"print 內含 cp950 編不出的字元：{line.strip()[:70]} ({exc})")


def test_slot_cap_makes_ten_positions_fit_within_capital():
    """1%/8% = 每檔 12.5%，10 檔 125% 會超出資金；槽位上限必須把它壓到 10%。"""
    capital = float(STRATEGY["capital"])
    max_open = int(STRATEGY["max_open_positions"])
    shares = entry_share_count(100.0, cash=capital, nav=capital, max_open=max_open)
    weight = shares * 100.0 / capital
    assert weight <= 1.0 / max_open + 0.005, f"單檔權重 {weight:.1%} 應被槽位綁住"


def test_cash_constraint_shrinks_later_entries():
    """現金遞減時，後面的部位不得還是拿到滿額槽位。"""
    capital = float(STRATEGY["capital"])
    max_open = int(STRATEGY["max_open_positions"])
    full = entry_share_count(100.0, cash=capital, nav=capital, max_open=max_open)
    starved = entry_share_count(100.0, cash=1_000.0, nav=capital, max_open=max_open)
    assert starved < full
    assert entry_share_count(100.0, cash=0.0, nav=capital, max_open=max_open) == 0


def test_migration_constrains_allowed_values():
    mig = (ROOT / "database" / "migrations" / "30_shares_source.sql").read_text(
        encoding="utf-8")
    assert "shares_source" in mig
    assert "'recorded'" in mig and "'reconstructed'" in mig
    assert "CHECK" in mig, "沒有 CHECK 的話任何字串都寫得進去"
