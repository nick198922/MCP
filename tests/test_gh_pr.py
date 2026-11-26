"""Opening the pull request of an agent branch in scripts/gh_pr.py (a fake GitHub, no network)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gh_pr  # noqa: E402


class FakeGitHub:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self.answers.pop(0)


def test_opens_a_new_pull_request():
    gh = FakeGitHub((201, {"html_url": "https://github.com/o/r/pull/7"}))
    url = gh_pr.open_pr(gh, "o/r", "agent/2026-10-14-x", "Put-skew v2", "summary " * 20000)
    assert url == "https://github.com/o/r/pull/7"
    method, path, body = gh.calls[0]
    assert (method, path) == ("POST", "/repos/o/r/pulls")
    assert body["head"] == "agent/2026-10-14-x" and body["base"] == "main" and body["title"] == "Put-skew v2"
    assert len(body["body"]) == gh_pr.BODY_LIMIT


def test_returns_the_existing_pull_request_of_the_branch():
    exists = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:agent/x."}]}
    gh = FakeGitHub((422, exists), (200, [{"html_url": "https://github.com/o/r/pull/3"}]))
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b") == "https://github.com/o/r/pull/3"
    assert gh.calls[1][0] == "GET" and "head=o%3Aagent%2Fx" in gh.calls[1][1] and "state=open" in gh.calls[1][1]


def test_a_ready_delivery_marks_the_existing_unmarked_pull_request():
    # 18.11: btc_charlie's branch was handed over "stuck" (an unmarked PR); its later delivery must mark that PR.
    exists = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:agent/x."}]}
    unmarked = [{"number": 5, "html_url": "https://github.com/o/r/pull/5", "body": "stuck as is"}]
    gh = FakeGitHub((422, exists), (200, unmarked), (200, {}))
    body = "done\n\n" + gh_pr.READY_MARK
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "Done", body) == "https://github.com/o/r/pull/5"
    method, path, patch = gh.calls[2]
    assert (method, path) == ("PATCH", "/repos/o/r/pulls/5") and gh_pr.READY_MARK in patch["body"] and patch["title"] == "Done"


def test_an_existing_pull_request_is_left_alone_unless_it_needs_the_mark():
    exists = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:agent/x."}]}
    marked = [{"number": 5, "html_url": "u5", "body": "x " + gh_pr.READY_MARK}]
    gh = FakeGitHub((422, exists), (200, marked))
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "t", "again " + gh_pr.READY_MARK) == "u5" and len(gh.calls) == 2
    gh = FakeGitHub((422, exists), (200, [{"number": 6, "html_url": "u6", "body": "stuck"}]))
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "t", "stuck again") == "u6" and len(gh.calls) == 2


def test_a_refused_mark_is_reported():
    exists = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:agent/x."}]}
    gh = FakeGitHub((422, exists), (200, [{"number": 5, "html_url": "u5", "body": ""}]), (403, {"message": "Forbidden"}))
    with pytest.raises(RuntimeError, match="403 marking PR #5 ready: Forbidden"):
        gh_pr.open_pr(gh, "o/r", "agent/x", "t", gh_pr.READY_MARK)


def test_a_refusal_names_github_reason():
    gh = FakeGitHub((403, {"message": "Resource not accessible by personal access token"}))
    with pytest.raises(RuntimeError, match="403.*not accessible"):
        gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b")


def test_a_transient_github_error_is_retried():
    # 25.11: one HTTP 500 left agent/2026-11-25-deribit-collector-gaps without a pull request, so it never merged.
    waits = []
    gh = FakeGitHub((500, {}), (0, {"message": "URLError"}), (201, {"html_url": "u7"}))
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b", sleep=waits.append) == "u7"
    assert waits == list(gh_pr.RETRY_DELAYS[:2]) and gh.calls[0] == gh.calls[2]


def test_a_retry_finds_the_pull_request_the_failed_request_created():
    exists = {"message": "Validation Failed", "errors": [{"message": "A pull request already exists for o:agent/x."}]}
    gh = FakeGitHub((502, {}), (422, exists), (200, [{"html_url": "u8", "body": ""}]))
    assert gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b", sleep=lambda _: None) == "u8"


def test_a_lasting_github_error_is_reported_after_the_retries():
    gh = FakeGitHub(*[(500, {})] * (len(gh_pr.RETRY_DELAYS) + 1))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b", sleep=lambda _: None)
    assert not gh.answers


def test_check_tells_a_read_only_token_apart():
    ok = FakeGitHub((200, []), (422, {"message": "Validation Failed", "errors": [{"field": "head", "code": "invalid"}]}))
    assert "read and open" in gh_pr.check(ok, "o/r")
    assert ok.calls[1][2]["head"] == gh_pr.PROBE_BRANCH
    read_only = FakeGitHub((200, []), (403, {"message": "Resource not accessible by personal access token"}))
    with pytest.raises(RuntimeError, match="can read but not open"):
        gh_pr.check(read_only, "o/r")


def test_without_a_token_it_fails_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gh_pr, "TOKEN_FILE", tmp_path / "missing.env")
    assert gh_pr.main(["--repo", "o/r", "--head", "agent/x"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "no GITHUB_TOKEN" in out.err
