"""End-to-end check of CLAUDE.md invariant 1 for a well-formed answer with a field of the wrong type inside.

Sibling of tests/test_tools_malformed_200.py (a malformed body) and tests/test_tools_envelope_shape.py (the happy-path
fakes, the seeded store, ARGS and SKIP, all reused here). A venue answers 200 with the expected keys, but one numeric
field is `null`, the string "abc" or an empty list. For every registered tool:
1. a warm-up call fills the in-process caches (Hyperliquid's coin map, ccxt markets) and runs one-shot side effects
   (carry_paper_close closes its pair); a second clean call records the numeric fields (numbers and numeric strings)
   of every JSON body and every ccxt result the tool reads. A field is named by its source (host + path, or ccxt
   exchange + method) and its path in the tree, with list indices folded to `[*]` (only short lists of scalars, i.e.
   rows like an OHLCV bar or a [price, size] level, keep their index); fields cached in-process are not mutated;
2. up to MAX_MUTATIONS (field, kind) pairs are picked deterministically from the sorted product, evenly spread;
3. for each pair the tool is called again with that field broken in the first place it occurs in every body.
The answer must be the envelope (`ok` true or false), never an error result. ccxt results get only `null` (ccxt already
turns a venue's strings into numbers or None, so "abc" and [] cannot come out of it).
KNOWN: violations found and not fixed yet (each also in docs/BACKLOG.md 6.41); a stale entry fails the test.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_server import EXPECTED_TOOLS
from test_tools_envelope_shape import ARGS, COINALYZE_TEST_KEY, NO_NETWORK, SKIP, _extra_fakes, _seed_store, _Transports
from test_tools_provider_outage import as_envelope
from tradedesk.analytics.options import build_chain
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools._util import parsed_rows

MAX_MUTATIONS = 30
SHORT_ROW = 8  # a list of scalars up to this length is a row (OHLCV bar, [price, size] level): its indices name fields
KINDS: dict[str, Any] = {"null": None, "abc": "abc", "empty_list": []}
CCXT_KINDS = ("null",)

# Tools that still raise on a wrong-type field: {"tool": "which field and what escapes"}; each is in BACKLOG 6.41.
KNOWN: dict[str, str] = {}

# Tools that read no numeric field on the recording call, so nothing is mutated (the test fails if one starts to):
# the calculators and store tools of NO_NETWORK, plus these two.
NO_FIELDS: dict[str, str] = {
    "carry_paper_close": "the warm-up call closes the seeded pair; the next calls answer from the store",
    "plan_review": "each tool gets a fresh store here, so there is no open plan whose candles it would fetch",
}


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    if isinstance(value, str) and value.strip():
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def numeric_fields(tree: Any, prefix: str = "") -> list[tuple[str, list]]:
    """(field name, concrete path) of every numeric leaf, in document order; the name folds list indices to [*]."""
    out: list[tuple[str, list]] = []

    def walk(node: Any, name: str, path: list) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{name}.{k}", [*path, k])
        elif isinstance(node, list):
            row = len(node) <= SHORT_ROW and not any(isinstance(x, dict | list) for x in node)
            for i, v in enumerate(node):
                walk(v, f"{name}[{i if row else '*'}]", [*path, i])
        elif _is_numeric(node):
            out.append((name, path))

    walk(tree, prefix, [])
    return out


def break_field(tree: Any, prefix: str, field: str, value: Any) -> bool:
    """Set the first leaf named `field` to `value` (a fresh copy); True when the tree had one."""
    for name, path in numeric_fields(tree, prefix):
        if name == field:
            node = tree
            for step in path[:-1]:
                node = node[step]
            node[path[-1]] = copy.copy(value)
            return True
    return False


def pick(fields: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Up to MAX_MUTATIONS (field, kind) pairs, evenly spread over the sorted product (deterministic)."""
    pairs = [(f, k) for f in sorted(fields) for k in fields[f]]
    if len(pairs) <= MAX_MUTATIONS:
        return pairs
    return [pairs[i * len(pairs) // MAX_MUTATIONS] for i in range(MAX_MUTATIONS)]


class Mutator:
    """Shared by the transports and the ccxt wrappers: records fields on the clean call, breaks one on the next ones."""

    def __init__(self) -> None:
        self.seen: dict[str, tuple[str, ...]] | None = None
        self.target: tuple[str, Any] | None = None
        self.hits = 0

    def apply(self, tree: Any, prefix: str, kinds: tuple[str, ...]) -> Any:
        if self.seen is not None:
            for name, _ in numeric_fields(tree, prefix):
                self.seen.setdefault(name, kinds)
        if self.target is not None:
            field, value = self.target
            if field.startswith(prefix + ".") or field.startswith(prefix + "["):
                tree = copy.deepcopy(tree)
                self.hits += break_field(tree, prefix, field, value)
        return tree


class _MutatingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, mutator: Mutator) -> None:
        self._inner = inner
        self._m = mutator

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        body = await resp.aread()
        headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding")]
        if resp.status_code == 200:
            try:
                tree = json.loads(body)
            except ValueError:
                pass
            else:
                tree = self._m.apply(tree, f"http:{request.url.host}{request.url.path}", tuple(KINDS))
                body = json.dumps(tree).encode()
        return httpx.Response(resp.status_code, headers=headers, content=body)


class _MutatingEx:
    """FakeEx whose async results go through the Mutator (attributes FakeEx lacks stay missing)."""

    def __init__(self, ex_id: str, mutator: Mutator) -> None:
        self._ex = FakeEx(ex_id)
        self._m = mutator
        self.id = ex_id
        self.has = self._ex.has

    def __getattr__(self, name: str):
        attr = getattr(self._ex, name)
        if name == "close" or not callable(attr):
            return attr

        async def mutated(*a, **k):
            return self._m.apply(await attr(*a, **k), f"ccxt:{self.id}.{name}", CCXT_KINDS)

        return mutated


def _runtime(tmp_path, mutator: Mutator) -> Runtime:
    fakes = _extra_fakes(make_transports(State()))
    # ~30 calls per tool against the fakes: the production buckets (GoPlus 0.2/s, CoinGecko 0.5/s) would cost minutes.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            Settings(
                http_retries=0,
                http_retry_backoff=0.0,
                cache_scale=0.0,
                db_path=str(tmp_path / "wrong_type.sqlite"),
                coinalyze_api_key=COINALYZE_TEST_KEY,
            ),
            transports=_Transports({name: _MutatingTransport(t, mutator) for name, t in fakes.items()}),
            exchange_factory=lambda ex_id: _MutatingEx(ex_id, mutator),
        )
    rt.now = lambda: NOW
    return rt


