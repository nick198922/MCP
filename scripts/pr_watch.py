"""Watch of the open agent/* and claude/* pull requests every 15 minutes: finishes the automatic merge when GitHub
did not, and tells the owner's Telegram when a PR is stuck.

The owner wants every merge automatic (2026-10-22), so before telling anything the watch
- re-runs the failed jobs once per PR head commit (a flaky test), and
- merges a ready PR whose checks all passed on its head by the rules of scripts/automerge.py (the merge workflow
  never fires when GitHub leaves a finished CI run "in progress").
Stuck means: CI failed again after the re-run, or the PR has not moved for STALE_HOURS. Each finding is told once
per PR head commit. No Claude tokens are spent; fixing is left to the doctor or a Claude session. Runs as a user
timer (deploy/user-units/tradedesk-pr-watch.*), reads the GitHub token like scripts/gh_pr.py and remembers what it
did in $LOG_DIR/pr_watch.json.

    python3 scripts/pr_watch.py [--repo owner/name] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from automerge import BRANCH_PREFIXES  # noqa: E402
from automerge import run as merge  # noqa: E402
from gh_pr import READY_MARK, TOKEN_FILE, Call, caller  # noqa: E402
from tg_send import load_env, send  # noqa: E402

STALE_HOURS = 2.0
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR") or Path.home() / ".local/state/tradedesk-agents")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def failed_checks(check_runs: list[dict]) -> list[str]:
    """Names of the completed check runs that did not pass."""
    return sorted({c.get("name", "?") for c in check_runs if c.get("status") == "completed" and c.get("conclusion") in FAILED})


def all_passed(check_runs: list[dict]) -> bool:
    return bool(check_runs) and all(
        c.get("status") == "completed" and c.get("conclusion") in ("success", "skipped", "neutral") for c in check_runs
    )


def failed_run_ids(check_runs: list[dict]) -> list[int]:
    """Actions run ids of the failed check runs (from their details_url .../actions/runs/<id>/job/<job>)."""
    ids = set()
    for c in check_runs:
        if c.get("status") == "completed" and c.get("conclusion") in FAILED:
            m = re.search(r"/actions/runs/(\d+)", c.get("details_url") or "")
            if m:
                ids.add(int(m.group(1)))
    return sorted(ids)


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


def message(pr: dict, kind: str, detail: str) -> str:
    title = f"#{pr['number']} {pr.get('title', '')}".strip()
    if kind == "red":
        return f"🔴 <b>CI впав у PR</b> {title}: {detail} (і після перезапуску). Автозлиття не буде, доки не виправлять."
    return f"⏳ <b>PR застряг</b> {detail} год без руху, хоча мав злитись сам: {title}."


def watch(call: Call, repo: str, state: dict, now: datetime) -> tuple[list[tuple[str, str]], dict]:
    """Messages to send (text, PR url) and the new state; a finding is told once per head commit. Merges and
    re-runs on the way (printed, not sent)."""
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
        ready = READY_MARK in (pr.get("body") or "")
        if ready and all_passed(check_runs):
            try:
                print(merge(call, repo, pr["number"], sha))
                continue
            except RuntimeError as e:  # e.g. merged meanwhile by the workflow, or a conflict: the stale check tells
                print(e, file=sys.stderr)
        rerun = failed_run_ids(check_runs) if ready and not told.get("rerun") else []
        for run_id in rerun:
            status, _ = call("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs", {})
            print(f"PR #{pr['number']}: re-run of the failed jobs of run {run_id}: HTTP {status}")
        if rerun:
            told["rerun"] = True
            new_state[str(pr["number"])] = told
            continue
        for kind, detail in findings(pr, check_runs, now):
            if kind in told["kinds"]:
                continue
            out.append((message(pr, kind, detail), pr["html_url"]))
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
