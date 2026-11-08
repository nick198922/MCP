"""The demo bands are written twice: in docs/CHECKPOINT-2026-11-30.md (section «Таблиця», registered before the review)
and in ``analytics.checkpoint.DEMO`` (the report's constants). A silent drift would give a wrong «узгоджується» / «ні» in
the one report of the review, so the n 30 and n 100 pairs (i.i.d. and rolling) of the table are parsed and compared with
the code. No network. A pair the document does not give (S12 n 100 «не пораховано») is skipped explicitly and listed,
so a new gap cannot pass unnoticed. A mismatch is not fixed here: the document's numbers are registered (item 249)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from tradedesk.analytics.checkpoint import DEMO, Band, DemoSpec

DOC = Path(__file__).resolve().parent.parent / "docs/CHECKPOINT-2026-11-30.md"
TOL = 0.0005
NS = (30, 100)  # the n the main table gives per demo setup (n 50 lives in «Смуги n 50 / 100», not checked here)

_NUM = r"([−+-]?\d+\.\d+)"
_PAIR = rf"i\.i\.d\. {_NUM} … {_NUM}, ковзна {_NUM} … {_NUM}"
_BAND = {
    30: re.compile(rf"(?:\bn 30|\b30 заповнених): {_PAIR}"),  # S28 / S17 say «30 заповнених»; «30 розміщень» is another band
    100: re.compile(rf"\bn 100: {_PAIR}"),
}
_SKIP = {100: re.compile(r"\bn 100 не пораховано")}
_ROW = re.compile(r"^\| Демо (S\d+)\b[^|]*\|[^|]*\|([^|]*)\|", re.MULTILINE)


def _f(s: str) -> float:
    return float(s.replace("−", "-"))


def table_section(text: str) -> str:
    """The text of «### Таблиця» up to the next heading of the same or a higher level."""
    m = re.search(r"^### Таблиця\s*$(.*?)(?=^#{1,3} )", text, re.MULTILINE | re.DOTALL)
    assert m, "section «### Таблиця» not found in the CHECKPOINT document"
    return m.group(1)


def parse_doc(text: str) -> tuple[dict[str, dict[int, Band]], list[str], list[str]]:
    """(setup -> {n: ((iid lo, hi), (rolling lo, hi))}, explicit skips «S12 n 100», problems) from the demo rows."""
    bands: dict[str, dict[int, Band]] = {}
    skips: list[str] = []
    problems: list[str] = []
    for m in _ROW.finditer(table_section(text)):
        setup, cell = m.group(1), m.group(2)
        if setup in bands:
            problems.append(f"{setup}: more than one table row")
            continue
        bands[setup] = {}
        for n in NS:
            found = _BAND[n].findall(cell)
            if len(found) == 1:
                a, b, c, d = (_f(x) for x in found[0])
                bands[setup][n] = ((a, b), (c, d))
            elif len(found) > 1:
                problems.append(f"{setup} n {n}: {len(found)} i.i.d. / rolling pairs in the cell, expected one")
            elif n in _SKIP and _SKIP[n].search(cell):
                skips.append(f"{setup} n {n}")
            else:
                problems.append(f"{setup} n {n}: no i.i.d. / rolling pair and no explicit «не пораховано»")
    return bands, skips, problems


def compare(doc: dict[str, dict[int, Band]], specs: Iterable[DemoSpec], tol: float = TOL) -> list[str]:
    """Mismatches between the parsed table and the code bands, one readable line each."""
    out: list[str] = []
    specs = list(specs)
    for s in specs:
        if s.setup not in doc:
            out.append(f"{s.setup}: in DEMO, no row «Демо {s.setup}» in the table")
            continue
        for n in NS:
            got, want = s.bands.get(n), doc[s.setup].get(n)
            if want is None:
                if got is not None:
                    out.append(f"{s.setup} n {n}: band in the code, none in the document")
                continue
            if got is None:
                out.append(f"{s.setup} n {n}: band in the document, none in the code")
                continue
            for kind, g, w in (("i.i.d.", got[0], want[0]), ("ковзна", got[1], want[1])):
                for end, gv, wv in (("p5", g[0], w[0]), ("p95", g[1], w[1])):
                    if abs(gv - wv) > tol:
                        out.append(f"{s.setup} n {n} {kind} {end}: code {gv:+.3f}, document {wv:+.3f}")
    out += [f"{k}: row in the table, not in DEMO" for k in sorted(set(doc) - {s.setup for s in specs})]
    return out


