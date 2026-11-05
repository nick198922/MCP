"""scripts/dashboard.py, the «Ринок» tab: the stablecoin-supply fold from the snapshot's ``stablecoins`` section.

The section is built by the real stablecoin_supply tool through the snapshot job's own `_call`, on the SYNTHETIC
DefiLlama sample of 05.11 (tests/fixtures/defillama_stablecoins_sample_2026-11-05.json, via test_stablecoin_supply's
server: 299.46e9 now, 297.71e9 7 days and 291.96e9 30 days earlier). No network.
"""

import asyncio
import functools
import json
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import assistant_brief_tg as ab  # noqa: E402
import dashboard as db  # noqa: E402

import test_stablecoin_supply as tss  # noqa: E402

SNAP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "assistant_snapshot_2026-10-20.json"
SUMMARY = "299.46 млрд $ · 7 дн +0.6 % · 30 дн +2.6 % · описово, не сигнал"


@functools.cache
def _section_json(down: bool = False) -> str:
    from mcp import Client

    from tradedesk.collector.assistant_snapshot import _call

    async def run() -> dict:
        srv = tss.make_server((lambda req: httpx.Response(503, text="down")) if down else tss.default_handler)
        async with Client(srv) as c:
            return await _call(c, "stablecoin_supply", {})

    return json.dumps(asyncio.run(run()))


def _snap(section: dict | None = None) -> tuple[dict, float]:
    """The real 20.10 snapshot (it has no `stablecoins` section) plus `section` when given."""
    snap, err = db.load_snapshot(SNAP_FIXTURE)
    assert err is None
    if section is not None:
        snap["sections"]["stablecoins"] = section
    return snap, db.parse_utc(snap["asof_utc"]).timestamp() + 300


def _fold(page: str) -> str:
    m = re.search(r"<section class='wide fold' id='b-stables'>.*?</section>", page, re.S)
    assert m, "no stablecoin fold"
    return m.group(0)


def test_stables_fold_from_the_tool_section():
    section = json.loads(_section_json())
    assert section["ok"] and section["missing"] == []
    snap, now = _snap(section)
    page = db.render_brief(snap, None, now)
    fold = _fold(page)
    # folded (rule 4), last of the tab's folds, the short «i» (rule 3), the summary carries the numbers
    assert "<details open" not in page and page.index("id='b-funding'") < page.index("id='b-stables'") < page.index("</main>")
    assert "<h2>Стейблкоїни · ліквідність" in fold and db._info("stables") in fold and "Для угоди:" not in fold
    assert f"<span class='fs'>{db._e(SUMMARY)}</span>" in fold
    for row in ("Усього в обігу", "299.46 млрд $", "За 7 днів", "+0.6 %", "За 30 днів", "+2.6 %", "Дані на", "05.11"):
        assert row in fold, row
    assert "ряд застарів" not in fold and "Даних про стейблкоїни" not in fold and "немає даних" not in fold
    # rule 5: the label once on the page; the same numbers as the brief's line (the same fields, nothing recomputed)
    assert page.count("описово, не сигнал") == 1
    assert ab._stablecoins(snap, now) == ["Стейблкоїни 299.46 млрд $ (7 дн +0.6 %, 30 дн +2.6 %) · описово, не сигнал"]
    # a stale series names the day of its point; a missing change is left out, not shown as a dash
    section["data"]["total"] |= {"stale": True, "change_30d": None}
    snap, now = _snap(section)
    assert db.stables_summary(snap) == "299.46 млрд $ · 7 дн +0.6 % · дані на 05.11"
    fold = _fold(db.render_brief(snap, None, now))
    assert "05.11 · ряд застарів" in fold and "За 30 днів" not in fold


def test_stables_fold_without_the_section():
    snap, now = _snap()  # the 20.10 snapshot predates the section
    assert "stablecoins" not in snap["sections"]
    page = db.render_brief(snap, None, now)
    fold = _fold(page)
    assert "<span class='fs'>даних немає</span>" in fold
    assert "Даних про стейблкоїни в знімку немає." in fold and "секції немає в знімку" in fold
    # the rest of the tab is intact
    assert "Funding · річних" in page and "Hyperliquid" in page and page.endswith("</body></html>")
    assert "описово, не сигнал" not in page and "не вдалося" not in page


def test_stables_fold_on_provider_failure_or_odd_data():
    section = json.loads(_section_json(down=True))
    assert not section["ok"] and section["data"] is None and section["missing"]
    snap, now = _snap(section)
    snap["missing"] = [*snap.get("missing", []), "stablecoins"]
    page = db.render_brief(snap, None, now)
    fold = _fold(page)
    first = section["missing"][0]
    assert "без даних: стейблкоїни" in page  # the stamp names the failed section
    assert "<span class='fs'>даних немає</span>" in fold and "Даних про стейблкоїни в знімку немає." in fold
    assert db._e(f"немає даних «стейблкоїни»: {db._clean(first.get('detail') or first.get('reason'))}") in fold
    assert "Funding · річних" in page and page.endswith("</body></html>")
    good = json.loads(_section_json())
    for odd in (
        {"ok": True, "data": {"total": None, "usdt": {"usd": 1.0}}},
        {"ok": True, "data": {"total": "x"}},
        {"ok": True, "data": "x"},
        "x",
        {**good, "data": {**good["data"], "total": {**good["data"]["total"], "usd": "<b>"}}},
        {**good, "data": {**good["data"], "total": {**good["data"]["total"], "usd": 0.0}}},
        {**good, "data": {**good["data"], "total": {**good["data"]["total"], "change_7d": "<i>", "as_of_ts": 1e30, "stale": True}}},
    ):
        snap, now = _snap(odd)
        page = db.render_brief(snap, None, now)
        fold = _fold(page)
        assert page.endswith("</body></html>") and "&lt;b&gt;" not in fold and "&lt;i&gt;" not in fold, odd
        assert "не вдалося" not in fold and "Funding · річних" in page, odd
    # the last odd one keeps what is usable: the sum and the 30-day change, no day for an impossible timestamp
    assert db.stables_summary(snap) == "299.46 млрд $ · 30 дн +2.6 %"
