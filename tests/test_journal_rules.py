"""scripts/journal_rules.py: what-if of the per-trade loss cap and the daily stop."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import journal_rules as jr  # noqa: E402

DAY = 1760000000  # one UTC day


def test_cap_and_daily_stop():
    rows = [
        (DAY, DAY + 10, "A", "long", -50.0),
        (DAY, DAY + 20, "B", "long", -300.0),
        (DAY, DAY + 30, "C", "long", 80.0),
        (DAY, DAY + 90000, "D", "short", 40.0),
    ]
    w = jr.what_if(rows)
    assert w["base"] == -230 and w["cap100"] == -30
    assert w["daily_stop"] == -310  # third trade that day skipped after two losses
    assert w["both"] == -110 and w["shorts"] == 40
