"""Extract paid-subscription facts from MOPS historical material events."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

import pandas as pd


SUBJECT_PATTERN = re.compile(
    r"現金增資|現增|認購比率|發行價格|每[仟千]股.{0,12}認購"
)
SETTLEMENT_SUBJECT_PATTERN = re.compile(
    r"(?:(?:現金增資|現增|增資新股|新股|股款).{0,30}"
    r"(?:繳款|認股基準|增資基準|上市|發放|交付|催繳)|"
    r"(?:繳款|認股基準|增資基準|上市|發放|交付|催繳).{0,30}"
    r"(?:現金增資|現增|增資新股|新股|股款))"
)
ROC_DATE_PATTERN = re.compile(r"(?<!\d)(\d{2,3})[年/](\d{1,2})[月/](\d{1,2})日?")
CHINESE_NUMBER = r"[〇零一二兩三四五六七八九十百]+"
DATE_TOKEN = rf"(?:\d{{2,3}}[年/]\d{{1,2}}[月/]\d{{1,2}}日?|{CHINESE_NUMBER}年{CHINESE_NUMBER}月{CHINESE_NUMBER}日)"
CHINESE_ROC_DATE_PATTERN = re.compile(
    rf"({CHINESE_NUMBER})年({CHINESE_NUMBER})月({CHINESE_NUMBER})日"
)
THOUSAND_SHARE_PATTERNS = [
    re.compile(
        r"每[仟千]股(?:可|得|有權)?(?:優先)?認購(?:現金增資)?(?:新股)?"
        r"[^0-9]{0,12}([0-9][0-9,]*(?:\.[0-9]+)?)\s*股"
    ),
    re.compile(
        r"原股東[^。；\n]{0,80}?每[仟千]股[^。；\n]{0,30}?"
        r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*股"
    ),
]
PER_SHARE_PATTERN = re.compile(
    r"每股(?:可|得|有權)?(?:優先)?認購(?:現金增資)?(?:新股)?"
    r"[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)\s*股"
)
ALLOCATION_PATTERNS = [
    re.compile(
        r"(?:發行(?:新)?股數|發行新股總額)之\s*([0-9]+(?:\.[0-9]+)?)\s*%"
        r"[^。；\n]{0,30}?(?:供|由)原股東認購"
    ),
    re.compile(
        r"其餘\s*([0-9]+(?:\.[0-9]+)?)\s*%[^。；\n]{0,30}?(?:供|由)原股東"
    ),
    re.compile(
        r"其餘(?:發行(?:新)?股數之)?\s*([0-9]+(?:\.[0-9]+)?)\s*%"
        r"[^。；\n]{0,60}?(?:供|由)原股東"
    ),
    re.compile(
        r"原股東認購(?:或無償配發)?(?:比率|比例)\s*[:：]\s*"
        r"[^。；\n%]{0,60}?([0-9]+(?:\.[0-9]+)?)\s*%"
    ),
    re.compile(
        r"(?:本次)?(?:增資)?發行(?:新股|股數|總股數|新股總額)(?:之)?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*%[^。；\n]{0,70}?(?:供|由)原股東"
    ),
]
PRICE_VALUE_PATTERN = re.compile(
    r"(?:每股)?\s*(?:新台幣|新臺幣|NT\$?)?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*元"
)
PRICE_CONTEXT_PATTERNS = [
    re.compile(r"(?:實際)?發行價格[^。；]{0,80}"),
    re.compile(r"(?:現金增資|現增)[^。；]{0,180}?每股[^。；]{0,30}?元(?:溢價)?發行"),
    re.compile(r"以每股[^。；]{0,30}?元(?:溢價)?發行"),
]


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def roc_date(value: str) -> date | None:
    text = str(value or "").strip()
    match = ROC_DATE_PATTERN.search(text)
    if not match:
        match = CHINESE_ROC_DATE_PATTERN.search(text)
        if not match:
            return None
        parts = [_chinese_integer(match.group(index)) for index in range(1, 4)]
        if any(part is None for part in parts):
            return None
        year, month, day = parts
    else:
        year, month, day = map(int, match.groups())
    try:
        return date(year + 1911, month, day)
    except ValueError:
        return None


def _chinese_integer(value: str) -> int | None:
    digits = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if all(character in digits for character in value):
        return int("".join(str(digits[character]) for character in value))
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
        elif character == "十":
            total += (current or 1) * 10
            current = 0
        elif character == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def _dates(values: list[str] | tuple[str, ...]) -> list[str]:
    result = []
    for value in values:
        parsed = roc_date(value)
        if parsed is not None:
            result.append(parsed.isoformat())
    return result


def extract_subscription_settlement_facts(text: str) -> dict[str, list[str]]:
    """Extract announced settlement dates without treating plans as completed shares."""
    normalized = str(text or "").replace("，", ",").replace("：", ":")
    facts = {
        "subscription_record_date": [],
        "subscription_payment_start": [],
        "subscription_payment_end": [],
        "capital_increase_effective_date": [],
        "new_share_delivery_date": [],
    }
    payment_patterns = [
        re.compile(
            rf"({DATE_TOKEN})\s*(?:起|至|-|~)\s*({DATE_TOKEN})"
            rf"[^。；\n]{{0,50}}?(?:原股東|股東|員工)[^。；\n]{{0,30}}?(?:股款)?繳(?:款|納)期間"
        ),
        re.compile(
            rf"(?:原股東|股東|員工)[^。；\n]{{0,50}}?(?:股款)?繳(?:款|納)(?:期間|日期)?"
            rf"[^。；\n]{{0,20}}?({DATE_TOKEN})\s*(?:起|至|-|~)\s*({DATE_TOKEN})"
        ),
    ]
    for pattern in payment_patterns:
        for match in pattern.finditer(normalized):
            parsed = _dates([match.group(1), match.group(2)])
            if len(parsed) == 2:
                facts["subscription_payment_start"].append(parsed[0])
                facts["subscription_payment_end"].append(parsed[1])

    directional = {
        "subscription_record_date": "認股基準日",
        "capital_increase_effective_date": "增資基準日",
    }
    for fact_type, keyword in directional.items():
        patterns = [
            re.compile(rf"{keyword}[^。；\n]{{0,20}}?({DATE_TOKEN})"),
            re.compile(rf"({DATE_TOKEN})[^。；\n]{{0,30}}?{keyword}"),
        ]
        for pattern in patterns:
            facts[fact_type].extend(
                parsed for match in pattern.finditer(normalized)
                for parsed in _dates([match.group(1)])
            )

    delivery_patterns = [
        re.compile(
            rf"(?:新股|股款繳納憑證|有價證券)[^。；\n]{{0,40}}?"
            rf"(?:上市|發放|交付)(?:日|日期)?[^。；\n]{{0,15}}?({DATE_TOKEN})"
        ),
        re.compile(
            rf"(?:於|自)\s*({DATE_TOKEN})\s*(?:起)?\s*(?:先行)?(?:以)?"
            rf"(?:新股|股款繳納憑證|有價證券)"
            rf"[^。；\n]{{0,20}}?(?:上市|發放|交付)"
        ),
        re.compile(
            rf"(?:新股|股款繳納憑證|有價證券)[^。；\n]{{0,20}}?"
            rf"(?:預定)?(?:於|自)\s*({DATE_TOKEN})\s*(?:起)?"
            rf"[^。；\n]{{0,12}}?(?:上市|發放|交付)"
        ),
    ]
    for pattern in delivery_patterns:
        facts["new_share_delivery_date"].extend(
            parsed for match in pattern.finditer(normalized)
            for parsed in _dates([match.group(1)])
        )
    return {key: sorted(set(values)) for key, values in facts.items()}


def history_records(payload: dict) -> list[dict]:
    result = payload.get("result") or {}
    records = []
    for row in result.get("data") or []:
        if len(row) < 6 or not isinstance(row[5], dict):
            continue
        detail = row[5]
        parameters = detail.get("parameters") or {}
        records.append({
            "stock_id": str(row[0]).strip(),
            "stock_name": str(row[1]).strip(),
            "announcement_date": roc_date(str(row[2])),
            "announcement_time": str(row[3]).strip(),
            "subject": str(row[4]).replace("\r", "").replace("\n", " ").strip(),
            "detail_api_name": str(detail.get("apiName") or ""),
            "market_kind": str(parameters.get("marketKind") or ""),
            "enter_date": str(parameters.get("enterDate") or ""),
            "serial_number": str(parameters.get("serialNumber") or ""),
        })
    return records


def detail_text(payload: dict) -> str:
    result = payload.get("result") or {}
    rows = result.get("data") or []
    if not rows:
        return ""
    row = rows[0]
    return str(row[9] if len(row) > 9 else "").replace("\r", "")


def extract_subscription_facts(text: str) -> dict:
    """Return conservative price/rate candidates from one announcement body."""
    normalized = str(text or "").replace("，", ",").replace("％", "%")
    rates = []
    for pattern in THOUSAND_SHARE_PATTERNS:
        rates.extend(_number(match) / 1000.0 for match in pattern.findall(normalized))
    rates.extend(_number(match) for match in PER_SHARE_PATTERN.findall(normalized))

    allocations = []
    for pattern in ALLOCATION_PATTERNS:
        allocations.extend(_number(match) / 100.0 for match in pattern.findall(normalized))

    prices = []
    for pattern in PRICE_CONTEXT_PATTERNS:
        for segment in pattern.findall(normalized):
            prices.extend(_number(match) for match in PRICE_VALUE_PATTERN.findall(segment))
    return {
        "subscription_rates": sorted({rate for rate in rates if 0 < rate < 10}),
        "subscription_prices": sorted({price for price in prices if 0 < price < 100_000}),
        "shareholder_allocation_fractions": sorted({
            value for value in allocations if 0 < value <= 1
        }),
    }


def announcement_fact_rows(history: dict, details: dict[tuple[str, str, str], dict]) -> pd.DataFrame:
    rows = []
    for record in history_records(history):
        key = (record["stock_id"], record["enter_date"], record["serial_number"])
        payload = details.get(key)
        if payload is None:
            continue
        body = detail_text(payload)
        facts = extract_subscription_facts(body)
        for fact_type, values in (
            ("subscription_rate", facts["subscription_rates"]),
            ("subscription_price", facts["subscription_prices"]),
            ("shareholder_allocation_fraction", facts["shareholder_allocation_fractions"]),
        ):
            for value in values:
                rows.append({
                    **record,
                    "fact_type": fact_type,
                    "fact_value": float(value),
                    "body": body,
                })
    return pd.DataFrame(rows)


def match_subscription_terms_to_events(
    events: pd.DataFrame,
    facts: pd.DataFrame,
    tolerance: float = 0.011,
    lookback_days: int = 400,
    settlement_forward_days: int = 240,
) -> pd.DataFrame:
    """Match unique official price/rate pairs using the TWSE reference equation."""
    fact_groups = {
        stock_id: group.copy()
        for stock_id, group in facts.groupby("stock_id", sort=False)
    }
    matches = []
    for _, event in events.iterrows():
        base = {
            "subscription_match_status": "not_applicable",
            "subscription_pair_candidates": 0,
            "official_subscription_rate": None,
            "official_subscription_price": None,
            "official_shareholder_allocation_fraction": None,
            "official_paid_dilution_rate": None,
            "subscription_reference_error": None,
            "subscription_rate_announcement_date": None,
            "subscription_price_announcement_date": None,
            "subscription_record_date": None,
            "subscription_payment_start": None,
            "subscription_payment_end": None,
            "capital_increase_effective_date": None,
            "new_share_delivery_date": None,
            "subscription_settlement_match_status": "not_applicable",
        }
        if str(event.get("event_kind") or "") not in {"ex_right", "ex_right_dividend"}:
            matches.append(base)
            continue
        reference = float(event.get("reference_price"))
        ex_reference = float(event.get("ex_right_reference_price"))
        if abs(reference - ex_reference) <= tolerance:
            matches.append(base)
            continue
        event_date = pd.Timestamp(event["event_date"])
        all_stock_facts = fact_groups.get(str(event["stock_id"]), pd.DataFrame()).copy()
        if all_stock_facts.empty:
            base["subscription_match_status"] = "announcement_facts_not_found"
            matches.append(base)
            continue
        all_stock_facts["announcement_date"] = pd.to_datetime(
            all_stock_facts["announcement_date"]
        )
        stock_facts = all_stock_facts[
            all_stock_facts["announcement_date"].between(
                event_date - timedelta(days=int(lookback_days)), event_date, inclusive="both"
            )
        ].copy()
        rate_facts = stock_facts[stock_facts["fact_type"].eq("subscription_rate")]
        price_facts = stock_facts[stock_facts["fact_type"].eq("subscription_price")]
        allocation_facts = stock_facts[
            stock_facts["fact_type"].eq("shareholder_allocation_fraction")
        ]
        if rate_facts.empty or price_facts.empty or allocation_facts.empty:
            base["subscription_match_status"] = "rate_price_or_allocation_not_found_before_event"
            matches.append(base)
            continue

        pre_close = float(event["pre_event_close"])
        cash = 0.0 if event["event_kind"] == "ex_right" else event.get("mops_cash_per_old_share")
        if event["event_kind"] == "ex_right" and abs(pre_close - ex_reference) <= tolerance:
            free_multiplier = 1.0
        else:
            free_multiplier = event.get("mops_free_share_multiplier")
        if pd.isna(cash) or pd.isna(free_multiplier):
            base["subscription_match_status"] = "free_share_or_cash_terms_missing"
            matches.append(base)
            continue

        pairs = []
        for _, rate_row in rate_facts.iterrows():
            entitlement_rate = float(rate_row["fact_value"])
            for _, price_row in price_facts.iterrows():
                price = float(price_row["fact_value"])
                for _, allocation_row in allocation_facts.iterrows():
                    allocation = float(allocation_row["fact_value"])
                    paid_dilution_rate = entitlement_rate / allocation
                    predicted = (
                        pre_close - float(cash) + price * paid_dilution_rate
                    ) / (float(free_multiplier) + paid_dilution_rate)
                    error = abs(predicted - reference)
                    if error <= tolerance:
                        pairs.append({
                            "rate": entitlement_rate,
                            "price": price,
                            "allocation": allocation,
                            "paid_dilution_rate": paid_dilution_rate,
                            "error": error,
                            "rate_date": rate_row["announcement_date"],
                            "price_date": price_row["announcement_date"],
                            "allocation_date": allocation_row["announcement_date"],
                        })
        economic = {}
        for pair in pairs:
            key = (
                round(pair["rate"], 8), round(pair["price"], 8),
                round(pair["allocation"], 8),
            )
            existing = economic.get(key)
            if existing is None or max(
                pair["rate_date"], pair["price_date"], pair["allocation_date"]
            ) > max(
                existing["rate_date"], existing["price_date"], existing["allocation_date"]
            ):
                economic[key] = pair
        base["subscription_pair_candidates"] = len(economic)
        if not economic:
            base["subscription_match_status"] = "reference_equation_no_match"
        elif len(economic) > 1:
            base["subscription_match_status"] = "reference_equation_ambiguous"
        else:
            pair = next(iter(economic.values()))
            base.update({
                "subscription_match_status": "matched_unique",
                "official_subscription_rate": pair["rate"],
                "official_subscription_price": pair["price"],
                "official_shareholder_allocation_fraction": pair["allocation"],
                "official_paid_dilution_rate": pair["paid_dilution_rate"],
                "subscription_reference_error": pair["error"],
                "subscription_rate_announcement_date": pair["rate_date"].date().isoformat(),
                "subscription_price_announcement_date": pair["price_date"].date().isoformat(),
            })
            if "fact_date" in all_stock_facts:
                settlement = all_stock_facts[
                    all_stock_facts["announcement_date"].between(
                        event_date - timedelta(days=120),
                        event_date + timedelta(days=int(settlement_forward_days)),
                        inclusive="both",
                    )
                    & all_stock_facts["fact_date"].notna()
                ].copy()
                settlement["_fact_date"] = pd.to_datetime(
                    settlement["fact_date"], errors="coerce"
                )
                settlement = settlement[
                    settlement["_fact_date"].between(
                        event_date,
                        event_date + timedelta(days=int(settlement_forward_days)),
                        inclusive="both",
                    )
                ]

                # Payment starts and ends must be stated in the same disclosure.
                # Otherwise a later cash issue can be silently spliced onto this one.
                identity_columns = [
                    column for column in ("enter_date", "serial_number")
                    if column in settlement.columns
                ]
                if not identity_columns:
                    identity_columns = ["announcement_date"]
                payment_windows = set()
                for _, announcement in settlement.groupby(
                    identity_columns, dropna=False, sort=False
                ):
                    starts = sorted(set(
                        announcement.loc[
                            announcement["fact_type"].eq("subscription_payment_start"),
                            "_fact_date",
                        ].dropna()
                    ))
                    ends = sorted(set(
                        announcement.loc[
                            announcement["fact_type"].eq("subscription_payment_end"),
                            "_fact_date",
                        ].dropna()
                    ))
                    # Multiple starts or ends in one disclosure commonly mean that
                    # both the old and revised schedule were quoted. Do not guess.
                    if len(starts) == 1 and len(ends) == 1:
                        start, end = starts[0], ends[0]
                        if event_date <= start <= end:
                            payment_windows.add((start, end))

                payment_ambiguous = len(payment_windows) > 1
                payment_end = None
                if len(payment_windows) == 1:
                    payment_start, payment_end = next(iter(payment_windows))
                    base["subscription_payment_start"] = payment_start.date().isoformat()
                    base["subscription_payment_end"] = payment_end.date().isoformat()

                def unique_date(fact_type, earliest=None, latest=None):
                    candidates = settlement.loc[
                        settlement["fact_type"].eq(fact_type), "_fact_date"
                    ].dropna()
                    if earliest is not None:
                        candidates = candidates[candidates.ge(earliest)]
                    if latest is not None:
                        candidates = candidates[candidates.le(latest)]
                    values = sorted(set(candidates))
                    return values[0] if len(values) == 1 else None, len(values) > 1

                record_date, record_ambiguous = unique_date(
                    "subscription_record_date", earliest=event_date
                )
                capital_date, capital_ambiguous = unique_date(
                    "capital_increase_effective_date",
                    earliest=payment_end or event_date,
                )
                delivery_date, delivery_ambiguous = unique_date(
                    "new_share_delivery_date",
                    earliest=payment_end or event_date,
                    latest=(payment_end + timedelta(days=180)) if payment_end else None,
                )
                if record_date is not None:
                    base["subscription_record_date"] = record_date.date().isoformat()
                if capital_date is not None:
                    base["capital_increase_effective_date"] = capital_date.date().isoformat()
                if delivery_date is not None:
                    base["new_share_delivery_date"] = delivery_date.date().isoformat()

                has_payment = bool(
                    base["subscription_payment_start"]
                    and base["subscription_payment_end"]
                )
                has_delivery = bool(base["new_share_delivery_date"])
                any_ambiguous = any((
                    payment_ambiguous, record_ambiguous,
                    capital_ambiguous, delivery_ambiguous,
                ))
                if any_ambiguous:
                    base["subscription_settlement_match_status"] = "settlement_dates_ambiguous"
                elif has_payment and has_delivery:
                    base["subscription_settlement_match_status"] = "matched_payment_and_delivery"
                elif has_payment:
                    base["subscription_settlement_match_status"] = "matched_payment_delivery_missing"
                elif has_delivery:
                    base["subscription_settlement_match_status"] = "matched_delivery_payment_missing"
                else:
                    base["subscription_settlement_match_status"] = "settlement_dates_missing"
        matches.append(base)
    return pd.concat([events.reset_index(drop=True), pd.DataFrame(matches)], axis=1)
