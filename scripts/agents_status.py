"""Project status for the owner's Telegram group, sent every 2 hours by scripts/agents_loop.sh.

Built from files and read-only checks, without any model call: what the agents are doing now (the run lock, the live
coordinator session, the loop state), the last hours from history.tsv, plan usage, idea cards on main and on unmerged
agent branches, what waits for the owner (PRs, open questions), what to improve (critic requests, the doctor's latest
report, the next BACKLOG items) and the server (services, collector freshness, disk, backups).

    python3 scripts/agents_status.py [--print]      # --print shows the message instead of sending it
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
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import send  # noqa: E402

GITHUB = "https://github.com/nick198922/MCP"
STATUS_ICON = {
    "proposed": "📝",
    "accepted": "👍",
    "forward": "🔭",
    "testing": "🔬",
    "confirmed": "🏆",
    "built": "🧰",
    "rejected": "✖️",
    "inconclusive": "❔",
}
ROLE = {"scout": "скаут", "quant": "квант", "builder": "розробник", "critic": "критик"}
OUTCOME_ICON = {
    "delivered": "✅",
    "continues": "🔄",
    "parked": "⏸",
    "unfinished": "⏳",
    "stuck": "⚠️",
    "wip": "⚠️",
    "error": "❌",
    "idle": "💤",
}


# ---- pure helpers (tested) -------------------------------------------------------------------------------


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return "?"
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} хв тому"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} год тому"
    return f"{seconds // 86400} дн тому"


def parse_card(text: str) -> tuple[str, str, bool]:
    """(title, status, critic asks for changes) from an idea card."""
    title = ""
    status = "?"
    for line in text.splitlines():
        if not title and line.startswith("# "):
            title = line[2:].split(": ", 1)[-1].strip()
            title = title[:1].upper() + title[1:]
        m = re.match(r"^Статус:\s*([a-z]+)", line)
        if m and status == "?":
            status = m.group(1)
    review = text.split("Рев'ю критика", 1)[-1] if "Рев'ю критика" in text else ""
    verdicts = re.findall(r"`(request changes|approve|reject)`", review)
    return title, status, bool(verdicts) and verdicts[-1] == "request changes"


def open_questions(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n(?=## Q-)", text):
        head = block.splitlines()[0] if block else ""
        if head.startswith("## Q-") and re.search(r"^Статус:\s*open\s*$", block, re.M):
            out.append(head[3:].strip())
    return out


def backlog_next(text: str, n: int = 3) -> list[str]:
    items = []
    for line in text.splitlines():
        m = re.match(r"^- \[ \]\s+(.*)$", line)
        if not m:
            continue
        item = re.sub(r"[`*]", "", m.group(1)).strip()
        if len(item) > 100:
            item = item[:100].rsplit(" ", 1)[0] + "…"
        items.append(item)
        if len(items) >= n:
            break
    return items


def history_counts(lines: list[str], since_stamp: str) -> Counter:
    c: Counter = Counter()
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] >= since_stamp:
            c[parts[1]] += 1
    return c


def last_agent_call(jsonl_lines: list[str]) -> tuple[str, str, str, bool] | None:
    """(role, description, time HH:MM, finished) of the coordinator's latest subagent call."""
    calls: dict[str, tuple[str, str, str]] = {}
    order: list[str] = []
    done: set[str] = set()
    for raw in jsonl_lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name") in ("Agent", "Task"):
                inp = part.get("input") or {}
                ts = str(rec.get("timestamp", ""))
                hhmm = ""
                if ts:
                    try:
                        hhmm = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
                    except ValueError:
                        pass
                calls[part.get("id", "")] = (str(inp.get("subagent_type", "")), str(inp.get("description", "")), hhmm)
                order.append(part.get("id", ""))
            elif part.get("type") == "tool_result":
                done.add(part.get("tool_use_id", ""))
    if not order:
        return None
    last = order[-1]
    role, desc, hhmm = calls[last]
    return ROLE.get(role, role or "агент"), desc, hhmm, last in done


