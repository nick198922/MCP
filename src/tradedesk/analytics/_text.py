"""Shared helpers for untrusted third-party text (pure: no I/O)."""

from __future__ import annotations

import re

# C0/C1 controls, zero-width characters and marks (U+200B-U+200F), bidi embeddings / overrides (U+202A-U+202E),
# word joiner and invisible operators (U+2060-U+2064) and the BOM (U+FEFF). Written as named escapes so a reviewer
# and a diff see exactly what is stripped; tests/test_no_invisible_chars.py keeps literal invisible characters out
# of src/.
CTRL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f"
    r"\N{ZERO WIDTH SPACE}-\N{RIGHT-TO-LEFT MARK}"
    r"\N{LEFT-TO-RIGHT EMBEDDING}-\N{RIGHT-TO-LEFT OVERRIDE}"
    r"\N{WORD JOINER}-\N{INVISIBLE PLUS}"
    r"\N{ZERO WIDTH NO-BREAK SPACE}]"
)
