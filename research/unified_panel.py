"""把三份不同管線的價格資料接成 2008~2026 的統一研究面板。

來源與接縫
----------
    2008–2014 TWSE   data/research_versions/twse_prices_2005_2014_v1
    2008–2014 TPEx   data/research/tpex_history_2008_2014
    2015–2026 兩市    data/research

接縫在 2015-01，已由 `scripts/audit_price_pipeline_seam.py` 稽核，
判決 **clean**（`reports/STEP0_PRICE_SEAM_AUDIT.md`）。

強制前處理（步驟 0 的結論，不做就不成立）
------------------------------------------
**全程限制四位數代號。** 舊 TWSE 來源含 2.57% 的非四位數證券
（權證／ETN／TDR 等），新資料 0%。不濾掉，兩段的母體組成就不同。

還原慣例
--------
三個事件來源用同一條路徑：把除權息日**之前**的價格乘上
``參考價 / 除權息前收盤``。已驗證 TWSE 官方的 ``adjustment_factor``
與 ``reference_price / pre_event_close`` **完全相等**（最大絕對差 0），
因此三份可以無縫統一。

**這個面板只供訊號比值使用**，不供執行帳本或績效宣稱
（沿用 D3 元件的既有限制）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

OLD_TWSE_PRICES = ROOT / "data/research_versions/twse_prices_2005_2014_v1/prices.parquet"
OLD_TPEX_PRICES = ROOT / "data/research/tpex_history_2008_2014/prices.parquet"
NEW_PRICES = ROOT / "data/research/prices.parquet"

TWSE_ACTIONS = (ROOT / "data/research_versions/twse_corporate_actions_2005_2014"
                       "_staging_v4/corporate_actions.parquet")
TPEX_DIVIDENDS = ROOT / "data/research/tpex_history_2008_2014/dividend_events.parquet"
NEW_DIVIDENDS = ROOT / "data/research/dividend_events.parquet"

PANEL_START = "2008-01-01"
PANEL_END = "2026-08-13"          # forward_only 從 2026-08-14 起，不得跨過

# 流動性篩選：沿用 MOM-1 §7.1／H04 的同一口徑，兩段套用同一套
MIN_PRICE = 10.0
LIQUIDITY_WINDOW = 20


def _require(path: Path, label: str) -> pd.DataFrame:
    """缺檔即拋錯。**不得靜默跳過**——步驟 0 的稽核就是這樣被自己騙過的。"""
    if not path.exists():
        raise FileNotFoundError(f"面板來源缺失：{label} → {path}")
    return pd.read_parquet(path)


def _load_prices(path: Path, columns: list[str]) -> pd.DataFrame:
    frame = _require(path, str(path))
    frame = frame.loc[:, [c for c in columns if c in frame.columns]].copy()
    frame["stock_id"] = frame["stock_id"].astype(str)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def load_events() -> pd.DataFrame:
    """三份事件來源合成一張表：``stock_id / ex_date / ratio``。

    ``ratio`` = 參考價 / 除權息前收盤，套用於**該日之前**的價格。
    """
    twse = _require(TWSE_ACTIONS, "twse_corporate_actions")
    twse = pd.DataFrame({
        "stock_id": twse["stock_id"].astype(str),
        "ex_date": pd.to_datetime(twse["event_date"]),
        "ratio": pd.to_numeric(twse["adjustment_factor"], errors="coerce"),
        "source": "twse_actions",
    })

    frames = [twse]
    for path, label in ((TPEX_DIVIDENDS, "tpex_dividends"),
                        (NEW_DIVIDENDS, "research_dividends")):
        raw = _require(path, label)
        frames.append(pd.DataFrame({
            "stock_id": raw["stock_id"].astype(str),
            "ex_date": pd.to_datetime(raw["ex_date"]),
            "ratio": (pd.to_numeric(raw["ref_price"], errors="coerce")
                      / pd.to_numeric(raw["pre_close"], errors="coerce")),
            "source": label,
        }))

    events = pd.concat(frames, ignore_index=True)
    events = events[events["stock_id"].str.fullmatch(r"\d{4}")]
    events = events.dropna(subset=["ex_date", "ratio"])
    events = events[(events["ratio"] > 0) & np.isfinite(events["ratio"])]
    # 同一檔同一天只留一筆——三個來源在邊界可能重疊
    return events.sort_values("source").drop_duplicates(["stock_id", "ex_date"])


def backward_adjust(raw_close: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """後復權：除權息日**之前**的價格乘上累積比值。

    向量化實作——逐檔迴圈在 2,000 檔 × 4,600 天上太慢。
    """
    relevant = events[events["stock_id"].isin(raw_close.columns)]
    positions = raw_close.index.searchsorted(relevant["ex_date"].to_numpy(), side="left")
    columns = raw_close.columns.get_indexer(relevant["stock_id"].to_numpy())
    factor = np.ones(raw_close.shape, dtype=float)
    for row, col, ratio in zip(positions, columns, relevant["ratio"].to_numpy()):
        if col < 0 or row <= 0:          # 事件在面板之前或該股不在面板中
            continue
        factor[:row, col] *= ratio
    # **要乘回原始價格。** 第一版直接回傳係數矩陣，於是「還原價」其實是
    # 一個階梯函數：動能訊號的中位數恰為 0、還原係數最後一日不等於 1，
    # 兩個症狀都指向同一個缺漏。
    return raw_close * pd.DataFrame(factor, index=raw_close.index,
                                    columns=raw_close.columns)


# 台股有 ±7%／±10% 漲跌幅限制，因此**單日 |報酬| > 20% 在物理上不可能**。
# 事件還原之後仍然出現的跳空，必定是事件表漏記的公司行動（實測 78 筆、
# 涉及 70／2,214 檔，含 0050 在 2025-06-18 的 1:4 分割 −74.8%）。
RESIDUAL_JUMP_THRESHOLD = 0.20


def repair_residual_jumps(adjusted: pd.DataFrame,
                          threshold: float = RESIDUAL_JUMP_THRESHOLD) -> tuple:
    """補掉事件表漏記的公司行動：把跳空**之前**的價格乘上跳空比值。

    **順序與專案其他腳本相反，這是刻意的。** 其他腳本做
    ``apply_total_return_adjustment(closes.apply(split_adjust), dividends)``
    ——先偵測跳空再套事件。那會讓**已記錄的減資被還原兩次**
    （例如 ratio 0.087 的個案，跳空偵測與事件表會各修一次）。

    這裡改成**事件還原之後**才偵測殘餘跳空，因此每一筆殘餘跳空
    依定義都是事件表沒有涵蓋的，不會重複扣。
    """
    # **只比對相鄰交易日。** 漲跌幅的論證只適用於連續交易的兩天：
    # 停牌數月後復牌的價格差是合法的大幅變動（台股復牌首日常無漲跌幅限制），
    # 把它當成分割修掉是錯的。第一版比較「連續的有效值」（跨越 NaN 缺口），
    # 於是修了 824 筆而不是 78 筆——多出來的 746 筆全是缺口，不是分割。
    values = adjusted.to_numpy(dtype=float, copy=True)
    repaired = []
    for col in range(values.shape[1]):
        series = values[:, col]
        previous, current = series[:-1], series[1:]
        usable = (np.isfinite(previous) & np.isfinite(current)
                  & (previous > 0) & (current > 0))
        ratio = np.where(usable, current / np.where(previous > 0, previous, 1.0), 1.0)
        hits = np.flatnonzero(usable & (np.abs(ratio - 1.0) > threshold))
        for hit in hits:
            row = hit + 1
            series[:row] *= float(ratio[hit])
            repaired.append({"column": int(col), "row": int(row),
                             "ratio": float(ratio[hit])})
        values[:, col] = series
    # 跨缺口的大跳空**不修**。台股復牌首日常無漲跌幅限制，因此停牌後的
    # 大幅重定價是合法的；把它當分割修掉是錯的（第一版就這樣多修了 746 筆）。
    # 但它同樣會污染跨越它的動能訊號，所以**標記為不可用**——
    # 修不確定的東西不如把它標成不可用。實測 746 筆、涉及 395 檔，
    # 其中只有約 31 筆比值接近 1:2／1:4／1:5／1:10 這類簡單分數。
    contaminated = pd.DataFrame(False, index=adjusted.index,
                                columns=adjusted.columns)
    gap_spanning = 0
    for col in range(values.shape[1]):
        series = values[:, col]
        finite = np.flatnonzero(np.isfinite(series) & (series > 0))
        if finite.size < 2:
            continue
        prices = series[finite]
        ratio = prices[1:] / prices[:-1]
        for hit in np.flatnonzero(np.abs(ratio - 1.0) > threshold):
            if finite[hit + 1] - finite[hit] > 1:      # 相鄰日的已被修掉
                contaminated.iat[finite[hit + 1], col] = True
                gap_spanning += 1

    out = pd.DataFrame(values, index=adjusted.index, columns=adjusted.columns)
    summary = {
        "gap_spanning_marked_unusable": gap_spanning,
        "gap_spanning_securities": int(contaminated.any().sum()),
        "threshold": threshold,
        "repaired_jumps": len(repaired),
        "securities_affected": len({r["column"] for r in repaired}),
        "why": ("Taiwan has daily price limits, so a single-day |return| > 20% "
                "after event adjustment can only be a corporate action the event "
                "tables missed (ETF splits are the common case)."),
    }
    return out, contaminated, summary


@dataclass(frozen=True)
class Panel:
    """統一研究面板。所有寬表 index=交易日、columns=stock_id。"""

    close: pd.DataFrame           # 原始收盤
    adjusted: pd.DataFrame        # 後復權收盤（僅供訊號比值）
    turnover: pd.DataFrame
    eligible: pd.DataFrame        # 流動性合格遮罩
    log_size: pd.DataFrame        # log1p(20 日均成交值)
    source_of_day: pd.Series      # 每個交易日來自哪一段（供診斷）
    contaminated: pd.DataFrame    # 跨停牌大跳空：不修，標為不可用
    jump_repair: dict             # 事件表漏記的公司行動修補摘要

    def usable_signal_mask(self, lookback: int) -> pd.DataFrame:
        """形成窗內含跨停牌跳空者一律排除。

        跨停牌的大幅重定價無法可靠地與分割區分，因此**不修也不用**——
        修不確定的東西不如把它標成不可用。
        """
        window = self.contaminated.rolling(lookback, min_periods=1).max()
        return ~window.fillna(0.0).astype(bool)

    @property
    def sessions(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.close.index)


def build_panel(start: str = PANEL_START, end: str = PANEL_END) -> Panel:
    columns = ["stock_id", "trade_date", "close", "turnover"]
    old_twse = _load_prices(OLD_TWSE_PRICES, columns)
    old_tpex = _load_prices(OLD_TPEX_PRICES, columns)
    new = _load_prices(NEW_PRICES, columns)

    old_twse["segment"] = "twse_2008_2014"
    old_tpex["segment"] = "tpex_2008_2014"
    new["segment"] = "research_2015_2026"

    frame = pd.concat([old_twse, old_tpex, new], ignore_index=True)
    # 步驟 0 的強制前處理
    frame = frame[frame["stock_id"].str.fullmatch(r"\d{4}")]
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start))
                  & (frame["trade_date"] <= pd.Timestamp(end))]
    if frame.duplicated(["stock_id", "trade_date"]).any():
        raise ValueError("同一 stock_id/trade_date 在多個來源出現——來源期間應互斥")

    close = frame.pivot(index="trade_date", columns="stock_id",
                        values="close").sort_index()
    turnover = frame.pivot(index="trade_date", columns="stock_id",
                           values="turnover").sort_index()
    source_of_day = (frame.groupby("trade_date")["segment"]
                     .agg(lambda s: "+".join(sorted(set(s)))))

    adjusted, contaminated, jump_repair = repair_residual_jumps(
        backward_adjust(close, load_events()))

    liquidity = turnover.rolling(LIQUIDITY_WINDOW).mean()
    eligible = (close >= MIN_PRICE) & liquidity.ge(liquidity.median(axis=1), axis=0)
    log_size = np.log1p(liquidity)

    return Panel(close=close, adjusted=adjusted, turnover=turnover,
                 eligible=eligible.fillna(False), log_size=log_size,
                 source_of_day=source_of_day, contaminated=contaminated,
                 jump_repair=jump_repair)


def month_end_sessions(sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每月最後一個交易日。"""
    series = pd.Series(sessions, index=sessions)
    return pd.DatetimeIndex(series.resample("ME").last().dropna())


def momentum_signal(adjusted: pd.DataFrame, *, lookback: int, skip: int) -> pd.DataFrame:
    """``close[t-skip] / close[t-lookback] - 1``，跳過最近 ``skip`` 個交易日。

    跳過最近一個月是為了避開短期反轉（Jegadeesh-Titman 的標準作法）。
    """
    if lookback <= skip:
        raise ValueError("lookback 必須大於 skip")
    return adjusted.shift(skip) / adjusted.shift(lookback) - 1.0
