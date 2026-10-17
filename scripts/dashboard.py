"""Project dashboard: one web page with what the agents do now, their queue and results, plan usage and the server.

    python3 scripts/dashboard.py serve [--port 8090]      # on the server (tradedesk-dashboard.service, 127.0.0.1 only)
    python3 scripts/dashboard.py once --out page.html     # one page, for a check

Read-only and model-free: built from the same files as the Telegram status (scripts/agents_status.py), the idea cards
on origin/main (scripts/results_board.py), the agents' run history and live transcript, the SQLite store (read-only)
and /proc. The page refreshes itself every minute; /status.json gives the same data to scripts. The server listens on
127.0.0.1 only: the owner opens it through an SSH tunnel (scripts/dashboard-open.sh). No secret is ever put on the page:
the plan usage is read with the agents' login, only percentages and reset times are kept.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


def read_usage() -> dict | None:
    try:
        token = json.loads((HOME / ".claude/.credentials.json").read_text())["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001 - usage is optional on the page
        return None
    out = {}
    for k in ("five_hour", "seven_day"):
        v = data.get(k) or {}
        out[k] = {"pct": v.get("utilization"), "resets_at": v.get("resets_at")}
    return out


def usage_with_trend(now: float) -> dict | None:
    u = read_usage()
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
        _git("fetch", "-q", "origin")
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
"""


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
        f"<header><h1>tradedesk</h1><span class='mut'>оновлено {_when(s['generated_at'])} · сторінка сама оновлюється щохвилини</span><span>{svc}</span></header><main>"
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
    out.append("<section><h2>Черга</h2><table><tr><th>#</th><th>Картка</th><th>Статус</th></tr>")
    for x in owner:
        out.append(
            f"<tr><td>{x['order'] if x['order'] is not None else '👤'}</td><td>{_e(x['title'])}<br><span class='mut'>{_e(x['name'][14:])}</span></td><td>{_e(x['status'])}</td></tr>"
        )
    for x in other:
        out.append(
            f"<tr><td class='mut'>·</td><td>{_e(x['title'])}<br><span class='mut'>{_e(x['name'][14:])}</span></td><td>{_e(x['status'])}</td></tr>"
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
        if self.path in ("/", "/index.html"):
            body, ctype = page.encode(), "text/html; charset=utf-8"
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
    a = ap.parse_args()
    if a.cmd == "once":
        a.out.write_text(render(snapshot()), encoding="utf-8")
        print(f"-> {a.out}")
        return
    threading.Thread(target=_refresh_loop, daemon=True).start()
    threading.Thread(target=_watch_self, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", a.port), _Handler).serve_forever()  # loopback only: no auth on this page


if __name__ == "__main__":
    main()
