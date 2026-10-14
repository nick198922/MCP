"""Open (or find) the pull request of an agent branch, so the owner gets a ready PR rather than a compare link.

The token comes from ~/.config/tradedesk/github.env (GITHUB_TOKEN=..., 0600, written by
scripts/server/11-github-token.sh): a fine-grained token for this repository only, with Pull requests read and
write and Contents read-only. It can open pull requests but cannot push or merge. It is never printed or logged.

    python3 scripts/gh_pr.py --repo owner/name --head agent/... --title "..." --body-file summary.md   # prints the URL
    python3 scripts/gh_pr.py --repo owner/name --check                                              # token usable?

Exit code 1 (and a reason on stderr) when there is no token or GitHub refuses; the caller then uses a compare link.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import load_env  # noqa: E402

TOKEN_FILE = Path.home() / ".config" / "tradedesk" / "github.env"
BODY_LIMIT = 60000  # GitHub allows 65536 characters

# (method, path, body) -> (HTTP status, decoded JSON)
Call = Callable[[str, str, dict | None], tuple[int, object]]


def caller(token: str) -> Call:
    def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "tradedesk-agents",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.load(e)
            except (ValueError, OSError):
                return e.code, {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return 0, {"message": type(e).__name__}

    return call


def message(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    errors = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in data.get("errors") or [])
    return " ".join(p for p in (str(data.get("message", "")), errors) if p)


def open_pr(call: Call, repo: str, head: str, title: str, body: str, base: str = "main") -> str:
    """The URL of the open PR for `head` (created if there is none). Raises RuntimeError with GitHub's reason."""
    status, data = call("POST", f"/repos/{repo}/pulls", {"title": title[:250], "head": head, "base": base, "body": body[:BODY_LIMIT]})
    if status == 201 and isinstance(data, dict):
        return str(data["html_url"])
    if status == 422 and "already exists" in message(data):
        owner = repo.split("/", 1)[0]
        query = urllib.parse.urlencode({"head": f"{owner}:{head}", "state": "open"})
        status2, found = call("GET", f"/repos/{repo}/pulls?{query}", None)
        if status2 == 200 and isinstance(found, list) and found:
            return str(found[0]["html_url"])
    raise RuntimeError(f"GitHub HTTP {status}: {message(data) or 'no reason given'}")


PROBE_BRANCH = "tradedesk-permission-probe-no-such-branch"


def check(call: Call, repo: str) -> str:
    """Read the PRs, then ask for a PR from a branch that does not exist: GitHub answers 422 when the token may open
    pull requests and 403 when it may not, and nothing is created either way."""
    status, data = call("GET", f"/repos/{repo}/pulls?per_page=1", None)
    if status != 200:
        raise RuntimeError(f"GitHub HTTP {status}: {message(data) or 'no reason given'}")
    status, data = call("POST", f"/repos/{repo}/pulls", {"title": "permission probe", "head": PROBE_BRANCH, "base": "main"})
    if status != 422:
        raise RuntimeError(f"the token can read but not open pull requests (GitHub HTTP {status}: {message(data)})")
    return f"the token works: it can read and open the pull requests of {repo}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--head", help="the branch to open the PR from")
    ap.add_argument("--title", default="")
    ap.add_argument("--body-file", type=Path)
    ap.add_argument("--base", default="main")
    ap.add_argument("--check", action="store_true", help="only test the token")
    args = ap.parse_args(argv)
    token = load_env(TOKEN_FILE).get("GITHUB_TOKEN", "")
    if not token:
        print(f"no GITHUB_TOKEN in {TOKEN_FILE}", file=sys.stderr)
        return 1
    call = caller(token)
    try:
        if args.check:
            print(check(call, args.repo))
            return 0
        if not args.head:
            ap.error("--head is required")
        body = args.body_file.read_text(encoding="utf-8", errors="replace") if args.body_file else ""
        print(open_pr(call, args.repo, args.head, args.title or args.head, body, args.base))
        return 0
    except (RuntimeError, OSError) as e:
        print(f"pull request not opened: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
