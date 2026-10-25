"""Merge an agents' pull request by itself when it is safe to (run by .github/workflows/automerge.yml from main).

A pull request qualifies when it comes from an agent/*, claude/* or chat/* branch of this repository (the doctor's -fix-
branches too), the launcher (or Claude's session, scripts/gh_pr.py --ready) marked it ready (the coordinator handed
the step over; a step stuck after its run limit is not ready) and CI passed on its current head. Since 2026-10-22 the
owner wants every such merge automatic, the control plane included (the agents' own scripts, permissions and
instructions, the server scripts, GitHub workflows, dependencies, CLAUDE.md, the security notes, ADRs, the execution
package): those paths are listed below only to name them in the merge report. One exception, also the owner's
(2026-10-22): the demo trader is Claude's responsibility, so a PR that touches what places its orders (HELD: the
execution package, the signal adapters and detectors, its timers, ADR 0007) or what could switch this hold off (the
merge rules, the watch, the trader review and their timers, GitHub workflows) is held until scripts/trader_review.py
(Claude, Opus) approves that very commit. The rules are read from main, never from the pull request.
scripts/pr_watch.py merges by the same rules when this workflow never ran (a hung or flaky CI run).

    python3 scripts/automerge.py --repo owner/name --pr N --sha TESTED_SHA     # in Actions, with GH_TOKEN
    git diff --name-only origin/main...BRANCH | python3 scripts/automerge.py --classify    # prints "auto", "claude: paths" or "owner: no changes"
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gh_pr import READY_MARK, Call, caller, message  # noqa: E402

# The control plane, named in the merge report: deploy/ except the feature switches and the user timers (both run as
# the unprivileged service user, like the rest of the code); their guards, src/tradedesk/features.py and
# scripts/user_units_sync.sh, are listed.
PROTECTED_PREFIXES = (".github/", ".claude/", "scripts/server/", "docs/adr/", "src/tradedesk/execution/")
DEPLOY_OPEN = ("deploy/features.env", "deploy/env.example", "deploy/README.md", "deploy/user-units/")
BRANCH_PREFIXES = ("agent/", "claude/", "chat/")  # the agents' launcher, Claude's own sessions, the owner's Telegram chat
PROTECTED_FILES = {
    "CLAUDE.md",
    "docs/SECURITY.md",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".gitignore",
    "scripts/nightly_agents.sh",
    "scripts/gh_pr.py",
    "scripts/automerge.py",
    "scripts/user_units_sync.sh",
    "src/tradedesk/features.py",
    # what decides the demo trader's orders (ADR 0007): the signal adapters and the detectors they run
    "src/tradedesk/analytics/setup_signals.py",
    "src/tradedesk/analytics/course_s14_ema.py",
    "src/tradedesk/analytics/course_basics.py",
    "src/tradedesk/analytics/course_fvg_ob.py",  # S12 (25.10)
    "src/tradedesk/analytics/short_setups.py",  # S38, S39 (25.10)
}
PROTECTED_GLOBS = (
    "scripts/agents_*",
    "scripts/tg_*",
    "scripts/telegram_*",
    "scripts/install_*",
    "deploy/user-units/tradedesk-demo-*",  # the demo trader's timers
)


# Held for Claude's review (scripts/trader_review.py): the demo trader (ADR 0007) and the guards of this hold.
HELD_PREFIXES = ("src/tradedesk/execution/", ".github/", "docs/adr/0007")
HELD_FILES = {
    "src/tradedesk/analytics/setup_signals.py",
    "src/tradedesk/analytics/course_s14_ema.py",
    "src/tradedesk/analytics/course_basics.py",
    "src/tradedesk/analytics/course_fvg_ob.py",
    "src/tradedesk/analytics/short_setups.py",
    "scripts/automerge.py",
    "scripts/pr_watch.py",
    "scripts/trader_review.py",
    "scripts/user_units_sync.sh",
    ".claude/trader-review.md",
}
HELD_GLOBS = (
    "deploy/user-units/tradedesk-demo-*",
    "scripts/demo_*.py",  # scripts that place demo orders (demo_option_probe.py, 23.10)
    "deploy/user-units/tradedesk-pr-watch.*",
    "deploy/user-units/tradedesk-trader-review.*",
)


def held(path: str) -> bool:
    return path.startswith(HELD_PREFIXES) or path in HELD_FILES or any(fnmatch.fnmatch(path, g) for g in HELD_GLOBS)


def protected(path: str) -> bool:
    if path.startswith("deploy/") and not path.startswith(DEPLOY_OPEN):
        return True
    return path.startswith(PROTECTED_PREFIXES) or path in PROTECTED_FILES or any(fnmatch.fnmatch(path, g) for g in PROTECTED_GLOBS)


def system_files(files: list[str]) -> list[str]:
    """The changed control-plane paths (reported with the merge, never a reason to hold it)."""
    return [f for f in files if f and protected(f)]


def classify(files: list[str]) -> str:
    """'auto'; 'claude: <paths>' when it touches the demo trader or the hold's guards (merged only after Claude's
    review); 'owner: no changes' when there is nothing to merge."""
    files = [f for f in files if f]
    if not files:
        return "owner: no changes"
    hit = [f for f in files if held(f)]
    if hit:
        return "claude: " + ", ".join(hit[:3]) + (" …" if len(hit) > 3 else "")
    return "auto"


def decide(pr: dict, files: list[str], tested_sha: str, reviewed: bool = False) -> tuple[bool, str]:
    """``reviewed``: scripts/trader_review.py approved exactly ``tested_sha``, which releases a 'claude:' hold."""
    head, base = pr.get("head") or {}, pr.get("base") or {}
    ref = str(head.get("ref", ""))
    if pr.get("state") != "open" or pr.get("merged"):
        return False, "not open"
    if base.get("ref") != "main":
        return False, "not into main"
    if (head.get("repo") or {}).get("full_name") != (base.get("repo") or {}).get("full_name"):
        return False, "from a fork"
    if not ref.startswith(BRANCH_PREFIXES):
        return False, f"{ref} is not an agents' or Claude's step branch"
    if READY_MARK not in (pr.get("body") or ""):
        return False, "not marked ready by the launcher"
    if head.get("sha") != tested_sha:
        return False, "new commits since CI ran"
    verdict = classify(files)
    return verdict == "auto" or (reviewed and verdict.startswith("claude:")), verdict


def pr_files(call: Call, repo: str, number: int) -> list[str]:
    out: list[str] = []
    for page in range(1, 31):  # GitHub lists at most 3000 files
        status, data = call("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}", None)
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"cannot list the files: HTTP {status} {message(data)}")
        for f in data:
            out += [f.get("filename", ""), f.get("previous_filename", "")]
        if len(data) < 100:
            return [f for f in out if f]
    raise RuntimeError("too many files to check")


def run(call: Call, repo: str, number: int, sha: str, reviewed: bool = False) -> str:
    status, pr = call("GET", f"/repos/{repo}/pulls/{number}", None)
    if status != 200 or not isinstance(pr, dict):
        raise RuntimeError(f"cannot read PR #{number}: HTTP {status} {message(pr)}")
    files = pr_files(call, repo, number)
    ok, why = decide(pr, files, sha, reviewed)
    if not ok:
        return f"PR #{number} not merged: {why}"
    ref = pr["head"]["ref"]
    body = {"merge_method": "merge", "sha": sha, "commit_title": f"Merge pull request #{number} from {ref} (auto)"}
    status, data = call("PUT", f"/repos/{repo}/pulls/{number}/merge", body)
    if status != 200:
        raise RuntimeError(f"GitHub did not merge PR #{number}: HTTP {status} {message(data)}")
    call("DELETE", f"/repos/{repo}/git/refs/heads/{ref}", None)
    system = system_files(files)
    return f"PR #{number} merged ({ref})" + (f"; system files: {', '.join(system[:5])}" if system else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--classify", action="store_true", help="read changed paths from stdin and print the verdict")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--pr", type=int)
    ap.add_argument("--sha", default="")
    args = ap.parse_args(argv)
    if args.classify:
        print(classify(sys.stdin.read().splitlines()))
        return 0
    token = os.environ.get("GH_TOKEN", "")
    if not (token and args.repo and args.pr and args.sha):
        ap.error("--repo, --pr, --sha and GH_TOKEN are required")
    try:
        print(run(caller(token), args.repo, args.pr, args.sha))
        return 0
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
