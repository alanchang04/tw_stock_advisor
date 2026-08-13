"""驗證 `docs/RESEARCH_METHOD_AMENDMENTS.md` 的每一條都真的被執行了。

**這個測試檔存在的理由是一次具體的失敗。**
`docs/SPEC_H01_BOLLINGER_REBREAKOUT.md` 寫得很完整，實作卻測錯了事件
（測 B2 而非 B1）。規格與程式之間沒有任何東西在把關，於是規格說一套、
程式做一套，直到使用者拿標註圖來才被發現。

寫規範不會讓規範被遵守，**只有會變紅的測試會**。因此這裡把登記表當成
可執行的契約來驗：

- 判定 `已採納` → 實作函式必須可匯入，測試類別必須存在
- 判定 `已採納（規範）` → 該編號必須被上位規範文件引用
- 判定 `不採納` → 本文件必須寫出理由

刪掉實作卻留著條目、或加了條目卻沒做，這個檔就會紅。
"""
from __future__ import annotations

import importlib
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
AMENDMENTS = ROOT / "docs/RESEARCH_METHOD_AMENDMENTS.md"
# 「規範」型修正案必須被至少一份上位規範文件引用，否則等於沒有落地
NORMATIVE_DOCS = (
    ROOT / "docs/RESEARCH_V2_PIPELINE.md",
    ROOT / "docs/STRATEGY_VALIDATION_PROTOCOL.md",
)

ROW = re.compile(r"^\|\s*(M\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|\s*$")
SYMBOL = re.compile(r"`([\w.]+):(\w+)`")
TEST_TARGET = re.compile(r"`(tests/[\w./]+\.py)::(\w+)`")


def cells(line: str):
    match = ROW.match(line.strip())
    if not match:
        return None
    identifier, claim, verdict, implementation, test = match.groups()
    return {
        "id": identifier,
        "claim": claim.strip(),
        "verdict": verdict.strip(),
        "implementation": implementation.strip(),
        "test": test.strip(),
    }


def load_amendments() -> list[dict]:
    rows = [cells(line) for line in AMENDMENTS.read_text(encoding="utf-8").splitlines()]
    return [row for row in rows if row]


AMENDMENT_ROWS = load_amendments()
AMENDMENT_TEXT = AMENDMENTS.read_text(encoding="utf-8")


def test_the_registry_table_is_not_empty():
    """解析失敗會讓所有參數化測試靜默消失——先擋住這種假通過。"""
    assert len(AMENDMENT_ROWS) >= 10


def test_amendment_ids_are_unique_and_contiguous():
    """編號跳號通常代表有人刪了一條卻沒說明。"""
    numbers = sorted(int(row["id"][1:]) for row in AMENDMENT_ROWS)
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(set(numbers)) == len(numbers)


def test_every_verdict_is_one_of_the_three_allowed_values():
    allowed = {"已採納", "已採納（規範）", "不採納"}
    unexpected = {row["id"]: row["verdict"] for row in AMENDMENT_ROWS
                  if row["verdict"] not in allowed}
    assert not unexpected, f"未知的判定值：{unexpected}"


@pytest.mark.parametrize("row", AMENDMENT_ROWS, ids=lambda r: r["id"])
def test_every_amendment_has_its_own_section(row):
    """登記表列了一條，本文就必須有對應的段落說明。"""
    assert re.search(rf"^## {row['id']} — ", AMENDMENT_TEXT, re.MULTILINE), (
        f"{row['id']} 在登記表中，但本文沒有 '## {row['id']} — ' 段落")


@pytest.mark.parametrize(
    "row", [r for r in AMENDMENT_ROWS if r["verdict"] == "已採納"],
    ids=lambda r: r["id"])
def test_adopted_amendments_have_importable_implementations(row):
    """`已採納` 就必須指得出實作，而且那個實作真的存在。"""
    symbols = SYMBOL.findall(row["implementation"])
    assert symbols, f"{row['id']} 判定為已採納，但沒有指出實作符號"
    for module_name, attribute in symbols:
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), (
            f"{row['id']} 指向 {module_name}:{attribute}，但該符號不存在")


@pytest.mark.parametrize(
    "row", [r for r in AMENDMENT_ROWS if r["verdict"] == "已採納"],
    ids=lambda r: r["id"])
def test_adopted_amendments_have_real_test_classes(row):
    """光有實作不夠——沒有測試的實作下次一樣會被悄悄改掉。"""
    targets = TEST_TARGET.findall(row["test"])
    assert targets, f"{row['id']} 判定為已採納，但沒有指出測試類別"
    for relative_path, class_name in targets:
        path = ROOT / relative_path
        assert path.exists(), f"{row['id']} 指向不存在的測試檔 {relative_path}"
        source = path.read_text(encoding="utf-8")
        assert re.search(rf"^class {class_name}\b", source, re.MULTILINE), (
            f"{row['id']} 指向 {relative_path}::{class_name}，但該類別不存在")


@pytest.mark.parametrize(
    "row", [r for r in AMENDMENT_ROWS if r["verdict"] == "已採納（規範）"],
    ids=lambda r: r["id"])
def test_normative_amendments_are_cited_by_an_upstream_spec(row):
    """規範型修正案若沒被上位規範引用，就只是這份文件裡的一段散文。"""
    cited = any(row["id"] in path.read_text(encoding="utf-8")
                for path in NORMATIVE_DOCS if path.exists())
    assert cited, (
        f"{row['id']} 判定為規範，但 {[p.name for p in NORMATIVE_DOCS]} "
        "都沒有引用它")


@pytest.mark.parametrize(
    "row", [r for r in AMENDMENT_ROWS if r["verdict"] == "不採納"],
    ids=lambda r: r["id"])
def test_rejected_amendments_state_a_reason(row):
    """拒絕一條批評時要寫理由，否則下次會有人重新提出同一件事。"""
    section = re.search(rf"^## {row['id']} — .*?(?=^## |\Z)",
                        AMENDMENT_TEXT, re.MULTILINE | re.DOTALL)
    assert section and "**理由：" in section.group(0), (
        f"{row['id']} 判定為不採納，但段落中沒有 '**理由：' ")
