"""Parse MOPS dividend distributions and match them to TWSE ex-date events.

MOPS reports dividend amounts in TWD per old share.  Stock dividends are also
reported as TWD per share (normally at TWD 10 par), so an amount of 0.5 means
0.05 new shares for each old share.  These declared terms, rather than a ratio
inferred from a rounded reference price, are required for an actual-share
position ledger.
"""
from __future__ import annotations

import math
import re
from io import StringIO

import pandas as pd


REFERENCE_TOLERANCE = 0.011
STOCK_ID_PATTERN = re.compile(r"^\s*([0-9A-Z]{4,6})\s*[-－]")


def _label(column) -> str:
    parts = column if isinstance(column, tuple) else (column,)
    return "".join(str(part) for part in parts if not str(part).startswith("Unnamed"))


def _compact(value) -> str:
    return re.sub(r"[\s、，,（）()]", "", str(value))


def _number(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sum_numbers(row: pd.Series, columns: list) -> float | None:
    values = [_number(row[column]) for column in columns]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _find_columns(table: pd.DataFrame, predicates: list[tuple[str, ...]]) -> list:
    result = []
    for column in table.columns:
        label = _compact(_label(column))
        if any(all(term in label for term in predicate) for predicate in predicates):
            result.append(column)
    return result


def _first_column(table: pd.DataFrame, term: str):
    columns = _find_columns(table, [(term,)])
    return columns[0] if columns else None


def parse_mops_dividend_html(raw: bytes, distribution_year_roc: int) -> pd.DataFrame:
    """Normalize all company tables in a Big5/CP950 MOPS aggregate response."""
    html = raw.decode("cp950", errors="replace")
    records: list[dict] = []
    for table_index, table in enumerate(pd.read_html(StringIO(html))):
        stock_column = _first_column(table, "公司代號")
        if stock_column is None:
            continue

        labels = {_compact(_label(column)): column for column in table.columns}
        old_cash = _find_columns(table, [("股東股利", "現金股利", "元/股")])
        new_cash = _find_columns(table, [
            ("股東配發內容", "盈餘分配", "現金股利", "元/股"),
            ("股東配發內容", "公積", "發放", "現金", "元/股"),
        ])
        old_stock = _find_columns(table, [
            ("股東股利", "盈餘配股", "元/股"),
            ("資本公積", "轉增資", "元/股"),
        ])
        new_stock = _find_columns(table, [
            ("股東配發內容", "盈餘轉增資配股", "元/股"),
            ("股東配發內容", "公積", "轉增資配股", "元/股"),
        ])
        cash_columns = new_cash or old_cash
        stock_columns = new_stock or old_stock
        if not cash_columns and not stock_columns:
            continue

        source_column = _first_column(table, "資料來源")
        progress_column = _first_column(table, "決議（擬議）進度")
        period_column = _first_column(table, "期別")
        board_column = _first_column(table, "董事會決議")
        shareholder_column = _first_column(table, "股東會日期")

        for row_index, row in table.iterrows():
            stock_text = str(row[stock_column])
            match = STOCK_ID_PATTERN.match(stock_text)
            if not match:
                continue
            cash = _sum_numbers(row, cash_columns)
            stock_value = _sum_numbers(row, stock_columns)
            cash = 0.0 if cash is None else cash
            stock_value = 0.0 if stock_value is None else stock_value
            records.append({
                "distribution_year_roc": int(distribution_year_roc),
                "stock_id": match.group(1),
                "stock_name": STOCK_ID_PATTERN.sub("", stock_text, count=1).strip(),
                "decision_status": (
                    str(row[progress_column]).strip() if progress_column is not None
                    else str(row[source_column]).strip() if source_column is not None
                    else None
                ),
                "period": str(row[period_column]).strip() if period_column is not None else None,
                "board_resolution_date_raw": (
                    str(row[board_column]).strip() if board_column is not None else None
                ),
                "shareholder_meeting_date_raw": (
                    str(row[shareholder_column]).strip()
                    if shareholder_column is not None else None
                ),
                "cash_per_old_share": cash,
                "stock_dividend_value_per_old_share": stock_value,
                "free_share_multiplier": 1.0 + stock_value / 10.0,
                "source_table_index": table_index,
                "source_row_index": int(row_index),
                "source_schema_columns": len(labels),
            })
    return pd.DataFrame.from_records(records)


def _economic_candidates(
    candidates: pd.DataFrame, *, include_cash: bool = True,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    result = candidates.copy()
    result["_cash_key"] = result["cash_per_old_share"].round(8)
    result["_multiplier_key"] = result["free_share_multiplier"].round(8)
    keys = ["_multiplier_key"]
    if include_cash:
        keys.insert(0, "_cash_key")
    return result.drop_duplicates(keys)


def match_mops_terms_to_events(
    events: pd.DataFrame,
    terms: pd.DataFrame,
    tolerance: float = REFERENCE_TOLERANCE,
) -> pd.DataFrame:
    """Add unique official dividend-term matches to TWSE corporate actions."""
    output: list[dict] = []
    by_stock = {
        stock_id: group.copy()
        for stock_id, group in terms.groupby("stock_id", sort=False)
    }
    for _, event in events.iterrows():
        kind = str(event.get("event_kind") or "")
        base = {
            "mops_match_status": "not_applicable",
            "mops_candidate_count": 0,
            "mops_match_method": None,
            "mops_distribution_year_roc": None,
            "mops_cash_per_old_share": None,
            "mops_stock_dividend_value_per_old_share": None,
            "mops_free_share_multiplier": None,
            "mops_reference_error": None,
        }
        if kind not in {"ex_right", "ex_right_dividend"}:
            output.append(base)
            continue
        pre_close = _number(event.get("pre_event_close"))
        ex_reference = _number(event.get("ex_right_reference_price"))
        if pre_close is None or ex_reference is None or ex_reference <= 0:
            base["mops_match_status"] = "event_reference_missing"
            output.append(base)
            continue
        event_year = pd.Timestamp(event["event_date"]).year - 1911
        candidates = by_stock.get(str(event["stock_id"]), pd.DataFrame()).copy()
        if candidates.empty:
            base["mops_match_status"] = "stock_not_found"
            output.append(base)
            continue
        same_year = candidates[candidates["distribution_year_roc"].eq(event_year)].copy()
        previous_year = candidates[
            candidates["distribution_year_roc"].eq(event_year - 1)
        ].copy()
        candidates = same_year if not same_year.empty else previous_year
        candidates = candidates[candidates["free_share_multiplier"] > 1.0].copy()
        if candidates.empty:
            base["mops_match_status"] = "year_or_stock_terms_not_found"
            output.append(base)
            continue
        if kind == "ex_right":
            candidates["_predicted_reference"] = (
                pre_close / candidates["free_share_multiplier"]
            )
        else:
            candidates["_predicted_reference"] = (
                (pre_close - candidates["cash_per_old_share"])
                / candidates["free_share_multiplier"]
            )
        candidates["_reference_error"] = (
            candidates["_predicted_reference"] - ex_reference
        ).abs()
        equation_matches = candidates[
            candidates["_reference_error"] <= tolerance
        ].copy()
        equation_matches = _economic_candidates(
            equation_matches, include_cash=kind == "ex_right_dividend"
        )
        match_method = "reference_equation"
        selected = equation_matches
        if selected.empty:
            # Before employee bonuses were expensed, the TWSE reference-price
            # equation could include dilution that is not a shareholder share
            # entitlement.  Fall back only to a unique declared term set in the
            # decision year, optionally anchored by TWT49U's declared cash.
            fallback = candidates.copy()
            twse_cash = _number(event.get("cash_value"))
            if kind == "ex_right_dividend" and twse_cash is not None:
                fallback = fallback[
                    (fallback["cash_per_old_share"] - twse_cash).abs() <= tolerance
                ]
                match_method = "declared_cash_and_decision_year"
            elif kind == "ex_right":
                match_method = "unique_stock_terms_in_decision_year"
            else:
                match_method = "unique_dividend_terms_in_decision_year"
            selected = _economic_candidates(
                fallback, include_cash=kind == "ex_right_dividend"
            )

        base["mops_candidate_count"] = int(len(selected))
        if len(selected) == 0:
            base["mops_match_status"] = "declared_terms_no_match"
        elif len(selected) > 1:
            base["mops_match_status"] = "reference_equation_ambiguous"
        else:
            candidate = selected.iloc[0]
            base.update({
                "mops_match_status": "matched_unique",
                "mops_match_method": match_method,
                "mops_distribution_year_roc": int(candidate["distribution_year_roc"]),
                "mops_cash_per_old_share": float(candidate["cash_per_old_share"]),
                "mops_stock_dividend_value_per_old_share": float(
                    candidate["stock_dividend_value_per_old_share"]
                ),
                "mops_free_share_multiplier": float(candidate["free_share_multiplier"]),
                "mops_reference_error": float(candidate["_reference_error"]),
            })
        output.append(base)
    return pd.concat([events.reset_index(drop=True), pd.DataFrame(output)], axis=1)