def render(d: dict) -> tuple[str, list[tuple[str, str]]]:
    """The message (Telegram HTML) and its buttons from the collected facts."""
    now = d.get("now") or datetime.now()
    out = [f"📊 <b>tradedesk · статус</b> · {now:%d.%m %H:%M}", ""]

    out.append("<b>🤖 Агенти</b>")
    out.append(esc(d.get("agents_line", "стан невідомий")))
    if d.get("activity"):
        out.append(esc(d["activity"]))
    if d.get("work"):
        br, runs = d["work"]
        out.append(f"🔄 У роботі <code>{esc(br)}</code> (прогін {runs}), PR буде, коли доведуть до кінця")
    for br, at in d.get("parked") or []:
        out.append(f"⏸ Відкладено до {at:%d.%m %H:%M} <code>{esc(br)}</code>: тим часом агенти беруть інші кроки")
    c2, c24 = d.get("counts_2h", Counter()), d.get("counts_24h", Counter())
    if sum(c24.values()):
        fmt = " · ".join(f"{OUTCOME_ICON[k]} {c24[k]}" for k in OUTCOME_ICON if c24.get(k))
        out.append(f"За добу: {fmt}" + (f" (за 2 год: {sum(c2.values())})" if c2 else ""))
    out.append("")

    usage = d.get("usage")
    if usage:
        out.append("<b>🔋 Ліміт Claude</b>")
        for label, key in (("5 год", "five_hour"), ("тиждень", "seven_day")):
            pct = usage.get(key)
            out.append(f"<code>{label:<7} {bar(pct)}</code> {pct if pct is not None else '?'}%")
        out.append("")

    cards = d.get("cards", [])
    if cards:
        counts = Counter(c["status"] for c in cards)
        out.append("<b>🧪 Ідеї</b> " + " · ".join(f"{STATUS_ICON.get(s, '•')}{n}" for s, n in sorted(counts.items())))
        for c in cards:
            if c["status"] in ("proposed", "accepted", "forward", "testing", "confirmed"):
                where = f" <i>(гілка {esc(c['branch'])})</i>" if c.get("branch") else ""
                out.append(f"{STATUS_ICON.get(c['status'], '•')} {esc(c['title'][:90])}{where}")
        out.append("")

    buttons: list[tuple[str, str]] = []
    waiting = []
    for br in d.get("open_branches", []):
        waiting.append(f"• PR <code>{esc(br)}</code>")
        buttons.append((f"🔀 {br.removeprefix('agent/')}", f"{GITHUB}/compare/main...{br}?expand=1"))
    for q in d.get("questions", []):
        waiting.append(f"• ❓ {esc(q[:110])}")
    out.append("<b>👀 Чекає на вас</b>")
    out.extend(waiting or ["нічого — агенти працюють самі"])
    out.append("")

    fixes = [f"• {esc(c['title'][:70])}: критик просить виправлень" for c in cards if c.get("changes_requested")]
    if d.get("doctor"):
        fixes.append(f"• 🩺 {esc(d['doctor'][:160])}")
    if fixes:
        out.append("<b>🛠 Потрібні виправлення</b>")
        out.extend(fixes)
        out.append("")
    if d.get("backlog"):
        out.append("<b>🗺 Далі в планах</b> <i>(BACKLOG)</i>")
        out.extend(f"• {esc(item)}" for item in d["backlog"])
        out.append("")

    if d.get("server"):
        out.append("<b>🖥 Сервер</b>")
        out.append(esc(d["server"]))
    return "\n".join(out).rstrip(), buttons


# ---- collection (I/O, best effort: a failing source is left out) -------------------------------------------


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def open_branches(repo: Path) -> list[str]:
    out = _git(repo, "branch", "-r", "--no-merged", "origin/main", "--list", "origin/agent/*")
    return [b.strip().removeprefix("origin/") for b in out.splitlines() if b.strip()]


