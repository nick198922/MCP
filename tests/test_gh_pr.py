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


def test_a_refusal_names_github_reason():
    gh = FakeGitHub((403, {"message": "Resource not accessible by personal access token"}))
    with pytest.raises(RuntimeError, match="403.*not accessible"):
        gh_pr.open_pr(gh, "o/r", "agent/x", "t", "b")


def test_without_a_token_it_fails_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gh_pr, "TOKEN_FILE", tmp_path / "missing.env")
    assert gh_pr.main(["--repo", "o/r", "--head", "agent/x"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and "no GITHUB_TOKEN" in out.err