# ------------------------------------------------------------------------------------------- the real document


def test_demo_bands_in_code_match_the_checkpoint_table():
    doc, skips, problems = parse_doc(DOC.read_text(encoding="utf-8"))
    assert problems == []
    assert sorted(doc) == sorted(s.setup for s in DEMO) == sorted(["S3", "S12", "S28", "S17", "S14", "S16", "S38", "S39", "S40"])
    # the only pair the document does not give; a new one must be added here on purpose
    assert skips == ["S12 n 100"]
    assert compare(doc, DEMO) == []


def test_s28_n30_is_the_filled_band_not_the_placements_one():
    doc, _, _ = parse_doc(DOC.read_text(encoding="utf-8"))
    (iid_lo, iid_hi), _rolling = doc["S28"][30]
    assert abs(iid_lo - -0.438) < 1e-9 and abs(iid_hi - 0.247) < 1e-9  # not «30 розміщень»: −0.333 … +0.218


# ------------------------------------------------------------------------------------------------- fixtures

_FIX = """# x

### Таблиця

| Форвард | Що порівнюємо | Смуга бектесту (джерело) | Мінімум n | «ні» |
|---|---|---|---|---|
| Демо S90 (4h) | середній R | n 30: i.i.d. −0.473 … +0.361, ковзна −0.719 … +0.652 ([п](x.md)); **n 100: i.i.d. −0.299 … +0.157, ковзна −0.418 … +0.307** | 30 | x |
| Демо S91 (лімітка) | R | 30 заповнених: i.i.d. −0.438 … +0.247, ковзна −0.667 … +0.397; 30 розміщень: i.i.d. −0.333 … +0.218; n 100 не пораховано | 30 | x |
| Папір carry A | x | n 30: i.i.d. −9.000 … +9.000, ковзна −9.000 … +9.000 | x | x |

### Інше

| Демо S92 | x | n 30: i.i.d. −1.000 … +1.000, ковзна −1.000 … +1.000 | x | x |
"""


def _spec(setup: str, bands) -> DemoSpec:
    return DemoSpec(setup, setup, None, bands)


S90 = {30: ((-0.473, 0.361), (-0.719, 0.652)), 100: ((-0.299, 0.157), (-0.418, 0.307))}
S91 = {30: ((-0.438, 0.247), (-0.667, 0.397))}


def test_fixture_parses_demo_rows_of_the_table_only_and_lists_the_skip():
    doc, skips, problems = parse_doc(_FIX)
    assert problems == []
    assert sorted(doc) == ["S90", "S91"]  # not «Папір carry», not S92 from another section
    assert doc["S90"] == S90
    assert doc["S91"] == S91  # «30 заповнених», not «30 розміщень»
    assert skips == ["S91 n 100"]
    assert compare(doc, [_spec("S90", S90), _spec("S91", S91)]) == []


def test_fixture_reports_a_mismatched_pair_beyond_the_tolerance():
    doc, _, _ = parse_doc(_FIX)
    near = {30: ((-0.4734, 0.361), (-0.719, 0.652)), 100: S90[100]}  # within 0.0005
    assert compare(doc, [_spec("S90", near), _spec("S91", S91)]) == []
    off = {30: S90[30], 100: ((-0.299, 0.157), (-0.418, 0.044))}
    assert compare(doc, [_spec("S90", off), _spec("S91", S91)]) == ["S90 n 100 ковзна p95: code +0.044, document +0.307"]


def test_fixture_reports_a_missing_setup_and_a_band_the_document_skips():
    doc, _, _ = parse_doc(_FIX)
    extra = {**S91, 100: ((-0.1, 0.1), (-0.1, 0.1))}
    got = compare(doc, [_spec("S90", S90), _spec("S91", extra), _spec("S93", S91)])
    assert got == ["S91 n 100: band in the code, none in the document", "S93: in DEMO, no row «Демо S93» in the table"]
    assert compare(doc, [_spec("S90", S90)]) == ["S91: row in the table, not in DEMO"]


def test_fixture_flags_a_row_with_no_pair_and_no_explicit_skip():
    text = _FIX.replace("; n 100 не пораховано", "")
    _, skips, problems = parse_doc(text)
    assert skips == []
    assert problems == ["S91 n 100: no i.i.d. / rolling pair and no explicit «не пораховано»"]
