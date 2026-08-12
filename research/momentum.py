"""MOM-1 訊號建構引擎（signal-only）。

規格來源：`docs/SPEC_DATA_FOUNDATION_AND_MOMENTUM.md`
§7.1（共同 universe）、§7.2（形成訊號與 10%／20% buffer）、§7.4（sizing 上限）、
§7.5（T+1 成交）。

範圍界線：本模組只建構「訊號、universe 資格、排名與成交日對齊」，
**不計算任何報酬、NAV、Sharpe、回撤或勝率**，也不讀取任何固定的資料路徑。
MOM1-0 的交付是 F0 正確性閘門，不是績效；把績效函式放進本檔案會讓
「開封 backward holdout」變成一次 import 就能發生的意外。

不得靜默退化的三個地方（AGENTS.md「不得發明代理指標」）：

1. 普通股資格一律來自 point-in-time security master 的 ``asset_type``，
   缺欄位就 raise，不回頭用代號前綴猜 ETF。
2. 掛牌區間一律來自 ``effective_from`` / ``delisting_date``，
   缺 ``effective_from`` 就 raise。
3. §7.1.6 的不可交易排除（處置／停止交易／全額交割）若沒有資料，
   呼叫端必須顯式傳入 ``allow_missing_restrictions=True`` 才能繼續，
   否則 raise。這是為了讓「這份資料還不存在」無法被沉默略過。
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

# ── SPEC §7.1 universe 門檻 ───────────────────────────────────────────
MIN_PRICE = 10.0                # §7.1.4 收盤價至少新台幣 10 元
MIN_HISTORY_SESSIONS = 252      # §7.1.3 至少 252 個有效交易日歷史
LIQUIDITY_WINDOW = 20           # §7.1.5 過去 20 日平均成交金額
LIQUIDITY_TOP_FRAC = 0.50       # §7.1.5 位於當日前 50%

# ── SPEC §7.2 形成訊號與 buffer ──────────────────────────────────────
FORMATION_SKIP = 20             # 跳過最近一個月
FORMATION_LOOKBACK = 120        # 形成窗起點
ENTRY_TOP_FRAC = 0.10           # 新進場只取前 10%
EXIT_BOTTOM_FRAC = 0.20         # 跌出前 20% 才賣

MASTER_COLUMNS = ("stock_id", "market", "asset_type", "effective_from", "delisting_date")


# ══════════════════════════════════════════════════════════════════
#  時間軸：月末決策日與 T+1 成交日
# ══════════════════════════════════════════════════════════════════
def _trading_index(trading_days: Iterable) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Index(list(trading_days))))
    return pd.DatetimeIndex(sorted(idx.unique()))


def month_end_sessions(trading_days: Iterable) -> pd.DatetimeIndex:
    """每個月最後一個「實際交易日」（SPEC §7.2「每月最後一個交易日收盤後計算」）。

    以交易日曆分組取 max，不使用日曆月底，因此月底連假、颱風停市或月底停牌
    都不會產生一個沒有行情的決策日。沒有任何交易日的月份不會出現在結果中。
    """
    idx = _trading_index(trading_days)
    if idx.empty:
        return idx
    s = pd.Series(idx, index=idx)
    return pd.DatetimeIndex(sorted(s.groupby([idx.year, idx.month]).max().to_numpy()))


def next_session(trading_days: Iterable, date) -> pd.Timestamp | None:
    """SPEC §7.5：訂單最早在 T+1 開盤成交。回傳嚴格晚於 ``date`` 的下一個交易日。

    ``date`` 本身不必是交易日（例如以日曆日詢問），一律回傳其後第一個交易日。
    樣本尾端沒有下一個交易日時回傳 ``None``——呼叫端必須把 ``None`` 當成
    「本樣本內無法成交」，依 §7.5 延後或取消，**不得假成交**。
    """
    idx = _trading_index(trading_days)
    pos = idx.searchsorted(pd.Timestamp(date), side="right")
    return None if pos >= len(idx) else idx[pos]


# ══════════════════════════════════════════════════════════════════
#  形成訊號
# ══════════════════════════════════════════════════════════════════
def compute_mom_6_1(adjusted_close: pd.DataFrame) -> pd.DataFrame:
    """SPEC §7.2：``mom_6_1(t) = adjusted_close[t-20] / adjusted_close[t-120] - 1``。

    位移單位是「交易日列」，不是日曆日，因此輸入必須是以交易日為 index、
    已按日期升冪排序的 wide frame（columns = stock_id）。

    輸入須為還原收盤價；還原正確性屬 D3 公司行動的責任，本函式不驗證，
    但它只讀 ``t-20`` 與 ``t-120``，永遠不碰 ``t`` 之後的列，因此不引入
    look-ahead。成交價另用未還原原始價，見 SPEC §7.5。
    """
    if not adjusted_close.index.is_monotonic_increasing:
        raise ValueError("adjusted_close 必須按交易日升冪排序，否則位移語意錯誤")
    return (
        adjusted_close.shift(FORMATION_SKIP)
        / adjusted_close.shift(FORMATION_LOOKBACK)
        - 1.0
    )


# ══════════════════════════════════════════════════════════════════
#  Point-in-time universe
# ══════════════════════════════════════════════════════════════════
def build_pit_master(staging: pd.DataFrame) -> pd.DataFrame:
    """把 D1 ``security_master_staging.parquet`` 正規化成 PIT 判定所需的最小欄位。

    這是 MOM1-0 稽核要取代的東西：舊 overlap 腳本以
    ``~stock_id.str.startswith("00")`` 近似排除 ETF，那是代號啟發式，
    既非官方分類也非 point-in-time。``asset_type`` 是 D1 由官方上市公司／
    終止上市公司資料集分類出來的欄位，``effective_from`` 是 D1 保證 100%
    覆蓋的生效日（普通股 ``common_effective_from_coverage = 1.0``）。

    採 ``effective_from`` 而非 ``listing_date`` 的理由：``listing_date`` 對
    部分證券為空（D1 標記 ``listing_date_is_exact=False``），若拿它當閘門
    會把有官方行情的股票整段剔除，等於用資料缺口製造倖存者偏誤。
    ``effective_from`` 在沒有官方上市日時退回「首次官方成交日」，方向保守
    （不會讓股票比實際更早進入 universe），且來源逐列記錄於
    ``effective_from_source``。
    """
    for column in ("stock_id", "market", "asset_type", "effective_from"):
        if column not in staging.columns:
            raise ValueError(
                f"security master 缺少 {column!r}；MOM-1 universe 必須由 PIT 欄位判定，"
                "不得以代號前綴或其他啟發式替代"
            )
    master = pd.DataFrame({
        "stock_id": staging["stock_id"].astype(str),
        "market": staging["market"],
        "asset_type": staging["asset_type"],
        "effective_from": pd.to_datetime(staging["effective_from"]),
        "delisting_date": pd.to_datetime(
            staging["delisting_date"] if "delisting_date" in staging.columns else pd.NaT
        ),
    })
    if master["effective_from"].isna().any():
        raise ValueError("effective_from 有缺值；D1 契約要求 100% 覆蓋，不得以缺值列靜默通過")
    return master


def pit_common_stock_mask(master: pd.DataFrame, as_of, stock_ids: Iterable[str]) -> pd.Series:
    """SPEC §7.1.1／§7.1.2：當日是否為有效上市的 TWSE 普通股。

    條件：``market == "TWSE"``、``asset_type == "common_stock"``、
    ``effective_from <= as_of < delisting_date``（``delisting_date`` 缺值
    視為當時尚未下市）。

    兩個刻意的保守選擇：
    - master 查無此代號者一律排除，不假設未知代號是普通股。
    - 下市日當天即排除（嚴格小於），避免最後一個交易日的殘值進入樣本，
      對齊 SPEC §D1「下市後最後價格不可 forward-fill 成永久可交易」。
    """
    missing = set(MASTER_COLUMNS) - set(master.columns)
    if missing:
        raise ValueError(f"PIT master 缺少必要欄位: {sorted(missing)}")

    as_of = pd.Timestamp(as_of)
    effective_from = pd.to_datetime(master["effective_from"])
    delisting = pd.to_datetime(master["delisting_date"])
    active = (
        (master["market"] == "TWSE")
        & (master["asset_type"] == "common_stock")
        & (effective_from <= as_of)
        & (delisting.isna() | (as_of < delisting))
    )
    eligible = set(master.loc[active, "stock_id"].astype(str))
    ids = [str(sid) for sid in stock_ids]
    return pd.Series([sid in eligible for sid in ids], index=ids, dtype=bool)


def pit_common_stock_mask_from_universe_history(
    universe_history: pd.DataFrame, as_of, stock_ids: Iterable[str]
) -> pd.Series:
    """同上，但直接消費 D1 released 的 ``stock_universe_history.parquet``。

    該表是 ``(snapshot_date, stock_id)`` 的月末 PIT snapshot。這裡取
    **不晚於** ``as_of`` 的最近一個 snapshot；取用未來 snapshot 就是 look-ahead，
    因此嚴格禁止。MOM-1 決策日本身就是月末交易日，通常會精確命中同月 snapshot。

    保留兩條路徑是為了讓引擎能消費 Data Authority 實際發佈的任一種形式，
    並在 ``as_of`` 為月末時互相對帳（見 tests/test_momentum.py）。
    """
    required = {"snapshot_date", "stock_id", "asset_type", "is_active"}
    missing = required - set(universe_history.columns)
    if missing:
        raise ValueError(f"universe history 缺少必要欄位: {sorted(missing)}")

    as_of = pd.Timestamp(as_of)
    frame = universe_history.copy()
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"])
    past = frame[frame["snapshot_date"] <= as_of]
    ids = [str(sid) for sid in stock_ids]
    if past.empty:
        return pd.Series(False, index=ids, dtype=bool)

    latest = past[past["snapshot_date"] == past["snapshot_date"].max()]
    active = latest["asset_type"].eq("common_stock") & latest["is_active"].astype(bool)
    if "market" in latest.columns:
        active &= latest["market"].eq("TWSE")
    eligible = set(latest.loc[active, "stock_id"].astype(str))
    return pd.Series([sid in eligible for sid in ids], index=ids, dtype=bool)


def eligible_universe(
    *,
    as_of,
    adjusted_close: pd.DataFrame,
    raw_close: pd.DataFrame,
    turnover: pd.DataFrame,
    signal: pd.DataFrame,
    pit_mask: pd.Series,
    restricted: pd.DataFrame | None = None,
    allow_missing_restrictions: bool = False,
) -> pd.Index:
    """SPEC §7.1 共同 universe，回傳依 stock_id 升冪排序的合格清單。

    七項條件對應：①②由 ``pit_mask`` 帶入（見 ``pit_common_stock_mask``）；
    ③至少 252 個有效交易日；④原始收盤價 >= 10 元；⑤近 20 日平均成交金額
    位於當日前 50%；⑥不可交易排除見下；⑦訊號與價格欄位無缺值，缺值不補 0。

    **無 look-ahead**：所有輸入 frame 一律先 ``.loc[:as_of]`` 截斷才聚合，
    ``as_of`` 之後的列在本函式內不可能被讀到。§7.1 尾段要求流動性門檻
    「不得包含 T+1 成交資訊」，這裡以截斷從結構上保證，而不是靠約定。

    ④用 ``raw_close`` 而非還原價：10 元門檻是「當時實際看得到的股價」，
    用還原價會讓早年高配息股的歷史價被乘上還原係數而虛假通過。

    ⑤有兩個容易寫錯的地方：

    - **母體**：§7.1.5 的比較對象是「當日 TWSE 普通股」，因此分位數的分母是
      當日所有有完整成交金額窗的 PIT 普通股，**不是**已經通過價格與歷史門檻的
      子集。在子集內取中位數會抬高門檻（低價股與新股多半成交金額也低，
      先把它們剔除會讓剩下的分布整體右移），使 universe 比規格更窄。
    - **缺值**：要求 20 個 session 全部有值（不足即淘汰），因為 §7.1.7 明定
      缺值不得補 0；對缺幾天就改用較短窗平均，等於默默放寬流動性門檻。

    ⑥``restricted`` 為當日不可交易旗標（True＝不可成交）。目前 TWSE
    2005~2014 並沒有這份資料（見 MOM1_ENGINE_READINESS.md 的資料缺陷），
    因此必須顯式傳 ``allow_missing_restrictions=True`` 才能在缺它的情況下
    繼續——讓「這條規則還沒被套用」永遠是一個刻意的決定。
    """
    as_of = pd.Timestamp(as_of)
    if restricted is None and not allow_missing_restrictions:
        raise ValueError(
            "SPEC §7.1.6 要求排除處置／停止交易／全額交割等不可成交股票；"
            "未提供 restricted 時必須顯式傳入 allow_missing_restrictions=True "
            "並在報告中揭露這個缺口"
        )

    stock_ids = adjusted_close.columns
    mask = pit_mask.reindex(stock_ids).fillna(False).astype(bool)

    history = adjusted_close.loc[:as_of].notna().sum() >= MIN_HISTORY_SESSIONS
    price_present = adjusted_close.loc[as_of].notna()
    price_floor = (raw_close.loc[as_of] >= MIN_PRICE).fillna(False)
    signal_present = signal.loc[as_of].notna()

    window = turnover.loc[:as_of].tail(LIQUIDITY_WINDOW)
    if len(window) < LIQUIDITY_WINDOW:
        return pd.Index([], name="stock_id", dtype=object)
    liquidity = window.mean(skipna=False).reindex(stock_ids)

    # 分母＝當日有完整 20 日成交金額窗的 PIT 普通股（見 docstring ⑤）。
    reference = liquidity[mask & liquidity.notna()]
    if reference.empty:
        return pd.Index([], name="stock_id", dtype=object)
    threshold = reference.quantile(1 - LIQUIDITY_TOP_FRAC)
    liquid_enough = liquidity.notna() & (liquidity >= threshold)

    base = mask & history & price_present & price_floor & signal_present & liquid_enough

    if restricted is not None:
        if as_of in restricted.index:
            blocked = restricted.loc[as_of].reindex(stock_ids).fillna(False).astype(bool)
        else:
            # 有 restricted 表卻查無當日資料，代表這一天的可交易狀態未知。
            # 未知不等於「可交易」，全部擋下比默默放行安全。
            blocked = pd.Series(True, index=stock_ids)
        base = base & ~blocked

    return pd.Index(sorted(stock_ids[base.to_numpy()]), name="stock_id", dtype=object)


# ══════════════════════════════════════════════════════════════════
#  排名與選股
# ══════════════════════════════════════════════════════════════════
def rank_percentile(signal_values: pd.Series) -> pd.Series:
    """SPEC §7.2「橫斷面百分位排名」的字面實作（越大越強，同分共享百分位）。

    只供診斷與報告使用。**選股不使用它**——理由見 ``deterministic_rank``。
    """
    return signal_values.rank(pct=True, method="average")


def deterministic_rank(signal_values: pd.Series) -> pd.Series:
    """依 ``(訊號降冪, stock_id 升冪)`` 的全序給 1-based 名次。

    為什麼不直接用百分位切門檻：``rank(method="average")`` 會讓同分股票拿到
    **相同**的百分位，於是一整團同分股票只能整團進、整團出，§7.2 明文要求的
    「同分時以 stock_id 升冪 deterministic tie-break」永遠不會生效；若改用
    ``method="first"`` 則名次取決於輸入欄位順序，等於把結果綁在
    DataFrame 的欄位排列上——兩者都不符合規格。建立顯式全序才能讓
    tie-break 真正決定「同分時誰入選」，且與輸入順序無關。
    """
    if signal_values.isna().any():
        raise ValueError("訊號有缺值；§7.1.7 要求缺值不得進入排名，也不得補 0")
    ordered = sorted(signal_values.index, key=lambda sid: (-signal_values[sid], str(sid)))
    return pd.Series({sid: i + 1 for i, sid in enumerate(ordered)}, dtype=int)


def _cutoff(n: int, fraction: float) -> int:
    """取 ``floor(n * fraction)``，用 1e-9 epsilon 吸收二進位浮點誤差。

    向下取整是「只取前 10%」的保守讀法（不超收）。名額為 0 時就是選不出股票，
    此時依 §7.4「候選不足時保留現金」，不得為了湊滿而放寬門檻。
    """
    return int(n * fraction + 1e-9)


def select_holdings(
    signal_values: pd.Series,
    previous_holdings: Iterable[str] = (),
    *,
    max_positions: int | None = None,
) -> list[str]:
    """SPEC §7.2：新進場取前 10%，既有部位跌出前 20% 才賣。

    名額以 ``floor(n * 10%)`` 與 ``floor(n * 20%)`` 計算，同分由
    ``deterministic_rank`` 的 ``stock_id`` 升冪裁決，與輸入順序無關。

    ``max_positions``（§7.4 最大 10 檔）：先保留仍在前 20% 的既有部位，
    剩餘名額才給新進場者。理由是 §7.6 把賣出觸發條件列舉完畢，
    「排名跌出前 20%」是唯一與排名有關的賣出理由；若讓新進場者擠掉一檔
    仍在前 20% 的持股，等於新增一條規格沒有的賣出規則，也抵銷 buffer
    本來要降低換手的目的。這個判讀已列入 readiness 報告的待確認事項。
    """
    rank = deterministic_rank(signal_values)
    n = len(rank)
    entry_cutoff = _cutoff(n, ENTRY_TOP_FRAC)
    keep_cutoff = _cutoff(n, EXIT_BOTTOM_FRAC)

    held = {str(sid) for sid in previous_holdings}
    keep = {sid for sid in rank.index if str(sid) in held and rank[sid] <= keep_cutoff}
    entrants = {sid for sid in rank.index if rank[sid] <= entry_cutoff} - keep

    ordered = sorted(keep, key=lambda sid: rank[sid]) + sorted(entrants, key=lambda sid: rank[sid])
    return ordered if max_positions is None else ordered[:max_positions]
