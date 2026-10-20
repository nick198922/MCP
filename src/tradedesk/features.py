"""Feature switches kept in git (deploy/features.env), so the agents can turn on a read-only job without root.

The owner's /etc/tradedesk-mcp.env always wins: a key already set in the environment is never overwritten. Only
switches and tuning numbers of read-only jobs are accepted (an allow-list of names and a strict value pattern); keys,
tokens, the bind address, the transport, paths and anything about execution are ignored with a warning, so a merged
change of this file cannot expose the server or point it at a secret. This file is control plane
(scripts/automerge.py): changing these rules needs the owner.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import MutableMapping
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "features.env"

_PREFIXES = (
    "TRADEDESK_COLLECTOR_",
    "TRADEDESK_COLLECT_",
    "TRADEDESK_RETENTION_",
    "TRADEDESK_PAPER_",
    "TRADEDESK_ASSISTANT_SNAPSHOT_",
    "TRADEDESK_ALERTS_",
    "TRADEDESK_TRADERS_",
    "TRADEDESK_OHLCV_HIST_",
    "TRADEDESK_PM_GAP_",
)
_SUFFIXES = (
    "_ENABLED",
    "_SECONDS",
    "_MINUTES",
    "_HOURS",
    "_DAYS",
    "_COOLDOWN",
    "_VENUES",
    "_SYMBOLS",
    "_MARKETS",
    "_PAGES",
    "_TOP_N",
    "_USD",
    "_INST_TYPE",
)
_DENY = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PATH", "HOST", "PORT", "URL", "EXECUTION", "BEARER")
_NAME = re.compile(r"^TRADEDESK_[A-Z0-9_]+$")
_VALUE = re.compile(r"^[A-Za-z0-9_.,:/-]{0,300}$")

log = logging.getLogger("tradedesk.features")


def allowed(name: str) -> bool:
    if not _NAME.match(name) or any(word in name for word in _DENY):
        return False
    return name.startswith(_PREFIXES) and name.endswith(_SUFFIXES)


def parse(text: str) -> tuple[dict[str, str], list[str]]:
    """(accepted switches, rejected lines with the reason)."""
    out: dict[str, str] = {}
    rejected: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep:
            rejected.append(f"line {n}: not NAME=value")
        elif not allowed(name):
            rejected.append(f"line {n}: {name} is not a feature switch")
        elif not _VALUE.match(value) or ("/" in value and not name.endswith(("_VENUES", "_SYMBOLS", "_MARKETS"))):
            rejected.append(f"line {n}: {name} has an unsafe value")
        else:
            out[name] = value
    return out, rejected


def load(path: Path = DEFAULT_PATH, env: MutableMapping[str, str] = os.environ) -> dict[str, str]:
    """Apply the switches that the environment does not set yet; returns what was applied."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    switches, rejected = parse(text)
    for why in rejected:
        log.warning("features.env ignored: %s", why)
    applied = {k: v for k, v in switches.items() if k not in env}
    env.update(applied)
    if applied:
        log.info("features.env: %s", ", ".join(f"{k}={v}" for k, v in sorted(applied.items())))
    return applied
