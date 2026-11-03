"""Shell scripts in scripts/ parse with `bash -n`.

The agents loop re-execs itself when its file changes (scripts/agents_loop.sh), so a syntax error merged to main
stops the loop until someone notices; this catches it in CI instead.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parent.parent / "scripts").rglob("*.sh"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_shell_script_parses(path: Path) -> None:
    res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
