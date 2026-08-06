"""Print this machine's research environment and data fingerprint.

Run it on every machine that produces backtest numbers and compare the output.
Backtest results are only reproducible within one environment: measured on
2026-08-06, the identical code, data and window gave 15.22%/237 trades under
pandas 2.2.3 and 14.07%/242 under both 2.3.3 and 3.0.5.

Deliberately dependency-light and read-only: no DB connection, no network, no
writes.  Safe to run on any machine, including one whose data you do not want to
touch.

    python scripts/machine_fingerprint.py
"""
from __future__ import annotations

import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _line(label: str, value) -> None:
    print(f"{label:24s} {value}")


def main() -> None:
    print("=" * 60)
    print("MACHINE FINGERPRINT")
    print("=" * 60)
    _line("project_dir", ROOT)
    _line("hostname", platform.node())
    _line("platform", platform.platform())
    _line("python", sys.version.split()[0])
    _line("python_exe", sys.executable)

    for name in ("pandas", "numpy", "scipy", "pyarrow"):
        try:
            module = __import__(name)
            _line(name, getattr(module, "__version__", "?"))
        except ImportError:
            _line(name, "NOT INSTALLED")

    print("-" * 60)
    print("GIT")
    print("-" * 60)
    for label, cmd in (("branch", "git rev-parse --abbrev-ref HEAD"),
                       ("commit", "git rev-parse --short HEAD"),
                       ("dirty_files", "git status --porcelain")):
        try:
            import subprocess
            out = subprocess.run(cmd.split(), cwd=ROOT, capture_output=True,
                                 text=True, timeout=30).stdout.strip()
            if label == "dirty_files":
                out = str(len(out.splitlines())) + " modified/untracked"
            _line(label, out or "(empty)")
        except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
            _line(label, f"(failed: {exc})")

    print("-" * 60)
    print("RESEARCH DATA (data/research parquet)")
    print("-" * 60)
    try:
        from agent.backtest import _load

        data = _load(parquet_dir=os.path.join(ROOT, "data", "research"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to load: {type(exc).__name__}: {exc}")
        return

    prices = data.get("prices")
    if prices is None or getattr(prices, "empty", True):
        print("prices empty or missing")
        return

    _line("price_rows", len(prices))
    _line("price_stocks", prices["stock_id"].nunique())
    _line("price_days", prices["trade_date"].nunique())
    _line("price_first", prices["trade_date"].min())
    _line("price_last", prices["trade_date"].max())
    per_day = prices.groupby("trade_date")["stock_id"].nunique()
    _line("median_stocks_per_day", per_day.median())

    for key, label in (("dividends", "dividend_events"),
                       ("inst", "institutional_rows"),
                       ("tech", "technical_rows"),
                       ("rev_map", "revenue_rows"),
                       ("margin", "margin_rows"),
                       ("imap", "industry_map_rows"),
                       ("stocks", "stock_meta_rows"),
                       # Delisted coverage decides whether the dataset carries
                       # survivorship bias; zero here makes every backtest optimistic.
                       ("delisted", "delisted_rows"),
                       ("disposition", "disposition_rows")):
        obj = data.get(key)
        if obj is None:
            _line(label, "None")
        else:
            try:
                _line(label, len(obj))
            except TypeError:
                _line(label, f"(unsized {type(obj).__name__})")

    print("=" * 60)
    print("Paste this whole block back for comparison.")


if __name__ == "__main__":
    main()
