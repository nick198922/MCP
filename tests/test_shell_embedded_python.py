"""Guard: Python embedded in `.sh` scripts compiles and does not reach the network unnoticed.

Item 247 of docs/ideas/IDEA-20261102-standing-improvements.md. Shell scripts in `scripts/` (server steps, owner
diagnostics, timers) run Python through `python3 -c '...'` and `python3 - <<'PY' ... PY`. No other test sees that code
(tests/test_scripts_import_safe.py and tests/test_network_boundary.py parse `.py` files only), so a syntax error breaks
a timer or an owner's admin script, and a direct `httpx` / `urllib.request` call goes past `core.http.Http` silently.

`extract_blocks(text)` (pure, stdlib, no network, nothing is run) finds in the text of one `.sh` file:
- `python* [-flags] -c '...'` / `-c "..."` / `-c \\"...\\"` (the last one nested in an outer double-quoted string, as
  in `ssh host "... python3 -c \\"...\\""`); a double-quoted body is unescaped as bash does (`\\"`, `\\\\`, `\\$`,
  a backslash-newline) and every shell expansion (`$X`, `${...}`, `$(...)`, backticks) becomes the name `_SH_`;
- `python* [-flags] [-] <<'TAG'` (also `<<"TAG"`, `<<TAG`, `<<-TAG`, and `python -" <<'EOF'` where the heredoc feeds
  a `bash -c "... python -"`): the body up to the line `TAG` (a closing quote of an outer `ssh '...'` after it is
  allowed); an unquoted tag gets the same expansion treatment.
Matches on comment lines are skipped. Every block must `compile()`; an import of `httpx`, `requests`, `aiohttp`,
`websockets`, `ccxt` or `urllib.request` (`ast`, nested imports included) is a finding, and the files with one are
listed in `ALLOWED` with a reason; the list must match the scan exactly.

Coverage is partial. Not caught (test_uncovered_forms_are_not_extracted pins the first four):
- Python fed by a pipe without a heredoc: `... | python3 -`;
- an interpreter held in a variable: `"$PY" -c '...'`, `$PYTHON - <<EOF`;
- Python inside a heredoc written to a file and run later: `cat >x.py <<EOF ... EOF`;
- `-c` bodies built from several concatenated quoted parts (`-c 'a'"$X"'b'`): only the first part is read;
- network reached through `socket`, `http.client`, a `tradedesk` provider or `__import__` / `importlib`;
- anything that fails only at run time (missing modules, wrong names, shell expansions that are not Python).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_DIRS = ("scripts", "deploy")
PLACEHOLDER = "_SH_"
NET_ROOTS = frozenset({"httpx", "requests", "aiohttp", "websockets", "ccxt"})

_PY = r"(?<![\w-])python(?:3(?:\.\d+)?)?(?![\w.-])"
_FLAGS = r"(?:[ \t]+-[A-Za-z]+)*?"
_DASH_C = re.compile(_PY + _FLAGS + r"[ \t]+-c[ \t]+(?P<q>\\\"|\"|')")
_HEREDOC = re.compile(_PY + r"(?:[ \t]+-[A-Za-z]+)*(?:[ \t]+-)?[\"']?[ \t]*<<-?[ \t]*(?P<q>['\"]?)(?P<tag>\w+)(?P=q)")
_NESTED_CLOSE = re.compile(r'(?<!\\)\\"')
_NAME = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class Block:
    line: int  # 1-based line of the `python` word in the `.sh` file
    kind: str  # "-c" or "heredoc"
    code: str | None  # None: the quote or heredoc is never closed


def _balanced_end(s: str, i: int, open_: str, close: str) -> int | None:
    """Index after the `close` matching the `open_` at s[i]; naive nesting count, no quote awareness."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == open_:
            depth += 1
        elif s[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _expansion_end(s: str, i: int) -> int | None:
    """End index of the shell expansion that starts with the `$` at s[i], or None when the `$` is literal."""
    nxt = s[i + 1 : i + 2]
    if nxt == "(":
        return _balanced_end(s, i + 1, "(", ")")
    if nxt == "{":
        return _balanced_end(s, i + 1, "{", "}")
    m = _NAME.match(s, i + 1)
    if m:
        return m.end()
    if nxt and (nxt.isdigit() or nxt in "@*#?$!-"):
        return i + 2
    return None


def _dq_body(s: str, *, heredoc: bool = False) -> str:
    """What bash makes of `s` inside double quotes (or an unquoted heredoc), with expansions as PLACEHOLDER."""
    out: list[str] = []
    j = 0
    while j < len(s):
        c = s[j]
        if c == "\\" and j + 1 < len(s):
            n = s[j + 1]
            if n == "\n":
                j += 2
                continue
            if n in "$`\\" or (n == '"' and not heredoc):
                out.append(n)
                j += 2
                continue
        elif c == "$":
            end = _expansion_end(s, j)
            if end is not None:
                out.append(PLACEHOLDER)
                j = end
                continue
        elif c == "`":
            k = s.find("`", j + 1)
            if k > 0:
                out.append(PLACEHOLDER)
                j = k + 1
                continue
        out.append(c)
        j += 1
    return "".join(out)


def _dq_end(s: str, i: int) -> int | None:
    """Index of the `"` closing a double-quoted string whose body starts at s[i]."""
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j
        if c == "$" and s[j + 1 : j + 2] in ("(", "{"):
            j = _expansion_end(s, j) or len(s)
            continue
        if c == "`":
            k = s.find("`", j + 1)
            j = k + 1 if k > 0 else len(s)
            continue
        j += 1
    return None


def _on_comment_line(text: str, pos: int) -> bool:
    return text[text.rfind("\n", 0, pos) + 1 : pos].lstrip().startswith("#")


def _dash_c(text: str, m: re.Match[str]) -> str | None:
    start, q = m.end(), m.group("q")
    if q == "'":
        end = text.find("'", start)
        return None if end < 0 else text[start:end]
    if q == '"':
        end = _dq_end(text, start)
        return None if end is None else _dq_body(text[start:end])
    close = _NESTED_CLOSE.search(text, start)  # `\"`: the outer double quotes unescape first, then the inner ones
    return None if close is None else _dq_body(_dq_body(text[start : close.start()]))


def _heredoc(text: str, m: re.Match[str]) -> str | None:
    eol = text.find("\n", m.end())
    if eol < 0:
        return None
    tag = re.escape(m.group("tag"))
    lines = text[eol + 1 :].split("\n")
    for k, line in enumerate(lines):
        if re.fullmatch(rf"\s*{tag}['\")]*\s*", line):
            body = "\n".join(lines[:k]) + "\n"
            return body if m.group("q") else _dq_body(body, heredoc=True)
    return None


def extract_blocks(text: str) -> list[Block]:
    """Pure: the Python blocks of one `.sh` file's text, in order of position."""
    found: list[tuple[int, Block]] = []
    for kind, rx, get in (("-c", _DASH_C, _dash_c), ("heredoc", _HEREDOC, _heredoc)):
        for m in rx.finditer(text):
            if not _on_comment_line(text, m.start()):
                found.append((m.start(), Block(text.count("\n", 0, m.start()) + 1, kind, get(text, m))))
    return [b for _, b in sorted(found, key=lambda x: x[0])]


def network_imports(code: str) -> list[str]:
    """Pure: network modules imported anywhere in `code` (sorted, unique); `code` must compile."""
    mods: set[str] = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            if name.split(".")[0] in NET_ROOTS:
                mods.add(name.split(".")[0])
            elif name == "urllib.request" or name.startswith("urllib.request."):
                mods.add("urllib.request")
    return sorted(mods)


def check_text(text: str) -> tuple[int, list[str], list[str]]:
    """Pure: (number of blocks, errors, network findings) for one `.sh` file's text."""
    blocks = extract_blocks(text)
    errors: list[str] = []
    net: list[str] = []
    for b in blocks:
        if b.code is None:
            errors.append(f"line {b.line}: unterminated python {b.kind} block")
            continue
        try:
            compile(b.code, f"<sh line {b.line}>", "exec")
        except SyntaxError as e:
            errors.append(f"line {b.line}: syntax error in python {b.kind} block: {e.msg} (block line {e.lineno})")
            continue
        net += [f"line {b.line}: imports {mod}" for mod in network_imports(b.code)]
    return len(blocks), errors, net


@dataclass
class Scan:
    files: int = 0  # .sh files read
    with_python: int = 0  # files with at least one block
    blocks: int = 0
    errors: dict[str, list[str]] = field(default_factory=dict)
    network: dict[str, list[str]] = field(default_factory=dict)


def scan(root: Path = ROOT, dirs: tuple[str, ...] = SHELL_DIRS) -> Scan:
    """Every `<dir>/**/*.sh` under `root`; paths in the result are relative to `root`."""
    out = Scan()
    for d in dirs:
        for path in sorted((root / d).rglob("*.sh")):
            rel = path.relative_to(root).as_posix()
            n, errors, net = check_text(path.read_text(encoding="utf-8"))
            out.files += 1
            out.blocks += n
            out.with_python += n > 0
            if errors:
                out.errors[rel] = errors
            if net:
                out.network[rel] = net
    return out


def check(found: dict[str, list[str]], allowed: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """Pure: (files with network imports that are not allowed, allowed files with nothing left to allow)."""
    return {p: r for p, r in found.items() if p not in allowed}, sorted(set(allowed) - set(found))


# path relative to the repository -> reason. Scan of 07.11 (docs/changes/2026-11-07-shell-embedded-python.md): all six
# are diagnostics or key checks the owner runs by hand from the Mac; none is a timer, a service or a collector step. The
# live timers and server steps with Python (backup.sh, 16-collector-liquidations.sh, 19-assistant-snapshot.sh,
# 09-alerts-telegram.sh) import only json / sqlite / tradedesk.
ALLOWED: dict[str, str] = {
    "scripts/server/ci-check.sh": (
        "owner diagnostic: GitHub Actions runs and open PRs through urllib.request with the server's GitHub token; "
        "GitHub, not a venue, so no Evidence; prints results only"
    ),
    "scripts/server/coinalyze-check.sh": (
        "owner key check: one httpx.get to Coinalyze /v1/exchanges with the key from the service env; prints the HTTP "
        "code only, the MCP itself goes through core.http.Http"
    ),
    "scripts/server/fx-check.sh": (
        "owner diagnostic: one BingX EURUSD candle through ccxt.async_support to see whether the server reaches BingX; nothing is stored"
    ),
    "scripts/server/liq-probe.sh": (
        "owner diagnostic: OKX instruments through urllib.request for the contract sizes, then the provider's liquidation "
        "streams for 45 s; prints counts only"
    ),
    "scripts/server/onchain-keys.sh": (
        "owner key setup in the Terminal: after storing the Etherscan / Dune keys, one urllib.request check per key; "
        "prints ok / HTTP code, never the key"
    ),
    "scripts/server/thegraph-key.sh": (
        "owner key setup in the Terminal: after storing the The Graph key, one subgraph query through urllib.request; "
        "prints ok / the block number, never the key"
    ),
}


# --- extraction and checks on fixtures -----------------------------------------------------------------------------------

NORMAL = """#!/bin/bash
# python3 -c 'this is a comment, not code(' is skipped
n="$(restic snapshots --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
runuser -u tradedesk -- python3 -I -c "
import json
d = json.load(open('$FILE'))
print(\\"rows\\", d[\\"n\\"], \\"\\\\n\\", r\\"^KEY=(.+)$\\")"
ssh host 'python3 -I - <<"PY"
import sqlite3
print(sqlite3.sqlite_version)
PY'
bash -c "cd $REPO && .venv/bin/python -" <<EOF
print("$HOME", ${N:-3} + 1, \\$PATH)
EOF
uv run --python 3.12 python scripts/x.py --flag
"""


def test_blocks_of_every_form_are_extracted_and_unquoted():
    blocks = extract_blocks(NORMAL)
    assert [(b.line, b.kind) for b in blocks] == [(3, "-c"), (4, "-c"), (8, "heredoc"), (12, "heredoc")]
    assert blocks[0].code == "import json,sys; print(len(json.load(sys.stdin)))"
    # double quotes: \" -> ", \\ -> \, `$FILE` -> _SH_, a `$` before `"` stays literal
    assert blocks[1].code == '\nimport json\nd = json.load(open(\'_SH_\'))\nprint("rows", d["n"], "\\n", r"^KEY=(.+)$")'
    assert blocks[2].code == "import sqlite3\nprint(sqlite3.sqlite_version)\n"  # quoted tag: literal; `PY'` closes it
    assert blocks[3].code == 'print("_SH_", _SH_ + 1, $PATH)\n'  # unquoted tag: expansions replaced, \$ kept literal
    n, errors, net = check_text(NORMAL)
    assert n == 4 and net == []
    # the `$` left by `\$PATH` is not Python (message text differs between Python versions: compare the prefix)
    assert len(errors) == 1 and errors[0].startswith("line 12: syntax error in python heredoc block: ")


def test_clean_script_has_no_findings():
    assert check_text(NORMAL.replace(", \\$PATH", "")) == (4, [], [])
    assert check_text("#!/bin/bash\necho hi\npython3 scripts/x.py\n") == (0, [], [])


def test_nested_double_quotes_are_unescaped_twice():
    text = 'ssh host "journalctl | grep \'$pat\'\npython3 -I -c \\"\nimport time\nprint(\'x\', \\\\\\"y\\\\\\", time.time())\n\\""\n'
    (block,) = extract_blocks(text)
    assert block.line == 2 and block.code == "\nimport time\nprint('x', \"y\", time.time())\n"
    assert check_text(text) == (1, [], [])


def test_bare_dash_heredoc_with_unquoted_tag_is_extracted():
    text = 'set -e\npython3 - <<EOF\nimport json\nprint(json.dumps({"home": "$HOME"}))\nEOF\necho done\n'
    (block,) = extract_blocks(text)
    assert (block.line, block.kind) == (2, "heredoc")
    assert block.code == 'import json\nprint(json.dumps({"home": "_SH_"}))\n'
    assert check_text(text) == (1, [], [])
    assert check_text(text.replace("import json", "import json, httpx"))[2] == ["line 2: imports httpx"]


def test_uncovered_forms_are_not_extracted():
    # Known gaps listed in the module docstring; if the extractor learns one, move it out of that list.
    assert extract_blocks("curl -s u | python3 -\n") == []
    assert extract_blocks("\"$PY\" -c 'import httpx'\n$PYTHON - <<EOF\nimport httpx\nEOF\n") == []
    assert extract_blocks("cat >/tmp/x.py <<EOF\nimport httpx\nEOF\n") == []
    (block,) = extract_blocks("python3 -c 'import json'\"$X\"'; import httpx'\n")
    assert block.code == "import json"


def test_syntax_error_and_unterminated_blocks_are_errors():
    (error,) = check_text("python3 -c 'print(1'\n")[1]
    assert error.startswith("line 1: syntax error in python -c block: ")
    assert check_text("python3 -c 'print(1)\n")[1] == ["line 1: unterminated python -c block"]
    assert check_text("python3 - <<'PY'\nprint(1)\n")[1] == ["line 1: unterminated python heredoc block"]


def test_network_imports_are_found():
    text = (
        "python3 -c 'import httpx; httpx.get(\"u\")'\n"
        "python3 - <<'PY'\nimport json, urllib.request\ndef f():\n    import ccxt.async_support as c\nPY\n"
        "python3 -c 'from urllib import request; from aiohttp import ClientSession; import urllib.parse'\n"
    )
    n, errors, net = check_text(text)
    assert (n, errors) == (3, [])
    assert net == [
        "line 1: imports httpx",
        "line 2: imports ccxt",
        "line 2: imports urllib.request",
        "line 7: imports aiohttp",
        "line 7: imports urllib.request",
    ]
    assert network_imports("import requests.adapters\nfrom websockets.client import connect\nimport urllib.parse\n") == [
        "requests",
        "websockets",
    ]


def test_allowed_file_is_not_a_failure_and_a_stale_entry_is():
    found = {"scripts/a.sh": ["line 1: imports httpx"], "scripts/b.sh": ["line 2: imports ccxt"]}
    assert check(found, {"scripts/a.sh": "reason"}) == ({"scripts/b.sh": ["line 2: imports ccxt"]}, [])
    assert check({}, {"scripts/gone.sh": "reason"}) == ({}, ["scripts/gone.sh"])


def test_scan_reads_sh_files_in_subdirectories(tmp_path):
    (tmp_path / "scripts" / "server").mkdir(parents=True)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "scripts" / "ok.sh").write_text("python3 -c 'print(1)'\n", encoding="utf-8")
    (tmp_path / "scripts" / "server" / "net.sh").write_text("python3 -c 'import httpx'\n", encoding="utf-8")
    (tmp_path / "deploy" / "bad.sh").write_text('python3 -c "print(\\"x\\""\n', encoding="utf-8")
    (tmp_path / "scripts" / "x.py").write_text("python3 -c 'import httpx'\n", encoding="utf-8")  # not a .sh
    s = scan(tmp_path)
    assert (s.files, s.with_python, s.blocks) == (3, 3, 3)
    assert s.network == {"scripts/server/net.sh": ["line 1: imports httpx"]}
    assert list(s.errors) == ["deploy/bad.sh"]


# --- the repository --------------------------------------------------------------------------------------------------------


# Scan of 07.11: lower bounds equal to the counts then, so an extractor that silently finds fewer blocks fails here.
# Lower them only together with the removal of a `.sh` file or of a Python block from one.
MIN_FILES, MIN_WITH_PYTHON, MIN_BLOCKS = 101, 17, 19


def test_shell_embedded_python_compiles_and_network_is_allowed():
    s = scan()
    assert s.files >= MIN_FILES and s.with_python >= MIN_WITH_PYTHON and s.blocks >= MIN_BLOCKS, (
        f"scanned {s.files} .sh files, {s.with_python} with Python, {s.blocks} blocks; expected at least "
        f"{MIN_FILES} / {MIN_WITH_PYTHON} / {MIN_BLOCKS} (07.11). Fewer: the extractor or SHELL_DIRS broke, or a "
        "script / block was removed on purpose (then lower the MIN_* numbers in the same change)"
    )
    assert not s.errors, f"Python in .sh that does not compile: {s.errors}"
    unexpected, stale = check(s.network, ALLOWED)
    assert not unexpected, f"Python in .sh with network imports (go through core.http.Http or add to ALLOWED): {unexpected}"
    assert not stale, f"ALLOWED entries with nothing left to allow: {stale}"


def test_allowed_entries_have_reasons_and_files():
    for rel, reason in ALLOWED.items():
        assert (ROOT / rel).is_file(), rel
        assert len(reason.strip()) > 20, rel
