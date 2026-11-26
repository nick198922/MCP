"""Item 469: tradedesk-update restarts tradedesk-mcp only when something the service reads changed.

scripts/restart_needed.sh (the decision, run as tradedesk) on a temporary git repository, and the update script that
scripts/server/31-update-restart-code-only.sh installs, run with stubbed runuser / systemctl / uv. No network, no root.
"""

import getpass
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "restart_needed.sh"
STEP = ROOT / "scripts" / "server" / "31-update-restart-code-only.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None, reason="needs bash and git")


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n[pull]\n\tff = only\n")
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
    return {**keep, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, env: dict[str, str], files: dict[str, str | None], msg: str = "c") -> str:
    """Write (str) or delete (None) the files, commit everything, return the full sha."""
    for name, text in files.items():
        p = repo / name
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    git(repo, env, "add", "-A")
    git(repo, env, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, env, "rev-parse", "HEAD")


BASE = {
    "src/tradedesk/server.py": "x = 1\n",
    "src/tradedesk/features.py": "f = 1\n",
    "deploy/features.env": "TRADEDESK_X_ENABLED=1\n",
    "deploy/user-units/tradedesk-x.timer": "[Timer]\n",
    "docs/HANDOFF.md": "a\n",
    "docs/old.md": "o\n",
    "tests/test_x.py": "t = 1\n",
    "scripts/verify_x.py": "v = 1\n",
    "pyproject.toml": "[project]\n",
    "uv.lock": "lock\n",
    "README.md": "r\n",
}


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, env, "init", "-q")
    commit(r, env, BASE)
    return r


