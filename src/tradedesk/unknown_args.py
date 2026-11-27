"""Argument names a tool does not have (backlog 27.11, review docs/reviews/2026-11-27-coinalyze-bybit-check.md).

The MCP SDK builds each tool's argument model without ``extra="forbid"``, so a key the tool does not have is dropped
without a word: ``perps_funding_history(symbol="ETH")`` (the parameter is ``base``) answered with the BTC default series.
``DeskServer.call_tool`` (server.py) checks the keys of every call against the tool's ``inputSchema.properties``: a
read tool still runs and its envelope gets a warning first in ``warnings``; a tool whose arguments shape a row written to
the local store (STORE_WRITE_TOOLS) is refused, nothing runs and the envelope is ok=false. The names are the caller's
text: control and format characters are stripped, each name is cut to MAX_NAME_CHARS and at most MAX_SHOWN_NAMES are
echoed. Pure functions, no I/O.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from .core.envelope import Builder

MAX_SHOWN_NAMES = 10
MAX_NAME_CHARS = 40

# Tools whose arguments become a row in the local store. Every tool carries readOnlyHint=True (the writes stay in the
# local SQLite, no external effect), so the annotation cannot tell these apart. Same set as WRITE_TOOLS of
# scripts/mcp_call.py; tests/test_unknown_args.py keeps both in step with the registered tools.
STORE_WRITE_TOOLS = frozenset(
    {
        "journal_add",
        "journal_close",
        "positions_add",
        "positions_close",
        "rules_add",
        "rules_disable",
        "plan_add",
        "plan_link",
        "events_add",
        "assistant_paper_open",
    }
)


def schema_params(input_schema: Any) -> list[str]:
    """Parameter names of a tool's JSON input schema (its top-level ``properties``), sorted; [] when it has none."""
    props = input_schema.get("properties") if isinstance(input_schema, Mapping) else None
    return sorted(str(k) for k in props) if isinstance(props, Mapping) else []


def unknown_keys(arguments: Mapping[Any, Any], params: Iterable[str]) -> list[Any]:
    """Keys of `arguments` that are not in `params`, in the caller's order."""
    known = set(params)
    return [k for k in arguments if k not in known]


def shown_name(key: Any) -> str:
    """`key` safe to echo: control / format / separator characters (newlines, bidi overrides, NUL) removed, cut to
    MAX_NAME_CHARS, quoted. A non-string key (a direct in-process call) is shown by its type only."""
    if not isinstance(key, str):
        return f"<{type(key).__name__} key>"
    clean = "".join(c for c in key[: MAX_NAME_CHARS * 4] if not unicodedata.category(c).startswith(("C", "Z")) or c == " ")
    cut = clean[:MAX_NAME_CHARS] + ("…" if len(clean) > MAX_NAME_CHARS else "")
    return f"'{cut}'"


def shown_names(keys: list[Any]) -> str:
    """At most MAX_SHOWN_NAMES of `keys` via `shown_name`, then how many more there were."""
    head = ", ".join(shown_name(k) for k in keys[:MAX_SHOWN_NAMES])
    rest = len(keys) - MAX_SHOWN_NAMES
    return head + (f" and {rest} more" if rest > 0 else "")


def valid_text(params: list[str]) -> str:
    return ", ".join(params) if params else "none (this tool takes no arguments)"


def unknown_warning(tool: str, unknown: list[Any], params: list[str]) -> str:
    """The warning a read tool's envelope gets when it ran with keys it does not have."""
    return (
        f"unknown argument name(s) ignored: {shown_names(unknown)}. {tool} has no such parameter, so the key was dropped "
        f"and the tool ran without it: defaults may have applied where a value was meant (e.g. BTC instead of the asset "
        f"asked for). Valid parameters: {valid_text(params)}."
    )


def refused_envelope(tool: str, unknown: list[Any], params: list[str]) -> dict[str, Any]:
    """ok=false envelope for a store-writing tool called with unknown keys: nothing ran, nothing was written."""
    b = Builder()
    b.miss("arguments", "tradedesk", "invalid_input", f"{len(unknown)} unknown argument name(s), call refused; see warnings")
    b.warn(
        f"unknown argument name(s): {shown_names(unknown)}. {tool} writes to the local store, so the call was refused "
        f"and nothing was written (a dropped key would have stored a row with its default instead). Valid parameters: "
        f"{valid_text(params)}."
    )
    return b.build(None, ok=False)
