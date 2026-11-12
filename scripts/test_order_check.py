"""Run the test suite in a shuffled order to find tests that depend on the order they run in.

Item 293 of docs/ideas/IDEA-20261102-standing-improvements.md (BACKLOG 6.37): pytest-randomly is not a dependency,
so this file is both the runner and a tiny pytest plugin (no new package, no network).

Plugin: loaded with ``-p test_order_check`` (``scripts/`` on ``PYTHONPATH``) and ``--order-seed N``. The collected items
are sorted by node id and shuffled with ``random.Random(N)`` in three levels, as pytest-randomly does: the order of
modules, then of classes and top-level functions inside a module, then of the methods inside a class. Keeping a module
together keeps module-scoped fixtures cheap. Without ``--order-seed`` the order is left alone. Under pytest-xdist every
worker collects and shuffles with the same seed, so all workers see the same list (xdist requires that) and the
scheduler hands out the shuffled order: tests meet different neighbours in each worker process. With a seed the plugin
writes ``test order: shuffled, --order-seed N`` to the terminal even under ``-q`` (the report header is not printed
there), and in one process also the first node ids of the shuffled collection, so the log shows the shuffle was on.

Runner:

    uv run --extra dev python scripts/test_order_check.py --seed 1 [--workers auto|0|N] [--log PATH] [-- PYTEST ARGS]

runs ``pytest -q -m "not slow" -p test_order_check --order-seed SEED`` (with ``-n WORKERS --dist worksteal`` unless
``--workers 0``) from the repository root, writes pytest's output to ``--log`` (default
``~/.cache/tradedesk-agents/tmp/test-order-SEED.log``) and prints the last lines. Exit code is pytest's. A test that
fails here but passes in ``make test`` and alone (``uv run pytest -q NODEID``) depends on order; rerun with the same seed.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PLUGIN = "test_order_check"
TAIL_LINES = 15
FIRST_IDS = 3


def shuffled(items: Sequence[Any], seed: int) -> list[Any]:
    """Items (anything with ``nodeid``) shuffled by module, then by class / top-level function, then inside a class."""
    rng = random.Random(seed)
    modules: dict[str, dict[str, list[Any]]] = {}
    for it in sorted(items, key=lambda i: i.nodeid):
        parts = it.nodeid.split("::")
        # "mod.py::Class::test[x]" groups under Class; "mod.py::test[x]" is its own block
        block = parts[1] if len(parts) > 2 else it.nodeid
        modules.setdefault(parts[0], {}).setdefault(block, []).append(it)
    mods = list(modules.values())
    rng.shuffle(mods)
    out: list[Any] = []
    for blocks in mods:
        groups = list(blocks.values())
        rng.shuffle(groups)
        for g in groups:
            rng.shuffle(g)
            out.extend(g)
    return out


# --- pytest plugin hooks (active only when loaded with -p test_order_check) ---


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--order-seed", type=int, default=None, help="shuffle collected tests with this seed (scripts/test_order_check.py)")


def order_line(config: Any) -> str | None:
    seed = config.getoption("--order-seed")
    return None if seed is None else f"test order: shuffled, --order-seed {seed}"


def _write(config: Any, line: str) -> None:
    # pytest_report_header is dropped under -q (the runner's mode), so the log would not show the shuffle was on
    tr = config.pluginmanager.get_plugin("terminalreporter")
    if tr is not None:
        tr.write_line(line)


def pytest_sessionstart(session: Any) -> None:
    line = order_line(session.config)
    if line is not None:
        _write(session.config, line)


def pytest_collection_modifyitems(session: Any, config: Any, items: list[Any]) -> None:
    seed = config.getoption("--order-seed")
    if seed is not None:
        items[:] = shuffled(items, seed)
        # one process only: under xdist the workers shuffle and their output does not reach the log
        if items:
            head = ", ".join(i.nodeid for i in items[:FIRST_IDS])
            _write(config, f"test order: first {min(FIRST_IDS, len(items))} of {len(items)} collected: {head}")


# --- runner (no pytest_ prefix below: pytest would reject an unknown hook in this plugin) ---


def build_command(seed: int, workers: str, extra: Sequence[str]) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "-q", "-m", "not slow", "-p", PLUGIN, "--order-seed", str(seed)]
    if workers != "0":
        cmd += ["-n", workers, "--dist", "worksteal"]
    return cmd + list(extra)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the test suite in a seeded shuffled order.")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--workers", default="auto", help="pytest-xdist -n value; 0 runs in one process (default auto)")
    ap.add_argument("--log", type=Path, default=None, help="pytest output file (default ~/.cache/tradedesk-agents/tmp/test-order-SEED.log)")
    ap.add_argument("extra", nargs="*", help="extra pytest arguments after --")
    args = ap.parse_args(argv)
    log = args.log or Path.home() / ".cache/tradedesk-agents/tmp" / f"test-order-{args.seed}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SCRIPTS), env.get("PYTHONPATH", "")) if p)
    cmd = build_command(args.seed, args.workers, args.extra)
    with log.open("w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.flush()
        rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, check=False).returncode
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-TAIL_LINES:]))
    print(f"seed {args.seed}, workers {args.workers}: exit {rc}; log {log}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
