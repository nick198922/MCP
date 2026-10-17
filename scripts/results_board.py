"""Results board: one page with every idea card and its verdict, generated from docs/ideas/ (owner's decision 2026-10-17).

    uv run python scripts/results_board.py [--ideas docs/ideas] [--out docs/RESULTS.md]

Each card's «Статус:» line gives the status and the short verdict; the first report under docs/verification/ the card
links is shown next to it. Groups: candidates (confirmed / built), in work (accepted / testing / proposed),
inconclusive, rejected. Run it whenever a card's status changes and stage the result with the card.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

GROUPS = [
    ("✅ Підтверджено або побудовано", ("confirmed", "built")),
    ("🔄 У роботі", ("testing", "accepted", "proposed")),
    ("◯ Без вердикту", ("inconclusive",)),
    ("✗ Відхилено", ("rejected",)),
]
# Results from before the card system (2026-10-12/13): positive backtests that wait for a forward paper test.
EARLY = [
    (
        "Funding carry на альтах (cash-and-carry, 5 слотів, вхід ≥ 20 % APR)",
        "+6.5 % на номінал за 2 роки, ≈ 3–5 % на капітал",
        "2026-10-12-funding-carry-alts",
    ),
    (
        "Carry перп-перп Binance ↔ Bybit",
        "+76…+90 % на номінал за 2 роки, лише хайпові лістинги; медіанна угода ≈ 0",
        "2026-10-13-funding-carry-perp-perp",
    ),
    ("VRP BTC (короткий стреддл з хеджем)", "≈ +2 % від спота на місяць на DVOL; на реальних марках не значущий", "2026-10-13-vrp"),
    ("SFP / свипи рейнджів (відео DoubleTop)", "✗ після витрат, і поза вибіркою", "2026-10-12-doubletop-ranges-sfp"),
]
STATUS_RE = re.compile(r"^Статус:[ \t]*([a-z]+)[ \t]*(.*)$", re.M)
TITLE_RE = re.compile(r"^#\s*IDEA-\d{8}-[a-z0-9-]+:\s*(.+)$", re.M)
REPORT_RE = re.compile(r"(?:\.\./)?(?:docs/)?verification/([0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+)\.md")


def parse_card(text: str, name: str) -> dict:
    """Status, short verdict, title, owner priority and order, first linked report of one card."""
    m = STATUS_RE.search(text)
    status, rest = (m.group(1), m.group(2)) if m else ("?", "")
    verdict = re.sub(r"^[\s—–-]+", "", rest).strip()
    verdict = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", verdict)
    if len(verdict) > 230:
        verdict = verdict[:229].rsplit(" ", 1)[0] + " …"
    t = TITLE_RE.search(text)
    order = re.search(r"^Порядок:\s*(\d+)\s*$", text, re.M)
    rep = REPORT_RE.search(text)
    return {
        "name": name,
        "title": (t.group(1).strip() if t else name)[:110],
        "status": status,
        "verdict": verdict,
        "owner": bool(re.search(r"^Пріоритет:\s*власник", text, re.M)),
        "order": int(order.group(1)) if order else None,
        "report": rep.group(1) if rep else "",
    }


def render(cards: list[dict], today: date) -> str:
    counts = {g: sum(c["status"] in sts for c in cards) for g, sts in GROUPS}
    out = [
        "# Дошка результатів",
        "",
        f"Згенеровано {today.isoformat()} скриптом `scripts/results_board.py` з карток `docs/ideas/`. Не редагувати вручну.",
        "",
        " · ".join(f"{g}: **{n}**" for g, n in counts.items()),
        "",
    ]
    for group, statuses in GROUPS:
        rows = [c for c in cards if c["status"] in statuses]
        if not rows:
            continue
        if statuses[0] == "testing":
            rows.sort(key=lambda c: (not c["owner"], c["order"] if c["order"] is not None else 99, c["name"]))
        else:
            rows.sort(key=lambda c: c["name"], reverse=True)
        out += [f"## {group}", "", "| Картка | Суть | Статус і вердикт | Звіт або джерело |", "|---|---|---|---|"]
        for c in rows:
            tag = " 👤" + (f" #{c['order']}" if c["order"] is not None else "") if c["owner"] else ""
            link = f"[{c['report']}](verification/{c['report']}.md)" if c["report"] else "—"
            verdict = (c["status"] + (": " + c["verdict"] if c["verdict"] else "")).replace("|", "/")
            out.append(f"| [{c['name']}](ideas/{c['name']}.md){tag} | {c['title'].replace('|', '/')} | {verdict} | {link} |")
        out.append("")
    out += ["## Результати до карток (бектести 12–13.10)", "", "| Що | Результат | Звіт |", "|---|---|---|"]
    out += [f"| {w} | {r} | [{rep}](verification/{rep}.md) |" for w, r, rep in EARLY]
    out.append("")
    out.append("Перші три — кандидати на форвард-папір (картки `forward-paper-carry`, `vrp-real-marks`).")
    out.append("")
    out.append("👤 — пріоритет власника, #N — порядок у черзі. Підтвердженим вважається лише `confirmed` після рев'ю критика.")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ideas", type=Path, default=Path("docs/ideas"))
    ap.add_argument("--out", type=Path, default=Path("docs/RESULTS.md"))
    a = ap.parse_args()
    cards = [parse_card(p.read_text(encoding="utf-8"), p.stem) for p in sorted(a.ideas.glob("IDEA-*.md"))]
    a.out.write_text(render(cards, date.today()), encoding="utf-8")
    print(f"{len(cards)} cards -> {a.out}")


if __name__ == "__main__":
    main()