def collect_cards(repo: Path, branches: list[str]) -> list[dict]:
    cards: dict[str, dict] = {}
    for path in _git(repo, "ls-tree", "-r", "--name-only", "origin/main", "docs/ideas").split():
        if Path(path).name.startswith("IDEA-"):
            title, status, changes = parse_card(_git(repo, "show", f"origin/main:{path}"))
            cards[path] = {"title": title or Path(path).stem, "status": status, "changes_requested": changes, "branch": ""}
    for br in branches:
        for path in _git(repo, "diff", "--name-only", f"origin/main...origin/{br}", "--", "docs/ideas").split():
            if Path(path).name.startswith("IDEA-"):
                text = _git(repo, "show", f"origin/{br}:{path}")
                if text:
                    title, status, changes = parse_card(text)
                    cards[path] = {"title": title or Path(path).stem, "status": status, "changes_requested": changes, "branch": br}
    return list(cards.values())


def usage() -> dict | None:
    try:
        creds = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
        token = creds["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return {k: (data.get(k) or {}).get("utilization") for k in ("five_hour", "seven_day")}
    except Exception:  # noqa: BLE001 - usage is optional in the status
        return None


def agents_state(log_dir: Path, worktree: Path) -> tuple[str, str]:
    lock_pid = log_dir / ".lock" / "pid"
    running = False
    try:
        os.kill(int(lock_pid.read_text().strip()), 0)
        running = True
    except (OSError, ValueError):
        pass
    if running:
        since = datetime.fromtimestamp(lock_pid.stat().st_mtime)
        line = f"🟢 Працюють над кроком з {since:%H:%M}"
        project = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(worktree))
        sessions = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if project.is_dir() else []
        if sessions:
            call = last_agent_call(sessions[-1].read_text(encoding="utf-8", errors="replace").splitlines())
            if call:
                role, desc, hhmm, finished = call
                state = "здав роботу координатору" if finished else "працює"
                return line, f"{role} ({hhmm}) {state}: «{desc}»"
        return line, ""
    try:
        stamp, text = (log_dir / "loop.state").read_text(encoding="utf-8").split("\t", 1)
        return f"⏸ {text.strip()} (з {datetime.fromtimestamp(int(stamp)):%H:%M})", ""
    except (OSError, ValueError):
        return "⚪ Цикл агентів не повідомив стан", ""


def work_state(log_dir: Path) -> tuple[str, int] | None:
    """The agents' work in progress (work.state of scripts/nightly_agents.sh): its branch and runs so far."""
    try:
        parts = (log_dir / "work.state").read_text(encoding="utf-8").split()
        return parts[0], int(parts[1])
    except (OSError, ValueError, IndexError):
        return None


def parked_state(log_dir: Path) -> list[tuple[str, datetime]]:
    """Work set aside until a time (parked.state of scripts/nightly_agents.sh, one line per branch): each branch and when
    it resumes, earliest first."""
    try:
        lines = (log_dir / "parked.state").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        try:
            out.append((parts[0], datetime.fromtimestamp(int(parts[1]))))
        except (ValueError, IndexError):
            continue
    return sorted(out, key=lambda x: x[1])


def wip_branches(history: list[str]) -> set[str]:
    """Branches whose last outcome in history.tsv set the work aside (parked, continues, unfinished): not for review."""
    last: dict[str, str] = {}
    for line in history:
        cols = line.split("\t")
        if len(cols) >= 3:
            for br in re.findall(r"`(agent/[^`]+)`", cols[2]):
                last[br] = cols[1]
    return {br for br, status in last.items() if status in ("parked", "continues", "unfinished")}


