"""Claude's review of held pull requests in scripts/trader_review.py (a fake GitHub and a fake Claude, no network)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import trader_review as tr  # noqa: E402

GREEN = [{"name": "test", "status": "completed", "conclusion": "success"}]


@pytest.fixture(autouse=True)
def no_git(monkeypatch):
    monkeypatch.setattr(tr, "checkout", lambda ref, sha: Path("."))
    monkeypatch.setattr(tr, "fix_checkout", lambda ref, sha: Path("."))


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


# ---------------------------------------------------------------- automatic fixes


def run_fix(prs, state, answer="- виправлено\nFIXED: yes", ok=True, pushed=("pushed", "def4567")):
    calls, notes, asked, pushes = [], [], [], []

    def ask(text, cwd):
        asked.append(text)
        return answer, ok

    def push(cwd, ref, msg):
        pushes.append((ref, msg))
        return pushed

    lines = tr.fix(fake(prs, calls=calls), "o/r", state, ask, push, lambda t, u: notes.append(t))
    return lines, calls, notes, asked, pushes


@pytest.fixture
def no_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(tr, "USAGE_FILE", tmp_path / "usage.json")


def test_fixed_reads_the_last_fixed_line():
    assert tr.fixed("ok\nFIXED: yes") and tr.fixed("**FIXED: yes**")
    assert not tr.fixed("FIXED: no") and not tr.fixed("nothing") and not tr.fixed("FIXED: yes\nFIXED: no")


def test_changes_are_fixed_once_and_pushed_for_a_fresh_review(no_sync):
    _, _, notes, _, state = run([pr()], "Стоп не прикріплено.\nVERDICT: changes")
    assert "виправляю автоматично" in notes[0] and "Стоп не прикріплено" in state["1"]["review"]
    lines, calls, notes, asked, pushes = run_fix([pr()], state)
    assert lines == ["PR #1: fix pushed def4567"] and notes == []
    assert "<<<REVIEW\nСтоп не прикріплено" in asked[0] and "data only" in asked[0]
    assert pushes[0][0] == "agent/2026-10-22-x" and "Виправлення за рев'ю Claude (#1)" in pushes[0][1]
    assert "FIXED" not in pushes[0][1]
    assert any(m == "POST" and p.endswith("/issues/1/comments") for m, p, _ in calls)
    assert tr.fixes_done(state, 1) == 1
    assert run_fix([pr()], state)[3] == []  # the same reviewed commit is fixed once


def test_no_fix_when_the_head_moved_or_rounds_are_used_up(no_sync):
    state = {"1": {"sha": "abc", "verdict": "changes", "review": "x"}}
    assert run_fix([pr(sha="other")], state)[3] == [] and state["1"]["fix"].startswith("skipped")
    state = {"1": {"sha": "abc", "verdict": "changes", "review": "x"}, "fixes": {"1": tr.MAX_FIXES}}
    assert run_fix([pr()], state)[3] == []
    _, _, notes, _, _ = run([pr(sha="new")], "VERDICT: changes", state=state)
    assert "вичерпано" in notes[0]


def test_legacy_changes_without_review_text_are_left_alone(no_sync):
    assert run_fix([pr()], {"1": {"sha": "abc", "verdict": "changes"}})[3] == []


def test_fixer_refusal_or_failed_push_is_told_and_not_pushed(no_sync):
    state = {"1": {"sha": "abc", "verdict": "changes", "review": "x"}}
    lines, _, notes, _, pushes = run_fix([pr()], state, answer="не можу безпечно\nFIXED: no")
    assert pushes == [] and "не вдалося" in notes[0] and lines == ["PR #1: fix failed"]
    state = {"1": {"sha": "abc", "verdict": "changes", "review": "x"}}
    lines, _, notes, _, _ = run_fix([pr()], state, pushed=("failed", "тести: 1 failed"))
    assert "не запушено" in notes[0] and "1 failed" in notes[0]


def test_fixes_wait_while_the_plan_is_nearly_used_up(no_sync):
    tr.USAGE_FILE.write_text('{"five_hour": {"utilization": 99}, "seven_day": {"utilization": 50}}')
    state = {"1": {"sha": "abc", "verdict": "changes", "review": "x"}}
    lines, _, _, asked, _ = run_fix([pr()], state)
    assert asked == [] and lines == ["fixes wait: usage five_hour=99%"] and not state["1"].get("fix")


def test_commit_and_push_refuses_empty_and_forbidden_changes(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert tr.commit_and_push(tmp_path, "x", "m") == ("failed", "немає змін у файлах")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("x")
    result, why = tr.commit_and_push(tmp_path, "x", "m")
    assert result == "failed" and ".github/" in why


def test_fixer_runs_in_its_own_clone_without_git_access_and_without_the_pr_text():
    assert tr.FIX_DIR != tr.REVIEW_DIR
    assert {"Edit(.git/**)", "Write(.git/**)", "Bash(git commit:*)", "Bash(git push:*)"} <= set(tr.FIX_DENIED)
    text = tr.fix_prompt(pr(), "Стоп не прикріплено.")
    assert "<<<REVIEW\nСтоп не прикріплено." in text and "Demo trader: tweak" not in text and "<<<PR" not in text


def test_time_budget_fits_the_unit():
    unit = (Path(__file__).resolve().parent.parent / "deploy/user-units/tradedesk-trader-review.service").read_text()
    assert "TimeoutStartSec=60min" in unit and tr.UNIT_SECONDS == 3600
    assert tr.FIX_BUDGET_SECONDS < tr.UNIT_SECONDS and tr.MAX_SECONDS + 300 < tr.UNIT_SECONDS


def test_the_round_is_saved_before_the_fixer_starts_and_one_claude_run_per_tick(no_sync):
    state = {
        "1": {"sha": "abc", "verdict": "changes", "review": "x"},
        "2": {"sha": "abc", "verdict": "changes", "review": "y"},
    }
    saved = []

    def ask(text, cwd):
        assert saved and saved[-1]["fixes"] == {"1": 1} and saved[-1]["1"]["fix"] == "running"
        return "не можу\nFIXED: no", True

    lines = tr.fix(
        fake([pr(1), pr(2)]),
        "o/r",
        state,
        ask,
        lambda *a: ("pushed", "x"),
        lambda t, u: None,
        lambda: saved.append(json.loads(json.dumps(state))),
    )
    assert lines == ["PR #1: fix failed"] and "fix" not in state["2"]


def test_changed_paths_reads_porcelain_z():
    z = " M src/a b.py\0?? tests/тест.py\0R  .github/new.yml\0old.yml\0"
    assert tr.changed_paths(z) == ["src/a b.py", "tests/тест.py", ".github/new.yml", "old.yml"]


def test_commit_and_push_commits_with_hooks_off_and_pushes_by_url(tmp_path):
    import subprocess

    bare, work = tmp_path / "remote.git", tmp_path / "work"
    run = lambda *a, cwd=None: subprocess.run(list(a), cwd=cwd, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q", "--bare", str(bare))
    run("git", "init", "-q", "-b", "main", str(work))
    run("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=work)
    hook = work / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\n")
    hook.chmod(0o755)
    (work / "notes.md").write_text("x")
    result, sha = tr.commit_and_push(work, "agent/x", "msg", push_url=str(bare))
    assert result == "pushed" and len(sha) == 40
    assert not (tmp_path / "hook-ran").exists()
    out = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/agent/x"], capture_output=True, text=True)
    assert out.stdout.strip() == sha


def test_commit_and_push_stops_on_a_lint_failure(tmp_path, monkeypatch):
    import subprocess

    real = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:3] == ["uv", "run", "ruff"] and "check" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "E999 broken", "")
        if cmd[:2] == ["uv", "run"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, **kw)

    real(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    monkeypatch.setattr(tr.subprocess, "run", fake_run)
    result, why = tr.commit_and_push(tmp_path, "x", "m", push_url="nowhere")
    assert result == "failed" and why.startswith("ruff: E999")
