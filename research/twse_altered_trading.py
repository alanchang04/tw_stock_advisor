"""TWSE 變更交易方法／全額交割證券的正規化（D6 第二部分）。

`SPEC_DATA_FOUNDATION_AND_MOMENTUM.md` §7.1.6 要求排除「非處置／停止交易／
全額交割等無法按模型成交的股票」。處置由
`twse_disposition_punish_2005_2014` 提供，本模組負責另一半：**變更交易方法**。

跨年代語意變遷（刻意保留，不合併）
----------------------------------
官方 `TWT85U` 報表在 2005~2014 之間換過名稱與欄位：

- 早期標題為「全額交割證券」，欄位僅 `證券代號`、`證券名稱`。
- 後期標題為「變更交易」，多一欄 `分盤集合競價(以**表示)`。

兩者都表示「該證券當日被變更交易方法」，因此 `altered_trading` 在整段期間
語意一致、可直接使用。但 `periodic_call_auction`（分盤集合競價）**只在後期
才被揭露**，早期必須是 `NA` 而不是 `False`——SPEC §2.2.5「缺資料不等於 0」。
把早期補成 False 會把「當時不揭露」誤述成「當時沒有分盤」，且方向剛好會讓
回測誤以為那些股票比實際更好成交。

本模組只做「官方回應 → 逐日觀察表」的轉換，不做 promotion，也不判斷
MOM-1 該不該排除某檔股票——那是策略層 `eligible_universe` 的責任。
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
from typing import Iterable

import pandas as pd

# 報表標題形如「094年01月03日 全額交割證券」或「103年12月31日 變更交易」
TITLE_PATTERN = re.compile(r"^(?P<roc>\d{2,3})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*(?P<report>.+?)\s*$")
PERIODIC_CALL_AUCTION_FIELD = "分盤集合競價"
PERIODIC_CALL_AUCTION_MARK = "**"

OBSERVATION_COLUMNS = [
    "snapshot_date", "stock_id", "stock_name",
    "altered_trading", "periodic_call_auction",
    "report_title", "source_schema", "raw_sha256",
]


def parse_title_date(title: str) -> pd.Timestamp | None:
    """由官方標題解析民國日期。無法解析時回傳 None，由呼叫端決定是否失敗。"""
    match = TITLE_PATTERN.match((title or "").strip())
    if not match:
        return None
    year = int(match.group("roc")) + 1911
    return pd.Timestamp(year=year, month=int(match.group("month")), day=int(match.group("day")))


def schema_of(fields: Iterable[str]) -> str:
    """依欄位判定該日屬於哪一種官方 schema。

    `with_periodic_call_auction` 才有分盤集合競價欄位；`code_name_only` 是早期
    的兩欄版本。出現未知欄位組合時回傳 `unknown`，並由品質閘門擋下——
    寧可停下來，也不要用猜的欄位順序去讀資料。
    """
    names = [str(f) for f in (fields or [])]
    if len(names) >= 3 and any(PERIODIC_CALL_AUCTION_FIELD in n for n in names):
        return "with_periodic_call_auction"
    if len(names) == 2:
        return "code_name_only"
    return "unknown"


def normalize_payload(payload: dict, *, raw_sha256: str,
                      fallback_date: pd.Timestamp | None = None) -> pd.DataFrame:
    """把單日官方回應轉成逐檔觀察列。

    `stat != "OK"` 直接拋出：官方明確表示查詢失敗時，不得當成「當日無變更交易」。
    這兩件事在語意上完全不同，混淆會讓缺漏的日子看起來像乾淨的日子。
    """
    if payload.get("stat") != "OK":
        raise ValueError(f"official stat={payload.get('stat')!r}；不得視為當日無事件")

    title = payload.get("title") or ""
    observed = parse_title_date(title) or fallback_date
    if observed is None:
        raise ValueError(f"無法由標題解析日期，且未提供 fallback_date: {title!r}")

    fields = payload.get("fields") or []
    schema = schema_of(fields)
    if schema == "unknown":
        raise ValueError(f"未知的官方欄位組合: {fields!r}")

    rows = []
    for row in payload.get("data") or []:
        stock_id = str(row[0]).strip()
        stock_name = str(row[1]).strip() if len(row) > 1 else ""
        # 官方在「當日無資料」時會回一列文字佔位，不是真的證券。
        if not stock_id or not re.fullmatch(r"[0-9A-Z]{4,6}", stock_id):
            continue
        if schema == "with_periodic_call_auction":
            mark = str(row[2]).strip() if len(row) > 2 else ""
            periodic = mark == PERIODIC_CALL_AUCTION_MARK
        else:
            periodic = None          # 早期不揭露；缺值不得補 False
        rows.append({
            "snapshot_date": observed,
            "stock_id": stock_id,
            "stock_name": stock_name,
            "altered_trading": True,
            "periodic_call_auction": periodic,
            "report_title": title.strip(),
            "source_schema": schema,
            "raw_sha256": raw_sha256,
        })

    frame = pd.DataFrame(rows, columns=OBSERVATION_COLUMNS)
    if not frame.empty:
        frame["periodic_call_auction"] = frame["periodic_call_auction"].astype("boolean")
        frame["altered_trading"] = frame["altered_trading"].astype("boolean")
    return frame


def read_raw(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def restriction_frame(observations: pd.DataFrame, trading_days: Iterable,
                      stock_ids: Iterable[str]) -> pd.DataFrame:
    """把逐日觀察展開成 `eligible_universe(restricted=...)` 需要的布林矩陣。

    只有「當日確實出現在官方變更交易名單」才是 True。沒有觀察的日子是 False，
    因為官方每個交易日都發佈完整名單——**前提是該日的 raw 存在**。
    快照建構器負責保證交易日全覆蓋，缺日必須擋在品質閘門，不能靠這裡補。
    """
    index = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Index(list(trading_days))).unique()))
    ids = [str(s) for s in stock_ids]
    frame = pd.DataFrame(False, index=index, columns=ids)
    if observations.empty:
        return frame
    known = set(ids)
    for day, stock_id in zip(pd.to_datetime(observations["snapshot_date"]),
                             observations["stock_id"].astype(str)):
        if stock_id in known and day in frame.index:
            frame.loc[day, stock_id] = True
    return frame
