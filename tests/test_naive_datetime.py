"""Guard: no naive (machine-local) time in src/tradedesk/ and scripts/.

`datetime.now()`, `datetime.utcnow()`, `datetime.utcfromtimestamp(ts)`, `datetime.fromtimestamp(ts)` without `tz` and
`date.today()` read the clock and time zone of whatever machine runs the code (Contabo, a Mac), so option T, UTC-day
boundaries and forward windows shift silently (invariant 4 in CLAUDE.md). The check is a pure function over source text
(stdlib `ast`; the scanned modules are not imported; no network).

Not flagged: calls that pass a time zone (`datetime.now(UTC)`, `fromtimestamp(ts, UTC)`, `tz=...`), the runtime clock
(`rt.now()`), and bare helpers such as `core.envelope.utcnow()`, which is tz-aware.
"""

from __future__ import annotations

import ast
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Always naive on the `datetime` class, whatever the arguments.
_ALWAYS_NAIVE = {"utcnow", "utcfromtimestamp", "today"}


def _aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Names bound to the `datetime` module, the `datetime` class and the `date` class in one module."""
    modules: set[str] = set()
    dt_classes, date_classes = {"datetime"}, {"date"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "datetime"}
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            dt_classes |= {a.asname or a.name for a in node.names if a.name == "datetime"}
            date_classes |= {a.asname or a.name for a in node.names if a.name == "date"}
    return modules, dt_classes - modules, date_classes


def _receiver(value: ast.expr, modules: set[str], dt_classes: set[str], date_classes: set[str]) -> str | None:
    """'datetime' / 'date' when `value` is that class (`datetime`, an alias, `mod.datetime`), else None."""
    if isinstance(value, ast.Name):
        return "datetime" if value.id in dt_classes else "date" if value.id in date_classes else None
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id in modules:
        return value.attr if value.attr in ("datetime", "date") else None
    return None


def _no_tz(given: list[ast.expr]) -> bool:
    return all(isinstance(g, ast.Constant) and g.value is None for g in given)


def _is_naive(kind: str, method: str, call: ast.Call) -> bool:
    if kind == "date":
        return method == "today"
    if method in _ALWAYS_NAIVE:
        return True
    tz = [k.value for k in call.keywords if k.arg == "tz"]
    if method == "now":  # now(tz=None)
        return _no_tz(call.args[:1] + tz)
    if method == "fromtimestamp":  # fromtimestamp(ts, tz=None)
        return _no_tz(call.args[1:2] + tz)
    return False


# A hit needs the identifier `date` or `datetime` (class, module or the `from datetime import` an alias comes from) and
# one of the method names below; source without both is not parsed (parsing and walking ~9 MB is the scan's cost).
# Python NFKC-normalises identifiers (a full-width `now` is `now`), so source with a character whose NFKC form has
# ASCII letters, digits or `_` is always parsed; Cyrillic text, emoji and dashes do not count.
_HAS_CLASS = re.compile(r"\b(?:date|datetime)\b")
_HAS_METHOD = re.compile(r"\b(?:now|utcnow|utcfromtimestamp|fromtimestamp|today)\b")


def _may_spell_ascii_names(source: str) -> bool:
    return any(
        not c.isascii() and any(n.isascii() and (n.isalnum() or n == "_") for n in unicodedata.normalize("NFKC", c)) for c in set(source)
    )


def _cannot_hit(source: str) -> bool:
    if _HAS_CLASS.search(source) and _HAS_METHOD.search(source):
        return False
    return not _may_spell_ascii_names(source)


def naive_time_calls(source: str) -> list[str]:
    """`"<line>: <class>.<method>()"` for every naive-time call in `source`, in line order; [] if none."""
    if _cannot_hit(source):
        return []
    tree = ast.parse(source)
    imports, calls = [], []  # one walk over the tree (it is most of the scan time), aliases resolved afterwards
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.append(node)
    modules, dt_classes, date_classes = _aliases(ast.Module(body=imports, type_ignores=[]))
    out = []
    for node in calls:
        kind = _receiver(node.func.value, modules, dt_classes, date_classes)
        if kind and _is_naive(kind, node.func.attr, node):
            out.append((node.lineno, f"{node.lineno}: {kind}.{node.func.attr}()"))
    return [s for _, s in sorted(out)]


def unexplained(found: dict[str, list[str]], allowed: dict[str, tuple[int, str]]) -> list[str]:
    """Problems: a file with naive calls that is not allowed, or whose count differs from its allowed count."""
    problems = [f"{path}: {hits}" for path, hits in found.items() if path not in allowed]
    problems += [
        f"{path}: {len(found.get(path, []))} naive calls, ALLOWED says {n}"
        for path, (n, _) in allowed.items()
        if len(found.get(path, [])) != n
    ]
    return problems


def scan(root: Path = ROOT) -> dict[str, list[str]]:
    """Naive-time calls per file (path relative to `root`) in src/tradedesk/**/*.py and scripts/*.py."""
    paths = sorted((root / "src" / "tradedesk").rglob("*.py")) + sorted((root / "scripts").glob("*.py"))
    found = {}
    for path in paths:
        hits = naive_time_calls(path.read_text(encoding="utf-8"))
        if hits:
            found[path.relative_to(root).as_posix()] = hits
    return found


# Repo path -> (number of naive calls, why machine-local time is intended there). Nothing in src/ belongs here.
ALLOWED: dict[str, tuple[int, str]] = {
    "scripts/agents_status.py": (
        7,
        "owner-facing Telegram status in the server's wall clock; history.tsv stamps it compares against are written by "
        "nightly_agents.sh with local `date +%Y-%m-%d_%H%M%S`, so the cut-offs must be local too",
    ),
    "scripts/dashboard.py": (
        3,
        "two cut-offs over history.tsv (local `date` stamps of nightly_agents.sh) and the page's 'updated at' / usage "
        "ETA labels in the server's wall clock; market times on the page go through kyiv() with UTC timestamps",
    ),
    "scripts/results_board.py": (
        1,
        "the 'generated on' date printed in docs/RESULTS.md: the calendar day of whoever runs the script, no numbers",
    ),
    "scripts/tg_bridge.py": (
        2,
        "its own log-line timestamps and the file-name stamp of downloaded Telegram images: local wall clock, no numbers",
    ),
}


def test_no_naive_time_outside_allowed():
    problems = unexplained(scan(), ALLOWED)
    assert problems == [], "naive time: pass a tz (datetime.now(UTC), fromtimestamp(ts, UTC)) or use rt.now()"
    assert all(reason.strip() for _, reason in ALLOWED.values())
    assert not any(path.startswith("src/") for path in ALLOWED)


_FIXTURE = """\
import datetime as dtm
from datetime import UTC, date, datetime, timezone
from datetime import datetime as DT
from tradedesk.core.envelope import utcnow

ok = [
    datetime.now(UTC),
    datetime.now(tz=timezone.utc),
    datetime.fromtimestamp(ts, UTC),
    datetime.fromtimestamp(ts / 1000, tz=UTC),
    dtm.datetime.now(dtm.UTC),
    DT.fromtimestamp(ts, timezone.utc),
    rt.now(),
    self.now(),
    utcnow(),
    datetime.strptime(s, "%Y").replace(tzinfo=UTC),
    date.fromisoformat(s),
]
bad_now = datetime.now()
bad_none = datetime.now(None)
bad_kw_none = datetime.now(tz=None)
bad_utcnow = datetime.utcnow()
bad_utcfrom = datetime.utcfromtimestamp(0)
bad_from = datetime.fromtimestamp(ts)
bad_today = date.today()
bad_dt_today = datetime.today()
bad_module = dtm.datetime.now()
bad_module_date = dtm.date.today()
bad_alias = DT.fromtimestamp(ts)
"""


def test_detector_on_fixture():
    hits = naive_time_calls(_FIXTURE)
    assert [h.split(": ", 1)[1] for h in hits] == [
        "datetime.now()",
        "datetime.now()",
        "datetime.now()",
        "datetime.utcnow()",
        "datetime.utcfromtimestamp()",
        "datetime.fromtimestamp()",
        "date.today()",
        "datetime.today()",
        "datetime.now()",
        "date.today()",
        "datetime.fromtimestamp()",
    ]
    first_bad = _FIXTURE.splitlines().index("bad_now = datetime.now()") + 1
    assert int(hits[0].split(":", 1)[0]) == first_bad
    assert naive_time_calls("from datetime import UTC, datetime\nx = datetime.now(UTC)\n") == []
    # The text pre-filter: full-width `today` (NFKC -> `today`) is not matched by its regex, yet the call is found.
    fullwidth = "from datetime import date\nd = date.ｔｏｄａｙ()\n"
    assert not _HAS_METHOD.search(fullwidth) and naive_time_calls(fullwidth) == ["2: date.today()"]
    assert _cannot_hit("import os\nx = os.now()\n") and not _cannot_hit("import datetime as d\nx = d.datetime.now()\n")
    assert _cannot_hit("import os\nx = os.now()  # Київ — 🙂\n")  # non-ASCII that cannot spell an identifier


def test_allowed_file_and_counts(tmp_path):
    (tmp_path / "src" / "tradedesk").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "tradedesk" / "good.py").write_text("from datetime import UTC, datetime\nx = datetime.now(UTC)\n")
    (tmp_path / "src" / "tradedesk" / "bad.py").write_text("from datetime import datetime\nx = datetime.now()\n")
    (tmp_path / "scripts" / "local.py").write_text("from datetime import date\nd = date.today()\n")
    found = scan(tmp_path)
    assert found == {"scripts/local.py": ["2: date.today()"], "src/tradedesk/bad.py": ["2: datetime.now()"]}

    allowed = {"scripts/local.py": (1, "owner's calendar day")}
    assert unexplained(found, allowed) == ["src/tradedesk/bad.py: ['2: datetime.now()']"]
    assert unexplained({"scripts/local.py": found["scripts/local.py"]}, allowed) == []
    # A second naive call in an allowed file, or a stale entry, is caught too.
    assert unexplained({"scripts/local.py": ["2: date.today()", "3: date.today()"]}, allowed) == [
        "scripts/local.py: 2 naive calls, ALLOWED says 1"
    ]
    assert unexplained({}, allowed) == ["scripts/local.py: 0 naive calls, ALLOWED says 1"]