def doctor_note(log_dir: Path) -> str:
    reports = sorted(log_dir.glob("*.doctor.md"), key=lambda p: p.stat().st_mtime)
    if not reports or time.time() - reports[-1].stat().st_mtime > 86400:
        return ""
    lines = [ln.strip() for ln in reports[-1].read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    return f"лікар ({datetime.fromtimestamp(reports[-1].stat().st_mtime):%H:%M}): {lines[0]}" if lines else ""


def server_line(db: Path, backups: Path) -> str:
    parts = []
    if shutil.which("systemctl"):
        for unit, label in (("tradedesk-mcp", "MCP"), ("tradedesk-agents-loop", "цикл")):
            r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
            parts.append(f"{label} {'🟢' if r.stdout.strip() == 'active' else '🔴'}")
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            newest = max((con.execute(f"SELECT max(ts) FROM {t}").fetchone()[0] or 0) for t in ("atm_term", "funding_hist", "basis_hist"))
            con.close()
            if newest > 1e12:
                newest /= 1000
            fresh = time.time() - newest < 3 * 3600
            parts.append(f"колектор {'🟢' if fresh else '🔴'} ({ago(time.time() - newest)})")
        except sqlite3.Error:
            parts.append("колектор ?")
    disk = shutil.disk_usage("/")
    pct = disk.used / disk.total * 100
    parts.append(f"диск {pct:.0f}%{' 🔴' if pct > 80 else ''}")
    files = sorted(backups.glob("*"), key=lambda p: p.stat().st_mtime) if backups.is_dir() else []
    if files:
        age = time.time() - files[-1].stat().st_mtime
        parts.append(f"бекап {'🟢' if age < 2 * 86400 else '🔴'} ({ago(age)})")
    return " · ".join(parts)


def collect(repo: Path, worktree: Path, log_dir: Path) -> dict:
    _git(repo, "fetch", "-q", "--no-write-fetch-head", "origin")
    branches = open_branches(repo)
    work = work_state(log_dir)
    parked = parked_state(log_dir)
    agents_line, activity = agents_state(log_dir, worktree)
    try:
        hist = (log_dir / "history.tsv").read_text(encoding="utf-8").splitlines()
    except OSError:
        hist = []
    busy = {b for b, _ in parked} | ({work[0]} if work else set()) | wip_branches(hist)
    waiting = [b for b in branches if b not in busy]
    stamp = lambda secs: datetime.fromtimestamp(time.time() - secs).strftime("%Y-%m-%d_%H%M%S")  # noqa: E731
    read = lambda rel: _git(repo, "show", f"origin/main:{rel}")  # noqa: E731
    data_dir = Path(os.environ.get("TRADEDESK_DATA_DIR", "/var/lib/tradedesk-mcp"))
    return {
        "now": datetime.now(),
        "agents_line": agents_line,
        "activity": activity,
        "counts_2h": history_counts(hist, stamp(2 * 3600)),
        "counts_24h": history_counts(hist, stamp(86400)),
        "usage": usage(),
        "cards": collect_cards(repo, branches),
        "work": work,
        "parked": parked,
        "open_branches": [b for b in waiting if "-fix-" not in b] + [b for b in waiting if "-fix-" in b],
        "questions": open_questions(read("docs/QUESTIONS.md")),
        "doctor": doctor_note(log_dir),
        "backlog": backlog_next(read("docs/BACKLOG.md")),
        "server": server_line(data_dir / "tradedesk.sqlite", data_dir / "backups"),
    }


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=here)
    ap.add_argument("--worktree", type=Path, default=Path(os.environ.get("TRADEDESK_AGENTS_WORKTREE", here.parent / "tradedesk-agents")))
    default_logs = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tradedesk-agents"
    ap.add_argument("--log-dir", type=Path, default=Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR", default_logs)))
    ap.add_argument("--print", action="store_true", help="print the message instead of sending it")
    args = ap.parse_args(argv)
    text, buttons = render(collect(args.repo, args.worktree, args.log_dir))
    if args.print or os.environ.get("TRADEDESK_AGENTS_NO_NOTIFY"):
        print(text)
        for label, url in buttons:
            print(f"[{label}] {url}")
        return 0
    return 0 if send(text, silent=True, buttons=buttons, topic="status") else 1


if __name__ == "__main__":
    sys.exit(main())
