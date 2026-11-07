"""End-to-end check of CLAUDE.md invariant 1 for a well-formed answer with a field of the wrong type inside.

Sibling of tests/test_tools_malformed_200.py (a malformed body) and tests/test_tools_envelope_shape.py (the happy-path
fakes, the seeded store, ARGS and SKIP, all reused here). A venue answers 200 with the expected keys, but one numeric
field is `null`, the string "abc" or an empty list. For every registered tool:
1. a warm-up call fills the in-process caches (Hyperliquid's coin map, ccxt markets) and runs one-shot side effects
   (carry_paper_close closes its pair); a second clean call records the numeric fields (numbers and numeric strings)
   of every JSON body and every ccxt result the tool reads. A field is named by its source (host + path, or ccxt
   exchange + method) and its path in the tree, with list indices folded to `[*]` (only short lists of scalars, i.e.
   rows like an OHLCV bar or a [price, size] level, keep their index);
2. up to MAX_MUTATIONS (field, kind, where) triples are picked deterministically from the sorted product, evenly
   spread; `where` is the occurrence of the field that is broken in every body: the first, the second (when a body
   has two) or the last (when it has three or more), so a bad row in the middle or at the end of an array is tried too;
3. for each triple the tool is called again with that occurrence broken.
ccxt results get the same three kinds as JSON bodies (ccxt normally hands out numbers or None, but a venue field it
passes through, or a parser change, can put a string or a list there).
Cold caches (item 197): a value cached in-process is read again and again, and the warm calls above never break it
before the cache is filled. test_no_tool_raises_on_a_bad_value_in_a_cold_cache builds a fresh runtime with the TTL
caches on (cache_scale=1) per mutation, breaks a cached field from the very first call, and calls the tool twice
(the first fills the cache with the bad value, the second answers from it). Cached fields: the numeric fields of ccxt
`load_markets` (kept 1 h) and the coin names of Hyperliquid's metaAndAssetCtxs universe, the source of the coin map
(kept 10 min; a name gets `null` or [] only, "abc" is a valid name).
The answer must be the envelope (`ok` true or false), never an error result.
KNOWN: violations found and not fixed yet (each also in docs/BACKLOG.md 6.41); a stale entry fails the test, and so
does a failure whose key does not match the entry's pattern.
"""

from __future__ import annotations

import copy
import json
import math
import re
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

MAX_MUTATIONS = 40
MAX_COLD = 12  # cold-cache mutations per tool: each builds its own runtime
SHORT_ROW = 8  # a list of scalars up to this length is a row (OHLCV bar, [price, size] level): its indices name fields
KINDS: dict[str, Any] = {"null": None, "abc": "abc", "empty_list": []}
KEY_KINDS = ("null", "empty_list")  # for a string field a cache is keyed by ("abc" is a valid string there)
WHERE = {"first": 0, "second": 1, "last": -1}

# Sources cached in-process, which only the cold-cache test breaks before the cache is filled.
CCXT_MARKETS = re.compile(r"^ccxt:[^.]+\.load_markets[.\[]")
HL_COIN_NAMES = re.compile(r"^http:api\.hyperliquid\.xyz/info(\[\*\])?\.universe\[\*\]\.name$")

# Tools that still raise on a wrong-type field: {"tool": regex}; each is in BACKLOG 6.41. Every failure key of the tool
# ("<field> -> <kind> @<where>") must fully match the regex, so a second, unrelated violation is not hidden by the entry.
KNOWN: dict[str, str] = {}
KNOWN_COLD: dict[str, str] = {}


def known_failures_match(failures: dict[str, str], pattern: str) -> bool:
    """True when there is at least one failure and every failure key fully matches the KNOWN pattern."""
    return bool(failures) and all(re.fullmatch(pattern, k) for k in failures)


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


def is_key(field: str) -> bool:
    """A string field an in-process cache is built from (mutated with KEY_KINDS)."""
    return bool(HL_COIN_NAMES.match(field))


def is_cached(field: str) -> bool:
    return is_key(field) or bool(CCXT_MARKETS.match(field))


def numeric_fields(tree: Any, prefix: str = "") -> list[tuple[str, list]]:
    """(field name, concrete path) of every numeric leaf and every cache-key string (is_key), in document order; the
    name folds list indices to [*]."""
    out: list[tuple[str, list]] = []

    def walk(node: Any, name: str, path: list) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{name}.{k}", [*path, k])
        elif isinstance(node, list):
            row = len(node) <= SHORT_ROW and not any(isinstance(x, dict | list) for x in node)
            for i, v in enumerate(node):
                walk(v, f"{name}[{i if row else '*'}]", [*path, i])
        elif _is_numeric(node) or (isinstance(node, str) and is_key(name)):
            out.append((name, path))

    walk(tree, prefix, [])
    return out


