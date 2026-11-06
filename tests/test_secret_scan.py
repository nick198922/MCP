"""scripts/secret_scan.py (item 151 of docs/ideas/IDEA-20261102-standing-improvements.md): secret scan.

Fake secrets are assembled at run time from pieces, so this file itself does not trip the scanner on the repo."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import secret_scan as ss  # noqa: E402

TG = "7412589630" + ":" + "AAHk3Jd9QwPz7LmN2vBx5RtY8uIo1EaSdFg"  # 35 chars after the colon
KEY = "Zq8" + "Lp3Wn7Rt5Vy2Kd9M"
BEARER = "eyJh" + "bGciOiJIUzI1NiJ9.k3Jd9QwPz7"
PWD = "c2Vj" + "cmV0LXBhc3M9MTIzNDU2Nzg"
HASH = "78d12eab" + "4f0c9a1e2b3d5f6a7c8e9d0b1a2c3e4f"  # 40 hex, like a git commit id


def test_patterns_found_and_masked():
    assert len(TG.split(":")[1]) == 35
    text = "\n".join(
        [
            f"url = 'https://api.telegram.org/bot{TG}/getMe'",
            f"BYBIT_API_KEY={KEY}",
            f'headers = {{"Authorization": "Bearer {BEARER}"}}',
            f"password={PWD}",
        ]
    )
    found = ss.find_secrets(text, "a/b.py")
    assert [(f.line, f.kind) for f in found] == [(1, "telegram"), (2, "api_key"), (3, "bearer"), (4, "assign")]
    assert [f.masked for f in found] == [TG[:4] + "…", KEY[:4] + "…", BEARER[:4] + "…", PWD[:4] + "…"]
    assert all(f.path == "a/b.py" for f in found)
    assert found[0].length == len(TG)
    # The raw value is never kept in a finding or in the rendered report.
    report = ss.render(ss.Report(files=1, scanned=1, findings=found))
    for raw in (TG, KEY, BEARER, PWD):
        assert raw not in report
        assert raw not in repr(found)


def test_one_finding_per_value_when_patterns_overlap():
    found = ss.find_secrets(f"api_secret = '{KEY}9Xy4Bc'", "x.py")
    assert [f.kind for f in found] == ["api_key"]


def test_placeholders_skipped():
    text = "\n".join(
        [
            "TELEGRAM_TOKEN=<your-bot-token>",
            "api_key = 'xxxxxxxxxxxxxxxxxxxx'",
            "api_secret: REDACTED_REDACTED_1234",
            "BYBIT_API_KEY=dummyKey1234567890abc",
            "token=example0123456789abcdefgh",
            "password=test_password_1234567890",
            "secret=${TRADEDESK_SECRET}",
            "Authorization: Bearer 00000000000000000000000000",
            "Authorization: Bearer <TOKEN>",
            "password=settings.demo_password",
            "token: telegram_bot_token_from_environment",
        ]
    )
    assert ss.find_secrets(text, "deploy/env.example") == []


def test_commit_hash_hex_not_flagged():
    assert len(HASH) == 40
    text = f"commit {HASH}\nsha256: {HASH}{HASH[:24]}\nMerge {HASH[:8]} into main\n"
    assert ss.find_secrets(text, "docs/HANDOFF.md") == []


def test_evm_address_after_token_not_flagged():
    addr = "0x" + HASH
    assert ss.find_secrets(f'{{"args": {{"token": "{addr}"}}}}', "f.json") == []
    # A 64-hex value (private-key length) after the same keyword is still flagged.
    assert [f.kind for f in ss.find_secrets(f"token={addr}{HASH[:24]}", "f.env")] == ["assign"]


def test_binary_detection():
    assert ss.is_binary(b"PNG\x00\x01\x02")
    assert ss.is_binary(b"\xff\xfe\xfa not utf-8")
    assert not ss.is_binary("plain текст".encode())


def test_scan_skips_binary_and_symlinks(tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00" + f"api_key={KEY}".encode())
    (tmp_path / "conf.env").write_text(f"api_key={KEY}\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text(f"api_key={KEY}\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    r = ss.scan(tmp_path, ["bin.dat", "conf.env", "link.txt"])
    assert (r.files, r.scanned, r.binary) == (3, 1, 1)
    assert [(f.path, f.kind) for f in r.findings] == [("conf.env", "api_key")]
    assert r.skipped == ["link.txt (symlink or not a file)"]


def test_main_writes_out_from_git_ls_files(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.md").write_text(f"token={PWD}\n", encoding="utf-8")
    (repo / "untracked.md").write_text(f"token={PWD}\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.md"], cwd=repo, check=True)
    out = tmp_path / "o" / "scan.md"
    assert ss.main(["--root", str(repo), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text == capsys.readouterr().out
    assert "Tracked files: 1;" in text
    assert "findings: 1." in text
    assert f"`a.md:1` assign: `{PWD[:4]}…`" in text
    assert PWD not in text
