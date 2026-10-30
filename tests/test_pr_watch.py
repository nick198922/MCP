"""The stuck pull request watch in scripts/pr_watch.py (a fake GitHub, no network)."""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

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


def test_a_held_pr_is_not_merged_and_its_stale_note_names_claude():
    calls = []
    old = pr(updated="2026-10-20T08:00:00Z", ready=True)
    msgs, _ = pw.watch(fake([old], {"abc": GREEN}, files=("src/tradedesk/execution/guard.py",), calls=calls), "o/r", {}, NOW)
    assert not any(m == "PUT" for m, _ in calls)
    assert len(msgs) == 1 and "рев'ю Claude" in msgs[0][0]


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


def hunk(ours, base, theirs):
    return f"<<<<<<< HEAD\n{ours}||||||| base\n{base}=======\n{theirs}>>>>>>> origin/main\n"


def test_union_resolve_keeps_mains_rows_then_the_branchs():
    text = "| a |\n" + hunk("| branch |\n", "", "| main |\n") + "end\n"
    assert pw.union_resolve(text) == "| a |\n| main |\n| branch |\nend\n"


def test_union_resolve_refuses_edits_of_the_same_lines_and_files_without_hunks():
    assert pw.union_resolve("x\n" + hunk("new A\n", "old\n", "new B\n")) is None
    assert pw.union_resolve("no conflict\n") is None
    assert pw.union_resolve("<<<<<<< HEAD\nhalf\n") is None


def test_union_resolve_keep_both_takes_mains_edit_then_the_branchs_new_lines():
    """Owner 30.10 (no idle): an edit conflict in docs keeps main's lines, then the branch's lines main lacks."""
    text = "x\n" + hunk("old note\nbranch line\n", "old note\n", "main edit\n") + "y\n"
    assert pw.union_resolve(text, keep_both=True) == "x\nmain edit\nold note\nbranch line\ny\n"
    same = "x\n" + hunk("same\n", "old\n", "same\n")
    assert pw.union_resolve(same, keep_both=True) == "x\nsame\n"


def test_is_docs():
    assert pw.is_docs("docs/library/README.md") and pw.is_docs("CHANGELOG.md")
    assert not pw.is_docs("scripts/x.py") and not pw.is_docs("src/tradedesk/README.md")


def dirty(prs):
    base = fake(prs)

    def call(method, path, body):
        status, data = base(method, path, body)
        if method == "GET" and "/pulls/" in path and "/files" not in path:
            data = {**data, "mergeable_state": "dirty"}
        return status, data

    return call


def test_a_ready_dirty_pr_gets_main_merged_in_and_waits_for_ci():
    seen = []
    msgs, state = pw.watch(dirty([pr(ready=True)]), "o/r", {}, NOW, fix=lambda ref: seen.append(ref) or ("fixed", ["docs/a.md"]))
    assert seen == ["agent/2026-10-20-x"] and msgs == []


def test_a_conflict_outside_docs_is_told_once():
    def fix(ref):
        return "blocked", ["не документація: scripts/x.py"]

    msgs, state = pw.watch(dirty([pr(ready=True)]), "o/r", {}, NOW, fix=fix)
    assert len(msgs) == 1 and "Конфлікт з main" in msgs[0][0] and "scripts/x.py" in msgs[0][0]
    again, _ = pw.watch(dirty([pr(ready=True)]), "o/r", state, NOW, fix=fix)
    assert again == []


def test_a_dirty_pr_not_marked_ready_is_left_to_its_author():
    msgs, _ = pw.watch(dirty([pr()]), "o/r", {}, NOW, fix=lambda ref: pytest.fail("not ready: no fix"))
    assert msgs == []


def sh(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_merge_main_resolves_additive_docs_conflicts_and_pushes(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    sh("clone", "-q", str(origin), str(work), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@x")):
        sh("config", k, v, cwd=work)
    (work / "docs").mkdir()
    (work / "docs" / "t.md").write_text("| h |\n")
    (work / "x.py").write_text("a = 1\n")
    sh("add", ".", cwd=work)
    sh("commit", "-qm", "base", cwd=work)
    sh("push", "-q", "origin", "main", cwd=work)
    sh("checkout", "-qb", "agent/x", cwd=work)
    (work / "docs" / "t.md").write_text("| h |\n| branch |\n")
    sh("commit", "-qam", "branch row", cwd=work)
    sh("push", "-q", "origin", "agent/x", cwd=work)
    sh("checkout", "-q", "main", cwd=work)
    (work / "docs" / "t.md").write_text("| h |\n| main |\n")
    sh("commit", "-qam", "main row", cwd=work)
    sh("push", "-q", "origin", "main", cwd=work)
    monkeypatch.setattr(pw, "REPO_DIR", work)
    monkeypatch.setattr(pw, "FIX_DIR", tmp_path / "fix")
    assert pw.merge_main("agent/x") == ("fixed", ["docs/t.md"])
    shown = subprocess.run(["git", "show", "agent/x:docs/t.md"], cwd=origin, capture_output=True, text=True, check=True)
    assert shown.stdout == "| h |\n| main |\n| branch |\n"
    # a code conflict is not touched: the branch on origin stays where it was
    sh("checkout", "-q", "agent/x", cwd=work)
    sh("pull", "-q", "--no-rebase", "origin", "agent/x", cwd=work)
    (work / "x.py").write_text("a = 2\n")
    sh("commit", "-qam", "branch code", cwd=work)
    sh("push", "-q", "origin", "agent/x", cwd=work)
    sh("checkout", "-q", "main", cwd=work)
    (work / "x.py").write_text("a = 3\n")
    sh("commit", "-qam", "main code", cwd=work)
    sh("push", "-q", "origin", "main", cwd=work)
    before = subprocess.run(["git", "rev-parse", "agent/x"], cwd=origin, capture_output=True, text=True).stdout
    assert pw.merge_main("agent/x") == ("blocked", ["не документація: x.py"])
    assert subprocess.run(["git", "rev-parse", "agent/x"], cwd=origin, capture_output=True, text=True).stdout == before
