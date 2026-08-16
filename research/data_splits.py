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


# ══════════════════════════════════════════════════════════════════
# 2026-08-16 使用者決策：動能族改用新切分（🔶 不可逆）
# ══════════════════════════════════════════════════════════════════
#
# 上面那組（2015/2021/2023）**仍然是波段策略族的有效切分，不刪**。
# 新增的是「同一條時間軸，不同假說族有不同污染狀態」這個概念。
#
# 為什麼要分族
# ------------
# 污染不是時間的屬性，是 **(資料, 假說)** 這一對的屬性。在 P 上調 A 的
# 參數，污染的是 (P, A)，不是 P 本身。舊設計把 `CONTAMINATED` 寫成全域
# 常數，會讓人誤以為 2025-06 之後對**所有**假說都不可用——但那個窗是
# 現行波段策略的調參目標窗，動能族從來沒有在那裡調過任何東西。
#
# 動能族的新切分，以及為什麼是這幾條線
# ------------------------------------
# 等權市場年報酬（實測）：
#   2008 -47.3%  2009 +123.5%  2010 +8.3%   2011 -30.1%  2012 +11.0%
#   2013 +21.2%  2014 +4.2%    2015 -12.3%  2016 +7.7%   2017 +18.6%
#   2018 -9.8%   2019 +18.1%   2020 +22.2%  2021 +27.3%  2022 -10.6%
#   2023 +29.7%  2024 +11.0%   2025 -0.9%   2026 +6.8%(至七月)
#
# 舊的「封存最近 2~3 年」有一個結構性缺陷：**極端 régime 全在過去**，
# 封存區只能拿到「最近剛好是什麼樣子」。2023~2025 那段沒有任何空頭年，
# 一個在崩盤時會爆的策略可以輕鬆通過。
#
# 分界拉到 2022-01 之後，封存區同時含空頭（2022，0050 -24.7%）、
# 強多頭（2023 +29.7%）與溫和／持平（2024~2026），**régime 數由 1 變 3**。
# 這是這次改動唯一的實質理由——不是為了調整比例。
#
# 2008~2014 為什麼放在探索區
# --------------------------
# 它對動能族**已經燒掉**（MOM-1 F1 開封兩次，見 SPLIT_ACCESS_LOG）。
# 既然已經燒了，納入探索區不新增任何成本，卻換到 2015 年後完全沒有的
# 金融海嘯 régime。
#
# 一個必須事前寫死的功效事實
# --------------------------
# MOM-1B 實測年化超額 +3.55%、追蹤誤差 25.89%。要讓它達到 t=2 需要
# **約 213 年**。因此**任何**長度的封存區都不足以支撐組合層級的顯著性
# 檢定——封存區只有搭配橫斷面估計式才有意義（每月約 1,800 檔而不是
# 一條 NAV）。切分比例救不了功效，估計式才可以。
MOMENTUM_EXPLORATION = (date(2008, 1, 1), date(2021, 12, 31))   # 14 年，9 個 régime
MOMENTUM_SEALED = (date(2022, 1, 1), date(2026, 8, 13))         # 4.6 年，3 個 régime
FORWARD_ONLY = (date(2026, 8, 14), date(2099, 12, 31))          # forward journal 的地盤

#: 假說族 → 切分表。**邊界可以共用，污染狀態不可以。**
FAMILY_SPLITS: dict[str, dict[str, tuple]] = {
    "momentum": {
        "exploration": MOMENTUM_EXPLORATION,
        "sealed": MOMENTUM_SEALED,
        "forward_only": FORWARD_ONLY,
    },
    "swing": {
        "development": DEVELOPMENT,
        "validation": VALIDATION,
        "holdout": HOLDOUT,
        "contaminated": CONTAMINATED,
    },
}

#: 每個族的 sealed 段被碰幾次就作廢；forward_only 一律不得回測。
_FAMILY_TRACKED = {"momentum": ("sealed",), "swing": ("validation", "holdout")}

#: 動能族封存區缺什麼——任何引用其結論的地方都必須附上這句。
MOMENTUM_SEALED_CAVEAT = (
    "⚠️ 動能族封存區（2022-01~2026-08-13）含空頭、強多頭與溫和三種 régime，"
    "但**不含 2008／2011 等級的崩盤**（等權 -47%／-30%）。"
    "因此結論只能宣稱『在該期間的市況下未被否證』，"
    "**對危機 régime 的行為未經檢驗**。"
)

#: 動能族在此期間與波段策略的調參窗重疊，引用時必須揭露。
MOMENTUM_OVERLAP_DISCLOSURE = (
    "2025-06~2026-07 是現行波段策略的調參目標窗。動能族未在該窗調過任何"
    "參數，但波段策略的 `w_trend_stack`（多頭排列，權重 0.8／總權重 11.8）"
    "是動能相鄰因子，其門檻確實在該窗被調過。這是揭露事項，不是失格。"
)


def family_split_of(d: date, family: str) -> str:
    """某一天對某個假說族屬於哪一段。**同一天對不同族可以是不同答案。**"""
    if family not in FAMILY_SPLITS:
        raise ValueError(f"未登記的假說族 {family!r}，可用：{sorted(FAMILY_SPLITS)}")
    for name, (lo, hi) in FAMILY_SPLITS[family].items():
        if lo <= d <= hi:
            return name
    return "unknown"


def family_slice_dates(dates, family: str, split: str,
                       purpose: str = "(未註明)") -> list:
    """取出某族某段的日期並記帳。**取得 sealed 區間的唯一入口。**

    `forward_only` 一律拒絕——那段是 forward journal 的地盤，
    一旦被回測吃掉就不再是 forward。
    """
    if family not in FAMILY_SPLITS:
        raise ValueError(f"未登記的假說族 {family!r}，可用：{sorted(FAMILY_SPLITS)}")
    table = FAMILY_SPLITS[family]
    if split not in table:
        raise ValueError(f"{family} 沒有 {split!r} 這一段，可用：{sorted(table)}")
    if split == "forward_only":
        raise ValueError(
            "forward_only 不得用於回測。那段是 forward journal 正在累積的資料，"
            "被回測吃掉就不再是乾淨的前瞻證據。")
    if split in _FAMILY_TRACKED.get(family, ()):
        touch(f"{family}:{split}", purpose)
    lo, hi = table[split]
    return [d for d in dates if lo <= (d.date() if hasattr(d, "date") else d) <= hi]


def family_access_count(family: str, split: str) -> int:
    """該族該段被碰過幾次。sealed 只要 >0 就不再是乾淨集合。"""
    return access_count(f"{family}:{split}")


def split_of(d: date) -> str:
    """某一天屬於哪一段。contaminated 先判，因為它與其他段不重疊但開區間到未來。"""
    for name in ("contaminated", "holdout", "validation", "development"):
        lo, hi = SPLITS[name]
        if lo <= d <= hi:
            return name
    return "unknown"


def touch(split: str, purpose: str) -> None:
    """把一次存取寫進稽核日誌。dev/contaminated 不記（規格書只限制另外兩段）。

    族別切分傳進來的是 ``"<family>:<split>"``（例如 ``"momentum:sealed"``），
    它一律記帳——由 `family_slice_dates` 判斷該不該呼叫。
    """
    if ":" not in split and split not in _TRACKED:
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
