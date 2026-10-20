"""The stuck pull request watch in scripts/pr_watch.py (a fake GitHub, no network)."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pr_watch as pw  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def pr(number=1, ref="agent/2026-10-20-x", sha="abc", updated="2026-10-20T11:30:00Z"):
    return {
        "number": number,
        "title": "t",
        "draft": False,
        "updated_at": updated,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": ref, "sha": sha},
    }


def fake(prs, runs=None, files=("docs/x.md",)):
    def call(method, path, body):
        if "/pulls?" in path:
            return 200, prs
        if "/check-runs" in path:
            return 200, {"check_runs": (runs or {}).get(path.split("/commits/")[1].split("/")[0], [])}
        if "/files" in path:
            return 200, [{"filename": f} for f in files]
        return 404, {}

    return call


def test_red_ci_is_told_once_per_commit():
    runs = {"abc": [{"name": "test (3.12)", "status": "completed", "conclusion": "failure"}]}
    msgs, state = pw.watch(fake([pr()], runs), "o/r", {}, NOW)
    assert len(msgs) == 1 and "CI впав" in msgs[0][0] and "test (3.12)" in msgs[0][0]
    again, state = pw.watch(fake([pr()], runs), "o/r", state, NOW)
    assert again == []
    runs["new"] = runs["abc"]
    later, _ = pw.watch(fake([pr(sha="new")], runs), "o/r", state, NOW)
    assert len(later) == 1


def test_stale_pr_names_who_must_act():
    old = pr(updated="2026-10-20T08:00:00Z")
    ((msg, url),) = pw.watch(fake([old]), "o/r", {}, NOW)[0]
    assert "мав злитись сам" in msg and url.endswith("/pull/1")
    ((msg, _),) = pw.watch(fake([old], files=("scripts/agents_loop.sh",)), "o/r", {}, NOW)[0]
    assert "чекає на ваше злиття" in msg


def test_fresh_green_and_foreign_prs_are_quiet():
    runs = {"abc": [{"name": "test", "status": "completed", "conclusion": "success"}, {"name": "lint", "status": "in_progress"}]}
    prs = [pr(), pr(number=2, ref="dependabot/x", updated="2026-10-01T00:00:00Z")]
    msgs, state = pw.watch(fake(prs, runs), "o/r", {}, NOW)
    assert msgs == [] and list(state) == ["1"]
