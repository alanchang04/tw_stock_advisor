"""Load a named immutable release for MOM-1 signal-only diagnostics.

This adapter is deliberately narrower than a backtest loader.  It verifies the
descriptor/component hashes needed by MOM-1, constructs PIT universe inputs,
and exposes no return, NAV, cost, or performance calculation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.momentum import build_pit_master, disposition_restriction_frame


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_COMPONENT_FILES = {
    "twse_prices_2005_2014": ("prices.parquet",),
    "twse_security_master_2005_2014": (
        "security_master_staging.parquet",
        "stock_universe_history.parquet",
    ),
    "twse_corporate_actions_2005_2014": ("corporate_actions.parquet",),
    "twse_market_structure_2005_2014": ("market_structure_monthly.parquet",),
    "twse_disposition_punish_2005_2014": (
        "disposition_events.parquet",
        "twse_disposition_source_status.parquet",
    ),
}


@dataclass(frozen=True)
class MomentumReleaseInputs:
    release_id: str
    descriptor_path: Path
    descriptor_sha256: str
    component_content_sha256: dict[str, str]
    verified_input_sha256: dict[str, str]
    raw_open: pd.DataFrame
    raw_close: pd.DataFrame
    volume_shares: pd.DataFrame
    adjusted_close: pd.DataFrame
    turnover: pd.DataFrame
    pit_master: pd.DataFrame
    universe_history: pd.DataFrame
    market_structure: pd.DataFrame
    disposition_events: pd.DataFrame
    disposition_restricted: pd.DataFrame
    adjustment_scope: str
    restriction_scope: str

    @property
    def trading_days(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.raw_close.index)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _inside(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    path.relative_to(base.resolve())
    return path


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_descriptor(root: Path, release_id: str) -> tuple[Path, dict[str, Any]]:
    release_dir = _inside(root, "reports/data_releases")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(release_dir.glob("*.json")):
        document = _load_json(path)
        if document.get("data_release_id") == release_id:
            matches.append((path.resolve(), document))
    if not matches:
        raise FileNotFoundError(f"找不到 data_release_id={release_id!r} 的 descriptor")
    if len(matches) != 1:
        raise ValueError(f"data_release_id={release_id!r} 對應到多個 descriptors")
    return matches[0]


def _verify_declared_file(
    snapshot_path: Path,
    manifest: dict[str, Any],
    filename: str,
) -> tuple[Path, str]:
    declared = manifest.get("files", {}).get(filename)
    if not isinstance(declared, dict):
        raise ValueError(f"component manifest 未宣告 {filename}")
    path = _inside(snapshot_path, filename)
    if not path.is_file():
        raise FileNotFoundError(path)
    actual_bytes = path.stat().st_size
    actual_sha = sha256_file(path)
    if actual_bytes != declared.get("bytes") or actual_sha != declared.get("sha256"):
        raise ValueError(f"released input hash/bytes mismatch: {path}")
    return path, actual_sha


def verify_release_inputs(
    release_id: str,
    *,
    root: Path = ROOT,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Path], dict[str, str]]:
    """Verify descriptor metadata and every material file consumed by MOM-1."""
    root = root.resolve()
    descriptor_path, descriptor = _find_descriptor(root, release_id)
    if descriptor.get("data_release_id") != release_id:
        raise ValueError("descriptor data_release_id 與要求的 release 不一致")
    if "signal-count diagnostics without returns" not in descriptor.get("usage_policy", {}).get(
        "allowed", []
    ):
        raise ValueError("此 data release 未授權 signal-count diagnostics")

    components = {item["component_id"]: item for item in descriptor.get("components", [])}
    missing = set(REQUIRED_COMPONENT_FILES) - set(components)
    if missing:
        raise ValueError(f"release 缺少 MOM-1 必要 components: {sorted(missing)}")

    manifests: dict[str, dict[str, Any]] = {}
    input_paths: dict[str, Path] = {}
    input_hashes: dict[str, str] = {}
    for component_id, filenames in REQUIRED_COMPONENT_FILES.items():
        component = components[component_id]
        if not component.get("structural_passed"):
            raise ValueError(f"component structural gate 未通過: {component_id}")
        snapshot_path = _inside(root, component["snapshot_path"])
        manifest_path = _inside(snapshot_path, "manifest.json")
        quality_path = _inside(snapshot_path, "quality_report.json")
        if sha256_file(manifest_path) != component["manifest_sha256"]:
            raise ValueError(f"component manifest SHA mismatch: {component_id}")
        if sha256_file(quality_path) != component["quality_sha256"]:
            raise ValueError(f"component quality SHA mismatch: {component_id}")
        manifest = _load_json(manifest_path)
        if manifest.get("content_sha256") != component["content_sha256"]:
            raise ValueError(f"component content SHA mismatch: {component_id}")
        manifests[component_id] = manifest
        for filename in filenames:
            path, digest = _verify_declared_file(snapshot_path, manifest, filename)
            key = f"{component_id}/{filename}"
            input_paths[key] = path
            input_hashes[key] = digest
    input_hashes["release_descriptor"] = sha256_file(descriptor_path)
    return descriptor, manifests, input_paths, input_hashes


def build_backward_adjusted_close(
    raw_close: pd.DataFrame,
    corporate_actions: pd.DataFrame,
) -> pd.DataFrame:
    """Apply official event factors to dates strictly before each event.

    The resulting series is suitable for signal ratios only.  The released D3
    component remains blocked for execution-ledger and performance promotion.
    """
    required = {"stock_id", "event_date", "adjustment_factor"}
    missing = required - set(corporate_actions.columns)
    if missing:
        raise ValueError(f"corporate actions 缺少必要欄位: {sorted(missing)}")
    if raw_close.index.has_duplicates or raw_close.columns.has_duplicates:
        raise ValueError("raw_close 的交易日與 stock_id 必須唯一")
    if not all(isinstance(stock_id, str) for stock_id in raw_close.columns):
        raise ValueError("raw_close stock_id 欄名必須先正規化為字串")
    if not raw_close.index.is_monotonic_increasing:
        raise ValueError("raw_close 必須按交易日升冪排序")

    events = corporate_actions.loc[:, list(required)].copy()
    events["stock_id"] = events["stock_id"].astype(str)
    events["event_date"] = pd.to_datetime(events["event_date"], errors="coerce")
    events["adjustment_factor"] = pd.to_numeric(events["adjustment_factor"], errors="coerce")
    if events[["event_date", "adjustment_factor"]].isna().any().any():
        raise ValueError("corporate action event_date/factor 不可為空或無法解析")
    factors = events["adjustment_factor"].to_numpy(dtype=float)
    if not np.isfinite(factors).all() or (factors <= 0).any():
        raise ValueError("corporate action adjustment_factor 必須是有限正數")
    if events.duplicated(["stock_id", "event_date"]).any():
        raise ValueError("同一 stock_id/event_date 有多個調整因子，需先由 Data Authority 對帳")

    relevant = events[events["stock_id"].isin(map(str, raw_close.columns))]
    event_dates = pd.DatetimeIndex(relevant["event_date"].unique())
    calendar = raw_close.index.union(event_dates).sort_values()
    event_factor = pd.DataFrame(1.0, index=calendar, columns=raw_close.columns, dtype=float)
    for event in relevant.itertuples(index=False):
        event_factor.at[pd.Timestamp(event.event_date), str(event.stock_id)] = float(
            event.adjustment_factor
        )

    # At row t we need the product of factors whose event date is strictly > t.
    future_product = event_factor.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    adjusted = raw_close * future_product.reindex(raw_close.index)
    return adjusted.where(raw_close.notna())


def load_momentum_release(
    release_id: str = "tw_stock_data_2005_2014_r1",
    *,
    root: Path = ROOT,
) -> MomentumReleaseInputs:
    """Load and validate the named release without opening holdout returns."""
    root = root.resolve()
    descriptor, manifests, paths, input_hashes = verify_release_inputs(release_id, root=root)

    prices = pd.read_parquet(paths["twse_prices_2005_2014/prices.parquet"])
    required_price = {"stock_id", "trade_date", "open", "close", "volume", "turnover"}
    if required_price - set(prices.columns):
        raise ValueError(f"prices 缺少欄位: {sorted(required_price - set(prices.columns))}")
    prices = prices.loc[:, list(required_price)].copy()
    prices["stock_id"] = prices["stock_id"].astype(str)
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
    if prices[["stock_id", "trade_date"]].isna().any().any():
        raise ValueError("prices stock_id/trade_date 不可為空")
    if prices.duplicated(["stock_id", "trade_date"]).any():
        raise ValueError("prices (stock_id, trade_date) 必須唯一")

    price_manifest = manifests["twse_prices_2005_2014"]
    if price_manifest.get("units", {}).get("prices.volume") != "shares":
        raise ValueError("released prices.volume 單位必須明確宣告為 shares")

    raw_open = prices.pivot(index="trade_date", columns="stock_id", values="open")
    raw_close = prices.pivot(index="trade_date", columns="stock_id", values="close")
    volume_shares = prices.pivot(index="trade_date", columns="stock_id", values="volume")
    turnover = prices.pivot(index="trade_date", columns="stock_id", values="turnover")
    columns = sorted(map(str, raw_close.columns))
    raw_close = raw_close.reindex(columns=columns).sort_index()
    raw_open = raw_open.reindex(index=raw_close.index, columns=columns)
    volume_shares = volume_shares.reindex(index=raw_close.index, columns=columns)
    turnover = turnover.reindex(index=raw_close.index, columns=columns)

    actions = pd.read_parquet(
        paths["twse_corporate_actions_2005_2014/corporate_actions.parquet"]
    )
    adjusted_close = build_backward_adjusted_close(raw_close, actions)

    master = build_pit_master(
        pd.read_parquet(
            paths["twse_security_master_2005_2014/security_master_staging.parquet"]
        )
    )
    universe_history = pd.read_parquet(
        paths["twse_security_master_2005_2014/stock_universe_history.parquet"]
    )
    market_structure = pd.read_parquet(
        paths["twse_market_structure_2005_2014/market_structure_monthly.parquet"]
    )
    disposition_events = pd.read_parquet(
        paths["twse_disposition_punish_2005_2014/disposition_events.parquet"]
    )
    source_status = pd.read_parquet(
        paths["twse_disposition_punish_2005_2014/twse_disposition_source_status.parquet"]
    )
    expected_years = set(range(2005, 2015))
    ok_years = set(
        source_status.loc[source_status["official_stat"].eq("OK"), "year"].astype(int)
    )
    if ok_years != expected_years:
        raise ValueError(f"D6 official source coverage 不完整: {sorted(expected_years - ok_years)}")
    disposition_restricted = disposition_restriction_frame(
        disposition_events, raw_close.index, raw_close.columns
    )

    content_hashes = {
        component_id: manifests[component_id]["content_sha256"]
        for component_id in REQUIRED_COMPONENT_FILES
    }
    descriptor_path, _ = _find_descriptor(root, release_id)
    return MomentumReleaseInputs(
        release_id=release_id,
        descriptor_path=descriptor_path,
        descriptor_sha256=input_hashes["release_descriptor"],
        component_content_sha256=content_hashes,
        verified_input_sha256=input_hashes,
        raw_open=raw_open,
        raw_close=raw_close,
        volume_shares=volume_shares,
        adjusted_close=adjusted_close,
        turnover=turnover,
        pit_master=master,
        universe_history=universe_history,
        market_structure=market_structure,
        disposition_events=disposition_events,
        disposition_restricted=disposition_restricted,
        adjustment_scope="official D3 factors; signal-only; blocked for performance promotion",
        restriction_scope="TWSE punish/disposition only; stop-trading/full-delivery not yet complete",
    )
