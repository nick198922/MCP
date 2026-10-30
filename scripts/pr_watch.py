"""Watch of the open agent/*, claude/* and chat/* pull requests every 15 minutes: finishes the automatic merge when GitHub
did not, and tells the owner's Telegram when a PR is stuck.

The owner wants every merge automatic (2026-10-22), so before telling anything the watch
- re-runs the failed jobs once per PR head commit (a flaky test), and
- merges a ready PR whose checks all passed on its head by the rules of scripts/automerge.py (the merge workflow
  never fires when GitHub leaves a finished CI run "in progress"), and
- brings a fresh main into a ready PR that conflicts with it when every conflict is in the documentation (docs/,
  top-level *.md) and both sides only added lines at the same place (two new rows of a table, two new BACKLOG items):
  the merge keeps main's lines, then the branch's, and is pushed to the branch in its own clone (~/tradedesk-prfix,
  never the live or the agents' clone); CI then runs and the merge follows. Any other conflict is told once.
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
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from automerge import BRANCH_PREFIXES, classify, pr_files  # noqa: E402
from automerge import run as merge  # noqa: E402
from gh_pr import READY_MARK, TOKEN_FILE, Call, caller  # noqa: E402
from tg_send import load_env, send  # noqa: E402

STALE_HOURS = 2.0
FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
LOG_DIR = Path(os.environ.get("TRADEDESK_AGENTS_LOG_DIR") or Path.home() / ".local/state/tradedesk-agents")
REPO_DIR = Path(__file__).resolve().parent.parent
FIX_DIR = Path(os.environ.get("TRADEDESK_PRFIX_DIR") or Path.home() / "tradedesk-prfix")
Fixer = Callable[[str], tuple[str, list[str]]]  # branch -> ("fixed" | "blocked", conflicted paths or reasons)


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


KEEP_BOTH_NEVER = ("docs/adr/",)  # a contract: two versions of a limit side by side would be read as either


def is_docs(path: str) -> bool:
    return path.startswith("docs/") or ("/" not in path and path.endswith(".md"))


def union_resolve(text: str, keep_both: bool = False) -> str | None:
    """The file with every conflict hunk (diff3 style) replaced by main's lines, then the branch's; None when a hunk
    changed lines both sides started from (a real edit conflict) or the file has no hunk at all. ``keep_both``: an edit
    conflict too keeps main's version of the lines, then the branch's lines main does not have (owner 30.10: an agent
    PR stuck overnight on a HANDOFF line both sides edited idled the agents; a doubled note beats a stalled pipeline)."""
    out: list[str] = []
    hunks = 0
    part, ours, base, theirs = "", [], [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<< ") and not part:
            part, ours, base, theirs = "ours", [], [], []
        elif line.startswith("||||||| ") and part == "ours":
            part = "base"
        elif line.rstrip("\r\n") == "=======" and part == "base":
            part = "theirs"
        elif line.startswith(">>>>>>> ") and part == "theirs":
            if base and not keep_both:
                return None
            out += theirs + ([x for x in ours if x not in theirs] if base else ours)  # theirs = origin/main, ours = the PR
            hunks += 1
            part = ""
        elif part == "ours":
            ours.append(line)
        elif part == "base":
            base.append(line)
        elif part == "theirs":
            theirs.append(line)
        else:
            out.append(line)
    if part or not hunks:
        return None
    return "".join(out)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or FIX_DIR, capture_output=True, text=True, check=check)


def merge_main(ref: str) -> tuple[str, list[str]]:
    """Merge origin/main into the PR branch in FIX_DIR; push it when it merges cleanly or every conflict is an
    additive docs one (union_resolve). Never forces: a branch that moved meanwhile is left for the next run."""
    if not (FIX_DIR / ".git").exists():
        url = git("remote", "get-url", "origin", cwd=REPO_DIR).stdout.strip()
        subprocess.run(["git", "clone", "-q", url, str(FIX_DIR)], check=True)
        git("config", "user.name", "tradedesk pr watch")
        git("config", "user.email", "tradedesk-pr-watch@users.noreply.github.com")
    git("fetch", "-q", "origin", "main", f"+refs/heads/{ref}:refs/remotes/origin/{ref}")
    git("checkout", "-q", "-f", "-B", "prfix", f"origin/{ref}")
    git("clean", "-fdxq")
    merged = git("-c", "merge.conflictStyle=diff3", "merge", "--no-edit", "-q", "origin/main", check=False)
    conflicted: list[str] = []
    if merged.returncode != 0:
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if not conflicted:
            git("merge", "--abort", check=False)
            return "blocked", [f"git merge: {(merged.stderr or merged.stdout).strip()[:200]}"]
        not_docs = [f for f in conflicted if not is_docs(f)]
        if not_docs:
            git("merge", "--abort", check=False)
            return "blocked", [f"не документація: {', '.join(not_docs[:5])}"]
        both: list[str] = []
        for f in conflicted:
            path = FIX_DIR / f
            text = path.read_text(encoding="utf-8") if path.is_file() else None
            resolved = None if text is None else union_resolve(text)
            if resolved is None and text is not None and not f.startswith(KEEP_BOTH_NEVER):
                resolved = union_resolve(text, keep_both=True)
                both += [f] if resolved is not None else []
            if resolved is None:
                git("merge", "--abort", check=False)
                return "blocked", [f"{f}: обидві сторони змінили ті самі рядки"]
            path.write_text(resolved, encoding="utf-8")
            git("add", "--", f)
        git("commit", "-q", "--no-edit")
        conflicted = [f"{f} (обидві версії рядків)" if f in both else f for f in conflicted]
    git("push", "-q", "origin", f"HEAD:refs/heads/{ref}")
    return "fixed", conflicted


def message(pr: dict, kind: str, detail: str, held: bool = False) -> str:
    title = f"#{pr['number']} {pr.get('title', '')}".strip()
    if kind == "conflict":
        return f"⚔️ <b>Конфлікт з main у PR</b> {title}: {detail}. Сам не розвʼязав, потрібен Claude або агенти."
    if kind == "red":
        return f"🔴 <b>CI впав у PR</b> {title}: {detail} (і після перезапуску). Автозлиття не буде, доки не виправлять."
    if held:
        return f"⏳ <b>PR чекає рев'ю Claude</b> {detail} год (код демо-трейдера або правила злиття): {title}."
    return f"⏳ <b>PR застряг</b> {detail} год без руху, хоча мав злитись сам: {title}."


def watch(call: Call, repo: str, state: dict, now: datetime, fix: Fixer | None = merge_main) -> tuple[list[tuple[str, str]], dict]:
    """Messages to send (text, PR url) and the new state; a finding is told once per head commit. Merges, re-runs
    and docs conflict fixes on the way (printed, not sent); ``fix=None`` (a dry run) only tells about conflicts."""
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
        if ready:
            status, detail = call("GET", f"/repos/{repo}/pulls/{pr['number']}", None)
            if status == 200 and isinstance(detail, dict) and detail.get("mergeable_state") == "dirty":
                verdict, paths = ("blocked", ["dry run"]) if fix is None else ("", [])
                if fix is not None:
                    try:
                        verdict, paths = fix(ref)
                    except (subprocess.CalledProcessError, OSError) as e:
                        verdict, paths = "error", [str(e)[:200]]
                print(f"PR #{pr['number']}: conflict with main: {verdict} {', '.join(paths)}")
                if verdict == "fixed":
                    new_state[str(pr["number"])] = told
                    continue
                if verdict == "blocked" and "conflict" not in told["kinds"]:
                    out.append((message(pr, "conflict", "; ".join(paths)), pr["html_url"]))
                    told["kinds"].append("conflict")
        if ready and all_passed(check_runs):
            try:
                result = merge(call, repo, pr["number"], sha)  # a 'claude:' hold waits for scripts/trader_review.py
                print(result)
                if result.startswith(f"PR #{pr['number']} merged"):
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
            hold = kind == "stale" and classify(pr_files(call, repo, pr["number"])).startswith("claude:")
            out.append((message(pr, kind, detail, hold), pr["html_url"]))
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
    msgs, new_state = watch(caller(token), args.repo, state, datetime.now(UTC), fix=None if args.dry_run else merge_main)
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
