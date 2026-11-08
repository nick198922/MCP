"""One JSON object per log line, for `journalctl -o cat | jq` (switched on by TRADEDESK_LOG_FORMAT=json).

Only a fixed set of fields leaves the process: ts, level, logger, msg, the allow-listed `extra` fields and the
traceback. Arbitrary `extra` keys and the environment are never dumped, and the text fields go through the same
`redact` that keeps secrets out of `system_health`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from .core.health import redact

# `extra={...}` keys that are copied into the JSON line; anything else passed as `extra` is ignored.
EXTRA_FIELDS = ("job", "error")
_TEXT_LIMIT = 20_000  # redact() truncates; keep whole messages and tracebacks, only cap pathological ones


def _clean(text: str) -> str:
    return redact(text, limit=_TEXT_LIMIT)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _clean(record.getMessage()),
        }
        for key in EXTRA_FIELDS:
            value = record.__dict__.get(key)
            if value is not None:
                out[key] = _clean(str(value))
        if record.exc_info:
            out["exc"] = _clean(self.formatException(record.exc_info))
        elif record.exc_text:
            out["exc"] = _clean(record.exc_text)
        if record.stack_info:
            out["stack"] = _clean(self.formatStack(record.stack_info))
        return json.dumps(out, ensure_ascii=False)
