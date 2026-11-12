"""scripts/test_order_check.py (item 293 of docs/ideas/IDEA-20261102-standing-improvements.md): seeded test shuffle."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import test_order_check as toc  # noqa: E402

IDS = [
    "tests/test_a.py::test_1",
    "tests/test_a.py::test_2",
    "tests/test_a.py::TestK::test_x",
    "tests/test_a.py::TestK::test_y[1]",
    "tests/test_a.py::TestK::test_y[2]",
    "tests/test_b.py::test_1[p]",
    "tests/test_b.py::test_1[q]",
    "tests/test_c.py::test_1",
]


def _items(ids):
    return [SimpleNamespace(nodeid=i) for i in ids]


def _order(seed, ids=IDS):
    return [i.nodeid for i in toc.shuffled(_items(ids), seed)]


def test_same_seed_same_order_whatever_the_input_order():
    assert _order(7) == _order(7, list(reversed(IDS)))
    assert sorted(_order(7)) == sorted(IDS)


def test_seeds_differ_and_modules_and_classes_stay_together():
    orders = {tuple(_order(s)) for s in range(20)}
    assert len(orders) > 5
    for order in orders:
        mods = [o.split("::")[0] for o in order]
        # each module is one contiguous run
        assert len([m for k, m in enumerate(mods) if k == 0 or mods[k - 1] != m]) == 3
        cls = [k for k, o in enumerate(order) if "::TestK::" in o]
        assert cls == list(range(cls[0], cls[0] + 3))


def _cfg(seed, lines):
    tr = SimpleNamespace(write_line=lines.append)
    return SimpleNamespace(getoption=lambda name: seed, pluginmanager=SimpleNamespace(get_plugin=lambda name: tr))


def test_hooks_leave_order_alone_without_seed():
    items, lines = _items(IDS), []
    cfg = _cfg(None, lines)
    toc.pytest_sessionstart(SimpleNamespace(config=cfg))
    toc.pytest_collection_modifyitems(None, cfg, items)
    assert [i.nodeid for i in items] == IDS
    assert lines == []
    cfg = _cfg(3, lines)
    toc.pytest_sessionstart(SimpleNamespace(config=cfg))
    toc.pytest_collection_modifyitems(None, cfg, items)
    assert [i.nodeid for i in items] == _order(3)
    assert lines == ["test order: shuffled, --order-seed 3", "test order: first 3 of 8 collected: " + ", ".join(_order(3)[:3])]


def test_build_command():
    cmd = toc.build_command(5, "auto", ["tests/test_x.py"])
    assert cmd[1:] == ["-m", "pytest", "-q", "-m", "not slow", "-p", "test_order_check", "--order-seed", "5", "-n", "auto"] + [
        "--dist",
        "worksteal",
        "tests/test_x.py",
    ]
    assert "-n" not in toc.build_command(5, "0", [])


SUITE = """
import os

def test_{n}_a():
    pass

def test_{n}_b():
    pass

class TestK:
    def test_x(self):
        pass

    def test_y(self):
        pass
"""


def _run_pytest(tmp_path, *args):
    for n in ("one", "two", "three"):
        (tmp_path / f"test_{n}.py").write_text(SUITE.format(n=n), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "scripts"), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "test_order_check", "-p", "no:cacheprovider", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_plugin_reorders_a_real_collection(tmp_path):
    res = _run_pytest(tmp_path, "--collect-only", "-q", "--order-seed", "11")
    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout.splitlines()
    got = [line for line in out if "::" in line and not line.startswith("test order:")]
    assert len(got) == 12
    assert got == [i.nodeid for i in toc.shuffled(_items(got), 11)]
    assert got != sorted(got)
    # the log of a -q run says the shuffle was on and how it starts (review of item 293, finding 1)
    assert "test order: shuffled, --order-seed 11" in out
    assert "test order: first 3 of 12 collected: " + ", ".join(got[:3]) in out


def test_plugin_runs_under_xdist(tmp_path):
    # every worker shuffles with the same seed, so xdist sees one collection (otherwise it errors out)
    res = _run_pytest(tmp_path, "-q", "-n", "2", "--order-seed", "4")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "12 passed" in res.stdout
    assert "test order: shuffled, --order-seed 4" in res.stdout.splitlines()


def test_no_order_lines_without_seed(tmp_path):
    res = _run_pytest(tmp_path, "-q")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "test order:" not in res.stdout