def break_field(tree: Any, prefix: str, field: str, value: Any, where: str = "first") -> bool:
    """Set one leaf named `field` to `value` (a fresh copy): the first, the second or the last one in document order.
    True when the tree had it (`second` needs two)."""
    paths = [path for name, path in numeric_fields(tree, prefix) if name == field]
    if len(paths) <= max(WHERE[where], 0):
        return False
    node = tree
    path = paths[WHERE[where]]
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = copy.copy(value)
    return True


def mutations(fields: dict[str, int], keep=lambda f: True, cap: int = MAX_MUTATIONS) -> list[tuple[str, str, str]]:
    """(field, kind, where) triples of the fields `keep` accepts, all of them or `cap` picked deterministically; `fields`
    maps a name to the most occurrences seen in one body. `second` is offered from two occurrences, `last` from three
    (with two it is the second). When capped, the field is picked evenly over the sorted product and its (kind, where)
    rotates with the pick index, so an even stride cannot land on one kind only."""
    combos: dict[str, list[tuple[str, str]]] = {}
    for f in sorted(x for x in fields if keep(x)):
        n = fields[f]
        wheres = ["first", *(["second"] if n >= 2 else []), *(["last"] if n >= 3 else [])]
        combos[f] = [(k, w) for k in (KEY_KINDS if is_key(f) else KINDS) for w in wheres]
    flat = [f for f in combos for _ in combos[f]]
    if len(flat) <= cap:
        return [(f, k, w) for f in combos for k, w in combos[f]]
    # picks that land in one field have consecutive indices, at most len(combos[f]) of them: no duplicates
    return [(f, *combos[f][i % len(combos[f])]) for i in range(cap) for f in [flat[i * len(flat) // cap]]]


class Mutator:
    """Shared by the transports and the ccxt wrappers: records fields on the clean call, breaks one on the next ones."""

    def __init__(self) -> None:
        self.seen: dict[str, int] | None = None  # field name -> most occurrences in one body
        self.target: tuple[str, Any, str] | None = None  # (field, value, where)
        self.hits = 0

    def apply(self, tree: Any, prefix: str) -> Any:
        if self.seen is not None:
            counts: dict[str, int] = {}
            for name, _ in numeric_fields(tree, prefix):
                counts[name] = counts.get(name, 0) + 1
            for name, n in counts.items():
                self.seen[name] = max(self.seen.get(name, 0), n)
        if self.target is not None:
            field, value, where = self.target
            if field.startswith(prefix + ".") or field.startswith(prefix + "["):
                tree = copy.deepcopy(tree)
                self.hits += break_field(tree, prefix, field, value, where)
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
                tree = self._m.apply(tree, f"http:{request.url.host}{request.url.path}")
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
            return self._m.apply(await attr(*a, **k), f"ccxt:{self.id}.{name}")

        return mutated


def _runtime(tmp_path, mutator: Mutator, cache_scale: float = 0.0, db: str = "wrong_type") -> Runtime:
    fakes = _extra_fakes(make_transports(State()))
    # ~30 calls per tool against the fakes: the production buckets (GoPlus 0.2/s, CoinGecko 0.5/s) would cost minutes.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            Settings(
                http_retries=0,
                http_retry_backoff=0.0,
                cache_scale=cache_scale,
                db_path=str(tmp_path / f"{db}.sqlite"),
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
        triples = mutations(m.seen, keep=lambda f: not is_key(f))
        m.seen = None
        for field, kind, where in triples:
            m.target, m.hits = (field, KINDS[kind], where), 0
            res = await c.call_tool(name, ARGS.get(name, {}))
            report["mutations"] += 1
            report["hits"] += bool(m.hits)
            if (err := _failure(res)) is not None:
                report["failures"][f"{field} -> {kind} @{where}"] = err
    return report


async def _cold_calls(tmp_path, name: str, m: Mutator, db: str, calls: int) -> list[str]:
    """A fresh runtime with the TTL caches on and an empty store, `calls` calls of the tool; the failures."""
    rt = _runtime(tmp_path, m, cache_scale=1.0, db=db)
    await _seed_store(rt)
    srv, _ = build_server(rt)
    out = []
    async with Client(srv) as c:
        for _ in range(calls):
            if (err := _failure(await c.call_tool(name, ARGS.get(name, {})))) is not None:
                out.append(err)
    return out


async def run_tool_cold(tmp_path, name: str) -> dict[str, Any]:
    """The cached fields one cold call reads, then per picked mutation a fresh runtime whose caches fill with it."""
    m = Mutator()
    m.seen = {}
    report: dict[str, Any] = {"mutations": 0, "hits": 0, "failures": {}}
    if errs := await _cold_calls(tmp_path, name, m, "cold_clean", 1):
        report["failures"]["clean"] = errs[0]
    triples, m.seen = mutations(m.seen, keep=is_cached, cap=MAX_COLD), None
    for i, (field, kind, where) in enumerate(triples):
        m.target, m.hits = (field, KINDS[kind], where), 0
        errs = await _cold_calls(tmp_path, name, m, f"cold_{i}", 2)  # the second call answers from the filled cache
        report["mutations"] += 1
        report["hits"] += bool(m.hits)
        if errs:
            report["failures"][f"{field} -> {kind} @{where}"] = errs[0]
    return report


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS - set(SKIP)))
async def test_no_tool_raises_on_a_wrong_type_field(tmp_path, name):
    report = await run_tool(tmp_path, name)
    assert report["hits"] == report["mutations"], "a picked field was not found again: the fakes are not deterministic"
    assert (report["mutations"] == 0) == (name in NO_NETWORK or name in NO_FIELDS), f"update NO_FIELDS: {report['mutations']} mutations"
    if name in KNOWN:
        assert report["failures"], f"{name} is fixed: remove it from KNOWN and BACKLOG 6.41"
        assert known_failures_match(report["failures"], KNOWN[name]), f"{name}: a failure outside KNOWN: {report['failures']}"
    else:
        assert report["failures"] == {}, f"{name}: fix it (type check / Builder.guard) or list it in KNOWN"


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS - set(SKIP) - NO_NETWORK))
async def test_no_tool_raises_on_a_bad_value_in_a_cold_cache(tmp_path, name):
    report = await run_tool_cold(tmp_path, name)
    assert report["hits"] == report["mutations"], "a picked field was not found again: the fakes are not deterministic"
    if name in KNOWN_COLD:
        assert report["failures"], f"{name} is fixed: remove it from KNOWN_COLD and BACKLOG 6.41"
        assert known_failures_match(report["failures"], KNOWN_COLD[name]), f"{name}: a failure outside KNOWN_COLD"
    else:
        assert report["failures"] == {}, f"{name}: fix it (type check / Builder.guard) or list it in KNOWN_COLD"


