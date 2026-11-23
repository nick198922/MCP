"""Labels of a public trader's call parsed from the pre-registered tag in its CSV ``note`` (card IDEA-20261113-x-traders, 3.3 / 3.3б).

Pure functions, no I/O. A tag is the last ``" · 3.3б (…): "`` or ``" · 3.3 (…): "`` block of a note, written before any
price was loaded; its ``"; "``-separated segments (separators inside parentheses or «» quotes do not split) are, in order:
``t0 …``, ``група …``, ownership, timeframe, then free extras (``виключення: …``, ``межовий за 3.3: …``, remarks). The
parser only reads what the tag says; it never decides a label itself, so a row the tag does not cover yields ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

TAG_RE = re.compile(r" · (3\.3б?) \(([^)]*)\): ")
T0_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}) UTC")
ID_RE = re.compile(r"\b[12]\d{17,19}\b")

GROUPS = {"ex ante": "ex_ante", "показано вже в плюсі": "in_profit", "стан невідомий": "unknown"}
OWNERSHIP = {
    "строго власна": "strict",
    "з наміром": "intent",
    "без названої власної позиції": "none",
    "без названої позиції": "none",
    "власна позиція названа": "named",
}
# exclusion keyword -> code; the first match in a "виключення: …" segment wins
EXCLUSIONS = (
    ("умовний", "conditional"),
    ("мем", "meme"),
    ("відновлений", "recovered"),
    ("збіг тікера", "collision"),
    ("TradFi", "tradfi"),
    ("одна угода", "same_trade"),
)
IDLIST_OUT = "списком id 3.3» — виключено"
LIMIT_CONDITIONAL = "зі стилем `limit` — умовний"
Q2_MARK = "шаблон Q-20261118-2"
Q2_OUT = "поза первинним"
# words of a border alternative that put a row out of / into the primary set
ALT_OUT = ("показано вже в плюсі", "мем", "умовний", "(а)", "стан невідомий")
ALT_IN = ("ex ante", "рядок у первинному", "у наборі")


@dataclass(frozen=True)
class Labels:
    tag: str  # "3.3б" | "3.3"
    t0_ms: int
    t0_id: str
    t0_moved: bool
    group: str  # ex_ante | in_profit | unknown
    ownership: str  # strict | intent | none | named | ""
    scalp: bool
    exclusion: str  # "" | conditional | meme | recovered | collision | tradfi | same_trade
    border: bool
    border_alt: str
    q2_template: bool
    q2_out: bool
    idlist_out: bool
    limit_conditional: bool

    @property
    def primary(self) -> bool:
        """Member of the primary set: ex ante, no exclusion, not the template row outside the 10-minute rule."""
        return self.group == "ex_ante" and not self.exclusion and not self.q2_out

    @property
    def idlist_variant(self) -> bool:
        """Member of the mandatory variant «exclusions by the 3.3 id list» (3.3б, review B p. 2)."""
        return self.primary and not self.idlist_out

    @property
    def border_variant(self) -> bool:
        """Member of the mandatory variant «border rows in the opposite group».

        A border row flips membership when its alternative names a group on the other side of the primary set; a border
        about ownership only (e.g. «строго власна») keeps membership. An excluded row stays out either way."""
        if not self.border:
            return self.primary
        alt = self.border_alt
        if self.primary:
            return not any(w in alt for w in ALT_OUT)
        if self.exclusion and self.exclusion != "conditional" and self.exclusion != "meme":
            return False
        return any(w in alt for w in ALT_IN)

    @property
    def border_ownership(self) -> str:
        """Ownership in the variant «border rows in the opposite group»: an alternative that names no group but names an
        ownership (03-20-SHORT: «строго власна») replaces the row's ownership; every other row keeps its own."""
        alt = self.border_alt
        if not self.border or any(w in alt for w in ALT_OUT + ALT_IN):
            return self.ownership
        return next((code for word, code in OWNERSHIP.items() if word in alt), self.ownership)


def split_top(text: str, sep: str = "; ") -> list[str]:
    """Split on ``sep`` outside parentheses and «» quotes."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "(«":
            depth += 1
        elif ch in ")»":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(sep, i):
            out.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def _t0(seg: str) -> tuple[int, str, bool]:
    stamps = T0_RE.findall(seg)
    if not stamps:
        raise ValueError(f"no t0 time in {seg[:80]!r}")
    d, m, y, hh, mm = (int(x) for x in stamps[-1])
    ms = int(datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp() * 1000)
    ids = ID_RE.findall(seg)
    return ms, (ids[0] if ids else ""), "перенесено" in seg


def parse_tag(note: str) -> Labels | None:
    """Labels of the last 3.3 / 3.3б tag of ``note``; ``None`` if the note has no tag. Raises ValueError on a malformed tag."""
    found = list(TAG_RE.finditer(note))
    if not found:
        return None
    m = found[-1]
    segs = split_top(note[m.end() :])
    if not segs or not segs[0].startswith("t0 "):
        raise ValueError(f"tag without t0: {note[m.start() : m.start() + 120]!r}")
    t0_ms, t0_id, moved = _t0(segs[0])
    grp_seg = next((s for s in segs if s.startswith("група ")), "")
    group = next((code for word, code in GROUPS.items() if grp_seg[6:].startswith(word)), "")
    if not group:
        raise ValueError(f"tag without a known group: {grp_seg[:80]!r}")
    ownership = next((code for s in segs for word, code in OWNERSHIP.items() if s.startswith(word)), "")
    tf = next((s for s in segs if s.startswith(("свінг", "скальп"))), "")
    exclusion = ""
    for s in segs:
        if s.startswith("виключення:"):
            exclusion = next((code for word, code in EXCLUSIONS if word in s), "other")
            break
    border_seg = next((s for s in segs if "межовий за 3.3" in s), "")
    alt = border_seg.split("альт.", 1)[1] if "альт." in border_seg else border_seg
    tail = note[m.start() :]
    return Labels(
        tag=m.group(1),
        t0_ms=t0_ms,
        t0_id=t0_id,
        t0_moved=moved,
        group=group,
        ownership=ownership,
        scalp=tf.startswith("скальп"),
        exclusion=exclusion,
        border=bool(border_seg),
        border_alt=alt.strip(),
        q2_template=Q2_MARK in tail,
        q2_out=Q2_OUT in grp_seg,
        idlist_out=IDLIST_OUT in tail,
        limit_conditional=LIMIT_CONDITIONAL in tail,
    )


def quote_units(symbol: str) -> str:
    """Currency of a row's return: BTC for ``*/BTC`` pairs, USD otherwise (card 3.3, metric p. 4)."""
    return "BTC" if symbol.upper().endswith("/BTC") else "USD"
