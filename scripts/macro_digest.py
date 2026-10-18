"""Macro digest for Telegram: weekly overview of high-impact events and "starts soon" reminders.

    uv run python scripts/macro_digest.py week   [--dry-run] [--out FILE] [--data-dir DIR]
    uv run python scripts/macro_digest.py remind [--dry-run] [--window-min 60] [--data-dir DIR]

Events come from the ForexFactory provider (through core.http, read-only) merged with the bundled seed by
``analytics/macro_calendar.py``; the texts are built by the pure ``analytics/macro_digest.py``. Sending reuses
``scripts/tg_send.py`` (token and chat id from ~/.config/tradedesk/agents.env, never printed) into the group's ``alerts``
topic. ``--dry-run`` prints instead of sending. ``week`` also saves the fetched weekly feed rows as
``DIR/ff_thisweek_YYYY-MM-DD.json`` (default DIR ``/var/lib/tradedesk-mcp/macro``, about 20 KB a week) so forecast history
accumulates; ``remind`` keeps the keys of reminders already sent in ``DIR/reminders_sent.json`` (trimmed to 500).
Feed text is untrusted: it is data only and is shown, never acted on. No trading code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tg_send import md_to_html, send  # noqa: E402

from tradedesk.analytics.macro_calendar import build_macro_calendar  # noqa: E402
from tradedesk.analytics.macro_digest import (  # noqa: E402
    due_reminders,
    event_key,
    format_reminder,
    format_week_digest,
)
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.runtime import Runtime  # noqa: E402
from tradedesk.tools.macro import _load_seed  # noqa: E402

DEFAULT_DATA_DIR = "/var/lib/tradedesk-mcp/macro"
SENT_KEEP = 500


async def fetch() -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime, list[str]]:
    """(this-week rows, all feed rows, now, notes). Notes name provider failures."""
    rt = Runtime()
    notes: list[str] = []
    try:
        now = rt.now()
        this_week: list[dict[str, Any]] = []
        next_week: list[dict[str, Any]] = []
        for label, call, target in (("thisweek", rt.forexfactory.this_week, this_week), ("nextweek", rt.forexfactory.next_week, next_week)):
            try:
                target.extend((await call()).data)
            except ProviderError as e:
                if label == "nextweek" and this_week:
                    continue  # nextweek 404s until mid-week; thisweek already covers the digest
                notes.append(f"ForexFactory {label} недоступний ({e.reason})")
        return this_week, [*this_week, *next_week], now, notes
    finally:
        await rt.aclose()


def save_week(data_dir: Path, rows: list[dict[str, Any]], now: datetime) -> Path | None:
    if not rows:
        return None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"ff_thisweek_{now:%Y-%m-%d}.json"
        path.write_text(json.dumps({"fetched_utc": now.isoformat(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
        return path
    except OSError as e:
        print(f"could not save the weekly feed: {e}", file=sys.stderr)
        return None


def load_sent(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(k) for k in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def deliver(text: str, dry_run: bool) -> bool:
    if dry_run:
        print(text)
        return True
    return send(md_to_html(text), topic="alerts")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["week", "remind"])
    ap.add_argument("--dry-run", action="store_true", help="print the text instead of sending it (nothing is saved either)")
    ap.add_argument("--out", help="also write the text to this file")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--window-min", type=int, default=60, help="remind: events starting within this many minutes")
    a = ap.parse_args(argv)
    data_dir = Path(a.data_dir)

    this_week, feed, now, notes = asyncio.run(fetch())
    if not feed:
        print("macro digest: no feed rows (" + "; ".join(notes) + ")", file=sys.stderr)
        if a.mode == "week" and not a.dry_run:
            send(md_to_html("# Макро-тиждень\n\nКалендар ForexFactory недоступний, огляд не сформовано."), topic="alerts")
        return 1

    try:
        seed = _load_seed()
    except (FileNotFoundError, ValueError, OSError):
        seed = []
    events = build_macro_calendar(feed, seed, now, 14)["events"]

    if a.mode == "week":
        text = format_week_digest(events, now, notes)
        if not a.dry_run:
            saved = save_week(data_dir, this_week, now)
            print(f"saved {saved}" if saved else "weekly feed not saved")
    else:
        sent_path = data_dir / "reminders_sent.json"
        sent = load_sent(sent_path)
        due = due_reminders(events, now, a.window_min, set(sent))
        maybe = format_reminder(due, now)
        if maybe is None:
            print("nothing to remind")
            return 0
        text = maybe
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    ok = deliver(text, a.dry_run)
    if ok and a.mode == "remind" and not a.dry_run:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            keys = [*sent, *(event_key(e) for e in due)][-SENT_KEEP:]
            tmp = sent_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(keys), encoding="utf-8")
            os.replace(tmp, sent_path)
        except OSError as e:
            print(f"could not save sent reminders: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
