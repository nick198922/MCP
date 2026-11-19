"""Invisible characters: the shared cleaner of untrusted text strips them, and src/ never contains them literally.

Literal zero-width / bidi characters in source ("Trojan Source") do not show in a diff, so a reviewer cannot see what a
regex matches or what a string really says. The cleaner regex uses named escapes (analytics/_text.py); the
guard below fails if any Unicode format character (category Cf, which covers every bidi control) appears in src/.
"""

import unicodedata
from pathlib import Path

from tradedesk.analytics import pendle, yields
from tradedesk.analytics._text import CTRL_RE

SRC = Path(__file__).resolve().parent.parent / "src" / "tradedesk"
BIDI_CONTROLS = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
# (path relative to src/tradedesk, code point) -> reason. Empty: no file needs a literal invisible character.
ALLOWED: dict[tuple[str, int], str] = {}

# The set the regex stripped before it was shared (literal characters in yields.py / pendle.py): must not change.
EXPECTED = (
    set(range(0x00, 0x20))
    | set(range(0x7F, 0xA0))
    | set(range(0x200B, 0x2010))
    | set(range(0x202A, 0x202F))
    | set(range(0x2060, 0x2065))
    | {0xFEFF}
)


def invisible_chars(text: str) -> list[tuple[int, int]]:
    """(line, code point) of every format / bidi-control character in `text`."""
    return [
        (n, ord(ch))
        for n, line in enumerate(text.splitlines(), 1)
        for ch in line
        if unicodedata.category(ch) == "Cf" or unicodedata.bidirectional(ch) in BIDI_CONTROLS
    ]


def test_ctrl_re_matches_exactly_the_old_set():
    matched = {cp for cp in range(0x10000) if CTRL_RE.fullmatch(chr(cp))}
    assert matched == EXPECTED
    assert pendle._CTRL is CTRL_RE and yields._CTRL is CTRL_RE  # one shared regex


def test_clean_strips_each_class_and_keeps_plain_text():
    zwsp, rlo, bom, wj, lrm = chr(0x200B), chr(0x202E), chr(0xFEFF), chr(0x2060), chr(0x200E)
    dirty = f"{bom}US{zwsp}DC{rlo}-ETH{chr(0x00)}{chr(0x9F)}{wj}{lrm}\t"
    for clean in (yields._text, pendle._text):
        assert clean(dirty) == "USDC-ETH"
        assert clean("Київ USDC/ETH 5.2 % (Aave v3)") == "Київ USDC/ETH 5.2 % (Aave v3)"
        assert clean(f"{zwsp}{rlo}{bom}") is None  # nothing visible left


def test_src_has_no_literal_invisible_characters():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for line, cp in invisible_chars(path.read_text(encoding="utf-8")):
            if (rel, cp) not in ALLOWED:
                found.append(f"  {rel}:{line} U+{cp:04X} {unicodedata.name(chr(cp), '?')}")
    assert not found, "literal invisible / bidi characters in src/ (write them as escapes):\n" + "\n".join(found)


def test_guard_scanner_flags_synthetic_characters():
    text = f"ok\nx = '{chr(0x200B)}'\n# {chr(0x2066)} isolate\nplain = 'Київ'\n"
    assert invisible_chars(text) == [(2, 0x200B), (3, 0x2066)]
    assert len(list(SRC.rglob("*.py"))) > 100  # the guard really scans the package
