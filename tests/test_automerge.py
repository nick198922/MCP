"""The automatic merge of agents' pull requests in scripts/automerge.py (a fake GitHub, no network)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import automerge as am  # noqa: E402
import gh_pr  # noqa: E402

REPO = {"full_name": "o/r"}


def pr(**over):
    p = {
        "state": "open",
        "merged": False,
        "body": "summary\n\n" + am.READY_MARK,
        "base": {"ref": "main", "repo": REPO},
        "head": {"ref": "agent/2026-10-14-dated-basis", "sha": "abc", "repo": REPO},
    }
    p.update(over)
    return p


def test_research_and_tools_merge_by_themselves():
    files = [
        "docs/verification/x.md",
        "src/tradedesk/analytics/basis.py",
        "src/tradedesk/tools/perps.py",
        "tests/test_basis.py",
        "scripts/verify_basis.py",
    ]
    assert am.classify(files) == "auto"


def test_the_control_plane_waits_for_the_owner():
    for path in (
        ".github/workflows/ci.yml",
        ".claude/nightly.md",
        "scripts/agents_loop.sh",
        "scripts/nightly_agents.sh",
        "scripts/server/10-tg-bridge.sh",
        "scripts/tg_bridge.py",
        "scripts/automerge.py",
        "CLAUDE.md",
        "docs/SECURITY.md",
        "pyproject.toml",
        "uv.lock",
        "src/tradedesk/execution/guard.py",
        "src/tradedesk/features.py",
        "scripts/user_units_sync.sh",
        "deploy/tradedesk-mcp.service",
        "deploy/backup-sqlite.sh",
    ):
        assert am.classify(["docs/x.md", path]).startswith("owner: changes " + path), path
    assert am.classify([]) == "owner: no changes"


def test_feature_switches_and_user_timers_merge_by_themselves():
    files = ["deploy/features.env", "deploy/env.example", "deploy/user-units/tradedesk-x.timer", "deploy/user-units/tradedesk-x.service"]
    assert am.classify(files) == "auto"


def test_claudes_own_branches_merge_like_the_agents():
    claude = pr(head={"ref": "claude/2026-10-19-self-service", "sha": "abc", "repo": REPO})
    assert am.decide(claude, ["docs/x.md"], "abc") == (True, "auto")
    other = pr(head={"ref": "feature/x", "sha": "abc", "repo": REPO})
    assert not am.decide(other, ["docs/x.md"], "abc")[0]


def test_decide_needs_a_ready_open_agent_pr_tested_at_its_head():
    ok_files = ["docs/x.md"]
    assert am.decide(pr(), ok_files, "abc") == (True, "auto")
    assert am.decide(pr(body="summary"), ok_files, "abc")[1] == "not marked ready by the launcher"
    assert am.decide(pr(), ok_files, "old")[1] == "new commits since CI ran"
    assert am.decide(pr(state="closed"), ok_files, "abc")[1] == "not open"
    fix = pr(head={"ref": "agent/2026-10-14-fix-loop", "sha": "abc", "repo": REPO})
    assert not am.decide(fix, ok_files, "abc")[0]
    fork = pr(head={"ref": "agent/x", "sha": "abc", "repo": {"full_name": "evil/r"}})
    assert am.decide(fork, ok_files, "abc")[1] == "from a fork"
    assert am.decide(pr(), ["scripts/agents_loop.sh"], "abc") == (False, "owner: changes scripts/agents_loop.sh")


class FakeGitHub:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path.split("?")[0], body))
        return self.routes.get((method, path.split("?")[0]), (404, {"message": "Not Found"}))


def test_run_merges_at_the_tested_sha_and_deletes_the_branch():
    gh = FakeGitHub(
        {
            ("GET", "/repos/o/r/pulls/7"): (200, pr()),
            ("GET", "/repos/o/r/pulls/7/files"): (200, [{"filename": "docs/x.md"}]),
            ("PUT", "/repos/o/r/pulls/7/merge"): (200, {"merged": True}),
            ("DELETE", "/repos/o/r/git/refs/heads/agent/2026-10-14-dated-basis"): (204, {}),
        }
    )
    assert am.run(gh, "o/r", 7, "abc") == "PR #7 merged (agent/2026-10-14-dated-basis)"
    merge = next(c for c in gh.calls if c[0] == "PUT")
    assert merge[2]["sha"] == "abc" and merge[2]["merge_method"] == "merge"
    assert gh.calls[-1][0] == "DELETE"


def test_run_leaves_a_renamed_control_file_for_the_owner():
    renamed = [{"filename": "docs/old-loop.sh", "previous_filename": "scripts/agents_loop.sh"}]
    gh = FakeGitHub({("GET", "/repos/o/r/pulls/7"): (200, pr()), ("GET", "/repos/o/r/pulls/7/files"): (200, renamed)})
    assert "waits for the owner" in am.run(gh, "o/r", 7, "abc")
    assert not any(c[0] == "PUT" for c in gh.calls)


def test_gh_pr_ready_marks_the_body(tmp_path, monkeypatch):
    sent = {}

    def fake_open(call, repo, head, title, body, base="main"):
        sent["body"] = body
        return "https://github.com/o/r/pull/9"

    (tmp_path / "github.env").write_text("GITHUB_TOKEN=github_pat_x\n")
    (tmp_path / "s.md").write_text("summary")
    monkeypatch.setattr(gh_pr, "TOKEN_FILE", tmp_path / "github.env")
    monkeypatch.setattr(gh_pr, "open_pr", fake_open)
    assert gh_pr.main(["--repo", "o/r", "--head", "agent/x", "--body-file", str(tmp_path / "s.md"), "--ready"]) == 0
    assert sent["body"].endswith(am.READY_MARK) and sent["body"].startswith("summary")
