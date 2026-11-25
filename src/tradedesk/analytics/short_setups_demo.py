"""Step 4 of IDEA-20261024-short-setups-demo: the registered verdict of demo S38 / S39 shorts against a placebo on the
same candle (card section «Попередня реєстрація кроку 4 (25.11)»).

Pure functions, no I/O. Δ values are R on the trade's risk (decimal R, not a return on capital). The inputs are the
outputs of :func:`tradedesk.analytics.demo_placebo.summarize` (day-clustered bootstrap) and
:func:`tradedesk.analytics.demo_placebo.cluster_t` (CR1 t by day) computed on the same Δ values.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

MIN_N = 30  # closed trades per setup (the card's step-4 condition)
MIN_CLUSTERS = 10  # signal days
MDE_MAX = 0.30  # R: power gate, blocks only the positive verdict
MIN_EFFECT = 0.05  # R: the card's «Критерій вбити» (Δ vs placebo ≥ +0.05R)
ALPHA = 0.05
FAMILY = ("S38", "S39")  # Holm family, fixed: a setup without a p enters with p = 1
# S39 precedence (registration 25.11): the step-4 S39 verdict counts only if the registration reached main before the
# registered v2 re-run of the demo-setups-depth card (2026-11-28 05:00 UTC) could show the S39 Δ.
S39_BLIND_DEADLINE_MS = 1_795_842_000_000  # 2026-11-28 05:00 UTC (the v2 registered --now-ms)


def manifest_line(row: Mapping[str, Any]) -> str:
    """Canonical ``link_id|status|closed_ts|r_multiple`` line of a journal row: ``closed_ts`` as an integer (ms) or
    ``None``, ``r_multiple`` with six decimals (``-0.000000`` written as ``0.000000``) or ``None``."""
    c = row.get("closed_ts")
    r = row.get("r_multiple")
    rs = "None" if r is None else f"{float(r):.6f}"
    if rs == "-0.000000":
        rs = "0.000000"
    return f"{row['link_id']}|{row['status']}|{'None' if c is None else int(c)}|{rs}"


def sample_manifest(
    closed: Iterable[Mapping[str, Any]], other: Iterable[Mapping[str, Any]] = (), setups: Sequence[str] = FAMILY
) -> dict[str, dict[str, Any]]:
    """Per setup: ``n_closed``, sorted closed ``link_ids``, ``sha256`` (hex) of the closed rows' :func:`manifest_line`
    sorted by ``link_id`` and joined by ``\\n`` (UTF-8), and ``other_terminal`` (sorted ``link_id|status`` of the other
    terminal rows, e.g. ``missed``). The output carries no R value; the hash of 30+ rows does not reveal one."""
    out: dict[str, dict[str, Any]] = {}
    cl = list(closed)
    ot = list(other)
    for s in setups:
        rows = sorted((r for r in cl if r["setup"] == s), key=lambda r: str(r["link_id"]))
        body = "\n".join(manifest_line(r) for r in rows).encode("utf-8")
        out[s] = {
            "n_closed": len(rows),
            "link_ids": [str(r["link_id"]) for r in rows],
            "sha256": hashlib.sha256(body).hexdigest(),
            "other_terminal": sorted(f"{r['link_id']}|{r['status']}" for r in ot if r["setup"] == s),
        }
    return out


def manifest_diff(expected: Mapping[str, Mapping[str, Any]], got: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Differences between a registered manifest and the current one (empty: identical). The messages name setups and
    link_ids only, never an R value."""
    out: list[str] = []
    for s in sorted(set(expected) | set(got)):
        e, g = expected.get(s), got.get(s)
        if e is None or g is None:
            out.append(f"{s}: {'not registered' if e is None else 'absent now'}")
            continue
        ei, gi = set(e.get("link_ids", [])), set(g.get("link_ids", []))
        if ei - gi:
            out.append(f"{s}: closed rows gone {sorted(ei - gi)}")
        if gi - ei:
            out.append(f"{s}: closed rows added {sorted(gi - ei)}")
        if int(e.get("n_closed", -1)) != int(g.get("n_closed", -2)):
            out.append(f"{s}: n_closed {e.get('n_closed')} registered, {g.get('n_closed')} now")
        eo, go = set(e.get("other_terminal", [])), set(g.get("other_terminal", []))
        if eo != go:
            out.append(f"{s}: other terminal rows registered {sorted(eo - go)} / now {sorted(go - eo)}")
        if e.get("sha256") != g.get("sha256"):
            same = "" if ei != gi else " with the same link_ids (status, closed_ts or r_multiple of a row changed)"
            out.append(f"{s}: sha256 {e.get('sha256')} registered, {g.get('sha256')} now{same}")
    return out


def s39_blind(registration_main_ms: int | None, deadline_ms: int = S39_BLIND_DEADLINE_MS) -> bool:
    """True if the registration reached main (latest first-parent commit time of the manifest, ms) before
    ``deadline_ms``; an unknown time (None) is treated as late, so S39 is then reported for the record only."""
    return registration_main_ms is not None and int(registration_main_ms) < deadline_ms


