"""The stuck pull request watch in scripts/pr_watch.py (a fake GitHub, no network)."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pr_watch as pw  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def pr(number=1, ref="agent/2026-10-20-x", sha="abc", updated="2026-10-20T11:30:00Z", ready=False):
    return {
        "number": number,
        "body": "s\n\n" + pw.READY_MARK if ready else "s",
        "title": "t",
        "draft": False,
        "updated_at": updated,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": ref, "sha": sha},
    }


def fake(prs, runs=None, files=("docs/x.md",), calls=None, merge_status=200):
    def call(method, path, body):
        if calls is not None:
            calls.append((method, path))
        if method == "PUT" and path.endswith("/merge"):
            return merge_status, {"merged": merge_status == 200}
        if method in ("POST", "DELETE"):
            return 201, {}
        if "/pulls/" in path and "/files" not in path:
            n = int(path.rsplit("/", 1)[1])
            p = next(x for x in prs if x["number"] == n)
            return 200, {
                **p,
                "state": "open",
                "merged": False,
                "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                "head": {**p["head"], "repo": {"full_name": "o/r"}},
            }
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


def test_stale_pr_is_told():
    old = pr(updated="2026-10-20T08:00:00Z")
    ((msg, url),) = pw.watch(fake([old]), "o/r", {}, NOW)[0]
    assert "мав злитись сам" in msg and url.endswith("/pull/1")


GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]
RED = [
    {"name": "test (3.12)", "status": "completed", "conclusion": "failure", "details_url": "https://github.com/o/r/actions/runs/555/job/9"}
]


def test_a_ready_green_pr_is_merged_even_without_the_workflow():
    calls = []
    old = pr(updated="2026-10-20T08:00:00Z", ready=True)
    msgs, state = pw.watch(fake([old], {"abc": GREEN}, files=("scripts/agents_loop.sh",), calls=calls), "o/r", {}, NOW)
    assert msgs == [] and "1" not in state
    assert ("PUT", "/repos/o/r/pulls/1/merge") in calls


def test_a_failed_merge_falls_back_to_the_stale_message():
    old = pr(updated="2026-10-20T08:00:00Z", ready=True)
    ((msg, _),) = pw.watch(fake([old], {"abc": GREEN}, merge_status=405), "o/r", {}, NOW)[0]
    assert "мав злитись сам" in msg


def test_a_green_pr_not_marked_ready_is_not_merged():
    calls = []
    pw.watch(fake([pr()], {"abc": GREEN}, calls=calls), "o/r", {}, NOW)
    assert not any(m == "PUT" for m, _ in calls)


def test_red_ci_of_a_ready_pr_is_rerun_once_then_told():
    calls = []
    msgs, state = pw.watch(fake([pr(ready=True)], {"abc": RED}, calls=calls), "o/r", {}, NOW)
    assert msgs == [] and ("POST", "/repos/o/r/actions/runs/555/rerun-failed-jobs") in calls
    calls.clear()
    msgs, _ = pw.watch(fake([pr(ready=True)], {"abc": RED}, calls=calls), "o/r", state, NOW)
    assert len(msgs) == 1 and "після перезапуску" in msgs[0][0] and not any(m == "POST" for m, _ in calls)


def test_fresh_green_and_foreign_prs_are_quiet():
    runs = {"abc": [{"name": "test", "status": "completed", "conclusion": "success"}, {"name": "lint", "status": "in_progress"}]}
    prs = [pr(), pr(number=2, ref="dependabot/x", updated="2026-10-01T00:00:00Z")]
    msgs, state = pw.watch(fake(prs, runs), "o/r", {}, NOW)
    assert msgs == [] and list(state) == ["1"]
