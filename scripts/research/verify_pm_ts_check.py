"""Timestamp (lookahead) check of CLOB ``/prices-history`` points against data-api ``/trades`` prints.

Part of IDEA-20261015-polymarket-yes-calibration-all-categories (triage condition 7). Kept as a thin alias: since
2026-10-21 the check lives in ``scripts/verify_pm_yes_calibration.py --stage ts_check`` (one implementation; it also
paginates trades, reaches t0 + 5 min for the k > 0 shifts and verifies empty ``/trades?end=t0`` answers).

Usage:
    uv run python scripts/research/verify_pm_ts_check.py --out ~/.cache/tradedesk-agents/pm_yes_calibration/ts_check_v2.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The implementation stays in scripts/ (one level up from scripts/research/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verify_pm_yes_calibration import DEFAULT_CACHE, stage_ts_check  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--rate", type=float, default=4.0)
    a = ap.parse_args()
    stage_ts_check(Path(a.cache_dir).expanduser(), a.out, min(a.rate, 5.0), n_pick=a.n)


if __name__ == "__main__":
    main()
