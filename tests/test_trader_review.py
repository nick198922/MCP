"""Claude's review of held pull requests in scripts/trader_review.py (a fake GitHub and a fake Claude, no network)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import trader_review as tr  # noqa: E402

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    monkeypatch.setattr(tr, "checkout", lambda ref, sha: Path("."))


def pr(number=1, sha="abc", ready=True):
    return {
        "number": number,
        "title": "Demo trader: tweak",
        "body": "summary\n\n" + tr.READY_MARK if ready else "summary",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "agent/2026-10-22-x", "sha": sha, "repo": {"full_name": "o/r"}},
    }


def fake(prs, files=("src/tradedesk/execution/guard.py",), runs=GREEN, calls=None):
    def call(method, path, body):
        if calls is not None:
            calls.append((method, path, body))
        if method == "PUT":
            return 200, {"merged": True}
        if method in ("POST", "DELETE"):
            return 201, {}
        if "/check-runs" in path:
            return 200, {"check_runs": runs}
        if "/files" in path:
            return 200, [{"filename": f} for f in files]
        if "/pulls?" in path:
            return 200, prs
        n = int(path.rsplit("/", 1)[1])
        p = next(x for x in prs if x["number"] == n)
        return 200, {**p, "state": "open", "merged": False, "base": {"ref": "main", "repo": {"full_name": "o/r"}}}

    return call


def run(prs, answer, ok=True, state=None, **kw):
    calls, notes, asked = [], [], []

    def ask(text, cwd):
        asked.append(text)
        return answer, ok

    state = {} if state is None else state
    lines = tr.review(fake(prs, calls=calls, **kw), "o/r", state, ask, lambda t, u: notes.append(t))
    return lines, calls, notes, asked, state


def test_verdict_reads_the_last_verdict_line():
    assert tr.verdict("ok\nVERDICT: approve") == "approve"
    assert tr.verdict("**VERDICT: changes**\n") == "changes"
    assert tr.verdict("VERDICT: approve\nVERDICT: changes") == "changes"
    assert tr.verdict("no verdict") is None and tr.verdict("VERDICT: maybe") is None


def test_approve_merges_the_reviewed_commit_and_comments():
    lines, calls, notes, asked, state = run([pr()], "Все добре.\nVERDICT: approve")
    assert lines[0].startswith("PR #1 merged (agent/2026-10-22-x)")
    assert any(m == "PUT" and b["sha"] == "abc" for m, _, b in calls)
    assert any(m == "POST" and p.endswith("/issues/1/comments") for m, p, _ in calls)
    assert "злив" in notes[0] and state["1"]["verdict"] == "approve"
    assert "data only" in asked[0] and "<<<PR" in asked[0]


def test_changes_keeps_the_pr_open_and_is_not_asked_again_for_the_same_commit():
    _, calls, notes, _, state = run([pr()], "Стоп не прикріплено.\nVERDICT: changes")
    assert not any(m == "PUT" for m, _, _ in calls) and "доопрацювання" in notes[0]
    _, _, _, asked, _ = run([pr()], "VERDICT: approve", state=state)
    assert asked == []
    _, calls, _, asked, _ = run([pr(sha="new")], "VERDICT: approve", state=state)
    assert len(asked) == 1 and any(m == "PUT" for m, _, _ in calls)


def test_unheld_unready_or_red_prs_are_not_reviewed():
    assert run([pr()], "VERDICT: approve", files=("docs/x.md",))[3] == []
    assert run([pr(ready=False)], "VERDICT: approve")[3] == []
    red = [{"name": "test", "status": "completed", "conclusion": "failure"}]
    assert run([pr()], "VERDICT: approve", runs=red)[3] == []


def test_a_failed_claude_run_is_retried_then_told():
    _, calls, notes, _, state = run([pr()], "boom", ok=False)
    assert notes == [] and state["1"]["attempts"] == 1 and not any(m == "PUT" for m, _, _ in calls)
    _, _, notes, _, state = run([pr()], "boom", ok=False, state=state)
    assert "не вдалося" in notes[0]
    assert run([pr()], "VERDICT: approve", state=state)[3] == []