def _failure(res) -> str | None:
    """None when the answer is the envelope; otherwise what is wrong with it."""
    if res.is_error:
        return "raised: " + (res.content[0].text if res.content else "")[:200]
    env = as_envelope(res)
    if not isinstance(env, dict) or not isinstance(env.get("ok"), bool):
        return "no envelope: " + str(env)[:200]
    return None


async def run_tool(tmp_path, name: str) -> dict[str, Any]:
    """The clean call plus every picked mutation of one tool; returns the counts and the failures by mutation."""
    m = Mutator()
    rt = _runtime(tmp_path, m)
    await _seed_store(rt)
    srv, _ = build_server(rt)
    report: dict[str, Any] = {"mutations": 0, "hits": 0, "failures": {}}
    async with Client(srv) as c:
        # Two clean calls: the first warms the in-process caches (Hyperliquid's coin map, ccxt markets) and runs the
        # one-shot side effects (carry_paper_close), the second records only the fields every call reads again.
        for recording in (None, {}):
            m.seen = recording
            res = await c.call_tool(name, ARGS.get(name, {}))
            if (err := _failure(res)) is not None:
                report["failures"]["clean"] = err
        pairs = pick(m.seen)
        m.seen = None
        for field, kind in pairs:
            m.target, m.hits = (field, KINDS[kind]), 0
            res = await c.call_tool(name, ARGS.get(name, {}))
            report["mutations"] += 1
            report["hits"] += bool(m.hits)
            if (err := _failure(res)) is not None:
                report["failures"][f"{field} -> {kind}"] = err
    return report


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS - set(SKIP)))
async def test_no_tool_raises_on_a_wrong_type_field(tmp_path, name):
    report = await run_tool(tmp_path, name)
    assert report["hits"] == report["mutations"], "a picked field was not found again: the fakes are not deterministic"
    assert (report["mutations"] == 0) == (name in NO_NETWORK or name in NO_FIELDS), f"update NO_FIELDS: {report['mutations']} mutations"
    if name in KNOWN:
        assert report["failures"], f"{name} is fixed: remove it from KNOWN and BACKLOG 6.41"
    else:
        assert report["failures"] == {}, f"{name}: fix it (type check / Builder.guard) or list it in KNOWN"


