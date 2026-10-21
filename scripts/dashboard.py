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

The «Опціони» tab (/options) is a strategy builder on the snapshot's month chain (marks, Δ, IV): sell/buy strangle and
straddle, iron condor; P&L at expiry, breakevens, max profit/loss, risk-neutral probability of profit and the fit to the
owner's risk budget; position greeks, a «through N days» line, a scenario table (incl. IV +5 points), a plain-language
conclusion and an «is vol expensive» block (DVOL percentile, VRP backtest by level). Everything is computed here on a plain GET form; the inline script only remembers deposit, risk
and size in the browser (localStorage) and resubmits on a change.

The «Журнал» tab (/journal) reads the owner's trade journal and the assistant's paper recommendations (rec_paper,
labelled «гіпотеза») from the SQLite store over a read-only connection: summary, by source, the trades page by page.
Every string from the database is untrusted text and is escaped; no database or no table gives an empty state.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
:root{--bg:#f5f7fa;--card:#fff;--fg:#1d2329;--mut:#66717d;--line:#e2e7ec;--acc:#2f6db5;--ok:#2f7a4b;--warn:#b8761d;--bad:#b33a36}
@media (prefers-color-scheme:dark){:root{--bg:#11161c;--card:#1a2129;--fg:#e3e8ee;--mut:#94a0ad;--line:#2b343e;--acc:#6aa3e8;--ok:#5fbf83;--warn:#e0a54a;--bad:#e2716c}}
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
nav.tabs{display:flex;gap:4px;padding:0 18px;border-bottom:1px solid var(--line);margin:0 0 12px}
nav.tabs a{padding:6px 12px;color:var(--mut);text-decoration:none;font-weight:600;border-bottom:3px solid transparent}
nav.tabs a[aria-current=page]{color:var(--fg);border-bottom-color:var(--acc)}
"""


def _nav(active: str) -> str:
    tabs = (("/", "project", "Проєкт"), ("/brief", "brief", "Бриф"), ("/options", "options", "Опціони"), ("/journal", "journal", "Журнал"))
    return (
        "<nav class='tabs'>"
        + "".join(f"<a href='{href}'{' aria-current=page' if key == active else ''}>{label}</a>" for href, key, label in tabs)
        + "</nav>"
    )


def _e(x: object) -> str:
    return html.escape(str(x), quote=True)


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
        f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>tradedesk — панель</title><style>{CSS}</style></head><body>"
    ]
    svc = " ".join(f"<span class='{'ok' if x['state'] == 'active' else 'bad'}'>● {_e(x['label'])}</span>" for x in s["services"])
    out.append(
        f"<header><h1>tradedesk</h1><span class='mut'>оновлено {_when(s['generated_at'])} · сторінка сама оновлюється щохвилини</span><span>{svc}</span></header>{_nav('project')}<main>"
    )

    # Project progress
    pr = s.get("progress")
    if pr:
        out.append("<section class='wide'><h2>Прогрес проєкту</h2>")
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
    out.append("<section><h2>Агенти зараз</h2>")
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
    out.append("<section><h2>Ліміт Claude</h2>")
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
    out.append("<section><h2>Черга</h2><table><tr><th>#</th><th>Картка</th><th>Статус</th><th>Кроки</th></tr>")
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
        ("без вердикту", ("inconclusive",), "mut"),
        ("відхилено", ("rejected",), "bad"),
    ]
    out.append("<section><h2>Результати перевірок</h2><div class='kpi'>")
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
    out.append("<section class='wide'><h2>Кроки агентів</h2><table><tr><th>Коли</th><th>Результат</th><th>Що</th><th>Час і ліміт</th></tr>")
    for h in a["history"]:
        cls = {"delivered": "ok", "error": "bad", "stuck": "bad"}.get(h["status"], "mut")
        out.append(
            f"<tr><td>{_e(h['when'])}</td><td class='{cls}'>{_e(STATUS_UA.get(h['status'], h['status']))}</td><td>{_e(h['text'])}</td><td class='mut'>{_e(h['cost'])}</td></tr>"
        )
    out.append("</table></section>")

    # Server
    gb = 1024**3
    out.append("<section><h2>Сервер</h2>")
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
    out.append("<section><h2>Колектор даних</h2><table><tr><th>Дані</th><th>Останній запис</th><th>Рядків</th></tr>")
    for t in s["tables"]:
        cls = "mut" if t["ok"] is None else ("ok" if t["ok"] else "bad")
        out.append(
            f"<tr><td>{_e(t['label'])}</td><td class='{cls}'>{_e(fmt_age(t['age_s']))}</td><td>{t['rows']:,}".replace(",", " ")
            + "</td></tr>"
        )
    out.append("</table></section>")

    # Commits and questions
    out.append("<section><h2>Останні зміни в main</h2><table>")
    for row in s["commits"]:
        if len(row) == 3:
            out.append(f"<tr><td class='mut'>{_e(row[1])}</td><td>{_e(row[2][:110])}</td></tr>")
    out.append("</table></section>")
    if s["questions"]:
        out.append(
            "<section><h2>Питання агентів до тебе</h2><ul>" + "".join(f"<li>{_e(q)}</li>" for q in s["questions"]) + "</ul></section>"
        )
    out.append("</main></body></html>")
    return "".join(out)


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
}
BRIEF_CAVEATS = (
    "Ймовірності з цін опціонів — ризик-нейтральні: це ціна страхування в ринку, а не прогноз; "
    "реальні ймовірності відрізняються на премію за ризик.",
    "Премія волатильності (VRP) тут рахується за DVOL; перевірка на марках опціонів ще триває (картка vrp-real-marks).",
    "Не інвестиційна порада: сторінка лише показує дані й нічого не торгує.",
)
BRIEF_CSS = """
main.brief{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.tile{border:1px solid var(--line);border-radius:6px;padding:6px 10px;display:flex;flex-direction:column;min-width:0}
.tile .k{font-size:12px;color:var(--mut)}.tile .v{font:600 19px Menlo,Consolas,monospace}.tile .s{font-size:12px;color:var(--mut)}
.num{font-family:Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
ul.key{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}ul.key li{display:grid;grid-template-columns:auto 1fr;gap:10px}
.chip{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;border-radius:999px;padding:1px 8px;white-space:nowrap;height:fit-content}
.chip.calm{background:rgba(47,109,181,.16);color:var(--acc)}.chip.risk{background:rgba(179,58,54,.16);color:var(--bad)}
.chip.opp{background:rgba(47,122,75,.16);color:var(--ok)}.chip.ev{background:rgba(184,118,29,.18);color:var(--warn)}
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


def _tile(k: str, v: str, s: str = "", cls: str = "") -> str:
    return f"<div class='tile'><span class='k'>{k}</span><span class='v'>{v}</span><span class='s {cls}'>{s}</span></div>"


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
            )
        )
    fng = _data(snap, "fng") or {}
    latest = fng.get("latest") if isinstance(fng.get("latest"), dict) else {}
    if _num(latest.get("value")) is not None:
        s = f"{_fng_zone(latest)} · 7 дн {_nf(fng.get('mean_7d'))}, 30 дн {_nf(fng.get('mean_30d'))}"
        tiles.append(_tile("Страх і жадібність", _e(_nf(latest["value"])), _e(s)))
    cur = (_data(snap, "vrp") or {}).get("current")
    cur = cur if isinstance(cur, dict) else {}
    dvol = _num(cur.get("dvol"))
    if dvol is None:
        lat = (_data(snap, "dvol") or {}).get("latest")
        v = _num(lat.get("value_pct")) if isinstance(lat, dict) else None
        dvol = v / 100 if v is not None else None
    if dvol is not None:
        pr = _num(cur.get("dvol_percentile_365d"))
        tiles.append(
            _tile(
                "DVOL (IV BTC, 30 дн)",
                _e(_nf(dvol * 100, 1)),
                _e(f"{_nf(pr * 100)}-й перцентиль за рік" if pr is not None else "перцентиля немає"),
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
        tiles.append(_tile("DVOL", _e(_nf(dvol * 100, 1)), _e(f"за 7 днів {_pc(ch, 1).replace(' %', '')} вп" if ch is not None else "")))
    rv30, rv7 = _num(cur.get("rv_30d")), _num(cur.get("rv_7d"))
    if rv30 is not None:
        tiles.append(_tile("Реалізована 30 дн", _e(_nf(rv30 * 100, 1)), _e(f"7 дн {_nf(rv7 * 100, 1)}" if rv7 is not None else "")))
    spr = _num(cur.get("spread_vs_rv30"))
    if spr is not None:
        tiles.append(_tile("Премія IV − RV30", _e(_pc(spr * 100, 1).replace(" %", "")), "пунктів волатильності"))
    out = [f"<div class='tiles'>{''.join(tiles)}</div>"] if tiles else []
    term = _data(snap, "term") or {}
    pts = []
    for x in _list(term.get("expiries")):
        if isinstance(x, dict) and _num(x.get("days")) is not None and _num(x.get("atm_iv")) is not None:
            pts.append((_num(x["days"]), _num(x["atm_iv"])))
    svg = term_svg(pts)
    if svg:
        out.append(svg + "<div class='legend'><span><i style='background:var(--acc)'></i>ATM IV за строками експірації, %</span></div>")
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


def block_events(snap: dict) -> str:
    ev = _data(snap, "events")
    if ev is None:
        return _empty("Календаря подій у знімку немає.") + _section_notes(snap, "events")
    rows = []
    for x in _list(ev.get("events"))[:12]:
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
            ("wide", "", block_tiles),
            ("", "Головне сьогодні", block_key),
            ("", "BTC за 120 днів і що закладають опціони", block_chart),
            ("", f"{scen} · ймовірності з цін опціонів", block_scenarios),
            ("", "Волатильність", block_vol),
            ("", "Події · час Київ", block_events),
            ("", "Funding · річних", block_funding),
        )
        for cls, title, fn in cards:
            h2 = f"<h2>{_e(title)}</h2>" if title else ""
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
    return render_brief(snap, err, time.time() if now is None else now, SNAPSHOT_PATH)


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
# (default, min, max): deposit USD, risk per trade %, size BTC (the owner trades 0.1, card «Депозит і ризик»)
OPT_SIZE = {"dep": (10000.0, 100.0, 1e8), "riskp": (1.0, 0.1, 100.0), "qty": (0.1, 0.01, 100.0)}
OPT_STRIKES = ("kput", "kcall", "kputw", "kcallw")
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
select,input[type=number]{font:14px Menlo,Consolas,monospace;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 8px;min-width:0;max-width:100%}
select:disabled{opacity:.6}.ctl button.go{font-weight:600;border:0;border-radius:999px;padding:7px 14px;background:var(--acc);color:var(--card);cursor:pointer}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.fit{font-size:14px;line-height:1.6}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}td.r,th.r{text-align:right}tr.cur{background:rgba(47,109,181,.12)}
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


def option_chain(snap: dict | None) -> dict | None:
    """The month chain for the builder: forward, days, ATM IV, expiry and rows {k, c/p: {mark, iv, delta}} (legs with a
    positive mark only). None when the snapshot has no usable chain."""
    chain = _data(snap, "chain") if isinstance(snap, dict) else None
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
    return {"F": F, "days": days, "T": days / 365.0, "iv": iv, "expiry": chain.get("expiry"), "rows": rows, "atm": atm["k"]}


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
    for name, (default, lo, hi) in OPT_SIZE.items():
        v = fnum(name, lo, hi)
        out[name] = default if v is None else v
    for name in OPT_STRIKES:
        out[name] = fnum(name, 0.0, 1e9)
    hzn = fnum("hzn", 0.0, 3650.0)
    out["hzn"] = OPT_HZN_DEFAULT if hzn is None else math.floor(hzn)
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


def _num_input(name: str, label: str, value: float, step: str) -> str:
    _, lo, hi = OPT_SIZE[name]
    return (
        f"<label for='{name}'>{label}<input type='number' id='{name}' name='{name}' min='{lo:g}' max='{hi:g}' step='{step}' "
        f"value='{value:.10g}'></label>"
    )


def block_builder(ch: dict, snap: dict, p: dict) -> str:
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
    exp = expiry_utc(ch.get("expiry"))
    exp_s = kyiv(exp, "%d.%m") if exp else _clean(ch.get("expiry") or "—", 12)
    out = [
        f"<section class='wide'><h2>Конструктор · BTC, експірація {_e(exp_s)} · {_e(_nf(ch['days'], 1))} дн · форвард {_e(_nf(ch['F']))}</h2>",
        "<form id='opt-form' method='get' action='/options'>",
        f"<input type='hidden' name='cur' value='{_e(strat)}'>",
        "<div class='seg' role='group' aria-label='Стратегія'>",
    ]
    for key, label in OPT_STRATEGIES.items():
        out.append(f"<button type='submit' name='s' value='{key}' aria-pressed={'true' if key == strat else 'false'}>{_e(label)}</button>")
    out.append("</div><div class='ctl'>")
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
    out.append("<button class='go' type='submit'>Порахувати</button></div></form>")
    out.append(
        "<p class='note'>Страйки — з ланцюжка знімка (Δ і IV Deribit); стреддл — на страйку, найближчому до форварду. "
        "Депозит, ризик і розмір запам'ятовуються в цьому браузері. Мінімальний лот опціонів BTC: Deribit — 0.1 BTC; "
        "OKX і Bybit — 0.01 BTC (уточнимо перед першою угодою).</p></section>"
    )

    legs, err = option_legs(ch, strat, strikes, qty)
    if err:
        out.append(f"<section class='wide'><h2>Позиція</h2>{_empty(err)}</section>")
        return "".join(out)
    F, T, iv = ch["F"], ch["T"], ch["iv"]
    credit = -sum((1 if lg["side"] == "long" else -1) * lg["size"] * lg["entry_premium"] for lg in legs)
    ext = payoff_extremes(legs)
    bes = payoff_breakevens(legs)
    pop = prob_of_profit(legs, F, T, iv)
    shown_bes = sorted({round(b, 2) for b in bes})

    out.append("<section class='wide'><h2>Прибуток і збиток на експірацію, USD</h2>")
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
        ),
        _tile("Беззбиток", _e(" / ".join(_kd(b) for b in shown_bes) or "—"), "на експірацію"),
        _tile(
            "Макс. прибуток",
            _e("необмежений" if ext["max_profit"] is None else _usd(ext["max_profit"])),
            "на експірацію",
        ),
    ]
    if ext["max_loss"] is None:
        down, up = payoff(legs, F * (1 - OPT_RANGE)), payoff(legs, F * (1 + OPT_RANGE))
        tiles.append(_tile("Макс. збиток", "необмежений", _e(f"на −30 %: {_usd(down)}, на +30 %: {_usd(up)}"), "bad"))
    else:
        tiles.append(_tile("Макс. збиток", _e(_usd(ext["max_loss"])), "на експірацію"))
    tiles.append(_tile("Ймовірність прибутку", _e(f"{_nf(pop * 100)} %"), "ризик-нейтральна, за ATM IV"))
    out.append(f"<section><h2>Позиція</h2><div class='tiles'>{''.join(tiles)}</div></section>")
    G = position_greeks(legs, F, T, iv)
    out.append(block_greeks(G))

    fit = risk_fit(ext["max_loss"], qty, dep, riskp)
    out.append("<section><h2>Чи вкладається в ризик</h2><div class='fit'>")
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
    pct365 = _num(((_data(snap, "vrp") or {}).get("current") or {}).get("dvol_percentile_365d")) if isinstance(snap, dict) else None
    out.append(
        "<section><h2>Що це означає</h2>" + verdict_html(legs, F, credit, shown_bes, pop, G, ext, fit, exp_s, qty, pct365) + "</section>"
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
        f"<section><h2>Греки позиції</h2><table class='greeks'>{body}</table>"
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
    tiles.append(_tile(f"ATM IV {_e(exp_s)}", _e(_nf(ch["iv"] * 100, 1)), _e(sub)))
    p365, dvol = _num(cur.get("dvol_percentile_365d")), _num(cur.get("dvol"))
    if p365 is not None:
        tiles.append(
            _tile("DVOL за рік", _e(f"{_nf(p365 * 100)}-й"), _e(f"перцентиль · DVOL {_nf(dvol * 100, 1) if dvol is not None else '—'}"))
        )
    if spr is not None:
        tiles.append(_tile("DVOL − RV30", _e(_pc(spr * 100, 1).replace(" %", "")), "пунктів волатильності"))
    out = ["<section><h2>Чи дорога зараз волатильність</h2>", f"<div class='tiles'>{''.join(tiles)}</div>"]
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
        "<section class='wide'><h2>Сценарії · P&amp;L позиції, USD</h2>"
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
        ch = option_chain(snap)
        if ch is None:
            out.append(
                "<section class='wide'><h2>Конструктор</h2>"
                + _empty("Ланцюжка опціонів у знімку немає: конструктор порахувати нема з чого.")
                + _section_notes(snap, "chain")
                + "</section></main></body></html>"
            )
            return "".join(out)
        try:
            out.append(block_builder(ch, snap, p))
        except Exception as e:  # noqa: BLE001 - one odd chain must not take the page down
            out.append(
                f"<section class='wide'>{_empty(f'Конструктор не вдалося показати: несподіваний формат даних ({type(e).__name__}).')}</section>"
            )
        notes = _section_notes(snap, "chain")
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
    return render_options(snap, err, time.time() if now is None else now, parse_options_query(query), SNAPSHOT_PATH)


# ---------------------------------------------------------------- «Журнал» tab: the owner's journal and the paper recommendations
#
# Read-only SQLite (mode=ro URI), no network. Summaries are stdlib copies of analytics.journal.summary and
# analytics.rec_paper.summarize (tests compare the two). Every string from the database (symbol, thesis, tags, note,
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


def load_journal(path: Path) -> tuple[dict | None, str | None]:
    """{'journal': rows | None, 'rec_paper': rows | None} (None = no such table), or (None, reason) with reason
    'missing' | 'unreadable: …'. Read-only connection; never raises."""
    try:
        if not path.exists():
            return None, "missing"
        con = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError) as e:  # OSError: e.g. PermissionError from exists() on a closed directory
        return None, f"unreadable: {type(e).__name__}"
    try:
        con.row_factory = sqlite3.Row
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        out: dict = {"journal": None, "rec_paper": None}
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


def block_journal_tiles(real: list[dict], n_open: int) -> str:
    s = journal_summary(real)
    if not s["closed_trades"]:
        return _empty("Закритих реальних угод ще немає.") + (f"<p class='note'>Відкритих: {n_open}.</p>" if n_open else "")
    total, exp = s["total_pnl"], s["expectancy_per_trade"]
    tiles = [
        _tile("Угод (закритих)", _nf(s["closed_trades"]), f"у плюсі {s['wins']} · у мінусі {s['losses']}"),
        _tile("Частка в плюсі", _share(s["win_rate"]), "за полем outcome"),
        _tile("P&L разом", f"<span class='{_signed_cls(total)}'>{_usd(total, True)}</span>", "USD, усі біржі й опціони разом"),
        _tile("Середнє на угоду", f"<span class='{_signed_cls(exp)}'>{_usd(exp, True)}</span>", "USD, усі джерела разом"),
        _tile("Середній R", _r(s["avg_r"]), f"угод зі стопом {s['r_count']}" if s["r_count"] else "стопи не записані"),
        _tile("Найбільша просадка", _usd(-max_drawdown_usd(real)), "USD, одна крива P&L усіх бірж і опціонів за датою закриття"),
    ]
    if n_open:
        tiles.append(_tile("Відкритих", _nf(n_open)))
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
        _tile("Розраховано", _nf(s["n"]), f"метод за ланцюгом · відкритих {n_open}"),
        _tile("У плюсі", _share(s["win_share"])),
        _tile("Середнє, % спота", f"<span class='{_signed_cls(mean)}'>{_pc(mean, 2)}</span>", "на 1 BTC, порівнянне з H1, основна метрика"),
        _tile("Середній R (як у тесті)", _r(s["mean_r_study"]), f"угод з R {s['n_r_study']}"),
        _tile("Середній R (від найгіршого)", _r(s["mean_r_worst_case"]), "P&L / ризик угоди"),
        _tile("P&L разом", f"<span class='{_signed_cls(s['sum_pnl_usd'])}'>{_usd(s['sum_pnl_usd'], True)}</span>", "USD на розмір угод"),
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


JOURNAL_CSS = """
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}.scroll table{min-width:560px}
.pager{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:8px 0 0}.pager a{color:var(--acc);font-weight:600}
h2 .chip{margin-left:8px;vertical-align:middle}
"""


def render_journal(data: dict | None, err: str | None, now: float, params: dict | None = None, path: Path | None = None) -> str:
    """The «Журнал» tab. Never raises: no database, no table or no rows gives an explained empty state."""
    p = params or {"page": 1}
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
        out = [head, f"<header><h1>Журнал</h1>{stamp}</header>{_nav('journal')}{caveats}<main class='brief'>"]
        rows = data.get("journal")
        if rows is None:
            out.append(
                "<section class='wide'><h2>Мої угоди</h2>"
                + _empty("Таблиці journal у базі ще немає: вона з'являється, коли MCP-сервер уперше відкриває базу.")
                + "</section>"
            )
        elif not rows:
            out.append(
                "<section class='wide'><h2>Мої угоди</h2>"
                + _empty("Угод ще немає.")
                + "<p class='note'>Додати першу: інструмент MCP <code>journal_add</code> (теза, стоп, ціль) і "
                "<code>journal_close</code> при виході; угоди з бірж — <code>scripts/bybit_import.py</code>.</p></section>"
            )
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
            out.append(f"<section class='wide'><h2>Мої угоди</h2>{block_journal_tiles(real, n_open)}")
            out.append(block_journal_other(kinds["paper"], kinds["airdrop"]))
            if len(rows) >= JOURNAL_MAX_ROWS:
                out.append(f"<p class='note warn'>Прочитано лише {JOURNAL_MAX_ROWS} останніх рядків журналу.</p>")
            out.append("</section>")
            out.append(f"<section><h2>За джерелом</h2>{block_journal_sources(groups)}</section>")
            out.append(f"<section class='wide'><h2>Угоди</h2>{block_journal_table(real, p['page'])}</section>")
        try:
            rec = block_rec_paper(data.get("rec_paper"))
        except Exception as e:  # noqa: BLE001 - one odd row must not take the page down
            rec = _empty(f"Блок не вдалося показати: несподіваний формат даних ({type(e).__name__}).")
        out.append(
            "<section class='wide'><h2>Паперові рекомендації асистента<span class='chip ev'>гіпотеза</span></h2>"
            f"<p class='note warn'>{_e(REC_CAVEAT)}</p>{rec}</section>"
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
    return render_journal(data, err, time.time() if now is None else now, parse_journal_query(query), JOURNAL_DB)


# ---------------------------------------------------------------- server


class _State:
    lock = threading.Lock()
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
        if path in ("/", "/index.html"):
            body, ctype = page.encode(), "text/html; charset=utf-8"
        elif path == "/brief":
            body, ctype = brief_page().encode(), "text/html; charset=utf-8"  # read per request: the age is always current
        elif path == "/options":
            body, ctype = options_page(query).encode(), "text/html; charset=utf-8"
        elif path == "/journal":
            body, ctype = journal_page(query).encode(), "text/html; charset=utf-8"
        elif self.path == "/status.json":
            body, ctype = json.dumps(snap or {}, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
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
    on.add_argument("--journal", action="store_true", help="the «Журнал» tab (first page) from the SQLite store, read-only")
    a = ap.parse_args()
    if a.cmd == "once":
        page = brief_page() if a.brief else options_page() if a.options else journal_page() if a.journal else render(snapshot())
        a.out.write_text(page, encoding="utf-8")
        print(f"-> {a.out}")
        return
    threading.Thread(target=_refresh_loop, daemon=True).start()
    threading.Thread(target=_watch_self, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), _Handler).serve_forever()  # loopback only: no auth on this page


if __name__ == "__main__":
    main()
