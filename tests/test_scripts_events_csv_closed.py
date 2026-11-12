"""The macro-events CSV readers in research scripts close their file (no ResourceWarning).

Standing card item 351: ``csv.DictReader(path.open())`` left the handle to the garbage collector, which
``pytest -W default`` reports as ``ResourceWarning: unclosed file``.
"""

from __future__ import annotations

import gc
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_event_straddle as ves  # noqa: E402
import verify_pre_event_vol as vpe  # noqa: E402
import verify_weekend_vol as vwv  # noqa: E402

LOADERS = {
    "verify_weekend_vol": lambda: vwv.load_events(),
    "verify_event_straddle": lambda: ves.load_events(ves.EVENTS_CSV),
    "verify_pre_event_vol": lambda: vpe.load_events(),
}


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_load_events_closes_the_csv(name):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = LOADERS[name]()
        gc.collect()
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, [str(w.message) for w in leaks]
    assert out and all(out)  # the real events file still parses into non-empty parts