async def test_the_check_catches_a_tool_that_trusts_the_schema(tmp_path):
    """Self-check: a tool that does float() on a venue field without a guard is reported, so a green run means something."""
    m = Mutator()
    rt = _runtime(tmp_path, m)
    srv, _ = build_server(rt)

    @srv.tool(name="zz_schema_trusting_probe")
    async def zz_schema_trusting_probe() -> dict:
        fetched = await rt.deribit.book_summary("BTC", "future")
        return {"ok": True, "mark": float(fetched.data[0]["mark_price"])}  # no type check

    async with Client(srv) as c:
        m.seen = {}
        assert _failure(await c.call_tool("zz_schema_trusting_probe", {})) is None
        fields, m.seen = m.seen, None
        assert fields, "the probe read no numeric field"
        m.target = (next(f for f in sorted(fields) if f.endswith("result[*].mark_price")), KINDS["abc"])
        assert (_failure(await c.call_tool("zz_schema_trusting_probe", {})) or "").startswith("raised")


def test_numeric_fields_and_break_field():
    tree = {"result": [{"p": "1.5", "s": "BTC"}, {"p": 2}], "bar": [1, 2.0, "3"], "flag": True, "inf": "inf"}
    names = [n for n, _ in numeric_fields(tree, "x")]
    assert names == ["x.result[*].p", "x.result[*].p", "x.bar[0]", "x.bar[1]", "x.bar[2]"]
    assert break_field(tree, "x", "x.result[*].p", None) and tree["result"] == [{"p": None, "s": "BTC"}, {"p": 2}]
    assert not break_field(tree, "x", "x.nope", None)


def test_parsed_rows_drops_only_the_broken_rows():
    rows = [["1", "2.5"], [2, None], [3, "abc"], [4, []], [5], ["6", 7.0]]
    assert parsed_rows(rows, lambda x: (int(x[0]), float(x[1]))) == [(1, 2.5), (6, 7.0)]
    assert parsed_rows(None, float) == []


def test_build_chain_treats_a_wrong_type_field_like_an_absent_one():
    row = {"instrument_name": "BTC-27NOV26-90000-C", "underlying_price": 83000.0, "mark_iv": 45.0, "mark_price": 0.02}
    good = build_chain([row], NOW)[0]
    for key in ("mark_iv", "mark_price", "open_interest"):
        for bad in ("abc", [], True):
            got = build_chain([{**row, key: bad}], NOW)[0]
            want = build_chain([{k: v for k, v in row.items() if k != key}], NOW)[0]
            assert got == want, (key, bad)
    assert good.iv == 0.45 and good.delta is not None
    assert build_chain([{**row, "underlying_price": "abc"}], NOW) == []


def test_pick_is_capped_and_spread():
    fields = {f"f{i:02d}": tuple(KINDS) for i in range(40)}
    picked = pick(fields)
    assert len(picked) == MAX_MUTATIONS and picked == pick(fields)
    assert len({f for f, _ in picked}) == MAX_MUTATIONS and {k for _, k in picked} == set(KINDS)


def test_lists_name_registered_tools_only():
    assert set(KNOWN) <= EXPECTED_TOOLS and set(NO_FIELDS) <= EXPECTED_TOOLS
    assert NO_NETWORK.isdisjoint(NO_FIELDS)
