"""
SPEC_QUANT_UPGRADE §4.1 資料三分——**切了之後不可再動**。

規格書原話：
    development set   最舊 ~6 年     隨便玩,所有探索在這裡
    validation set    中間 ~2 年     每月最多碰一次,用來確認 dev 上的發現
    final holdout     最新 ~2 年     整個研發週期只碰一次,上線前的最終判決

2026-07-24 使用者決策（🔶不可逆，之後不可再動）：採「務實分級切」。
理由：§4.1 自己規定「已被反覆使用的資料視為已污染，歸入 development」，而按同一標準
10 年資料也被 §3.4 因子篩選與 §5.1~5.5 五輪 A/B 間接碰過。承認污染程度有差別：
  - 2025-06~2026-07 是**硬污染**（現行策略 07-09 趨勢版 / 07-15 中型股版的調參目標窗）
  - 其餘 10 年是**間接污染**（被拿來挑過贏家，但不是逐點調參的目標）
→ 硬污染區歸 dev；其餘照 6/2/2.4 年切。**所有 holdout 結論一律標註打折看待。**

§4 的精神是「制度、不靠自律」，所以本模組把紀律做成機制：
  - 想取得 validation / holdout 的日期區間，**只能**經由 `slice_dates()`
  - `slice_dates()` 會自動把每次存取寫進 `research/SPLIT_ACCESS_LOG.md`
  - 於是「validation 碰過幾次、holdout 碰過幾次」是可稽核的事實，不是印象
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

# ── 切分定義（🔒 不可再動）────────────────────────────────────────
DEVELOPMENT = (date(2015, 1, 1), date(2020, 12, 31))    # 6.0 年，隨便玩
VALIDATION = (date(2021, 1, 1), date(2022, 12, 31))     # 2.0 年，每月最多一次
HOLDOUT = (date(2023, 1, 1), date(2025, 5, 31))         # 2.4 年，整個研發週期一次
CONTAMINATED = (date(2025, 6, 1), date(2099, 12, 31))   # 硬污染，歸 dev，不可用於驗收

SPLITS = {
    "development": DEVELOPMENT,
    "validation": VALIDATION,
    "holdout": HOLDOUT,
    "contaminated": CONTAMINATED,
}

#: 任何用到 holdout 的報告都必須把這句話印出來（見 §4.1 使用者決策）
HOLDOUT_CAVEAT = (
    "⚠️ 本 holdout（2023-01~2025-05）已被 §3.4 因子篩選與 §5.1~5.5 五輪 A/B "
    "間接污染——那些實驗都在完整 10 年上挑過贏家。結論須打折看待，"
    "真正乾淨的驗收只有 2026-07-24 起的前向紙上交易。"
)

_LOG = Path(__file__).with_name("SPLIT_ACCESS_LOG.md")
#: 這兩段每碰一次就少一分可信度，所以要記帳；dev 隨便玩，不記。
_TRACKED = ("validation", "holdout")


def split_of(d: date) -> str:
    """某一天屬於哪一段。contaminated 先判，因為它與其他段不重疊但開區間到未來。"""
    for name in ("contaminated", "holdout", "validation", "development"):
        lo, hi = SPLITS[name]
        if lo <= d <= hi:
            return name
    return "unknown"


def touch(split: str, purpose: str) -> None:
    """把一次存取寫進稽核日誌。dev/contaminated 不記（規格書只限制另外兩段）。"""
    if split not in _TRACKED:
        return
    if not _LOG.exists():
        _LOG.write_text(
            "# 資料切分存取紀錄（SPEC_QUANT_UPGRADE §4.1）\n\n"
            "validation 每月最多碰一次、holdout 整個研發週期只碰一次。\n"
            "本檔由 `research/data_splits.py::slice_dates()` 自動追加，**請勿手動編輯**。\n\n"
            "| 時間 | 切分 | 用途 |\n|---|---|---|\n",
            encoding="utf-8",
        )
    with _LOG.open("a", encoding="utf-8") as f:
        f.write(f"| {datetime.now():%Y-%m-%d %H:%M} | {split} | {purpose} |\n")


def access_count(split: str) -> int:
    """這一段被碰過幾次——決定結論該打幾折的依據。"""
    if not _LOG.exists():
        return 0
    return sum(1 for ln in _LOG.read_text(encoding="utf-8").splitlines()
               if ln.startswith("|") and f"| {split} |" in ln)


def slice_dates(dates, split: str, purpose: str = "(未註明)") -> list:
    """
    取出屬於某一段的日期，**並自動記帳**。

    這是取得 validation / holdout 區間的唯一入口——不要繞過它直接用上面的常數，
    繞過就等於把 §4 想建立的制度變回自律。
    """
    if split not in SPLITS:
        raise ValueError(f"未知的切分 {split!r}，可用：{sorted(SPLITS)}")
    lo, hi = SPLITS[split]
    touch(split, purpose)
    return [d for d in dates if lo <= (d.date() if hasattr(d, "date") else d) <= hi]
