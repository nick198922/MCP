"""MCP server entry point.

tradedesk-mcp                        # stdio (Claude Desktop / Claude Code, local)
TRADEDESK_TRANSPORT=streamable-http TRADEDESK_PORT=8000 tradedesk-mcp    # always-on server
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult

from . import __version__, features
from .http_auth import BearerAuthMiddleware, parse_tokens
from .logfmt import JsonFormatter
from .runtime import Runtime
from .tools import register_all
from .tools._util import STORE_ERRORS
from .unknown_args import STORE_WRITE_TOOLS, refused_envelope, schema_params, unknown_keys, unknown_warning

log = logging.getLogger("tradedesk.server")

INSTRUCTIONS = """\
Personal trading desk for crypto derivatives, options and quant tooling. Read-only market data.

Conventions: IV and rates are DECIMALS (0.45 = 45 %); funding is annualised as a simple fraction;
prices in USD unless a field says otherwise. Every response is an envelope {ok, data, evidence, missing, warnings, as_of}:
`evidence` shows the source and fetch time of each number, `missing` lists anything a provider could not supply and why.
Probabilities derived from options are risk-neutral, not forecasts. If results look empty, call system_health(probe=true).
Typical flow: deribit_vol_surface -> deribit_option_chain / prob_from_options; perps_funding_compare -> perps_open_interest;
deribit_futures_curve for basis.
"""


class DeskServer(MCPServer):
    """MCPServer that does not drop an unknown argument name silently (see unknown_args.py).

    The SDK's protocol handler (``_handle_call_tool``: every Client, stdio, streamable-http, in-memory) and a direct
    ``server.call_tool(...)`` both come through ``call_tool``. A read tool runs as before and gets a warning first in
    ``warnings``; a STORE_WRITE_TOOLS tool is refused (ok=false envelope, nothing runs). A call with known keys only goes
    straight to the SDK, unchanged. The lookup uses the SDK's ``_tool_manager`` and ``Tool.fn_metadata.convert_result``;
    tests/test_unknown_args.py fails if either stops working."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        tool, params, unknown = None, [], []
        try:
            tool = self._tool_manager.get_tool(name)
            if tool is not None and isinstance(arguments, Mapping):
                params = schema_params(tool.parameters)
                unknown = unknown_keys(arguments, params)
        except Exception:  # noqa: BLE001 - the check must never cost a call its answer
            log.exception("unknown-argument check failed for tool %r; the call runs unchecked", name)
            unknown = []
        if not unknown or tool is None:
            return await super().call_tool(name, arguments, context)
        if name in STORE_WRITE_TOOLS:
            env = refused_envelope(name, unknown, params)
            try:
                return tool.fn_metadata.convert_result(env)
            except Exception as e:  # noqa: BLE001 - still refused, as an error result instead of an envelope
                raise ToolError(f"Error executing tool {name}: {env['warnings'][0]}") from e
        res = await super().call_tool(name, arguments, context)
        sc = res.structured_content if isinstance(res, CallToolResult) and not res.is_error else None
        if not (isinstance(sc, dict) and isinstance(sc.get("warnings"), list)):
            log.warning("tool %r ran with unknown argument names but its result has no envelope warnings", name)
            return res
        try:
            return tool.fn_metadata.convert_result({**sc, "warnings": [unknown_warning(name, unknown, params), *sc["warnings"]]})
        except Exception:  # noqa: BLE001 - the tool's own answer is still returned
            log.exception("could not add the unknown-argument warning to %r", name)
            return res


def build_server(rt: Runtime | None = None) -> tuple[MCPServer, Runtime]:
    rt = rt or Runtime()

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        from .collector import build_default_scheduler  # noqa: PLC0415

        store_ok = True
        if rt.store is not None:
            try:
                await rt.store.init()
            except STORE_ERRORS as e:  # a broken or locked DB file must not cost the session its market tools
                store_ok = False
                logging.getLogger("tradedesk.store").error(
                    "store init failed, collector not started; store tools will report missing: store (%s: %s)", type(e).__name__, e
                )
        # Every collector job writes to the store (build_default_scheduler returns None without one): none on a failed init.
        rt.scheduler = build_default_scheduler(rt) if store_ok else None
        if rt.scheduler is not None:
            await rt.scheduler.start()
            logging.getLogger("tradedesk.collector").info("collector started with %d jobs", len(rt.scheduler.jobs))
        try:
            yield
        finally:
            if rt.scheduler is not None:
                await rt.scheduler.stop()
            await rt.aclose()

    server = DeskServer("tradedesk", instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)
    register_all(server, rt)
    return server, rt


def json_logs() -> bool:
    return os.environ.get("TRADEDESK_LOG_FORMAT", "").strip().lower() == "json"


def configure_logging() -> None:
    # stderr only: in stdio mode stdout carries the MCP protocol.
    if json_logs():
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO; the Telegram URL carries the bot token, so keep it out of the journal.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def is_loopback(host: str) -> bool:
    """True only for an address reachable from this machine alone (127.0.0.0/8, ::1, localhost).

    0.0.0.0 / :: (all interfaces), LAN addresses, names other than localhost and an empty string are not loopback."""
    h = host.strip().lower().removeprefix("[").removesuffix("]")
    if h in ("localhost", "localhost."):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # same answer on every Python (3.13 changed is_loopback for mapped addresses)
    return ip.is_loopback


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    features.load()
    server, rt = build_server()
    s = rt.settings
    if s.transport != "streamable-http":
        server.run(s.transport)  # type: ignore[arg-type]
        return

    import uvicorn  # noqa: PLC0415

    app = server.streamable_http_app(host=s.host)
    tokens = parse_tokens(s.http_bearer_tokens)
    if tokens:
        app = BearerAuthMiddleware(app, tokens)
        logging.getLogger("tradedesk.auth").info("bearer auth enabled (%d token(s))", len(tokens))
    else:
        logging.getLogger("tradedesk.auth").warning(
            "streamable-http running WITHOUT auth; bind to 127.0.0.1 or set TRADEDESK_HTTP_BEARER_TOKEN"
        )
    if not is_loopback(s.host):  # warn, do not refuse: the owner may bind on purpose behind a proxy
        logging.getLogger("tradedesk.auth").warning(
            "streamable-http binds to non-loopback host %r: the MCP server is reachable from the network "
            "(auth %s); the default is 127.0.0.1",
            s.host,
            "enabled" if tokens else "DISABLED",
        )
    if json_logs():  # no uvicorn handlers: its records propagate to the root JSON handler, so every line is JSON
        uvicorn.run(app, host=s.host, port=s.port, log_level="info", log_config=None)
    else:
        uvicorn.run(app, host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