def decide(repo: Path, env: dict[str, str], deployed: str, *extra: str) -> str:
    res = subprocess.run(["bash", str(HELPER), str(repo), deployed, *extra], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert len(lines) == 1, res.stdout  # exactly one answer line
    return lines[0]


def test_same_commit_skips(repo, env):
    head = git(repo, env, "rev-parse", "HEAD")
    assert decide(repo, env, head) == "skip"


@pytest.mark.parametrize(
    "files",
    [
        {"docs/HANDOFF.md": "b\n"},
        {"docs/new/card.md": "n\n", "docs/old.md": None},
        {"tests/test_x.py": "t = 2\n", "scripts/verify_x.py": "v = 2\n", "scripts/server/31-x.sh": "#!/bin/bash\n"},
        {".claude/nightly.md": "n\n", ".github/workflows/ci.yml": "on: push\n"},
        {"README.md": "r2\n", "CHANGELOG.md": "c\n", "CLAUDE.md": "c\n"},
        {"deploy/user-units/tradedesk-x.timer": "[Timer]\nOnCalendar=daily\n", "deploy/user-units/tradedesk-y.service": "[Service]\n"},
    ],
    ids=["docs", "docs-add-delete", "tests-scripts", "agents-ci", "top-markdown", "user-units"],
)
def test_paths_the_service_never_reads_skip(repo, env, files):
    deployed = git(repo, env, "rev-parse", "HEAD")
    commit(repo, env, files)
    assert decide(repo, env, deployed) == "skip"


@pytest.mark.parametrize(
    "files",
    [
        {"src/tradedesk/server.py": "x = 2\n"},
        {"deploy/features.env": "TRADEDESK_X_ENABLED=0\n"},
        {"deploy/coingecko_extra_ids.txt": "bitcoin\n"},
        {"pyproject.toml": "[project]\nname = 'x'\n"},
        {"uv.lock": "lock2\n"},
        {"docs/HANDOFF.md": "b\n", "src/tradedesk/features.py": "f = 2\n"},
        {"Makefile": "all:\n"},
        {"deploy/README.md": "d\n"},
        {"docs/a b.md": "space\n"},
        {"docs/нотатка.md": "non-ascii\n"},
    ],
    ids=["src", "features-env", "coingecko-ids", "pyproject", "uv-lock", "mixed", "new-top-level", "deploy-other", "space", "non-ascii"],
)
def test_anything_else_restarts(repo, env, files):
    deployed = git(repo, env, "rev-parse", "HEAD")
    commit(repo, env, files)
    assert decide(repo, env, deployed).startswith("restart: ")


def test_rename_from_src_to_docs_restarts(repo, env):
    deployed = git(repo, env, "rev-parse", "HEAD")
    git(repo, env, "mv", "src/tradedesk/server.py", "docs/server.py")
    commit(repo, env, {})
    assert decide(repo, env, deployed) == "restart: src/tradedesk/server.py changed"


def test_rename_inside_docs_skips(repo, env):
    deployed = git(repo, env, "rev-parse", "HEAD")
    git(repo, env, "mv", "docs/old.md", "docs/renamed.md")
    commit(repo, env, {})
    assert decide(repo, env, deployed) == "skip"


def test_src_change_reverted_later_skips_on_the_same_tree(repo, env):
    deployed = git(repo, env, "rev-parse", "HEAD")
    commit(repo, env, {"src/tradedesk/server.py": "x = 2\n"})
    assert decide(repo, env, deployed).startswith("restart: ")
    commit(repo, env, {"src/tradedesk/server.py": "x = 1\n", "docs/HANDOFF.md": "b\n"})
    assert decide(repo, env, deployed) == "skip"  # the service already runs this src/ (stamp is older, tree diff is what counts)


@pytest.mark.parametrize(
    "deployed",
    ["", "none", "abc1234", "0" * 40, "f" * 40, "HEAD", "-" + "a" * 39, "A" * 40, "a" * 40 + "\n"],
    ids=["empty", "none", "short", "zeros", "unknown", "ref-name", "option", "upper", "newline"],
)
def test_missing_or_unknown_deployed_commit_restarts(repo, env, deployed):
    assert decide(repo, env, deployed).startswith("restart: ")


def test_short_sha_of_a_real_commit_restarts(repo, env):
    head = git(repo, env, "rev-parse", "HEAD")
    assert decide(repo, env, head[:7]).startswith("restart: ")


def test_bad_repo_or_arguments_restart(repo, env, tmp_path):
    head = git(repo, env, "rev-parse", "HEAD")
    assert decide(tmp_path / "nowhere", env, head).startswith("restart: ")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert decide(plain, env, head).startswith("restart: ")
    assert decide(repo, env, head, "extra").startswith("restart: ")


def test_blob_id_is_not_a_commit(repo, env):
    blob = git(repo, env, "rev-parse", "HEAD:README.md")
    assert decide(repo, env, blob).startswith("restart: ")


def test_git_env_of_the_caller_is_ignored(repo, env, tmp_path):
    deployed = git(repo, env, "rev-parse", "HEAD")
    commit(repo, env, {"src/tradedesk/server.py": "x = 3\n"})
    other = tmp_path / "other"
    other.mkdir()
    git(other, env, "init", "-q")
    commit(other, env, {"docs/a.md": "a\n"})
    assert decide(repo, {**env, "GIT_DIR": str(other / ".git")}, deployed).startswith("restart: ")


# --- the update script of step 31, with stubs ---------------------------------------------------------------------


def update_script(repo: Path, stubs: Path, stamp_dir: Path) -> str:
    text = STEP.read_text()
    m = re.search(r"<<'EOF'\n(.*?)\nEOF\n", text, re.S)
    assert m, "heredoc of the update script not found"
    body = m.group(1)
    for line, new in (
        ("SVC=tradedesk", f"SVC={getpass.getuser()}"),
        ("REPO=/home/tradedesk/tradedesk-mcp", f"REPO={repo}"),
        ("UV=/home/tradedesk/.local/bin/uv", f"UV={stubs / 'uv'}"),
        ("RUNUSER=/usr/sbin/runuser", f"RUNUSER={stubs / 'runuser'}"),
        ("STAMP_DIR=/var/lib/tradedesk-update", f"STAMP_DIR={stamp_dir}"),
    ):
        body, n = re.subn(rf"^{re.escape(line)}$", new, body, flags=re.M)
        assert n == 1, line
    return body + "\n"


class Desk:
    def __init__(self, tmp_path: Path, env: dict[str, str]) -> None:
        self.env = env
        self.origin = tmp_path / "origin"
        self.origin.mkdir()
        git(self.origin, env, "init", "-q")
        units = "#!/bin/bash\necho synced\n"
        self.first = commit(
            self.origin, env, {**BASE, "scripts/restart_needed.sh": HELPER.read_text(), "scripts/user_units_sync.sh": units}
        )
        self.clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], env=env, check=True)
        self.stubs = stubs = tmp_path / "stubs"
        stubs.mkdir()
        self.log = tmp_path / "calls.log"
        self.runuser_log = tmp_path / "runuser.log"
        self.timeout_log = tmp_path / "timeout.log"
        self.uv_fail = tmp_path / "uv-fail"
        self.inactive = tmp_path / "service-inactive"
        self.timed_out = tmp_path / "helper-timed-out"
        real_git = shutil.which("git")
        for name, body in (
            # Records its argv, marks what it runs, drops "-u <user> --" (the tests run as one user).
            (
                "runuser",
                f'echo "runuser $*" >>{self.runuser_log}\nexport UNDER_RUNUSER=1\n'
                '[[ "$1" == -u ]] && shift 2\n[[ "$1" == -- ]] && shift\nexec "$@"\n',
            ),
            # Any git run by the update script (or by the helper it starts) outside runuser fails loudly.
            ("git", f'[[ -n "${{UNDER_RUNUSER:-}}" ]] || {{ echo "git outside runuser" >&2; exit 99; }}\nexec {real_git} "$@"\n'),
            (
                "systemctl",
                f'if [[ "$1" == is-active ]]; then [[ -e {self.inactive} ]] && exit 3; exit 0; fi\necho "systemctl $*" >>{self.log}\n',
            ),
            # Records its argv; with the flag it answers like a timeout (124) without running or waiting.
            ("timeout", f'echo "timeout $*" >>{self.timeout_log}\n[[ -e {self.timed_out} ]] && exit 124\nshift 3\nexec "$@"\n'),
            ("uv", f'[[ -e {self.uv_fail} ]] && exit 1\necho "uv $*" >>{self.log}\n'),
        ):
            (stubs / name).write_text("#!/bin/bash\n" + body)
            (stubs / name).chmod(0o755)
        self.stamp = tmp_path / "stamp" / "deployed"
        self.script = tmp_path / "tradedesk-update"
        self.script.write_text(update_script(self.clone, stubs, self.stamp.parent))
        self.run_env = {**env, "PATH": f"{stubs}:{env['PATH']}"}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.unlink(missing_ok=True)
        return subprocess.run(["bash", str(self.script), *args], env=self.run_env, capture_output=True, text=True, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def merge(self, files: dict[str, str | None]) -> str:
        return commit(self.origin, self.env, files)


RESTARTED = ["uv --directory {repo} sync --quiet --extra dev", "systemctl restart tradedesk-mcp.service"]


@pytest.fixture
def desk(tmp_path, env) -> Desk:
    return Desk(tmp_path, env)


def restarted(d: Desk) -> bool:
    calls = d.calls()
    assert calls in ([], [c.format(repo=d.clone) for c in RESTARTED]), calls
    return bool(calls)


def test_generated_update_script_parses():
    res = subprocess.run(
        ["bash", "-n"], input=update_script(Path("/r"), Path("/s"), Path("/d")), capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr


def test_update_restarts_without_stamp_then_only_on_code(desk):
    r = desk.run()
    assert r.returncode == 0, r.stderr
    assert restarted(desk) and "synced" in r.stdout
    assert desk.stamp.read_text() == desk.first + "\n"

    r = desk.run()  # nothing new
    assert r.returncode == 0 and not restarted(desk) and "no restart" in r.stdout and "synced" in r.stdout

    desk.merge({"docs/HANDOFF.md": "docs only\n"})
    r = desk.run()
    assert r.returncode == 0 and not restarted(desk), r.stdout
    assert desk.stamp.read_text() == desk.first + "\n"  # the stamp names the commit the service runs

    code = desk.merge({"src/tradedesk/server.py": "x = 9\n"})
    r = desk.run()
    assert r.returncode == 0 and restarted(desk), r.stdout + r.stderr
    assert "src/tradedesk/server.py changed" in r.stdout
    assert desk.stamp.read_text() == code + "\n"


def test_force_always_restarts(desk):
    assert desk.run().returncode == 0
    r = desk.run("--force")
    assert r.returncode == 0 and restarted(desk)


def test_failed_sync_is_not_lost(desk):
    assert desk.run().returncode == 0
    code = desk.merge({"pyproject.toml": "[project]\nname = 'y'\n"})
    desk.uv_fail.touch()
    r = desk.run()
    assert r.returncode != 0 and desk.calls() == []  # no restart after a failed sync, stamp unchanged
    assert desk.stamp.read_text() == desk.first + "\n"
    desk.uv_fail.unlink()
    r = desk.run()  # HEAD did not move since the failed run, yet the service still runs the old code
    assert r.returncode == 0 and restarted(desk)
    assert desk.stamp.read_text() == code + "\n"


def test_pull_by_someone_else_is_not_lost(desk):
    assert desk.run().returncode == 0
    desk.merge({"src/tradedesk/server.py": "x = 5\n"})
    git(desk.clone, desk.env, "pull", "-q", "--ff-only")  # e.g. another scripts/server step pulled the clone
    r = desk.run()
    assert r.returncode == 0 and restarted(desk)


@pytest.mark.parametrize("stamp", ["", "garbage\n", "0" * 40 + "\n"], ids=["empty", "garbage", "unknown"])
def test_bad_stamp_restarts(desk, stamp):
    assert desk.run().returncode == 0
    desk.stamp.write_text(stamp)
    r = desk.run()
    assert r.returncode == 0 and restarted(desk)
    assert desk.stamp.read_text() == desk.first + "\n"


def test_missing_or_broken_helper_restarts(desk):
    assert desk.run().returncode == 0
    desk.merge({"scripts/restart_needed.sh": None})  # a scripts/ change, but the helper is gone
    r = desk.run()
    assert r.returncode == 0 and restarted(desk)
    desk.merge({"scripts/restart_needed.sh": "#!/bin/bash\necho skipping\nexit 3\n"})
    r = desk.run()
    assert r.returncode == 0 and restarted(desk)
    desk.merge({"scripts/restart_needed.sh": "#!/bin/bash\necho ' skip'\n"})
    r = desk.run()
    assert r.returncode == 0 and restarted(desk)
    desk.merge({"scripts/restart_needed.sh": "#!/bin/bash\necho skipx\nexit 0\n"})
    r = desk.run()
    assert r.returncode == 0 and restarted(desk) and "restart needed: skipx" in r.stdout


def test_helper_timeout_restarts(desk):
    assert desk.run().returncode == 0
    desk.merge({"docs/HANDOFF.md": "docs only\n"})
    desk.timeout_log.unlink(missing_ok=True)
    desk.timed_out.touch()
    r = desk.run()
    assert r.returncode == 0 and restarted(desk) and "helper failed" in r.stdout
    user, repo = getpass.getuser(), desk.clone
    assert desk.timeout_log.read_text().splitlines() == [
        f"timeout -k 5 120 {desk.stubs / 'runuser'} -u {user} -- bash {repo}/scripts/restart_needed.sh {repo} {desk.first}"
    ]


def test_repository_commands_run_as_the_service_user(desk):
    desk.merge({"docs/HANDOFF.md": "docs only\n"})  # the pull moves HEAD
    r = desk.run()
    assert r.returncode == 0 and restarted(desk), r.stderr
    user, repo = getpass.getuser(), desk.clone
    lines = desk.runuser_log.read_text().splitlines()
    assert lines and all(x.startswith(f"runuser -u {user} -- ") for x in lines), lines
    cmds = [x.removeprefix(f"runuser -u {user} -- ") for x in lines]
    for want in (
        f"git -C {repo} rev-parse HEAD",
        f"git -C {repo} pull -q --ff-only",
        f"bash {repo}/scripts/restart_needed.sh {repo} none",
        f"{desk.stubs / 'uv'} --directory {repo} sync --quiet --extra dev",
    ):
        assert want in cmds, (want, cmds)
    assert any(c.endswith(f"bash {repo}/scripts/user_units_sync.sh") for c in cmds), cmds


def test_stopped_service_is_started_by_a_merge_only(desk):
    assert desk.run().returncode == 0
    desk.inactive.touch()  # e.g. stopped by hand, or exited and not restarted by systemd
    r = desk.run()  # main did not move: a service stopped by hand stays stopped
    assert r.returncode == 0 and not restarted(desk) and "no restart" in r.stdout
    docs = desk.merge({"docs/HANDOFF.md": "docs only\n"})
    desk.timeout_log.unlink(missing_ok=True)
    r = desk.run()  # a docs-only merge: the helper would say skip, but the service is down
    assert r.returncode == 0 and restarted(desk) and "service not active and main moved" in r.stdout
    assert not desk.timeout_log.exists()  # the helper was not asked
    assert desk.stamp.read_text() == docs + "\n"
    r = desk.run()  # still "inactive" in the stub, main unchanged
    assert r.returncode == 0 and not restarted(desk)
    desk.inactive.unlink()
    desk.merge({"docs/HANDOFF.md": "docs again\n"})
    r = desk.run()  # active service, docs-only merge: no restart
    assert r.returncode == 0 and not restarted(desk)


def test_skip_list_is_pinned():
    """Any change to the paths that skip a restart must show up in this test's diff (critic's remark 3a)."""
    text = HELPER.read_text()
    assert re.findall(r"^SKIP_DIRS=.*$", text, re.M) == ["SKIP_DIRS=(docs/ tests/ scripts/ .claude/ .github/ deploy/user-units/)"]
    assert re.findall(r"^SKIP_FILES=.*$", text, re.M) == ["SKIP_FILES=(README.md CHANGELOG.md CLAUDE.md)"]


# --- the preflight of step 31 (before anything is installed), with stubs -------------------------------------------


def preflight(desk: Desk, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    text = STEP.read_text()
    head, sep, _ = text.partition('tmp="$(mktemp)"')
    assert sep, "end of the preflight not found"
    dst = tmp_path / "installed-update"
    dst.write_text("#!/bin/bash\n")
    dst.chmod(0o755)
    for line, new in (
        ("SVC=tradedesk", f"SVC={getpass.getuser()}"),
        ('REPO="/home/$SVC/tradedesk-mcp"', f"REPO={desk.clone}"),
        ("DST=/usr/local/sbin/tradedesk-update", f"DST={dst}"),
    ):
        head, n = re.subn(rf"^{re.escape(line)}$", new, head, flags=re.M)
        assert n == 1, line
    script = tmp_path / "preflight.sh"
    script.write_text(head + "\necho PREFLIGHT_OK\n")
    return subprocess.run(["bash", str(script)], env=desk.run_env, capture_output=True, text=True, check=False)


def test_step31_preflight_passes_with_both_scripts(desk, tmp_path):
    r = preflight(desk, tmp_path)
    assert r.returncode == 0 and "PREFLIGHT_OK" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("missing", ["scripts/restart_needed.sh", "scripts/user_units_sync.sh"])
def test_step31_preflight_stops_when_a_script_is_not_in_main(desk, tmp_path, missing):
    desk.merge({missing: None})  # main without the file: the preflight pulls it and must stop before installing
    r = preflight(desk, tmp_path)
    assert r.returncode != 0 and "PREFLIGHT_OK" not in r.stdout, r.stdout
    assert f"no {missing} in {desk.clone}" in r.stdout
    assert not (desk.clone / missing).exists()