def step4_card_status(verdicts: Mapping[str, str], s39_counts: bool) -> str:
    """:func:`card_status` of the governing verdicts: when S39 is for the record only (``s39_counts`` False) it enters as
    '◯', so only an S38 '✓' moves the card and 'rejected' (both '✗') is impossible."""
    v = dict(verdicts)
    if not s39_counts and "S39" in v:
        v["S39"] = "◯"
    return card_status(v)


def days_to_gate(mde: float | None, clusters: int, gate: float = MDE_MAX) -> int | None:
    """Signal days needed for MDE80 to reach ``gate`` under MDE ∝ 1/√days: ⌈(mde / gate)² · clusters⌉ (``clusters`` if
    already at or below the gate); None without an MDE."""
    if mde is None or clusters <= 0:
        return None
    return max(int(clusters), math.ceil((float(mde) / gate) ** 2 * clusters - 1e-9))


def holm_fixed(p: Mapping[str, float | None], family: Sequence[str] = FAMILY) -> dict[str, float]:
    """Holm step-down adjusted p over the fixed ``family``; a member with no p (None, absent) counts as p = 1, so the
    family size never shrinks after the data are seen."""
    ps = [(1.0 if p.get(s) is None else float(p[s]), s) for s in family]  # type: ignore[arg-type]
    m = len(ps)
    out: dict[str, float] = {}
    run = 0.0
    for i, (v, s) in enumerate(sorted(ps)):
        run = max(run, min(1.0, (m - i) * v))
        out[s] = run
    return out


def verdict_step4(
    boot: Mapping[str, Any],
    ct: Mapping[str, Any],
    *,
    p_holm: float | None,
    new_mean: float | None,
    min_n: int = MIN_N,
    min_clusters: int = MIN_CLUSTERS,
    mde_max: float = MDE_MAX,
    min_effect: float = MIN_EFFECT,
    alpha: float = ALPHA,
) -> str:
    """Registered rule, first match wins; returns one of '✓', '≈', '✗', '◯'.

    1. n < ``min_n``, fewer than ``min_clusters`` clusters, or a missing CI -> '◯'.
    2. Upper bounds of both CIs (bootstrap and CR1 t by day) < ``min_effect`` -> '✗' (an excess of ``min_effect`` over
       the placebo is excluded; the power gate does not block it).
    3. Lower bounds of both CIs > 0, Holm p < ``alpha``, mean ≥ ``min_effect``, max(MDE80 of both) ≤ ``mde_max`` and
       the mean Δ of the trades whose R was in no published report (``new_mean``) > 0 -> '✓'.
    4. Lower bounds of both CIs > 0 and Holm p < ``alpha`` (some other condition of 3 fails) -> '≈'.
    5. Otherwise -> '◯'.
    """
    if boot.get("n", 0) < min_n or boot.get("clusters", 0) < min_clusters or boot.get("ci") is None or ct.get("ci") is None:
        return "◯"
    (bl, bh), (tl, th) = boot["ci"], ct["ci"]
    if max(bh, th) < min_effect:
        return "✗"
    significant = bl > 0 and tl > 0 and p_holm is not None and p_holm < alpha
    if not significant:
        return "◯"
    mde = max(float(boot["mde80"]), float(ct["mde80"]))
    if float(boot["mean"]) >= min_effect and mde <= mde_max and new_mean is not None and new_mean > 0:
        return "✓"
    return "≈"


def card_status(verdicts: Mapping[str, str]) -> str:
    """Card status from the per-setup verdicts: 'confirmed' if any is '✓' (the card's hypothesis is «A or B»),
    'rejected' if every member is '✗', 'inconclusive' otherwise (also for an empty mapping)."""
    vs = list(verdicts.values())
    if any(v == "✓" for v in vs):
        return "confirmed"
    if vs and all(v == "✗" for v in vs):
        return "rejected"
    return "inconclusive"


def regime_at(open_ms: Sequence[int], dirs: Sequence[int], ts_ms: int, bar_ms: int) -> int:
    """Direction (+1 / 0 / −1) of the last bar closed at or before ``ts_ms`` (its open + ``bar_ms`` ≤ ``ts_ms``);
    ``open_ms`` ascending, ``dirs[i]`` computed on bars 0..i only. 0 when no bar has closed. Bars that close after
    ``ts_ms`` never change the answer."""
    i = bisect.bisect_right(list(open_ms), ts_ms - bar_ms) - 1
    return int(dirs[i]) if i >= 0 else 0


def best_cluster(values: Sequence[float], clusters: Sequence[Any]) -> Any | None:
    """The cluster with the largest sum of ``values`` (ties: the smallest key); None for no values."""
    sums: dict[Any, float] = {}
    for v, c in zip(values, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + float(v)
    if not sums:
        return None
    return sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def without_top(values: Sequence[float], k: int) -> list[int]:
    """Indices that remain after removing the ``k`` largest values (ties: the earlier index is removed first)."""
    order = sorted(range(len(values)), key=lambda i: (-float(values[i]), i))
    drop = set(order[: max(0, k)])
    return [i for i in range(len(values)) if i not in drop]
