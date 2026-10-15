"""Merge an agents' pull request by itself when it is safe to (run by .github/workflows/automerge.yml from main).

A pull request qualifies when it comes from an agent/* branch of this repository (not a doctor's -fix- branch), the
launcher marked it ready (the coordinator handed the step over; a step stuck after its run limit is not ready), CI
passed on its current head, and it changes nothing of the control plane: the agents' own scripts, permissions and
instructions, the server scripts, GitHub workflows, dependencies, CLAUDE.md, the security notes, ADRs or an execution
package. Anything else waits for the owner. The rules are read from main, never from the pull request.

    python3 scripts/automerge.py --repo owner/name --pr N --sha TESTED_SHA     # in Actions, with GH_TOKEN
    git diff --name-only origin/main...BRANCH | python3 scripts/automerge.py --classify    # prints "auto" or "owner: why"
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gh_pr import READY_MARK, Call, caller, message  # noqa: E402

PROTECTED_PREFIXES = (".github/", ".claude/", "scripts/server/", "deploy/", "docs/adr/", "src/tradedesk/execution/")
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
}
PROTECTED_GLOBS = ("scripts/agents_*", "scripts/tg_*", "scripts/telegram_*", "scripts/install_*")


def protected(path: str) -> bool:
    return path.startswith(PROTECTED_PREFIXES) or path in PROTECTED_FILES or any(fnmatch.fnmatch(path, g) for g in PROTECTED_GLOBS)


def classify(files: list[str]) -> str:
    """'auto' when the changed files may be merged without the owner, otherwise 'owner: <why>'."""
    files = [f for f in files if f]
    if not files:
        return "owner: no changes"
    hit = [f for f in files if protected(f)]
    if hit:
        return "owner: changes " + ", ".join(hit[:3]) + (" …" if len(hit) > 3 else "")
    return "auto"


def decide(pr: dict, files: list[str], tested_sha: str) -> tuple[bool, str]:
    head, base = pr.get("head") or {}, pr.get("base") or {}
    ref = str(head.get("ref", ""))
    if pr.get("state") != "open" or pr.get("merged"):
        return False, "not open"
    if base.get("ref") != "main":
        return False, "not into main"
    if (head.get("repo") or {}).get("full_name") != (base.get("repo") or {}).get("full_name"):
        return False, "from a fork"
    if not ref.startswith("agent/") or "-fix-" in ref:
        return False, f"{ref} is not an agents' step branch"
    if READY_MARK not in (pr.get("body") or ""):
        return False, "not marked ready by the launcher"
    if head.get("sha") != tested_sha:
        return False, "new commits since CI ran"
    verdict = classify(files)
    return verdict == "auto", verdict


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


def run(call: Call, repo: str, number: int, sha: str) -> str:
    status, pr = call("GET", f"/repos/{repo}/pulls/{number}", None)
    if status != 200 or not isinstance(pr, dict):
        raise RuntimeError(f"cannot read PR #{number}: HTTP {status} {message(pr)}")
    ok, why = decide(pr, pr_files(call, repo, number), sha)
    if not ok:
        return f"PR #{number} waits for the owner: {why}"
    ref = pr["head"]["ref"]
    body = {"merge_method": "merge", "sha": sha, "commit_title": f"Merge pull request #{number} from {ref} (auto: agents)"}
    status, data = call("PUT", f"/repos/{repo}/pulls/{number}/merge", body)
    if status != 200:
        raise RuntimeError(f"GitHub did not merge PR #{number}: HTTP {status} {message(data)}")
    call("DELETE", f"/repos/{repo}/git/refs/heads/{ref}", None)
    return f"PR #{number} merged ({ref})"


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
