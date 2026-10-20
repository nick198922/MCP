"""Hourly watch of the open agent/* and claude/* pull requests: tells the owner's Telegram when one is stuck.

Stuck means: CI failed on the PR's latest commit, or the PR has not moved for STALE_HOURS (no automatic merge, no
new commit). Each finding is told once per PR head commit. No Claude tokens are spent; fixing is left to the doctor
or a Claude session. Runs as a user timer (deploy/user-units/tradedesk-pr-watch.*), reads the GitHub token like
scripts/gh_pr.py and remembers what it told in $LOG_DIR/pr_watch.json.

    python3 scripts/pr_watch.py [--repo owner/name] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from automerge import BRANCH_PREFIXES, classify, pr_files  # noqa: E402
from gh_pr import TOKEN_FILE, Call, caller  # noqa: E402
from tg_send import load_env, send  # noqa: E402

STALE_HOURS = 2.0
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR") or Path.home() / ".local/state/tradedesk-agents")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def failed_checks(check_runs: list[dict]) -> list[str]:
    """Names of the completed check runs that did not pass."""
    return sorted({c.get("name", "?") for c in check_runs if c.get("status") == "completed" and c.get("conclusion") in FAILED})


def findings(pr: dict, check_runs: list[dict], now: datetime) -> list[tuple[str, str]]:
    """(kind, detail) for one open PR: ("red", failed check names) and/or ("stale", hours without movement)."""
    out: list[tuple[str, str]] = []
    bad = failed_checks(check_runs)
    if bad:
        out.append(("red", ", ".join(bad)))
    hours = (now - parse_time(pr["updated_at"])).total_seconds() / 3600
    if hours > STALE_HOURS and not pr.get("draft"):
        out.append(("stale", f"{hours:.0f}"))
    return out


def message(pr: dict, kind: str, detail: str, verdict: str) -> str:
    title = f"#{pr['number']} {pr.get('title', '')}".strip()
    if kind == "red":
        return f"🔴 <b>CI впав у PR</b> {title}: {detail}. Автозлиття не буде, доки не виправлять."
    if verdict.startswith("owner"):
        return f"⏳ <b>PR чекає на ваше злиття</b> {detail} год: {title} (змінює системні файли)."
    return f"⏳ <b>PR застряг</b> {detail} год без руху, хоча мав злитись сам: {title}."


def watch(call: Call, repo: str, state: dict, now: datetime) -> tuple[list[tuple[str, str]], dict]:
    """Messages to send (text, PR url) and the new state; a finding is told once per head commit."""
    status, prs = call("GET", f"/repos/{repo}/pulls?state=open&per_page=100", None)
    if status != 200 or not isinstance(prs, list):
        raise RuntimeError(f"GitHub pulls: HTTP {status}")
    new_state: dict = {}
    out: list[tuple[str, str]] = []
    for pr in prs:
        ref, sha = pr["head"]["ref"], pr["head"]["sha"]
        if not ref.startswith(BRANCH_PREFIXES):
            continue
        status, runs = call("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", None)
        check_runs = runs.get("check_runs", []) if status == 200 and isinstance(runs, dict) else []
        told = state.get(str(pr["number"]), {})
        told = told if told.get("sha") == sha else {"sha": sha, "kinds": []}
        for kind, detail in findings(pr, check_runs, now):
            if kind in told["kinds"]:
                continue
            verdict = classify(pr_files(call, repo, pr["number"])) if kind == "stale" else ""
            out.append((message(pr, kind, detail, verdict), pr["html_url"]))
            told["kinds"].append(kind)
        new_state[str(pr["number"])] = told
    return out, new_state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="nick198922/MCP")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending; keep the state unchanged")
    args = ap.parse_args(argv)
    token = load_env(TOKEN_FILE).get("GITHUB_TOKEN")
    if not token:
        print(f"no GITHUB_TOKEN in {TOKEN_FILE}", file=sys.stderr)
        return 1
    state_file = LOG_DIR / "pr_watch.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    msgs, new_state = watch(caller(token), args.repo, state, datetime.now(UTC))
    for text, url in msgs:
        if args.dry_run:
            print(text, url)
        else:
            send(text, buttons=[("🔀 Відкрити PR", url)], topic="agents")
    if not args.dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(new_state))
    print(f"{len(msgs)} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
