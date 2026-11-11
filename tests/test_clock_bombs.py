"""Guard: tests that read the real clock next to hard-coded late-2026 dates ("clock bombs").

A test that compares the real `now` with a fixed date (an option expiry `27NOV26`, a fixture day `2026-12-04`) passes
today and turns red on the day the date passes, breaking `make test` and the nightly pipeline (BACKLOG 6.26:
`test_my_options` would have failed on 27.11). The check is a pure function over source text (stdlib `ast`; the scanned
modules are not imported; no network).

A file is a suspect when it has both:
- a real-clock read: `datetime.now(...)` / `utcnow()` / `today()`, `date.today()`, `time.time()` (aliases resolved),
  a bare `utcnow()` (core.envelope), or a `Runtime(...)` / `build_server()` built in a function that never pins a clock
  there (`x.now = ...`, `setattr(x, "now" | "_now", ...)`, `monkeypatch.setattr(x, "now" | "_now", ...)`);
- a fixed date in Nov/Dec 2026: a string with `NOV26` / `DEC26` / `2026-11-` / `2026-12-`, or
  `datetime(2026, 11|12, ...)` / `date(2026, 11|12, ...)`.

Every suspect is reviewed by hand and listed in REVIEWED with the number of its clock reads and the verdict, so a new
unpinned clock read in such a file (or a new suspect file) fails here and gets looked at.

Not detected: a real clock read inside the code under test reached through a script or src function that defaults
`now` to the wall clock (the original `test_my_options` case: `bybit_options_positions._now`; also
`pm_gap_paper_report.build_verdict`, fixed on 07.11), dates written as epoch seconds/milliseconds, and dates after
2026. Those need reading the test; the guard only narrows where to look.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_LATE_2026 = re.compile(r"\d{1,2}(?:NOV|DEC)26\b|2026-1[12]-")
# Text pre-filter: a source without one of these cannot hold a late-2026 date (strings or `(2026, 11|12` calls), so it
# is not parsed (parsing and walking the trees is the scan's cost).
_MAY_HAVE_DATE = re.compile(r"(?:NOV|DEC)26|2026-1[12]-|2026,\s*1[12]\b")
_CLOCK_METHODS = {"now", "utcnow", "today"}
_PIN_NAMES = {"now", "_now"}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _call_name(node: ast.Call) -> str | None:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)


def _is_late_2026_call(node: ast.Call) -> bool:
    """`datetime(2026, 11|12, ...)` / `date(2026, 11|12, ...)` (any receiver: `dt.datetime(...)` too)."""
    if _call_name(node) not in ("datetime", "date") or len(node.args) < 2:
        return False
    year, month = node.args[0], node.args[1]
    return all(isinstance(a, ast.Constant) for a in (year, month)) and year.value == 2026 and month.value in (11, 12)


def _aliases(imports: Iterable[ast.Import | ast.ImportFrom]) -> tuple[set[str], set[str], set[str]]:
    """Names bound to the `datetime` / `date` classes, to the `time` module, and bare clock functions."""
    classes, time_modules, bare = {"datetime", "date"}, {"time"}, {"utcnow"}
    for node in imports:
        if isinstance(node, ast.Import):
            time_modules |= {a.asname or a.name for a in node.names if a.name == "time"}
        elif node.module == "datetime":
            classes |= {a.asname or a.name for a in node.names if a.name in ("datetime", "date")}
        elif node.module == "time":
            bare |= {a.asname or a.name for a in node.names if a.name == "time"}
    return classes, time_modules, bare


def _clock_read(node: ast.Call, classes: set[str], time_modules: set[str], bare: set[str]) -> str | None:
    f = node.func
    if isinstance(f, ast.Name) and f.id in bare:
        return f"{f.id}()"
    if not isinstance(f, ast.Attribute):
        return None
    if isinstance(f.value, ast.Name) and f.value.id in time_modules and f.attr == "time":
        return f"{f.value.id}.time()"
    # `datetime.now()`, an alias `DT.now()`, or through the module: `dtm.datetime.now()`
    recv = f.value.id if isinstance(f.value, ast.Name) else f.value.attr if isinstance(f.value, ast.Attribute) else None
    if f.attr in _CLOCK_METHODS and recv in classes:
        return f"{recv}.{f.attr}()"
    return None


def _builds_runtime(node: ast.Call) -> str | None:
    name = _call_name(node)
    if name == "Runtime":
        return "Runtime()"
    if name == "build_server" and not node.args and not node.keywords:
        return "build_server()"
    return None


def _is_pin(node: ast.AST) -> bool:
    """`x.now = ...` / `x._now: T = ...`, or `setattr(x, "now" | "_now", ...)` (monkeypatch.setattr too)."""
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(isinstance(t, ast.Attribute) and t.attr in _PIN_NAMES for t in targets)
    return (
        isinstance(node, ast.Call)
        and _call_name(node) == "setattr"
        and any(isinstance(a, ast.Constant) and a.value in _PIN_NAMES for a in node.args[1:2])
    )


def clock_bomb_hits(source: str) -> tuple[list[str], list[str]]:
    """(real-clock reads, late-2026 dates) in `source`, each `"<line>: <what>"` in source order.

    One walk over the tree; every function body (nested ones on their own) is a scope, the rest of the module another.
    A `Runtime(...)` / `build_server()` counts as a clock read only in a scope that pins no clock."""
    if not _MAY_HAVE_DATE.search(source):
        return [], []
    tree = ast.parse(source)
    imports, calls, dates, pinned = [], [], [], set()
    stack: list[tuple[ast.AST, int]] = [(tree, 0)]
    n_scopes = 0
    while stack:
        node, scope = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (m := _LATE_2026.search(node.value)):
            dates.append(((node.lineno, node.col_offset), f"{node.lineno}: {m.group(0)}"))
        elif isinstance(node, ast.Call):
            calls.append((node, scope))
            if _is_late_2026_call(node):
                what = f"{ast.unparse(node.func)}(2026, {node.args[1].value}, ...)"
                dates.append(((node.lineno, node.col_offset), f"{node.lineno}: {what}"))
        if _is_pin(node):
            pinned.add(scope)
        if isinstance(node, _FUNCS):
            n_scopes += 1
            scope = n_scopes
        stack.extend((child, scope) for child in ast.iter_child_nodes(node))
    aliases = _aliases(imports)
    clocks = []
    for node, scope in calls:
        what = _clock_read(node, *aliases)
        if what is None and scope not in pinned and (built := _builds_runtime(node)):
            what = f"{built} without a pinned clock"
        if what:
            clocks.append(((node.lineno, node.col_offset), f"{node.lineno}: {what}"))
    return [s for _, s in sorted(clocks)], [s for _, s in sorted(dates)]


def scan(root: Path = ROOT) -> dict[str, list[str]]:
    """Suspect files (path relative to `root`) in tests/*.py -> their real-clock reads; only files with late-2026 dates."""
    found = {}
    for path in sorted((root / "tests").glob("*.py")):
        clocks, dates = clock_bomb_hits(path.read_text(encoding="utf-8"))
        if clocks and dates:
            found[path.relative_to(root).as_posix()] = clocks
    return found


def unexplained(found: dict[str, list[str]], reviewed: dict[str, tuple[int, str]]) -> list[str]:
    """Problems: a suspect file not reviewed, or one whose clock-read count differs from the reviewed count."""
    problems = [f"{path}: {hits}" for path, hits in found.items() if path not in reviewed]
    problems += [
        f"{path}: {len(found.get(path, []))} clock reads, REVIEWED says {n}"
        for path, (n, _) in reviewed.items()
        if len(found.get(path, [])) != n
    ]
    return problems


# Repo path -> (number of clock reads the scan finds, why the real clock cannot meet a late-2026 date there).
REVIEWED: dict[str, tuple[int, str]] = {
    "tests/test_assistant_paper.py": (1, "test_no_store_configured: no store, both tools fail before any date is read"),
    "tests/test_assistant_paper_job.py": (2, "the two Runtimes only build the scheduler's job list; no tool, no clock read"),
    "tests/test_claude_auth.py": (
        2,
        "wrapper tests: the subprocess reads the real clock, so the login end is real now +/- days (relative)",
    ),
    "tests/test_contract_risk.py": (2, "GoPlus provider calls on a MockTransport (cache and health); no date logic"),
    "tests/test_dashboard.py": (1, "read_usage gets time.time() and offsets from it: cache ages are relative, no fixed date"),
    "tests/test_forecast_log.py": (1, "test_tool_without_store: the tool answers 'no store' before reading the clock"),
    "tests/test_option_legs.py": (1, "_live_rt: scan_option_legs is called with the fixture's LIVE_NOW, not rt.now()"),
    "tests/test_radar.py": (1, "test_job_needs_a_store: the job raises on the missing store before rt.now()"),
    "tests/test_stablecoin_supply.py": (
        2,
        "_runtime is pinned by its caller make_server (rt.now = NOW); test_provider_slims_payloads reads the provider only",
    ),
}


def test_no_unreviewed_clock_bombs():
    problems = unexplained(scan(), REVIEWED)
    assert problems == [], "real clock next to a late-2026 date: pin the clock (rt.now = lambda: NOW) or review it here"
    assert all(reason.strip() for _, reason in REVIEWED.values())


_FIXTURE = """\
import datetime as dtm
import time
import time as _time
from datetime import UTC, date, datetime
from datetime import datetime as DT
from time import time as wall
from tradedesk.core.envelope import utcnow
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

EXPIRY = "BTC-27NOV26-81000-C"
DAY = "2026-12-04"
OLD = "BTC-26SEP26-1-C", "2026-10-31", "2027-11-01"
FIXED = datetime(2026, 11, 27, 8, tzinfo=UTC)
NOT_LATE = datetime(2026, 10, 1), date(2027, 11, 1)


def test_reads():
    a = datetime.now(UTC)
    b = date.today()
    c = time.time()
    d = utcnow()
    e = rt.now(), time.monotonic(), datetime.fromisoformat("2026-01-01")
    f = _time.time(), wall(), DT.utcnow(), dtm.datetime.today()


def test_unpinned():
    rt = Runtime()
    srv, _ = build_server()


def test_pinned(monkeypatch):
    rt = Runtime()
    rt.now = lambda: FIXED

    def inner():
        return Runtime()

    srv, _ = build_server(rt)


def test_pinned_by_setattr(monkeypatch):
    rt = Runtime()
    monkeypatch.setattr(mod, "_now", lambda: FIXED)


class TestGroup:
    def test_method(self):
        return Runtime()
"""


def test_detector_on_fixture():
    clocks, dates = clock_bomb_hits(_FIXTURE)
    assert [h.split(": ", 1)[1] for h in dates] == ["27NOV26", "2026-12-", "datetime(2026, 11, ...)"]
    assert [h.split(": ", 1)[1] for h in clocks] == [
        "datetime.now()",
        "date.today()",
        "time.time()",
        "utcnow()",
        "_time.time()",
        "wall()",
        "DT.utcnow()",
        "datetime.today()",
        "Runtime() without a pinned clock",
        "build_server() without a pinned clock",
        "Runtime() without a pinned clock",  # `inner` is its own scope: the outer pin does not reach it
        "Runtime() without a pinned clock",  # a method is a scope too
    ]
    lines = _FIXTURE.splitlines()
    assert int(clocks[0].split(":", 1)[0]) == lines.index("    a = datetime.now(UTC)") + 1
    assert int(clocks[-2].split(":", 1)[0]) == lines.index("        return Runtime()") + 1
    assert clock_bomb_hits("x = 1\n") == ([], [])
    # the text pre-filter: no late-2026 date, no parse (a syntax error would raise otherwise)
    assert clock_bomb_hits("import time\nx = time.time(\n") == ([], [])


def test_scan_and_review(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bomb.py").write_text("from datetime import UTC, datetime\nE = '2026-11-27'\nx = datetime.now(UTC)\n")
    (tmp_path / "tests" / "test_clock_only.py").write_text("import time\nx = time.time()\n")
    (tmp_path / "tests" / "test_date_only.py").write_text("E = 'BTC-27NOV26-1-C'\n")
    found = scan(tmp_path)
    assert found == {"tests/test_bomb.py": ["3: datetime.now()"]}

    reviewed = {"tests/test_bomb.py": (1, "x is only logged")}
    assert unexplained(found, reviewed) == []
    assert unexplained(found, {}) == ["tests/test_bomb.py: ['3: datetime.now()']"]
    assert unexplained({}, reviewed) == ["tests/test_bomb.py: 0 clock reads, REVIEWED says 1"]
    assert unexplained({"tests/test_bomb.py": ["3: datetime.now()", "4: time.time()"]}, reviewed) == [
        "tests/test_bomb.py: 2 clock reads, REVIEWED says 1"
    ]
