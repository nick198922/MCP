"""Project dashboard: one web page with what the agents do now, their queue and results, plan usage and the server.

    python3 scripts/dashboard.py serve [--port 8090]      # on the server (tradedesk-dashboard.service, 127.0.0.1 only)
    python3 scripts/dashboard.py once --out page.html     # one page, for a check

Read-only and model-free: built from the same files as the Telegram status (scripts/agents_status.py), the idea cards
on origin/main (scripts/results_board.py), the agents' run history and live transcript, the SQLite store (read-only)
and /proc. The page refreshes itself every minute; /status.json gives the same data to scripts. The server listens on
127.0.0.1 only: the owner opens it through an SSH tunnel (scripts/dashboard-open.sh). No secret is ever put on the page:
the plan usage is read with the agents' login, only percentages and reset times are kept.

The «Бриф» tab (/brief) renders the assistant's morning brief from the snapshot JSON written by the collector job
``assistant_snapshot`` (no network here: the collector does all fetching). Every string from that file is untrusted
text and is escaped; a missing, stale or corrupt snapshot gives an empty state, never an error.

The «Опціони» tab (/options) is a strategy builder on a snapshot chain (marks, Δ, IV) of the month expiry or of the
nearest one at least 25 h away (``chain_near``, where rule 8's 1-3 day zone and its thesis / target fields show up): sell/buy strangle and
straddle, iron condor; P&L at expiry, breakevens, max profit/loss, risk-neutral probability of profit and the fit to the
owner's risk budget and his rules 1 and 8 (analytics.rules_check); position greeks, a «through N days» line, a scenario table (incl. IV +5 points), a plain-language
conclusion and an «is vol expensive» block (DVOL percentile, VRP backtest by level). Everything is computed here on a plain GET form; the inline script only remembers deposit, risk
and size in the browser (localStorage) and resubmits on a change.

The «Радар» tab (/radar?asset=BTC|ETH) renders the snapshot's ``radar`` section (analytics.radar.radar_view): five
«current situation» lines, price, ATM IV 7/30/90/180, IV Rank, IV vs RV, 25-delta skew, 30-day smile, a forward-vol
chart with events and rule-based «what it means for trades» (hypotheses, not advice), each block with its own «week
ago» dates (surface rows vs daily DVOL). Null fields show their reason from the section's ``missing``.

The «Журнал» tab (/journal) reads the owner's trade journal and the assistant's paper recommendations (rec_paper,
labelled «гіпотеза») from the SQLite store over a read-only connection: summary, the cumulative R curve and the weekly
review by ISO week of the close (analytics.journal.week_stats copies), by source, the trades page by page, the owner's
rule violations by ISO week (analytics.rules_check), and a summary of the assistant's trade plans
(trade_plan: counts by verdict and status, the resolved 'enter' plans' R).
Every string from the database is untrusted text and is escaped; no database or no table gives an empty state.

The «Демо» tab (/demo) reads the demo trader's book (demo_trade) and its 'refused' / 'skipped' decisions (exec_audit)
over a read-only connection: counts, R by setup with a t 95 % CI against 0 («замало для висновку» under 30 per setup),
the latest trades and the refusals by reason. The «demo vs backtest» replay needs candles, so only its whereabouts
(the weekly Telegram message) is shown. Symbols and reasons from the database are untrusted text and are escaped.

The «Polymarket» tab (/polymarket) lists the CURRENT Polymarket <-> Deribit-options gaps of the latest pm_gap_snapshot
that pass the paper test's registered entry filters (net edge >= 5 pp; a collapsed 3-5 pp table), with R:R and the size
for a $100 risk: a hint for manual trades. It reads the store read-only and does not feed the paper test.
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agents_status as st  # noqa: E402
import results_board as rb  # noqa: E402

HOME = Path.home()
REPO = Path(os.environ.get("TRADEDESK_REPO", HOME / "tradedesk-mcp"))
WORKTREE = Path(os.environ.get("TRADEDESK_AGENTS_WORKTREE", HOME / "tradedesk-agents"))
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR", HOME / ".local/state/tradedesk-agents"))
DATA_DIR = Path(os.environ.get("TRADEDESK_DATA_DIR", "/var/lib/tradedesk-mcp"))
STATE = Path(os.environ.get("TRADEDESK_DASHBOARD_STATE", HOME / ".local/state/tradedesk-dashboard"))
MAX_USAGE = float(os.environ.get("TRADEDESK_AGENTS_MAX_USAGE", "97"))
OWNER_ONLY_AT = float(os.environ.get("TRADEDESK_AGENTS_OWNER_ONLY_AT", "80"))
REFRESH_S = 60
FETCH_EVERY_S = 300
# (table, timestamp column, label, newest row expected within this many seconds)
TABLES = [
    ("funding_hist", "ts", "funding", 3 * 3600),
    ("basis_hist", "ts", "basis", 3 * 3600),
    ("atm_term", "ts", "Deribit ATM-строки", 3 * 3600),
    ("liquidations", "ts", "ліквідації (WebSocket)", 3600),
    ("ohlcv_hist", "ts", "свічки (історія)", 6 * 3600),
    ("trader_positions", "ts", "позиції трейдерів", 6 * 3600),
    ("trader_snapshot", "day", "лідерборди (денні)", 2 * 86400),
    ("supply_snapshot", "day", "пропозиція токенів (денна)", 2 * 86400),
    ("carry_paper", "opened_ts", "паперовий carry", None),
]
SERVICES = [
    ("tradedesk-mcp", "MCP"),
    ("tradedesk-agents-loop", "цикл агентів"),
    ("tradedesk-tgbridge", "Telegram-міст"),
    ("tradedesk-dashboard", "панель"),
]
STATUS_UA = {
    "delivered": "здано",
    "continues": "проміжний",
    "parked": "відкладено",
    "unfinished": "не завершено",
    "stuck": "застряг",
    "wip": "незбережено",
    "error": "збій",
    "idle": "немає роботи",
    "dry": "пробний",
}


# ---------------------------------------------------------------- pure helpers (tested)


def usage_eta(samples: list[tuple[float, float]], now: float, limit: float) -> dict:
    """Weekly usage trend from (unix time, %) samples since the last reset: rate in points a day and when ``limit`` is
    reached at that rate (None when the rate is flat or the limit is already reached)."""
    pts = sorted(samples)
    start = 0
    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1] - 5:  # the window reset
            start = i
    pts = [p for p in pts[start:] if p[0] >= now - 86400] or pts[start:][-2:]
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1800:
        return {"rate_per_day": None, "eta": None}
    rate = (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]) * 86400
    last = pts[-1][1]
    eta = None if rate <= 0.05 or last >= limit else pts[-1][0] + (limit - last) / rate * 86400
    return {"rate_per_day": round(rate, 1), "eta": eta}


def history_rows(lines: list[str], n: int = 15) -> list[dict]:
    """The last ``n`` agent step outcomes from history.tsv, newest first: time, status, text, cost."""
    out = []
    for line in lines[-n:][::-1]:
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        try:
            when = datetime.strptime(cols[0], "%Y-%m-%d_%H%M%S").strftime("%d.%m %H:%M")
        except ValueError:
            when = cols[0]
        text = re.sub(r"\*\*|`", "", cols[2])
        out.append({"when": when, "status": cols[1], "text": text[:180], "cost": (cols[3] if len(cols) > 3 else "")[:90]})
    return out


def delivered_pace(lines: list[str], now: float, days: int = 7) -> float:
    """Agent steps delivered per day over the last ``days`` days of history.tsv."""
    since = datetime.fromtimestamp(now - days * 86400).strftime("%Y-%m-%d_%H%M%S")
    n = sum(1 for ln in lines if ln.split("\t", 2)[0] >= since and ln.split("\t", 2)[1:2] == ["delivered"])
    return n / days


def project_progress(cards: list[dict], progress_md: str, pace: float) -> dict:
    """Overall progress: the owner's cards (their «## Прогрес» steps) plus the product milestones of docs/PROGRESS.md.

    Every step weighs the same; the total grows whenever a new card or step is added, so the percentage can fall."""
    owner = [c for c in cards if c["owner"]]
    cd = sum(rb.card_progress(c)[0] for c in owner)
    ct = sum(rb.card_progress(c)[1] for c in owner)
    groups = [{"label": "Дослідження й побудова (картки власника)", "done": cd, "total": ct}]
    groups += [{"label": h, "done": d, "total": t} for h, d, t in rb.checklists(progress_md)]
    done, total = sum(g["done"] for g in groups), sum(g["total"] for g in groups)
    left = ct - cd
    return {
        "done": done,
        "total": total,
        "pct": 100.0 * done / total if total else 0.0,
        "groups": groups,
        "cards_left": left,
        "pace": pace,
        "eta_days": left / pace if pace > 0 else None,
    }


def table_freshness(db: Path, now: float) -> list[dict]:
    """Newest row and an approximate row count (max rowid) per collector table, read-only."""
    if not db.exists():
        return []
    out = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        for table, col, label, expect in TABLES:
            try:
                newest, rows = con.execute(f"SELECT max({col}), max(rowid) FROM {table}").fetchone()  # noqa: S608
            except sqlite3.Error:
                continue
            ts = None
            if isinstance(newest, str):
                try:
                    ts = datetime.strptime(newest[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()  # noqa: UP017 - runs on the system python3
                except ValueError:
                    ts = None
            elif newest:
                ts = newest / 1000 if newest > 1e12 else float(newest)
            age = now - ts if ts else None
            ok = None if expect is None or age is None else age <= expect
            out.append({"label": label, "table": table, "rows": rows or 0, "age_s": age, "ok": ok})
    finally:
        con.close()
    return out


def fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return st.ago(max(0.0, seconds))


# ---------------------------------------------------------------- collectors (read-only I/O)


def _git(*args: str) -> str:
    return st._git(REPO, *args)


USAGE_CACHE = LOG_DIR / "usage.json"  # the agents' last good answer (scripts/agents_usage.sh)
USAGE_OWN_EVERY_S = 900  # the endpoint rate-limits: call it ourselves at most every 15 minutes
_usage_last_call = [0.0]


def _usage_from(data: dict) -> dict:
    out = {}
    for k in ("five_hour", "seven_day"):
        v = data.get(k) or {}
        out[k] = {"pct": v.get("utilization"), "resets_at": v.get("resets_at")}
    return out


def read_usage(now: float | None = None) -> dict | None:
    """Plan usage from the agents' cache when under 20 minutes old, else from the endpoint at most every 15 minutes
    (the answer is cached for the agents too); None when neither is available."""
    now = now or time.time()
    try:
        cached = json.loads(USAGE_CACHE.read_text())
        age = now - USAGE_CACHE.stat().st_mtime
    except (OSError, ValueError):
        cached, age = None, 1e9
    if cached and age < 1200 and (cached.get("seven_day") or {}).get("utilization") is not None:
        return _usage_from(cached)
    if now - _usage_last_call[0] < USAGE_OWN_EVERY_S:
        return _usage_from(cached) if cached and age < 3600 else None
    _usage_last_call[0] = now
    try:
        token = json.loads((HOME / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if (data.get("seven_day") or {}).get("utilization") is None:
            raise ValueError("no utilization")
        tmp = USAGE_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(USAGE_CACHE)
        return _usage_from(data)
    except Exception:  # noqa: BLE001 - usage is optional on the page
        return _usage_from(cached) if cached and age < 3600 else None


def usage_with_trend(now: float) -> dict | None:
    u = read_usage(now)
    path = STATE / "usage_samples.json"
    try:
        samples = [tuple(x) for x in json.loads(path.read_text())]
    except (OSError, ValueError):
        samples = []
    if u and u["seven_day"]["pct"] is not None:
        if not samples or now - samples[-1][0] >= 600 or abs(samples[-1][1] - u["seven_day"]["pct"]) >= 0.5:
            samples.append((now, float(u["seven_day"]["pct"])))
            samples = [s for s in samples if s[0] >= now - 8 * 86400][-3000:]
            STATE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(samples))
    if not u:
        return None
    trend = usage_eta(samples, now, MAX_USAGE)
    owner_only = usage_eta(samples, now, OWNER_ONLY_AT)
    u["trend"] = {**trend, "eta_owner_only": owner_only["eta"], "limit": MAX_USAGE, "owner_only_at": OWNER_ONLY_AT}
    return u


_progress_cache: dict = {}


def _short_progress(line: str) -> str:
    line = re.sub(r"`", "", line)
    if "помилка інструмента" in line:
        return (
            line.split("помилка інструмента")[0]
            + "помилка інструмента: "
            + ("відмовлено в дозволі" if "denied" in line else line.split(":", 2)[-1].strip()[:80])
        )
    return line[:180]


def live_progress(n: int = 14) -> list[str]:
    """The latest lines of the running (or last) agent session, as agents-progress.sh shows them."""
    project = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(WORKTREE))
    sessions = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if project.is_dir() else []
    if not sessions:
        return []
    key = (str(sessions[-1]), sessions[-1].stat().st_mtime)
    if _progress_cache.get("key") == key:
        return _progress_cache["lines"]
    try:
        r = subprocess.run(
            [sys.executable, "-I", str(HERE / "agents_transcript.py"), f"--project={project.name}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = [ln for ln in r.stdout.splitlines() if re.match(r"^(## |### |\*\*|  - )", ln)]
    except (OSError, subprocess.SubprocessError):
        lines = []
    lines = [_short_progress(ln) for ln in lines[-n:]]
    _progress_cache.update(key=key, lines=lines)
    return lines


_cards_cache: dict = {}


def cards_on_main() -> list[dict]:
    head = _git("rev-parse", "origin/main").strip()
    if _cards_cache.get("head") == head:
        return _cards_cache["cards"]
    cards = []
    for path in _git("ls-tree", "-r", "--name-only", "origin/main", "docs/ideas").split():
        name = Path(path).stem
        if name.startswith("IDEA-"):
            cards.append(rb.parse_card(_git("show", f"origin/main:{path}"), name))
    _cards_cache.update(head=head, cards=cards)
    return cards


def services() -> list[dict]:
    out = []
    for unit, label in SERVICES:
        state = "?"
        if shutil.which("systemctl"):
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            state = r.stdout.strip() or "?"
        out.append({"label": label, "unit": unit, "state": state})
    return out


def machine() -> dict:
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    try:
        load = Path("/proc/loadavg").read_text().split()[:3]
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        load, uptime = [], None
    disk = shutil.disk_usage("/")
    backups = DATA_DIR / "backups"
    files = sorted(backups.glob("*"), key=lambda p: p.stat().st_mtime) if backups.is_dir() else []
    return {
        "mem_total": mem.get("MemTotal"),
        "mem_used": (mem["MemTotal"] - mem["MemAvailable"]) if "MemAvailable" in mem else None,
        "load": load,
        "uptime_s": uptime,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "db_bytes": (DATA_DIR / "tradedesk.sqlite").stat().st_size if (DATA_DIR / "tradedesk.sqlite").exists() else None,
        "backup_age_s": time.time() - files[-1].stat().st_mtime if files else None,
    }


_last_fetch = [0.0]


def snapshot() -> dict:
    now = time.time()
    if now - _last_fetch[0] >= FETCH_EVERY_S:
        _git("fetch", "-q", "--no-write-fetch-head", "origin")
        _last_fetch[0] = now
    try:
        hist = (LOG_DIR / "history.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        hist = []
    branches = st.open_branches(REPO)
    work = st.work_state(LOG_DIR)
    parked = st.parked_state(LOG_DIR)
    busy = {b for b, _ in parked} | ({work[0]} if work else set()) | st.wip_branches(hist)
    agents_line, activity = st.agents_state(LOG_DIR, WORKTREE)
    cards = cards_on_main()
    since = datetime.fromtimestamp(now - 86400).strftime("%Y-%m-%d_%H%M%S")
    commits = [
        ln.split("\t", 2) for ln in _git("log", "origin/main", "-15", "--format=%h\t%ad\t%s", "--date=format:%d.%m %H:%M").splitlines()
    ]
    return {
        "generated_at": now,
        "agents": {
            "line": agents_line,
            "activity": activity,
            "work": work,
            "parked": [(b, at.strftime("%d.%m %H:%M")) for b, at in parked],
            "awaiting_review": [b for b in branches if b not in busy and "-fix-" not in b],
            "doctor_fixes": [b for b in branches if "-fix-" in b],
            "counts_24h": dict(st.history_counts(hist, since)),
            "history": history_rows(hist),
            "progress": live_progress(),
            "doctor": st.doctor_note(LOG_DIR),
        },
        "usage": usage_with_trend(now),
        "cards": cards,
        "progress": project_progress(cards, _git("show", "origin/main:docs/PROGRESS.md"), delivered_pace(hist, now)),
        "commits": commits,
        "questions": st.open_questions(_git("show", "origin/main:docs/QUESTIONS.md")),
        "services": services(),
        "machine": machine(),
        "tables": table_freshness(DATA_DIR / "tradedesk.sqlite", now),
    }


# ---------------------------------------------------------------- page

CSS = """
:root{--brand:#5b3fd1;--brand-deep:#2a1a7a;--mint:#7ff0b0;--bg:#f6f5fb;--card:#fff;--fg:#1c1838;--mut:#615c7e;--line:#e3e0ef;--acc:#5b3fd1;--ok:#1e7f50;--warn:#9a5b0c;--bad:#b33a36;--acc-rgb:91,63,209;--ok-rgb:30,127,80;--warn-rgb:154,91,12;--bad-rgb:179,58,54}
@media (prefers-color-scheme:dark){:root{--bg:#100d22;--card:#1a1633;--fg:#ece9fb;--mut:#a29dc4;--line:#2d2752;--acc:#a28cff;--ok:#6ee7a8;--warn:#f0b45a;--bad:#ff8a84;--acc-rgb:162,140,255;--ok-rgb:110,231,168;--warn-rgb:240,180,90;--bad-rgb:255,138,132}}
body::before{content:'';display:block;height:4px;background:linear-gradient(90deg,var(--brand),var(--mint))}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:14px 18px 8px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:baseline}
h1{font-size:19px;margin:0}.mut{color:var(--mut)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px;padding:0 12px 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:0}
section.wide{grid-column:1/-1}h2{font-size:14px;margin:0 0 8px;color:var(--acc);text-transform:uppercase;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600}.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:3px 0 6px}
.bar i{display:block;height:100%;background:var(--acc)}.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;font:12px/1.4 Menlo,Consolas,monospace;margin:0;max-height:330px;overflow:auto}
.kpi{display:flex;flex-wrap:wrap;gap:8px}.kpi div{border:1px solid var(--line);border-radius:6px;padding:4px 10px}.kpi b{font-size:17px}
code{font:12px Menlo,Consolas,monospace}
nav.tabs{display:flex;flex-wrap:wrap;gap:4px;padding:0 18px;border-bottom:1px solid var(--line);margin:0 0 12px}
nav.tabs a{padding:6px 12px;color:var(--mut);text-decoration:none;font-weight:600;border-bottom:3px solid transparent}
nav.tabs a[aria-current=page]{color:var(--fg);border-bottom-color:var(--acc)}
"""


def _nav(active: str) -> str:
    tabs = (
        ("/", "project", "Проєкт"),
        ("/brief", "brief", "Бриф"),
        ("/options", "options", "Опціони"),
        ("/radar", "radar", "Радар"),
        ("/journal", "journal", "Журнал"),
        ("/demo", "demo", "Демо"),
        ("/polymarket", "polymarket", "Polymarket"),
    )
    return (
        "<nav class='tabs'>"
        + "".join(f"<a href='{href}'{' aria-current=page' if key == active else ''}>{label}</a>" for href, key, label in tabs)
        + "</nav>"
        + ("" if active == "project" else HOWTO)
    )


def _e(x: object) -> str:
    return html.escape(str(x), quote=True)


# ---------------------------------------------------------------- help icons and trade-lean highlights (assistant tabs)
#
# Every tile and block of the assistant tabs gets an «i» icon: hover (desktop) or tap (phone) opens a short note —
# what the number is, how to read it, what it means for a trade. Tiles whose number speaks for or against buying
# options get a coloured edge and a one-line badge. All texts are ours (static), never data from the snapshot.

DEMO_ENTRY_WAIT_MS = 10 * 60_000  # execution.orders.ENTRY_WAIT_MS (no execution import here; a test pins it)

# key -> (title, what it is, how to read it, what it means for a trade)
HELP: dict[str, tuple[str, str, str, str]] = {
    "price": (
        "Ціна і зміна за добу",
        "Остання ціна безстрокового ф'ючерсу (перпа) на Binance і зміна за 24 години.",
        "Зелене — ціна зросла за добу, червоне — впала.",
        "Сама зміна за добу — не причина входити: дивись рівні, очікуваний рух і ризик.",
    ),
    "fng": (
        "Страх і жадібність",
        "Індекс настрою крипторинку від 0 до 100 (alternative.me): волатильність, обсяги, соцмережі, домінація BTC.",
        "До 25 — крайній страх, 25–45 — страх, 45–55 — нейтрально, 55–75 — жадібність, понад 75 — крайня жадібність.",
        "На крайнощах натовп панікує або в ейфорії, рухи різкіші: менший розмір. Як сигнал входу ми це не перевіряли — лише контекст.",
    ),
    "dvol": (
        "DVOL — «індекс страху» BTC",
        "Індекс Deribit: яку волатильність на 30 днів закладено в ціни опціонів BTC, у % річних (як VIX для акцій).",
        "DVOL 40 означає, що ринок чекає ±40 % за рік, тобто ≈ ±11.5 % за місяць (40 / √12). "
        "Перцентиль — де DVOL зараз відносно останніх 365 днів: 10-й — дешевше, ніж у 90 % днів року.",
        "Низький — опціони дешеві: купувати дешевше, а продаж дає мало премії. Високий — навпаки. "
        "Але «купувати дешеву волатильність» як окремий сигнал ми перевірили 18.10 — переваги не дало.",
    ),
    "iv": (
        "IV — закладена волатильність",
        "Волатильність, яку «зашито» в ціну конкретного опціону. Чим вища IV, тим дорожчий опціон. ATM IV — на страйку біля поточної ціни.",
        "Порівнюй з реалізованою (RV): IV вища за RV — опціони дорожчі за фактичний рух.",
        "Купуючи опціон, ти платиш за IV і заробляєш, якщо ціна рухатиметься сильніше, ніж закладено. Продаючи — навпаки.",
    ),
    "rv": (
        "Реалізована волатильність (RV)",
        "Наскільки BTC реально рухався за останні 7 або 30 днів, у % річних — та сама шкала, що й IV.",
        "RV 7 днів вища за 30 днів — ринок останнім часом пожвавився; нижча — заспокоївся.",
        "Якщо IV помітно вища за RV, покупець опціону платить за рух, якого поки немає.",
    ),
    "vrp": (
        "Премія IV − RV30",
        "На скільки пунктів волатильності опціони (DVOL) дорожчі за фактичний рух BTC за 30 днів.",
        "Звично +3…+10: продавці беруть премію за ризик. Понад +10 — опціони помітно дорогі; нижче 0 — дешевші за фактичний рух.",
        "Від'ємна — сприяє покупцю; велика додатна — продавцю. Перевірка цієї премії на реальних цінах опціонів ще триває.",
    ),
    "term": (
        "Крива строків",
        "ATM IV для різних експірацій: від найближчих до далеких.",
        "Контанго (далекі дорожчі) — звичний спокійний стан. Беквордація (близькі дорожчі) — ринок чекає руху зараз: подія або стрес.",
        "У беквордації короткі опціони дорогі: купувати їх дорожче, продавати — ризикованіше.",
    ),
    "skew": (
        "Скью (RR 25Δ)",
        "Різниця IV колів і путів з дельтою 25 (≈ 25 % шансу опинитись у грошах): кол − пут.",
        "Від'ємний — пути дорожчі: ринок страхується від падіння. Додатний — коли дорожчі: платять за участь у зростанні.",
        "При сильно від'ємному скью купувати пути дорожче, а продаж путів дає більше премії, але й більше ризику обвалу.",
    ),
    "key": (
        "Головне сьогодні",
        "Короткий підсумок знімка: ринок, діапазон, волатильність, захист, найближча подія.",
        "Мітки: «спокій» — до відома, «ризик» — обережно з розміром, «подія» — врахуй час, «нагода» — умови сприяють.",
        "Це опис ринку, не порада: рішення — за твоїми правилами й ризиком на угоду.",
    ),
    "chart": (
        "Графік і що закладають опціони",
        "Денні закриття BTC за 120 днів і діапазони з цін опціонів.",
        "Синя зона — ±1σ до місячної експірації (≈ 68 % шансу за цінами ринку). Жовті лінії — беззбиток тижневого стреддлу.",
        "Якщо твоя ціль далеко за синьою зоною, ринок вважає такий рух малоймовірним: опціон під нього дешевий, але й шанс малий.",
    ),
    "scenarios": (
        "Сценарії з цін опціонів",
        "Ймовірність, що BTC на експірацію опиниться в кожному діапазоні, виведена з цін опціонів Deribit.",
        "Жирним — діапазон поточного форварду. Це ризик-нейтральні ймовірності: «ціна страховки», а не прогноз.",
        "Якщо ти обґрунтовано вважаєш сценарій імовірнішим, ніж тут, — опціон під нього тобі дешевий. Якщо ні — переваги нема.",
    ),
    "em": (
        "Очікуваний рух",
        "Скільки ринок закладає руху до експірації: ціна ATM-стреддлу (кол + пут) або 1σ з IV.",
        "±3 % до п'ятниці — ринок платить за рух приблизно такого розміру.",
        "Якщо твоєму опціону для прибутку потрібен рух більший за очікуваний — ти ставиш на те, що ринок недооцінює рух.",
    ),
    "events": (
        "Події",
        "Макрорелізи (CPI, FOMC, NFP та ін.) за київським часом на 14 днів.",
        "Червоне — високий вплив. Наш замір: у першу годину після CPI / FOMC волатильність BTC у 2–3 рази вища.",
        "За 48 год до події — менший розмір або позиція з обмеженим ризиком; опціони перед подією зазвичай дорожчі.",
    ),
    "funding": (
        "Funding",
        "Платіж між лонгами й шортами безстрокових ф'ючерсів, щоб ціна трималась біля спота; тут — у % річних.",
        "Додатний — лонги платять шортам (натовп у лонгах), від'ємний — навпаки. Дуже високий — перегрів.",
        "Тримати лонг перпа при високому funding дорого. Заробіток на різниці funding ми перевіряли: малий і слабшає.",
    ),
    "conditions": (
        "Купувати чи продавати опціони",
        "Зведення: що зараз за цінами опціонів сприяє покупцю, а що продавцю.",
        "Зелений «+» — на користь, червоний «−» — проти, жовтий «!» — обережно. Оцінюється ціна волатильності й ризик, не напрям ціни.",
        "Це про те, дорого чи дешево зараз платити за опціон. Доведеної переваги жодна сторона не має (метод асистента відхилено 18.10).",
    ),
    "builder": (
        "Конструктор",
        "Збери опціонну позицію на експірації BTC — місячній (≥ 25 днів) чи найближчій (≥ 25 год): стратегія, страйки, "
        "депозит, ризик і розмір.",
        "Купівля стреддлу / стренглу — ставка на сильний рух у будь-який бік. Продаж — на спокій. Кондор — продаж із захистом.",
        "Нижче — графік P&L, «за і проти», греки, ризик і сценарії для цієї позиції. Поля «Теза» і «Ціль» з'являються, "
        "коли до експірації 1–3 доби (умовна зона правила 8, зазвичай на найближчій експірації): без них рядок 8а — "
        "«порушено».",
    ),
    "payoff": (
        "Прибуток і збиток",
        "P&L позиції залежно від ціни BTC: на експірацію і через N днів.",
        "Де лінія вище нуля — прибуток. Вертикальна лінія — поточний форвард, зона — ±1σ до експірації.",
        "Порівняй, де беззбиток, із зоною ±1σ: якщо він далеко за нею, для прибутку потрібен незвично великий рух.",
    ),
    "premium": (
        "Премія",
        "Скільки ти платиш (купівля) або отримуєш (продаж) за позицію за марками Deribit, без спреду й комісій.",
        "Для покупця премія — це і є максимальний збиток.",
        "Реальна ціна входу трохи гірша: на біржі купуєш по аску, продаєш по біду.",
    ),
    "breakeven": (
        "Беззбиток",
        "Ціна BTC на експірацію, за якої P&L дорівнює нулю.",
        "Для купленого стренглу — два рівні: нижче нижнього або вище верхнього — прибуток.",
        "Порівнюй відстань до беззбитку з очікуваним рухом: що далі, то менший шанс.",
    ),
    "maxprofit": (
        "Максимальний прибуток",
        "Найкращий можливий результат на експірацію.",
        "«Необмежений» — у купленого кола (і стреддлу) прибуток росте з ціною.",
        "Великий макс. прибуток ще не робить угоду вигідною: дивись шанс і ціну.",
    ),
    "maxloss": (
        "Максимальний збиток",
        "Найгірший можливий результат на експірацію.",
        "«Необмежений» — продаж опціонів без захисту: збиток росте разом із рухом.",
        "Має вкладатися у твій ризик на угоду. Необмежений збиток правило ризику не виконує — потрібен захист або стоп.",
    ),
    "pop": (
        "Ймовірність прибутку",
        "Шанс закінчити в плюсі на експірацію за цінами ринку (ризик-нейтрально, за ATM IV).",
        "У покупця опціонів зазвичай 20–40 %, зате виграш більший за ставку. У продавця 60–80 %, зате рідкі збитки великі.",
        "Сама по собі не каже, вигідна угода чи ні: за справедливими цінами очікуваний результат ≈ 0 мінус комісії.",
    ),
    "proscons": (
        "За і проти",
        "Перевірка цієї позиції: ризик, ціна волатильності, потрібний рух, вплив часу, події.",
        "Зелений «+» — на користь угоди, червоний «−» — проти, жовтий «!» — обережно, сірий «·» — нейтрально.",
        "Більше червоних — угода дорога або ризикована саме зараз. Це оцінка ціни й ризику, не прогноз напряму.",
    ),
    "greeks": (
        "Греки",
        "Як вартість позиції реагує на ціну (дельта, гамма), волатильність (вега) і час (тета).",
        "Дельта — як скільки BTC на споті; гамма — як швидко міняється дельта; вега — на 1 пункт IV; тета — за день.",
        "Покупець: тета мінус (час з'їдає), вега плюс (росте IV — заробляєш). Продавець — навпаки.",
    ),
    "riskfit": (
        "Чи вкладається в ризик",
        "Максимальний збиток позиції проти твого ризику на угоду (депозит × %).",
        "Зелене — вкладається; червоне — більше за ризик, і показано, який розмір вкладається.",
        "Твоє правило — ризик ≈ 1 % депозиту на угоду. Позиція понад нього — зменш розмір.",
    ),
    "rules_pre": (
        "Правила власника перед угодою",
        "Твої правила 1 і 8 (затверджено 18.10, правило 8 уточнено 22.10) на цій структурі: макс. збиток ≤ 1 % депозиту, "
        "премія ≤ 1 % депозиту; менше 24 год до експірації — не купувати ніколи; за 1–3 доби — лише коли виконано три "
        "умови: записані теза й ціль, рух до беззбитку не більший за 1σ руху за реалізованою волою (RV30) до "
        "експірації, IV не вище 1.2 × RV30; понад 3 доби — лише ліміт премії.",
        "«Порушено» — угода суперечить правилу; «невідомо» — бракує даних (теза, RV30, IV); «не стосується» — "
        "наприклад, продаж: премію отримуєш, а не платиш. Ліміт — 1 % від депозиту у формі, а не поле «Ризик на угоду». "
        "Пороги 1σ і 1.2 — стартовий вибір, не доведена перевага.",
        "Будь-яке «порушено» — переробити угоду (менший розмір, дальша експірація, спред замість купленого опціону, "
        "захисні опціони) або не входити. Решту правил (стоп, денний стоп, R:R) перевір чеклістом.",
    ),
    "verdict": (
        "Що це означає",
        "Та сама позиція простими словами: що купуєш, коли заробляєш, скільки забирає час, де межа збитку.",
        "Числа — з графіка й греків вище.",
        "Прочитай перед входом: якщо щось тут тебе не влаштовує — угоду варто не відкривати.",
    ),
    "volcost": (
        "Чи дорога зараз волатильність",
        "ATM IV експірації, DVOL і його перцентиль за рік, різниця з фактичним рухом.",
        "Таблиця — історичний бектест продажу стреддлу на кожному рівні DVOL (за індексом, не за реальними цінами).",
        "Дорога вола — гірше для покупця, краще для продавця, і навпаки. Як окремий сигнал переваги не доведено.",
    ),
    "optscen": (
        "Сценарії",
        "P&L позиції, якщо BTC зміниться на ±N % — зараз, через кілька днів і на експірацію; плюс сценарій «IV +5 пунктів».",
        "Зелене — прибуток, червоне — збиток.",
        "Знайди рядок, де тобі боляче: чи готовий ти до такого руху?",
    ),
    "j_trades": (
        "Мої угоди",
        "Підсумок закритих реальних угод з імпорту бірж і журналу.",
        "Частка в плюсі, P&L, середнє на угоду, середній R і найбільша просадка.",
        "Середнє на угоду нижче нуля — система поки збиткова: менший розмір, суворіші правила входу.",
    ),
    "r": (
        "R — результат у ризиках",
        "Прибуток або збиток угоди, поділений на запланований ризик (відстань до стопу).",
        "+2R — заробив удвічі більше, ніж ризикував; −1R — спрацював стоп.",
        "Середній R вище 0 на достатній кількості угод (≥ 30) — ознака переваги; менше — ще не доказ.",
    ),
    "drawdown": (
        "Найбільша просадка",
        "Найглибше падіння кривої P&L від попереднього максимуму.",
        "Показує, який біль система вже завдала.",
        "Розмір позицій має бути таким, щоб таку просадку повторно пережити спокійно.",
    ),
    "sources": (
        "За джерелом",
        "Ті самі угоди, розбиті за біржею або джерелом.",
        "Де найбільший мінус — там варто зупинитись і переглянути правила.",
        "Джерело з довгим мінусом — кандидат на паузу.",
    ),
    "rec": (
        "Паперові рекомендації асистента",
        "Угоди, які асистент запропонував за своїм методом, розраховані на папері (без грошей).",
        "Середнє в % спота — основна метрика; R — у ризиках угоди.",
        "Метод поки гіпотеза: у перевірці 18.10 переваги не показав. Паперовий журнал це перевіряє.",
    ),
    "plans": (
        "Плани угод асистента",
        "Плани з вердиктами enter / wait / skip і їх паперова оцінка на свічках.",
        "95 % ДІ — інтервал, де з 95 % впевненості лежить справжній середній R. Містить 0 — переваги не видно.",
        "Перший чесний висновок — після ≈ 30 розв'язаних планів enter.",
    ),
    "fc": (
        "Журнал прогнозів",
        "Щодня для BTC і ETH — ймовірності чотирьох подій на ціну delivery експірації приблизно через 7 днів: вище спота, "
        "у межах ±1σ, ±5 % і ±10 %. Їх дає усмішка опціонів Deribit.",
        "Після експірації кожен прогноз отримує результат 1 / 0 і оцінку Brier; плитки — підсумок уже розрахованих.",
        "Це ризик-нейтральні ймовірності (ціна опціонів), а не прогноз асистента і не сигнал: журнал перевіряє, "
        "чи переоцінюють опціони рух.",
    ),
    "fc_n": (
        "Прогнозів розраховано",
        "Скільки прогнозів уже отримали результат на delivery; відкриті ще чекають своєї експірації.",
        "Щодня 8 прогнозів (2 активи × 4 події), але вони не незалежні: багато з них мають спільний результат.",
        "Кількість прогнозів — не розмір вибірки: дивись плитку «Експірацій».",
    ),
    "fc_expiries": (
        "Експірацій — розмір вибірки",
        "Скільки різних пар «актив × експірація» вже розраховано.",
        "Прогнози різних днів на одну експірацію мають спільний результат, тож вибірка — не більше за це число (BTC і ETH одного дня сильно корельовані, ефективно менше). Менше 30 — висновку немає.",
        "Поки експірацій мало, жодна цифра цього блоку не підстава для угоди.",
    ),
    "fc_brier": (
        "Brier",
        "Середній квадрат різниці між ймовірністю і результатом (1 — подія сталась, 0 — ні).",
        "0 — ідеально, 0.25 — завжди казати 50 %, 1 — завжди впевнено помилятися. Менше — краще.",
        "Сам по собі мало що каже: порівнюй з еталонами в сусідніх плитках.",
    ),
    "fc_ref": (
        "Проти фіксованого еталона",
        "Навичка = 1 − Brier прогнозів / Brier наївної ймовірності: 50 % для «вище спота» і 68.27 % для ±1σ (нормальний "
        "розподіл). Лише ці дві події: для ±5 % і ±10 % природного еталона немає.",
        "Вище 0 — опціони точніші за наївну оцінку, нижче 0 — гірші.",
        "Навичка вище 0 на ≥ 30 експіраціях може вказувати, що ринок оцінює діапазони краще за наївну оцінку (перевірки значущості немає); це не перевага для угоди.",
    ),
    "fc_clim": (
        "Проти кліматології",
        "Навичка проти частоти кожної події в цій самій вибірці — скільки разів подія справді сталась.",
        "Цю частоту відомо лише заднім числом, тож еталон суворий: нижче 0 — прогнози гірші за просту частоту.",
        "Еталон обчислено заднім числом на тій самій вибірці, тож програш йому мало що доводить; "
        "премія за волатильність — плата за ризик, не помилка. Як угоду це треба перевіряти окремо.",
    ),
    "fc_calib": (
        "Калібрування",
        "Прогнози в 10 кошиках за ймовірністю: середній прогноз кошика проти частоти, з якою подія сталась.",
        "Точка на діагоналі — ймовірності чесні; вище діагоналі — подія стається частіше, ніж оцінено, нижче — рідше. "
        "Більша точка — більше прогнозів.",
        "Діапазони стабільно вище діагоналі означали б, що опціони переоцінюють рух (на користь продавця) — гіпотеза, не доведено.",
    ),
    "fc_weekly": (
        "Прогнози за тижнями",
        "Brier прогнозів, згрупованих за ISO-тижнем експірації (UTC), загалом і за кожною подією.",
        "У тижні лише кілька експірацій, тож окремий рядок дуже шумний; дивись на тенденцію за багато тижнів.",
        "Один добрий чи поганий тиждень — не сигнал.",
    ),
    "fc_rv": (
        "Опціони проти RV30",
        "Ті самі чотири події оцінює проста модель на історії: логнормальний розподіл від спота з волатильністю останніх "
        "30 днів (Binance, денні закриття), без скью і подій. Число — різниця Brier опціонів і цієї моделі на однакових прогнозах.",
        "Мінус — опціони прогнозують точніше, плюс — проста історія точніша. ДІ рахується по датах експірації "
        "(BTC і ETH однієї дати рухаються разом — одна одиниця); поки він містить 0, різниці не видно.",
        "Стабільний плюс означав би, що опціони систематично помиляються з розміром руху — гіпотеза, перевіряється лише від 30 дат "
        "експірації (≈ 30 тижнів).",
    ),
    "r_price": (
        "Ціна за тиждень",
        "Індекс Deribit о 19:00 UTC останнього дня архіву поверхні і того самого часу тиждень тому.",
        "Дати біля чисел — дні рядків архіву; «зараз» — не живий ринок, а останній записаний день.",
        "Тижнева зміна — контекст для IV і скью нижче: падіння ціни зазвичай супроводжує зростання IV і put skew.",
    ),
    "r_term": (
        "ATM IV за строками",
        "Закладена волатильність біля поточної ціни на 7, 30, 90 і 180 днів — зараз і тиждень тому.",
        "Вища лінія «зараз» — опціони подорожчали. 7 днів вище за 30 — інверсія фронту: ринок платить за найближчі дні.",
        "При інверсії тижневі опціони дорогі: купувати краще довший строк, а тижневі — продавати проти нього меншим обсягом.",
    ),
    "r_rank": (
        "IV Rank і перцентиль",
        "Де денний DVOL зараз у своєму річному діапазоні: Rank = (DVOL − мін) / (макс − мін), перцентиль — частка днів року нижче.",
        "До 20 % — біля дна року (зелене), 20–60 % — посередині, понад 60 % — біля верху. Пунктир — тиждень тому.",
        "Низький — опціони дешеві відносно року, але як сигнал купувати це перевагу не показало (H1 відхилено 18.10).",
    ),
    "r_ivrv": (
        "IV проти реального руху",
        "ATM IV на 30 днів проти фактичної волатильності за 30 і 7 днів (денні закриття Binance).",
        "IV вища за реалізовану — опціони дорожчі за рух, що був. Реалізована 7 днів вища за 30 — ринок пожвавився.",
        "Покупець опціону виграє, лише якщо майбутній рух перевищить закладений. Велика премія — на користь продавця, але без гарантій.",
    ),
    "r_skew": (
        "Скью 25Δ на 30 днів",
        "Put skew — IV 25-дельтового пута мінус ATM; RR — IV 25Δ кола мінус 25Δ пута.",
        "Зростання put skew або падіння RR за тиждень — захист від падіння подорожчав.",
        "Коли захист дорогий, пут-спред вигідніший за голий пут. Рівні з іншими джерелами не порівнювати — лише зміни.",
    ),
    "r_smile": (
        "Усмішка на 30 днів",
        "IV за дельтами: пути 10/25/35Δ, ATM, коли 35/25/10Δ — зараз і тиждень тому.",
        "Ліва частина вища — пути дорожчі за коли (страх падіння). Підйом усієї кривої — опціони подорожчали загалом.",
        "Показує, яку частину опціонів дешевше купувати чи продавати саме зараз.",
    ),
    "r_skewhist": (
        "Скью за рік",
        "Put skew і RR 25Δ на 30 днів з архіву поверхні за кожен день, медіана за 7 днів.",
        "Put skew вгору або RR вниз — захист від падіння дорожчає; лінія біля верху року — страх високий.",
        "Порівнюй поточний скью з його ж історією, а не з іншими джерелами: конвенція дельти в них інша.",
    ),
    "r_fwd": (
        "Форвардна вола і події",
        "Волатильність, закладена між двома сусідніми експіраціями, і макроподії, що припадають на це вікно.",
        "«Гарячий тиждень» — вікно, де вола помітно вища за сусідні (≥ медіани + 3 вп): ринок кладе туди подію.",
        "Опціон під подію береться з експірацією після неї, і він уже дорожчий. Підпис події — за датою, не доказ причини.",
    ),
    "r_now": (
        "Поточна ситуація",
        "П'ять рядків з блоків нижче: ціна, IV за строками, IV Rank, скью, найдорожчий тиждень форвардної воли.",
        "Колір мітки: синій — спокійно, зелений — опціони дешеві відносно року, червоний — ризик, жовтий — подія.",
        "Це опис ринку, а не сигнал: доведеної переваги жоден рядок не має.",
    ),
    "r_deal": (
        "Що це означає для угод",
        "Детерміновані правила макета: режим IV за роком, IV проти реального руху, події, фронт, скью, розмір.",
        "На наші перевірки спираються лише рядки про режим IV і IV проти RV; решта — правила макета, не перевірені. Тексту від моделі немає.",
        "Не порада: метод рекомендацій асистента (H1) перевіркою 18.10 відхилено. Ризик на угоду — за правилом 8.",
    ),
    "pm": (
        "Розриви Polymarket ↔ опціони",
        "Ринки Polymarket про ціну BTC / ETH, де ціна боку нижча за ймовірність, порахованою з опціонів Deribit.",
        "Edge — різниця в п.п. після комісії. R:R — скільки виграєш на кожен $ ризику.",
        "Edge ≥ 5 п.п. і R:R ≥ 2 — проходить правила. Це паперова гіпотеза: перевага ще перевіряється.",
    ),
    "pm_near": (
        "Близько до порогу",
        "Боки, де розрив між ціною Polymarket і ймовірністю з опціонів 3–5 п.п. після комісії — нижче порогу реєстрації.",
        "Лише для спостереження: до наступного знімка (щогодини) розрив може вирости або зникнути.",
        "Не сигнал: правило входу — edge ≥ 5 п.п. і R:R ≥ 2.",
    ),
    "pm_model": (
        "Модель: шанс боку",
        "Ризик-нейтральна ймовірність виграшу боку з поверхні IV Deribit, %: k=1 — IV як зібрано, k=0.92 — IV × 0.92.",
        "Edge рахується від гіршого з двох читань: бік показано, лише якщо розрив пережив обидва.",
        "Це ціна страхування в ринку опціонів, а не прогноз; на хвостах і при різних експіраціях модель помиляється сильніше.",
    ),
    "pm_edge": (
        "Чистий edge",
        "Шанс боку з моделі (гірше читання) мінус ціна входу: аск + верхня межа комісії, у п.п.",
        "+6 п.п. — за моделлю бік дешевший приблизно на 6 центів на кожен $1 виплати.",
        "Поріг правила — ≥ 5 п.п. Перевага не доведена: паперовий тест дасть вердикт ≈ 23.02.2027.",
    ),
    "pm_rr": (
        "R:R",
        "Скільки виграєш на кожен $ ставки, якщо бік зіграє: (1 − ціна входу) / ціна входу, ціна входу = аск + комісія.",
        "2 — виграш удвічі більший за ставку; нижче 2 — дорогий бік: мала виплата на великий ризик.",
        "Правило 5 власника — R:R ≥ 2, рядки нижче підсвічено. Розмір під ризик $100 — у сусідній колонці.",
    ),
    "position": (
        "Позиція",
        "Головні числа зібраної позиції, USD: премія, беззбиток, макс. прибуток і збиток на експірацію, ймовірність прибутку.",
        "У кожної плитки своя «i». Червоний край — збиток необмежений або більший за твій ризик, зелений — у межах ризику.",
        "Почни з максимального збитку: він має вкластися в ризик на угоду. Ймовірність прибутку ризик-нейтральна — не прогноз.",
    ),
    "r_spot": (
        "Ціна зараз",
        "Індекс Deribit (≈ спот) о 19:00 UTC останнього дня архіву поверхні, USD.",
        "Дата під числом — день рядка архіву: це не живий ринок, відставання до доби.",
        "Для входу бери живу ціну біржі; тут — точка, на яку пораховано IV і скью нижче.",
    ),
    "r_week": (
        "Ціна тиждень тому",
        "Той самий індекс Deribit о 19:00 UTC на 7 днів раніше, USD.",
        "Порівнюй із «Зараз»: різниця — рух за тиждень.",
        "Контекст для блоків нижче: вони порівнюють ці самі два дні.",
    ),
    "r_change": (
        "Зміна за тиждень",
        "Зміна індексу Deribit між двома днями архіву, %.",
        "Зелене — ціна зросла, червоне — впала.",
        "Сама зміна — не сигнал. Після різкого падіння IV і захист від падіння зазвичай дорожчі: купувати опціони дорожче.",
    ),
    "put_skew": (
        "Put skew 25Δ",
        "На скільки пунктів волатильності 25-дельтовий пут на 30 днів дорожчий за ATM: ціна захисту від падіння.",
        "Вище — захист дорожчий, ринок більше боїться падіння. Зміна за тиждень понад 0.2 вп підсвічена.",
        "Дорогий захист — пут-спред замість голого пута; продаж путів платить більше, але й ризик обвалу більший.",
    ),
    "j_winrate": (
        "Частка в плюсі",
        "Частка закритих реальних угод з результатом «win», %.",
        "Сама по собі мало що каже: багато дрібних виграшів і кілька великих збитків дають мінус. Дивись разом із середнім.",
        "Висока частка при від'ємному середньому — ознака, що прибутки ріжуться рано, а збитки тримаються довго.",
    ),
    "j_pnl": (
        "P&L разом",
        "Сума прибутків і збитків закритих реальних угод, USD: імпорт бірж і записи journal_add, без airdrop і паперових.",
        "Зелене — у плюсі, червоне — у мінусі.",
        "Залежить від розміру угод: систему краще оцінювати за середнім на угоду і середнім R.",
    ),
    "j_expectancy": (
        "Середнє на угоду",
        "Середній P&L однієї закритої реальної угоди, USD.",
        "Нижче нуля — система за цей період збиткова (червоний край).",
        "Від'ємне середнє — менший розмір і суворіші правила входу, поки воно не стане додатним на ≥ 30 угодах.",
    ),
    "j_open": (
        "Відкриті угоди",
        "Реальні угоди журналу, ще не закриті.",
        "У підсумки вище не входять: P&L з'являється після journal_close або імпорту закриття.",
        "Перевір, що в кожної є стоп і що разом вони не перевищують твого ризику.",
    ),
    "j_table": (
        "Угоди",
        "Усі реальні угоди, нові зверху: дата, джерело, інструмент, сторона, розмір, вхід і вихід, P&L у USD, R, теза.",
        "R порожній, якщо стоп не записано. Теги й нотатки — з імпорту або journal_add.",
        "Шукай повтори: той самий інструмент чи сетап раз у раз у мінусі — кандидат на паузу.",
    ),
    "j_review": (
        "Крива в R і тижневий розбір",
        "Статистика твоїх закритих реальних угод (без airdrop, паперових і демо-бота), не порада: накопичений R угода за "
        "угодою і підсумок кожного ISO-тижня закриття (UTC).",
        "R є лише в угодах із записаним стопом; угоди з бірж стопів не мають, тож у криву не входять. P&L — у USD, як "
        "записано в журналі. Менше 30 угод — «замало для висновку»: це опис, а не доказ переваги чи її відсутності.",
        "Записуй стоп у /trade чи journal_add: тоді угода потрапляє в криву, і за ≥ 30 угодами видно, чи є перевага.",
    ),
    "j_rcurve": (
        "Крива в R",
        "Сума R угод зі стопом за порядком закриття, від 0. R = результат угоди, поділений на ризик до стопу; "
        "вісь X — номер угоди, вісь Y — накопичений R.",
        "Крива, що росте, — угоди в сумі заробляють більше, ніж ризикують; просадка — найглибше падіння від максимуму, у R. "
        "Зелена точка — угода в плюсі, червона — у мінусі.",
        "Довга просадка в R при дотриманих стопах — привід зменшити розмір чи переглянути сетап, а не збільшувати ризик.",
    ),
    "j_weeks": (
        "Розбір за тижнями",
        "Закриті реальні угоди, згруповані за ISO-тижнем закриття (UTC), нові зверху: скільки закрито, скільки в плюсі, "
        "P&L разом і на угоду (USD), середній R лише серед угод зі стопом, найкраща й найгірша угода за P&L.",
        "«Невідомо» в колонці R — у тижні не було угод зі стопом. У тижні зазвичай кілька угод, тож окремий рядок шумний.",
        "Один добрий чи поганий тиждень — не сигнал; дивись, чи повторюється той самий інструмент серед найгірших.",
    ),
    "j_rules": (
        "За правилами",
        "Частка твоїх реальних угод (без airdrop, паперових і демо-бота), що перевірені на правила 1, 2, 4, 5 і 8 і не "
        "порушили жодного: «без порушень» з «перевірених», %. Це статистика дотримання правил, не порада і не міра переваги.",
        "Перевірена — коли кожне правило, що стосується угоди, вдалося оцінити, або коли хоч одне точно порушено. "
        "«Невідомо» — порушень не видно, але даних не вистачає (в імпорті з бірж немає стопу й цілі): це не «за правилами», "
        "у частку не входить. Угоди, де єдине порушення — денний стоп за часом закриття (наближено), теж окремо. "
        "Угода без стопу потрапляє в перевірені лише тоді, коли щось точно порушила, тож поки стопів у журналі мало, "
        "частка занижена. Менше 30 перевірених — «замало для висновку».",
        "Записуй стоп і ціль у journal_add чи /trade: тоді угода стає перевіреною, а частка — чесною. Розбивка за тижнями "
        "й правилами — у тижневому звіті порушень нижче.",
    ),
    "rules_week": (
        "Порушення правил за тиждень",
        "Твої реальні угоди, перевірені на правила 1, 2, 4, 5 і 8 (затверджено 18.10), за ISO-тижнем відкриття, нові зверху.",
        "«2 з 8» — порушили 2 з 8 угод, які вдалося перевірити; «невідомо» — правило стосується, але даних нема (стопу "
        "в імпорті з бірж немає, Bybit не дає часу відкриття). «P&L цих угод» — результат угод, що точно порушили хоч "
        "одне правило; «наближено» — угоди, де єдине порушення — денний стоп, оцінений за часом закриття (окремо). "
        "Правило 8: «< 24 год» — куплено в останню добу до експірації; «1–3 доби без умов» — без тези й цілі (імпорт "
        "з бірж їх не має).",
        "Ефект правил міряє журнал: через 4–8 тижнів порівняй P&L угод з порушеннями і без. Правила змінює лише власник.",
    ),
    "rec_n": (
        "Розраховано",
        "Скільки паперових угод методу за ланцюгом уже дійшли до експірації й пораховані; поруч — відкриті.",
        "Менше ≈ 30 — підсумок описовий, висновку про перевагу не дає.",
        "Метод — гіпотеза (H1 відхилено 18.10): за цими угодами не торгувати, лише стежити.",
    ),
    "rec_win": (
        "У плюсі (папір)",
        "Частка розрахованих паперових угод з P&L > 0, %.",
        "У покупця опціонів частка в плюсі зазвичай низька, зате виграш більший за збиток: дивись разом із середнім.",
        "Сама по собі перевагу не доводить.",
    ),
    "rec_pct": (
        "Середнє, % спота",
        "Середній P&L паперової угоди на 1 BTC у % від спота на вході — та сама метрика, що в тесті H1 (−0.59 %, n = 27).",
        "Вище нуля — метод на папері поки в плюсі; на малій вибірці це шум.",
        "Основна метрика перевірки. Поки n мале, це не доказ переваги.",
    ),
    "rec_r_worst": (
        "Середній R (від найгіршого)",
        "Середній P&L угоди, поділений на її найгірший можливий збиток (ризик угоди).",
        "−1 R — втрачено весь ризик. Консервативніша оцінка, ніж R «як у тесті».",
        "Показує, скільки дає кожен $ ризику; на малій вибірці — не доказ.",
    ),
    "rec_pnl": (
        "P&L разом (папір)",
        "Сумарний паперовий P&L розрахованих угод методу, USD, на розміри, які пропонував метод.",
        "Залежить від розміру угод, тож для оцінки краще «% спота».",
        "Гроші не задіяні: це перевірка гіпотези, не результат торгівлі.",
    ),
    "plan_n": (
        "Розв'язано",
        "Скільки планів enter уже оцінено на свічках: ціль, стоп, строк або вхід так і не стався.",
        "«З входом» — ціна дійшла до входу; «з R» — у плану є результат у ризиках.",
        "Перший чесний висновок — після ≈ 30 планів з R; до того — описово.",
    ),
    "plan_win": (
        "У плюсі (плани)",
        "Частка розв'язаних планів enter з R > 0, %.",
        "Читай разом із середнім R і його 95 % ДІ.",
        "Висока частка без додатного середнього R переваги не означає.",
    ),
    "plan_exits": (
        "Виходи",
        "Чим завершились розв'язані плани enter: ціль / стоп, за строком, без входу.",
        "Багато «без входу» — вхід надто далеко; багато «за строком» — ціль надто далеко для строку.",
        "Підказує, що підправити в планах (вхід, ціль, строк); перевагу не доводить.",
    ),
    # project page (meta: about the project, not the market)
    "p_progress": (
        "Прогрес проєкту",
        "Скільки кроків до готового продукту зроблено: кроки карток власника й етапи з docs/PROGRESS.md, у штуках і %.",
        "Кожен крок важить однаково; нові задачі збільшують загальну кількість, тож % може й падати. Дні — грубо, за темпом 7 днів.",
        "Прямо на угоди не впливає: показує, коли з'являться нові інструменти. Незробленим кроком не користуйся як готовим.",
    ),
    "p_agents": (
        "Агенти зараз",
        "Що робить конвеєр агентів (скаут → критик → квант → розробник → PR): гілка в роботі, відкладені, ті, що чекають тебе.",
        "«Чекають твого перегляду» — PR, які без тебе не зіллються. Рядок «за добу» — скільки кроків здано, з помилкою тощо.",
        "На угоди не впливає: нове в панелі й MCP з'являється лише після злиття в main.",
    ),
    "p_limit": (
        "Ліміт Claude",
        "Використання плану Claude за 5 годин і за тиждень, %; темп тижня — у п. п. на добу.",
        "Пороги під смужками: від першого агенти беруть лише твої картки, від другого — пауза. Ліміт спільний із твоїми сесіями.",
        "На угоди не впливає; біля порогу асистент у Telegram і агенти відповідатимуть повільніше.",
    ),
    "p_queue": (
        "Черга",
        "Картки ідей у роботі: спершу твої (з номером черги), потім від агентів; статус і зроблені кроки.",
        "proposed — запропоновано, accepted — прийнято, testing — іде перевірка. Смужка — частка кроків розділу «Прогрес» картки.",
        "Ідея в черзі ще не перевірена: торгувати за нею рано, чекай вердикту в «Результатах перевірок».",
    ),
    "p_results": (
        "Результати перевірок",
        "Скільки ідей уже перевірено на наших даних і з яким вердиктом; нижче — останні 12.",
        "«Підтверджено» — результат є (або інструмент збудовано), «форвард» — паперова перевірка в реальному часі, "
        "«без вердикту» — даних замало, «відхилено» — переваги не знайдено.",
        "За відхиленою ідеєю не торгувати. Навіть підтверджена — це минуле: розмір усе одно за правилом ризику.",
    ),
    "p_steps": (
        "Кроки агентів",
        "Останні кроки конвеєра: коли, результат, що зроблено, скільки часу й ліміту пішло.",
        "Зелене — здано, червоне — помилка або завис. Кілька червоних поспіль — варто глянути логи.",
        "На угоди не впливає.",
    ),
    "p_server": (
        "Сервер",
        "Стан сервера: пам'ять і диск у ГБ, навантаження (load), час роботи, розмір бази в МБ, вік бекапу.",
        "Смужка жовтіє від 70 % і червоніє від 90 %. Бекап — вік найсвіжішого файлу бекапу: помітно старший, ніж звично, — бекап не спрацював.",
        "Перевантажений сервер або повний диск — дані колектора можуть відставати: перевір їх свіжість.",
    ),
    "p_collector": (
        "Колектор даних",
        "Таблиці бази, які колектор 24/7 поповнює з бірж: вік останнього запису і приблизна кількість рядків.",
        "Зелене — свіже, червоне — запис запізнюється (завдання стало або біржа недоступна), сіре — норми свіжості нема.",
        "Червоний рядок — інструменти на цих даних показують застарілу картину: не спирайся на них, поки не оновиться.",
    ),
    "p_changes": (
        "Останні зміни в main",
        "Останні коміти основної гілки: дата і короткий опис.",
        "Нове в панелі й MCP з'являється після злиття сюди; сервер підтягує main сам.",
        "На угоди не впливає; пояснює, чому щось у панелі змінилось.",
    ),
    "p_questions": (
        "Питання агентів до тебе",
        "Відкриті питання з docs/QUESTIONS.md, на які агенти чекають твоєї відповіді.",
        "Поки питання відкрите, пов'язана робота може стояти.",
        "На угоди не впливає напряму; відповідь прискорює картки, від яких залежать нові інструменти.",
    ),
    # «Демо» tab (demo trader on the Bybit Demo Trading account, play money)
    "demo": (
        "Демо-угоди",
        "Угоди демо-трейдера на демо-рахунку Bybit (ігрові гроші): сетапи S14, S16, S3, вхід зі стопом і ціллю, ризик ≈ $100.",
        "Закриті, відкриті й невиконані входи, P&L і середній R усіх сетапів разом.",
        "Це полігон для перевірки сетапів наживо, не твої реальні угоди; рішення — за R окремого сетапу на ≥ 30 угодах.",
    ),
    "demo_closed": (
        "Закрито",
        "Демо-угоди, що вже вийшли з позиції, і як саме: ціль, стоп, беззбиток або вихід за часом.",
        "Багато виходів у беззбиток — правило беззбитку часто спрацьовує раніше за ціль.",
        "Кількість закритих угод визначає, чи можна вже робити висновок про сетап (≥ 30).",
    ),
    "demo_open": (
        "Відкрито",
        "Позиції на демо зараз, разом з лімітними входами, що ще чекають заповнення, і рядками до відповіді біржі.",
        f"«Чекають входу» — мейкер-ордер стоїть до {DEMO_ENTRY_WAIT_MS // 60_000} хв (переставляється щохвилини); далі решта йде "
        "ринком, лише якщо ціна ще поруч із планом, інакше угода стане «вхід не заповнився».",
        "Відкриті угоди ще не дають R: до підсумків вони не входять.",
    ),
    "demo_missed": (
        "Не виконано",
        "Сигнали, які демо-трейдер узяв, але позиції не відкрив: лімітний вхід не заповнився або біржа відхилила ордер.",
        "Багато незаповнених входів — ціна йде без відкату, мейкер-вхід пропускає рух.",
        "Такі сигнали в R не входять; що дали б їхні тіньові плани — у блоці «Демо проти бектесту».",
    ),
    "demo_pnl": (
        "P&L демо",
        "Сума закритого P&L біржі за демо-угодами, USD, з комісіями. Гроші ігрові.",
        "Залежить від розміру ризику на угоду; для порівняння сетапів дивись R, а не долари.",
        "Позитивний P&L на малій вибірці — не доказ переваги.",
    ),
    "demo_r_all": (
        "Середній R, усі сетапи",
        "Середній результат закритих демо-угод у ризиках (P&L / ризик на стопі) і його 95 % довірчий інтервал.",
        "ДІ містить 0 — переваги не видно; вище 0 — є ознака переваги, якщо угод ≥ 30.",
        "Сетапи різні: висновок робити за кожним окремо, нижче.",
    ),
    "demo_setups": (
        "R за сетапом",
        "Для кожного сетапу демо (S14, S16, S3): закриті угоди, частка в плюсі, середній R і його 95 % ДІ проти 0.",
        "Менше 30 угод з R — «замало для висновку»: числа описові.",
        "Сетап із ДІ вище 0 на ≥ 30 угодах — кандидат на реальні гроші; з ДІ нижче 0 — кандидат на вимкнення.",
    ),
    "demo_setup": (
        "Середній R сетапу",
        "Середній R закритих демо-угод цього сетапу, t-інтервал 95 % середнього, частка в плюсі й P&L.",
        "Жовта позначка «замало для висновку» — менше 30 угод з R.",
        "Не збільшуй ризик на сетапі, доки його ДІ не стане вищим за 0 на достатній вибірці.",
    ),
    "demo_versions": (
        "Версії сетапів",
        "Закриті демо-угоди окремо за кожною версією правил сетапу (вхід, ціль, беззбиток, виходи): кількість, частка в плюсі "
        "(R > 0), середній R, P&L. Версія угоди — за часом її відкриття; нижче — таблиця змін правил з комітами.",
        "До 30 закритих угод з R у версії — «замало для висновку». «Правила змінились під час угоди» — угода "
        "була відкрита, коли почалась наступна версія, і далі велась за новими правилами.",
        "Судити про сетап за його поточною версією; результат старої версії не переносити на нову.",
    ),
    "demo_recent": (
        "Останні демо-угоди",
        "Двадцять останніх угод демо-трейдера (новіші за закриттям угори): дати сигналу й закриття (Київ), монета, сетап і "
        "таймфрейм, сторона, стан, вихід, R і P&L.",
        "Стан «чекає входу» — лімітний ордер ще стоїть; «вхід не заповнився» — його зняли без заповнення.",
        "Опис, не рекомендація: це угоди автоматичного сканера на демо.",
    ),
    "demo_refusals": (
        "Відмови guard за причинами",
        "Сигнали, які демо-трейдер не відкрив, з журналу рішень exec_audit: відмова guard (ліміт чи правило) або не відібрано.",
        "Найчастіші причини вгорі; «за 7 днів» — свіжі. Одне рішення може мати кілька причин.",
        "Часті відмови за R:R або стопом — детектор дає сигнали, що не проходять правила ризику; варто дивитись сам сетап.",
    ),
    "demo_refused": (
        "Відмов guard",
        "Рішень «refused»: сигнал порушив правило guard (R:R, відстань стопу, позиція вже є, список монет) або не вдалось підготувати ордер.",
        "Це запобіжники ризику: такий сигнал не міг стати угодою за жодних умов.",
        "Не означає втрачену угоду: guard відсікає те, що поза правилами.",
    ),
    "demo_skipped": (
        "Не відібрано",
        "Рішень «skipped»: сигнал пройшов правила ризику guard, але мав низький бал, упирався в денні ліміти або поступився сильнішому.",
        "Багато пропусків через бал — фільтр балів відсікає частину сигналів; чи даремно, покаже звірка з тіньовими планами.",
        "Тіньові плани таких сигналів оцінюються окремо (plan_review → demo_shadow).",
    ),
    "demo_vs_bt": (
        "Демо проти бектесту",
        "Звірка кожної закритої демо-угоди з бектестом того самого сетапу: чи бектест дав би цю угоду з тим самим входом, стопом і R.",
        "Панель без мережі, а звірці потрібні свічки біржі: щоденний таймер пише її вихід у файл, панель лише читає файл.",
        "Якщо демо систематично розходиться з бектестом, історичним тестам сетапу вірити не можна.",
    ),
    "demo_vs_bt_total": (
        "Звірка з бектестом",
        "Скільки закритих демо-угод бектест відтворив: збіг, немає сигналу, розбіжність; наростаючим підсумком від першої угоди.",
        "Частка збігів і різниця R (демо − бектест) в одиницях ризику бектесту; до 30 звірених угод — «замало для висновку».",
        "Низька частка збігів — бектест сетапу описує не ту торгівлю, що веде демо.",
    ),
    "demo_not_executed": (
        "Не виконані проти тіньових планів",
        "Сигнали, які демо відібрало, але не виконало (вхід не заповнився; не відібрано за балом чи денними лімітами; відмова "
        "guard, напр. R:R після комісій нижче 1.5), і що дали б їхні тіньові плани; поруч — тіньові плани виконаних угод.",
        "R в одиницях ризику демо, без беззбитку, ринковий вхід за ціною рішення; до 30 планів з R — «замало для висновку».",
        "Якщо пропущені дають стабільно більше R, ніж виконані, — фільтр балів, мейкер-вхід або запобіжник guard відсікає добрі угоди.",
    ),
}

HOWTO = (
    "<div class='howto'><span><span class='info' aria-hidden='true'>i</span> наведи або торкнись — що це і як читати</span>"
    "<span><i class='sw good'></i>на користь угоди</span><span><i class='sw warn'></i>обережно</span>"
    "<span><i class='sw bad'></i>проти угоди</span>"
    "<span class='mut'>Підсвітка оцінює ціну й ризик, а не напрям ціни; доведеної переваги вона не означає.</span></div>"
)

HELP_CSS = """
.info{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;margin-left:6px;border-radius:50%;
border:1.5px solid var(--acc);color:var(--acc);font:italic 700 11px Georgia,serif;cursor:help;vertical-align:middle;position:relative;
text-transform:none;letter-spacing:0;user-select:none;-webkit-user-select:none;flex:none;-webkit-tap-highlight-color:transparent}
.info:hover,.info:focus,.info.open{background:var(--acc);color:var(--card);outline:none}
.info .tip{display:none;position:absolute;z-index:50;left:50%;top:22px;transform:translateX(-50%);width:min(330px,88vw);
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
box-shadow:0 8px 28px rgba(0,0,0,.22);font:13px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
font-style:normal;text-align:left;white-space:normal;cursor:auto}
.info:hover .tip,.info:focus .tip,.info.open .tip{display:block}
.tip b{display:block;margin-bottom:4px;color:var(--acc);font-size:13px}.tip span{display:block}
.tip .rd{margin-top:6px;color:var(--mut)}.tip .tr{margin-top:6px;padding-top:6px;border-top:1px dashed var(--line)}
.tile .k{display:flex;align-items:center}
.tile.t-good{border-left:4px solid var(--ok)}.tile.t-warn{border-left:4px solid var(--warn)}.tile.t-bad{border-left:4px solid var(--bad)}
.sig{font-size:12px;font-weight:600;margin-top:2px}.sig.good{color:var(--ok)}.sig.warn{color:var(--warn)}.sig.bad{color:var(--bad)}
.howto{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin:-4px 12px 12px;font-size:12px}
.howto .info{cursor:default}.howto .sw{display:inline-block;width:4px;height:14px;border-radius:2px;vertical-align:middle;margin-right:6px}
.sw.good{background:var(--ok)}.sw.warn{background:var(--warn)}.sw.bad{background:var(--bad)}
ul.pc{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
ul.pc li{display:grid;grid-template-columns:22px 1fr;gap:6px;padding:6px 8px;border-radius:6px;font-size:13px;line-height:1.5}
ul.pc .pi{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;
font:700 13px/1 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#fff;background:var(--mut);margin-top:1px}
ul.pc li.good .pi{background:var(--ok)}ul.pc li.bad .pi{background:var(--bad)}ul.pc li.warn .pi{background:var(--warn)}
ul.pc li.good{background:rgba(var(--ok-rgb),.11)}ul.pc li.bad{background:rgba(var(--bad-rgb),.11)}ul.pc li.warn{background:rgba(var(--warn-rgb),.13)}
.pcsum{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
.cols2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px}
.cols2 h3{font-size:13px;margin:0 0 6px}
"""

# Hover is followed for a mouse only (a tap also fires enter/leave). A tap fires focus and click in a row: the click
# only closes a tip opened earlier, not just now.
# Open tips are positioned `fixed` so table scrollers and section edges never clip them.
HELP_JS = """
(function () {
  var open = null, at = 0;
  function place(i) {
    var t = i.querySelector('.tip'); if (!t) return;
    var w = Math.min(330, innerWidth - 16), r = i.getBoundingClientRect();
    t.style.position = 'fixed'; t.style.transform = 'none'; t.style.width = w + 'px'; t.style.display = 'block';
    t.style.left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8)) + 'px';
    var h = t.offsetHeight, y = r.bottom + 6;
    if (y + h > innerHeight - 8 && r.top - h - 6 > 8) y = r.top - h - 6;
    t.style.top = Math.max(8, y) + 'px';
  }
  function show(i) { if (open && open !== i) hide(open); if (open !== i) at = Date.now(); open = i; i.classList.add('open'); place(i); }
  function hide(i) { i.classList.remove('open'); var t = i.querySelector('.tip'); if (t) t.removeAttribute('style'); if (open === i) open = null; }
  document.querySelectorAll('.info').forEach(function (i) {
    if (!i.querySelector('.tip')) return;
    i.addEventListener('pointerenter', function (e) { if (e.pointerType === 'mouse') show(i); });
    i.addEventListener('pointerleave', function (e) { if (e.pointerType === 'mouse') hide(i); });
    i.addEventListener('focus', function () { show(i); });
    i.addEventListener('blur', function () { hide(i); });
    i.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); if (open === i && Date.now() - at > 400) { hide(i); } else { show(i); } });
    i.addEventListener('keydown', function (e) { if (e.key === 'Escape') { hide(i); i.blur(); } });
  });
  document.addEventListener('click', function () { if (open) hide(open); });
  addEventListener('scroll', function () { if (open) place(open); }, { passive: true });
  addEventListener('resize', function () { if (open) place(open); });
})();
"""


def _info(key: str) -> str:
    """The «i» icon with its note (static text from HELP); '' for an unknown key."""
    h = HELP.get(key)
    if not h:
        return ""
    title, what, read, trade = (_e(x) for x in h)
    return (
        f"<span class='info' tabindex='0' role='button' aria-label='Пояснення: {title}'>i"
        f"<span class='tip' role='tooltip'><b>{title}</b><span>{what}</span><span class='rd'>{read}</span>"
        f"<span class='tr'>Для угоди: {trade}</span></span></span>"
    )


def _h2(title: str, key: str = "", extra: str = "") -> str:
    """A section heading (title escaped here) with its help icon; `extra` is trusted HTML (chips)."""
    return f"<h2>{_e(title)}{_info(key)}{extra}</h2>"


def with_help(page: str) -> str:
    """Add the tooltip script to a rendered assistant page."""
    return page.replace("</body>", f"<script>{HELP_JS}</script></body>", 1)


def _flip(tone: str) -> str:
    return {"good": "bad", "bad": "good"}.get(tone, tone)


def vol_lean(pct365: float | None, spr: float | None) -> list[tuple[str, str, str]]:
    """(tone for an option BUYER, buyer's text, seller's text) from DVOL's 365-day percentile and the DVOL − RV30
    spread; the seller's tone is the flipped one. About the price of options only: no edge is claimed (H1 rejected
    18.10)."""
    out: list[tuple[str, str, str]] = []
    if pct365 is not None:
        p = f"{pct365 * 100:.0f}-й перцентиль за рік"
        if pct365 < 0.3:
            out.append(
                (
                    "good",
                    f"Волатильність дешева ({p}): опціони коштують менше, ніж зазвичай",
                    f"Волатильність дешева ({p}): премія за продаж менша, ніж зазвичай",
                )
            )
        elif pct365 > 0.7:
            out.append(
                (
                    "bad",
                    f"Волатильність дорога ({p}): опціони коштують більше, ніж зазвичай",
                    f"Волатильність дорога ({p}): премія за продаж більша, ніж зазвичай",
                )
            )
        else:
            out.append(("", f"Волатильність на середньому рівні ({p})", f"Волатильність на середньому рівні ({p})"))
    if spr is not None:
        v = f"{spr * 100:+.1f}".replace("-", "−")
        if spr <= 0:
            out.append(
                (
                    "good",
                    f"IV не вища за фактичний рух за 30 днів ({v} вп): опціони не переплачені",
                    f"IV не вища за фактичний рух за 30 днів ({v} вп): продаж не дає премії понад фактичний рух",
                )
            )
        elif spr >= 0.10:
            out.append(
                (
                    "bad",
                    f"IV вища за фактичний рух на {v} вп: платиш за рух, якого поки немає",
                    f"IV вища за фактичний рух на {v} вп: продаж бере премію понад фактичний рух",
                )
            )
        else:
            out.append(
                ("", f"IV вища за фактичний рух на {v} вп — у звичних межах", f"IV вища за фактичний рух на {v} вп — у звичних межах")
            )
    return out


def lean_badge(tone: str, buyer: bool = True) -> str:
    """A tile badge for the buyer's view of an option price signal."""
    return {"good": "покупцю дешево", "bad": "покупцю дорого"}.get(tone, "") if buyer else ""


CHIP_HYPOTHESIS = "<span class='chip ev'>гіпотеза</span>"
CHIP_LEDGER = "<span class='chip calm'>облік</span>"
CHIP_HINT = "<span class='chip ev'>підказка</span>"
PC_ICON = {"good": "+", "bad": "−", "warn": "!", "": "·"}  # drawn as coloured dots (CSS), no emoji font needed


def pros_cons_html(items: list[tuple[str, str]]) -> str:
    """A for / against / caution list with a count line; texts are escaped here."""
    if not items:
        return _empty("Даних для оцінки немає.")
    n = {t: sum(1 for x, _ in items if x == t) for t in ("good", "bad", "warn")}
    summ = (
        f"<div class='pcsum'><span class='chip opp'>за: {n['good']}</span><span class='chip risk'>проти: {n['bad']}</span>"
        f"<span class='chip ev'>обережно: {n['warn']}</span></div>"
    )
    return (
        summ
        + "<ul class='pc'>"
        + "".join(f"<li class='{t}'><span class='pi'>{PC_ICON[t]}</span><span>{_e(s)}</span></li>" for t, s in items)
        + "</ul>"
    )


def _bar(pct: float | None, warn: float, bad: float) -> str:
    if pct is None:
        return ""
    cls = "bad" if pct >= bad else "warn" if pct >= warn else ""
    return f'<div class="bar"><i class="{cls}" style="width:{min(100, pct):.0f}%"></i></div>'


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d.%m %H:%M") if ts else "—"


def _reset(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%a %d.%m %H:%M")
    except ValueError:
        return iso


def render(s: dict) -> str:
    a, u, m = s["agents"], s.get("usage"), s["machine"]
    out = [
        f"<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>tradedesk — панель</title><style>{CSS}{HELP_CSS}</style></head><body>"
    ]
    svc = " ".join(f"<span class='{'ok' if x['state'] == 'active' else 'bad'}'>● {_e(x['label'])}</span>" for x in s["services"])
    out.append(
        f"<header><h1>tradedesk</h1><span class='mut'>оновлено {_when(s['generated_at'])} · сторінка сама оновлюється щохвилини</span><span>{svc}</span></header>{_nav('project')}<main>"
    )

    # Project progress
    pr = s.get("progress")
    if pr:
        out.append(f"<section class='wide'>{_h2('Прогрес проєкту', 'p_progress')}")
        out.append(
            f"<p><b style='font-size:17px'>{pr['pct']:.0f} %</b> — зроблено {pr['done']} з {pr['total']} кроків до готового продукту "
            "<span class='mut'>(кожен крок важить однаково; коли додаються нові задачі, загальна кількість росте)</span></p>"
        )
        out.append(f'<div class="bar" style="height:14px"><i style="width:{min(100, pr["pct"]):.0f}%"></i></div>')
        out.append("<table><tr><th>Етап</th><th>Зроблено</th><th style='width:45%'>Прогрес</th></tr>")
        for g in pr["groups"]:
            pct = 100.0 * g["done"] / g["total"] if g["total"] else 0.0
            out.append(
                f"<tr><td>{_e(g['label'])}</td><td>{g['done']} / {g['total']}</td>"
                f'<td><div class="bar"><i style="width:{pct:.0f}%"></i></div></td></tr>'
            )
        out.append("</table>")
        if pr["eta_days"] is not None:
            out.append(
                f"<p class='mut'>Карткам власника лишилось {pr['cards_left']} кроків; за темпом останніх 7 днів "
                f"({pr['pace']:.1f} здачі на день) — ≈ {pr['eta_days']:.0f} дн. роботи агентів. Грубо: без пауз через ліміт, "
                "кроків, що ще з'являться, і 4-тижневих паперових вікон.</p>"
            )
        out.append(
            "<p class='mut'>Кроки карток — розділ «Прогрес» у кожній картці; етапи продукту — <code>docs/PROGRESS.md</code>.</p></section>"
        )

    # Agents now
    out.append(f"<section>{_h2('Агенти зараз', 'p_agents')}")
    out.append(f"<p><b>{_e(a['line'])}</b></p>")
    if a["activity"]:
        out.append(f"<p>{_e(a['activity'])}</p>")
    if a["work"]:
        out.append(f"<p>🔄 у роботі <code>{_e(a['work'][0])}</code>, прогін {a['work'][1]}</p>")
    for br, at in a["parked"]:
        out.append(f"<p>⏸ відкладено до {_e(at)}: <code>{_e(br)}</code></p>")
    if a["awaiting_review"]:
        out.append("<p class='warn'>Чекають твого перегляду: " + ", ".join(f"<code>{_e(b)}</code>" for b in a["awaiting_review"]) + "</p>")
    if a.get("doctor_fixes"):
        out.append(
            "<p class='mut'>Виправлення від лікаря (PR без автозливу): "
            + ", ".join(f"<code>{_e(b)}</code>" for b in a["doctor_fixes"])
            + "</p>"
        )
    c = a["counts_24h"]
    if c:
        out.append(
            "<p class='mut'>За добу: "
            + " · ".join(f"{_e(STATUS_UA.get(k, k))} {v}" for k, v in sorted(c.items(), key=lambda kv: -kv[1]))
            + "</p>"
        )
    if a["doctor"]:
        out.append(f"<p class='mut'>{_e(a['doctor'])}</p>")
    if a["progress"]:
        out.append("<pre>" + _e("\n".join(a["progress"])) + "</pre>")
    out.append("</section>")

    # Usage
    out.append(f"<section>{_h2('Ліміт Claude', 'p_limit')}")
    if u:
        for k, label in (("five_hour", "5 годин"), ("seven_day", "тиждень")):
            pct = u[k]["pct"]
            out.append(
                f"<div>{label}: <b>{'—' if pct is None else f'{pct:.0f} %'}</b> <span class='mut'>· скидання {_e(_reset(u[k]['resets_at']))}</span></div>"
            )
            out.append(_bar(pct, OWNER_ONLY_AT, MAX_USAGE))
        t = u["trend"]
        if t["rate_per_day"] is not None:
            out.append(f"<p>Темп тижня ≈ <b>{t['rate_per_day']:+.1f} п. п./добу</b>.")
            if t["eta_owner_only"]:
                out.append(f" {t['owner_only_at']:.0f} % (далі лише твої картки) ≈ <b>{_when(t['eta_owner_only'])}</b>;")
            if t["eta"]:
                out.append(f" пауза агентів ({t['limit']:.0f} %) ≈ <b>{_when(t['eta'])}</b>.")
            out.append("</p>")
        else:
            out.append("<p class='mut'>Темп — коли наберуться заміри (кілька годин).</p>")
        out.append(
            f"<p class='mut'>Від {OWNER_ONLY_AT:.0f} % агенти беруть лише картки власника, від {MAX_USAGE:.0f} % — пауза. Ліміт спільний із твоїми сесіями.</p>"
        )
    else:
        out.append("<p class='mut'>Не вдалося прочитати.</p>")
    out.append("</section>")

    # Queue
    cards = s["cards"]
    owner = sorted(
        [x for x in cards if x["owner"] and x["status"] in ("accepted", "testing", "proposed")],
        key=lambda x: (x["order"] if x["order"] is not None else 99, x["name"]),
    )
    other = [x for x in cards if not x["owner"] and x["status"] in ("accepted", "testing", "proposed")]
    out.append(f"<section>{_h2('Черга', 'p_queue')}<table><tr><th>#</th><th>Картка</th><th>Статус</th><th>Кроки</th></tr>")
    for x in owner:
        d, t = rb.card_progress(x)
        out.append(
            f"<tr><td>{x['order'] if x['order'] is not None else '👤'}</td><td>{_e(x['title'])}<br><span class='mut'>{_e(x['name'][14:])}</span></td><td>{_e(x['status'])}</td>"
            f'<td style="min-width:90px">{d} / {t}<div class="bar"><i style="width:{100.0 * d / t if t else 0:.0f}%"></i></div></td></tr>'
        )
    for x in other:
        out.append(
            f"<tr><td class='mut'>·</td><td>{_e(x['title'])}<br><span class='mut'>{_e(x['name'][14:])}</span></td><td>{_e(x['status'])}</td><td></td></tr>"
        )
    out.append("</table></section>")

    # Results
    groups = [
        ("підтверджено", ("confirmed", "built"), "ok"),
        ("форвард", ("forward",), "mut"),
        ("без вердикту", ("inconclusive",), "mut"),
        ("відхилено", ("rejected",), "bad"),
    ]
    out.append(f"<section>{_h2('Результати перевірок', 'p_results')}<div class='kpi'>")
    for label, sts, cls in groups:
        out.append(f"<div><b class='{cls}'>{sum(x['status'] in sts for x in cards)}</b> {label}</div>")
    out.append("</div><table style='margin-top:8px'><tr><th>Картка</th><th>Вердикт</th></tr>")
    done = sorted(
        [x for x in cards if x["status"] in ("confirmed", "built", "inconclusive", "rejected")], key=lambda x: x["name"], reverse=True
    )
    for x in done[:12]:
        cls = {"rejected": "bad", "inconclusive": "mut"}.get(x["status"], "ok")
        out.append(
            f"<tr><td>{_e(x['title'][:70])}</td><td class='{cls}'>{_e(x['status'])}<span class='mut'> {_e(x['verdict'][:110])}</span></td></tr>"
        )
    out.append("</table><p class='mut'>Повністю — <code>docs/RESULTS.md</code>.</p></section>")

    # History
    out.append(
        f"<section class='wide'>{_h2('Кроки агентів', 'p_steps')}<table><tr><th>Коли</th><th>Результат</th><th>Що</th><th>Час і ліміт</th></tr>"
    )
    for h in a["history"]:
        cls = {"delivered": "ok", "error": "bad", "stuck": "bad"}.get(h["status"], "mut")
        out.append(
            f"<tr><td>{_e(h['when'])}</td><td class='{cls}'>{_e(STATUS_UA.get(h['status'], h['status']))}</td><td>{_e(h['text'])}</td><td class='mut'>{_e(h['cost'])}</td></tr>"
        )
    out.append("</table></section>")

    # Server
    gb = 1024**3
    out.append(f"<section>{_h2('Сервер', 'p_server')}")
    if m["mem_total"]:
        mp = m["mem_used"] / m["mem_total"] * 100
        out.append(f"<div>Пам'ять: <b>{m['mem_used'] / gb:.1f}</b> з {m['mem_total'] / gb:.0f} ГБ</div>{_bar(mp, 70, 90)}")
    dp = m["disk_used"] / m["disk_total"] * 100
    out.append(f"<div>Диск: <b>{m['disk_used'] / gb:.0f}</b> з {m['disk_total'] / gb:.0f} ГБ</div>{_bar(dp, 70, 90)}")
    extra = []
    if m["load"]:
        extra.append("load " + " / ".join(m["load"]))
    if m["uptime_s"]:
        extra.append(f"сервер працює {fmt_age(m['uptime_s']).replace(' тому', '')}")
    if m["db_bytes"]:
        extra.append(f"база {m['db_bytes'] / 1024**2:.0f} МБ")
    extra.append(f"бекап {fmt_age(m['backup_age_s'])}")
    out.append(f"<p class='mut'>{_e(' · '.join(extra))}</p></section>")

    # Collector
    out.append(f"<section>{_h2('Колектор даних', 'p_collector')}<table><tr><th>Дані</th><th>Останній запис</th><th>Рядків</th></tr>")
    for t in s["tables"]:
        cls = "mut" if t["ok"] is None else ("ok" if t["ok"] else "bad")
        out.append(
            f"<tr><td>{_e(t['label'])}</td><td class='{cls}'>{_e(fmt_age(t['age_s']))}</td><td>{t['rows']:,}".replace(",", " ")
            + "</td></tr>"
        )
    out.append("</table></section>")

    # Commits and questions
    out.append(f"<section>{_h2('Останні зміни в main', 'p_changes')}<table>")
    for row in s["commits"]:
        if len(row) == 3:
            out.append(f"<tr><td class='mut'>{_e(row[1])}</td><td>{_e(row[2][:110])}</td></tr>")
    out.append("</table></section>")
    if s["questions"]:
        out.append(
            f"<section>{_h2('Питання агентів до тебе', 'p_questions')}<ul>"
            + "".join(f"<li>{_e(q)}</li>" for q in s["questions"])
            + "</ul></section>"
        )
    out.append("</main></body></html>")
    return with_help("".join(out))


# ---------------------------------------------------------------- assistant brief tab (snapshot JSON only, no network)

SNAPSHOT_PATH = Path(os.environ.get("TRADEDESK_ASSISTANT_SNAPSHOT_PATH") or DATA_DIR / "assistant" / "snapshot.json")
SNAPSHOT_SCHEMA = 1
SNAPSHOT_STALE_S = 30 * 60  # the job runs every 10 minutes: older than this means it stopped or failed
SNAPSHOT_OLD_S = 6 * 3600
MONTHS_UA = ("січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру")
_MON_EN = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
FNG_UA = {
    "extreme_fear": "крайній страх",
    "fear": "страх",
    "neutral": "нейтрально",
    "greed": "жадібність",
    "extreme_greed": "крайня жадібність",
}
TERM_SHAPE_UA = {
    "contango": "у контанго: далекі строки дорожчі за близькі",
    "backwardation": "у беквордації: близькі строки дорожчі за далекі",
    "flat": "пласка",
}
IMPACT_UA = {"high": "високий", "medium": "середній", "low": "низький"}
VENUE_NAMES = {"binanceusdm": "Binance", "hyperliquid": "Hyperliquid", "okx": "OKX", "deribit": "Deribit", "bybit": "Bybit"}
SECTION_UA = {
    "tickers": "ціни",
    "ohlc": "свічки BTC",
    "fng": "страх і жадібність",
    "dvol": "DVOL",
    "vrp": "VRP / реалізована вола",
    "term": "крива строків",
    "skew": "скью",
    "funding_btc": "funding BTC",
    "funding_eth": "funding ETH",
    "events": "календар подій",
    "em_week": "очікуваний рух (тиждень)",
    "em_month": "очікуваний рух (місяць)",
    "chain": "ланцюжок опціонів",
    "chain_near": "ланцюжок опціонів (найближча експірація)",
    "radar": "радар",
    "paper_rec": "паперова рекомендація",
}
BRIEF_CAVEATS = (
    "Ймовірності з цін опціонів — ризик-нейтральні: це ціна страхування в ринку, а не прогноз; "
    "реальні ймовірності відрізняються на премію за ризик.",
    "Премія волатильності (VRP) тут рахується за DVOL; перевірка на марках опціонів ще триває (картка vrp-real-marks).",
    "Не інвестиційна порада: сторінка лише показує дані й нічого не торгує.",
)
BRIEF_CSS = (
    """
main.brief{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.tile{border:1px solid var(--line);border-radius:6px;padding:6px 10px;display:flex;flex-direction:column;min-width:0}
.tile .k{font-size:12px;color:var(--mut)}.tile .v{font:600 19px Menlo,Consolas,monospace}.tile .s{font-size:12px;color:var(--mut)}
.num{font-family:Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
ul.key{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}ul.key li{display:grid;grid-template-columns:auto 1fr;gap:10px}
.chip{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border-radius:999px;padding:1px 8px;white-space:nowrap;height:fit-content}
.chip.calm{background:rgba(var(--acc-rgb),.16);color:var(--acc)}.chip.risk{background:rgba(var(--bad-rgb),.16);color:var(--bad)}
.chip.opp{background:rgba(var(--ok-rgb),.16);color:var(--ok)}.chip.ev{background:rgba(var(--warn-rgb),.18);color:var(--warn)}
.hbars{display:flex;flex-direction:column;gap:5px}
.hb{display:grid;grid-template-columns:minmax(90px,130px) 1fr 56px;gap:8px;align-items:center;font-size:13px}
.hb .t{height:10px;background:var(--line);border-radius:5px;overflow:hidden}.hb .t i{display:block;height:100%;background:var(--acc)}
.hb .t i.neg{background:var(--bad)}.hb .r{text-align:right}
svg{display:block;width:100%;height:auto}svg text{font:11px Menlo,Consolas,monospace;fill:var(--mut)}
.note{font-size:12px;color:var(--mut);margin:6px 0 0}.empty{color:var(--mut);font-style:italic}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--mut)}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.caveat{border:1px dashed var(--warn);border-radius:8px;padding:6px 12px;margin:0 12px 12px;font-size:13px}
.caveat ul{margin:4px 0;padding-left:18px}
details{font-size:12px;color:var(--mut);margin-top:6px}
"""
    + HELP_CSS
)


def load_snapshot(path: Path) -> tuple[dict | None, str | None]:
    """The snapshot dict, or (None, reason) with reason 'missing' | 'corrupt' | 'unreadable: …'. Never raises."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        snap = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "corrupt"
    if not isinstance(snap, dict) or not isinstance(snap.get("sections"), dict):
        return None, "corrupt"
    return snap, None


def _num(x: object) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if math.isfinite(v) else None


def _section(snap: dict, name: str) -> dict:
    secs = snap.get("sections")
    sec = secs.get(name) if isinstance(secs, dict) else None
    return sec if isinstance(sec, dict) else {}


def _data(snap: dict, name: str) -> dict | None:
    """The data of a section that has it (fresh or carried forward), else None."""
    sec = _section(snap, name)
    return sec["data"] if sec.get("ok") and isinstance(sec.get("data"), dict) else None


def _list(x: object) -> list:
    return x if isinstance(x, list) else []


def parse_utc(s: object) -> datetime | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)  # noqa: UP017 - runs on the system python3
    except ValueError:
        return None


def expiry_utc(token: object) -> datetime | None:
    """Deribit expiry token ('27NOV26') -> its delivery time, 08:00 UTC."""
    m = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", str(token or ""))
    if not m or m.group(2) not in _MON_EN:
        return None
    try:
        return datetime(2000 + int(m.group(3)), _MON_EN[m.group(2)], int(m.group(1)), 8, tzinfo=timezone.utc)  # noqa: UP017
    except ValueError:
        return None


def _kyiv_tz() -> ZoneInfo | None:
    for name in ("Europe/Kyiv", "Europe/Kiev"):
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - no tzdata: times are shown in UTC instead
            continue
    return None


KYIV = _kyiv_tz()


def kyiv(dt: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    if dt is None:
        return "—"
    if KYIV is None:
        return dt.astimezone(timezone.utc).strftime(fmt) + " UTC"  # noqa: UP017
    return dt.astimezone(KYIV).strftime(fmt)


def _nf(x: object, d: int = 0) -> str:
    v = _num(x)
    return "—" if v is None else f"{v:,.{d}f}".replace(",", " ").replace("-", "−")


def _pc(x: object, d: int = 1) -> str:
    """Signed percent of a number already in percent units."""
    v = _num(x)
    return "—" if v is None else f"{v:+.{d}f} %".replace("-", "−")


def _k(x: object) -> str:
    v = _num(x)
    if v is None:
        return "—"
    if abs(v) < 10000:
        return _nf(v)
    return (f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k").replace("-", "−")


def nice_step(span: float, target: int) -> float:
    raw = span / max(target, 1)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= raw:
            return m * p
    return 10 * p


# ---- risk-neutral range probabilities (a stdlib copy of analytics.probability.prob_above; tests compare the two)


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def chain_smile(chain: dict) -> tuple[float, float, list[tuple[float, float]]] | None:
    """(forward, T in years, [(strike, iv)]) of the OTM smile from a deribit_option_chain section: puts below the
    forward, calls above (the other side when one is missing). None when the chain is unusable."""
    F, days = _num(chain.get("forward")), _num(chain.get("days"))
    if F is None or F <= 0 or days is None or days <= 0:
        return None
    pts: dict[float, float] = {}
    for row in _list(chain.get("strikes")):
        if not isinstance(row, dict):
            continue
        k = _num(row.get("strike"))
        if k is None or k <= 0:
            continue
        for side in ("put", "call") if k < F else ("call", "put"):
            leg = row.get(side)
            iv = _num(leg.get("iv")) if isinstance(leg, dict) else None
            if iv is not None and iv > 0:
                pts[k] = iv
                break
    if len(pts) < 2:
        return None
    return F, days / 365.0, sorted(pts.items())


def smile_iv(pts: list[tuple[float, float]], F: float, K: float) -> float:
    """Linear interpolation in log-moneyness, flat outside the quoted strikes (as analytics.options.Smile.iv)."""
    x = math.log(K / F)
    xs = [math.log(k / F) for k, _ in pts]
    if x <= xs[0]:
        return pts[0][1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1])
    return pts[-1][1]


def prob_above(pts: list[tuple[float, float]], F: float, T: float, K: float) -> float:
    """Risk-neutral P(S_T > K) = N(d2) - vega * dsigma/dK (Breeden-Litzenberger with the smile slope), clipped to [0, 1]."""
    sigma = smile_iv(pts, F, K)
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    h = 0.005
    slope = (smile_iv(pts, F, K * (1 + h)) - smile_iv(pts, F, K * (1 - h))) / (K * 2 * h)
    vega_unit = F * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * math.sqrt(T)
    return min(1.0, max(0.0, _ncdf(d1 - v) - vega_unit * slope))


def scenario_cuts(F: float) -> list[float]:
    """Six round price levels around the forward (step ~6 % of it): 70k..95k for a forward of 83k."""
    step = nice_step(F * 0.06, 1)
    base = math.floor(F / step) * step
    return [base + (i - 2) * step for i in range(6)]


def range_probabilities(pts: list[tuple[float, float]], F: float, T: float, cuts: list[float]) -> list[dict]:
    """Bands below the first cut, between cuts and above the last, each with its risk-neutral probability (they sum to 1)
    and the smile IV at the band's middle (at the cut for the open tails)."""
    above: list[float] = []
    for c in cuts:
        p = prob_above(pts, F, T, c)
        above.append(min(p, above[-1]) if above else p)  # keep P(S > K) non-increasing in K
    bands = [{"lo": None, "hi": cuts[0], "p": 1.0 - above[0], "iv": smile_iv(pts, F, cuts[0])}]
    for i in range(len(cuts) - 1):
        mid = (cuts[i] + cuts[i + 1]) / 2
        bands.append({"lo": cuts[i], "hi": cuts[i + 1], "p": above[i] - above[i + 1], "iv": smile_iv(pts, F, mid)})
    bands.append({"lo": cuts[-1], "hi": None, "p": above[-1], "iv": smile_iv(pts, F, cuts[-1])})
    return bands


# ---- charts (inline SVG, scaled to the card width by the viewBox)


def price_svg(rows: list, cone: tuple | None = None, week: tuple | None = None) -> str:
    """Daily closes plus the options' ±1σ cone to the monthly expiry and the weekly straddle breakevens.

    rows: [[ts_ms, open, high, low, close, ...]]; cone / week: (expiry ts_ms, low, high). '' when there is too little data."""
    pts = []
    for r in rows:
        if isinstance(r, list) and len(r) >= 5:
            t, h, lo_, c = _num(r[0]), _num(r[2]), _num(r[3]), _num(r[4])
            if None not in (t, h, lo_, c):
                pts.append((t, c, h, lo_))
    if len(pts) < 2:
        return ""
    pts.sort()
    W, H, L, R, Tp, B = 720, 280, 58, 12, 10, 26
    t0, t_last, last = pts[0][0], pts[-1][0], pts[-1][1]
    extras = [x for x in (cone, week) if x and x[0] > t_last]
    t_end = max([t_last] + [x[0] for x in extras])
    lo = min([p[3] for p in pts] + [x[1] for x in extras]) * 0.98
    hi = max([p[2] for p in pts] + [x[2] for x in extras]) * 1.02
    if hi <= lo or t_end <= t0:
        return ""

    def X(t: float) -> float:
        return L + (t - t0) / (t_end - t0) * (W - L - R)

    def Y(p: float) -> float:
        return Tp + (hi - p) / (hi - lo) * (H - Tp - B)

    g = []
    st_ = nice_step(hi - lo, 5)
    p = math.ceil(lo / st_) * st_
    while p <= hi:
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(p) + 4:.1f}" text-anchor="end">{_e(_k(p))}</text>'
        )
        p += st_
    d = datetime.fromtimestamp(t0 / 1000, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # noqa: UP017
    while True:
        d = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
        t = d.timestamp() * 1000
        if t > t_end:
            break
        g.append(
            f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{Tp}" y2="{H - B}" style="stroke:var(--line)"/>'
            f'<text x="{X(t):.1f}" y="{H - 8}" text-anchor="middle">{MONTHS_UA[d.month - 1]}</text>'
        )
    if cone and cone[0] > t_last:
        te, clo, chi = cone
        g.append(
            f'<polygon points="{X(t_last):.1f},{Y(last):.1f} {X(te):.1f},{Y(chi):.1f} {X(te):.1f},{Y(clo):.1f}" '
            'style="fill:var(--acc);fill-opacity:.15;stroke:var(--acc);stroke-width:1"/>'
            f'<text x="{X(te) - 4:.1f}" y="{Y(chi) - 6:.1f}" text-anchor="end" style="fill:var(--acc)">{_e(_k(chi))}</text>'
            f'<text x="{X(te) - 4:.1f}" y="{Y(clo) + 14:.1f}" text-anchor="end" style="fill:var(--acc)">{_e(_k(clo))}</text>'
        )
    if week and week[0] > t_last:
        for p in week[1:]:
            g.append(
                f'<line x1="{X(t_last):.1f}" x2="{X(week[0]):.1f}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" '
                'style="stroke:var(--warn);stroke-width:2;stroke-dasharray:4 3"/>'
            )
    path = "".join(f"{'L' if i else 'M'}{X(t):.1f},{Y(c):.1f}" for i, (t, c, _, _) in enumerate(pts))
    g.append(f'<path d="{path}" style="fill:none;stroke:var(--fg);stroke-width:1.6"/>')
    g.append(f'<circle cx="{X(t_last):.1f}" cy="{Y(last):.1f}" r="3.5" style="fill:var(--fg)"/>')
    g.append(f'<text x="{X(t_last) - 6:.1f}" y="{Y(last) - 8:.1f}" text-anchor="end" style="fill:var(--fg)">{_e(_nf(last))}</text>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="BTC, денні закриття">{"".join(g)}</svg>'


def term_svg(points: list[tuple[float, float]]) -> str:
    """ATM IV (decimal) by days to expiry, square-root time axis. '' with fewer than two points."""
    pts = sorted((d, iv * 100) for d, iv in points if d > 0)
    if len(pts) < 2:
        return ""
    W, H, L, R, Tp, B = 520, 150, 44, 10, 10, 24
    dmax = pts[-1][0]
    lo = math.floor(min(v for _, v in pts) - 1)
    hi = math.ceil(max(v for _, v in pts) + 1)

    def X(d: float) -> float:
        return L + math.sqrt(d / dmax) * (W - L - R)

    def Y(v: float) -> float:
        return Tp + (hi - v) / (hi - lo) * (H - Tp - B)

    g = []
    st_ = nice_step(hi - lo, 3)
    p = math.ceil(lo / st_) * st_
    while p <= hi:
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(p) + 4:.1f}" text-anchor="end">{_e(_nf(p, 0 if st_ >= 1 else 1))}</text>'
        )
        p += st_
    for d in (1, 7, 30, 90, 180, 365):
        if d <= dmax:
            g.append(f'<text x="{X(d):.1f}" y="{H - 6}" text-anchor="middle">{d} дн</text>')
    g.append(
        '<path d="'
        + "".join(f"{'L' if i else 'M'}{X(d):.1f},{Y(v):.1f}" for i, (d, v) in enumerate(pts))
        + '" style="fill:none;stroke:var(--acc);stroke-width:2"/>'
    )
    g += [f'<circle cx="{X(d):.1f}" cy="{Y(v):.1f}" r="2.5" style="fill:var(--acc)"/>' for d, v in pts]
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="ATM IV за строками">{"".join(g)}</svg>'


# ---- blocks: each returns HTML for one card body; data comes only from the snapshot, every string is escaped


def _empty(text: str) -> str:
    return f"<p class='empty'>{_e(text)}</p>"


def _clean(s: object, n: int = 160) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()[:n]


def _section_notes(snap: dict, *names: str) -> str:
    """Why a block is empty or old: the section's failure reason or its own (older) time."""
    notes = []
    for n in names:
        sec, label = _section(snap, n), SECTION_UA.get(n, n)
        if not sec.get("ok"):
            miss = _list(sec.get("missing"))
            first = miss[0] if miss and isinstance(miss[0], dict) else {}
            why = _clean(first.get("detail") or first.get("reason") or "секції немає в знімку")
            notes.append(f"немає даних «{label}»: {why}")
        elif sec.get("stale"):
            notes.append(f"«{label}» — з попереднього знімка, дані на {kyiv(parse_utc(sec.get('asof_utc')))} за Києвом")
    return "".join(f"<p class='note warn'>{_e(x)}</p>" for x in notes)


def _hbar(label: str, frac: float, value: str, neg: bool = False, bold: bool = False) -> str:
    w = max(0.0, min(1.0, frac)) * 100
    lab = f"<b>{label}</b>" if bold else label
    return f"<div class='hb'><span>{lab}</span><span class='t'><i class='{'neg' if neg else ''}' style='width:{w:.0f}%'></i></span><span class='r num'>{value}</span></div>"


def _tickers(snap: dict) -> dict[str, dict]:
    out = {}
    for t in _list((_data(snap, "tickers") or {}).get("tickers")):
        if isinstance(t, dict) and isinstance(t.get("symbol"), str):
            out[t["symbol"].split("/")[0]] = t
    return out


def _tile(k: str, v: str, s: str = "", cls: str = "", help: str = "", tone: str = "", badge: str = "") -> str:
    """A number tile; `k`, `v`, `s` are trusted HTML. `help` is a HELP key, `tone` good|warn|bad colours the edge and
    the `badge` line (plain text, escaped here)."""
    sig = f"<span class='sig {tone}'>{_e(badge)}</span>" if badge else ""
    return (
        f"<div class='tile{f' t-{tone}' if tone else ''}'><span class='k'>{k}{_info(help)}</span><span class='v'>{v}</span>"
        f"<span class='s {cls}'>{s}</span>{sig}</div>"
    )


def _fng_zone(latest: dict) -> str:
    z = latest.get("zone") or latest.get("classification")
    return FNG_UA.get(str(z), _clean(z, 40)) if z else ""


def block_tiles(snap: dict) -> str:
    tiles = []
    tick = _tickers(snap)
    for base, t in tick.items():
        last, pct = _num(t.get("last")), _num(t.get("percentage"))
        if last is None:
            continue
        tiles.append(
            _tile(
                _e(base),
                _e(_nf(last, 0 if last >= 1000 else 2)),
                _e(f"{_pc(pct, 2)} за 24 год"),
                "" if pct is None else ("ok" if pct >= 0 else "bad"),
                help="price",
            )
        )
    fng = _data(snap, "fng") or {}
    latest = fng.get("latest") if isinstance(fng.get("latest"), dict) else {}
    if _num(latest.get("value")) is not None:
        s = f"{_fng_zone(latest)} · 7 дн {_nf(fng.get('mean_7d'))}, 30 дн {_nf(fng.get('mean_30d'))}"
        fv = _num(latest["value"])
        extreme = fv <= 25 or fv >= 75
        tiles.append(
            _tile(
                "Страх і жадібність",
                _e(_nf(fv)),
                _e(s),
                help="fng",
                tone="warn" if extreme else "",
                badge="крайність: менший розмір" if extreme else "",
            )
        )
    cur = (_data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    dvol = _num(cur.get("dvol"))
    if dvol is None:
        lat = (_data(snap, "dvol") or {}).get("latest")
        v = _num(lat.get("value_pct")) if isinstance(lat, dict) else None
        dvol = v / 100 if v is not None else None
    if dvol is not None:
        pr = _num(cur.get("dvol_percentile_365d"))
        tone = (vol_lean(pr, None) or [("", "")])[0][0]
        tiles.append(
            _tile(
                "DVOL (IV BTC, 30 дн)",
                _e(_nf(dvol * 100, 1)),
                _e(f"{_nf(pr * 100)}-й перцентиль за рік" if pr is not None else "перцентиля немає"),
                help="dvol",
                tone=tone,
                badge=lean_badge(tone),
            )
        )
    body = f"<div class='tiles'>{''.join(tiles)}</div>" if tiles else _empty("Цін у знімку немає.")
    return body + _section_notes(snap, "tickers", "fng", "vrp")


def _em(snap: dict, name: str) -> dict | None:
    rows = _list((_data(snap, name) or {}).get("expiries"))
    return rows[0] if rows and isinstance(rows[0], dict) else None


def _exp_label(snap: dict, key: str) -> str:
    exp = (snap.get("expiries") or {}).get(key) if isinstance(snap.get("expiries"), dict) else None
    return kyiv(expiry_utc(exp), "%d.%m") if expiry_utc(exp) else "—"


def _skew_ref(snap: dict) -> dict | None:
    rows = [x for x in _list((_data(snap, "skew") or {}).get("expiries")) if isinstance(x, dict) and _num(x.get("rr")) is not None]
    if not rows:
        return None
    return next((x for x in rows if (_num(x.get("days")) or 0) > 7), rows[0])


def _in_hours(h: float | None) -> str:
    if h is None:
        return ""
    return f"через {h:.0f} год" if h < 48 else f"через {h / 24:.0f} дн"


def block_key(snap: dict) -> str:
    items: list[tuple[str, str, str]] = []  # (chip class, heading, escaped HTML text)
    btc = _tickers(snap).get("BTC")
    fng = _data(snap, "fng") or {}
    latest = fng.get("latest") if isinstance(fng.get("latest"), dict) else {}
    parts = []
    if btc and _num(btc.get("last")) is not None:
        parts.append(f"BTC <b class='num'>{_e(_nf(btc['last']))}</b>, {_e(_pc(btc.get('percentage')))} за добу.")
    if _num(latest.get("value")) is not None:
        parts.append(
            _e(
                f"Страх і жадібність {_nf(latest['value'])} ({_fng_zone(latest)}); у середньому за 7 дн {_nf(fng.get('mean_7d'))}, за 30 дн {_nf(fng.get('mean_30d'))}."
            )
        )
    if parts:
        items.append(("calm", "Ринок", " ".join(parts)))
    w, m = _em(snap, "em_week"), _em(snap, "em_month")
    parts = []
    if w and _num(w.get("straddle_pct")) is not None:
        parts.append(
            f"Опціони закладають ±{_nf(w['straddle_pct'], 1)} % до {_exp_label(snap, 'week')} "
            f"(беззбиток тижневого стреддлу {_k(w.get('breakeven_lo'))} – {_k(w.get('breakeven_hi'))})"
        )
    if m and _num(m.get("sigma_move_pct")) is not None:
        parts.append(
            f"±{_nf(m['sigma_move_pct'], 1)} % (1σ) до {_exp_label(snap, 'month')} ({_k(m.get('sigma_lo'))} – {_k(m.get('sigma_hi'))})"
        )
    if parts:
        items.append(("calm", "Діапазон", _e(" і ".join(parts) + ".")))
    cur = (_data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    dvol, pr, spr = _num(cur.get("dvol")), _num(cur.get("dvol_percentile_365d")), _num(cur.get("spread_vs_rv30"))
    if dvol is not None:
        txt = f"DVOL {_nf(dvol * 100, 1)}"
        if pr is not None:
            txt += f" — {_nf(pr * 100)}-й перцентиль за рік"
        if spr is not None:
            txt += f"; премія IV − RV30 {_pc(spr * 100, 1).replace(' %', '')} вп"
        txt += ". Рівень волатильності сам по собі не сигнал: метод «купувати дешеву волатильність» перевірено 18.10 і відхилено."
        items.append(("risk" if pr is not None and pr >= 0.8 else "calm", "Волатильність", _e(txt)))
    sk = _skew_ref(snap)
    if sk:
        rr = _num(sk["rr"])
        exp = _clean(sk.get("expiry"), 12)
        if rr < 0:
            items.append(
                (
                    "risk" if rr <= -0.02 else "calm",
                    "Захист",
                    _e(
                        f"Пути 25Δ дорожчі за коли на {_nf(-rr * 100, 1)} вп волатильності ({exp}): ринок платить за страховку від падіння."
                    ),
                )
            )
        else:
            items.append(
                (
                    "calm",
                    "Скью",
                    _e(f"Коли 25Δ дорожчі за пути на {_nf(rr * 100, 1)} вп волатильності ({exp}): ринок платить за участь у зростанні."),
                )
            )
    ev = _data(snap, "events")
    if ev is not None:
        nxt = ev.get("next_event") if isinstance(ev.get("next_event"), dict) else None
        if nxt:
            when = kyiv(_iso(nxt.get("ts_utc")))
            txt = f"{_clean(nxt.get('title'), 80)} — {when} за Києвом, {_in_hours(_num(nxt.get('hours_ahead')))}."
            if ev.get("high_impact_within_48h") is True:
                items.append(
                    ("risk", "Подія", _e(txt + " До події з високим впливом менше 48 год: менший розмір або позиція з обмеженим ризиком."))
                )
            else:
                items.append(("ev", "Подія", _e(txt)))
        else:
            items.append(("calm", "Подія", _e("Подій з високим впливом у календарі на 14 днів немає.")))
    if not items:
        return _empty("Даних для головного немає.") + _section_notes(snap, "tickers", "em_week", "em_month", "vrp", "skew", "events")
    lab = {"calm": "спокій", "opp": "нагода", "risk": "ризик", "ev": "подія"}
    return (
        "<ul class='key'>"
        + "".join(f"<li><span class='chip {c}'>{lab[c]}</span><span><b>{_e(h)}.</b> {t}</span></li>" for c, h, t in items)
        + "</ul>"
    )


def _iso(s: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # noqa: UP017


def block_chart(snap: dict) -> str:
    rows = _list((_data(snap, "ohlc") or {}).get("rows"))
    cone = week = None
    m, w = _em(snap, "em_month"), _em(snap, "em_week")
    tm, tw = expiry_utc((snap.get("expiries") or {}).get("month")), expiry_utc((snap.get("expiries") or {}).get("week"))
    if m and tm and _num(m.get("sigma_lo")) and _num(m.get("sigma_hi")):
        cone = (tm.timestamp() * 1000, _num(m["sigma_lo"]), _num(m["sigma_hi"]))
    if w and tw and _num(w.get("breakeven_lo")) and _num(w.get("breakeven_hi")):
        week = (tw.timestamp() * 1000, _num(w["breakeven_lo"]), _num(w["breakeven_hi"]))
    svg = price_svg(rows, cone, week)
    if not svg:
        return _empty("Свічок BTC у знімку немає.") + _section_notes(snap, "ohlc")
    legend = ["<span><i style='background:var(--fg)'></i>ціна закриття (Binance, день)</span>"]
    if cone:
        legend.append(f"<span><i style='background:var(--acc)'></i>±1σ до {_exp_label(snap, 'month')} (опціони)</span>")
    if week:
        legend.append(f"<span><i style='background:var(--warn)'></i>беззбиток стреддлу до {_exp_label(snap, 'week')}</span>")
    return svg + f"<div class='legend'>{''.join(legend)}</div>" + _section_notes(snap, "ohlc", "em_month", "em_week")


def block_scenarios(snap: dict) -> str:
    chain = _data(snap, "chain")
    sm = chain_smile(chain) if chain else None
    if not sm:
        return _empty("Ланцюжка опціонів у знімку немає: сценарії не пораховано.") + _section_notes(snap, "chain")
    F, T, pts = sm
    bands = range_probabilities(pts, F, T, scenario_cuts(F))
    mx = max(b["p"] for b in bands) or 1.0
    rows = []
    for b in reversed(bands):
        if b["lo"] is None:
            label = f"нижче {_k(b['hi'])}"
        elif b["hi"] is None:
            label = f"вище {_k(b['lo'])}"
        else:
            label = f"{_k(b['lo'])} – {_k(b['hi'])}"
        here = (b["lo"] is None or b["lo"] <= F) and (b["hi"] is None or F < b["hi"])
        rows.append(
            _hbar(
                _e(label) + f" <span class='mut'>IV {_e(_nf(b['iv'] * 100, 0))}</span>",
                b["p"] / mx,
                _e(f"{_nf(b['p'] * 100)} %"),
                bold=here,
            )
        )
    note = (
        f"Ризик-нейтральні ймовірності з ланцюжка Deribit на {_e(_exp_label(snap, 'month'))} (IV на кожному страйку, нахил усмішки враховано), "
        f"форвард {_e(_nf(F))} (жирним — його діапазон). Це ціна страхування в ринку, а не наш прогноз: реальні ймовірності "
        "відрізняються на премію за ризик. Прогнози асистента з'являться окремо, з оцінкою точності (Brier)."
    )
    return f"<div class='hbars'>{''.join(rows)}</div><p class='note'>{note}</p>" + _section_notes(snap, "chain")


def block_vol(snap: dict) -> str:
    vrp = _data(snap, "vrp") or {}
    cur = vrp.get("current") if isinstance(vrp.get("current"), dict) else {}
    dv = _data(snap, "dvol") or {}
    tiles = []
    dvol = _num(cur.get("dvol"))
    if dvol is not None:
        ch = _num(dv.get("change_7d_pts"))
        tiles.append(
            _tile("DVOL", _e(_nf(dvol * 100, 1)), _e(f"за 7 днів {_pc(ch, 1).replace(' %', '')} вп" if ch is not None else ""), help="dvol")
        )
    rv30, rv7 = _num(cur.get("rv_30d")), _num(cur.get("rv_7d"))
    if rv30 is not None:
        tiles.append(
            _tile("Реалізована 30 дн", _e(_nf(rv30 * 100, 1)), _e(f"7 дн {_nf(rv7 * 100, 1)}" if rv7 is not None else ""), help="rv")
        )
    spr = _num(cur.get("spread_vs_rv30"))
    if spr is not None:
        tone = vol_lean(None, spr)[0][0]
        tiles.append(
            _tile(
                "Премія IV − RV30",
                _e(_pc(spr * 100, 1).replace(" %", "")),
                "пунктів волатильності",
                help="vrp",
                tone=tone,
                badge=lean_badge(tone),
            )
        )
    out = [f"<div class='tiles'>{''.join(tiles)}</div>"] if tiles else []
    term = _data(snap, "term") or {}
    pts = []
    for x in _list(term.get("expiries")):
        if isinstance(x, dict) and _num(x.get("days")) is not None and _num(x.get("atm_iv")) is not None:
            pts.append((_num(x["days"]), _num(x["atm_iv"])))
    svg = term_svg(pts)
    if svg:
        out.append(
            svg
            + f"<div class='legend'><span><i style='background:var(--acc)'></i>ATM IV за строками експірації, %{_info('term')}</span>"
            + f"<span>скью{_info('skew')}</span></div>"
        )
    notes = []
    shape = term.get("shape")
    if svg and isinstance(shape, str):
        notes.append(f"Крива {TERM_SHAPE_UA.get(shape, _clean(shape, 30))}.")
    sk = [
        x
        for x in _list((_data(snap, "skew") or {}).get("expiries"))
        if isinstance(x, dict) and _num(x.get("rr")) is not None and (_num(x.get("days")) or 0) > 1
    ]
    if sk:
        a, b = sk[0], sk[-1]
        notes.append(
            f"Скью (RR 25Δ, кол − пут): {_pc(_num(a['rr']) * 100, 1).replace(' %', '')} вп ({_clean(a.get('expiry'), 12)}) і "
            f"{_pc(_num(b['rr']) * 100, 1).replace(' %', '')} вп ({_clean(b.get('expiry'), 12)}); від'ємний — пути дорожчі."
        )
    cell = next((x for x in _list(vrp.get("by_dvol_level")) if isinstance(x, dict) and x.get("current") is True), None)
    if cell and _num(cell.get("mean_straddle_pnl")) is not None:
        notes.append(
            f"Бектест на поточному рівні DVOL ({_clean(cell.get('dvol_percentile_365d'), 12)} перцентиля за рік): короткий 30-денний стреддл ATM "
            f"з щоденним дельта-хеджем у середньому {_pc(_num(cell['mean_straddle_pnl']) * 100, 2)} спота, у плюсі {_nf((_num(cell.get('share_positive')) or 0) * 100)} %, "
            f"n = {_nf(cell.get('n_starts'))} перекривних стартів. Історія за DVOL, не за марками; не сигнал."
        )
    if notes:
        out.append(f"<p class='note'>{_e(' '.join(notes))}</p>")
    warns = [_clean(x, 1200) for x in _list(_section(snap, "vrp").get("warnings")) if isinstance(x, str)]
    if warns and vrp:
        out.append("<details><summary>Бектест VRP: результат і межі</summary>" + "".join(f"<p>{_e(x)}</p>" for x in warns) + "</details>")
    if not out:
        return _empty("Даних про волатильність у знімку немає.") + _section_notes(snap, "vrp", "dvol", "term", "skew")
    return "".join(out) + _section_notes(snap, "vrp", "dvol", "term", "skew")


def market_lean(snap: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(buyer items, seller items) for options now: the price of volatility, the term curve, the skew and the next
    high-impact event. Tones: good | bad | warn | '' (neutral)."""
    cur = (_data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    lean = vol_lean(_num(cur.get("dvol_percentile_365d")), _num(cur.get("spread_vs_rv30")))
    buy = [(t, b) for t, b, _ in lean]
    sell = [(_flip(t), x) for t, _, x in lean]
    shape = (_data(snap, "term") or {}).get("shape")
    if shape == "backwardation":
        buy.append(("warn", "Близькі експірації дорожчі за далекі (беквордація): ринок чекає руху вже зараз, короткі опціони дорогі"))
        sell.append(("warn", "Беквордація: ринок чекає руху вже зараз — продаж коротких опціонів ризикованіший"))
    elif shape == "contango":
        buy.append(("", "Крива строків у контанго: звичний спокійний стан"))
        sell.append(("", "Крива строків у контанго: звичний спокійний стан"))
    sk = _skew_ref(snap)
    if sk and _num(sk.get("rr")) is not None and _num(sk["rr"]) <= -0.04:
        rr = f"{_num(sk['rr']) * 100:.1f}".replace("-", "−")
        buy.append(("warn", f"Пути помітно дорожчі за коли (скью {rr} вп): захист від падіння дорогий, коли відносно дешевші"))
        sell.append(("warn", f"Пути помітно дорожчі за коли (скью {rr} вп): продаж путів платить більше, але ринок боїться обвалу"))
    ev = _data(snap, "events") or {}
    nxt = ev.get("next_event") if isinstance(ev.get("next_event"), dict) else None
    if ev.get("high_impact_within_48h") is True and nxt:
        t = f"{_clean(nxt.get('title'), 60)} {_in_hours(_num(nxt.get('hours_ahead')))}"
        buy.append(("warn", f"{t}: перед подією опціони зазвичай дорожчають — менший розмір"))
        sell.append(("bad", f"{t}: різкий рух після релізу б'є по проданих опціонах — менший розмір або захист"))
    return buy, sell


def block_conditions(snap: dict) -> str:
    buy, sell = market_lean(snap)
    if not buy and not sell:
        return _empty("Даних для оцінки в знімку немає.") + _section_notes(snap, "vrp", "term", "skew", "events")
    return (
        "<div class='cols2'><div><h3>Купувати опціони</h3>"
        + pros_cons_html(buy)
        + "</div><div><h3>Продавати опціони</h3>"
        + pros_cons_html(sell)
        + "</div></div><p class='note'>Оцінка ціни волатильності й ризику подій, а не напряму ціни. Метод «обирати угоду за режимом "
        "волатильності» перевірено 18.10 і відхилено: це підказка, де дорожче чи дешевше, а не сигнал входу.</p>"
    )


def merge_same_day(events: object) -> list[dict]:
    """Calendar rows of one kind on one UTC day as one row (a live calendar lists one FOMC as statement + rate + press
    conference): the earliest row, the other titles appended, high impact if any row is high. Rows without a kind stay."""
    out: list[dict] = []
    by_key: dict[tuple[str, str], dict] = {}
    for x in _list(events):
        if not isinstance(x, dict):
            continue
        kind, day = str(x.get("kind") or ""), str(x.get("ts_utc") or "")[:10]
        if not kind or not day:
            out.append(x)
            continue
        m = by_key.get((kind, day))
        if m is None:
            by_key[(kind, day)] = m = dict(x, title=_clean(x.get("title"), 100))
            out.append(m)
            continue
        title = _clean(x.get("title"), 100)
        if title and title not in m["title"].split(" · "):
            m["title"] = f"{m['title']} · {title}"
        if x.get("impact") == "high":
            m["impact"] = "high"
    return out


def block_events(snap: dict) -> str:
    ev = _data(snap, "events")
    if ev is None:
        return _empty("Календаря подій у знімку немає.") + _section_notes(snap, "events")
    rows = []
    for x in merge_same_day(ev.get("events"))[:12]:
        if not isinstance(x, dict):
            continue
        when = kyiv(_iso(x.get("ts_utc")))
        imp = str(x.get("impact") or "")
        cls = "bad" if imp == "high" else "mut"
        imp = IMPACT_UA.get(imp, imp)
        seed = " · час орієнтовний" if x.get("source") == "seed" else ""
        rows.append(
            f"<tr><td class='num' style='white-space:nowrap'>{_e(when)}</td><td>{_e(_clean(x.get('title'), 100))}"
            f"<br><span class='mut' style='font-size:12px'>{_e(_in_hours(_num(x.get('hours_ahead'))))}{_e(seed)}</span></td>"
            f"<td class='{cls}'>{_e(_clean(imp, 12))}</td></tr>"
        )
    body = f"<table>{''.join(rows)}</table>" if rows else _empty("Подій на 14 днів у календарі немає.")
    warns = [_clean(x, 300) for x in _list(_section(snap, "events").get("warnings")) if isinstance(x, str)]
    miss = [_clean((m or {}).get("field", "") if isinstance(m, dict) else m, 60) for m in _list(_section(snap, "events").get("missing"))]
    if miss:
        warns.append("Неповне джерело: " + ", ".join(miss))
    body += "<p class='note'>За 48 год до події з високим впливом — менший розмір або позиція з обмеженим ризиком.</p>"
    body += "".join(f"<p class='note'>{_e(x)}</p>" for x in warns)
    return body + _section_notes(snap, "events")


def block_funding(snap: dict) -> str:
    groups = []
    for base, name in (("BTC", "funding_btc"), ("ETH", "funding_eth")):
        d = _data(snap, name)
        venues = [v for v in _list((d or {}).get("venues")) if isinstance(v, dict) and _num(v.get("annualized")) is not None]
        if venues:
            groups.append((base, venues, d))
    if not groups:
        return _empty("Funding у знімку немає.") + _section_notes(snap, "funding_btc", "funding_eth")
    mx = max(abs(_num(v["annualized"])) for _, vs, _ in groups for v in vs) or 1.0
    out = []
    for base, venues, d in groups:
        out.append(f"<h3 style='font-size:14px;margin:6px 0 4px'>{base}</h3><div class='hbars'>")
        for v in venues:
            a = _num(v["annualized"])
            label = VENUE_NAMES.get(str(v.get("venue")), _clean(v.get("venue"), 20))
            out.append(_hbar(_e(label), abs(a) / mx, _e(_pc(a * 100, 1)), neg=a < 0))
        out.append("</div>")
        ws = d.get("widest_spread")
        if isinstance(ws, dict) and _num(ws.get("annualized_spread")) is not None:
            lv = VENUE_NAMES.get(str(ws.get("long_venue")), _clean(ws.get("long_venue"), 20))
            sv = VENUE_NAMES.get(str(ws.get("short_venue")), _clean(ws.get("short_venue"), 20))
            gap = _nf(_num(ws["annualized_spread"]) * 100, 1)
            txt = f"Найбільша різниця: лонг {lv}, шорт {sv} — {gap} п. п. річних до комісій, базису й маржі."
            out.append(f"<p class='note'>{_e(txt)}</p>")
    out.append("<p class='note'>Річних, у відсотках: додатний funding — лонги платять шортам.</p>")
    return "".join(out) + _section_notes(snap, "funding_btc", "funding_eth")


def _guard(fn, snap: dict) -> str:
    try:
        return fn(snap)
    except Exception as e:  # noqa: BLE001 - one odd section must not take the page down
        return _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")


def _snapshot_problem(err: str | None, path: Path | None) -> str:
    """Why there is no snapshot to show (HTML, escaped)."""
    where = f" (<code>{_e(path)}</code>)" if path else ""
    return {
        "missing": f"Знімка асистента ще немає{where}: завдання колектора <code>assistant_snapshot</code> вимкнене або ще не відпрацювало.",
        "corrupt": f"Файл знімка пошкоджений або має несподіваний формат{where}. Наступний прогін колектора (кожні 10 хв) його перепише.",
    }.get(err or "", f"Знімок не читається{where}: {_e(err)}.")


def _snapshot_stamp(snap: dict, now: float) -> str:
    """The snapshot's time and age (a warning when older than 30 min) plus its schema / missing / carried notes."""
    asof = parse_utc(snap.get("asof_utc"))
    age = now - asof.timestamp() if asof else None
    if age is None:
        stamp = "<span class='bad'>Час знімка невідомий: дані можуть бути застарілими.</span>"
    else:
        cls = "bad" if age > SNAPSHOT_OLD_S else "warn" if age > SNAPSHOT_STALE_S else "mut"
        stamp = f"<span class='{cls}'>Знімок {_e(kyiv(asof))} за Києвом · {_e(fmt_age(age))}"
        if age > SNAPSHOT_STALE_S:
            stamp += " · старший за 30 хв: завдання колектора, схоже, зупинилось, дані можуть бути неактуальні"
        stamp += "</span>"
    extra = []
    if snap.get("schema") != SNAPSHOT_SCHEMA:
        extra.append(f"схема знімка {_clean(snap.get('schema'), 10)}, панель знає {SNAPSHOT_SCHEMA}: частина блоків може бути порожня")
    miss = [SECTION_UA.get(str(n), _clean(n, 30)) for n in _list(snap.get("missing"))]
    stale = [SECTION_UA.get(str(n), _clean(n, 30)) for n in _list(snap.get("stale"))]
    if miss:
        extra.append("без даних: " + ", ".join(miss))
    if stale:
        extra.append("з попереднього знімка: " + ", ".join(stale))
    return stamp + "".join(f"<span class='warn'>{_e(x)}</span>" for x in extra)


def render_brief(snap: dict | None, err: str | None, now: float, path: Path | None = None) -> str:
    """The «Бриф» tab. Never raises: a missing, stale or corrupt snapshot gives an explained empty state."""
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>tradedesk — бриф</title><style>{CSS}{BRIEF_CSS}</style></head><body>"
    )
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in BRIEF_CAVEATS) + "</ul></div>"
    try:
        if snap is None:
            msg = _snapshot_problem(err, path)
            return head + f"<header><h1>Бриф</h1><span class='bad'>{msg}</span></header>{_nav('brief')}{caveats}</body></html>"
        out = [head, f"<header><h1>Бриф</h1>{_snapshot_stamp(snap, now)}"]
        out.append(f"</header>{_nav('brief')}{caveats}<main class='brief'>")
        # the expiry label only next to its data: `expiries` can name an expiry whose section was dropped as too old
        month = _exp_label(snap, "month") if _data(snap, "chain") else "—"
        scen = f"Сценарії на {month}" if month != "—" else "Сценарії на місячну експірацію"
        cards = (
            ("wide", "", "", block_tiles),
            ("", "Головне сьогодні", "key", block_key),
            ("", "Купувати чи продавати опціони зараз", "conditions", block_conditions),
            ("", "BTC за 120 днів і що закладають опціони", "chart", block_chart),
            ("", f"{scen} · ймовірності з цін опціонів", "scenarios", block_scenarios),
            ("", "Волатильність", "iv", block_vol),
            ("", "Події · час Київ", "events", block_events),
            ("", "Funding · річних", "funding", block_funding),
        )
        for cls, title, key, fn in cards:
            h2 = _h2(title, key) if title else ""
            out.append(f"<section class='{cls}'>{h2}{_guard(fn, snap)}</section>")
        out.append("</main></body></html>")
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header><h1>Бриф</h1><span class='bad'>Знімок не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('brief')}{caveats}</body></html>"
        )


def brief_page(now: float | None = None) -> str:
    snap, err = load_snapshot(SNAPSHOT_PATH)
    return with_help(render_brief(snap, err, time.time() if now is None else now, SNAPSHOT_PATH))


# ---------------------------------------------------------------- «Опціони» tab: strategy builder on the snapshot chain
#
# All numbers are computed here (stdlib copies of analytics.strategy: payoff, payoff_extremes, breakevens,
# prob_of_profit; tests compare the two). The page is a plain GET form: the inline script only keeps deposit, risk and
# size in localStorage and resubmits the form on a change, so there is one implementation of every formula.

OPT_STRATEGIES = {
    "short_strangle": "Продаж стренглу",
    "short_straddle": "Продаж стреддлу",
    "iron_condor": "Залізний кондор",
    "long_straddle": "Купівля стреддлу",
    "long_strangle": "Купівля стренглу",
}
OPT_DEFAULT_STRAT = "long_strangle"
# expiry choices: query value -> (snapshot section, label). «near» = the nearest expiry at least 25 h away
# (analytics.assistant.pick_near_expiry), where rule 8's 1-3 day zone shows up; the month chain stays the default
OPT_EXPIRIES = {"near": ("chain_near", "найближча"), "month": ("chain", "місячна")}
OPT_DEFAULT_EXP = "month"
# (default, min, max): deposit USD, risk per trade %, size BTC (the owner trades 0.1, card «Депозит і ризик»)
OPT_SIZE = {"dep": (10000.0, 100.0, 1e8), "riskp": (1.0, 0.1, 100.0), "qty": (0.1, 0.01, 100.0)}
OPT_STRIKES = ("kput", "kcall", "kputw", "kcallw")
OPT_THESIS_MAX = 200  # characters of the builder's thesis field (rule 8 (a))
OPT_RANGE = 0.3  # chart: forward ±30 %
OPT_CAVEATS = (
    "Ймовірність прибутку — ризик-нейтральна (лог-нормальна за ATM IV експірації): це ціна страхування в ринку, а не прогноз.",
    "Премії — марки Deribit на час знімка, без спреду й комісій: реальна ціна входу гірша. P&L — лише на експірацію.",
    "Премія волатильності (VRP) на марках опціонів ще перевіряється (картка vrp-real-marks): «дорога» чи «дешева» волатильність — не доведена перевага.",
    "Не інвестиційна порада: сторінка лише рахує, нічого не торгує.",
)
OPT_CSS = """
.seg{display:flex;flex-wrap:wrap;gap:6px}
.seg button{font:600 13px -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;padding:6px 12px;cursor:pointer}
.seg button[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:var(--card)}
.ctl{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:end;margin-top:10px}
.ctl label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}
select,input[type=number],input[type=text]{font:14px Menlo,Consolas,monospace;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;min-width:0;max-width:100%}
select:disabled{opacity:.6}.ctl button.go{font-weight:600;border:0;border-radius:999px;padding:7px 14px;background:var(--acc);color:var(--card);cursor:pointer}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.fit{font-size:14px;line-height:1.6}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}td.r,th.r{text-align:right}tr.cur{background:rgba(var(--acc-rgb),.12)}
table.greeks td.mut{font-size:12px}table.scen th,table.scen td{white-space:nowrap}
input[type=range]{accent-color:var(--acc);width:min(220px,100%)}
"""
OPT_JS = """
(function () {
  var f = document.getElementById('opt-form'); if (!f) return;
  var KEY = 'td-size', keys = ['dep', 'riskp', 'qty'], m = {};
  try { m = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { m = {}; }
  var q = new URLSearchParams(location.search), changed = false;
  if (!keys.some(function (k) { return q.has(k); })) {
    keys.forEach(function (k) { var v = +m[k]; if (v > 0 && f.elements[k] && +f.elements[k].value !== v) { f.elements[k].value = v; changed = true; } });
  }
  if (changed) { f.submit(); return; }
  var save = function () { var o = {}; keys.forEach(function (k) { o[k] = +f.elements[k].value; }); try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) {} };
  f.addEventListener('submit', save);
  f.addEventListener('change', function () { save(); f.submit(); });
  var h = f.elements['hzn'], hv = document.getElementById('hznv');
  if (h && hv) h.addEventListener('input', function () { hv.textContent = h.value + ' дн'; });
})();
"""


def option_chain(snap: dict | None, which: str = OPT_DEFAULT_EXP) -> dict | None:
    """The chain of one builder expiry (OPT_EXPIRIES key; the month by default): forward, days, ATM IV, expiry and rows
    {k, c/p: {mark, iv, delta}} (legs with a positive mark only), plus ``which``. None when the snapshot has no usable
    chain for it."""
    which = which if which in OPT_EXPIRIES else OPT_DEFAULT_EXP
    chain = _data(snap, OPT_EXPIRIES[which][0]) if isinstance(snap, dict) else None
    if not chain:
        return None
    F, days = _num(chain.get("forward")), _num(chain.get("days"))
    if F is None or F <= 0 or days is None or days <= 0:
        return None
    by: dict[float, dict] = {}
    for r in _list(chain.get("strikes")):
        if not isinstance(r, dict):
            continue
        k = _num(r.get("strike"))
        if k is None or k <= 0:
            continue
        row = {"k": k}
        for side, key in (("call", "c"), ("put", "p")):
            leg = r.get(side)
            if not isinstance(leg, dict):
                continue
            mark = _num(leg.get("mark_usd"))
            if mark is not None and mark > 0:
                row[key] = {"mark": mark, "iv": _num(leg.get("iv")), "delta": _num(leg.get("delta"))}
        if "c" in row or "p" in row:
            by[k] = row
    rows = [by[k] for k in sorted(by)]
    both = [r for r in rows if "c" in r and "p" in r]
    if not both:
        return None
    atm = min(both, key=lambda r: abs(r["k"] - F))
    iv = _num(chain.get("atm_iv"))
    if iv is None or iv <= 0:
        ivs = [x for x in (atm["c"]["iv"], atm["p"]["iv"]) if x is not None and x > 0]
        iv = sum(ivs) / len(ivs) if ivs else None
    if iv is None:
        return None
    return {
        "F": F,
        "days": days,
        "T": days / 365.0,
        "iv": iv,
        "expiry": chain.get("expiry"),
        "rows": rows,
        "atm": atm["k"],
        "which": which,
    }


def _nearest_delta(rows: list[dict], side: str, target: float) -> float | None:
    cand = [r for r in rows if side in r and r[side]["delta"] is not None]
    return min(cand, key=lambda r: abs(abs(r[side]["delta"]) - target))["k"] if cand else None


def default_strikes(ch: dict, strat: str) -> dict[str, float | None]:
    """As in the mock-up: straddles at the strike nearest the forward, strangles at 25Δ, condor wings at 10Δ."""
    rows = ch["rows"]
    if "straddle" in strat:
        kput = kcall = ch["atm"]
    else:
        kput, kcall = _nearest_delta(rows, "p", 0.25), _nearest_delta(rows, "c", 0.25)
    return {"kput": kput, "kcall": kcall, "kputw": _nearest_delta(rows, "p", 0.10), "kcallw": _nearest_delta(rows, "c", 0.10)}


def option_legs(ch: dict, strat: str, strikes: dict, qty: float) -> tuple[list[dict], str | None]:
    """Legs in the analytics.strategy format (kind, side, size, strike, entry_premium = mark, USD per BTC), or an error."""
    by = {r["k"]: r for r in ch["rows"]}
    kput, kcall, kputw, kcallw = (strikes.get(k) for k in OPT_STRIKES)
    if "straddle" in strat:
        kput = kcall = ch["atm"]
    plan = {
        "short_strangle": [("call", kcall, "short"), ("put", kput, "short")],
        "short_straddle": [("call", kcall, "short"), ("put", kput, "short")],
        "long_straddle": [("call", kcall, "long"), ("put", kput, "long")],
        "long_strangle": [("call", kcall, "long"), ("put", kput, "long")],
        "iron_condor": [("call", kcall, "short"), ("put", kput, "short"), ("call", kcallw, "long"), ("put", kputw, "long")],
    }[strat]
    if kput is not None and kcall is not None and kput > kcall:
        return [], "Страйк пута має бути не вище страйку кола."
    if strat == "iron_condor" and not (None not in (kputw, kput, kcall, kcallw) and kputw < kput < kcall < kcallw):
        return [], "Кондор: захисний пут < пут < кол < захисний кол."
    legs = []
    for kind, k, side in plan:
        leg = by.get(k, {}).get("c" if kind == "call" else "p") if k is not None else None
        if not leg:
            return [], "На цьому страйку в знімку немає марки опціону."
        legs.append({"kind": kind, "side": side, "size": qty, "strike": k, "entry_premium": leg["mark"], "iv": leg["iv"]})
    return legs, None


def payoff(legs: list[dict], s: float) -> float:
    """Terminal P&L in USD at price s (analytics.strategy.payoff_curve for option legs)."""
    total = 0.0
    for leg in legs:
        sign = 1 if leg["side"] == "long" else -1
        intrinsic = max(s - leg["strike"], 0.0) if leg["kind"] == "call" else max(leg["strike"] - s, 0.0)
        total += sign * leg["size"] * (intrinsic - leg["entry_premium"])
    return total


def _tail_slope(legs: list[dict]) -> float:
    return sum((1 if leg["side"] == "long" else -1) * leg["size"] for leg in legs if leg["kind"] == "call")


def payoff_extremes(legs: list[dict]) -> dict[str, float | None]:
    """Exact max profit / max loss over S in [0, inf); None = unbounded (copy of analytics.strategy.payoff_extremes)."""
    kinks = [0.0] + sorted({leg["strike"] for leg in legs})
    values = [payoff(legs, s) for s in kinks]
    slope = _tail_slope(legs)
    return {"max_profit": None if slope > 1e-12 else max(values), "max_loss": None if slope < -1e-12 else min(values)}


def payoff_breakevens(legs: list[dict]) -> list[float]:
    """Exact prices where the terminal P&L turns from loss to profit or back (profit = strictly > 0)."""
    kinks = [0.0] + sorted({leg["strike"] for leg in legs})
    values = [payoff(legs, s) for s in kinks]
    out = []
    for i in range(1, len(kinks)):
        v0, v1 = values[i - 1], values[i]
        if (v0 > 0) != (v1 > 0):
            out.append(kinks[i - 1] + (0.0 - v0) * (kinks[i] - kinks[i - 1]) / (v1 - v0))
    slope, last = _tail_slope(legs), values[-1]
    if (last > 0) != (slope > 0 or (slope == 0 and last > 0)):
        out.append(kinks[-1] - last / slope)
    return out


def _lognormal_cdf(F: float, T: float, iv: float, x: float) -> float:
    """Risk-neutral P(S_T <= x), log-normal around the forward (r = 0), as analytics.strategy._lognormal_cdf."""
    if x <= 0:
        return 0.0
    v = iv * math.sqrt(T)
    return _ncdf((math.log(x / F) + 0.5 * v * v) / v)


def prob_of_profit(legs: list[dict], F: float, T: float, iv: float) -> float:
    """Risk-neutral probability that the terminal P&L is > 0, summed over the profit intervals between breakevens."""
    bounds = [0.0] + payoff_breakevens(legs) + [math.inf]
    p = 0.0
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        if hi <= lo:
            continue
        mid = max(2 * lo, lo + F) if math.isinf(hi) else (lo + hi) / 2
        if payoff(legs, mid) > 0:
            p += (1.0 if math.isinf(hi) else _lognormal_cdf(F, T, iv, hi)) - _lognormal_cdf(F, T, iv, lo)
    return min(1.0, max(0.0, p))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _b76(F: float, K: float, T: float, iv: float, call: bool) -> float:
    """Black-76 price, r = 0 (copy of analytics.options.black76_price)."""
    if T <= 0 or iv <= 0:
        return max(F - K, 0.0) if call else max(K - F, 0.0)
    v = iv * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    return F * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - F * _ncdf(-d1)


def _b76_greeks(F: float, K: float, T: float, iv: float, call: bool) -> dict[str, float]:
    """Delta (BTC), gamma (BTC per USD), vega (USD per vol point), theta (USD per day): analytics.options.black76_greeks."""
    if T <= 0 or iv <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    st = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * iv * iv * T) / (iv * st)
    pdf = _npdf(d1)
    return {
        "delta": _ncdf(d1) if call else _ncdf(d1) - 1.0,
        "gamma": pdf / (F * iv * st),
        "vega": F * pdf * st / 100.0,
        "theta": -F * pdf * iv / (2.0 * st) / 365.0,
    }


def _leg_iv(leg: dict, iv_default: float) -> float:
    iv = leg.get("iv")
    return iv if isinstance(iv, (int, float)) and iv > 0 else iv_default


def position_greeks(legs: list[dict], F: float, T: float, iv_default: float) -> dict[str, float]:
    """Sum of sign × size × Black-76 greeks at the forward, each leg at its own IV from the chain (ATM IV if absent):
    analytics.strategy.aggregate_greeks applied leg by leg."""
    tot = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    for leg in legs:
        sign = 1 if leg["side"] == "long" else -1
        g = _b76_greeks(F, leg["strike"], T, _leg_iv(leg, iv_default), leg["kind"] == "call")
        for k in tot:
            tot[k] += sign * leg["size"] * g[k]
    return tot


def value_change(legs: list[dict], s: float, days_left: float, F: float, T: float, iv_default: float, d_iv: float = 0.0) -> float:
    """USD change of the position's Black-76 value from now (forward F, T years) to forward s with days_left to expiry
    and every leg's IV shifted by d_iv: analytics.strategy.mtm_value(scenario) − mtm_value(now), leg by leg. Anchored at
    the model value, not the mark, so «today, no move» is exactly 0 (Deribit's USD marks sit ~0.5 % below the model)."""
    total = 0.0
    for leg in legs:
        sign, call, iv = (1 if leg["side"] == "long" else -1), leg["kind"] == "call", _leg_iv(leg, iv_default)
        now = _b76(F, leg["strike"], T, iv, call)
        later = _b76(s, leg["strike"], max(days_left, 0.0) / 365.0, max(iv + d_iv, 1e-6), call)
        total += sign * leg["size"] * (later - now)
    return total


OPT_MOVES = (-15, -10, -5, 0, 5, 10, 15)  # scenario columns: forward move, %
OPT_HZN_DEFAULT = 10.0  # the «through N days» line, days from now (as in the mock-up)


def scenario_rows(legs: list[dict], ch: dict, exp_s: str) -> list[dict]:
    """The scenario table: today, today with IV +5 points, through 10 and 20 days (if before expiry), at expiry.
    Before expiry: value change from now (value_change); at expiry: the exact payoff net of the marks (payoff)."""
    F, T, days, iv = ch["F"], ch["T"], ch["days"], ch["iv"]
    plan = [("сьогодні", 0.0, 0.0), ("сьогодні, IV +5 п.", 0.0, 0.05)]
    plan += [(f"через {d:.0f} дн", d, 0.0) for d in (10.0, 20.0) if d < days]
    out = []
    for label, passed, d_iv in plan:
        out.append({"label": label, "pnl": [value_change(legs, F * (1 + m / 100), days - passed, F, T, iv, d_iv) for m in OPT_MOVES]})
    out.append({"label": f"на експірацію {exp_s}", "pnl": [payoff(legs, F * (1 + m / 100)) for m in OPT_MOVES]})
    return out


def risk_fit(max_loss: float | None, qty: float, dep: float, riskp: float) -> dict:
    """Does the position's max loss fit the risk budget (deposit × risk %)? Also the largest size that fits, in 0.01 BTC."""
    budget = dep * riskp / 100.0
    if max_loss is None:
        return {"budget": budget, "unbounded": True}
    loss = max(0.0, -max_loss)
    per_btc = loss / qty if qty > 0 else 0.0
    qmax = math.floor(budget / per_btc * 100 + 1e-9) / 100 if per_btc > 0 else None
    return {
        "budget": budget,
        "unbounded": False,
        "loss": loss,
        "fits": loss <= budget * (1 + 1e-9),
        "ratio": loss / budget if budget > 0 else None,
        "pct_dep": loss / dep * 100 if dep > 0 else None,
        "qmax": qmax,
    }


def parse_options_query(query: str) -> dict:
    """Form parameters from the URL query (untrusted): unknown or out-of-range values fall back to the defaults."""
    try:
        q = urllib.parse.parse_qs(query or "", max_num_fields=40)
    except ValueError:
        q = {}

    def one(name: str) -> str | None:
        v = q.get(name)
        return v[0][:40] if v else None

    def fnum(name: str, lo: float, hi: float) -> float | None:
        try:
            v = float(one(name))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and lo <= v <= hi else None

    s, cur = one("s"), one("cur")
    s = s if s in OPT_STRATEGIES else None
    cur = cur if cur in OPT_STRATEGIES else None
    out: dict = {"strat": s or cur or OPT_DEFAULT_STRAT, "keep_strikes": s is None or s == cur}
    e, cexp = one("exp"), one("cexp")
    out["exp"] = e if e in OPT_EXPIRIES else OPT_DEFAULT_EXP
    # another expiry's strikes are not kept: switching the expiry resets them, like switching the strategy
    out["keep_strikes"] = out["keep_strikes"] and (cexp is None or cexp == out["exp"])
    for name, (default, lo, hi) in OPT_SIZE.items():
        v = fnum(name, lo, hi)
        out[name] = default if v is None else v
    for name in OPT_STRIKES:
        out[name] = fnum(name, 0.0, 1e9)
    hzn = fnum("hzn", 0.0, 3650.0)
    out["hzn"] = OPT_HZN_DEFAULT if hzn is None else math.floor(hzn)
    # rule 8 (a) in the 1-3 day zone: the owner's thesis (free text, shown escaped) and target price
    th = q.get("thesis")
    out["thesis"] = _clean(th[0], OPT_THESIS_MAX) if th else ""
    out["target"] = fnum("target", 0.0, 1e9) or None
    return out


def horizon_days(hzn: float, days: float) -> int:
    """The «through N days» slider value, clamped to [0, last whole day before expiry]."""
    top = max(0, math.ceil(days) - 1)
    return int(min(max(hzn, 0.0), top))


def _usd(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    sign = "−" if x < -0.5 else ("+" if signed and x > 0.5 else "")
    return f"{sign}${_nf(abs(x))}"


def _kd(x: float) -> str:
    return "$" + _k(x)


def payoff_svg(legs: list[dict], F: float, T: float, iv: float, bes: list[float], hzn: int | None = None) -> str:
    """P&L at expiry over forward ±30 %: profit/loss areas, the ±1σ band, the forward and the breakevens; with hzn,
    also the value change through hzn days at unchanged IVs (dashed)."""
    W, H, L, R, Tp, B = 720, 300, 70, 14, 12, 28
    lo, hi, n = F * (1 - OPT_RANGE), F * (1 + OPT_RANGE), 241
    xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    ys = [payoff(legs, x) for x in xs]
    yh = [value_change(legs, x, T * 365.0 - hzn, F, T, iv) for x in xs] if hzn is not None else []
    ymin, ymax = min(ys + yh + [0.0]), max(ys + yh + [0.0])
    pad = (ymax - ymin) * 0.08 or 1.0
    ymin, ymax = ymin - pad, ymax + pad

    def X(x: float) -> float:
        return L + (x - lo) / (hi - lo) * (W - L - R)

    def Y(y: float) -> float:
        return Tp + (ymax - y) / (ymax - ymin) * (H - Tp - B)

    y0 = Y(0.0)
    g = [
        f'<defs><clipPath id="cpU"><rect x="0" y="0" width="{W}" height="{y0:.1f}"/></clipPath>'
        f'<clipPath id="cpD"><rect x="0" y="{y0:.1f}" width="{W}" height="{H}"/></clipPath></defs>'
    ]
    v = iv * math.sqrt(T)
    s_lo, s_hi = max(lo, F * math.exp(-v)), min(hi, F * math.exp(v))
    g.append(
        f'<rect x="{X(s_lo):.1f}" y="{Tp}" width="{X(s_hi) - X(s_lo):.1f}" height="{H - Tp - B}" style="fill:var(--acc);fill-opacity:.08"/>'
    )
    sy = nice_step(ymax - ymin, 6)
    y = math.ceil(ymin / sy) * sy
    while y <= ymax:
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(y):.1f}" y2="{Y(y):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(y) + 4:.1f}" text-anchor="end">{_e(_usd(y))}</text>'
        )
        y += sy
    sx = nice_step(hi - lo, 8)
    x = math.ceil(lo / sx) * sx
    while x <= hi:
        g.append(f'<text x="{X(x):.1f}" y="{H - 8}" text-anchor="middle">{_e(_kd(x))}</text>')
        x += sx
    path = "".join(f"{'L' if i else 'M'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(zip(xs, ys, strict=True)))
    area = path + f"L{X(hi):.1f},{y0:.1f}L{X(lo):.1f},{y0:.1f}Z"
    g.append(f'<path d="{area}" clip-path="url(#cpU)" style="fill:var(--ok);fill-opacity:.16"/>')
    g.append(f'<path d="{area}" clip-path="url(#cpD)" style="fill:var(--bad);fill-opacity:.16"/>')
    g.append(f'<line x1="{L}" x2="{W - R}" y1="{y0:.1f}" y2="{y0:.1f}" style="stroke:var(--mut)"/>')
    g.append(
        f'<line x1="{X(F):.1f}" x2="{X(F):.1f}" y1="{Tp}" y2="{H - B}" style="stroke:var(--mut);stroke-dasharray:2 3"/>'
        f'<text x="{X(F) + 4:.1f}" y="{Tp + 10}">зараз {_e(_kd(F))}</text>'
    )
    if yh:
        ph = "".join(f"{'L' if i else 'M'}{X(x):.1f},{Y(y):.1f}" for i, (x, y) in enumerate(zip(xs, yh, strict=True)))
        g.append(f'<path d="{ph}" style="fill:none;stroke:var(--warn);stroke-width:2;stroke-dasharray:6 4"/>')
    g.append(f'<path d="{path}" style="fill:none;stroke:var(--acc);stroke-width:2.4"/>')
    for b in bes:
        if lo <= b <= hi:
            g.append(
                f'<circle cx="{X(b):.1f}" cy="{y0:.1f}" r="4" style="fill:var(--card);stroke:var(--fg);stroke-width:1.5"/>'
                f'<text x="{X(b):.1f}" y="{y0 + 18:.1f}" text-anchor="middle" style="fill:var(--fg)">{_e(_kd(b))}</text>'
            )
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="P&amp;L позиції на експірацію, USD">{"".join(g)}</svg>'


def _strike_select(ch: dict, name: str, side: str, sel: float | None, label: str, disabled: bool) -> str:
    opts = []
    for r in ch["rows"]:
        leg = r.get(side)
        if not leg:
            continue
        d = "—" if leg["delta"] is None else f"{leg['delta']:.2f}".replace("-", "−")
        ivs = "—" if leg["iv"] is None else _nf(leg["iv"] * 100, 1)
        opts.append(
            f"<option value='{r['k']:.10g}'{' selected' if r['k'] == sel else ''}>{_e(_nf(r['k']))} · Δ {_e(d)} · IV {_e(ivs)}</option>"
        )
    dis = " disabled" if disabled else ""
    return f"<label for='{name}'>{label}<select id='{name}' name='{name}'{dis}>{''.join(opts)}</select></label>"


def _exp_short(ch: dict) -> str:
    exp = expiry_utc(ch.get("expiry"))
    return kyiv(exp, "%d.%m") if exp else _clean(ch.get("expiry") or "—", 12)


def _expiry_select(snap: dict, cur: str) -> str:
    """The expiry choice: only the expiries whose chain the snapshot has (label, date, days at the snapshot)."""
    opts = []
    for key, (_, label) in OPT_EXPIRIES.items():
        c = option_chain(snap, key)
        if c is None:
            continue
        text = f"{label} · {_exp_short(c)} · {_nf(c['days'], 1)} дн"
        opts.append(f"<option value='{key}'{' selected' if key == cur else ''}>{_e(text)}</option>")
    return f"<label for='exp'>Експірація<select id='exp' name='exp'>{''.join(opts)}</select></label>"


def builder_now_ts(ch: dict, now: float | None) -> float:
    """«Now» of the builder's rule checks: the page time, else the snapshot's (expiry minus the chain's days)."""
    if now is not None:
        return now
    exp = expiry_utc(ch.get("expiry"))
    return exp.timestamp() - ch["days"] * 86400.0 if exp else 0.0


def builder_zone(ch: dict, now: float | None) -> str | None:
    """Rule 8's zone (copy of analytics.rules_check.buy_zone) for a buy now on this chain's expiry; None if unknown."""
    exp = expiry_utc(ch.get("expiry"))
    return rules_buy_zone((exp.timestamp() - builder_now_ts(ch, now)) / 3600.0) if exp else None


def _num_input(name: str, label: str, value: float, step: str) -> str:
    _, lo, hi = OPT_SIZE[name]
    return (
        f"<label for='{name}'>{label}<input type='number' id='{name}' name='{name}' min='{lo:g}' max='{hi:g}' step='{step}' "
        f"value='{value:.10g}'></label>"
    )


def position_pros_cons(
    legs: list[dict],
    F: float,
    T: float,
    iv: float,
    credit: float,
    bes: list[float],
    G: dict[str, float],
    ext: dict,
    fit: dict,
    snap: dict,
) -> list[tuple[str, str]]:
    """For and against this position: risk fit, the move it needs vs the market's 1σ, time decay, and the market's
    lean for its side (market_lean). Tones good | bad | warn | ''. Our numbers only."""
    buyer = credit < 0
    items: list[tuple[str, str]] = []
    if fit["unbounded"]:
        items.append(("bad", "Збиток необмежений: без стопу або захисних опціонів правило ризику на угоду не виконується"))
    elif fit["fits"]:
        items.append(("good", f"Макс. збиток {_usd(fit['loss'])} вкладається в ризик на угоду {_usd(fit['budget'])}"))
    elif fit["qmax"] is not None and fit["qmax"] >= 0.01:
        items.append(
            ("bad", f"Макс. збиток {_usd(fit['loss'])} більший за ризик {_usd(fit['budget'])}: під нього — до {_nf(fit['qmax'], 2)} BTC")
        )
    else:
        items.append(("bad", f"Макс. збиток {_usd(fit['loss'])} більший за ризик {_usd(fit['budget'])} навіть на 0.01 BTC"))
    sigma = F * iv * math.sqrt(T) if T > 0 and iv > 0 else 0.0
    if bes and sigma > 0:
        d = min(abs(b - F) for b in bes)
        ratio, dp, sp = d / sigma, d / F * 100, sigma / F * 100
        if buyer and payoff(legs, F) > 0:
            items.append(("good", "На поточній ціні позиція вже в плюсі на експірацію"))
        elif buyer:
            tone = "good" if ratio < 0.7 else "bad" if ratio > 1.2 else ""
            items.append(
                (tone, f"Для прибутку BTC має пройти щонайменше {dp:.1f} % до беззбитку; ринок закладає ±{sp:.1f} % (1σ) до експірації")
            )
        else:
            tone = "good" if ratio > 1.2 else "bad" if ratio < 0.7 else ""
            items.append((tone, f"У мінус позиція піде, якщо BTC пройде понад {dp:.1f} %; ринок закладає ±{sp:.1f} % (1σ) до експірації"))
    t = G["theta"]
    if buyer and credit < 0 and t < 0:
        share = -t / -credit * 100
        tone = "bad" if share > 4 else "warn" if share > 2 else ""
        items.append((tone, f"Час забирає ≈ {_usd(-t)} на день — {share:.1f} % сплаченої премії щодня"))
    elif not buyer and t > 0:
        items.append(("good", f"Час працює на тебе: ≈ +{_usd(t)} на день, якщо ціна й IV не зміняться"))
    buy, sell = market_lean(snap) if isinstance(snap, dict) else ([], [])
    items.extend(buy if buyer else sell)
    return items


def block_builder(ch: dict, snap: dict, p: dict, now: float | None = None) -> str:
    strat = p["strat"]
    strikes = default_strikes(ch, strat)
    if p["keep_strikes"]:
        for name, side in (("kput", "p"), ("kcall", "c"), ("kputw", "p"), ("kcallw", "c")):
            k = p.get(name)
            if k is not None and any(r["k"] == k and side in r for r in ch["rows"]):
                strikes[name] = k
    if "straddle" in strat:
        strikes["kput"] = strikes["kcall"] = ch["atm"]
    qty, dep, riskp = p["qty"], p["dep"], p["riskp"]
    which = ch.get("which", OPT_DEFAULT_EXP)
    exp_s = _exp_short(ch)
    title = f"Конструктор · BTC, експірація {exp_s} · {_nf(ch['days'], 1)} дн · форвард {_nf(ch['F'])}"
    out = [
        f"<section class='wide'>{_h2(title, 'builder')}",
        "<form id='opt-form' method='get' action='/options'>",
        f"<input type='hidden' name='cur' value='{_e(strat)}'>",
        f"<input type='hidden' name='cexp' value='{_e(which)}'>",
        "<div class='seg' role='group' aria-label='Стратегія'>",
    ]
    for key, label in OPT_STRATEGIES.items():
        out.append(f"<button type='submit' name='s' value='{key}' aria-pressed={'true' if key == strat else 'false'}>{_e(label)}</button>")
    out.append("</div><div class='ctl'>")
    out.append(_expiry_select(snap, which))
    strad, condor = "straddle" in strat, strat == "iron_condor"
    out.append(_strike_select(ch, "kput", "p", strikes["kput"], "Пут, страйк", strad))
    out.append(_strike_select(ch, "kcall", "c", strikes["kcall"], "Кол, страйк", strad))
    if condor:
        out.append(_strike_select(ch, "kputw", "p", strikes["kputw"], "Захисний пут", False))
        out.append(_strike_select(ch, "kcallw", "c", strikes["kcallw"], "Захисний кол", False))
    out.append("</div><div class='ctl'>")
    out.append(_num_input("dep", "Депозит, $", dep, "100"))
    out.append(_num_input("riskp", "Ризик на угоду, %", riskp, "0.1"))
    out.append(_num_input("qty", "Розмір, BTC", qty, "0.01"))
    hzn = horizon_days(p.get("hzn", OPT_HZN_DEFAULT), ch["days"])
    out.append(
        f"<label for='hzn'>Лінія «через N днів»: <span id='hznv' class='num'>{hzn} дн</span>"
        f"<input type='range' id='hzn' name='hzn' min='0' max='{horizon_days(1e9, ch['days'])}' step='1' value='{hzn}'></label>"
    )
    cond = builder_zone(ch, now) == RULES_ZONE_COND
    if cond:  # rule 8 (a): the thesis and the target only matter for a buy 24-72 h before the delivery
        tgt = p.get("target")
        out.append(
            "</div><div class='ctl'>"
            f"<label for='thesis'>Теза: напрям і причина руху (подія чи рівень)<input type='text' id='thesis' name='thesis' "
            f"maxlength='{OPT_THESIS_MAX}' size='40' value='{_e(p.get('thesis') or '')}'></label>"
            "<label for='target'>Ціль, $<input type='number' id='target' name='target' min='0' step='any' "
            f"value='{'' if tgt is None else f'{tgt:.10g}'}'></label>"
        )
    out.append("<button class='go' type='submit'>Порахувати</button></div></form>")
    if which != p.get("exp", which):
        want = OPT_EXPIRIES.get(p.get("exp"), ("", "обрана"))[1]
        out.append(
            f"<p class='note warn'>Ланцюжка експірації «{_e(want)}» у знімку немає (старий знімок чи збій колектора): "
            f"показано «{_e(OPT_EXPIRIES[which][1])}».</p>"
        )
    if cond:
        out.append(
            "<p class='note'>До експірації 1–3 доби: за правилом 8 купівля — лише з тезою й ціллю (поля вище), "
            "беззбитком у межах 1σ руху за RV30 і IV ≤ 1.2 × RV30 (рядки 8а–8в у блоці правил нижче).</p>"
        )
    out.append(
        "<p class='note'>Страйки — з ланцюжка знімка (Δ і IV Deribit); стреддл — на страйку, найближчому до форварду. "
        "Експірація: місячна (≥ 25 днів) або найближча, до якої щонайменше 25 год (там трапляється зона правила 8 "
        "«1–3 доби»). Депозит, ризик і розмір запам'ятовуються в цьому браузері. Мінімальний лот опціонів BTC: "
        "Deribit — 0.1 BTC; OKX і Bybit — 0.01 BTC (уточнимо перед першою угодою).</p></section>"
    )

    legs, err = option_legs(ch, strat, strikes, qty)
    if err:
        out.append(f"<section class='wide'>{_h2('Позиція', 'position')}{_empty(err)}</section>")
        return "".join(out)
    F, T, iv = ch["F"], ch["T"], ch["iv"]
    credit = -sum((1 if lg["side"] == "long" else -1) * lg["size"] * lg["entry_premium"] for lg in legs)
    ext = payoff_extremes(legs)
    bes = payoff_breakevens(legs)
    pop = prob_of_profit(legs, F, T, iv)
    shown_bes = sorted({round(b, 2) for b in bes})

    out.append(f"<section class='wide'>{_h2('Прибуток і збиток на експірацію, USD', 'payoff')}")
    out.append(payoff_svg(legs, F, T, iv, shown_bes, hzn))
    out.append(
        "<div class='legend'><span><i style='background:var(--acc)'></i>на експірацію</span>"
        f"<span><i style='background:var(--warn)'></i>через {hzn} дн (ті самі IV)</span>"
        "<span><i style='background:var(--mut)'></i>поточний форвард</span>"
        f"<span><i style='background:var(--acc);opacity:.25;height:8px'></i>±1σ до експірації (ATM IV {_e(_nf(iv * 100, 1))})</span></div></section>"
    )

    tiles = [
        _tile(
            "Отримуєш премію" if credit >= 0 else "Платиш премію",
            _e(_usd(abs(credit))),
            _e(f"за марками Deribit, {_nf(qty, 2)} BTC"),
            help="premium",
        ),
        _tile("Беззбиток", _e(" / ".join(_kd(b) for b in shown_bes) or "—"), "на експірацію", help="breakeven"),
        _tile(
            "Макс. прибуток",
            _e("необмежений" if ext["max_profit"] is None else _usd(ext["max_profit"])),
            "на експірацію",
            help="maxprofit",
        ),
    ]
    if ext["max_loss"] is None:
        down, up = payoff(legs, F * (1 - OPT_RANGE)), payoff(legs, F * (1 + OPT_RANGE))
        tiles.append(
            _tile(
                "Макс. збиток",
                "необмежений",
                _e(f"на −30 %: {_usd(down)}, на +30 %: {_usd(up)}"),
                "bad",
                help="maxloss",
                tone="bad",
                badge="без захисту",
            )
        )
    else:
        fits = risk_fit(ext["max_loss"], qty, dep, riskp)["fits"]
        tiles.append(
            _tile(
                "Макс. збиток",
                _e(_usd(ext["max_loss"])),
                "на експірацію",
                help="maxloss",
                tone="good" if fits else "bad",
                badge="в межах ризику" if fits else "більше за твій ризик",
            )
        )
    tiles.append(_tile("Ймовірність прибутку", _e(f"{_nf(pop * 100)} %"), "ризик-нейтральна, за ATM IV", help="pop"))
    out.append(f"<section>{_h2('Позиція', 'position')}<div class='tiles'>{''.join(tiles)}</div></section>")
    G = position_greeks(legs, F, T, iv)
    out.append(
        f"<section>{_h2('За і проти цієї угоди', 'proscons')}"
        + pros_cons_html(position_pros_cons(legs, F, T, iv, credit, shown_bes, G, ext, risk_fit(ext["max_loss"], qty, dep, riskp), snap))
        + "</section>"
    )
    out.append(block_greeks(G))

    fit = risk_fit(ext["max_loss"], qty, dep, riskp)
    out.append(f"<section>{_h2('Чи вкладається в ризик', 'riskfit')}<div class='fit'>")
    out.append(f"<p>Ризик на угоду: <b class='num'>{_e(_usd(fit['budget']))}</b> ({_e(_nf(riskp, 1))} % від депозиту {_e(_usd(dep))}).</p>")
    if fit["unbounded"]:
        out.append(
            "<p><span class='chip risk'>ризик необмежений</span> Продаж без захисних опціонів не має межі збитку, тож "
            "правило «ризик на угоду» він не виконує: потрібен стоп або захисні опціони (залізний кондор).</p>"
        )
    else:
        chip = (
            "<span class='chip opp'>в межах ризику</span>"
            if fit["fits"]
            else f"<span class='chip risk'>більше за ризик у {_e(_nf(fit['ratio'], 1))} раза</span>"
        )
        out.append(
            f"<p>Макс. збиток <b class='num'>{_e(_usd(fit['loss']))}</b> за {_e(_nf(qty, 2))} BTC "
            f"({_e(_nf(fit['pct_dep'], 1))} % депозиту) {chip}</p>"
        )
        qmax = fit["qmax"]
        if qmax is None:
            out.append("<p class='mut'>Збитку на експірацію немає за жодної ціни.</p>")
        elif qmax >= 0.01:
            lot = " (лише OKX / Bybit: на Deribit мінімум 0.1 BTC)" if qmax < 0.1 else ""
            out.append(f"<p class='mut'>Під твій ризик — до <b class='num'>{_e(_nf(qmax, 2))} BTC</b>{_e(lot)}.</p>")
        else:
            per = fit["loss"] / qty * 0.01
            out.append(f"<p class='mut'>Навіть 0.01 BTC — це {_e(_usd(per))} збитку: більше за ризик.</p>")
    out.append("</div></section>")
    rv30 = _num(((_data(snap, "vrp") or {}).get("current") or {}).get("rv_30d")) if isinstance(snap, dict) else None
    out.append(block_rules_pre(legs, ch, dep, now, rv30, p.get("thesis"), p.get("target")))
    pct365 = _num(((_data(snap, "vrp") or {}).get("current") or {}).get("dvol_percentile_365d")) if isinstance(snap, dict) else None
    out.append(
        f"<section>{_h2('Що це означає', 'verdict')}"
        + verdict_html(legs, F, credit, shown_bes, pop, G, ext, fit, exp_s, qty, pct365)
        + "</section>"
    )
    out.append(block_vol_cost(snap, ch, exp_s))
    out.append(block_opt_scenarios(scenario_rows(legs, ch, exp_s), F))
    out.append(
        "<section class='wide'><p class='note'>Ціни — марки Deribit, USD за 1 BTC контракту, помножені на розмір; P&amp;L на експірацію "
        "= внутрішня вартість мінус премія. Беззбиток і максимуми — точні (ламана з вузлами на страйках). Ймовірність прибутку — "
        "ризик-нейтральна, лог-нормальна з ATM IV на форварді експірації (r = 0, T = дні/365), без нахилу усмішки. "
        "Греки, лінія «через N днів» і сценарії до експірації — Black-76 на форварді (r = 0), кожна нога за своєю IV з ланцюжка; "
        "вега — на 1 пункт волатильності, тета — за день, USD. Комісії й спред не враховано.</p></section>"
    )
    return "".join(out)


def _sg(x: float, d: int) -> str:
    return f"{x:+.{d}f}".replace("-", "−")


def _cls(x: float) -> str:
    return "ok" if x > 1e-12 else "bad" if x < -1e-12 else ""


def block_greeks(G: dict[str, float]) -> str:
    """Position greeks with a plain one-line meaning each (all numbers ours; nothing untrusted)."""
    d, g, v, t = G["delta"], G["gamma"], G["vega"], G["theta"]
    side = "лонг" if d > 0 else "шорт"
    gm = 0.5 * g * 1000.0 * 1000.0  # second-order P&L of a $1 000 move, USD
    rows = [
        (
            "Дельта",
            f"{_sg(d, 3)} BTC",
            d,
            f"BTC +$1 000 → {_usd(d * 1000, True)}, −$1 000 → {_usd(-d * 1000, True)}: як {abs(d):.3f} BTC {side} на споті",
        ),
        (
            "Гамма",
            f"{_sg(g * 1000, 4)} BTC",
            g,
            f"так змінюється дельта на кожні +$1 000 BTC; рух на $1 000 у будь-який бік {'додає' if g >= 0 else 'забирає'} ще ≈ {_usd(abs(gm))}",
        ),
        ("Вега", _usd(v, True), v, "якщо волатильність (IV) зросте на 1 пункт; на −1 пункт — те саме з протилежним знаком"),
        ("Тета", _usd(t, True), t, "за один день, якщо ціна й IV не зміняться"),
    ]
    body = "".join(
        f"<tr><td>{_e(k)}</td><td class='r num {_cls(x)}' style='white-space:nowrap'>{_e(val)}</td><td class='mut'>{_e(s)}</td></tr>"
        for k, val, x, s in rows
    )
    return (
        f"<section>{_h2('Греки позиції', 'greeks')}<table class='greeks'>{body}</table>"
        "<p class='note'>Black-76 на форварді, кожна нога за своєю IV; діють лише на малі рухи й поки решта не змінилась.</p></section>"
    )


def _vol_level(p: float) -> str:
    if p < 0.2:
        return "низький: опціони дешевші, ніж зазвичай за рік"
    if p < 0.4:
        return "нижче середнього за рік"
    if p < 0.6:
        return "середній за рік"
    if p < 0.8:
        return "вище середнього за рік"
    return "високий: опціони дорожчі, ніж зазвичай за рік"


def verdict_html(
    legs: list[dict],
    F: float,
    credit: float,
    bes: list[float],
    pop: float,
    G: dict[str, float],
    ext: dict,
    fit: dict,
    exp_s: str,
    qty: float,
    pct365: float | None,
) -> str:
    """The plain-language conclusion. Our numbers only; the expiry label is escaped."""

    def b(s: str) -> str:
        return f"<b class='num'>{_e(s)}</b>"

    q, exp = _e(_nf(qty, 2)), _e(exp_s)
    if credit < 0:
        parts = [f"Ти купуєш волатильність за {b(_usd(-credit))} на {q} BTC."]
    else:
        parts = [f"Ти продаєш волатильність і отримуєш {b(_usd(credit))} премії на {q} BTC."]
    inside = payoff(legs, F) > 0
    if len(bes) == 2:
        lo, hi = b(_kd(bes[0])), b(_kd(bes[1]))
        where = f"між {lo} і {hi}" if inside else f"нижче {lo} або вище {hi}"
        parts.append(
            f"Заробляєш, якщо на експірацію {exp} BTC буде {where}; за цінами ринку (ризик-нейтрально) шанс ≈ {b(_nf(pop * 100) + ' %')}."
        )
    else:
        parts.append(f"Ризик-нейтральний шанс прибутку на експірацію {exp} ≈ {b(_nf(pop * 100) + ' %')}.")
    t, v = G["theta"], G["vega"]
    if t < 0:
        parts.append(f"Щодня час забирає ≈ {b(_usd(-t))}; +5 пунктів IV дали б ≈ {b(_usd(5 * v, True))}, −5 пунктів — стільки ж мінусом.")
    else:
        parts.append(f"Щодня час приносить ≈ {b(_usd(t))}, але +5 пунктів IV — це ≈ {b(_usd(5 * v, True))} паперового збитку.")
    if ext["max_loss"] is None:
        parts.append("Ризик необмежений: потрібен стоп або захисні опціони (залізний кондор).")
    elif credit < 0:
        parts.append(f"Найбільший збиток — сплачена премія {b(_usd(-ext['max_loss']))}.")
    else:
        parts.append(f"Збиток обмежений захисними опціонами: щонайбільше {b(_usd(-ext['max_loss']))}.")
    if not fit["unbounded"]:
        if fit["fits"]:
            parts.append(f"У твій ризик на угоду ({_e(_usd(fit['budget']))}) вкладається.")
        elif fit["qmax"] is not None and fit["qmax"] >= 0.01:
            parts.append(f"Це більше за твій ризик на угоду ({_e(_usd(fit['budget']))}): під нього — до {b(_nf(fit['qmax'], 2) + ' BTC')}.")
        else:
            parts.append(f"Це більше за твій ризик на угоду ({_e(_usd(fit['budget']))}) навіть на 0.01 BTC.")
    if pct365 is not None:
        parts.append(
            f"DVOL зараз на {b(_nf(pct365 * 100) + '-му')} перцентилі за рік ({_e(_vol_level(pct365))}). Це рівень ціни, а не сигнал: "
            "перевагу купівлі чи продажу волатильності не доведено (див. «Чи дорога зараз волатильність»)."
        )
    return f"<p class='fit'>{' '.join(parts)}</p>"


def block_vol_cost(snap: dict, ch: dict, exp_s: str) -> str:
    """«Is vol expensive»: ATM IV vs realized, DVOL's 365-day percentile, the VRP backtest by DVOL level, honest caveats."""
    vrp = _data(snap, "vrp") or {}
    cur = vrp.get("current") if isinstance(vrp.get("current"), dict) else {}
    tiles = []
    rv30, spr = _num(cur.get("rv_30d")), _num(cur.get("spread_vs_rv30"))
    sub = f"реалізована 30 дн {_nf(rv30 * 100, 1)}" if rv30 is not None else "реалізованої в знімку немає"
    tiles.append(_tile(f"ATM IV {_e(exp_s)}", _e(_nf(ch["iv"] * 100, 1)), _e(sub), help="iv"))
    p365, dvol = _num(cur.get("dvol_percentile_365d")), _num(cur.get("dvol"))
    if p365 is not None:
        tone = vol_lean(p365, None)[0][0]
        tiles.append(
            _tile(
                "DVOL за рік",
                _e(f"{_nf(p365 * 100)}-й"),
                _e(f"перцентиль · DVOL {_nf(dvol * 100, 1) if dvol is not None else '—'}"),
                help="dvol",
                tone=tone,
                badge=lean_badge(tone),
            )
        )
    if spr is not None:
        tone = vol_lean(None, spr)[0][0]
        tiles.append(
            _tile(
                "DVOL − RV30",
                _e(_pc(spr * 100, 1).replace(" %", "")),
                "пунктів волатильності",
                help="vrp",
                tone=tone,
                badge=lean_badge(tone),
            )
        )
    out = [f"<section>{_h2('Чи дорога зараз волатильність', 'volcost')}", f"<div class='tiles'>{''.join(tiles)}</div>"]
    if p365 is not None:
        out.append(f"<p class='fit'>Рівень DVOL — {_e(_vol_level(p365))}.</p>")
    cells = [x for x in _list(vrp.get("by_dvol_level")) if isinstance(x, dict) and _num(x.get("mean_straddle_pnl")) is not None]
    if cells:
        out.append(
            "<div class='scroll'><table style='margin-top:8px'><tr><th>DVOL, перцентиль за рік</th><th class='r'>Продаж стреддлу 30 дн, середнє</th>"
            "<th class='r'>У плюсі</th><th class='r'>стартів (перекривні)</th></tr>"
        )
        for x in cells:
            now_ = x.get("current") is True
            m = _num(x["mean_straddle_pnl"])
            share = _num(x.get("share_positive"))
            out.append(
                f"<tr{' class=cur' if now_ else ''}><td>{_e(_clean(x.get('dvol_percentile_365d'), 16))}{' · зараз' if now_ else ''}</td>"
                f"<td class='r num {_cls(m)}'>{_e(_pc(m * 100, 2))}</td>"
                f"<td class='r num'>{_e(_nf(share * 100) + ' %' if share is not None else '—')}</td>"
                f"<td class='r num'>{_e(_nf(x.get('n_starts')))}</td></tr>"
            )
        out.append("</table></div>")
    out.append(
        "<p class='note'>Перцентиль показує, де DVOL зараз відносно останніх 365 днів: «дешево» не означає «вигідно купувати». "
        "Таблиця — бектест VRP на індексі DVOL, не на марках опціонів (перевірка на марках триває, картка vrp-real-marks): продаж "
        "30-денного стреддлу ATM із щоденним дельта-хеджем, % спота на угоду, перекривні старти (незалежних угод ≈ n/30, без CI, дослідницький результат: не читати як «продавай, коли вола дорога»). "
        "Метод рекомендацій асистента (вибір угоди за режимом волатильності) перевірено 18.10 і відхилено (H1): −0.59 % спота за угоду, "
        "95 % CI [−2.43; +1.51], n = 27, переваги не показано, тож будь-яка рекомендація тут — лише гіпотеза. "
        "Ймовірності на сторінці — ризик-нейтральні: ціна страхування в ринку, а не прогноз.</p>"
    )
    warns = [_clean(x, 1200) for x in _list(_section(snap, "vrp").get("warnings")) if isinstance(x, str)]
    if warns and vrp:
        out.append("<details><summary>Бектест VRP: результат і межі</summary>" + "".join(f"<p>{_e(x)}</p>" for x in warns) + "</details>")
    out.append(_section_notes(snap, "vrp") + "</section>")
    return "".join(out)


def block_opt_scenarios(rows: list[dict], F: float) -> str:
    head = "".join(
        f"<th class='r num'>{_e(('+' if m > 0 else '−' if m < 0 else '') + f'{abs(m)} %')}<br><span class='mut'>{_e(_kd(F * (1 + m / 100)))}</span></th>"
        for m in OPT_MOVES
    )
    body = "".join(
        f"<tr><td style='white-space:nowrap'>{_e(r['label'])}</td>"
        + "".join(f"<td class='r num {_cls(round(x))}'>{_e(_usd(x, True))}</td>" for x in r["pnl"])
        + "</tr>"
        for r in rows
    )
    return (
        f"<section class='wide'>{_h2('Сценарії · P&L позиції, USD', 'optscen')}"
        f"<div class='scroll'><table class='scen'><tr><th>BTC</th>{head}</tr>{body}</table></div>"
        "<p class='note'>Рух — форварду експірації (≈ спота). До експірації — зміна вартості позиції від поточної за Black-76, "
        "кожна нога за своєю IV; «IV +5 п.» — усі ноги на 5 пунктів одразу. На експірацію — точна виплата мінус премія за марками. "
        "Комісії й спред не враховано.</p></section>"
    )


def render_options(snap: dict | None, err: str | None, now: float, params: dict | None = None, path: Path | None = None) -> str:
    """The «Опціони» tab. Never raises: no snapshot or no chain gives an explained empty state."""
    p = params or parse_options_query("")
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>tradedesk — опціони</title><style>{CSS}{BRIEF_CSS}{OPT_CSS}</style></head><body>"
    )
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in OPT_CAVEATS) + "</ul></div>"
    tail = f"<script>{OPT_JS}</script></body></html>"
    try:
        if snap is None:
            msg = _snapshot_problem(err, path)
            return head + f"<header><h1>Опціони</h1><span class='bad'>{msg}</span></header>{_nav('options')}{caveats}</body></html>"
        out = [head, f"<header><h1>Опціони</h1>{_snapshot_stamp(snap, now)}</header>{_nav('options')}{caveats}<main class='brief'>"]
        want = p.get("exp", OPT_DEFAULT_EXP)
        # the chosen expiry's section first, then the month one (tried when the chosen chain is missing)
        secs = list(dict.fromkeys((OPT_EXPIRIES.get(want, OPT_EXPIRIES[OPT_DEFAULT_EXP])[0], OPT_EXPIRIES[OPT_DEFAULT_EXP][0])))
        ch = option_chain(snap, want)
        if ch is None and want != OPT_DEFAULT_EXP:  # e.g. a snapshot written before the nearer chain: the month one
            ch = option_chain(snap, OPT_DEFAULT_EXP)
        if ch is None:
            out.append(
                f"<section class='wide'>{_h2('Конструктор', 'builder')}"
                + _empty("Ланцюжка опціонів у знімку немає: конструктор порахувати нема з чого.")
                + _section_notes(snap, *secs)
                + "</section></main></body></html>"
            )
            return "".join(out)
        try:
            out.append(block_builder(ch, snap, p, now))
        except Exception as e:  # noqa: BLE001 - one odd chain must not take the page down
            out.append(
                f"<section class='wide'>{_empty(f'Конструктор не вдалося показати: несподіваний формат даних ({type(e).__name__}).')}</section>"
            )
        notes = _section_notes(snap, *dict.fromkeys((OPT_EXPIRIES[ch["which"]][0], secs[0])))  # the shown and the chosen chain
        if notes:
            out.append(f"<section class='wide'>{notes}</section>")
        out.append("</main>" + tail)
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header><h1>Опціони</h1><span class='bad'>Знімок не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('options')}{caveats}</body></html>"
        )


def options_page(query: str = "", now: float | None = None) -> str:
    snap, err = load_snapshot(SNAPSHOT_PATH)
    return with_help(render_options(snap, err, time.time() if now is None else now, parse_options_query(query), SNAPSHOT_PATH))


# ---------------------------------------------------------------- «Радар» tab: the snapshot's radar section (BTC, ETH)
#
# Renders `analytics.radar.radar_view` of one asset from the snapshot's `radar` section; nothing is recomputed here
# (the changes are the file's own). «A week ago» has two anchors: the surface (price, ATM IV, skew, smile, forward vol)
# compares archive rows `surface.now_day` / `surface.week_day` (19:00 UTC), IV Rank compares daily DVOL closes
# `iv_rank.now.day` / `iv_rank.week.day` (1-3 days apart from the surface dates); each block names its own dates.
# A null field shows the reason from the asset's `missing`. Every string from the file is untrusted and escaped.

RADAR_ASSETS = ("BTC", "ETH")
RADAR_TENORS = (("7", "7 дн"), ("30", "30 дн"), ("90", "90 дн"), ("180", "180 дн"))
RADAR_SMILE = (("10P", "10Δ P"), ("25P", "25Δ P"), ("35P", "35Δ P"), ("ATM", "ATM"), ("35C", "35Δ C"), ("25C", "25Δ C"), ("10C", "10Δ C"))
RADAR_FWD_DAYS = 100  # forward-vol table horizon, days to the window's end (analytics.radar.HOT_HORIZON_DAYS)
RADAR_FWD_MIN_DAYS = 5  # shorter windows are noisy and never «hot» (analytics.radar.HOT_MIN_DAYS)
# short chart labels of the forward-vol event kinds (analytics.radar.FWD_EVENT_KINDS); other kinds show their title
RADAR_EVN = {"fomc": "FOMC", "cpi": "CPI", "core_pce": "PCE", "pce": "PCE", "nfp": "NFP", "ppi": "PPI", "gdp": "ВВП", "election": "Вибори"}
RADAR_MISS_UA = {
    "surface": "поверхня зараз",
    "surface_week": "поверхня тиждень тому",
    "iv_rank": "IV Rank зараз",
    "iv_rank_week": "IV Rank тиждень тому",
    "rv30": "реалізована 30 дн",
    "rv7": "реалізована 7 дн",
    "forward_vol": "форвардна вола",
    "year_price": "ціна за рік",
    "year_dvol": "DVOL за рік",
    "year_skew": "скью за рік",
}
RADAR_YEAR_MAX_DAYS = 400  # a yearly series longer than this is cut (analytics.radar.YEAR_DAYS = 365)
RADAR_YEAR_FULL_DAYS = 360  # fewer days with data: the chart notes «N з 365 днів»
RADAR_HIST_MIN_DAYS = 20  # skew history: fewer archive days draw no chart (as the mock-up)
RADAR_HIST_MEDIAN = 7  # skew history: rolling median over this many days, at least 3 with data (as the mock-up)
RADAR_CAVEATS = (
    "Поверхня — марки Deribit з угод за 6 год до 19:00 UTC, один рядок архіву на день: «зараз» — останній день архіву, "
    "а не живий ринок. IV Rank рахується за денним DVOL, тож його дати можуть відрізнятися від дат поверхні на 1–3 дні.",
    "Скью й усмішка — за форвардними дельтами Black-76, не за дельтами Deribit: рівні можуть відрізнятися від інших "
    "джерел, порівнюй зміни за тиждень.",
    "IV Rank і перцентиль — денний DVOL за 365 днів (30-денний індекс, а не ATM кожного строку). «Низький IV Rank — "
    "купувати опціони» як перевагу наші дані не підтвердили (H1 відхилено 18.10).",
    "Форвардна вола підписана подіями лише за датою (час у календарі орієнтовний): високе значення на тижні не доводить, "
    "що ринок закладає саме цю подію.",
    "Не інвестиційна порада: сторінка лише показує дані й нічого не торгує.",
)
RADAR_CSS = """
.rhead{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:0 12px 12px;font-size:13px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg a{padding:4px 16px;color:var(--mut);text-decoration:none;font-weight:600}
.seg a[aria-current=page]{background:var(--acc);color:var(--card)}
td.r,th.r{text-align:right}.scroll{overflow-x:auto}tr.hot td{background:rgba(var(--warn-rgb),.13)}
"""


def parse_radar_query(query: str) -> dict:
    q = urllib.parse.parse_qs(query)
    a = (q.get("asset") or [""])[0].strip().upper()
    return {"asset": a if a in RADAR_ASSETS else RADAR_ASSETS[0]}


def _rd(x: object) -> dict:
    return x if isinstance(x, dict) else {}


def _vp(x: object, d: int = 1) -> str:
    """A decimal vol as percent: 0.371 -> '37.1 %'."""
    v = _num(x)
    return "—" if v is None else f"{_nf(v * 100, d)} %"


def _dvp(x: object, d: int = 1) -> str:
    """A decimal vol difference in signed vol points: 0.015 -> '+1.5 вп'."""
    v = _num(x)
    return "—" if v is None else f"{v * 100:+.{d}f} вп".replace("-", "−")


def _lvp(x: object, d: int = 1) -> str:
    """A decimal vol difference as a level in vol points (skew, RR): 0.0265 -> '2.6 вп'."""
    v = _num(x)
    return "—" if v is None else f"{_nf(v * 100, d)} вп"


def _dd(day: object) -> str:
    """'2026-10-18' (or an ISO UTC stamp) -> '18.10'; '—' for anything else."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(day or ""))
    return f"{m.group(3)}.{m.group(2)}" if m else "—"


def _radar_missing(a: dict, *fields: str) -> str:
    """The asset's `missing` reasons for these fields (escaped notes)."""
    out = []
    for m in _list(a.get("missing")):
        if isinstance(m, dict) and isinstance(m.get("field"), str) and m["field"] in fields:
            out.append(f"немає даних «{RADAR_MISS_UA.get(m['field'], m['field'])}»: {_clean(m.get('reason'))}")
    return "".join(f"<p class='note warn'>{_e(x)}</p>" for x in out)


def _radar_source_notes(sec: dict, asset: str) -> str:
    """The section's provider failures that concern this asset (fields `radar:<what>:<asset>`) or both (no asset)."""
    out = []
    for m in _list(sec.get("missing")):
        if not isinstance(m, dict):
            continue
        field = str(m.get("field") or "")
        tail = field.rsplit(":", 1)[-1]
        if tail in RADAR_ASSETS and tail != asset:
            continue
        out.append(f"збій джерела {_clean(field, 40)}: {_clean(m.get('detail') or m.get('reason'))}")
    return "".join(f"<p class='note warn'>{_e(x)}</p>" for x in out)


def radar_lines_svg(cats: list[str], now: list[float | None], week: list[float | None], aria: str) -> str:
    """Two lines over categories (decimal vols): «тиждень тому» dashed, «зараз» with value labels; gaps for None."""
    vals = [v for v in (*now, *week) if v is not None]
    if not vals:
        return ""
    W, H, L, R, Tp, B = 520, 200, 40, 16, 18, 26
    lo, hi = min(vals), max(vals)
    pad = max((hi - lo) * 0.25, 0.005)
    lo, hi = lo - pad, hi + pad
    n = len(cats)

    def X(i: int) -> float:
        return L + (i / (n - 1) if n > 1 else 0.5) * (W - L - R)

    def Y(v: float) -> float:
        return Tp + (hi - v) / (hi - lo) * (H - Tp - B)

    g = []
    st_ = nice_step((hi - lo) * 100, 4) / 100
    for i in range(int(math.ceil(lo / st_)), int(math.floor(hi / st_)) + 1):
        v = i * st_
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{_e(_nf(v * 100, 1 if st_ < 0.01 else 0))}</text>'
        )
    g += [f'<text x="{X(i):.1f}" y="{H - 6}" text-anchor="middle">{_e(c)}</text>' for i, c in enumerate(cats)]
    for vs, style, labels in ((week, "stroke:var(--mut);stroke-dasharray:5 4", False), (now, "stroke:var(--acc)", True)):
        d, pen = "", False
        for i, v in enumerate(vs):
            if v is None:
                pen = False
                continue
            d += f"{'L' if pen else 'M'}{X(i):.1f},{Y(v):.1f}"
            pen = True
        if d:
            g.append(f'<path d="{d}" style="fill:none;stroke-width:2;{style}"/>')
        col = "var(--acc)" if labels else "var(--mut)"
        for i, v in enumerate(vs):
            if v is None:
                continue
            g.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.5" style="fill:{col}"/>')
            if labels:
                g.append(
                    f'<text x="{X(i):.1f}" y="{Y(v) - 8:.1f}" text-anchor="middle" style="fill:var(--acc);font-weight:600">{_e(_nf(v * 100, 1))}</text>'
                )
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(aria)}">{"".join(g)}</svg>'


def rank_gauge_svg(now: float | None, week: float | None) -> str:
    """A half-dial 0..100 % (green to 20, amber to 60, red above, as in the mock-up): solid needle now, dashed a week ago."""
    W, H, cx, cy, r, sw = 300, 186, 150.0, 156.0, 114.0, 21

    def P(p: float) -> tuple[float, float]:
        p = min(max(p, 0.0), 1.0)
        return cx - r * math.cos(math.pi * p), cy - r * math.sin(math.pi * p)

    def arc(p0: float, p1: float, col: str) -> str:
        (x0, y0), (x1, y1) = P(p0), P(p1)
        return f'<path d="M{x0:.1f},{y0:.1f} A{r:.0f},{r:.0f} 0 0 1 {x1:.1f},{y1:.1f}" style="fill:none;stroke:{col};stroke-width:{sw}"/>'

    g = [arc(0, 0.2, "var(--ok)"), arc(0.2, 0.6, "var(--warn)"), arc(0.6, 1, "var(--bad)")]
    if week is not None:
        x, y = P(week)
        g.append(
            f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{x:.1f}" y2="{y:.1f}" style="stroke:var(--mut);stroke-width:2;stroke-dasharray:3 3"/>'
        )
    if now is not None:
        x, y = P(now)
        g.append(
            f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{x:.1f}" y2="{y:.1f}" style="stroke:var(--fg);stroke-width:3.5;stroke-linecap:round"/>'
        )
    g.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="6" style="fill:var(--fg)"/>')
    g.append(
        f'<text x="{cx - r:.0f}" y="{cy + 20:.0f}" text-anchor="middle">0</text><text x="{cx + r:.0f}" y="{cy + 20:.0f}" text-anchor="middle">100</text>'
    )
    label = "—" if now is None else f"{_nf(now * 100, 1)} %"
    g.append(
        f'<text x="{cx:.0f}" y="{cy - r * 0.4:.0f}" text-anchor="middle" style="font:700 28px Menlo,Consolas,monospace;fill:var(--fg)">{_e(label)}</text>'
    )
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="IV Rank: зараз і тиждень тому" style="max-width:340px;margin:0 auto">{"".join(g)}</svg>'


def _surface_days(a: dict) -> tuple[str, str]:
    s = _rd(a.get("surface"))
    return _dd(s.get("now_day")), _dd(s.get("week_day"))


def _two_lines_legend(nd: str, wd: str) -> str:
    return (
        f"<div class='legend'><span><i style='background:var(--acc)'></i>зараз · {_e(nd)}</span>"
        f"<span><i style='background:var(--mut)'></i>тиждень тому · {_e(wd)}</span></div>"
    )


def _year_grid(obj: object, *cols: str) -> tuple[datetime, list[list[float | None]]] | None:
    """(first day, columns) of a yearly series (`analytics.radar.daily_grid`: day i = start + i days); None if malformed."""
    g = _rd(obj)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(g.get("start") or ""))
    if not m:
        return None
    try:
        start = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)  # noqa: UP017
    except ValueError:
        return None
    out = []
    for c in cols:
        vals = g.get(c)
        if not isinstance(vals, list):
            return None
        out.append([_num(v) for v in vals[:RADAR_YEAR_MAX_DAYS]])
    return start, out


def _day_label(start: datetime, i: int) -> str:
    return (start + timedelta(days=i)).strftime("%d.%m.%y")


def year_svg(
    start: datetime,
    lines: list[tuple[list[float | None], str]],
    aria: str,
    ylab,
    *,
    pad: float = 0.08,
    zero: bool = False,
    hlines: tuple = (),
    dots: tuple = (),
) -> str:
    """Lines over a daily grid (index i = start + i days) with month labels, as the mock-up's year charts: gaps for None,
    ``hlines`` = ((value, label), ...) dashed reference lines, ``dots`` = ((index, value, colour), ...). '' if too little."""
    n = max((len(v) for v, _ in lines), default=0)
    vals = [v for vs, _ in lines for v in vs if v is not None]
    if n < 2 or len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    span = hi - lo or abs(hi) * 0.1 or 1.0
    lo, hi = lo - span * pad, hi + span * pad
    W, H, L, R, Tp, B = 520, 190, 48, 12, 12, 24

    def X(i: float) -> float:
        return L + i / (n - 1) * (W - L - R)

    def Y(v: float) -> float:
        return Tp + (hi - v) / (hi - lo) * (H - Tp - B)

    g = []
    st_ = nice_step(hi - lo, 4)
    for k in range(int(math.ceil(lo / st_)), int(math.floor(hi / st_)) + 1):
        v = k * st_
        axis = zero and abs(v) < st_ / 1000
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var({"--mut" if axis else "--line"})"/>'
            f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{_e(ylab(v, st_))}</text>'
        )
    firsts = [i for i in range(n) if (start + timedelta(days=i)).day == 1]
    for i in firsts[:: 2 if len(firsts) > 6 else 1]:
        g.append(f'<text x="{X(i):.1f}" y="{H - 6}" text-anchor="middle">{MONTHS_UA[(start + timedelta(days=i)).month - 1]}</text>')
    for v, lab in hlines:
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var(--mut);stroke-dasharray:4 3"/>'
            f'<text x="{W - R - 2}" y="{Y(v) - 3:.1f}" text-anchor="end" style="fill:var(--mut)">{_e(lab)}</text>'
        )
    for vs, style in lines:
        d, pen = "", False
        for i, v in enumerate(vs):
            if v is None:
                pen = False
                continue
            d += f"{'L' if pen else 'M'}{X(i):.1f},{Y(v):.1f}"
            pen = True
        if d:
            g.append(f'<path d="{d}" style="fill:none;stroke-width:1.5;{style}"/>')
    for i, v, col in dots:
        g.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3.5" style="fill:{col}"/>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(aria)}">{"".join(g)}</svg>'


def _vol_tick(v: float, st_: float) -> str:
    return _nf(v * 100, 0 if round(st_ * 100, 6) % 1 == 0 else 1)


def _last_and_week(vals: list[float | None]) -> tuple[int | None, int | None]:
    """Index of the newest value and of the value 7 days before it (None when that day is empty)."""
    last = next((i for i in range(len(vals) - 1, -1, -1) if vals[i] is not None), None)
    week = last - 7 if last is not None and last >= 7 and vals[last - 7] is not None else None
    return last, week


def block_r_year_price(a: dict, asset: str) -> str:
    """Price over the year (the snapshot's `year.price`: Binance daily closes), as the mock-up's block 1 chart."""
    miss = _radar_missing(a, "year_price")
    g = _year_grid(_rd(a.get("year")).get("price"), "values")
    if g is None:
        return miss
    start, (vals,) = g
    vals = [v if v is not None and v > 0 else None for v in vals]
    last, week = _last_and_week(vals)
    dots = ((week, vals[week], "var(--mut)"),) if week is not None else ()
    if last is not None:
        dots += ((last, vals[last], "var(--fg)"),)
    svg = year_svg(start, [(vals, "stroke:var(--fg)")], f"{asset}: ціна за рік", lambda v, _: _k(v), pad=0.06, dots=dots)
    if not svg:
        return miss
    n = sum(1 for v in vals if v is not None)
    short = f", {n} з 365 днів" if n < RADAR_YEAR_FULL_DAYS else ""
    note = (
        f"<p class='note'>Ціна за рік: денні закриття Binance {_e(asset)}/USDT за днями UTC, {_e(_day_label(start, 0))}–"
        f"{_e(_day_label(start, last or 0))}{short}. Остання точка — поточна ціна дня, тож вона відрізняється від індексу "
        "Deribit о 19:00 UTC у плитках; сіра точка — тиждень тому.</p>"
    )
    return svg + note + miss


def block_r_price(a: dict, asset: str = "") -> str:
    p, (nd, wd) = _rd(a.get("price")), _surface_days(a)
    now, week, ch = _num(p.get("now")), _num(p.get("week")), _num(p.get("change"))
    miss = _radar_missing(a, "surface", "surface_week")
    chart = block_r_year_price(a, asset)
    if now is None and week is None:
        return _empty("Ціни з архіву поверхні немає.") + miss + chart
    cls = "" if ch is None else "ok" if ch >= 0 else "bad"
    tiles = (
        _tile("Зараз", f"<span class='num'>{_e(_nf(now))}</span>", f"{_e(nd)}, 19:00 UTC, індекс Deribit", help="r_spot")
        + _tile("Тиждень тому", f"<span class='num mut'>{_e(_nf(week))}</span>", f"{_e(wd)}, 19:00 UTC", help="r_week")
        + _tile(
            "Зміна",
            f"<span class='num {cls}'>{_e(_pc(ch * 100) if ch is not None else '—')}</span>",
            f"{_e(nd)} проти {_e(wd)}",
            help="r_change",
        )
    )
    return f"<div class='tiles'>{tiles}</div>" + miss + chart


def block_r_term(a: dict) -> str:
    at, (nd, wd) = _rd(a.get("atm")), _surface_days(a)
    n, w, c = _rd(at.get("now")), _rd(at.get("week")), _rd(at.get("change"))
    miss = _radar_missing(a, "surface", "surface_week")
    if not n and not w:
        return _empty("ATM IV з архіву поверхні немає.") + miss
    nv = [_num(n.get(k)) for k, _ in RADAR_TENORS]
    wv = [_num(w.get(k)) for k, _ in RADAR_TENORS]
    svg = radar_lines_svg([lab for _, lab in RADAR_TENORS], nv, wv, "ATM IV за строками: зараз і тиждень тому")
    rows = []
    for (k, lab), x, y in zip(RADAR_TENORS, nv, wv, strict=True):
        d = _num(c.get(k))
        cls = " warn" if d is not None and d > 0.002 else ""
        rows.append(
            f"<tr><td>{lab}</td><td class='r num'>{_e(_vp(x))}</td><td class='r num mut'>{_e(_vp(y))}</td><td class='r num{cls}'>{_e(_dvp(d))}</td></tr>"
        )
    table = (
        f"<div class='scroll'><table><tr><th>Строк</th><th class='r'>Зараз · {_e(nd)}</th><th class='r'>Тиждень тому · {_e(wd)}</th>"
        f"<th class='r'>Зміна</th></tr>{''.join(rows)}</table></div>"
    )
    note = "<p class='note'>ATM IV на постійний строк: інтерполяція сумарної дисперсії між сусідніми експіраціями; без екстраполяції, тому крайній строк може бути порожнім.</p>"
    return svg + _two_lines_legend(nd, wd) + table + note + miss


def block_r_year_dvol(a: dict) -> str:
    """DVOL over the year (the snapshot's `year.dvol`) with the series' min / max and the IV Rank 20 % / 60 % levels."""
    miss = _radar_missing(a, "year_dvol")
    g = _year_grid(_rd(a.get("year")).get("dvol"), "values")
    if g is None:
        return miss
    start, (vals,) = g
    vals = [v if v is not None and v > 0 else None for v in vals]
    pts = [v for v in vals if v is not None]
    last, week = _last_and_week(vals)
    if last is None or len(pts) < 2:
        return miss
    lo, hi = min(pts), max(pts)
    hl = ((lo, f"мін {_vp(lo)}"), (hi, f"макс {_vp(hi)}"))
    if hi > lo:
        hl += ((lo + 0.2 * (hi - lo), "Rank 20 %"), (lo + 0.6 * (hi - lo), "Rank 60 %"))
    dots = ((week, vals[week], "var(--mut)"),) if week is not None else ()
    dots += ((last, vals[last], "var(--acc)"),)
    svg = year_svg(start, [(vals, "stroke:var(--acc)")], "DVOL за рік", _vol_tick, hlines=hl, dots=dots)
    n = len(pts)
    short = f", {n} з 365 днів" if n < RADAR_YEAR_FULL_DAYS else ""
    note = (
        f"<p class='note'>DVOL за рік, {_e(_day_label(start, 0))}–{_e(_day_label(start, last))}{_e(short)}: зараз "
        f"{_e(_vp(vals[last]))}, діапазон {_e(_vp(lo))} … {_e(_vp(hi))}. Пунктир — межі ряду і рівні IV Rank 20 % і 60 % "
        "(межі зон спідометра); сіра точка — тиждень тому.</p>"
    )
    return svg + note + miss


def block_r_rank(a: dict) -> str:
    r = _rd(a.get("iv_rank"))
    n, w = _rd(r.get("now")), _rd(r.get("week"))
    rn, rw, ch = _num(n.get("rank")), _num(w.get("rank")), _num(r.get("change"))
    miss = _radar_missing(a, "iv_rank", "iv_rank_week")
    if rn is None and rw is None:
        return _empty("IV Rank порахувати нема з чого.") + miss + block_r_year_dvol(a)
    parts = []
    if rn is not None:
        pct = _num(n.get("percentile"))
        parts.append(
            f"DVOL {_e(_dd(n.get('day')))}: IV Rank <b class='num'>{_e(_nf(rn * 100, 1))} %</b>, "
            f"перцентиль {_e(_vp(pct, 0))}; DVOL {_e(_vp(n.get('dvol')))}, за рік {_e(_vp(n.get('min')))} … {_e(_vp(n.get('max')))}."
        )
    if rw is not None:
        chs = "" if ch is None else f", зміна <b class='num'>{_e(f'{ch * 100:+.1f}'.replace('-', '−'))} п.</b>"
        parts.append(f"Тиждень тому (DVOL {_e(_dd(w.get('day')))}, пунктир): <b class='num'>{_e(_nf(rw * 100, 1))} %</b>{chs}.")
    note = (
        "<p class='note'>"
        + " ".join(parts)
        + " Rank — де DVOL між мінімумом і максимумом року; перцентиль — частка днів року з нижчим DVOL. Один сплеск IV тримає "
        "Rank низьким надовго, тому дивись і на перцентиль. Дати — за денним DVOL, не за поверхнею.</p>"
    )
    return rank_gauge_svg(rn, rw) + note + miss + block_r_year_dvol(a)


def block_r_ivrv(a: dict) -> str:
    x, (nd, _) = _rd(a.get("iv_vs_rv")), _surface_days(a)
    iv, rv30, rv7 = _num(x.get("iv30")), _num(x.get("rv30")), _num(x.get("rv7"))
    miss = _radar_missing(a, "surface", "rv30", "rv7")
    vals = [v for v in (iv, rv30, rv7) if v is not None]
    if not vals:
        return _empty("Ні IV, ні реалізованої волатильності в знімку немає.") + miss
    mx = max(vals) * 1.1 if max(vals) > 0 else 1.0
    bars = "".join(
        _hbar(lab, (v or 0.0) / mx, _e(_vp(v)), bold=bold)
        for lab, v, bold in ((f"IV 30 дн · {_e(nd)}", iv, True), ("Реалізована 30 дн", rv30, False), ("Реалізована 7 дн", rv7, False))
    )
    p30, p7 = _num(x.get("premium_rv30")), _num(x.get("premium_rv7"))
    if p30 is not None:
        lead = "ринок закладає більший рух, ніж монета реально ходила" if p30 > 0 else "опціони дешевші за реальний рух"
        prem = f"Премія IV − RV30 <b class='num'>{_e(_dvp(p30))}</b>, IV − RV7 <b class='num'>{_e(_dvp(p7))}</b>: {lead}. "
    else:
        prem = ""
    note = (
        f"<p class='note'>{prem}IV — ATM на 30 днів з поверхні {_e(nd)} 19:00 UTC; реалізована — денні закриття Binance до "
        "моменту знімка (останнє — поточна ціна дня). На реальних марках Deribit продаж ATM-стреддлу з хеджем дав ≈ 0: "
        "премія сама по собі ще не перевага.</p>"
    )
    return f"<div class='hbars'>{bars}</div>" + note + miss


def block_r_skew(a: dict) -> str:
    s, (nd, wd) = _rd(a.get("skew")), _surface_days(a)
    n, w, c = _rd(s.get("now")), _rd(s.get("week")), _rd(s.get("change"))
    miss = _radar_missing(a, "surface", "surface_week")
    if not n and not w:
        return _empty("Скью з архіву поверхні немає.") + miss
    tiles = ""
    for key, lab in (("put_skew_25", "Put skew 25Δ"), ("rr_25", "RR 25Δ")):
        d = _num(c.get(key))
        cls = "warn" if d is not None and abs(d) > 0.002 else ""
        sub = f"{_e(nd)}; тиждень тому ({_e(wd)}) {_e(_lvp(w.get(key)))} · <span class='{cls}'>{_e(_dvp(d))}</span>"
        tiles += _tile(lab, f"<span class='num'>{_e(_lvp(n.get(key)))}</span>", sub, help="skew" if key == "rr_25" else "put_skew")
    note = (
        "<p class='note'>Put skew — на скільки 25-дельтовий пут дорожчий за ATM: ціна захисту від падіння. RR — 25Δ кол мінус "
        "25Δ пут: нижче нуля — пути дорожчі за коли. Строк 30 днів.</p>"
    )
    return f"<div class='tiles'>{tiles}</div>" + note + miss


def block_r_smile(a: dict) -> str:
    sm, (nd, wd) = _rd(a.get("smile")), _surface_days(a)
    n, w = _rd(sm.get("now")), _rd(sm.get("week"))
    miss = _radar_missing(a, "surface", "surface_week")
    if not n and not w:
        return _empty("Усмішки з архіву поверхні немає.") + miss
    nv = [_num(n.get(k)) for k, _ in RADAR_SMILE]
    wv = [_num(w.get(k)) for k, _ in RADAR_SMILE]
    svg = radar_lines_svg([lab.replace(" ", "") for _, lab in RADAR_SMILE], nv, wv, "Усмішка на 30 днів: зараз і тиждень тому")
    head = "".join(f"<th class='r'>{_e(lab)}</th>" for _, lab in RADAR_SMILE)
    row_n = "".join(f"<td class='r num'>{_e(_nf(v * 100, 1) if v is not None else '—')}</td>" for v in nv)
    row_w = "".join(f"<td class='r num mut'>{_e(_nf(v * 100, 1) if v is not None else '—')}</td>" for v in wv)
    table = (
        f"<div class='scroll'><table><tr><th>IV, %</th>{head}</tr><tr><td>{_e(nd)}</td>{row_n}</tr>"
        f"<tr><td class='mut'>{_e(wd)}</td>{row_w}</tr></table></div>"
    )
    return svg + _two_lines_legend(nd, wd) + table + miss


def rolling_median(vals: list[float | None], w: int = RADAR_HIST_MEDIAN, min_n: int = 3) -> list[float | None]:
    """Median of the last ``w`` days (this one included) with at least ``min_n`` values, else None (the mock-up's rule)."""
    out: list[float | None] = []
    for i in range(len(vals)):
        win = [v for v in vals[max(0, i - w + 1) : i + 1] if v is not None]
        out.append(statistics.median(win) if len(win) >= min_n else None)
    return out


def block_r_skewhist(a: dict) -> str:
    """Skew over the year (the snapshot's `year.skew`, from the radar archive): 7-day medians of put skew and RR 25Δ."""
    miss = _radar_missing(a, "year_skew")
    g = _year_grid(_rd(a.get("year")).get("skew"), "put_skew_25", "rr_25")
    if g is None:
        return _empty("Історії скью ще немає: в архіві поверхні нема рядків за рік (бекфіл scripts/radar_backfill.py не прогнано).") + miss
    start, (ps, rr) = g
    days = [i for i in range(min(len(ps), len(rr))) if ps[i] is not None or rr[i] is not None]
    if len(days) < RADAR_HIST_MIN_DAYS:
        return (
            _empty(
                f"Історії скью ще мало: {len(days)} дн. в архіві поверхні; графік — від {RADAR_HIST_MIN_DAYS} днів, повний рік — після "
                "бекфілу scripts/radar_backfill.py."
            )
            + miss
        )
    sp, sr = rolling_median(ps), rolling_median(rr)
    svg = year_svg(
        start,
        [(sp, "stroke:var(--bad)"), (sr, "stroke:var(--acc)")],
        "Put skew і RR 25Δ за рік",
        _vol_tick,
        zero=True,
    )
    legend = (
        "<div class='legend'><span><i style='background:var(--bad)'></i>put skew 25Δ</span>"
        "<span><i style='background:var(--acc)'></i>RR 25Δ</span><span>медіана за 7 днів, вп</span></div>"
    )
    short = f", {len(days)} з 365 днів" if len(days) < RADAR_YEAR_FULL_DAYS else ""
    note = (
        f"<p class='note'>Архів поверхні {_e(_day_label(start, days[0]))}–{_e(_day_label(start, days[-1]))}{_e(short)}, строк 30 днів, "
        "медіана за 7 днів (щонайменше 3 дні з даними). Put skew вгору або RR вниз — захист від падіння дорожчає. Рівні з "
        "іншими джерелами не порівнювати (інша конвенція дельти), лише зміни.</p>"
    )
    return svg + legend + note + miss


def _fwd_split(a: dict) -> tuple[list[dict], list[dict], float | None]:
    """(windows ending within RADAR_FWD_DAYS, the farther ones, median of the weekly windows) of the forward-vol block."""
    fv = _rd(a.get("forward_vol"))
    windows = [x for x in _list(fv.get("windows")) if isinstance(x, dict)]
    # the mock-up's horizon: windows ending within RADAR_FWD_DAYS (the quarterly ones list a dozen events each)
    far = [x for x in windows if (_num(x.get("to_days")) or 0.0) > RADAR_FWD_DAYS]
    return [x for x in windows if x not in far], far, _num(fv.get("median_weekly"))


def _fwd_hot(shown: list[dict]) -> list[dict]:
    """The «hot» windows (flagged by analytics.radar), the highest forward vol first."""
    return sorted((x for x in shown if x.get("hot") is True and _num(x.get("fwd")) is not None), key=lambda x: -_num(x.get("fwd")))


def _fwd_span(x: dict) -> str:
    return f"{_dd(x.get('from_utc'))}–{_dd(x.get('to_utc'))}"


def _ev_title(ev: dict, n: int = 60) -> str:
    return _clean(ev.get("title") or ev.get("kind") or "подія", n)


def fwd_vol_svg(shown: list[dict]) -> str:
    """Forward vol per window as bars on a day axis from the archive row's 19:00 UTC (as in the mock-up): «hot» windows
    amber, windows shorter than RADAR_FWD_MIN_DAYS grey and unlabelled; calendar events as dashed red lines."""
    bars = []
    for x in shown:
        f, to, days = _num(x.get("fwd")), _num(x.get("to_days")), _num(x.get("days"))
        if f is None or to is None or days is None or f <= 0 or days <= 0 or to <= 0:
            continue
        bars.append((to - days, to, f, x))
    if not bars:
        return ""
    W, H, L, R, Tp, B = 720, 260, 40, 12, 50, 34
    t1 = max(b[1] for b in bars)  # > 0: bars ending at or before the row's day are dropped above
    hi = max(b[2] for b in bars) * 1.15

    def X(d: float) -> float:
        return L + max(0.0, min(d, t1)) / t1 * (W - L - R)

    def Y(v: float) -> float:
        return Tp + (hi - v) / hi * (H - Tp - B)

    g = []
    st_ = nice_step(hi * 100, 4) / 100
    for i in range(int(math.floor(hi / st_)) + 1):
        v = i * st_
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{_e(_nf(v * 100, 0))}</text>'
        )
    for d0, d1, f, x in bars:
        hot, short = x.get("hot") is True, d1 - d0 < RADAR_FWD_MIN_DAYS
        style = (
            "fill:var(--warn);stroke:var(--warn)"
            if hot
            else "fill:var(--line);stroke:var(--mut)"
            if short
            else "fill:rgba(var(--acc-rgb),.25);stroke:var(--acc)"
        )
        x0, x1 = X(d0), X(d1)
        g.append(
            f'<rect x="{x0 + 0.5:.1f}" y="{Y(f):.1f}" width="{max(x1 - x0 - 1, 1):.1f}" height="{Y(0) - Y(f):.1f}" style="{style};stroke-width:1"/>'
        )
        if not short:
            g.append(
                f'<text x="{(x0 + x1) / 2:.1f}" y="{Y(f) - 5:.1f}" text-anchor="middle" style="fill:var(--fg);font-weight:600">{_e(_nf(f * 100, 0))}</text>'
            )
    # expiry dates under the axis, skipping ones that would overlap
    ticks = sorted({(d0, _dd(x.get("from_utc"))) for d0, _, _, x in bars} | {(d1, _dd(x.get("to_utc"))) for _, d1, _, x in bars})
    last = -1e9
    for d, lab in ticks:
        if X(d) - last < 34:
            continue
        last = X(d)
        anchor = "end" if X(d) > W - R - 20 else "middle"
        g.append(f'<text x="{X(d):.1f}" y="{H - 18}" text-anchor="{anchor}">{_e(lab)}</text>')
    # events: days from the row's reference, which is the window's end minus its `to_days`
    ref = None
    for _, d1, _, x in bars:
        end = parse_utc(x.get("to_utc"))
        if end is not None:
            ref = end.timestamp() - d1 * 86400
            break
    rows_end = [-1e9, -1e9, -1e9]
    for _, _, _, x in bars:
        for ev in _list(x.get("events")):
            ts = parse_utc(ev.get("ts_utc")) if isinstance(ev, dict) else None
            if ts is None or ref is None:
                continue
            d = (ts.timestamp() - ref) / 86400
            if not 0 < d <= t1:
                continue
            lab = f"{RADAR_EVN.get(str(ev.get('kind')), _ev_title(ev, 14))} {_dd(ts.isoformat())}"
            xe = X(d)
            row = next((i for i, r in enumerate(rows_end) if r < xe), 2)
            wpx = len(lab) * 6.6 + 8
            rows_end[row] = xe + wpx
            tx, anchor = (xe - 4, "end") if xe + wpx > W - R else (xe + 4, "start")
            g.append(
                f'<line x1="{xe:.1f}" x2="{xe:.1f}" y1="{Tp - 30 + row * 11}" y2="{Y(0):.1f}" style="stroke:var(--bad);stroke-width:1.5;stroke-dasharray:4 3"/>'
                f'<text x="{tx:.1f}" y="{Tp - 22 + row * 11}" text-anchor="{anchor}" style="fill:var(--bad)">{_e(lab)}</text>'
            )
    g.append(f'<text x="{L}" y="{H - 4}">експірації Deribit (дата UTC); висота — форвардна вола між сусідніми експіраціями, %</text>')
    return (
        "<div class='scroll'><div style='min-width:560px'>"
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Форвардна вола між експіраціями і події">{"".join(g)}</svg></div></div>'
    )


def block_r_fwd(a: dict) -> str:
    nd, _ = _surface_days(a)
    shown, far, med = _fwd_split(a)
    miss = _radar_missing(a, "surface", "forward_vol")
    if not shown and not far:
        return _empty("Форвардної воли немає: потрібен рядок архіву поверхні з двома й більше експіраціями.") + miss
    svg = fwd_vol_svg(shown)
    legend = (
        "<div class='legend'><span><i style='background:var(--acc)'></i>вікно від 5 днів</span>"
        "<span><i style='background:var(--warn)'></i>гарячий тиждень</span><span><i style='background:var(--mut)'></i>коротше 5 днів (шум)</span>"
        "<span><i style='background:var(--bad)'></i>подія</span></div>"
    )
    hot = _fwd_hot(shown)
    if hot:
        lead = "".join(
            f"<p><span class='chip ev'>гарячий тиждень</span> <b class='num'>{_e(_fwd_span(x))}</b>: {_e(_vp(x.get('fwd')))} проти медіани "
            f"тижневих вікон {_e(_vp(med))}.</p>"
            for x in hot
        )
    else:
        lead = f"<p>Жоден тиждень не виділяється (медіана тижневих вікон {_e(_vp(med))}).</p>" if med is not None else ""
    evs, gaps = [], []
    for x in shown:
        if _num(x.get("fwd")) is None:
            gaps.append(f"{_e(_fwd_span(x))} ({_e(_clean(x.get('reason') or 'немає', 80))})")
        for ev in _list(x.get("events")):
            if isinstance(ev, dict):
                evs.append(
                    f"{_e(_ev_title(ev))} <span class='mut'>{_e(kyiv(parse_utc(ev.get('ts_utc'))))}</span> — вікно {_e(_fwd_span(x))}, "
                    f"{_e(_vp(x.get('fwd')))}"
                )
    out = svg + legend + lead
    if evs:
        out += "<p class='note'>Події у вікнах (час Київ): " + "; ".join(evs) + ".</p>"
    if gaps:
        out += "<p class='note'>Без форвардної воли: " + "; ".join(gaps) + ".</p>"
    if far:
        last = _dd(far[-1].get("to_utc"))
        out += f"<p class='note'>Вікна, що закінчуються далі ніж за {RADAR_FWD_DAYS} днів (до {_e(last)}), не показано, як у макеті: {len(far)}.</p>"
    note = (
        "<p class='note'>Форвардна вола — скільки руху ринок закладає саме між двома сусідніми експіраціями (з ATM IV "
        f"поверхні {_e(nd)} 19:00 UTC): так видно, на який тиждень ринок кладе подію. «Гарячий тиждень» — вікно від 5 днів, що "
        "закінчується в межах 100 днів, з волою не нижче медіани таких вікон + 3 вп. Вікна коротші за 5 днів шумні (сірим, без "
        "підпису). Події — FOMC, CPI, PCE, NFP, PPI, ВВП, вибори, за датою; підпис події — не доказ, що ринок закладає саме її.</p>"
    )
    return out + note + miss


# ---- «Поточна ситуація» and «Що це означає для угод»: deterministic text from the same fields, no recomputation


def radar_situation(a: dict, asset: str) -> list[tuple[str, str, str]]:
    """Up to five (chip class, chip label, plain text) lines in the mock-up's style: price, IV by tenor and the front,
    IV Rank, skew, the «hot» week. A line whose fields are null is left out. Text is plain: the caller escapes it."""
    nd, wd = _surface_days(a)
    out: list[tuple[str, str, str]] = []
    p = _rd(a.get("price"))
    pn, pw, ch = _num(p.get("now")), _num(p.get("week")), _num(p.get("change"))
    if pn is not None:
        if pw is not None and ch is not None:
            out.append(("calm", "ціна", f"{asset} {_pc(ch * 100)} за тиждень: {_nf(pn)} проти {_nf(pw)} ({nd} проти {wd}, 19:00 UTC)."))
        else:
            out.append(("calm", "ціна", f"{asset} {_nf(pn)} ({nd}, 19:00 UTC); порівняти з тижнем тому нема з чим."))
    at = _rd(a.get("atm"))
    n, c = _rd(at.get("now")), _rd(at.get("change"))
    d = [_num(c.get(k)) for k, _ in RADAR_TENORS]
    ok = [x for x in d if x is not None]
    parts, tone = [], "ev" if d[0] is not None and d[0] > 0.01 else "calm"
    if ok:
        how = (
            "виросла на всіх строках"
            if all(x > 0.002 for x in ok)
            else "знизилась на всіх строках"
            if all(x < -0.002 for x in ok)
            else "змінилась по-різному"
        )
        parts.append(f"IV {how}: " + ", ".join(f"{lab} {_dvp(x)}" for (_, lab), x in zip(RADAR_TENORS, d, strict=True) if x is not None))
    v7, v30 = _num(n.get("7")), _num(n.get("30"))
    if v7 is not None and v30 is not None:
        sl = v7 - v30
        if sl > 0.005:
            parts.append(f"інверсія фронту: 7 днів дорожчі за 30 на {_lvp(sl)}, ринок платить за найближчі дні")
            tone = "risk"
        elif sl < -0.005:
            parts.append(f"структура висхідна: 7 днів нижче 30 на {_lvp(-sl)}")
        else:
            parts.append(f"фронт плаский: 7 днів {_vp(v7)} проти 30 днів {_vp(v30)}")
    if parts:
        txt = "; ".join(parts) + "."
        out.append((tone, "вола", txt[0].upper() + txt[1:]))
    r = _rd(a.get("iv_rank"))
    rn_, rw_ = _rd(r.get("now")), _rd(r.get("week"))
    rn, rw = _num(rn_.get("rank")), _num(rw_.get("rank"))
    if rn is not None:
        pos, tone = ("біля дна", "opp") if rn < 0.2 else ("біля верху", "risk") if rn > 0.6 else ("посередині", "calm")
        wk = f" (тиждень тому {_nf(rw * 100, 1)} %)" if rw is not None else ""
        lo, hi = _num(rn_.get("min")), _num(rn_.get("max"))
        rng = f" {_nf(lo * 100, 1)}–{_nf(hi * 100, 1)} %" if lo is not None and hi is not None else ""
        out.append(
            (
                tone,
                "IV Rank",
                f"IV Rank {_nf(rn * 100, 1)} %{wk}, перцентиль {_vp(rn_.get('percentile'), 0)}: IV {pos} річного діапазону DVOL{rng} "
                f"(DVOL {_dd(rn_.get('day'))}).",
            )
        )
    s = _rd(a.get("skew"))
    sn, sw, sc = _rd(s.get("now")), _rd(s.get("week")), _rd(s.get("change"))
    if _num(sn.get("put_skew_25")) is not None or _num(sn.get("rr_25")) is not None:
        ds, dr = _num(sc.get("put_skew_25")), _num(sc.get("rr_25"))
        fear = (ds is not None and ds > 0.004) or (dr is not None and dr < -0.008)
        calm = (ds is not None and ds < -0.004) or (dr is not None and dr > 0.008)
        how = (
            "порівняти з тижнем тому нема з чим"
            if ds is None and dr is None
            else "захист від падіння подорожчав відносно ATM і колів"
            if fear
            else "захист від падіння подешевшав"
            if calm
            else "захист від падіння майже не змінився"
        )
        out.append(
            (
                "risk" if fear else "calm",
                "скью",
                f"Put skew {_lvp(sw.get('put_skew_25'))} → {_lvp(sn.get('put_skew_25'))}, RR {_lvp(sw.get('rr_25'))} → {_lvp(sn.get('rr_25'))}: {how}.",
            )
        )
    shown, _, med = _fwd_split(a)
    hot = _fwd_hot(shown)
    if hot:
        x = hot[0]
        evs = [f"{_ev_title(ev)} {kyiv(parse_utc(ev.get('ts_utc')))}" for ev in _list(x.get("events")) if isinstance(ev, dict)]
        tail = f"; на нього припадає: {', '.join(evs)}" if evs else "; подій з календаря на нього не припадає"
        out.append(
            ("ev", "події", f"Найдорожчий тиждень — {_fwd_span(x)}: форвардна вола {_vp(x.get('fwd'))} проти медіани {_vp(med)}{tail}.")
        )
    elif med is not None:
        out.append(("calm", "події", f"Жоден тиждень до {RADAR_FWD_DAYS} днів не виділяється: форвардна вола близько медіани {_vp(med)}."))
    return out


def block_r_now(a: dict, asset: str) -> str:
    lines = radar_situation(a, asset)
    if not lines:
        return _empty("Поточну ситуацію скласти нема з чого: у секції радару немає ні ціни, ні IV, ні IV Rank, ні скью.")
    items = "".join(f"<li><span class='chip {c}'>{_e(lab)}</span><span>{_e(t)}</span></li>" for c, lab, t in lines)
    return f"<ul class='key'>{items}</ul>"


RADAR_DEAL_NOTE = (
    "Це детерміновані правила макета, а не порада й не сигнал: на наші перевірки спираються лише рядки про режим IV і IV проти "
    "RV, решта не перевірена; доведеної переваги жодне з них не має, тож усе тут — гіпотези. Метод рекомендацій асистента перевірено 18.10 (H1) і відхилено: його угоди без хеджу дали −0.59 % спота за угоду "
    "[−2.43; +1.51], n = 27. IV, «закладений рух» і ймовірності з цін опціонів — ризик-нейтральні: це ціна страхування в ринку, а "
    "не прогноз. Сторінка нічого не торгує."
)


def block_r_deal(a: dict, asset: str) -> str:
    """«Що це означає для угод»: cautious rule-based lines from the mock-up (no LLM text). Only the IV-regime and IV-vs-RV
    lines cite our checks; the H2 number is quoted only in the regime H2 tested (DVOL 365-day percentile ≥ 0.80)."""
    L: list[str] = []
    rk = _rd(_rd(a.get("iv_rank")).get("now"))
    rn, pct = _num(rk.get("rank")), _num(rk.get("percentile"))
    if rn is not None:
        r = _e(_nf(rn * 100, 0))
        if rn < 0.2:
            L.append(
                f"<b>Опціони дешеві відносно року</b> (IV Rank {r} %), але це не сигнал купувати: за нашими перевірками при низькій IV "
                "ні купівля, ні продаж волатильності переваги не мають. Куплена волатильність з хеджем у нас програвала "
                "(−1.18 % спота за угоду [−1.81; −0.48]); без хеджу заробляв лише напрям. Купувати — лише під власну тезу чи подію "
                "і закритою конструкцією (спред), розмір — за правилом 8."
            )
        elif rn > 0.6:
            sell = (
                "загалом продаж ATM-стреддлу з хеджем на реальних марках Deribit дав +0.09 % [−0.49; +0.88], перевагу не доведено. "
                "Якщо продавати — лише закриті конструкції (кондор, метелик) і малий розмір."
            )
            if pct is not None and pct >= 0.8:
                L.append(
                    f"<b>IV у верхній частині року</b> (IV Rank {r} %, перцентиль DVOL {_e(_vp(pct, 0))} за рік). У перевіреному "
                    "режимі «перцентиль DVOL ≥ 80 % за рік» продаж ATM-стреддлу з хеджем на реальних марках Deribit дав +1.43 % спота "
                    "[−0.13; +3.45], але це описово (n = 16, тримається на 5 угодах 2021–23); " + sell
                )
            else:
                L.append(
                    f"<b>IV у верхній частині діапазону</b> (IV Rank {r} %), але перевірений режим (перцентиль DVOL ≥ 80 % за рік) "
                    f"не настав: зараз перцентиль {_e(_vp(pct, 0))}. " + sell[0].upper() + sell[1:]
                )
        else:
            L.append(
                f"<b>IV посередині річного діапазону</b> (IV Rank {r} %): режим нейтральний, перевага не на боці ні покупця, ні продавця."
            )
    iv30, rv30 = _num(_rd(a.get("iv_vs_rv")).get("iv30")), _num(_rd(a.get("iv_vs_rv")).get("rv30"))
    if iv30 is not None and rv30 is not None:
        if iv30 - rv30 > 0.03:
            L.append(
                f"Відносно реального руху опціони <b>не дешеві</b>: IV 30 днів {_e(_vp(iv30))} проти реалізованої {_e(_vp(rv30))} за 30 днів. "
                "Куплений опціон у середньому програє, якщо рух лишиться таким, як був; продаж ATM-стреддлу з хеджем на реальних марках дав ≈ 0 "
                "(+0.09 % [−0.49; +0.88]), тож премія сама по собі ще не перевага й для продавця."
            )
        else:
            L.append(
                f"Відносно реального руху опціони <b>дешеві або чесні</b>: IV 30 днів {_e(_vp(iv30))} проти реалізованої {_e(_vp(rv30))} за 30 днів."
            )
    hot = _fwd_hot(_fwd_split(a)[0])
    if hot:
        x = hot[0]
        titles = [_ev_title(ev) for ev in _list(x.get("events")) if isinstance(ev, dict)]
        evs = f" ({_e(', '.join(titles))})" if titles else ""
        L.append(
            f"Ринок закладає підвищений рух на <b>{_e(_fwd_span(x))}</b>{evs}. Опціон під подію береться з експірацією після неї, "
            "і він уже дорожчий за сусідні тижні. Підпис події — за датою, не доказ, що ринок закладає саме її."
        )
    n = _rd(_rd(a.get("atm")).get("now"))
    v7, v30 = _num(n.get("7")), _num(n.get("30"))
    if v7 is not None and v30 is not None and v7 - v30 > 0.005:
        L.append(
            f"Фронт дорожчий за місяць (7 днів {_e(_vp(v7))} проти 30 днів {_e(_vp(v30))}): тижневі опціони дорогі; якщо купувати, "
            "то довший строк. Правило макета, нами не перевірено."
        )
    sc = _rd(_rd(a.get("skew")).get("change"))
    ds, dr = _num(sc.get("put_skew_25")), _num(sc.get("rr_25"))
    if (ds is not None and ds > 0.004) or (dr is not None and dr < -0.008):
        L.append(
            f"Захист від падіння подорожчав за тиждень (put skew {_e(_dvp(ds))}, RR {_e(_dvp(dr))}): пути дорожчають відносно ATM, "
            "тож у пут-спреді проданий дальший пут окуповує більшу частку ціни купленого, ніж тиждень тому. Правило макета, нами "
            "не перевірено."
        )
    spot = _num(_rd(a.get("price")).get("now"))
    if spot is not None and spot > 0 and v30 is not None and v30 > 0:
        call = _b76(spot, spot, 30 / 365, v30, True)
        qty = 100 / call if call > 0 else None
        lot = " (на Bybit чи OKX лот 0.01 BTC; на Deribit мінімум 0.1 — завеликий)" if asset == "BTC" else ""
        L.append(
            f"Розмір — за правилом 8: ризик на угоду не більше $100. Куплений ATM-кол на 30 днів коштує ≈ {_e(_usd(call))} за 1 {asset} "
            f"(Black-76 за ATM IV 30 днів, r = 0, ціна індексу замість форварду), тож $100 — це ≈ "
            f"{_e(_nf(qty, 3 if asset == 'BTC' else 2))} {asset}{_e(lot)}. Спред чи дальший страйк дозволяє взяти більше."
        )
    body = "".join(f"<p style='margin:0 0 8px'>{x}</p>" for x in L) if L else _empty("Даних для висновків у секції радару немає.")
    return body + f"<p class='note'>{_e(RADAR_DEAL_NOTE)}</p>"


def _radar_switch(asset: str) -> str:
    links = "".join(f"<a href='/radar?asset={a}'{' aria-current=page' if a == asset else ''}>{a}</a>" for a in RADAR_ASSETS)
    return f"<div class='seg' role='group' aria-label='Актив'>{links}</div>"


def _radar_dates(a: dict) -> str:
    nd, wd = _surface_days(a)
    r = _rd(a.get("iv_rank"))
    rn, rw = _dd(_rd(r.get("now")).get("day")), _dd(_rd(r.get("week")).get("day"))
    return (
        f"<span class='mut'>Поверхня (ціна, IV, скью, усмішка, форвардна вола): <b>{_e(nd)}</b> 19:00 UTC проти <b>{_e(wd)}</b> · "
        f"IV Rank (денний DVOL): <b>{_e(rn)}</b> проти <b>{_e(rw)}</b></span>"
    )


def render_radar(snap: dict | None, err: str | None, now: float, params: dict | None = None, path: Path | None = None) -> str:
    """The «Радар» tab. Never raises: no snapshot, no radar section or an empty archive gives an explained state."""
    asset = (params or parse_radar_query(""))["asset"]
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>tradedesk — радар</title><style>{CSS}{BRIEF_CSS}{RADAR_CSS}</style></head><body>"
    )
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in RADAR_CAVEATS) + "</ul></div>"
    try:
        if snap is None:
            msg = _snapshot_problem(err, path)
            return head + f"<header><h1>Радар</h1><span class='bad'>{msg}</span></header>{_nav('radar')}{caveats}</body></html>"
        out = [head, f"<header><h1>Радар</h1>{_snapshot_stamp(snap, now)}</header>{_nav('radar')}{caveats}"]
        sec = _section(snap, "radar")
        if not sec:
            out.append(
                f"<div class='rhead'>{_radar_switch(asset)}</div><main class='brief'><section class='wide'>"
                + _empty(
                    "Секції «радар» у знімку ще немає: знімок зібрано версією колектора без радару або завдання "
                    "assistant_snapshot ще не відпрацювало з новим кодом."
                )
                + "</section></main></body></html>"
            )
            return "".join(out)
        a = _rd(_rd(sec.get("data")).get("assets")).get(asset)
        notes = _section_notes(snap, "radar") + _radar_source_notes(sec, asset)
        if not isinstance(a, dict):
            out.append(
                f"<div class='rhead'>{_radar_switch(asset)}</div><main class='brief'><section class='wide'>"
                + _empty(f"Даних {asset} у секції радару немає.")
                + notes
                + "</section></main></body></html>"
            )
            return "".join(out)
        out.append(f"<div class='rhead'>{_radar_switch(asset)}{_radar_dates(a)}</div><main class='brief'>")
        if _rd(a.get("surface")).get("now_day") is None:
            notes = (
                _empty(
                    f"Архіву поверхні {asset} ще немає або він відстає: ціна, ATM IV, скью, усмішка й форвардна вола з'являться, "
                    "коли завдання колектора radar_archive запише рядок (або буде прогнано бекфіл scripts/radar_backfill.py). "
                    "IV Rank і реалізована вола від архіву не залежать."
                )
                + notes
            )
        if notes:
            out.append(f"<section class='wide'>{notes}</section>")
        out.append(f"<section class='wide'>{_h2('Поточна ситуація', 'r_now')}{_guard(lambda x: block_r_now(x, asset), a)}</section>")
        cards = (
            ("", "1 · Ціна", "r_price", lambda x: block_r_price(x, asset)),
            ("", "2 · ATM IV за строками", "r_term", block_r_term),
            ("", "3 · IV Rank за рік", "r_rank", block_r_rank),
            ("", "4 · IV проти реального руху", "r_ivrv", block_r_ivrv),
            ("", "5 · Скью 25Δ · 30 днів", "r_skew", block_r_skew),
            ("", "Усмішка на 30 днів", "r_smile", block_r_smile),
            ("", "Скью за рік", "r_skewhist", block_r_skewhist),
            ("wide", "6 · Форвардна вола між експіраціями і події", "r_fwd", block_r_fwd),
        )
        for cls, title, key, fn in cards:
            out.append(f"<section class='{cls}'>{_h2(title, key)}{_guard(fn, a)}</section>")
        out.append(
            f"<section class='wide'>{_h2('Що це означає для угод', 'r_deal')}{_guard(lambda x: block_r_deal(x, asset), a)}</section>"
        )
        out.append("</main></body></html>")
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header><h1>Радар</h1><span class='bad'>Знімок не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('radar')}{caveats}</body></html>"
        )


def radar_page(query: str = "", now: float | None = None) -> str:
    snap, err = load_snapshot(SNAPSHOT_PATH)
    return with_help(render_radar(snap, err, time.time() if now is None else now, parse_radar_query(query), SNAPSHOT_PATH))


# ---------------------------------------------------------------- the owner's rules (docs/knowledge/trading-strategy.md, 18.10)
#
# Stdlib copies of analytics.rules_check (pre_trade_option, trade_checks, weekly_violations, compliance_summary; tests compare each pair on
# the same inputs): rules 1 and 8 before an option trade («Опціони»), rules 1, 2, 4, 5 and 8 week by week on the
# owner's real trades («Журнал»). Statuses: ok / violated / unknown (the data cannot tell) / na (does not apply).

RULES_DEPOSIT = 10_000.0  # the card: deposit ≈ $10 000, 1 % = $100 (the journal has no deposit column)
RULES_RISK_PCT = 1.0  # rule 1, % of the deposit
RULES_MAX_ALT_LONGS = 3  # rule 2
RULES_DAY_LOSS_PCT = 2.0  # rule 4, % of the deposit
RULES_STREAK = 2  # rule 4: losing trades in a row
RULES_MIN_RR = 2.0  # rule 5
RULES_PREMIUM_PCT = 1.0  # rule 8, % of the deposit
RULES_BUY_BAN_HOURS = 24.0  # rule 8: under this many hours to expiry never buy
RULES_BUY_COND_HOURS = 72.0  # rule 8: from 24 h up to this (inclusive) only with all three checks
RULES_BE_MAX_SIGMA = 1.0  # rule 8 (b): breakeven move <= this many 1-sigma moves at RV over the time left
RULES_IV_MAX_RV = 1.2  # rule 8 (c): the bought option's IV <= this x RV
RULES_ZONE_BAN, RULES_ZONE_COND, RULES_ZONE_FREE = "under_24h", "1_3_days", "over_3_days"
RULES_IMPORT_THESIS_PLACEHOLDER = "теза не записана"  # what scripts/bybit_import.py writes as an import's thesis
RULES_MAJORS = frozenset({"BTC", "ETH"})
RULES_NUMS = (1, 2, 4, 5, 8)
RULES_WEEKS = 12  # newest ISO weeks in the journal table
RULES_MIN_CHECKED = 30  # «За правилами»: under this many checked trades the share is descriptive only
RULES_OPTION_RE = re.compile(r"([A-Z0-9]+)-(\d{1,2})([A-Z]{3})(\d{2})-\d+(?:[.d]\d+)?-[CP](?:-[A-Z]+)?")
RULES_QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")
RULE_STATUS_UA = {
    "ok": ("виконано", "opp"),
    "violated": ("порушено", "risk"),
    "unknown": ("невідомо", "calm"),
    "na": ("не стосується", "calm"),
}
RULES_WEEK_HEAD = (
    (1, "1 · ризик ≤ 1 %"),
    (2, "2 · ≤ 3 лонги в альтах"),
    (4, "4 · денний стоп"),
    (5, "5 · R:R ≥ 2"),
    (8, "8 · опціони"),
)
RULES_WEEK_NOTE = (
    "Правила власника, затверджені 18.10 (база знань «Торгова стратегія»). Тиждень — ISO-тиждень відкриття угоди (UTC), "
    "нові зверху; у клітинці «порушено з перевірених», «невідомо» — правило стосується угоди, але даних не вистачає. "
    "1 і 5 потребують стопу (і цілі): в імпорті з бірж їх немає, тож там лише куплені опціони (ризик = премія). "
    "2 — відкриття 4-го різного альта (усе, крім BTC і ETH, без опціонів) при трьох уже відкритих; рядки Bybit без часу "
    "відкриття — «невідомо», як і символи HIP-3 Hyperliquid (xyz:TSLA — акції, золото, не крипто-альти). "
    "4 — угода, відкрита того ж дня UTC після 2 збиткових поспіль або −2 % депозиту за день (порядок закриття); "
    "рядки без часу відкриття (здебільшого Bybit) оцінено за часом закриття — це «наближено», вони рахуються окремо "
    "від точних порушень і не входять у «Порушили» та «P&L цих угод». "
    "8 — куплений опціон: премія > 1 % депозиту; менше 24 год до експірації (08:00 UTC) — порушено завжди; за 1–3 доби — "
    "лише з тезою й ціллю, беззбитком у межах 1σ руху за реалізованою волою і IV ≤ 1.2 × RV: в імпорті з бірж тези й цілі "
    "немає, тож такі купівлі — «порушено», а спота й IV на час відкриття журнал не має (ці дві умови — «невідомо»). "
    "Депозит — сьогоднішні $10 000 для всіх тижнів. 3, 6, 7 і «48 год перерви» з журналу не перевіряються. "
    "Минуле не гарантує майбутнього."
)


def rules_option_expiry_ts(symbol: object) -> float | None:
    """Delivery time (unix s, 08:00 UTC) of an option symbol ('BTC-31OCT26-90000-C', '…-C-USDT'); None otherwise."""
    m = RULES_OPTION_RE.fullmatch(str(symbol or "").upper())
    if not m or m.group(3) not in _MON_EN:
        return None
    try:
        return datetime(2000 + int(m.group(4)), _MON_EN[m.group(3)], int(m.group(2)), 8, tzinfo=timezone.utc).timestamp()  # noqa: UP017
    except ValueError:
        return None


def rules_is_hip3(symbol: object) -> bool:
    """A Hyperliquid HIP-3 symbol 'dex:ASSET' ('xyz:TSLA': stocks, gold): not a crypto alt for rule 2."""
    s = str(symbol or "")
    return ":" in s and "/" not in s


def rules_base_asset(symbol: object) -> str:
    s = str(symbol or "").upper().strip()
    s = s.split(":")[-1] if rules_is_hip3(s) else s.split("/")[0].split(":")[0]
    s = s.split("-")[0] if "-" in s else s
    for q in RULES_QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _rules_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")  # noqa: UP017


def _rules_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()  # noqa: UP017
    return f"{y}-W{w:02d}"


def _rules_status(violated: int, checked: int, unknown: int) -> str:
    if violated:
        return "violated"
    if checked:
        return "ok"
    return "unknown" if unknown else "na"


def rules_buy_zone(hours: float) -> str:
    """Copy of analytics.rules_check.buy_zone: rule 8 zone of a purchase this many hours before the delivery."""
    return RULES_ZONE_BAN if hours < RULES_BUY_BAN_HOURS else RULES_ZONE_COND if hours <= RULES_BUY_COND_HOURS else RULES_ZONE_FREE


def rules_thesis_check(thesis: object, target: object) -> tuple[str, str]:
    """Copy of analytics.rules_check.thesis_check: rule 8 (a), a thesis and a target price both needed."""
    has_thesis = isinstance(thesis, str) and bool(thesis.strip())
    t = _num(target)
    has_target = t is not None and t > 0
    if has_thesis and has_target:
        return "ok", "thesis and target recorded"
    miss = [w for w, ok in (("thesis", has_thesis), ("target", has_target)) if not ok]
    return "violated", "no " + " and no ".join(miss)


def rules_owner_thesis(thesis: object, tags: list[str]) -> object:
    """Copy of analytics.rules_check.owner_thesis: an import's placeholder thesis is no thesis, any other text counts."""
    if "import" in tags and isinstance(thesis, str) and RULES_IMPORT_THESIS_PLACEHOLDER in thesis:
        return None
    return thesis


def rules_conditions_status(*statuses: str) -> str:
    return "violated" if "violated" in statuses else "unknown" if "unknown" in statuses else "ok"


def rules_breakeven_move(legs: list[dict], spot: float) -> float | None:
    """Copy of analytics.rules_check.breakeven_move: smallest move from spot (fraction) to a profit at expiry."""
    if not legs or spot <= 0:
        return None
    if payoff(legs, spot) > 0:
        return 0.0
    bes = payoff_breakevens(legs)
    return min(abs(b - spot) for b in bes) / spot if bes else None


def rules_pre_trade(
    legs: list[dict],
    now_ts: float,
    expiry_ts: float | None,
    dep: float = RULES_DEPOSIT,
    spot: float | None = None,
    rv: float | None = None,
    thesis: str | None = None,
    target: float | None = None,
) -> list[dict]:
    """Copy of analytics.rules_check.pre_trade_option (rules 1 and 8 on the builder's legs)."""
    d = _num(dep)
    d = d if d is not None and d > 0 else None
    opts = [lg for lg in legs if lg.get("kind") in ("call", "put") and (_num(lg.get("size")) or 0.0) > 0]
    bought = [lg for lg in opts if lg.get("side") == "long"]
    out: list[dict] = []
    budget = d * RULES_RISK_PCT / 100.0 if d else None
    if not opts:
        out.append({"rule": 1, "key": "risk", "status": "na", "value": None, "limit": budget, "detail": "no option legs"})
    else:
        ml = payoff_extremes(legs)["max_loss"]
        if ml is None:
            out.append(
                {
                    "rule": 1,
                    "key": "risk",
                    "status": "violated",
                    "value": None,
                    "limit": budget,
                    "detail": "loss unbounded at expiry: needs a stop on the exchange or protective wings",
                }
            )
        else:
            loss = max(0.0, -ml)
            st = "unknown" if budget is None else "ok" if loss <= budget * (1 + 1e-9) else "violated"
            out.append({"rule": 1, "key": "risk", "status": st, "value": loss, "limit": budget, "detail": "max loss at expiry"})
    limit = d * RULES_PREMIUM_PCT / 100.0 if d else None
    prems = [_num(lg.get("entry_premium")) for lg in opts]
    debit = None
    if bought and all(p is not None for p in prems):
        debit = sum((1.0 if lg.get("side") == "long" else -1.0) * float(lg["size"]) * float(p) for lg, p in zip(opts, prems, strict=True))  # type: ignore[arg-type]
    floor = RULES_BUY_BAN_HOURS / 24.0
    if not bought or (debit is not None and debit <= 0):
        why = "nothing bought" if not bought else "net premium received"
        out.append({"rule": 8, "key": "premium", "status": "na", "value": debit, "limit": limit, "detail": why})
        out.append({"rule": 8, "key": "days_to_expiry", "status": "na", "value": None, "limit": floor, "zone": None, "detail": why})
        return out
    if debit is None or limit is None:
        out.append(
            {"rule": 8, "key": "premium", "status": "unknown", "value": debit, "limit": limit, "detail": "premium or deposit unknown"}
        )
    else:
        st = "ok" if debit <= limit * (1 + 1e-9) else "violated"
        out.append({"rule": 8, "key": "premium", "status": st, "value": debit, "limit": limit, "detail": "net premium paid"})
    if _num(expiry_ts) is None:
        out.append(
            {
                "rule": 8,
                "key": "days_to_expiry",
                "status": "unknown",
                "value": None,
                "limit": floor,
                "zone": None,
                "detail": "expiry unknown",
            }
        )
        return out
    hours = (float(expiry_ts) - float(now_ts)) / 3600.0  # type: ignore[arg-type]
    days = hours / 24.0
    zone = rules_buy_zone(hours)
    row = {"rule": 8, "key": "days_to_expiry", "value": days, "limit": floor, "zone": zone}
    if zone == RULES_ZONE_BAN:
        out.append({**row, "status": "violated", "detail": "under 24 h to expiry: never buy"})
        return out
    if zone == RULES_ZONE_FREE:
        out.append({**row, "status": "ok", "detail": "over 3 days to expiry: the premium limit only"})
        return out
    th_st, th_why = rules_thesis_check(thesis, target)
    a = {"rule": 8, "key": "thesis", "status": th_st, "value": th_st == "ok", "limit": None, "detail": th_why}
    s, r = _num(spot), _num(rv)
    s = s if s is not None and s > 0 else None
    r = r if r is not None and r > 0 else None
    rv_why = "RV30: 30 daily closes, zero mean, x365 (same method as the forecast log's RV30 model)"
    if debit is None:  # mirrors the analytics; the builder's legs always carry a premium (its payoff needs one)
        b = {"rule": 8, "key": "breakeven_move", "status": "unknown", "value": None, "limit": None, "detail": "premium unknown"}
    elif s is None or r is None:
        b = {"rule": 8, "key": "breakeven_move", "status": "unknown", "value": None, "limit": None, "detail": "spot or RV unknown"}
    else:
        move, cap = rules_breakeven_move(opts, s), RULES_BE_MAX_SIGMA * r * math.sqrt(days / 365.0)
        if move is None:
            b_st, b_why = "violated", "no breakeven: the structure never profits at expiry"
        else:
            b_st = "ok" if move <= cap * (1 + 1e-9) else "violated"
            b_why = f"move to the nearest breakeven vs {RULES_BE_MAX_SIGMA:g} sigma at RV over the time left; {rv_why}"
        b = {"rule": 8, "key": "breakeven_move", "status": b_st, "value": move, "limit": cap, "detail": b_why}
    ivs = [_num(lg.get("iv")) for lg in bought]
    if r is None or not ivs or any(v is None or v <= 0 for v in ivs):
        c = {"rule": 8, "key": "iv_vs_rv", "status": "unknown", "value": None, "limit": None, "detail": "option IV or RV unknown"}
    else:
        iv = max(v for v in ivs if v is not None)
        cap_iv = RULES_IV_MAX_RV * r
        c_st = "ok" if iv <= cap_iv * (1 + 1e-9) else "violated"
        c = {
            "rule": 8,
            "key": "iv_vs_rv",
            "status": c_st,
            "value": iv,
            "limit": cap_iv,
            "detail": f"highest bought IV vs {RULES_IV_MAX_RV:g} x RV; {rv_why}",
        }
    out.append(
        {
            **row,
            "status": rules_conditions_status(a["status"], b["status"], c["status"]),
            "detail": "1-3 days to expiry: only with all three checks below",
        }
    )
    out.extend((a, b, c))
    return out


def _rules_normalize(rows: list[dict]) -> list[dict]:
    out = []
    for i, t in enumerate(rows):
        status = t.get("status")
        opened = _num(t.get("opened_ts"))
        if status not in ("open", "closed") or opened is None:
            continue
        closed = _num(t.get("closed_ts")) if status == "closed" else None
        if status == "closed" and closed is None:
            closed = opened
        symbol = str(t.get("symbol") or "")
        exp = rules_option_expiry_ts(symbol)
        tags = journal_tags(t.get("tags"))
        is_opt = exp is not None or "option" in tags
        out.append(
            {
                "i": i,
                "id": t.get("id"),
                "opened": opened,
                "closed": closed,
                "open_known": status == "open" or (closed is not None and closed > opened),
                "side": t.get("side"),
                "base": "" if is_opt else rules_base_asset(symbol),
                "hip3": rules_is_hip3(symbol),
                "option": is_opt,
                "expiry": exp,
                "size": _num(t.get("size")),
                "entry": _num(t.get("entry")),
                "stop": _num(t.get("stop")),
                "target": _num(t.get("target")),
                "thesis": rules_owner_thesis(t.get("thesis"), tags),
                "pnl": _num(t.get("pnl")) if status == "closed" else None,
            }
        )
    return out


def _rules_day_stops(xs: list[dict], day_loss: float, streak: int = RULES_STREAK) -> dict[str, dict]:
    closed = sorted((x for x in xs if x["closed"] is not None and x["pnl"] is not None), key=lambda x: (x["closed"], x["i"]))
    stops: dict[str, dict] = {}
    run: dict[str, tuple[int, float]] = {}
    for x in closed:
        day = _rules_day(x["closed"])
        if day in stops:
            continue
        n, cum = run.get(day, (0, 0.0))
        n = n + 1 if x["pnl"] < 0 else 0
        cum += x["pnl"]
        run[day] = (n, cum)
        hit_streak, hit_loss = n >= streak, cum <= -day_loss + 1e-9
        if hit_streak or hit_loss:
            stops[day] = {"ts": x["closed"], "reason": "both" if hit_streak and hit_loss else "streak" if hit_streak else "day_loss"}
    return stops


def rules_day_stops(rows: list[dict], dep: float = RULES_DEPOSIT) -> dict[str, dict]:
    """Copy of analytics.rules_check.day_stops: {UTC day: {ts, reason}} where rule 4's day stop was hit."""
    return _rules_day_stops(_rules_normalize(rows), dep * RULES_DAY_LOSS_PCT / 100.0)


def _rules_alt_counts(xs: list[dict], now_ts: float) -> dict[int, tuple[int, bool]]:
    alts = sorted(
        (
            x
            for x in xs
            if not x["option"] and x["side"] == "long" and x["base"] and x["base"] not in RULES_MAJORS and x["open_known"] and not x["hip3"]
        ),
        key=lambda x: (x["opened"], x["i"]),
    )
    active: list[tuple[float, str, bool]] = []
    out: dict[int, tuple[int, bool]] = {}
    for x in alts:
        t = x["opened"]
        # a closed trade is held until its close (exclusive), an open one up to now_ts inclusive
        active = [(end, b, still) for end, b, still in active if end > t or (still and end >= t)]
        held = {b for _, b, _ in active}
        new = x["base"] not in held
        n = len(held) + (1 if new else 0)
        out[x["i"]] = (n, new and n > RULES_MAX_ALT_LONGS)
        still_open = x["closed"] is None
        active.append((max(float(now_ts), t) if still_open else x["closed"], x["base"], still_open))
    return out


def rules_trade_checks(rows: list[dict], now_ts: float, dep: float = RULES_DEPOSIT) -> list[dict]:
    """Copy of analytics.rules_check.trade_checks with the card's thresholds."""
    xs = _rules_normalize(rows)
    risk_cap, prem_cap = dep * RULES_RISK_PCT / 100.0, dep * RULES_PREMIUM_PCT / 100.0
    stops = _rules_day_stops(xs, dep * RULES_DAY_LOSS_PCT / 100.0)
    alt_n = _rules_alt_counts(xs, now_ts)
    out = []
    for x in xs:
        r: dict[int, str] = {}
        long_opt = x["option"] and x["side"] == "long"
        premium = x["size"] * x["entry"] if long_opt and x["size"] is not None and x["entry"] is not None else None
        if long_opt:
            r[1] = "unknown" if premium is None else "ok" if premium <= risk_cap * (1 + 1e-9) else "violated"
        elif x["option"] or None in (x["stop"], x["size"], x["entry"]):
            r[1] = "unknown"
        else:
            r[1] = "ok" if x["size"] * abs(x["entry"] - x["stop"]) <= risk_cap * (1 + 1e-9) else "violated"
        if x["option"] or x["side"] != "long" or x["base"] in RULES_MAJORS or not x["base"]:
            r[2] = "na"
        elif x["hip3"] or x["i"] not in alt_n:
            r[2] = "unknown"
        else:
            r[2] = "violated" if alt_n[x["i"]][1] else "ok"
        stop = stops.get(_rules_day(x["opened"]))
        r[4] = "violated" if stop is not None and x["opened"] > stop["ts"] else "ok"
        if x["option"]:
            r[5] = "na"
        elif None in (x["stop"], x["target"], x["entry"]) or x["entry"] == x["stop"]:
            r[5] = "unknown"
        else:
            r[5] = "ok" if abs(x["target"] - x["entry"]) / abs(x["entry"] - x["stop"]) >= RULES_MIN_RR - 1e-9 else "violated"
        late = big = zone = conds = None
        if long_opt:
            big = None if premium is None else premium > prem_cap * (1 + 1e-9)
            if x["expiry"] is not None and x["open_known"]:
                zone = rules_buy_zone((x["expiry"] - x["opened"]) / 3600.0)
            if zone == RULES_ZONE_COND:
                conds = {"thesis": rules_thesis_check(x["thesis"], x["target"])[0], "breakeven_move": "unknown", "iv_vs_rv": "unknown"}
                cst = rules_conditions_status(*conds.values())
                late = True if cst == "violated" else None if cst == "unknown" else False
            elif zone is not None:
                late = zone == RULES_ZONE_BAN
            r[8] = "violated" if late or big else "unknown" if late is None or big is None else "ok"
        else:
            r[8] = "na"
        out.append(
            {
                "id": x["id"],
                "week": _rules_week(x["opened"]),
                "opened_ts": x["opened"],
                "pnl": x["pnl"],
                "rules": r,
                "approx": r[4] == "violated" and not x["open_known"],
                "alt_longs": alt_n[x["i"]][0] if x["i"] in alt_n else None,
                "buy_zone": zone,
                "buy_conditions": conds,
                "late_buy": late,
                "big_premium": big,
            }
        )
    return out


def rules_weekly(rows: list[dict], now_ts: float, dep: float = RULES_DEPOSIT) -> list[dict]:
    """Copy of analytics.rules_check.weekly_violations: violations by ISO week of the opening (UTC), newest first."""
    checks = rules_trade_checks(rows, now_ts, dep)
    stops = rules_day_stops(rows, dep)
    weeks: dict[str, list[dict]] = {}
    for c in checks:
        weeks.setdefault(c["week"], []).append(c)
    for s in stops.values():
        weeks.setdefault(_rules_week(s["ts"]), [])
    out = []
    for wk in sorted(weeks, reverse=True):
        cs = weeks[wk]
        rules = {}
        for n in RULES_NUMS:
            st = [c["rules"][n] for c in cs if not (n == 4 and c["approx"])]  # rule 4 on the close time: apart
            v, k, u = st.count("violated"), st.count("ok") + st.count("violated"), st.count("unknown")
            rules[n] = {"status": _rules_status(v, k, u), "violated": v, "checked": k, "unknown": u}
        bad = [c for c in cs if _rules_exact_violation(c)]
        bad_approx = [c for c in cs if c["approx"] and not _rules_exact_violation(c)]
        pnls = [c["pnl"] for c in bad if c["pnl"] is not None]
        pnls_approx = [c["pnl"] for c in bad_approx if c["pnl"] is not None]
        alts = [c["alt_longs"] for c in cs if c["alt_longs"] is not None]
        out.append(
            {
                "week": wk,
                "n_trades": len(cs),
                "rules": rules,
                "days_stopped": sum(1 for s in stops.values() if _rules_week(s["ts"]) == wk),
                "approx": sum(1 for c in cs if c["approx"]),
                "max_alt_longs": max(alts) if alts else None,
                "late_buys": sum(1 for c in cs if c["late_buy"]),
                "buys_under_24h": sum(1 for c in cs if c["buy_zone"] == RULES_ZONE_BAN),
                "buys_cond_failed": sum(1 for c in cs if c["buy_zone"] == RULES_ZONE_COND and c["late_buy"]),
                "big_premiums": sum(1 for c in cs if c["big_premium"]),
                "n_violating": len(bad),
                "pnl_violating": sum(pnls) if pnls else None,
                "n_violating_approx": len(bad_approx),
                "pnl_violating_approx": sum(pnls_approx) if pnls_approx else None,
            }
        )
    return out


def _rules_exact_violation(c: dict) -> bool:
    """Copy of analytics.rules_check._exact_violation: any violation but a rule 4 judged on the close time."""
    return any(st == "violated" and not (n == 4 and c["approx"]) for n, st in c["rules"].items())


def rules_compliance(rows: list[dict], now_ts: float, dep: float = RULES_DEPOSIT) -> dict:
    """Copy of analytics.rules_check.compliance_summary: each trade is violated (exact), approx (rule 4 on the close
    time only), unknown (no violation, a rule cannot be told; not compliant) or ok; share_ok = ok / (ok + violated)."""
    n_ok = n_bad = n_unknown = n_approx = 0
    checks = rules_trade_checks(rows, now_ts, dep)
    for c in checks:
        if _rules_exact_violation(c):
            n_bad += 1
        elif c["approx"]:
            n_approx += 1
        elif "unknown" in c["rules"].values():
            n_unknown += 1
        else:
            n_ok += 1
    n_checked = n_ok + n_bad
    return {
        "n_trades": len(checks),
        "n_checked": n_checked,
        "n_ok": n_ok,
        "n_violated": n_bad,
        "n_unknown": n_unknown,
        "n_approx": n_approx,
        "share_ok": n_ok / n_checked if n_checked else None,
        "enough": n_checked >= RULES_MIN_CHECKED,
    }


def tile_rules_compliance(rows: list[dict], now: float) -> str:
    """«За правилами» tile: the owner's real trades (pass them without airdrop and *-paper rows, as to
    block_rules_week). Never raises: an odd row gives an explained tile."""
    try:
        c = rules_compliance(rows, now)
    except Exception as e:  # noqa: BLE001 - one odd row must not take the tiles down
        return _tile("За правилами", "—", _e(f"не вдалося порахувати: несподіваний формат даних ({type(e).__name__})"), help="j_rules")
    if not c["n_trades"]:
        return _tile("За правилами", "—", "немає угод з часом відкриття: перевіряти нема що", help="j_rules")
    sub = (
        f"{c['n_ok']} з {c['n_checked']} перевірених без жодного порушення"
        + (f" · порушили {c['n_violated']}" if c["n_violated"] else "")
        + f" · невідомо {c['n_unknown']} (бракує стопу, цілі чи часу відкриття — не «за правилами», поза часткою)"
        + (f" · лише наближене правило 4: {c['n_approx']} (поза часткою)" if c["n_approx"] else "")
        + f" · усього {c['n_trades']} угод. Статистика дотримання правил, не порада"
        + (". Угода без стопу стає перевіреною лише через порушення, тож частка занижена" if c["n_unknown"] else "")
    )
    return _tile(
        "За правилами",
        _share(c["share_ok"]),
        _e(sub),
        help="j_rules",
        tone="" if c["enough"] else "warn",
        badge="" if c["enough"] else "замало для висновку",
    )


def _rule_chip(status: str) -> str:
    label, cls = RULE_STATUS_UA.get(status, ("невідомо", "calm"))
    return f"<span class='chip {cls}'>{_e(label)}</span>"


def _rule_pre_text(c: dict) -> tuple[str, str]:
    """(rule name, what the check found) in Ukrainian for one rules_pre_trade row."""
    v, lim, st, why = c.get("value"), c.get("limit"), c.get("status"), c.get("detail")
    lim_s = f" ({_usd(lim)})" if lim is not None else ""
    if c.get("key") == "risk":
        name = f"Ризик ≤ {RULES_RISK_PCT:g} % депозиту{lim_s}"
        if st == "na":
            return name, "Немає опціонних ніг."
        if v is None:
            return name, "Збиток на експірацію необмежений: потрібен стоп на біржі або захисні опціони (залізний кондор)."
        if lim is None:
            return name, f"Макс. збиток {_usd(v)}; депозит не задано."
        more = f" — у {_nf(v / lim, 1)} раза більше за ліміт" if st == "violated" and lim > 0 else ""
        return name, f"Макс. збиток на експірацію {_usd(v)}{more}."
    if c.get("key") == "premium":
        name = f"Премія ≤ {RULES_PREMIUM_PCT:g} % депозиту{lim_s}"
        if st == "na":
            return name, (
                "Нічого не купуєш."
                if why == "nothing bought"
                else f"Чиста премія отримується ({_usd(-(v or 0.0))}): правило про купівлю не стосується."
            )
        if v is None or lim is None:
            return name, "Премію чи депозит не відомо."
        more = f" — у {_nf(v / lim, 1)} раза більше за ліміт" if st == "violated" and lim > 0 else ""
        return name, f"Платиш {_usd(v)}{more}."
    if c.get("key") == "thesis":
        name = "8а · Теза і ціль записані"
        if st == "ok":
            return name, "Напрям і причину (поле «Теза») та ціль записано."
        miss = {"no thesis": "тези", "no target": "цілі", "no thesis and no target": "тези й цілі"}.get(str(why), "тези чи цілі")
        return name, f"Немає {miss}: напиши напрям, причину руху (подія чи рівень) і ціль у формі вище."
    if c.get("key") == "breakeven_move":
        name = f"8б · Рух до беззбитку ≤ {RULES_BE_MAX_SIGMA:g}σ за реалізованою волою"
        if st == "unknown":
            return name, "Спот чи реалізована вола (RV30) невідомі."
        if v is None:
            return name, "Беззбитку немає: на експірацію структура прибутку не дає."
        return name, f"До беззбитку {_nf(v * 100, 1)} % від форварду; 1σ за RV30 до експірації — {_nf((lim or 0.0) * 100, 1)} %."
    if c.get("key") == "iv_vs_rv":
        name = f"8в · IV ≤ {RULES_IV_MAX_RV:g} × реалізованої"
        if st == "unknown":
            return name, "IV опціону чи RV30 невідомі."
        rv = (lim or 0.0) / RULES_IV_MAX_RV
        more = " Той самий погляд дешевше — спредом." if st == "violated" else ""
        return name, f"IV куплених ніг до {_vp(v)}; межа {_vp(lim)} ({RULES_IV_MAX_RV:g} × RV30 {_vp(rv)}).{more}"
    name = "Час до експірації: < 24 год — ні; 1–3 доби — з умовами"
    if st == "na":
        return name, "Нічого не купуєш." if why == "nothing bought" else "Чиста премія отримується: це продаж, не купівля."
    if v is None:
        return name, "Дату експірації не розпізнано."
    zone = c.get("zone")
    left = _rule_time_left(v, zone)
    if zone == RULES_ZONE_BAN:
        return name, f"До експірації {left}: менше доби — не купувати ніколи."
    if zone == RULES_ZONE_COND:
        return name, f"До експірації {left}: 1–3 доби — лише коли виконано всі три умови нижче."
    return name, f"До експірації {left}. Понад 3 доби — лише ліміт премії."


def _rule_time_left(days: float, zone: object) -> str:
    """Time to expiry for rule 8's row, rounded so it never contradicts the zone: hours to 0.1 h, rounded down under
    24 h and in the 24-72 h zone (23.96 h is '23.9 год', not '24'), up just above 72 h (72.04 h is '72.1 год');
    days (to 0.1) from 96 h on, where rounding cannot cross a boundary."""
    h = days * 24.0
    if zone == RULES_ZONE_FREE and h >= 96.0:
        return f"{_nf(days, 1)} дн"
    t = h * 10.0  # tenths of an hour; the clamps absorb float noise from days * 24
    ban, cond = round(RULES_BUY_BAN_HOURS * 10), round(RULES_BUY_COND_HOURS * 10)
    if zone == RULES_ZONE_BAN:
        x = min(math.floor(t + 1e-6), ban - 1)
    elif zone == RULES_ZONE_COND:
        x = min(max(math.floor(t + 1e-6), ban), cond)
    else:
        x = max(math.ceil(t - 1e-6), cond + 1)
    return f"{_nf(x / 10.0, 1).removesuffix('.0')} год"


def block_rules_pre(
    legs: list[dict],
    ch: dict,
    dep: float,
    now: float | None,
    rv: float | None = None,
    thesis: str | None = None,
    target: float | None = None,
) -> str:
    """Rules 1 and 8 on the constructed structure (copy of analytics.rules_check.pre_trade_option); ``rv`` is the
    snapshot's RV30 (decimal), the forward stands in for spot (days before expiry they are close)."""
    exp = expiry_utc(ch.get("expiry"))
    exp_ts = exp.timestamp() if exp else None
    now_ts = builder_now_ts(ch, now)
    rows = ["<div class='scroll'><table><tr><th>№</th><th>Правило</th><th>Стан</th><th>Що з цією угодою</th></tr>"]
    for c in rules_pre_trade(legs, now_ts, exp_ts, dep, ch.get("F"), rv, thesis, target):
        name, text = _rule_pre_text(c)
        rows.append(
            f"<tr><td class='num'>{_e(c['rule'])}</td><td>{_e(name)}</td><td>{_rule_chip(str(c['status']))}</td><td>{_e(text)}</td></tr>"
        )
    rows.append("</table></div>")
    note = (
        f"Правила власника 1 і 8 (затверджено 18.10, правило 8 уточнено 22.10; база знань «Торгова стратегія»). Ліміти — "
        f"{RULES_RISK_PCT:g} % від депозиту у формі, незалежно від поля «Ризик на угоду». За 1–3 доби до експірації "
        "купівля — лише з тезою й ціллю, беззбитком у межах 1σ руху за реалізованою волою (RV30) і IV ≤ 1.2 × RV30; "
        "пороги 1σ і 1.2 — стартовий вибір, а не доведена перевага. Решту правил (стоп на біржі, денний стоп, R:R ≥ 2, "
        "не більше 3 лонгів в альтах) перевіряє чекліст перед угодою (pre_trade_checklist); тижневий звіт порушень — "
        "вкладка «Журнал»."
    )
    return f"<section>{_h2('Правила власника перед угодою', 'rules_pre')}{''.join(rows)}<p class='note'>{_e(note)}</p></section>"


def _rule_cell(w: dict, n: int) -> str:
    r = w["rules"][n]
    st, v, k, u = r["status"], r["violated"], r["checked"], r["unknown"]
    if st == "na":
        main = "<span class='mut'>—</span>"
    elif st == "unknown":
        main = f"<span class='mut'>невідомо ({u})</span>"
    else:
        main = f"<span class='{'bad' if v else 'ok'}'>{v} з {k}</span>" + (f" <span class='mut'>· невідомо {u}</span>" if u else "")
    extra = []
    if n == 2 and w.get("max_alt_longs") is not None:
        extra.append(f"макс. одночасно {w['max_alt_longs']}")
    if n == 4:
        if w.get("days_stopped"):
            extra.append(f"стоп-днів {w['days_stopped']}")
        if w.get("approx"):
            extra.append(f"+ наближено {w['approx']} (за часом закриття)")
    if n == 8 and (w.get("late_buys") or w.get("big_premiums")):
        extra.append(
            f"< 24 год {w.get('buys_under_24h') or 0} · 1–3 доби без умов {w.get('buys_cond_failed') or 0} · "
            f"дорогих {w.get('big_premiums') or 0}"
        )
    return main + "".join(f"<br><span class='mut'>{_e(x)}</span>" for x in extra)


def block_rules_week(rows: list[dict] | None, now: float) -> str:
    """«Порушення правил за тиждень»: the owner's real trades (pass them without airdrop and *-paper rows)."""
    if rows is None:
        return _empty("Таблиці journal у базі ще немає: перевіряти нема що.")
    if not rows:
        return _empty("Угод ще немає: звіт порушень з'явиться з першою угодою.")
    weeks = rules_weekly(rows, now)
    if not weeks:
        return _empty("Відкритих чи закритих угод з часом відкриття немає: перевіряти нема що.")
    head = "".join(f"<th>{_e(t)}</th>" for _, t in RULES_WEEK_HEAD)
    out = [f"<div class='scroll'><table><tr><th>Тиждень</th><th>Угод</th>{head}<th>Порушили</th><th>P&amp;L цих угод</th></tr>"]
    for w in weeks[:RULES_WEEKS]:
        pnl, n_ax, pnl_ax = w["pnl_violating"], w["n_violating_approx"], w["pnl_violating_approx"]
        cells = "".join(f"<td class='num'>{_rule_cell(w, n)}</td>" for n, _ in RULES_WEEK_HEAD)
        ax_n = f"<br><span class='mut'>+ наближено {n_ax}</span>" if n_ax else ""
        ax_pnl = f"<br><span class='mut'>наближені {_usd(pnl_ax, True)}</span>" if n_ax and pnl_ax is not None else ""
        out.append(
            f"<tr><td class='num'>{_e(w['week'])}</td><td class='num'>{w['n_trades']}</td>{cells}"
            f"<td class='num'>{w['n_violating']}{ax_n}</td><td class='num {_signed_cls(pnl)}'>{_usd(pnl, True)}{ax_pnl}</td></tr>"
        )
    out.append("</table></div>")
    n_bad = sum(w["n_violating"] for w in weeks)
    n_ax = sum(w["n_violating_approx"] for w in weeks)
    n_all = sum(w["n_trades"] for w in weeks)
    pnls = [w["pnl_violating"] for w in weeks if w["pnl_violating"] is not None]
    pnls_ax = [w["pnl_violating_approx"] for w in weeks if w["pnl_violating_approx"] is not None]
    total = (
        f"За весь журнал: хоч одне правило точно порушили {n_bad} угод з {n_all}"
        + (f", їхній P&L разом {_usd(sum(pnls), True)}" if pnls else "")
        + "."
        + (
            f" Ще {n_ax} угод — лише наближене порушення правила 4 (немає часу відкриття, оцінено за часом закриття: "
            "угода могла відкритися задовго до стопу)"
            + (f", їхній P&L {_usd(sum(pnls_ax), True)}" if pnls_ax else "")
            + "; у порівнянні з чистими угодами їх не враховано."
            if n_ax
            else ""
        )
        + (f" Показано {RULES_WEEKS} останніх тижнів з {len(weeks)}." if len(weeks) > RULES_WEEKS else "")
    )
    out.append(f"<p class='note'>{_e(total)}</p><p class='note'>{_e(RULES_WEEK_NOTE)}</p>")
    return "".join(out)


# ---------------------------------------------------------------- «Журнал» tab: the owner's journal and the paper recommendations
#
# Read-only SQLite (mode=ro URI), no network. Summaries are stdlib copies of analytics.journal.summary,
# analytics.rec_paper.summarize and analytics.trade_plan._stats (tests compare each pair): the service runs on the
# system python3 -I, without the project's environment. Every string from the database (symbol, thesis, tags, note,
# structure) is untrusted text and is escaped.

JOURNAL_DB = DATA_DIR / "tradedesk.sqlite"
JOURNAL_PAGE_ROWS = 50
JOURNAL_MAX_ROWS = 20000  # the whole journal is read for the summary (≈ 400 rows today)
REC_MAX_ROWS = 2000
REC_TABLE_ROWS = 20
JOURNAL_VENUES = {"bybit": "Bybit", "binance": "Binance", "hyperliquid": "Hyperliquid", "okx": "OKX", "deribit": "Deribit"}
JOURNAL_PLAIN_TAGS = {"import", "linear", "usdm", "perp", "option"}  # import bookkeeping, not worth a column
SIDE_UA = {"long": "лонг", "short": "шорт"}
REC_TRIGGER_UA = {"chain": "ланцюг (як у тесті H1)", "regime": "зміна режиму", "event": "подія"}
JOURNAL_CAVEATS = (
    "Реальні угоди — з імпорту бірж і з journal_add; P&L у USD — як його записав імпорт (дані біржі), без перерахунку.",
    "Рядки з тегом airdrop і паперові carry-угоди (теги *-paper) до підсумку реальних угод не входять: їх показано окремо.",
    "Минулий результат не гарантує майбутнього. Не інвестиційна порада: сторінка лише читає базу, нічого не торгує.",
)
REC_CAVEAT = (
    "Гіпотеза, не доказ переваги: у перевірці кванта 18.10 угоди, обрані методом, дали −0.59 % спота за угоду "
    "(95 % CI [−2.43; +1.51], n = 27); великий ефект виключено, перевагу ≈ +0.2 R — ні. Паперовий журнал це перевіряє. "
    "Угоди паперові: ціни входу — марки Deribit ± 1 вп, розрахунок на delivery."
)
PLAN_MAX_ROWS = 20000
PLAN_SHADOW_TAG = "src:demo-shadow"  # tools/trade_plan.py SHADOW_TAG: the demo trader's shadow plans, kept out of the summary
PLAN_MIN_N = 30  # BACKLOG: the first honest cut of the 'enter' plans after ~30 of them
PLAN_OUTCOMES = ("target", "stop", "expired", "not_filled")  # analytics.trade_plan.OUTCOMES
PLAN_VERDICT_UA = {"enter": "enter (✅ входити)", "wait": "wait (⏳ чекати)", "skip": "skip (❌ пропустити)"}
PLAN_CAVEAT = (
    "Облік планів асистента, не реальні угоди: паперова оцінка на свічках (high / low), без гарантії виконання. "
    "R — у одиницях запланованого ризику |вхід − стоп|, за вирахуванням тейкер-комісії 0.05 % на бік; без іншого "
    "проковзування й funding; свічка, що торкнулась і стопу, і цілі, — стоп. Опціонні плани з rec_paper оцінено "
    "паперовою угодою (r_worst_case). Тіньові плани демо-трейдера (src:demo-shadow) до підсумку не входять, як у plan_review. "
    "Це запис судження асистента, не доказ переваги."
)
# Two-sided 97.5 % Student-t quantiles by degrees of freedom (1..30); normal beyond (analytics.smart_money.t975).
_T975 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip


def journal_tags(raw: object) -> list[str]:
    """Tags of a journal row (a JSON array of strings); anything else gives []."""
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        return []
    return [str(x) for x in v if isinstance(x, (str, int, float)) and not isinstance(x, bool)][:30] if isinstance(v, list) else []


def journal_kind(tags: list[str]) -> tuple[str, str]:
    """('real' | 'paper' | 'airdrop', source label). Airdrop and *-paper rows are kept out of the real-trade stats,
    as scripts/bybit_import.py and scripts/journal_rules.py do."""
    paper = next((t for t in tags if t.endswith("-paper")), None)
    if paper:
        return "paper", paper
    venue = next((JOURNAL_VENUES[t] for t in tags if t in JOURNAL_VENUES), None)
    label = (venue + (" опціони" if "option" in tags else "")) if venue else "без біржі"
    return ("airdrop" if "airdrop" in tags else "real"), label


def journal_summary(rows: list[dict]) -> dict:
    """Stdlib copy of analytics.journal.summary (the fields the tab shows): closed trades with a numeric pnl."""
    closed = [t for t in rows if t.get("status") == "closed" and _num(t.get("pnl")) is not None]
    n = len(closed)
    if n == 0:
        return {"closed_trades": 0}
    pnls = [float(t["pnl"]) for t in closed]
    rs = [float(t["r_multiple"]) for t in closed if _num(t.get("r_multiple")) is not None]
    return {
        "closed_trades": n,
        "wins": sum(1 for t in closed if (t.get("outcome") or "") == "win"),
        "losses": sum(1 for t in closed if (t.get("outcome") or "") == "loss"),
        "win_rate": sum(1 for t in closed if (t.get("outcome") or "") == "win") / n,
        "total_pnl": sum(pnls),
        "expectancy_per_trade": sum(pnls) / n,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "r_count": len(rs),
    }


def max_drawdown_usd(rows: list[dict]) -> float:
    """Largest peak-to-trough fall (USD, >= 0) of the cumulative P&L of closed trades in closing order, from 0."""
    closed = [t for t in rows if t.get("status") == "closed" and _num(t.get("pnl")) is not None]
    closed.sort(key=lambda t: (_num(t.get("closed_ts")) or _num(t.get("opened_ts")) or 0.0, _num(t.get("id")) or 0.0))
    cum = peak = worst = 0.0
    for t in closed:
        cum += float(t["pnl"])
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


# The «Крива в R і тижневий розбір» block: stdlib copies of analytics.journal week_start / _closed_rows / week_stats
# (the fields shown; tests compare the pairs) over the owner's real trades, as scripts/journal_weekly_tg.py picks them.
JOURNAL_BOT_TAGS = frozenset({"auto", "demo-paper", "bybit-demo"})  # scripts/journal_close_tg.BOT_TAGS: the demo trader
JR_SMALL_SAMPLE = 30  # analytics.journal.SMALL_SAMPLE: fewer is a description, not a conclusion
JR_WEEK_S = 7 * 86400.0
JR_WEEKS = 12  # newest ISO weeks in the table
JR_DOTS_MAX = 80  # a dot per trade on the R curve up to this many trades
JR_SYMBOL_SHOWN = 24  # characters of a symbol in the weekly table


def journal_owner_trades(rows: list[dict]) -> list[dict]:
    """The owner's real trades: journal_kind 'real' (no airdrop, no *-paper) and no demo-trader row (JOURNAL_BOT_TAGS),
    the same rows as scripts/journal_weekly_tg.real_trades."""
    out = []
    for t in rows:
        tags = journal_tags(t.get("tags"))
        if journal_kind(tags)[0] == "real" and not JOURNAL_BOT_TAGS.intersection(tags):
            out.append(t)
    return out


def jr_week_start(ts: float) -> float:
    """Monday 00:00 UTC (unix s) of the ISO week holding ``ts`` (copy of analytics.journal.week_start)."""
    d = datetime.fromtimestamp(ts, timezone.utc)  # noqa: UP017 - runs on the system python3
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def jr_closed_rows(rows: list[dict]) -> list[dict]:
    """Copy of analytics.journal._closed_rows: closed rows with a finite P&L and a close time (the opening time when a
    closed row has none), as copies with ``ts``, a numeric ``pnl``, ``r_multiple`` only when finite and ``outcome``
    filled from the P&L when missing."""
    out = []
    for t in rows:
        pnl = _num(t.get("pnl"))
        ts = _num(t.get("closed_ts"))
        ts = _num(t.get("opened_ts")) if ts is None else ts
        if t.get("status") != "closed" or pnl is None or ts is None:
            continue
        outcome = t.get("outcome") if t.get("outcome") in ("win", "loss", "break_even") else None
        if outcome is None:
            outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "break_even"
        out.append({**t, "ts": ts, "pnl": pnl, "r_multiple": _num(t.get("r_multiple")), "outcome": outcome})
    return out


def jr_week_stats(rows: list[dict], start_ts: float, end_ts: float) -> dict:
    """Copy of analytics.journal.week_stats (without by_source): the summary of the trades closed in [start_ts, end_ts),
    best / worst by P&L and small_sample (fewer than JR_SMALL_SAMPLE)."""
    return _jr_bucket_stats([x for x in jr_closed_rows(rows) if start_ts <= x["ts"] < end_ts], start_ts, end_ts)


def _jr_bucket_stats(closed: list[dict], start_ts: float, end_ts: float) -> dict:
    """jr_week_stats over ``closed``: jr_closed_rows output already limited to [start_ts, end_ts)."""
    xs = sorted(closed, key=lambda x: (x["ts"], str(x.get("id"))))
    s = journal_summary(xs)
    return {
        **s,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "best": max(xs, key=lambda x: x["pnl"]) if xs else None,
        "worst": min(xs, key=lambda x: x["pnl"]) if xs else None,
        "small_sample": s["closed_trades"] < JR_SMALL_SAMPLE,
    }


def jr_week_buckets(rows: list[dict]) -> list[tuple[float, list[dict]]]:
    """The closed trades (jr_closed_rows) grouped by ISO week (UTC) of the close in one pass: [(week start, rows)],
    newest first; every bucket is non-empty and its length is the week's closed_trades."""
    groups: dict[float, list[dict]] = {}
    for x in jr_closed_rows(rows):
        groups.setdefault(jr_week_start(x["ts"]), []).append(x)
    return sorted(groups.items(), key=lambda kv: kv[0], reverse=True)


def jr_weeks(rows: list[dict]) -> list[dict]:
    """jr_week_stats of every ISO week (UTC) with a closed trade, newest first."""
    return [_jr_bucket_stats(xs, s, s + JR_WEEK_S) for s, xs in jr_week_buckets(rows)]


def jr_r_curve(rows: list[dict]) -> list[dict]:
    """Cumulative R of the closed trades with a recorded R (only rows with a stop have one), in closing order:
    [{'id', 'ts', 'r', 'cum'}]."""
    xs = sorted((x for x in jr_closed_rows(rows) if x["r_multiple"] is not None), key=lambda x: (x["ts"], _num(x.get("id")) or 0.0))
    out, cum = [], 0.0
    for x in xs:
        cum += x["r_multiple"]
        out.append({"id": x.get("id"), "ts": x["ts"], "r": x["r_multiple"], "cum": cum})
    return out


def jr_r_drawdown(curve: list[dict]) -> float:
    """Largest peak-to-trough fall of the cumulative R curve (R, >= 0), starting from 0."""
    peak = worst = 0.0
    for c in curve:
        peak = max(peak, c["cum"])
        worst = max(worst, peak - c["cum"])
    return worst


def rec_summary(rows: list[dict]) -> dict:
    """Stdlib copy of analytics.rec_paper.summarize over the settled rows with numeric P&L."""
    settled = [
        t for t in rows if t.get("status") == "settled" and _num(t.get("pnl_usd")) is not None and _num(t.get("pnl_pct_spot")) is not None
    ]
    n = len(settled)
    if n == 0:
        return {"n": 0}
    pct = [float(t["pnl_pct_spot"]) for t in settled]
    rw = [float(t["r_worst_case"]) for t in settled if _num(t.get("r_worst_case")) is not None]
    rs = [float(t["r_study"]) for t in settled if _num(t.get("r_study")) is not None]
    return {
        "n": n,
        "win_share": sum(1 for t in settled if float(t["pnl_usd"]) > 0) / n,
        "mean_pnl_pct_spot": sum(pct) / n,
        "mean_r_worst_case": sum(rw) / len(rw) if rw else None,
        "mean_r_study": sum(rs) / len(rs) if rs else None,
        "n_r_study": len(rs),
        "sum_pnl_usd": sum(float(t["pnl_usd"]) for t in settled),
    }


def t975(df: int) -> float:
    """Student-t 97.5 % quantile (two-sided 95 % CI); 1.96 for df > 30. Copy of analytics.smart_money.t975."""
    if df < 1:
        return float("inf")
    return _T975[df - 1] if df <= 30 else 1.96


def plan_stats(rows: list[dict]) -> dict:
    """Stdlib copy of analytics.trade_plan._stats: n plans, fills, outcome counts, hit rate (targets / filled), win rate
    (R > 0) and mean / median R with a t 95 % CI on the mean. A non-numeric r_result is skipped instead of raising."""
    filled = [x for x in rows if x.get("outcome") in ("target", "stop", "expired")]
    rs = [v for x in filled if (v := _num(x.get("r_result"))) is not None]
    s: dict = {
        "n": len(rows),
        "n_filled": len(filled),
        "outcomes": {k: sum(1 for x in rows if x.get("outcome") == k) for k in PLAN_OUTCOMES},
        "hit_rate": sum(1 for x in filled if x["outcome"] == "target") / len(filled) if filled else None,
        "win_rate": sum(1 for v in rs if v > 0) / len(rs) if rs else None,
        "mean_r": statistics.fmean(rs) if rs else None,
        "median_r": statistics.median(rs) if rs else None,
        "ci95_mean_r": None,
    }
    if len(rs) >= 2:
        h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
        s["ci95_mean_r"] = [s["mean_r"] - h, s["mean_r"] + h]
    s["n_r"] = len(rs)
    return s


# The forecast journal (table forecast_log, schema v20): stdlib copies of analytics.forecast_log calibration / score /
# iso_week / weekly (tests compare each pair). Risk-neutral probabilities from the Deribit smile, settled at delivery.
FC_MAX_ROWS = 20000  # 8 forecasts a day ≈ 2 900 a year
FC_N_BINS = 10  # analytics.forecast_log.N_BINS
FC_WEEKS = 12  # newest ISO weeks in the weekly table (as forecast_log_status's default)
FC_MIN_EXPIRIES = 30  # below this many settled (asset, expiry) outcomes the block says: no conclusion
# analytics.forecast_log.T975: Student t 0.975 quantiles for 1..30 degrees of freedom (above: 1.96)
FC_T975 = (
    12.706,
    4.303,
    3.182,
    2.776,
    2.571,
    2.447,
    2.365,
    2.306,
    2.262,
    2.228,
    2.201,
    2.179,
    2.160,
    2.145,
    2.131,
    2.120,
    2.110,
    2.101,
    2.093,
    2.086,
    2.080,
    2.074,
    2.069,
    2.064,
    2.060,
    2.056,
    2.052,
    2.048,
    2.045,
    2.042,
)
FC_KINDS = ("above_spot", "in_1sigma", "in_pct_5", "in_pct_10")  # analytics.forecast_log.KINDS
FC_KIND_UA = {"above_spot": "вище спота", "in_1sigma": "±1σ", "in_pct_5": "±5 %", "in_pct_10": "±10 %"}
FC_CAVEAT = (
    "Це ймовірності, які закладають опціони Deribit (усмішка експірації ≈ 7 днів), а не реальні шанси і не порада. "
    "Премія за волатильність сама по собі має робити діапазони влучними частіше, ніж оцінено: журнал це перевіряє "
    "(гіпотеза, перевагу не показано). Прогнози різних днів на одну експірацію мають спільний результат і корельовані: "
    "розмір вибірки — кількість експірацій (n_expiries), а не прогнозів; мала n — висновку немає. Кліматологія — частота "
    "подій у цій самій вибірці, відома лише заднім числом."
)


def _fc_settled(rows: list[dict]) -> list[dict]:
    return [x for x in rows if x.get("outcome") is not None and x.get("prob") is not None]


def _fc_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _fc_brier(p: float, o: int) -> float:
    return (float(p) - float(o)) ** 2


def fc_calibration(rows: list[dict], n_bins: int = FC_N_BINS) -> list[dict]:
    """Stdlib copy of analytics.forecast_log.calibration: settled forecasts in equal probability bins (the last one
    includes 1.0), non-empty bins only, with n, mean forecast and observed frequency."""
    bins: dict[int, list[dict]] = {}
    for x in _fc_settled(rows):
        bins.setdefault(min(int(float(x["prob"]) * n_bins), n_bins - 1), []).append(x)
    return [
        {
            "lo": i / n_bins,
            "hi": (i + 1) / n_bins,
            "n": len(xs),
            "mean_prob": _fc_mean([float(x["prob"]) for x in xs]),
            "freq": _fc_mean([float(x["outcome"]) for x in xs]),
        }
        for i, xs in sorted(bins.items())
    ]


def fc_score(rows: list[dict], n_bins: int = FC_N_BINS) -> dict:
    """Stdlib copy of analytics.forecast_log.score: Brier, the fixed and the in-sample climatology references, skill,
    calibration and the binned Murphy decomposition of the settled rows."""
    xs = _fc_settled(rows)
    n = len(xs)
    if n == 0:
        return {"n": 0}
    ps = [float(x["prob"]) for x in xs]
    os_ = [int(x["outcome"]) for x in xs]
    bs = _fc_mean([_fc_brier(p, o) for p, o in zip(ps, os_, strict=True)])
    by_kind: dict[str, list[int]] = {}
    for x, o in zip(xs, os_, strict=True):
        by_kind.setdefault(str(x["kind"]), []).append(o)
    base = {k: _fc_mean(v) for k, v in by_kind.items()}
    bs_clim = _fc_mean([_fc_brier(base[str(x["kind"])], o) for x, o in zip(xs, os_, strict=True)])
    ref = [(p, float(x["ref_prob"]), o) for x, p, o in zip(xs, ps, os_, strict=True) if x.get("ref_prob") is not None]
    bins = fc_calibration(xs, n_bins)
    obar = _fc_mean([float(o) for o in os_])
    out: dict = {
        "n": n,
        "n_expiries": len({(x.get("asset"), x.get("expiry_ts")) for x in xs}),
        "mean_prob": _fc_mean(ps),
        "base_rate": obar,
        "brier": bs,
        "brier_climatology": bs_clim,
        "skill_vs_climatology": 1.0 - bs / bs_clim if bs_clim > 0 else None,
        "n_ref": len(ref),
        "brier_on_ref": _fc_mean([_fc_brier(p, o) for p, _, o in ref]) if ref else None,
        "brier_ref": _fc_mean([_fc_brier(q, o) for _, q, o in ref]) if ref else None,
        "skill_vs_ref": None,
        "reliability": sum(b["n"] * (b["mean_prob"] - b["freq"]) ** 2 for b in bins) / n,
        "resolution": sum(b["n"] * (b["freq"] - obar) ** 2 for b in bins) / n,
        "uncertainty": obar * (1.0 - obar),
        "calibration": bins,
    }
    if ref and out["brier_ref"] > 0:
        out["skill_vs_ref"] = 1.0 - out["brier_on_ref"] / out["brier_ref"]
    return out


def fc_compare(rows: list[dict]) -> dict:
    """Stdlib copy of analytics.forecast_log.compare: paired Brier of the risk-neutral forecast against the RV30 model
    on the settled rows that have both, clustered by expiry date (BTC and ETH of one date are one unit); diff < 0 means
    the options forecast better."""
    xs = [x for x in _fc_settled(rows) if _num(x.get("prob_rv")) is not None]
    if not xs:
        return {"n": 0, "n_dates": 0}
    clusters: dict = {}
    for x in xs:
        o = int(x["outcome"])
        clusters.setdefault(x.get("expiry_ts"), []).append(_fc_brier(float(x["prob"]), o) - _fc_brier(float(x["prob_rv"]), o))
    means = [_fc_mean(v) for v in clusters.values()]
    k = len(means)
    diff = _fc_mean(means)
    se = math.sqrt(sum((m - diff) ** 2 for m in means) / (k - 1) / k) if k >= 2 else None
    t = FC_T975[k - 2] if 2 <= k <= len(FC_T975) + 1 else 1.96
    return {
        "n": len(xs),
        "n_dates": k,
        "brier_risk_neutral": _fc_mean([_fc_brier(float(x["prob"]), int(x["outcome"])) for x in xs]),
        "brier_rv30": _fc_mean([_fc_brier(float(x["prob_rv"]), int(x["outcome"])) for x in xs]),
        "diff": diff,
        "se": se,
        "ci95": None if se is None else [diff - t * se, diff + t * se],
        "better": "risk_neutral" if diff < 0 else "rv30" if diff > 0 else "tie",
        "enough": k >= FC_MIN_EXPIRIES,
    }


def fc_iso_week(ts: object) -> str:
    """ISO week of a unix timestamp (UTC), e.g. '2026-W43' (copy of analytics.forecast_log.iso_week)."""
    y, w, _ = datetime.fromtimestamp(int(ts), timezone.utc).isocalendar()  # noqa: UP017 - runs on the system python3
    return f"{y}-W{w:02d}"


def fc_weekly(rows: list[dict]) -> list[dict]:
    """Stdlib copy of analytics.forecast_log.weekly: settled forecasts by the ISO week of their expiry, oldest first."""
    weeks: dict[str, list[dict]] = {}
    for x in _fc_settled(rows):
        weeks.setdefault(fc_iso_week(x["expiry_ts"]), []).append(x)
    out = []
    for wk, xs in sorted(weeks.items()):
        s = fc_score(xs)
        kinds: dict[str, list[float]] = {}
        for x in xs:
            kinds.setdefault(str(x["kind"]), []).append(_fc_brier(float(x["prob"]), int(x["outcome"])))
        out.append(
            {
                "week": wk,
                "n": s["n"],
                "n_expiries": s["n_expiries"],
                "brier": s["brier"],
                "n_ref": s["n_ref"],
                "brier_on_ref": s["brier_on_ref"],
                "brier_ref": s["brier_ref"],
                "skill_vs_ref": s["skill_vs_ref"],
                "brier_by_kind": {k: _fc_mean(v) for k, v in sorted(kinds.items())},
            }
        )
    return out


def load_journal(path: Path) -> tuple[dict | None, str | None]:
    """{'journal': rows | None, 'rec_paper': rows | None, 'trade_plan': rows | None, 'plan_shadow': int,
    'forecast_log': rows | None} (None = no such table; trade_plan without the demo trader's shadow plans, which are
    only counted), or (None, reason) with reason 'missing' | 'unreadable: …'. Read-only connection; never raises."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:  # OSError: e.g. PermissionError from exists() on a closed directory
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out: dict = {"journal": None, "rec_paper": None, "trade_plan": None, "plan_shadow": 0, "forecast_log": None}
        if "journal" in names:
            out["journal"] = [
                dict(r)
                for r in con.execute(
                    "SELECT * FROM journal ORDER BY COALESCE(closed_ts, opened_ts) DESC, id DESC LIMIT ?", (JOURNAL_MAX_ROWS,)
                )
            ]
        if "rec_paper" in names:
            out["rec_paper"] = [
                dict(r) for r in con.execute("SELECT * FROM rec_paper ORDER BY opened_ts DESC, id DESC LIMIT ?", (REC_MAX_ROWS,))
            ]
        if "trade_plan" in names:
            shadow = f'%"{PLAN_SHADOW_TAG}"%'  # the same JSON-array match as Store.plan_list(exclude_tag=...)
            out["trade_plan"] = [
                dict(r)
                for r in con.execute(
                    "SELECT id, created_ts, verdict, status, outcome, r_result FROM trade_plan WHERE tags NOT LIKE ?"
                    " ORDER BY created_ts DESC, id DESC LIMIT ?",
                    (shadow, PLAN_MAX_ROWS),
                )
            ]
            out["plan_shadow"] = int(con.execute("SELECT COUNT(*) FROM trade_plan WHERE tags LIKE ?", (shadow,)).fetchone()[0])
        if "forecast_log" in names:
            # prob_rv (the RV30 model) arrives with schema v23; an older store reads it as NULL
            cols = {r[1] for r in con.execute("PRAGMA table_info(forecast_log)")}
            rv = "prob_rv" if "prob_rv" in cols else "NULL AS prob_rv"
            out["forecast_log"] = [
                dict(r)
                for r in con.execute(
                    f"SELECT asset, kind, expiry_ts, prob, ref_prob, {rv}, status, outcome FROM forecast_log"
                    " ORDER BY issued_ts DESC, id DESC LIMIT ?",
                    (FC_MAX_ROWS,),
                )
            ]
        return out, None
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def parse_journal_query(query: str) -> dict:
    """Page number from the URL query (untrusted): anything odd gives page 1."""
    try:
        v = urllib.parse.parse_qs(query or "", max_num_fields=20).get("page", ["1"])[0][:6]
    except ValueError:
        v = "1"
    return {"page": int(v) if v.isascii() and v.isdigit() and 1 <= int(v) <= 100000 else 1}


def _day(ts: object) -> str:
    v = _num(ts)
    if v is None or v <= 0:
        return "—"
    try:
        return kyiv(datetime.fromtimestamp(v / 1000 if v > 1e12 else v, timezone.utc), "%d.%m.%Y")  # noqa: UP017
    except (OverflowError, OSError, ValueError):
        return "—"


def _signed_cls(x: float | None) -> str:
    return "" if x is None or abs(x) < 0.005 else "ok" if x > 0 else "bad"


def _share(x: object) -> str:
    v = _num(x)
    return "—" if v is None else f"{100 * v:.0f} %"


def _px(x: object) -> str:
    """A price with enough decimals for cheap coins (0.0123), two for the rest."""
    v = _num(x)
    if v is None:
        return "—"
    d = 2 if abs(v) >= 10 else 4 if abs(v) >= 0.01 or v == 0 else 8
    return _nf(v, d)


def _r(x: object) -> str:
    v = _num(x)
    return "—" if v is None else f"{v:+.2f} R".replace("-", "−")


def block_journal_tiles(real: list[dict], n_open: int, now: float) -> str:
    s = journal_summary(real)
    rules = tile_rules_compliance(real, now)
    if not s["closed_trades"]:
        return (
            _empty("Закритих реальних угод ще немає.")
            + (f"<p class='note'>Відкритих: {n_open}.</p>" if n_open else "")
            + (f"<div class='tiles'>{rules}</div>" if real else "")
        )
    total, exp = s["total_pnl"], s["expectancy_per_trade"]
    tiles = [
        _tile("Угод (закритих)", _nf(s["closed_trades"]), f"у плюсі {s['wins']} · у мінусі {s['losses']}", help="j_trades"),
        _tile("Частка в плюсі", _share(s["win_rate"]), "за полем outcome", help="j_winrate"),
        _tile(
            "P&L разом", f"<span class='{_signed_cls(total)}'>{_usd(total, True)}</span>", "USD, усі біржі й опціони разом", help="j_pnl"
        ),
        _tile(
            "Середнє на угоду",
            f"<span class='{_signed_cls(exp)}'>{_usd(exp, True)}</span>",
            "USD, усі джерела разом",
            help="j_expectancy",
            tone="bad" if exp is not None and exp < 0 else "",
            badge="система поки в мінусі" if exp is not None and exp < 0 else "",
        ),
        _tile("Середній R", _r(s["avg_r"]), f"угод зі стопом {s['r_count']}" if s["r_count"] else "стопи не записані", help="r"),
        _tile(
            "Найбільша просадка",
            _usd(-max_drawdown_usd(real)),
            "USD, одна крива P&L усіх бірж і опціонів за датою закриття",
            help="drawdown",
        ),
        rules,
    ]
    if n_open:
        tiles.append(_tile("Відкритих", _nf(n_open), help="j_open"))
    return f"<div class='tiles'>{''.join(tiles)}</div>"


def block_journal_sources(groups: dict[str, list[dict]]) -> str:
    rows = []
    for label, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = journal_summary(items)
        if not s["closed_trades"]:
            continue
        rows.append(
            f"<tr><td>{_e(label)}</td><td class='num'>{s['closed_trades']}</td><td class='num'>{_share(s['win_rate'])}</td>"
            f"<td class='num {_signed_cls(s['total_pnl'])}'>{_usd(s['total_pnl'], True)}</td>"
            f"<td class='num {_signed_cls(s['expectancy_per_trade'])}'>{_usd(s['expectancy_per_trade'], True)}</td></tr>"
        )
    if not rows:
        return _empty("Закритих угод немає.")
    return (
        "<div class='scroll'><table><tr><th>Джерело</th><th>Угод</th><th>У плюсі</th><th>P&amp;L</th><th>Середнє</th></tr>"
        + "".join(rows)
        + "</table></div>"
    )


def block_journal_table(real: list[dict], page: int) -> str:
    if not real:
        return _empty("Угод ще немає.")
    pages = max(1, math.ceil(len(real) / JOURNAL_PAGE_ROWS))
    page = min(page, pages)
    chunk = real[(page - 1) * JOURNAL_PAGE_ROWS : page * JOURNAL_PAGE_ROWS]
    out = [
        "<div class='scroll'><table><tr><th>Закрито</th><th>Джерело</th><th>Інструмент</th><th>Сторона</th><th>Розмір</th>"
        "<th>Вхід → вихід</th><th>P&amp;L</th><th>R</th><th>Теза · теги · нотатка</th></tr>"
    ]
    for t in chunk:
        tags = journal_tags(t.get("tags"))
        _, src = journal_kind(tags)
        pnl = _num(t.get("pnl"))
        when = _day(t.get("closed_ts")) if t.get("status") == "closed" else f"відкрита з {_day(t.get('opened_ts'))}"
        if t.get("status") == "cancelled":
            when = "скасована"
        extra = [x for x in tags if x not in JOURNAL_PLAIN_TAGS and x not in JOURNAL_VENUES]
        text = _e(_clean(t.get("thesis") or "", 120))
        if extra:
            text += f" <span class='chip calm'>{_e(', '.join(_clean(x, 30) for x in extra[:6]))}</span>"
        if t.get("note"):
            text += f"<br><span class='mut'>{_e(_clean(t['note'], 80))}</span>"
        side = str(t.get("side") or "")
        out.append(
            f"<tr><td class='num'>{_e(when)}</td><td>{_e(src)}</td><td>{_e(_clean(t.get('symbol') or '—', 40))}</td>"
            f"<td>{_e(SIDE_UA.get(side, _clean(side, 12)))}</td><td class='num'>{_e(_nf(t.get('size'), 4).rstrip('0').rstrip('.'))}</td>"
            f"<td class='num'>{_e(_px(t.get('entry')))} → {_e(_px(t.get('exit')))}</td>"
            f"<td class='num {_signed_cls(pnl)}'>{_usd(pnl, True)}</td><td class='num'>{_r(t.get('r_multiple'))}</td><td>{text}</td></tr>"
        )
    out.append("</table></div>")
    nav = [f"<span class='mut'>сторінка {page} з {pages} · {len(real)} угод, нові зверху</span>"]
    if page > 1:
        nav.insert(0, f"<a href='/journal?page={page - 1}'>← новіші</a>")
    if page < pages:
        nav.append(f"<a href='/journal?page={page + 1}'>старіші →</a>")
    out.append(f"<p class='pager'>{' '.join(nav)}</p>")
    return "".join(out)


def block_journal_other(paper: list[dict], airdrop: list[dict]) -> str:
    notes = []
    if paper:
        s = journal_summary(paper)
        notes.append(
            f"Паперові carry-угоди (теги *-paper): {len(paper)} рядків, закритих {s['closed_trades']}"
            + (f", P&L разом {_usd(s['total_pnl'], True)}" if s["closed_trades"] else "")
            + ". Не реальні угоди; детально — інструменти carry_paper_* і journal_stats(tag=…)."
        )
    if airdrop:
        notes.append(f"Рядків з тегом airdrop: {len(airdrop)} — не торгові угоди, до підсумку не входять.")
    return "".join(f"<p class='note'>{_e(x)}</p>" for x in notes)


def r_curve_svg(curve: list[dict]) -> str:
    """Cumulative R (y) by trade number in closing order (x), from 0; '' with fewer than two trades."""
    n = len(curve)
    if n < 2:
        return ""
    W, H, L, R, Tp, B = 720, 240, 48, 16, 12, 26
    ys = [0.0] + [c["cum"] for c in curve]
    pad = max(0.5, (max(ys) - min(ys)) * 0.08)
    lo, hi = min(ys) - pad, max(ys) + pad

    def X(i: float) -> float:
        return L + i / n * (W - L - R)

    def Y(v: float) -> float:
        return Tp + (hi - v) / (hi - lo) * (H - Tp - B)

    g = []
    st_ = nice_step(hi - lo, 4)
    p = math.ceil(lo / st_) * st_
    while p <= hi:
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(p) + 4:.1f}" text-anchor="end">{_e(_nf(p, 0 if st_ >= 1 else 1))}</text>'
        )
        p += st_
    g.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" style="stroke:var(--mut);stroke-dasharray:4 3"/>')
    xs = max(1, round(nice_step(n, 5)))
    for i in range(xs, n + 1, xs):
        g.append(f'<text x="{X(i):.1f}" y="{H - 8}" text-anchor="middle">{i}</text>')
    g.append(f'<text x="{L}" y="{H - 8}" text-anchor="start">угода №</text>')
    pts = [(0, 0.0)] + [(i + 1, c["cum"]) for i, c in enumerate(curve)]
    path = "".join(f"{'L' if i else 'M'}{X(k):.1f},{Y(v):.1f}" for i, (k, v) in enumerate(pts))
    g.append(f'<path d="{path}" style="fill:none;stroke:var(--acc);stroke-width:2"/>')
    if n <= JR_DOTS_MAX:
        g += [
            f'<circle cx="{X(i + 1):.1f}" cy="{Y(c["cum"]):.1f}" r="2.5" style="fill:var(--{"ok" if c["r"] > 0 else "bad" if c["r"] < 0 else "mut"})"/>'
            for i, c in enumerate(curve)
        ]
    last = curve[-1]["cum"]
    g.append(f'<text x="{X(n) - 4:.1f}" y="{Y(last) - 8:.1f}" text-anchor="end" style="fill:var(--fg)">{_e(_r(last))}</text>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Крива результату в R за угодами">{"".join(g)}</svg>'


JR_NO_STOP = (
    "R з'явиться, коли в угодах будуть стопи: /trade СИМВОЛ long ВХІД size N stop X у Telegram або journal_add зі "
    "стопом. Імпорт з бірж стопів не має, тож у цих угодах R невідомий."
)


def _jr_trade(t: dict | None) -> str:
    """'SYMBOL +$12 (#7)' for the weekly table; the symbol is untrusted text: cleaned, cut and escaped."""
    if not t:
        return "—"
    sym = _clean(t.get("symbol") or "?", JR_SYMBOL_SHOWN)
    return f"{_e(sym)} <span class='{_signed_cls(t['pnl'])}'>{_usd(t['pnl'], True)}</span> <span class='mut'>#{_e(t.get('id'))}</span>"


def block_journal_review(rows: list[dict] | None, now: float) -> str:
    """«Крива в R і тижневий розбір»: the cumulative R of the owner's closed real trades with a recorded R (a stop), in
    closing order, and the weekly review by ISO week (UTC) of the close. ``rows`` = all journal rows (None = no
    table); the owner's real trades (journal_owner_trades) are picked here."""
    if rows is None:
        return _empty("Таблиці journal у базі ще немає: крива й розбір з'являться з першою закритою угодою.")
    real = journal_owner_trades(rows)
    buckets = jr_week_buckets(real)
    if not buckets:
        return _empty("Закритих реальних угод ще немає: крива в R і тижневий розбір з'являться з першою закритою угодою.")
    # stats only for the shown weeks; totals and the 30+ count come from the bucket sizes (= closed_trades)
    weeks = [_jr_bucket_stats(xs, s, s + JR_WEEK_S) for s, xs in buckets[:JR_WEEKS]]
    n_closed = sum(len(xs) for _, xs in buckets)
    n_weeks = len(buckets)
    n_big = sum(1 for _, xs in buckets if len(xs) >= JR_SMALL_SAMPLE)
    curve = jr_r_curve(real)
    out = [f"<h3 style='font-size:14px;margin:6px 0 4px'>Крива в R{_info('j_rcurve')}</h3>"]
    if not curve:
        out.append(_empty(JR_NO_STOP))
    else:
        n, total = len(curve), curve[-1]["cum"]
        small = n < JR_SMALL_SAMPLE
        dd = jr_r_drawdown(curve)
        one = f"Поки одна угода з R ({_r(total)}): крива з'явиться з другою."
        out.append(r_curve_svg(curve) or f"<p class='note'>{_e(one)}</p>")
        line = (
            f"Угод з R: {n} з {n_closed} закритих; сума {_r(total)}, середній {_r(total / n)} на угоду, "
            f"найбільша просадка {_r(-dd) if dd > 0 else '0.00 R'}."
            + (f" Решта {n_closed - n} без стопу: R невідомий, у криву не входять." if n < n_closed else "")
        )
        chip = f" <span class='chip ev'>замало для висновку</span> {_e(f'менше {JR_SMALL_SAMPLE} угод з R')}" if small else ""
        out.append(f"<p class='note'>{_e(line)}{chip}</p>")
    out.append(f"<h3 style='font-size:14px;margin:12px 0 4px'>За тижнями{_info('j_weeks')}</h3>")
    cur = jr_week_start(now)
    rows_html = [
        "<div class='scroll'><table><tr><th>Тиждень</th><th>Закрито</th><th>У плюсі</th><th>P&amp;L</th><th>На угоду</th>"
        "<th>Середній R</th><th>Найкраща</th><th>Найгірша</th></tr>"
    ]
    for w in weeks:
        mon = datetime.fromtimestamp(w["start_ts"], timezone.utc)  # noqa: UP017
        label = f"{_rules_week(w['start_ts'])} <span class='mut'>з {mon:%d.%m}{' · поточний' if w['start_ts'] == cur else ''}</span>"
        r = (
            f"{_r(w['avg_r'])} <span class='mut'>за {w['r_count']} з {w['closed_trades']}</span>"
            if w["r_count"]
            else "<span class='mut'>невідомо</span>"
        )
        rows_html.append(
            f"<tr><td class='num'>{label}</td><td class='num'>{w['closed_trades']}</td>"
            f"<td class='num'>{w['wins']} ({_share(w['win_rate'])})</td>"
            f"<td class='num {_signed_cls(w['total_pnl'])}'>{_usd(w['total_pnl'], True)}</td>"
            f"<td class='num {_signed_cls(w['expectancy_per_trade'])}'>{_usd(w['expectancy_per_trade'], True)}</td>"
            f"<td class='num'>{r}</td><td>{_jr_trade(w['best'])}</td><td>{_jr_trade(w['worst'])}</td></tr>"
        )
    rows_html.append("</table></div>")
    out.append("".join(rows_html))
    note = (
        "Тиждень — ISO-тиждень закриття (UTC), нові зверху; P&L у USD — як записано в журналі, усі біржі разом. "
        f"Тижнів із {JR_SMALL_SAMPLE} і більше угодами: {n_big} з {n_weeks}; решта — опис, не висновок."
        + (f" Показано {JR_WEEKS} останніх тижнів з {n_weeks}." if n_weeks > JR_WEEKS else "")
        + " Статистика твоїх угод, не порада."
    )
    out.append(f"<p class='note'>{_e(note)}</p>")
    return "".join(out)


def rec_groups(rows: list[dict]) -> dict[str, list[dict]]:
    """The three groups assistant_paper_status summarizes separately (same definitions): 'method' = the chain trades
    (trigger None or 'chain'), comparable with the H1 walk-forward; 'method_triggered' = extra method trades opened on
    a regime change or an event (overlap the chain, correlated); 'manual' = picked by a person or a model."""
    meth = [t for t in rows if t.get("source") == "method"]
    return {
        "method": [t for t in meth if t.get("trigger") in (None, "chain")],
        "method_triggered": [t for t in meth if t.get("trigger") not in (None, "chain")],
        "manual": [t for t in rows if t.get("source") == "manual"],
    }


REC_OTHER_UA = {
    "method_triggered": "Додаткові угоди методу (зміна режиму / подія; перекриваються з ланцюгом, корельовані)",
    "manual": "Ручні угоди (обрані людиною чи моделлю, не метод)",
}


def _rec_tiles(s: dict, n_open: int) -> str:
    if not s["n"]:
        return _empty(
            f"Розрахованих паперових угод методу за ланцюгом (порівнянних з H1) ще немає (відкритих: {n_open}); "
            "підсумок з'явиться після першої експірації."
        )
    mean = 100 * s["mean_pnl_pct_spot"]
    tiles = [
        _tile("Розраховано", _nf(s["n"]), f"метод за ланцюгом · відкритих {n_open}", help="rec_n"),
        _tile("У плюсі", _share(s["win_share"]), help="rec_win"),
        _tile(
            "Середнє, % спота",
            f"<span class='{_signed_cls(mean)}'>{_pc(mean, 2)}</span>",
            "на 1 BTC, порівнянне з H1, основна метрика",
            help="rec_pct",
        ),
        _tile("Середній R (як у тесті)", _r(s["mean_r_study"]), f"угод з R {s['n_r_study']}", help="r"),
        _tile("Середній R (від найгіршого)", _r(s["mean_r_worst_case"]), "P&L / ризик угоди", help="rec_r_worst"),
        _tile(
            "P&L разом",
            f"<span class='{_signed_cls(s['sum_pnl_usd'])}'>{_usd(s['sum_pnl_usd'], True)}</span>",
            "USD на розмір угод",
            help="rec_pnl",
        ),
    ]
    return f"<div class='tiles'>{''.join(tiles)}</div>"


def block_rec_paper(rows: list[dict] | None) -> str:
    if rows is None:
        return _empty("Таблиці rec_paper у базі ще немає (з'являється зі схемою v16, коли MCP-сервер або колектор відкриють базу).")
    if not rows:
        return _empty("Паперових рекомендацій ще немає: завдання колектора assistant_paper записує їх щоранку.")
    groups = rec_groups(rows)
    n_open = sum(1 for t in groups["method"] if t.get("status") == "open")
    out = [
        "<p class='note'>Головний підсумок — лише угоди методу за ланцюгом (порівнянні з тестом H1), як method у "
        "assistant_paper_status; інші групи нижче, окремо, з власним n.</p>",
        _rec_tiles(rec_summary(groups["method"]), n_open),
    ]
    for key in ("method_triggered", "manual"):
        g = groups[key]
        if not g:
            continue
        s = rec_summary(g)
        n_o = sum(1 for t in g if t.get("status") == "open")
        line = f"{REC_OTHER_UA[key]}: розраховано {s['n']}, відкритих {n_o}"
        if s["n"]:
            line += (
                f"; у плюсі {_share(s['win_share'])}, середнє {_pc(100 * s['mean_pnl_pct_spot'], 2)} спота, "
                f"P&L разом {_usd(s['sum_pnl_usd'], True)}"
            )
        out.append(f"<p class='note'>{_e(line)}.</p>")
    out.append(
        "<div class='scroll'><table style='margin-top:8px'><tr><th>Відкрито</th><th>Структура</th><th>Експірація</th><th>Розмір</th>"
        "<th>Ризик</th><th>Чому</th><th>Стан</th><th>P&amp;L</th><th>% спота</th><th>R</th></tr>"
    )
    for t in rows[:REC_TABLE_ROWS]:
        pnl, pct = _num(t.get("pnl_usd")), _num(t.get("pnl_pct_spot"))
        within = "" if t.get("within_risk") else " <span class='warn'>понад ризик</span>"
        trig = str(t.get("trigger") or "")
        why = REC_TRIGGER_UA.get(trig, _clean(trig, 20)) if trig else ("вручну" if t.get("source") != "method" else "—")
        state = "розрахована" if t.get("status") == "settled" else "відкрита" if t.get("status") == "open" else _clean(t.get("status"), 12)
        cur = _clean(t.get("currency") or "", 8)
        out.append(
            f"<tr><td class='num'>{_e(_day(t.get('opened_ts')))}</td><td>{_e(_clean(t.get('structure') or '—', 40))} {_e(cur)}</td>"
            f"<td class='num'>{_e(_day(t.get('expiry_ts')))}</td><td class='num'>{_e(_nf(t.get('size_btc'), 2))}</td>"
            f"<td class='num'>{_usd(_num(t.get('risk_usd')))}{within}</td><td>{_e(why)}</td><td>{_e(state)}</td>"
            f"<td class='num {_signed_cls(pnl)}'>{_usd(pnl, True)}</td><td class='num'>{_e(_pc(None if pct is None else 100 * pct, 2))}</td>"
            f"<td class='num'>{_r(t.get('r_study'))}</td></tr>"
        )
    out.append("</table></div>")
    if len(rows) > REC_TABLE_ROWS:
        out.append(f"<p class='note'>Показано {REC_TABLE_ROWS} останніх з {len(rows)}.</p>")
    return "".join(out)


def _ci(ci: object) -> str:
    if not isinstance(ci, list) or len(ci) != 2 or _num(ci[0]) is None or _num(ci[1]) is None:
        return "—"
    return f"[{ci[0]:+.2f}; {ci[1]:+.2f}]".replace("-", "−")


def block_trade_plans(rows: list[dict] | None, n_shadow: int = 0) -> str:
    """Counts of the assistant's plans by verdict and status, and the resolved 'enter' plans' R (as plan_review's
    summary.by_verdict.enter). Below PLAN_MIN_N plans with R it says so: the numbers are descriptive only."""
    if rows is None:
        return _empty("Таблиці trade_plan у базі ще немає (з'являється зі схемою v17, коли MCP-сервер або колектор відкриють базу).")
    shadow = (
        f"<p class='note'>Тіньових планів демо-трейдера (src:demo-shadow): {n_shadow}; до підсумку не входять, їхній підсумок — plan_review → demo_shadow.</p>"
        if n_shadow
        else ""
    )
    if not rows:
        return _empty("Планів асистента ще немає: їх записує інструмент MCP plan_add (крок 10 чекліста), оцінює plan_review.") + shadow
    table = ["<div class='scroll'><table><tr><th>Вердикт</th><th>Усього</th><th>Відкритих</th><th>Розв'язаних</th></tr>"]
    for v, label in PLAN_VERDICT_UA.items():
        g = [x for x in rows if x.get("verdict") == v]
        n_open = sum(1 for x in g if x.get("status") == "open")
        n_done = sum(1 for x in g if x.get("status") == "resolved")
        table.append(
            f"<tr><td>{_e(label)}</td><td class='num'>{len(g)}</td><td class='num'>{n_open}</td><td class='num'>{n_done}</td></tr>"
        )
    table.append("</table></div>")
    out = ["".join(table)]
    s = plan_stats([x for x in rows if x.get("status") == "resolved" and x.get("verdict") == "enter"])
    out.append("<h3 style='font-size:14px;margin:10px 0 4px'>Плани enter: результат розв'язаних</h3>")
    if not s["n"]:
        out.append(_empty("Розв'язаних планів enter ще немає: підсумок з'явиться після першої оцінки (plan_review або щогодинна задача)."))
    else:
        o = s["outcomes"]
        mean, ci = s["mean_r"], s["ci95_mean_r"]
        if ci is None:
            vs0, cls = "ДІ — від 2 планів з R", ""
        elif ci[0] > 0:
            vs0, cls = "95 % ДІ вище 0", "ok"
        elif ci[1] < 0:
            vs0, cls = "95 % ДІ нижче 0", "bad"
        else:
            vs0, cls = "95 % ДІ містить 0: переваги не видно", ""
        tiles = [
            _tile("Розв'язано", _nf(s["n"]), f"з входом {s['n_filled']} · з R {s['n_r']}", help="plan_n"),
            _tile("У плюсі", _share(s["win_rate"]), "частка планів з R > 0", help="plan_win"),
            _tile("Середній R", f"<span class='{_signed_cls(mean)}'>{_r(mean)}</span>", f"95 % ДІ {_ci(ci)}", help="r"),
            _tile("Проти 0", f"<span class='{cls}'>{_e(vs0)}</span>", "t-інтервал середнього R", help="plans"),
            _tile(
                "Виходи",
                f"{o['target']} / {o['stop']}",
                f"ціль / стоп · за строком {o['expired']} · без входу {o['not_filled']}",
                help="plan_exits",
            ),
        ]
        out.append(f"<div class='tiles'>{''.join(tiles)}</div>")
    if s["n_r"] < PLAN_MIN_N:
        out.append(
            f"<p class='note warn'>Замало планів: n = {s['n_r']} планів enter з R (потрібно ≥ {PLAN_MIN_N}). "
            "Підсумок описовий, не доказ переваги; перший чесний зріз — після ~30 планів enter.</p>"
        )
    out.append(shadow)
    if len(rows) >= PLAN_MAX_ROWS:
        out.append(f"<p class='note warn'>Прочитано лише {PLAN_MAX_ROWS} останніх планів.</p>")
    return "".join(out)


def _bs(x: object) -> str:
    """A Brier score (decimal), three digits."""
    return _nf(x, 3)


def _skill(x: object) -> str:
    """Skill 1 − BS / BS_ref as a signed percent, coloured."""
    v = _num(x)
    return "—" if v is None else f"<span class='{_signed_cls(v)}'>{_pc(100 * v, 0)}</span>"


def calibration_svg(bins: list[dict]) -> str:
    """Reliability diagram: mean forecast (x) vs observed frequency (y), both 0..1, dot area ~ n; '' without bins."""
    pts = [
        (p, f, n)
        for b in bins
        if (p := _num(b.get("mean_prob"))) is not None and (f := _num(b.get("freq"))) is not None and (n := _num(b.get("n")))
    ]
    if not pts:
        return ""
    W, H, L, R, Tp, B = 300, 270, 40, 12, 10, 40

    def X(p: float) -> float:
        return L + max(0.0, min(1.0, p)) * (W - L - R)

    def Y(f: float) -> float:
        return Tp + (1.0 - max(0.0, min(1.0, f))) * (H - Tp - B)

    g = []
    for v in (0.0, 0.25, 0.5, 0.75, 1.0):
        g.append(
            f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" style="stroke:var(--line)"/>'
            f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{Tp}" y2="{H - B}" style="stroke:var(--line)"/>'
            f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{100 * v:.0f}</text>'
            f'<text x="{X(v):.1f}" y="{H - B + 14}" text-anchor="middle">{100 * v:.0f}</text>'
        )
    g.append(
        f'<line x1="{X(0):.1f}" y1="{Y(0):.1f}" x2="{X(1):.1f}" y2="{Y(1):.1f}" style="stroke:var(--mut);stroke-dasharray:5 4"/>'
        f'<text x="{(L + W - R) / 2:.1f}" y="{H - 6}" text-anchor="middle">прогноз, %</text>'
        f'<text x="12" y="{(Tp + H - B) / 2:.1f}" text-anchor="middle" transform="rotate(-90 12 {(Tp + H - B) / 2:.1f})">частота події, %</text>'
    )
    nmax = max(n for _, _, n in pts)
    g += [
        f'<circle cx="{X(p):.1f}" cy="{Y(f):.1f}" r="{3 + 6 * math.sqrt(n / nmax):.1f}" style="fill:var(--acc);fill-opacity:.75"/>'
        for p, f, n in pts
    ]
    return (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Калібрування: прогноз проти частоти події" '
        f'style="max-width:340px">{"".join(g)}</svg>'
    )


def _fc_calibration_block(s: dict) -> str:
    rows = [
        "<div class='scroll'><table><tr><th>Кошик</th><th>Прогнозів</th><th>Середній прогноз</th><th>Частота події</th>"
        "<th>Частота − прогноз</th></tr>"
    ]
    for b in s["calibration"]:
        gap = 100 * (b["freq"] - b["mean_prob"])
        rows.append(
            f"<tr><td class='num'>{100 * b['lo']:.0f}–{100 * b['hi']:.0f} %</td><td class='num'>{b['n']}</td>"
            f"<td class='num'>{_share(b['mean_prob'])}</td><td class='num'>{_share(b['freq'])}</td>"
            f"<td class='num'>{_e(_pc(gap, 0).replace(' %', ' вп'))}</td></tr>"
        )
    rows.append("</table></div>")
    note = (
        f"Усього: середній прогноз {_share(s['mean_prob'])}, частота подій {_share(s['base_rate'])}. Розклад Brier за "
        f"кошиками: надійність {_bs(s['reliability'])} (менше — краще, 0 — ідеальне калібрування), роздільність "
        f"{_bs(s['resolution'])} (більше — краще), невизначеність {_bs(s['uncertainty'])}; Brier ≈ надійність − "
        "роздільність + невизначеність (точно, лише коли прогнози в кошику однакові)."
    )
    return calibration_svg(s["calibration"]) + "".join(rows) + f"<p class='note'>{_e(note)}</p>"


def _fc_weekly_block(rows: list[dict]) -> str:
    weeks = fc_weekly(rows)
    if not weeks:
        return ""
    head = "".join(f"<th>Brier {_e(FC_KIND_UA[k])}</th>" for k in FC_KINDS)
    out = [
        "<div class='scroll'><table><tr><th>Тиждень експірації</th><th>Прогнозів</th><th>Експірацій</th><th>Brier</th>"
        f"<th>Brier на еталонних / еталон</th><th>Навичка проти еталона</th>{head}</tr>"
    ]
    for w in reversed(weeks[-FC_WEEKS:]):
        ref = "—" if w["brier_on_ref"] is None else f"{_bs(w['brier_on_ref'])} / {_bs(w['brier_ref'])}"
        kinds = "".join(f"<td class='num'>{_bs(w['brier_by_kind'].get(k))}</td>" for k in FC_KINDS)
        out.append(
            f"<tr><td class='num'>{_e(w['week'])}</td><td class='num'>{w['n']}</td><td class='num'>{w['n_expiries']}</td>"
            f"<td class='num'>{_bs(w['brier'])}</td><td class='num'>{_e(ref)}</td><td class='num'>{_skill(w['skill_vs_ref'])}</td>{kinds}</tr>"
        )
    out.append("</table></div>")
    more = f" Показано {FC_WEEKS} останніх тижнів з {len(weeks)}." if len(weeks) > FC_WEEKS else ""
    out.append(
        "<p class='note'>Тиждень — ISO-тиждень експірації (UTC), нові зверху. Кліматології тут немає: у тижні замало "
        f"результатів.{_e(more)}</p>"
    )
    return "".join(out)


def block_forecasts(rows: list[dict] | None) -> str:
    """Weekly Brier and calibration of the risk-neutral range forecasts (table forecast_log), as forecast_log_status
    scores them. Small n_expiries is said out loud: the numbers are descriptive."""
    if rows is None:
        return _empty("Таблиці forecast_log у базі ще немає (з'являється зі схемою v20, коли MCP-сервер або колектор відкриють базу).")
    if not rows:
        return _empty("Прогнозів ще немає: завдання колектора forecast_log записує їх щодня після 09:00 UTC (BTC і ETH).")
    n_open = sum(1 for x in rows if x.get("status") == "open")
    s = fc_score(rows)
    if not s["n"]:
        nxt = min((v for x in rows if x.get("status") == "open" and (v := _num(x.get("expiry_ts"))) is not None), default=None)
        return _empty(
            f"Розрахованих прогнозів ще немає (відкритих: {n_open}); перша оцінка — після delivery експірації "
            f"{_day(nxt)} (08:00 UTC), коли колектор її розрахує."
        )
    small = s["n_expiries"] < FC_MIN_EXPIRIES
    tiles = [
        _tile("Прогнозів розраховано", _nf(s["n"]), f"відкритих {n_open} · 4 події × BTC і ETH щодня", help="fc_n"),
        _tile(
            "Експірацій",
            _nf(s["n_expiries"]),
            "розмір вибірки: актив × експірація, результат спільний",
            help="fc_expiries",
            tone="warn" if small else "",
            badge="замало для висновку" if small else "",
        ),
        _tile("Brier", _bs(s["brier"]), "середнє (p − o)² · 0 — ідеально, 0.25 — завжди 50 %", help="fc_brier"),
        _tile(
            "Проти фіксованого еталона",
            _skill(s["skill_vs_ref"]),
            (
                f"Brier {_bs(s['brier_on_ref'])} проти {_bs(s['brier_ref'])} · {s['n_ref']} прогнозів «вище спота» і «±1σ»"
                if s["n_ref"]
                else "еталон є лише для «вище спота» і «±1σ»"
            ),
            help="fc_ref",
        ),
        _tile(
            "Проти кліматології",
            _skill(s["skill_vs_climatology"]),
            f"Brier {_bs(s['brier'])} проти {_bs(s['brier_climatology'])} · частота в цій вибірці, заднім числом",
            help="fc_clim",
        ),
    ]
    c = fc_compare(rows)
    if c["n"]:
        ci = c["ci95"]
        tiles.append(
            _tile(
                "Опціони проти RV30",
                f"<span class='{_signed_cls(-c['diff'])}'>{c['diff']:+.3f}</span>",
                f"Brier {_bs(c['brier_risk_neutral'])} проти {_bs(c['brier_rv30'])} · "
                + (f"95 % ДІ {ci[0]:+.3f} … {ci[1]:+.3f}" if ci else "ДІ — від 2 дат експірації")
                + f" · {c['n_dates']} дат експірації · мінус — опціони точніші",
                help="fc_rv",
                tone="" if c["enough"] else "warn",
                badge="" if c["enough"] else "замало для висновку",
            )
        )
    out = [f"<div class='tiles'>{''.join(tiles)}</div>"]
    if small:
        out.append(
            f"<p class='note warn'>Замало експірацій: n_expiries = {s['n_expiries']} (потрібно ≥ {FC_MIN_EXPIRIES}). "
            "Цифри описові, висновку немає.</p>"
        )
    out.append(f"<h3 style='font-size:14px;margin:12px 0 4px'>Калібрування, 10 кошиків{_info('fc_calib')}</h3>")
    out.append(_fc_calibration_block(s))
    out.append(f"<h3 style='font-size:14px;margin:12px 0 4px'>За тижнями{_info('fc_weekly')}</h3>")
    out.append(_fc_weekly_block(rows))
    if len(rows) >= FC_MAX_ROWS:
        out.append(f"<p class='note warn'>Прочитано лише {FC_MAX_ROWS} останніх прогнозів.</p>")
    return "".join(out)


JOURNAL_CSS = """
form.jr{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:0 12px 12px}
form.jr button{font:600 14px -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;border:0;border-radius:999px;padding:9px 18px;
background:var(--acc);color:var(--card);cursor:pointer}form.jr button:disabled{opacity:.6;cursor:default}.jrs{font-size:13px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.scroll table{min-width:560px}
.pager{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:8px 0 0}.pager a{color:var(--acc);font-weight:600}
h2 .chip{margin-left:8px;vertical-align:middle}
"""


def render_journal(
    data: dict | None, err: str | None, now: float, params: dict | None = None, path: Path | None = None, refresh: dict | None = None
) -> str:
    """The «Журнал» tab. Never raises: no database, no table or no rows gives an explained empty state. `refresh` is
    the status of the «Оновити угоди» run (journal_refresh.py)."""
    p = params or {"page": 1}
    try:
        bar = block_journal_refresh(refresh or {}, now)
    except Exception:  # noqa: BLE001 - an odd status file must not take the page down
        bar = block_journal_refresh({}, now)
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>tradedesk — журнал</title><style>{CSS}{BRIEF_CSS}{JOURNAL_CSS}</style></head><body>"
    )
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in JOURNAL_CAVEATS) + "</ul></div>"
    where = f" <code>{_e(path)}</code>" if path else ""
    try:
        if data is None:
            msg = (
                f"Бази ще немає{where}: журнал з'явиться, коли MCP-сервер або колектор її створять."
                if err == "missing"
                else f"Базу не вдалося прочитати{where}: {_e(err)}."
            )
            return head + f"<header><h1>Журнал</h1><span class='bad'>{msg}</span></header>{_nav('journal')}{caveats}</body></html>"
        stamp = f"<span class='mut'>База{where} · лише читання · прочитано {_e(kyiv(datetime.fromtimestamp(now, timezone.utc)))} за Києвом</span>"  # noqa: UP017
        out = [head, f"<header><h1>Журнал</h1>{stamp}</header>{_nav('journal')}{bar}{caveats}<main class='brief'>"]
        rows = data.get("journal")
        try:
            review = block_journal_review(rows, now)
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            review = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        review = f"<section class='wide'>{_h2('Крива в R і тижневий розбір', 'j_review')}{review}</section>"
        if rows is None:
            out.append(
                f"<section class='wide'>{_h2('Мої угоди', 'j_trades')}"
                + _empty("Таблиці journal у базі ще немає: вона з'являється, коли MCP-сервер уперше відкриває базу.")
                + "</section>"
            )
            out.append(review)
        elif not rows:
            out.append(
                f"<section class='wide'>{_h2('Мої угоди', 'j_trades')}"
                + _empty("Угод ще немає.")
                + "<p class='note'>Додати першу: інструмент MCP <code>journal_add</code> (теза, стоп, ціль) і "
                "<code>journal_close</code> при виході; угоди з бірж — <code>scripts/bybit_import.py</code>.</p></section>"
            )
            out.append(review)
        else:
            kinds: dict[str, list[dict]] = {"real": [], "paper": [], "airdrop": []}
            groups: dict[str, list[dict]] = {}
            for t in rows:
                kind, src = journal_kind(journal_tags(t.get("tags")))
                kinds[kind].append(t)
                if kind == "real":
                    groups.setdefault(src, []).append(t)
            real = kinds["real"]
            n_open = sum(1 for t in real if t.get("status") == "open")
            out.append(f"<section class='wide'>{_h2('Мої угоди', 'j_trades')}{block_journal_tiles(real, n_open, now)}")
            out.append(block_journal_other(kinds["paper"], kinds["airdrop"]))
            if len(rows) >= JOURNAL_MAX_ROWS:
                out.append(f"<p class='note warn'>Прочитано лише {JOURNAL_MAX_ROWS} останніх рядків журналу.</p>")
            out.append("</section>")
            out.append(review)
            out.append(f"<section>{_h2('За джерелом', 'sources')}{block_journal_sources(groups)}</section>")
            out.append(f"<section class='wide'>{_h2('Угоди', 'j_table')}{block_journal_table(real, p['page'])}</section>")
        try:
            rules = block_rules_week(kinds["real"] if rows else rows, now)
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            rules = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        out.append(f"<section class='wide'>{_h2('Порушення правил за тиждень', 'rules_week')}{rules}</section>")
        try:
            rec = block_rec_paper(data.get("rec_paper"))
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            rec = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        out.append(
            f"<section class='wide'>{_h2('Паперові рекомендації асистента', 'rec', CHIP_HYPOTHESIS)}"
            f"<p class='note warn'>{_e(REC_CAVEAT)}</p>{rec}</section>"
        )
        try:
            plans = block_trade_plans(data.get("trade_plan"), int(_num(data.get("plan_shadow")) or 0))
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            plans = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        out.append(
            f"<section class='wide'>{_h2('Плани угод асистента', 'plans', CHIP_LEDGER)}"
            f"<p class='note warn'>{_e(PLAN_CAVEAT)}</p>{plans}</section>"
        )
        try:
            fc = block_forecasts(data.get("forecast_log"))
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            fc = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        out.append(
            f"<section class='wide'>{_h2('Журнал прогнозів', 'fc', CHIP_LEDGER)}"
            f"<p class='note warn'><b>Ризик-нейтральні ймовірності, не прогноз асистента.</b> {_e(FC_CAVEAT)}</p>{fc}</section>"
        )
        out.append("</main></body></html>")
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header><h1>Журнал</h1><span class='bad'>Журнал не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('journal')}{caveats}</body></html>"
        )


def journal_page(query: str = "", now: float | None = None) -> str:
    data, err = load_journal(JOURNAL_DB)
    page = render_journal(data, err, time.time() if now is None else now, parse_journal_query(query), JOURNAL_DB, read_refresh_status())
    return with_help(page)


# The «Оновити угоди» button: POST /journal/refresh starts scripts/journal_refresh.py (Bybit + Hyperliquid imports,
# read-only, duplicates skipped) in the background with the project's uv; the page shows its status file and reloads
# itself while it runs. One run at a time (the script's lock) and not more often than once a minute.
REFRESH_STATUS = STATE / "journal-refresh.json"
REFRESH_STALE_S = 20 * 60  # a 'running' status older than this is a crashed run: a new one may start
REFRESH_MIN_GAP_S = 60
UV = HOME / ".local" / "bin" / "uv"
VENUE_UA = {"bybit": "Bybit", "hyperliquid": "Hyperliquid"}


def read_refresh_status(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or REFRESH_STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def refresh_running(st: dict, now: float) -> bool:
    started = _num(st.get("started"))
    return st.get("state") == "running" and started is not None and now - started < REFRESH_STALE_S


def start_journal_refresh(now: float | None = None, status: dict | None = None, spawn=None) -> str:
    """'started', 'running' or 'too_soon'. `spawn` (tests) replaces the background process start."""
    now = time.time() if now is None else now
    st = read_refresh_status() if status is None else status
    if refresh_running(st, now):
        return "running"
    last = _num(st.get("finished")) or _num(st.get("started"))
    if last is not None and now - last < REFRESH_MIN_GAP_S:
        return "too_soon"
    cmd = [str(UV), "run", "-q", "python", str(REPO / "scripts" / "journal_refresh.py")]
    if spawn is None:
        STATE.mkdir(parents=True, exist_ok=True)
        # the status file says 'running' at once, so the redirected page already shows it
        tmp = REFRESH_STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps({"state": "running", "started": now, "finished": None, "venues": {}}), encoding="utf-8")
        tmp.replace(REFRESH_STATUS)
        subprocess.Popen(
            cmd, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )  # noqa: S603
    else:
        spawn(cmd)
    return "started"


def block_journal_refresh(st: dict, now: float) -> str:
    """The refresh button and the last run's result (our own status file: venue names, counts, short reasons)."""
    running = refresh_running(st, now)
    btn = (
        "<form method='post' action='/journal/refresh' class='jr'>"
        f"<button type='submit'{' disabled' if running else ''}>{'⏳ Оновлюю…' if running else '🔄 Оновити угоди'}</button>"
    )
    if running:
        msg = f"Тягну нові угоди з Bybit і Hyperliquid · почато {fmt_age(now - (_num(st.get('started')) or now))}"
        done = [f"{VENUE_UA.get(k, k)} ✓" for k, v in (st.get("venues") or {}).items() if isinstance(v, dict) and v.get("ok")]
        if done:
            msg += " · " + ", ".join(done)
        line = f"<span class='mut'>{_e(msg)}</span><script>setTimeout(function () {{ location.replace('/journal'); }}, 4000);</script>"
    elif st.get("state") in ("done", "error"):
        parts = []
        for k in ("bybit", "hyperliquid"):
            v = (st.get("venues") or {}).get(k)
            if not isinstance(v, dict):
                continue
            n = int(_num(v.get("added")) or 0)
            if v.get("ok"):
                parts.append(f"<span class='ok'>{_e(VENUE_UA[k])}: +{n}</span>")
            elif v.get("skipped"):
                parts.append(f"<span class='warn'>{_e(VENUE_UA[k])}: пропущено — {_e(_clean(v.get('error'), 120))}</span>")
            else:
                parts.append(f"<span class='bad'>{_e(VENUE_UA[k])}: помилка — {_e(_clean(v.get('error'), 120))}</span>")
        when = _num(st.get("finished"))
        stamp = f"оновлено {_e(kyiv(datetime.fromtimestamp(when, timezone.utc)))} · {_e(fmt_age(now - when))}" if when else ""  # noqa: UP017
        line = f"<span class='mut'>{stamp}</span> " + " · ".join(parts)
    else:
        line = "<span class='mut'>Ще не оновлювали з панелі. Кнопка тягне закриті угоди Bybit за 30 днів і Hyperliquid.</span>"
    return btn + f"<span class='jrs'>{line}</span></form>"


# ---------------------------------------------------------------- «Polymarket» tab: current Polymarket <-> Deribit gaps
#
# A hint for manual trades, not the paper test: the paper test (docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md,
# «Реєстрація») reads the same table on its own and nothing here feeds it. Read-only SQLite (mode=ro URI), no network.
# The filters are stdlib copies of analytics.pm_gap_paper (rules_accepted, accepted_rule_hashes, eligible, model_prob /
# vol_scaled_prob, the edge of entry_signals), pm_touch.fee_per_share and probability.prob_touch; tests compare them
# with the originals on real rows. Every string from the database (question, slug, rules) is untrusted text.

PM_DB = JOURNAL_DB
PM_PASS = 0.05  # registered entry threshold: net edge, decimal of the $1 payoff
PM_NEAR = 0.03  # the «близько» table: PM_NEAR <= edge < PM_PASS
PM_MIN_DEPTH_USD = 200.0  # USD standing at the best ask of the bought side
PM_MIN_ASK, PM_MAX_ASK = 0.02, 0.98
PM_MIN_HOURS, PM_MAX_HOURS = 2.0, 14 * 24.0  # snapshot to market end
PM_VOL_KS = (1.0, 0.92)  # the side's win probability is the lower one over these implied-vol multipliers
PM_MAX_SKEW_S = 60.0  # book vs Deribit surface read times, applied when the row has both
PM_STALE_S = 2 * 3600  # the collector runs hourly
PM_RISK_USD = 100.0  # the owner's risk per trade (deposit ~$10k, 1 %)
PM_MIN_RR = 2.0  # the owner's rule 5
PM_YEAR_S = 365 * 86_400
PM_PAIRS = {"BTC": "BTC/USDT", "ETH": "ETH/USDT"}
PM_IDEA_TAG = "idea:polymarket-vs-options-forward"
PM_CAVEATS = (
    "Ймовірності з опціонів Deribit — ризик-нейтральні (ціна страхування в ринку), а не реальні. Бік показано, лише якщо "
    "розрив пережив обидва читання моделі: як зібрано і з IV × 0.92.",
    "Перевага не доведена: паперовий тест (картка polymarket-vs-options-forward) дасть вердикт лише після 16 тижнів, "
    "≈ 23.02.2027. Ця вкладка — підказка для ручних угод. Код вкладки тест не змінює, але власні угоди на тих самих "
    "тонких ринках можуть зсунути книги, з яких тест рахує CLV: тому кожну угоду записуй з market_id.",
    "Книги тонкі й рухаються: аск і обсяг — на момент щогодинного знімка. Перед угодою перевір книгу на polymarket.com.",
    "Кожна позиція ризикує всією ставкою (до $100): вона входить у правило 2 (сумарний ризик ≤ $300) і в денний стоп "
    "правила 4, як і будь-яка інша угода.",
    "Якщо береш угоду — запиши її через journal_add: symbol PM:<market_id>:<YES|NO> (стовпець «Запис»), side long, "
    "size = кількість акцій, entry = ціна з комісією, stop 0.001, теги "
    f"{PM_IDEA_TAG} і pm:<market_id>; після розв'язання — journal_close з exit 1 (виграш) або 0.001 (програш). "
    "plan_add тут не підходить: plan_review оцінює плани на свічках бірж, бінарний ринок так не оцінити.",
    "Не інвестиційна порада: сторінка лише читає базу, нічого не торгує.",
)
PM_CSS = """
details.near{font-size:13px;color:var(--fg)}details.near summary{cursor:pointer;color:var(--acc);font-weight:600}
table.pm td,table.pm th{white-space:nowrap}table.pm td.q{white-space:normal;min-width:180px;max-width:320px}
table.pm a{color:var(--acc)}.scroll table.pm{min-width:900px}
"""


def pm_rules_accepted(description: object, asset: object) -> bool:
    """Stdlib copy of pm_gap_paper.rules_accepted (with pm_threshold.standard_rules): the standard Binance-candle text or
    the wording used since 2026-10-07, both with the strict "Close ... higher than" clause."""
    d = str(description or "").replace("\n", " ").replace("“", '"').replace("”", '"')
    pair = PM_PAIRS.get(str(asset))
    if not pair or '"Close" price higher than the price specified in the title' not in d:
        return False
    return f"Binance 1 minute candle for {pair} 12:00 in the ET timezone" in d or (
        f"Binance 1 minute candle for {pair} closing at 12:00 in the ET timezone" in d
    )


def pm_accepted_hashes(rows: list[dict]) -> set[str]:
    """Stdlib copy of pm_gap_paper.accepted_rule_hashes: rules_standard = 1, or a threshold text pm_rules_accepted."""
    ok: set[str] = set()
    for r in rows:
        h = r.get("rules_hash")
        if not h:
            continue
        if r.get("rules_standard") == 1:
            ok.add(str(h))
        elif r.get("kind") == "above" and r.get("rules") and pm_rules_accepted(r["rules"], r.get("asset")):
            ok.add(str(h))
    return ok


def pm_fee(price: float, rate: float, exponent: float) -> float:
    """Stdlib copy of pm_touch.fee_per_share: upper bound of Polymarket's taker fee per share."""
    return rate * (price * (1.0 - price)) ** exponent if rate > 0 else 0.0


def pm_prob_touch(F: float, barrier: float, sigma: float, T: float) -> float:
    """Stdlib copy of probability.prob_touch (continuous monitoring, driftless forward, constant sigma)."""
    if T <= 0 or sigma <= 0:
        return 1.0 if barrier == F else 0.0
    s = sigma * math.sqrt(T)
    mu = -0.5 * sigma * sigma
    if barrier == F:
        return 1.0
    if barrier > F:
        b = math.log(barrier / F)
        return _ncdf((-b + mu * T) / s) + math.exp(2 * mu * b / (sigma * sigma)) * _ncdf((-b - mu * T) / s)
    a = math.log(F / barrier)
    return _ncdf((-a - mu * T) / s) + math.exp(-2 * mu * a / (sigma * sigma)) * _ncdf((-a + mu * T) / s)


def _pm_g(kind: str, F: float, K: float, T: float, sigma: float) -> float:
    if kind == "above":
        v = sigma * math.sqrt(T)
        return _ncdf((math.log(F / K) + 0.5 * v * v) / v - v)  # N(d2)
    return pm_prob_touch(F, K, sigma, T)


def pm_model_prob(row: dict, k: float = 1.0) -> float | None:
    """Stdlib copy of pm_gap_paper.model_prob: the row's model probability of YES with implied vol x ``k``
    (p_k = p + g(k sigma) - g(sigma), forward = the snapshot's Deribit index ``spot``; clipped to [0, 1])."""
    p = row.get("model_prob")
    if p is None:
        return None
    if k == 1.0:
        return float(p)
    iv, F, K = row.get("model_iv"), row.get("spot"), float(row["strike"])
    tau = (int(row["end_ts"]) - int(row["ts"])) / PM_YEAR_S
    if iv is None or F is None or iv <= 0 or F <= 0 or K <= 0 or tau <= 0:
        return None
    kind = str(row["kind"])
    q = float(p) + _pm_g(kind, float(F), K, tau, k * float(iv)) - _pm_g(kind, float(F), K, tau, float(iv))
    return min(1.0, max(0.0, q))


def pm_eligible(row: dict, accepted: set[str]) -> bool:
    """Stdlib copy of pm_gap_paper.eligible with the registered rule: model note 'ok', strike inside the quoted strikes,
    accepted resolution text, book and surface read within 60 s (when the row has both times), 2 h <= to end <= 14 d
    (measured from the snapshot, as registered)."""
    if row.get("model_note") != "ok" or row.get("model_prob") is None or row.get("model_covered") != 1:
        return False
    if str(row.get("rules_hash")) not in accepted:
        return False
    bt, st_ = row.get("book_ts"), row.get("surface_ts")
    if bt is not None and st_ is not None and abs(float(bt) - float(st_)) > PM_MAX_SKEW_S:
        return False
    h = (int(row["end_ts"]) - int(row["ts"])) / 3600
    return PM_MIN_HOURS <= h <= PM_MAX_HOURS


def pm_sides(row: dict, accepted: set[str]) -> list[dict]:
    """Both sides of one eligible snapshot row that have a tradable book (ask in [0.02; 0.98], >= $200 at the ask),
    whatever their edge: net edge = p_win - (ask + taker fee bound), p_win = the less favourable reading over PM_VOL_KS
    (the same number as pm_gap_paper.entry_signals). Plus R:R of buying the side and the size for a $100 risk."""
    if not pm_eligible(row, accepted):
        return []
    ps = [pm_model_prob(row, k) for k in PM_VOL_KS]
    if any(p is None for p in ps):
        return []
    p_lo, p_hi = min(ps), max(ps)  # type: ignore[type-var]
    fr, fe = float(row.get("fee_rate") or 0.0), float(row.get("fee_exp") or 1.0)
    out = []
    for side, p_win, p_k1, p_k2 in (
        ("yes", p_lo, ps[0], ps[1]),
        ("no", 1.0 - p_hi, 1.0 - ps[0], 1.0 - ps[1]),  # type: ignore[operator]
    ):
        ask, depth = row.get(f"{side}_ask"), row.get(f"{side}_ask_usd")
        if ask is None or not PM_MIN_ASK <= ask <= PM_MAX_ASK or depth is None or depth < PM_MIN_DEPTH_USD:
            continue
        cost = ask + pm_fee(ask, fr, fe)
        shares = math.floor(PM_RISK_USD / cost + 1e-9)
        out.append(
            {
                "market_id": str(row["market_id"]),
                "side": side,
                "asset": row.get("asset"),
                "kind": row.get("kind"),
                "question": row.get("question"),
                "event_slug": row.get("event_slug"),
                "end_ts": int(row["end_ts"]),
                "model_expiry": row.get("model_expiry"),
                "expiry_gap_hours": row.get("expiry_gap_hours"),
                "ask": float(ask),
                "cost": cost,
                "p_win": p_win,
                "p_k1": p_k1,
                "p_k092": p_k2,
                "edge": p_win - cost,
                "depth_usd": float(depth),
                "rr": (1.0 - cost) / cost,
                "shares": shares,
                "stake": shares * cost,
                "payout": float(shares),
            }
        )
    return out


def pm_gap_view(data: dict, now: float) -> dict:
    """Rows of the latest snapshot split for the tab: 'pass' (edge >= 5 pp) and 'near' (3-5 pp), each sorted by net
    edge (largest first). The registered filters are applied at the snapshot time; on top of that a side whose market
    ends less than 2 h after ``now`` is dropped and counted ('n_short': the rule no longer enters there, the snapshot
    may be up to hours old). A row the copies cannot read (odd types) is skipped and counted, never raised."""
    rows = data.get("rows") or []
    accepted = pm_accepted_hashes(list(data.get("text_rows") or []) + list(rows))
    out: dict = {"pass": [], "near": [], "n_rows": len(rows), "n_eligible": 0, "n_short": 0, "n_bad": 0, "n_synced": 0}
    for r in rows:
        try:
            if r.get("book_ts") is not None and r.get("surface_ts") is not None:
                out["n_synced"] += 1
            if not pm_eligible(r, accepted):
                continue
            out["n_eligible"] += 1
            sides = pm_sides(r, accepted)
        except (TypeError, ValueError, KeyError, OverflowError, ZeroDivisionError):
            out["n_bad"] += 1
            continue
        for s in sides:
            if s["edge"] < PM_NEAR:
                continue
            if s["end_ts"] - now < PM_MIN_HOURS * 3600:
                out["n_short"] += 1
                continue
            out["pass" if s["edge"] >= PM_PASS else "near"].append(s)
    for key in ("pass", "near"):
        out[key].sort(key=lambda s: -s["edge"])
    return out


def load_pm_gap(path: Path) -> tuple[dict | None, str | None]:
    """{'ts': latest snapshot ts | None, 'rows': its rows | None (no table), 'text_rows': resolution texts of its
    markets}, or (None, 'missing' | 'unreadable: …'). Read-only connection; works with and without the book_ts /
    surface_ts columns (SELECT *); never raises."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "pm_gap_snapshot" not in names:
            return {"ts": None, "rows": None, "text_rows": []}, None
        ts = con.execute("SELECT MAX(ts) FROM pm_gap_snapshot").fetchone()[0]
        if ts is None:
            return {"ts": None, "rows": [], "text_rows": []}, None
        rows = [dict(r) for r in con.execute("SELECT * FROM pm_gap_snapshot WHERE ts = ?", (ts,))]
        # the store keeps the resolution text only when its hash changes: read it from any row of these markets
        text_rows = [
            dict(r)
            for r in con.execute(
                "SELECT market_id, asset, kind, rules_hash, rules_standard, rules FROM pm_gap_snapshot"
                " WHERE market_id IN (SELECT market_id FROM pm_gap_snapshot WHERE ts = ?) AND rules IS NOT NULL",
                (ts,),
            )
        ]
        return {"ts": int(ts), "rows": rows, "text_rows": text_rows}, None
    except (sqlite3.Error, OSError, TypeError, ValueError) as e:
        return None, f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def _pp(x: float) -> str:
    return f"{100 * x:+.1f} п.п.".replace("-", "−")


def _hours_left(h: float) -> str:
    if h < 1:
        return f"{max(0.0, h) * 60:.0f} хв"
    return f"{h:.1f} год" if h < 10 else f"{h:.0f} год" if h < 48 else f"{h / 24:.1f} дн"


def _pm_expiry(s: dict) -> str:
    try:
        exp = datetime.fromisoformat(str(s.get("model_expiry")))
    except ValueError:
        return "—"
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)  # noqa: UP017
    gap = _num(s.get("expiry_gap_hours"))
    gap_s = "" if gap is None else f" ({gap:+.0f} год)".replace("-", "−")
    return kyiv(exp) + gap_s


def block_pm_table(sides: list[dict], now: float) -> str:
    out = [
        "<div class='scroll'><table class='pm'><tr><th>Актив</th><th>Ринок</th><th>Купити</th><th>Аск</th>"
        f"<th>Модель: шанс боку{_info('pm_model')}<br>k=1 · k=0.92</th><th>Чистий edge{_info('pm_edge')}</th><th>$ на аску</th>"
        f"<th>До кінця</th><th>Експірація Deribit<br>(різниця)</th><th>R:R{_info('pm_rr')}</th><th>На ризик $100</th>"
        "<th>Запис (journal_add)</th></tr>"
    ]
    for s in sides:
        slug = str(s.get("event_slug") or "")
        q = _e(_clean(s.get("question") or "—", 140))
        if slug:
            href = "https://polymarket.com/event/" + urllib.parse.quote(slug[:200], safe="-_.")
            q = f"<a href='{_e(href)}' target='_blank' rel='noopener noreferrer'>{q}</a>"
        mid = _clean(s.get("market_id") or "—", 40)
        side = "YES" if s["side"] == "yes" else "NO"
        record = f"<code>PM:{_e(mid)}:{side}</code><br><span class='mut'>теги {_e(PM_IDEA_TAG)}, pm:{_e(mid)}</span>"
        rr = s["rr"]
        flag = "" if rr >= PM_MIN_RR else "<br><span class='warn'>нижче правила 5 власника</span>"
        size = (
            f"{s['shares']} шт · ставка ${_nf(s['stake'], 2)}<br>якщо вгадав: виплата ${_nf(s['payout'])} "
            f"(+${_nf(s['payout'] - s['stake'], 2)})"
        )
        out.append(
            f"<tr><td>{_e(_clean(s.get('asset') or '—', 8))}</td><td class='q'>{q}</td>"
            f"<td><b>{side}</b></td><td class='num'>{s['ask']:.3f}</td>"
            f"<td class='num'>{100 * s['p_k1']:.1f} % · {100 * s['p_k092']:.1f} %</td>"
            f"<td class='num ok'><b>{_pp(s['edge'])}</b></td><td class='num'>{_usd(s['depth_usd'])}</td>"
            f"<td class='num'>{_hours_left((s['end_ts'] - now) / 3600)}</td><td class='num'>{_e(_pm_expiry(s))}</td>"
            f"<td class='num'>{rr:.2f}{flag}</td><td class='num'>{size}</td><td>{record}</td></tr>"
        )
    out.append("</table></div>")
    return "".join(out)


def render_polymarket(data: dict | None, err: str | None, now: float, path: Path | None = None) -> str:
    """The «Polymarket» tab. Never raises: no database, no table, an empty or a stale snapshot gives an explained state."""
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{5 * REFRESH_S}'><title>tradedesk — Polymarket</title>"
        f"<style>{CSS}{BRIEF_CSS}{JOURNAL_CSS}{PM_CSS}</style></head><body>"
    )
    title = "<h1>Polymarket ↔ опціони</h1>"
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in PM_CAVEATS) + "</ul></div>"
    where = f" <code>{_e(path)}</code>" if path else ""
    try:
        if data is None:
            msg = (
                f"Бази ще немає{where}: вона з'являється, коли MCP-сервер або колектор її створять."
                if err == "missing"
                else f"Базу не вдалося прочитати{where}: {_e(err)}."
            )
            return head + f"<header>{title}<span class='bad'>{msg}</span></header>{_nav('polymarket')}{caveats}</body></html>"
        if data.get("rows") is None or data.get("ts") is None:
            msg = (
                "Таблиці pm_gap_snapshot у базі ще немає (схема v15)."
                if data.get("rows") is None
                else "Знімків ще немає: завдання колектора pm_gap (TRADEDESK_PM_GAP_ENABLED=1) ще не відпрацювало."
            )
            return head + f"<header>{title}<span class='warn'>{_e(msg)}</span></header>{_nav('polymarket')}{caveats}</body></html>"
        ts = int(data["ts"])
        age = now - ts
        cls = "bad" if age > PM_STALE_S else "mut"
        stamp = f"<span class='{cls}'>Знімок {_e(kyiv(datetime.fromtimestamp(ts, timezone.utc)))} за Києвом · {_e(fmt_age(age))}"  # noqa: UP017
        if age > PM_STALE_S:
            stamp += " · старший за 2 год: колектор pm_gap, схоже, зупинився, ціни неактуальні"
        stamp += "</span>"
        v = pm_gap_view(data, now)
        out = [head, f"<header>{title}{stamp}</header>{_nav('polymarket')}{caveats}<main class='brief'>"]
        notes = [
            f"У знімку ринків: {v['n_rows']}; пройшли відбір реєстрації (модель ok, страйк у котируваннях Deribit, прийнятий "
            f"текст правил, 2 год – 14 дн до кінця): {v['n_eligible']}. Бік показано, якщо аск у [0.02; 0.98] і на аску "
            f"≥ ${PM_MIN_DEPTH_USD:.0f}; edge = менша з двох ймовірностей моделі − (аск + верхня межа комісії).",
        ]
        if not v["n_synced"]:
            notes.append("Синхронність книги й поверхні Deribit (≤ 60 с) не перевірено: колектор ще не пише book_ts / surface_ts.")
        if v["n_short"]:
            notes.append(f"Боків, чиї ринки зараз ближче ніж за 2 год до кінця (правило там уже не входить), не показано: {v['n_short']}.")
        if v["n_bad"]:
            notes.append(f"Рядків з несподіваним форматом пропущено: {v['n_bad']}.")
        stale = age > PM_STALE_S
        n_low = sum(1 for s in v["pass"] if s["rr"] < PM_MIN_RR)
        out.append(f"<section class='wide'>{_h2('Проходять поріг (≥ 5 п.п.)', 'pm', CHIP_HINT)}")
        if v["pass"]:
            table = block_pm_table(v["pass"], now)
            if n_low:
                low = f"R:R нижче 2 (правило 5 власника): {n_low} з {len(v['pass'])}. Дорогий бік: мала виплата на великий ризик."
                table += f"<p class='note warn'>{_e(low)}</p>"
            if stale:  # old prices: kept for reference, never presented as entries
                table = (
                    f"<details class='near'><summary>Ціни застарілі (знімок старший за 2 год): це не сигнали · рядків "
                    f"{len(v['pass'])}</summary>{table}</details>"
                )
            out.append(table)
        else:
            out.append(_empty("Зараз жоден бік не проходить поріг 5 п.п. з усіма фільтрами."))
        out.append("".join(f"<p class='note'>{_e(x)}</p>" for x in notes))
        out.append("</section>")
        near = block_pm_table(v["near"], now) if v["near"] else _empty("Немає.")
        old = " · ціни застарілі" if stale else ""
        out.append(
            "<section class='wide'><details class='near'><summary>Близько (3–5 п.п.) — нижче порогу реєстрації, не сигнал"
            f"{old}: {len(v['near'])}{_info('pm_near')}</summary>{near}</details></section>"
        )
        out.append("</main></body></html>")
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header>{title}<span class='bad'>Розриви не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('polymarket')}{caveats}</body></html>"
        )


def polymarket_page(now: float | None = None) -> str:
    data, err = load_pm_gap(PM_DB)
    return with_help(render_polymarket(data, err, time.time() if now is None else now, PM_DB))


# ---------------------------------------------------------------- «Демо» tab: the demo trader's book (read-only)
#
# BACKLOG 0.5 (a) «блок на панелі» and Phase 4.4 «Вкладка «Демо»»: the demo trader's own trades (demo_trade, schema
# v19), R by setup with a t 95 % CI against 0, and the decisions it did not take (exec_audit kinds 'refused' /
# 'skipped') grouped by reason. Read-only SQLite, no network. The «demo vs backtest» replay needs public candles, so it
# is not computed here: the block reads the JSON file scripts/demo_vs_backtest.py --json writes daily (with the
# missed / skipped signals against their shadow plans) and explains its absence.

DEMO_DB = JOURNAL_DB  # execution.demo_trader.DEFAULT_DB: the same store
DEMO_SETUPS = ("S14", "S16", "S3")  # execution.demo_trader.SETUPS
DEMO_MIN_N = 30  # analytics.demo_vs_backtest.MIN_TRADES (owner 23.10: no conclusion before ~30 closed trades)
DEMO_MAX_ROWS = 20000
DEMO_AUDIT_MAX_ROWS = 20000
DEMO_TABLE_ROWS = 20
DEMO_REASONS_SHOWN = 15
DEMO_WEEK_MS = 7 * 86_400_000
DEMO_OPEN = ("open", "pending", "unknown")
DEMO_VS_BT_FLAG = DATA_DIR / "assistant" / "demo-vs-backtest-telegram.on"  # scripts/server/29-demo-vs-backtest-weekly-timer.sh
# scripts/demo_vs_backtest.py --json, written daily by deploy/user-units/tradedesk-demo-vs-backtest-daily.*
DEMO_VS_BT_JSON = DATA_DIR / "demo-vs-backtest" / "latest.json"
DEMO_VS_BT_SCHEMA = 2  # scripts/demo_vs_backtest.JSON_SCHEMA (a test pins it)
DEMO_VS_BT_STALE_S = 36 * 3600  # the job runs daily: older than this means it stopped or failed
DEMO_VS_BT_MAX_BYTES = 50_000_000
DEMO_NE_GROUPS = (  # analytics.demo_shadow.GROUPS
    ("missed", "Вхід не заповнився"),
    ("skipped", "Не відібрано"),
    ("refused", "Відмова guard"),  # since 24.10; an older schema-2 file has no such group (the block says so)
    ("taken", "Виконані (для порівняння)"),
)
# analytics.setup_versions.EPOCHS (stdlib copy, a test compares the pair): (tag, from unix ms UTC, what changed, source,
# setups it applies to; () = every setup). A trade's version follows its open time; changes of which signals are taken
# (daily cap, option leg) are not versions.
DEMO_EPOCHS: tuple[tuple[str, int, str, str, tuple[str, ...]], ...] = (
    (
        "v1",
        1_792_607_957_000,  # 2026-10-21 18:39:17 UTC
        "запуск: ринковий вхід зі стопом і ціллю на біржі, беззбиток за +30 % ROE на плечі угоди",
        "de3540f, docs/changes/2026-10-21-demo-trader.md",
        (),
    ),
    (
        "v2",
        1_792_649_599_000,  # 2026-10-22 06:13:19 UTC
        "мейкерські лімітні вхід і ціль, ринок лише за потреби (беззбиток ще за +30 % ROE)",
        "8ec1f37, docs/changes/2026-10-22-demo-maker-orders.md",
        (),
    ),
    (
        "v3",
        1_792_653_159_000,  # 2026-10-22 07:12:39 UTC
        "беззбиток на половині шляху до цілі замість +30 % ROE",
        "8d82847, docs/changes/2026-10-22-demo-breakeven-path.md",
        (),
    ),
)
DEMO_STATUS_UA = {
    "open": "відкрита",
    "pending": "чекає входу",
    "unknown": "невідомо (до відповіді біржі)",
    "closed": "закрита",
    "missed": "вхід не заповнився",
    "rejected": "біржа відхилила",
}
DEMO_EXIT_UA = {"target": "ціль", "stop": "стоп", "breakeven": "беззбиток", "time": "за часом"}
DEMO_CAVEATS = (
    "Демо-рахунок Bybit (api-demo.bybit.com), ігрові гроші. Сторінка лише читає базу: нічого не торгує й лімітів не змінює.",
    f"R = P&L біржі (з комісіями) / ризик на стопі з обома тейкер-комісіями. До {DEMO_MIN_N} закритих угод сетапу числа "
    "описові, не доказ переваги.",
    "Правила змінювались (22.10: мейкерські ордери, беззбиток на половині шляху до цілі замість +30 % ROE): плитки й «R за "
    "сетапом» змішують версії, окремо за версіями — у блоці «Версії сетапів».",
    "Минулий результат не гарантує майбутнього. Не інвестиційна порада.",
)
# guard / selection messages (execution/guard.py check, demo_trader._select_and_enter) -> a Ukrainian reason; the
# numbers of each message differ, so they are grouped by the rule, with the rule's threshold where it is in the text
_DEMO_REASONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p), ua)
    for p, ua in (
        (r"^score \d+ < (\d+)$", "бал сигналу нижче {0}"),
        (r"^reward/risk .* after fees < ([\d.]+)$", "R:R після комісій нижче {0}"),
        (r"^stop .* from the entry > (\d+%)$", "стоп далі {0} від входу"),
        (r"^already in ", "уже є позиція в цій монеті"),
        (r"is not in today's universe$", "монети немає в сьогоднішньому списку"),
        (r"^levels:", "стоп і ціль не по різні боки входу"),
        (r"^lower rank$", "обрано сильніший сигнал"),
        (r"trades at risk today = max$", "денний ліміт угод під ризиком"),
        (r"entries today = max$", "денний ліміт входів"),
        (r"orders in the last hour = max$", "ліміт ордерів за годину"),
        (r"open positions = max$", "максимум відкритих позицій"),
        (r"^kill-switch is on$", "kill-switch увімкнено"),
        (r"^open risk ", "відкритий ризик понад ліміт"),
        (r"^realised loss today ", "денний ліміт збитку"),
        (r"^this signal was already decided$", "сигнал уже вирішено"),
        (r"^maintenance \+ loss at all stops ", "крос-маржа: підтримка + збиток на стопах понад ліміт"),
        (r"^margin .* of equity$", "застава понад частку капіталу"),
        (r"^open size ", "сукупний розмір позицій понад ліміт"),
        (r"^notional ", "розмір позиції понад ліміт"),
        (r"^risk .* USD > ", "ризик угоди понад ліміт"),
        (r"^leverage ", "плече поза межами"),
        (r"^size is zero", "розмір нульовий (крок лота)"),
        (r"^equity unknown", "капітал невідомий"),
        (r"^mode ", "режим не дозволено"),
        (r"^side ", "невідома сторона"),
        # why a maker entry was cancelled ('missed', execution/orders.py entry_step, demo_trader._sync_pending)
        (r"^price at the stop before the fill", "ціна дійшла до стопу до заповнення"),
        (r"^price ran .* from the planned entry \(> ([\d.]+R)\)$", "ціна втекла від плану далі {0}"),
        (r"^reward/risk at the market .* < ([\d.]+)$", "R:R за ринком нижче {0}"),
        (r"^the venue has no such order$", "біржа не знає ордера"),
        (r"no position after the fill$", "після заповнення позиції немає"),
    )
)


def demo_reason(v: object) -> str:
    """A Ukrainian label of one guard / selection message (untrusted text from exec_audit); an unknown one is kept with
    its numbers and pair symbols masked, so the same rule groups together. Plain text: escape it when rendering."""
    s = "" if v is None else _clean(v, 200)
    if not s:
        return "причину не записано"
    for rx, ua in _DEMO_REASONS:
        m = rx.search(s)
        if m:
            return ua.format(*m.groups())
    s = re.sub(r"\b[A-Z0-9]{1,20}/[A-Z0-9]{1,10}(?::[A-Z0-9]{1,10})?", "<символ>", s)
    return re.sub(r"\d[\d,]*(?:\.\d+)?%?", "N", s)[:80]


def load_demo(path: Path) -> tuple[dict | None, str | None]:
    """{'trades': demo_trade rows | None, 'audit': exec_audit 'refused' / 'skipped' rows | None, 'option_legs':
    {status: count} of demo_option_leg | None} (None = no such table, an older store), or (None, reason) with reason
    'missing' | 'unreadable: …'. Read-only connection; never raises. Dry-run rows (kinds 'dry_*') are not read."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out: dict = {"trades": None, "audit": None, "option_legs": None}
        if "demo_trade" in names:
            out["trades"] = [
                dict(r)
                for r in con.execute(
                    "SELECT symbol, side, setup, timeframe, signal_ts, status, opened_ts, closed_ts, pnl_usd, r_multiple,"
                    " exit_reason FROM demo_trade ORDER BY COALESCE(closed_ts, opened_ts, signal_ts) DESC LIMIT ?",
                    (DEMO_MAX_ROWS,),
                )
            ]
        if "exec_audit" in names:
            out["audit"] = [
                dict(r)
                for r in con.execute(
                    "SELECT ts, kind, setup, payload FROM exec_audit WHERE kind IN ('refused', 'skipped')"
                    " ORDER BY ts DESC, id DESC LIMIT ?",
                    (DEMO_AUDIT_MAX_ROWS,),
                )
            ]
        if "demo_option_leg" in names:
            out["option_legs"] = {str(r[0]): int(r[1]) for r in con.execute("SELECT status, COUNT(*) FROM demo_option_leg GROUP BY status")}
        return out, None
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"
    finally:
        con.close()


def load_demo_vs_bt(path: Path) -> tuple[dict | None, str | None]:
    """The JSON of scripts/demo_vs_backtest.py --json, or (None, reason) with reason 'missing' | 'too_big' | 'corrupt' |
    'old_schema' | 'unreadable: …'. Read-only, no network; never raises."""
    try:
        if path.stat().st_size > DEMO_VS_BT_MAX_BYTES:
            return None, "too_big"
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"unreadable: {type(e).__name__}"
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "corrupt"
    if not isinstance(doc, dict) or not isinstance(doc.get("summary"), dict):
        return None, "corrupt"
    if doc.get("schema") != DEMO_VS_BT_SCHEMA:
        return None, "old_schema"
    return doc, None


def demo_r_stats(rs: list[float]) -> dict:
    """n, share of R > 0, mean R and its t 95 % CI (from 2 values), as plan_stats does for the plans."""
    s: dict = {"n": len(rs), "win_rate": None, "mean_r": None, "ci95": None}
    if rs:
        s["win_rate"] = sum(1 for v in rs if v > 0) / len(rs)
        s["mean_r"] = statistics.fmean(rs)
    if len(rs) >= 2:
        h = t975(len(rs) - 1) * statistics.stdev(rs) / len(rs) ** 0.5
        s["ci95"] = [s["mean_r"] - h, s["mean_r"] + h]
    return s


def demo_setup_stats(trades: list[dict]) -> dict[str, dict]:
    """Per setup (S14, S16, S3 always, then any other in the store): closed trades, R stats of those with R, P&L USD."""
    out: dict[str, dict] = {}
    names = list(DEMO_SETUPS) + sorted({str(t.get("setup")) for t in trades} - set(DEMO_SETUPS))
    for name in names:
        closed = [t for t in trades if str(t.get("setup")) == name and t.get("status") == "closed"]
        rs = [v for t in closed if (v := _num(t.get("r_multiple"))) is not None]
        s = demo_r_stats(rs)
        s.update(closed=len(closed), pnl=sum(_num(t.get("pnl_usd")) or 0.0 for t in closed))
        out[name] = s
    return out


def _demo_epoch_applies(e: tuple, setup: str | None) -> bool:
    return not e[4] or setup in e[4]


def demo_version_of(setup: str | None, open_ms: float | None, now_ms: float, epochs: tuple = DEMO_EPOCHS) -> tuple | None:
    """Copy of analytics.setup_versions.version_of: the latest epoch of the setup started by both the open time and
    now (the exact epoch minute is in the new version); None without an open time or before the first epoch."""
    if open_ms is None:
        return None
    best = None
    for e in epochs:
        if _demo_epoch_applies(e, setup) and e[1] <= open_ms and e[1] <= now_ms and (best is None or e[1] >= best[1]):
            best = e
    return best


def demo_version_stats(trades: list[dict], now_ms: float, epochs: tuple = DEMO_EPOCHS, min_n: int = DEMO_MIN_N) -> dict:
    """Copy of analytics.setup_versions.version_stats (same keys; a test compares the pair): per (setup, version) closed
    trades, R > 0 share, mean R, P&L USD, the trades a later epoch began inside, and the «замало для висновку» verdict
    below ``min_n`` closed trades with R."""
    eps = sorted((e for e in epochs if e[1] <= now_ms), key=lambda e: e[1])
    groups: dict = {}
    for t in trades:
        if t.get("status") != "closed":
            continue
        closed_ms = _num(t.get("closed_ts"))
        if closed_ms is not None and closed_ms > now_ms:
            continue
        raw = t.get("setup")
        setup = ("" if raw is None else str(raw).strip()) or None
        e = demo_version_of(setup, _num(t.get("opened_ts")), now_ms, eps)
        tag = None if e is None else e[0]
        g = groups.setdefault(
            (setup, tag),
            {"setup": setup, "version": tag, "from_ms": None if e is None else e[1], "rs": [], "closed": 0, "pnl": 0.0, "inside": 0},
        )
        g["closed"] += 1
        g["pnl"] += _num(t.get("pnl_usd")) or 0.0
        r = _num(t.get("r_multiple"))
        if r is not None:
            g["rs"].append(r)
        if e is not None and closed_ms is not None:
            later = [x[1] for x in eps if _demo_epoch_applies(x, setup) and x[1] > e[1]]
            if later and closed_ms > min(later):
                g["inside"] += 1
    rows = []
    for g in groups.values():
        rs = g["rs"]
        wins = sum(1 for v in rs if v > 0)
        rows.append(
            {
                "setup": g["setup"],
                "version": g["version"],
                "from_ms": g["from_ms"],
                "closed": g["closed"],
                "n_r": len(rs),
                "wins": wins,
                "win_share": wins / len(rs) if rs else None,
                "mean_r": statistics.fmean(rs) if rs else None,
                "pnl_usd": g["pnl"],
                "changed_inside": g["inside"],
                "enough": len(rs) >= min_n,
                "verdict": f"замало для висновку ({len(rs)} з {min_n})" if len(rs) < min_n else "достатньо угод для висновку",
            }
        )
    rows.sort(key=lambda x: (x["setup"] is None, x["setup"] or "", x["from_ms"] is None, x["from_ms"] or 0))
    return {
        "rows": rows,
        "epochs": [{"tag": e[0], "from_ms": e[1], "what": e[2], "source": e[3], "setups": list(e[4])} for e in eps],
        "unversioned": sum(x["closed"] for x in rows if x["version"] is None),
        "min_n": min_n,
    }


def demo_refusals(audit: list[dict], now_ms: float) -> list[dict]:
    """exec_audit 'refused' / 'skipped' rows grouped by reason: {'reason', 'refused', 'skipped', 'week', 'last_ts'},
    most frequent first. A row counts once per reason even when its message list repeats it."""
    groups: dict[str, dict] = {}
    for a in audit:
        try:
            p = json.loads(a.get("payload") or "{}")
        except (TypeError, ValueError):
            p = {}
        vs = p.get("violations") if isinstance(p, dict) else None
        labels = {demo_reason(v) for v in vs} if isinstance(vs, list) and vs else {"причину не записано"}
        ts = _num(a.get("ts")) or 0.0
        kind = "refused" if a.get("kind") == "refused" else "skipped"
        for lab in labels:
            g = groups.setdefault(lab, {"reason": lab, "refused": 0, "skipped": 0, "week": 0, "last_ts": 0.0})
            g[kind] += 1
            g["week"] += ts >= now_ms - DEMO_WEEK_MS
            g["last_ts"] = max(g["last_ts"], ts)
    return sorted(groups.values(), key=lambda g: (-(g["refused"] + g["skipped"]), g["reason"]))


def _demo_vs0(ci: object, n: int) -> tuple[str, str]:
    if not isinstance(ci, list):
        return ("угод з R ще немає" if not n else "ДІ — від 2 угод з R"), ""
    if ci[0] > 0:
        return "95 % ДІ вище 0", "ok"
    if ci[1] < 0:
        return "95 % ДІ нижче 0", "bad"
    return "95 % ДІ містить 0: переваги не видно", ""


def _demo_no_table(name: str) -> str:
    return _empty(
        f"Таблиці {name} у базі ще немає: база до схеми v19 (з'явиться, коли демо-трейдер або MCP-сервер відкриють її новим кодом)."
    )


def block_demo_tiles(trades: list[dict]) -> str:
    if not trades:
        return _empty("Демо-угод ще немає: демо-трейдер записує угоду перед першим ордером (таймер tradedesk-demo-trader, щогодини).")
    by = {k: sum(1 for t in trades if t.get("status") == k) for k in (*DEMO_OPEN, "closed", "missed", "rejected")}
    closed = [t for t in trades if t.get("status") == "closed"]
    exits = {k: sum(1 for t in closed if t.get("exit_reason") == k) for k in DEMO_EXIT_UA}
    exits_s = " · ".join(f"{DEMO_EXIT_UA[k]} {n}" for k, n in exits.items() if n) or "причини виходу не записані"
    n_open = sum(by[k] for k in DEMO_OPEN)
    pnl = sum(_num(t.get("pnl_usd")) or 0.0 for t in closed)
    s = demo_r_stats([v for t in closed if (v := _num(t.get("r_multiple"))) is not None])
    vs0, cls = _demo_vs0(s["ci95"], s["n"])
    tiles = [
        _tile("Закрито", _nf(by["closed"]), _e(exits_s), help="demo_closed"),
        _tile("Відкрито", _nf(n_open), f"з них чекають входу {by['pending']}" if by["pending"] else "позиції на демо", help="demo_open"),
        _tile(
            "Не виконано",
            _nf(by["missed"] + by["rejected"]),
            f"вхід не заповнився {by['missed']} · біржа відхилила {by['rejected']}",
            help="demo_missed",
        ),
        _tile(
            "P&L демо", f"<span class='{_signed_cls(pnl)}'>{_usd(pnl, True)}</span>", "USD, закриті угоди, ігрові гроші", help="demo_pnl"
        ),
        _tile(
            "Середній R, усі сетапи",
            f"<span class='{_signed_cls(s['mean_r'])}'>{_r(s['mean_r'])}</span>",
            f"угод з R {s['n']} · 95 % ДІ {_ci(s['ci95'])} · <span class='{cls}'>{_e(vs0)}</span>",
            help="demo_r_all",
        ),
    ]
    return f"<div class='tiles'>{''.join(tiles)}</div>"


def demo_option_legs_note(legs: object) -> str:
    """The futures tiles leave the option legs out (demo_option_leg, a separate paired test): say so, with their count."""
    if isinstance(legs, dict) and legs:
        n = sum(v for v in legs.values() if isinstance(v, int))
        closed = legs.get("closed", 0) if isinstance(legs.get("closed"), int) else 0
        count = f"У базі опціонних ніг: {n} (закрито {closed}). "
    elif isinstance(legs, dict):
        count = "Опціонних ніг у базі ще немає. "
    else:
        count = "Таблиці demo_option_leg у базі ще немає. "
    return (
        "<p class='note warn'>Лише ф'ючерсні угоди (demo_trade): опціонні ноги демо-трейдера (demo_option_leg) не враховано ні в "
        f"лічильниках, P&amp;L і R, ні серед відмов нижче (рішення option_skipped). {_e(count)}"
        "Парне порівняння ф'ючерс / опціон — у plan_review → demo_option_pairs.</p>"
    )


def block_demo_setups(trades: list[dict]) -> str:
    stats = demo_setup_stats(trades)
    tiles = []
    for name, s in stats.items():
        vs0, cls = _demo_vs0(s["ci95"], s["n"])
        few = s["n"] < DEMO_MIN_N
        sub = (
            f"закрито {s['closed']} · з R {s['n']} · у плюсі {_share(s['win_rate'])} · 95 % ДІ {_ci(s['ci95'])} · "
            f"<span class='{cls}'>{_e(vs0)}</span> · P&L {_usd(s['pnl'], True)}"
        )
        tiles.append(
            _tile(
                f"{_e(_clean(name, 12))} · середній R",
                f"<span class='{_signed_cls(s['mean_r'])}'>{_r(s['mean_r'])}</span>",
                sub,
                help="demo_setup",
                tone="warn" if few else "",
                badge=f"замало для висновку ({s['n']} з {DEMO_MIN_N})" if few else "",
            )
        )
    note = (
        f"<p class='note warn'>Висновок про сетап — не раніше {DEMO_MIN_N} закритих угод з R; до того середній R і ДІ лише описують "
        "те, що вже сталось.</p>"
    )
    return f"<div class='tiles'>{''.join(tiles)}</div>{note}"


def _demo_at(ms: object) -> str:
    v = _num(ms)
    return "—" if v is None else kyiv(datetime.fromtimestamp(v / 1000, timezone.utc))  # noqa: UP017


def block_demo_versions(trades: list[dict], now: float) -> str:
    """Closed demo trades per (setup, version) of the rules (demo_version_stats), then the table of the rule epochs."""
    s = demo_version_stats(trades, now * 1000)
    epochs = [
        "<div class='scroll'><table><tr><th>Версія</th><th>Діє з (Київ)</th><th>Що змінилось</th><th>Джерело</th></tr>",
        *(
            f"<tr><td>{_e(e['tag'])}</td><td>{_e(_demo_at(e['from_ms']))}</td><td>{_e(e['what'])}</td><td class='mut'>{_e(e['source'])}</td></tr>"
            for e in s["epochs"]
        ),
        "</table></div>",
    ]
    how = (
        f"<p class='note'>Зміна правил ведення угоди = нова версія; висновок про версію — не раніше {DEMO_MIN_N} закритих угод з R. "
        "Демо-трейдер версії поки не записує, тож версію визначено за часом відкриття угоди. Час зміни — час коміту (демо-трейдер "
        "працює з тієї самої копії коду), фактичний час розгортання не записано. Зміни відбору сигналів (денний ліміт угод під "
        "ризиком, опціонна нога) версією не вважаються: вони змінюють, які сигнали беруться, а не як ведеться угода.</p>"
    )
    if not s["rows"]:
        return _empty("Закритих демо-угод ще немає: статистика за версіями з'явиться після першого закриття.") + "".join(epochs) + how
    rows = [
        "<div class='scroll'><table><tr><th>Сетап</th><th>Версія</th><th>Діє з (Київ)</th><th>Закрито</th><th>З R</th><th>У плюсі</th>"
        "<th>Середній R</th><th>P&amp;L, USD</th><th>Правила змінились під час угоди</th><th>Висновок</th></tr>"
    ]
    for x in s["rows"]:
        mr = _num(x["mean_r"])
        setup = "без сетапу" if x["setup"] is None else _clean(x["setup"], 12)
        rows.append(
            f"<tr><td>{_e(setup)}</td><td>{_e(x['version'] or 'без версії')}</td><td>{_e(_demo_at(x['from_ms']))}</td>"
            f"<td class='num'>{x['closed']}</td><td class='num'>{x['n_r']}</td><td class='num'>{_share(x['win_share'])}</td>"
            f"<td class='num {_signed_cls(mr)}'>{_r(mr)}</td><td class='num {_signed_cls(x['pnl_usd'])}'>{_usd(x['pnl_usd'], True)}</td>"
            f"<td class='num'>{x['changed_inside']}</td><td class='{'' if x['enough'] else 'warn'}'>{_e(x['verdict'])}</td></tr>"
        )
    rows.append("</table></div>")
    out = ["".join(rows)]
    inside = sum(x["changed_inside"] for x in s["rows"])
    if inside:
        out.append(
            f"<p class='note warn'>{inside} закритих угод були відкриті, коли почалася наступна версія: демо-трейдер вів їх далі вже за "
            "новими правилами, тож їхній результат змішує дві версії.</p>"
        )
    if s["unversioned"]:
        out.append(
            f"<p class='note warn'>Без версії: {s['unversioned']} закритих угод (немає часу відкриття або відкриті до першої версії).</p>"
        )
    return "".join(out) + "".join(epochs) + how


def block_demo_table(trades: list[dict]) -> str:
    if not trades:
        return _empty("Угод ще немає.")
    rows = [
        "<div class='scroll'><table><tr><th>Сигнал</th><th>Закрито</th><th>Монета</th><th>Сетап</th><th>Сторона</th><th>Стан</th>"
        "<th>Вихід</th><th>R</th><th>P&amp;L, USD</th></tr>"
    ]
    for t in trades[:DEMO_TABLE_ROWS]:
        r = _num(t.get("r_multiple"))
        pnl = _num(t.get("pnl_usd"))
        status = str(t.get("status"))
        rows.append(
            f"<tr><td>{_e(_day(t.get('signal_ts')))}</td><td>{_e(_day(t.get('closed_ts')))}</td>"
            f"<td>{_e(_clean(str(t.get('symbol') or '—').split(':')[0], 24))}</td>"
            f"<td>{_e(_clean(t.get('setup'), 12))} {_e(_clean(t.get('timeframe'), 6))}</td>"
            f"<td>{_e(SIDE_UA.get(str(t.get('side')), _clean(t.get('side'), 8)))}</td>"
            f"<td>{_e(DEMO_STATUS_UA.get(status, _clean(status, 20)))}</td>"
            f"<td>{_e(DEMO_EXIT_UA.get(str(t.get('exit_reason')), '—'))}</td>"
            f"<td class='num {_signed_cls(r)}'>{_r(r)}</td><td class='num {_signed_cls(pnl)}'>{_usd(pnl, True) if pnl is not None else '—'}</td></tr>"
        )
    rows.append("</table></div>")
    if len(trades) > DEMO_TABLE_ROWS:
        rows.append(f"<p class='note'>Показано {DEMO_TABLE_ROWS} останніх з {len(trades)}.</p>")
    return "".join(rows)


def block_demo_refusals(audit: list[dict] | None, now: float) -> str:
    if audit is None:
        return _demo_no_table("exec_audit")
    n_ref = sum(1 for a in audit if a.get("kind") == "refused")
    n_skip = len(audit) - n_ref
    tiles = [
        _tile("Відмов guard", _nf(n_ref), "сигнал порушив ліміт або правило guard", help="demo_refused"),
        _tile("Не відібрано", _nf(n_skip), "низький бал, денні ліміти або сильніший сигнал", help="demo_skipped"),
    ]
    out = [f"<div class='tiles'>{''.join(tiles)}</div>"]
    if not audit:
        out.append(_empty("Відмов і пропусків ще не було."))
        return "".join(out)
    groups = demo_refusals(audit, now * 1000)
    table = [
        "<div class='scroll'><table><tr><th>Причина</th><th>Відмова guard</th><th>Не відібрано</th><th>За 7 днів</th><th>Останній раз</th></tr>"
    ]
    for g in groups[:DEMO_REASONS_SHOWN]:
        table.append(
            f"<tr><td>{_e(g['reason'])}</td><td class='num'>{g['refused']}</td><td class='num'>{g['skipped']}</td>"
            f"<td class='num'>{g['week']}</td><td>{_e(_day(g['last_ts']))}</td></tr>"
        )
    table.append("</table></div>")
    out.append("".join(table))
    if len(groups) > DEMO_REASONS_SHOWN:
        out.append(f"<p class='note'>Показано {DEMO_REASONS_SHOWN} найчастіших причин з {len(groups)}.</p>")
    out.append(
        "<p class='note'>Одне рішення може мати кілька причин, тож сума за причинами більша за кількість рішень. "
        "Що дали б тіньові плани не відібраних і відхилених сигналів — у блоці «Демо проти бектесту» нижче.</p>"
    )
    if len(audit) >= DEMO_AUDIT_MAX_ROWS:
        out.append(f"<p class='note warn'>Прочитано лише {DEMO_AUDIT_MAX_ROWS} останніх рішень.</p>")
    return "".join(out)


DEMO_VS_BT_ERR_UA = {
    "missing": "файла звірки ще немає",
    "too_big": "файл звірки завеликий, його не читаю",
    "corrupt": "файл звірки пошкоджений або має несподіваний формат",
    "old_schema": "файл звірки старого формату (буде перезаписаний наступним запуском)",
}


def _demo_vs_bt_file_note(doc_err: str | None) -> str:
    why = DEMO_VS_BT_ERR_UA.get(str(doc_err), f"файл звірки не вдалося прочитати ({_clean(doc_err, 60)})")
    return (
        f"<p class='note warn'>Чисел звірки тут немає: {_e(why)} (<code>{_e(DEMO_VS_BT_JSON)}</code>). Звірці потрібні публічні "
        "свічки Bybit, а панель працює без мережі, тож вона лише читає файл, який щодня о 07:40 за Києвом пише user-таймер "
        "<code>tradedesk-demo-vs-backtest-daily</code>. Вручну: <code>uv run python scripts/demo_vs_backtest.py --json "
        f"{_e(DEMO_VS_BT_JSON)}</code>.</p>"
    )


def _demo_vs_bt_summary(doc: dict, now: float) -> str:
    s = doc["summary"]
    c = s.get("counts") if isinstance(s.get("counts"), dict) else {}
    decided = int(_num(s.get("decided")) or 0)
    n = int(_num(s.get("n")) or 0)
    min_n = int(_num(s.get("min_n")) or DEMO_MIN_N)
    few = not s.get("enough")
    gen = _num(doc.get("generated_ms"))
    age = None if gen is None else now - gen / 1000
    at = None if gen is None else datetime.fromtimestamp(gen / 1000, timezone.utc)  # noqa: UP017
    stamp = "час звірки не записано" if at is None else f"звірка від {_day(gen)} {kyiv(at, '%H:%M')}"
    old = age is not None and age > DEMO_VS_BT_STALE_S
    undecided = sum(int(_num(c.get(k)) or 0) for k in ("pending", "no_data", "unsupported"))
    md = _num(s.get("mean_r_diff"))
    tiles = [
        _tile(
            "Звірено угод",
            _nf(decided),
            f"із закритих {n} · не вирішено {undecided}",
            help="demo_vs_bt_total",
            tone="warn" if few else "",
            badge=f"замало для висновку ({decided} з {min_n})" if few else "",
        ),
        _tile(
            "Збіг з бектестом",
            _share(s.get("match_share")),
            f"збіг {int(_num(c.get('match')) or 0)} · немає сигналу {int(_num(c.get('no_signal')) or 0)} · розбіжність "
            f"{int(_num(c.get('mismatch')) or 0)}",
            help="demo_vs_bt_total",
        ),
        _tile(
            "Різниця R, демо − бектест",
            f"<span class='{_signed_cls(md)}'>{_r(md)}</span>",
            f"угод з обома R {int(_num(s.get('n_r')) or 0)} · 95 % ДІ {_ci(s.get('ci95_r_diff'))}",
            help="demo_vs_bt_total",
        ),
    ]
    out = [f"<div class='tiles'>{''.join(tiles)}</div>"]
    out.append(f"<p>Висновок: {_e(_clean(s.get('conclusion') or '—', 300))}.</p>")
    cls = "note warn" if old or gen is None else "note"
    late = " — старіша за 36 год: таймер, схоже, не спрацював" if old else ""
    out.append(f"<p class='{cls}'>{_e(stamp)}{_e(late)}; свічки {_e(_clean(doc.get('venue') or '—', 20))}, файл — лише читання.</p>")
    return "".join(out)


def _demo_ne_cells(s: dict) -> str:
    mr = _num(s.get("mean_r"))
    sr = _num(s.get("sum_r"))
    return (
        f"<td class='num'>{int(_num(s.get('n')) or 0)}</td><td class='num'>{int(_num(s.get('n_r')) or 0)}</td>"
        f"<td class='num {_signed_cls(mr)}'>{_r(mr)}</td><td class='num'>{_ci(s.get('ci95_r'))}</td>"
        f"<td class='num {_signed_cls(sr)}'>{_r(sr)}</td>"
    )


def _demo_not_executed(ne: object) -> str:
    """The not-executed block of the file (analytics.demo_shadow.not_executed): groups, then reasons."""
    head = f"<h3>Відібрані, але не виконані сигнали проти тіньових планів{_info('demo_not_executed')}</h3>"
    if not isinstance(ne, dict) or not isinstance(ne.get("groups"), dict):
        return head + _empty("У файлі звірки цього блоку немає.")
    if ne.get("error"):
        return head + _empty(f"Не вдалося прочитати базу для цього блоку: {_clean(ne.get('error'), 80)}.")
    groups = ne["groups"]
    rows = [
        "<div class='scroll'><table><tr><th>Група</th><th>Сигналів</th><th>За 7 днів</th><th>План закрито / відкритий / без плану</th>"
        "<th>З R</th><th>Середній R</th><th>95 % ДІ</th><th>Сума R</th><th>Висновок</th></tr>"
    ]
    reasons = []
    for key, label in DEMO_NE_GROUPS:
        s = groups.get(key)
        if not isinstance(s, dict):
            continue
        n, n_r = int(_num(s.get("n")) or 0), int(_num(s.get("n_r")) or 0)
        few = n_r < DEMO_MIN_N
        verdict = f"замало для висновку ({n_r} з {DEMO_MIN_N})" if few else "достатньо планів для висновку"
        rows.append(
            f"<tr><td>{_e(label)}</td><td class='num'>{n}</td><td class='num'>{int(_num(s.get('week')) or 0)}</td>"
            f"<td class='num'>{int(_num(s.get('resolved')) or 0)} / {int(_num(s.get('open')) or 0)} / {int(_num(s.get('no_plan')) or 0)}</td>"
            f"<td class='num'>{n_r}</td><td class='num {_signed_cls(_num(s.get('mean_r')))}'>{_r(s.get('mean_r'))}</td>"
            f"<td class='num'>{_ci(s.get('ci95_r'))}</td><td class='num {_signed_cls(_num(s.get('sum_r')))}'>{_r(s.get('sum_r'))}</td>"
            f"<td class='{'warn' if few else ''}'>{_e(verdict)}</td></tr>"
        )
        for x in (s.get("reasons") if isinstance(s.get("reasons"), list) else [])[:DEMO_REASONS_SHOWN]:
            if isinstance(x, dict):
                reasons.append(f"<tr><td>{_e(label)}</td><td>{_e(demo_reason(x.get('example') or None))}</td>{_demo_ne_cells(x)}</tr>")
    rows.append("</table></div>")
    out = [head, "".join(rows)]
    if reasons:
        out.append(
            "<div class='scroll'><table><tr><th>Група</th><th>Причина</th><th>Сигналів</th><th>З R</th><th>Середній R</th>"
            f"<th>95 % ДІ</th><th>Сума R</th></tr>{''.join(reasons)}</table></div>"
        )
    refused = groups.get("refused")
    if not isinstance(refused, dict):
        out.append(
            "<p class='note'>Відмов guard у файлі звірки ще немає: його записано до 24.10, наступний щоденний запуск їх додасть.</p>"
        )
    else:
        ex = refused.get("excluded") if isinstance(refused.get("excluded"), dict) else {}
        out.append(
            f"<p class='note'>Відмови guard без тіньового плану (стоп далі від входу, ніж дозволяє guard, або помилка рівнів): "
            f"{int(_num(ex.get('n')) or 0)} (за 7 днів {int(_num(ex.get('week')) or 0)}); рахувати їм R нема з чого, у таблиці "
            "їх немає. Відмова з кількома правилами показана за першим.</p>"
        )
    out.append(
        "<p class='note'>R тіньового плану — в одиницях ризику демо, як «R без беззбитку» бектесту: ринковий вхід за ціною "
        "рішення (до 2 год після закриття сигнальної свічки), 1h-свічки Bybit, стоп виграє свічку, що торкнулась обох рівнів, "
        f"без переносу в беззбиток. Це простий сетап, а не те, що записало б демо. До {DEMO_MIN_N} планів з R у групі — лише "
        "опис.</p>"
    )
    return "".join(out)


def block_demo_vs_bt(
    trades: list[dict], flag_on: bool | None, doc: dict | None = None, doc_err: str | None = "missing", now: float | None = None
) -> str:
    """No replay here (it needs public candles and the panel has no network): the numbers come from the file of
    scripts/demo_vs_backtest.py --json (``DEMO_VS_BT_JSON``, daily user timer); without it the block says why."""
    closed = sum(1 for t in trades if t.get("status") == "closed")
    when = (
        "стан прапорця тижневого повідомлення не вдалося прочитати"
        if flag_on is None
        else "тижневе повідомлення увімкнене"
        if flag_on
        else "тижневе повідомлення поки вимкнене (вмикає власник кроком scripts/server/29-demo-vs-backtest-weekly-timer.sh)"
    )
    weekly = f"<p class='note'>Тижневе повідомлення в Telegram (тема «🧪 Демо-рахунок», неділя 20:30 за Києвом): {_e(when)}.</p>"
    if doc is None:
        return (
            _demo_vs_bt_file_note(doc_err)
            + weekly
            + f"<p class='note'>Закритих демо-угод для звірки: {closed}. Висновок звірки — не раніше {DEMO_MIN_N} звірених угод; до того "
            "«замало для висновку».</p>"
        )
    return _demo_vs_bt_summary(doc, time.time() if now is None else now) + _demo_not_executed(doc.get("not_executed")) + weekly


def render_demo(
    data: dict | None,
    err: str | None,
    now: float,
    path: Path | None = None,
    vs_bt_on: bool | None = None,
    vs_bt: tuple[dict | None, str | None] = (None, "missing"),
) -> str:
    """The «Демо» tab. Never raises: no database, no table or no rows gives an explained empty state. ``vs_bt``: the
    result of load_demo_vs_bt (the reconciliation file)."""
    head = (
        "<!doctype html><html lang='uk'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>tradedesk — демо</title><style>{CSS}{BRIEF_CSS}{JOURNAL_CSS}</style></head><body>"
    )
    title = "<h1>Демо-трейдер</h1>"
    caveats = "<div class='caveat'><b>Застереження</b><ul>" + "".join(f"<li>{_e(c)}</li>" for c in DEMO_CAVEATS) + "</ul></div>"
    where = f" <code>{_e(path)}</code>" if path else ""
    try:
        if data is None:
            msg = (
                f"Бази ще немає{where}: вона з'являється, коли MCP-сервер, колектор або демо-трейдер її створять."
                if err == "missing"
                else f"Базу не вдалося прочитати{where}: {_e(err)}."
            )
            return head + f"<header>{title}<span class='bad'>{msg}</span></header>{_nav('demo')}{caveats}</body></html>"
        stamp = f"<span class='mut'>База{where} · лише читання · прочитано {_e(kyiv(datetime.fromtimestamp(now, timezone.utc)))} за Києвом</span>"  # noqa: UP017
        out = [head, f"<header>{title}{stamp}</header>{_nav('demo')}{caveats}<main class='brief'>"]
        trades = data.get("trades")

        def guarded(fn, *args) -> str:
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
                return _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")

        if trades is None:
            out.append(f"<section class='wide'>{_h2('Демо-угоди', 'demo')}{_demo_no_table('demo_trade')}</section>")
        else:
            out.append(
                f"<section class='wide'>{_h2('Демо-угоди', 'demo', CHIP_LEDGER)}{guarded(block_demo_tiles, trades)}"
                f"{guarded(demo_option_legs_note, data.get('option_legs'))}</section>"
            )
            out.append(f"<section class='wide'>{_h2('R за сетапом', 'demo_setups')}{guarded(block_demo_setups, trades)}</section>")
            out.append(
                f"<section class='wide'>{_h2('Версії сетапів', 'demo_versions')}{guarded(block_demo_versions, trades, now)}</section>"
            )
            out.append(f"<section class='wide'>{_h2('Останні демо-угоди', 'demo_recent')}{guarded(block_demo_table, trades)}</section>")
        out.append(
            f"<section class='wide'>{_h2('Відмови guard за причинами', 'demo_refusals')}{guarded(block_demo_refusals, data.get('audit'), now)}</section>"
        )
        out.append(
            f"<section class='wide'>{_h2('Демо проти бектесту', 'demo_vs_bt')}"
            f"{guarded(block_demo_vs_bt, trades or [], vs_bt_on, vs_bt[0], vs_bt[1], now)}</section>"
        )
        out.append("</main></body></html>")
        return "".join(out)
    except Exception as e:  # noqa: BLE001 - the page always renders
        return (
            head
            + f"<header>{title}<span class='bad'>Демо не вдалося показати ({_e(type(e).__name__)}).</span></header>{_nav('demo')}{caveats}</body></html>"
        )


def demo_page(now: float | None = None) -> str:
    data, err = load_demo(DEMO_DB)
    try:
        flag = DEMO_VS_BT_FLAG.exists()
    except OSError:
        flag = None
    return with_help(render_demo(data, err, time.time() if now is None else now, DEMO_DB, flag, load_demo_vs_bt(DEMO_VS_BT_JSON)))


# ---------------------------------------------------------------- server


# The link with a key (owner 22.10: a permanent link instead of the Tailscale app): requests that came through a proxy
# (tailscale serve / funnel add X-Forwarded-For) need the key once in the link (?k=…), which becomes a year-long cookie;
# direct loopback requests (the server's own scripts, the Mac's SSH tunnel) pass as before.
KEY_FILE = Path.home() / ".config" / "tradedesk" / "dashboard.env"  # DASHBOARD_KEY=…, 0600, made on the first start
KEY_COOKIE = "td_key"
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-funnel-request", "tailscale-user-login")


def dashboard_key(path: Path | None = None, create: bool = False) -> str | None:
    """The dashboard's access key from KEY_FILE; with ``create`` a new random one is written (0600) when missing."""
    path = path or KEY_FILE
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DASHBOARD_KEY=") and len(line.split("=", 1)[1].strip()) >= 20:
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    if not create:
        return None
    key = secrets.token_urlsafe(24)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"DASHBOARD_KEY={key}\n")
    return key


def access(client: str, headers: list[tuple[str, str]], query: str, key: str | None) -> str:
    """'ok', 'set' (the right key in the link: set the cookie and redirect without it) or 'deny'."""
    h: dict[str, str] = {}
    for k, v in headers:
        h.setdefault(k.lower(), v)
    if client in ("127.0.0.1", "::1") and not any(x in h for x in PROXY_HEADERS):
        return "ok"
    if not key:
        return "deny"
    if any(hmac.compare_digest(v.encode(), key.encode()) for v in urllib.parse.parse_qs(query).get("k", [])):
        return "set"
    try:
        morsel = SimpleCookie(h.get("cookie", "")).get(KEY_COOKIE)
    except CookieError:
        morsel = None
    return "ok" if morsel is not None and hmac.compare_digest(morsel.value.encode(), key.encode()) else "deny"


BRAND = Path(__file__).resolve().parent.parent / "docs" / "brand"
# public icons (no key needed: the logo is not secret, and a home-screen icon is fetched without the cookie)
ICONS = {"/favicon.ico": "logo-64.png", "/apple-touch-icon.png": "logo-180.png", "/logo-512.png": "logo-512.png"}


def _logo_uri() -> str:
    """docs/brand/logo.svg as a data URI ('' when the file is missing)."""
    try:
        return "data:image/svg+xml," + urllib.parse.quote((BRAND / "logo.svg").read_text(encoding="utf-8").strip())
    except OSError:
        return ""


def brand(page: str) -> str:
    """The logo on every page: the tab icon, the home-screen icon and a small mark before the first title."""
    uri = _logo_uri()
    if not uri:
        return page
    links = (
        f"<link rel='icon' type='image/svg+xml' href='{uri}'><link rel='icon' type='image/png' href='/favicon.ico'>"
        "<link rel='apple-touch-icon' href='/apple-touch-icon.png'><meta name='theme-color' content='#3a2a9e'>"
    )
    mark = f"<img src='{uri}' alt='' width='22' height='22' style='vertical-align:-3px;margin-right:8px'>"
    return page.replace("<head>", "<head>" + links, 1).replace("<header><h1>", "<header><h1>" + mark, 1)


DENIED_PAGE = (
    "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
    "<p style='font:16px system-ui;margin:2em'>Панель відкривається лише за посиланням з ключем. Попросіть його в Claude.</p>"
)


class _State:
    lock = threading.Lock()
    key: str | None = None
    snap: dict | None = None
    page = "<!doctype html><meta charset='utf-8'><p>Збираю дані…</p><meta http-equiv='refresh' content='5'>"


def _refresh_loop() -> None:
    while True:
        try:
            snap = snapshot()
            page = render(snap)
            with _State.lock:
                _State.snap, _State.page = snap, page
        except Exception as e:  # noqa: BLE001 - keep the last good page
            print(f"dashboard refresh failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(REFRESH_S)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        with _State.lock:
            snap, page = _State.snap, _State.page
        path, _, query = self.path.partition("?")
        if path in ICONS:
            try:
                self._send(200, (BRAND / ICONS[path]).read_bytes(), "image/png")
            except OSError:
                self.send_error(404)
            return
        verdict = access(self.client_address[0], list(self.headers.items()), query, _State.key)
        if verdict == "deny":
            self._send(401, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            return
        if verdict == "set":
            rest = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(query) if k != "k"])
            self.send_response(302)
            self.send_header("Location", path + (f"?{rest}" if rest else ""))
            self.send_header("Set-Cookie", f"{KEY_COOKIE}={_State.key}; Max-Age=31536000; Path=/; Secure; HttpOnly; SameSite=Lax")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            body, ctype = brand(page).encode(), "text/html; charset=utf-8"
        elif path == "/brief":
            body, ctype = brand(brief_page()).encode(), "text/html; charset=utf-8"  # read per request: the age is always current
        elif path == "/options":
            body, ctype = brand(options_page(query)).encode(), "text/html; charset=utf-8"
        elif path == "/radar":
            body, ctype = brand(radar_page(query)).encode(), "text/html; charset=utf-8"
        elif path == "/journal":
            body, ctype = brand(journal_page(query)).encode(), "text/html; charset=utf-8"
        elif path == "/demo":
            body, ctype = brand(demo_page()).encode(), "text/html; charset=utf-8"
        elif path == "/polymarket":
            body, ctype = brand(polymarket_page()).encode(), "text/html; charset=utf-8"
        elif self.path == "/status.json":
            body, ctype = json.dumps(snap or {}, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self._send(200, body, ctype)

    def do_POST(self) -> None:  # noqa: N802
        path, _, _query = self.path.partition("?")
        # the cookie (SameSite=Lax) or loopback only: a key in the link is not accepted for a POST
        if access(self.client_address[0], list(self.headers.items()), "", _State.key) != "ok":
            self._send(401, DENIED_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path != "/journal/refresh":
            self.send_error(404)
            return
        try:
            start_journal_refresh()
        except OSError as e:
            print(f"journal refresh did not start: {type(e).__name__}", file=sys.stderr, flush=True)
        self.send_response(303)
        self.send_header("Location", "/journal")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # quiet
        return


def _watch_self() -> None:
    """Re-exec when this file or its helpers change (the repo is pulled nightly), like the agents loop does."""
    files = [HERE / "dashboard.py", HERE / "agents_status.py", HERE / "results_board.py"]
    stamp = [f.stat().st_mtime for f in files if f.exists()]
    while True:
        time.sleep(60)
        if [f.stat().st_mtime for f in files if f.exists()] != stamp:
            os.execv(sys.executable, [sys.executable, *sys.argv])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8090)
    on = sub.add_parser("once")
    on.add_argument("--out", type=Path, required=True)
    on.add_argument("--brief", action="store_true", help="the «Бриф» tab from the assistant snapshot instead of the project page")
    on.add_argument("--options", action="store_true", help="the «Опціони» tab (default strategy and size) from the snapshot")
    on.add_argument("--radar", default="", metavar="ASSET", help="the «Радар» tab of BTC or ETH from the assistant snapshot")
    on.add_argument("--journal", action="store_true", help="the «Журнал» tab (first page) from the SQLite store, read-only")
    on.add_argument("--polymarket", action="store_true", help="the «Polymarket» tab (current gaps) from the SQLite store, read-only")
    a = ap.parse_args()
    if a.cmd == "once":
        page = (
            brief_page()
            if a.brief
            else options_page()
            if a.options
            else radar_page(urllib.parse.urlencode({"asset": a.radar}))
            if a.radar
            else journal_page()
            if a.journal
            else polymarket_page()
            if a.polymarket
            else render(snapshot())
        )
        a.out.write_text(page, encoding="utf-8")
        print(f"-> {a.out}")
        return
    _State.key = dashboard_key(create=True)
    threading.Thread(target=_refresh_loop, daemon=True).start()
    threading.Thread(target=_watch_self, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), _Handler).serve_forever()  # loopback only; proxied requests need the key


if __name__ == "__main__":
    main()