async def test_the_cold_check_reaches_both_caches(tmp_path):
    """Self-check: the coin map and ccxt markets are both broken by the cold test, so a green run covers them."""
    hl, ccxt_ = await run_tool_cold(tmp_path, "hyperliquid_trades"), await run_tool_cold(tmp_path, "market_markets")
    assert hl["mutations"] >= 2 and ccxt_["mutations"] >= 2


async def test_the_check_catches_a_tool_that_trusts_the_schema(tmp_path):
    """Self-check: a tool that does float() on a venue field without a guard is reported, so a green run means something."""
    m = Mutator()
    rt = _runtime(tmp_path, m)
    srv, _ = build_server(rt)

    @srv.tool(name="zz_schema_trusting_probe")
    async def zz_schema_trusting_probe() -> dict:
        fetched = await rt.deribit.book_summary("BTC", "future")
        return {"ok": True, "mark": float(fetched.data[-1]["mark_price"])}  # no type check, last row only

    async with Client(srv) as c:
        m.seen = {}
        assert _failure(await c.call_tool("zz_schema_trusting_probe", {})) is None
        fields, m.seen = m.seen, None
        assert fields, "the probe read no numeric field"
        field = next(f for f in sorted(fields) if f.endswith("result[*].mark_price"))
        m.target = (field, KINDS["abc"], "first")
        assert _failure(await c.call_tool("zz_schema_trusting_probe", {})) is None  # the first row is not read
        m.target = (field, KINDS["abc"], "last")
        assert (_failure(await c.call_tool("zz_schema_trusting_probe", {})) or "").startswith("raised")


