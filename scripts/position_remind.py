"""One-off reminder about the owner's open Bybit option legs: their P&L now and his exit plan, to Telegram. Read-only.

    uv run python scripts/position_remind.py --symbols BTC-13NOV26-83000-C-USDT,BTC-13NOV26-78000-P-USDT \
        --title "Стренгл 13NOV26 (журнал #419, #420)" --plan "..." --cost 314.21 | python3 scripts/tg_send.py --topic chat

Reads the positions file of scripts/bybit_options_positions.py (refreshed hourly by tradedesk-bybit-options.timer;
no key here, no request) through analytics.my_options.my_options_view, keeps the named legs and prints a light-markdown
message: each leg's mark and unrealised P&L, the total against the entry cost (premium + fees, given by the caller),
days left, and the plan. A leg no longer on Bybit is named as closed. Nothing places, changes or cancels an order.
Never raises: an unusable file is said in the message, so the reminder still arrives.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradedesk.analytics.my_options import my_options_view  # noqa: E402

POSITIONS_DEFAULT = Path("/var/lib/tradedesk-mcp/assistant/my_options.json")


def _kyiv(stamp: Any) -> str:
    try:
        t = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return "—"
    return t.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m %H:%M") + " за Києвом"


def _n(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _usd(x: float) -> str:
    return f"{'+' if x > 0 else '−' if x < 0 else ''}${_n(abs(x))}"


def message(state: Any, now: datetime, symbols: list[str], title: str, plan: str, cost: float | None) -> str:
    """The reminder text (light markdown for tg_send). Pure."""
    out = [f"**Нагадування: {title}**", f"План: {plan}", ""]
    data, why = my_options_view(state, now)
    if data is None:
        return "\n".join([*out, f"Позиції з Bybit прочитати не вдалося: {why}. Глянь у застосунку."])
    by = {p["symbol"]: p for p in data["positions"]}
    total, found = 0.0, 0
    for sym in symbols:
        p = by.get(sym)
        if p is None:
            out.append(f"• `{sym}` — на Bybit уже немає (закрита?)")
            continue
        found += 1
        total += p["upnl_usd"]
        out.append(f"• `{sym}`: вхід ${_n(p['avg_price'])}, марк ${_n(p['mark_price'])}, P&L {_usd(p['upnl_usd'])}")
    if found:
        line = f"Разом відкриті ноги: {_usd(total)}"
        if cost:
            line += f" ({total / cost * 100:+.0f} % від вартості входу ${_n(cost)} з комісіями)".replace("-", "−")
        days = min(by[s]["days"] for s in symbols if s in by)
        fwd = next((by[s]["forward"] for s in symbols if s in by and by[s].get("forward")), None)
        out += ["", line, f"До експірації {days:.1f} дн" + (f", форвард ${_n(fwd)}" if fwd else "") + "."]
    out += ["", f"Марки Bybit на {_kyiv(data.get('asof_utc'))}. Закриєш — напиши тут ціни виходу, оновлю журнал."]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", required=True, help="comma-separated Bybit option symbols")
    ap.add_argument("--title", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--cost", type=float, help="entry cost in USD (premium + fees) for the percentage")
    ap.add_argument("--positions", type=Path, default=POSITIONS_DEFAULT)
    a = ap.parse_args(argv)
    try:
        state = json.loads(a.positions.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    print(message(state, datetime.now(UTC), [s.strip() for s in a.symbols.split(",") if s.strip()], a.title, a.plan, a.cost))
    return 0


if __name__ == "__main__":
    sys.exit(main())