def test_numeric_fields_and_break_field():
    tree = {"result": [{"p": "1.5", "s": "BTC"}, {"p": 2}, {"p": 3}], "bar": [1, 2.0, "3"], "flag": True, "inf": "inf"}
    names = [n for n, _ in numeric_fields(tree, "x")]
    assert names == ["x.result[*].p"] * 3 + ["x.bar[0]", "x.bar[1]", "x.bar[2]"]
    for where, i in WHERE.items():  # every mutation breaks a fresh body
        t = copy.deepcopy(tree)
        assert break_field(t, "x", "x.result[*].p", [], where)
        assert [r["p"] for r in t["result"]] == [[] if j == i % 3 else r["p"] for j, r in enumerate(tree["result"])]
    assert not break_field(tree, "x", "x.nope", None)
    assert not break_field({"bar": [1]}, "x", "x.bar[0]", None, "second")
    universe = {"universe": [{"name": "BTC", "szDecimals": 3}]}
    assert [n for n, _ in numeric_fields([universe, []], "http:api.hyperliquid.xyz/info")] == [
        "http:api.hyperliquid.xyz/info[*].universe[*].name",  # a string, recorded only because the coin map is built from it
        "http:api.hyperliquid.xyz/info[*].universe[*].szDecimals",
    ]
    assert is_cached("http:api.hyperliquid.xyz/info[*].universe[*].name") and is_key("http:api.hyperliquid.xyz/info[*].universe[*].name")
    assert is_cached("ccxt:bybit.load_markets.BTC/USDT:USDT.contractSize") and not is_cached("ccxt:bybit.fetch_ticker.last")


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


def test_mutations_are_capped_and_spread():
    fields = {f"f{i:02d}": 3 for i in range(40)}
    picked = mutations(fields)
    assert len(picked) == MAX_MUTATIONS and picked == mutations(fields)
    assert {k for _, k, _ in picked} == set(KINDS) and {w for _, _, w in picked} == set(WHERE)
    assert len(set(picked)) == len({f for f, _, _ in picked}) == MAX_MUTATIONS  # 40 fields, one triple each
    assert [w for _, _, w in mutations({"a": 1})] == ["first"] * 3 and len(mutations({"a": 2})) == 6
    assert {k for _, k, _ in mutations({"http:api.hyperliquid.xyz/info[*].universe[*].name": 1})} == set(KEY_KINDS)


def test_known_failures_must_all_match_the_pattern():
    pattern = r"ccxt:[^.]+\.fetch_funding_rate_history\[\*\]\.fundingRate -> \w+ @\w+"
    one = {"ccxt:bybit.fetch_funding_rate_history[*].fundingRate -> abc @last": "raised: ValueError"}
    assert known_failures_match(one, pattern)
    assert not known_failures_match({}, pattern)  # fixed: the entry is stale
    other = {**one, "ccxt:bybit.fetch_ticker.last -> abc @first": "raised: TypeError"}
    assert not known_failures_match(other, pattern)  # an unrelated violation is not hidden by the entry


class _NoCloseEx(FakeEx):
    """OHLCV rows whose close is never a usable number."""

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        return [[*x[:4], bad, *x[5:]] for x, bad in zip(rows, [None, "abc", [], 0] * len(rows), strict=False)]


async def test_market_ohlcv_without_a_numeric_close_is_not_ok(tmp_path):
    def rt_with(factory) -> Runtime:
        rt = Runtime(
            Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / f"{factory.__name__}.sqlite")),
            transports=make_transports(State()),
            exchange_factory=factory,
        )
        rt.now = lambda: NOW
        return rt

    args = {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT", "timeframe": "1h", "limit": 20}
    async with Client(build_server(rt_with(_NoCloseEx))[0]) as c:
        res = await c.call_tool("market_ohlcv", args)
    assert _failure(res) is None
    env = as_envelope(res)
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["reason"], m["detail"]) for m in env["missing"]] == [("ohlcv", "data_unavailable", "no numeric close")]
    async with Client(build_server(rt_with(FakeEx))[0]) as c:  # the happy path keeps its summary
        good = as_envelope(await c.call_tool("market_ohlcv", args))
    assert good["ok"] is True and good["data"]["summary"]["candles"] == 20 and good["data"]["summary"]["first_close"] > 0


def test_lists_name_registered_tools_only():
    assert set(KNOWN) <= EXPECTED_TOOLS and set(NO_FIELDS) <= EXPECTED_TOOLS and set(KNOWN_COLD) <= EXPECTED_TOOLS
    assert NO_NETWORK.isdisjoint(NO_FIELDS)
