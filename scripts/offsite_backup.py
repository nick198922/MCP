"""Off-server backup into the owner's Telegram group, encrypted with age (docs/AGENTS.md → «Бекап поза сервером»).

Two archives, each tar + zstd, encrypted to the owner's age public key (~/.config/tradedesk/backup.pub; the secret
key is only in the owner's password manager, never on the server) and sent in parts of at most 45 MB to the topic
"💾 Бекапи" (created on first use, its id kept in ~/.config/tradedesk/agents.env as TELEGRAM_TOPIC_BACKUPS):
  - core, every night: the latest SQLite backup (/var/lib/tradedesk-mcp/backups), Claude's memory
    (~/.claude/projects/*/memory) and the agents' state (~/.local/state/tradedesk-agents);
  - private, only when its file list (path, size, mtime) changed: ~/private (the paid courses and the like).
Run by the user timer deploy/user-units/tradedesk-offsite-backup.timer; no Claude tokens. Restore: see the docs.

    python3 scripts/offsite_backup.py              # send what is due
    python3 scripts/offsite_backup.py --dry-run    # build and encrypt, print the parts, send nothing
    python3 scripts/offsite_backup.py --force      # send the private archive even if unchanged
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import load_env, send, write_env  # noqa: E402

HOME = Path.home()
PUBKEY = HOME / ".config" / "tradedesk" / "backup.pub"
STATE = HOME / ".local" / "state" / "tradedesk-backup" / "state.json"
DB_BACKUPS = Path("/var/lib/tradedesk-mcp/backups")
PRIVATE = HOME / "private"
AGE = HOME / ".local" / "bin" / "age"
PART = 45 * 1024 * 1024  # bots may upload up to 50 MB
TOPIC_NAME = "💾 Бекапи"


def core_paths() -> list[Path]:
    dbs = sorted(DB_BACKUPS.glob("tradedesk-*.sqlite.gz"))
    paths = dbs[-1:] + sorted((HOME / ".claude" / "projects").glob("*/memory"))
    paths.append(HOME / ".local" / "state" / "tradedesk-agents")
    return [p for p in paths if p.exists()]


def fingerprint(root: Path) -> str:
    """Hash of the file list (relative path, size, mtime): changes when anything under root is added or edited."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            h.update(f"{p.relative_to(root)}\0{st.st_size}\0{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def build(name: str, paths: list[Path], recipient: str, workdir: Path) -> list[Path]:
    """tar | zstd | age -r, split into parts; returns the part files."""
    out = workdir / f"{name}.tar.zst.age"
    rel = [str(p.relative_to("/")) for p in paths]
    with open(out, "wb") as f:
        tar = subprocess.Popen(
            ["tar", "-C", "/", "-cf", "-", "--ignore-failed-read", *rel], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        zst = subprocess.Popen(["zstd", "-q", "-T0", "-10", "-c"], stdin=tar.stdout, stdout=subprocess.PIPE)
        age = subprocess.Popen([str(AGE), "-r", recipient], stdin=zst.stdout, stdout=f)
        assert tar.stdout and zst.stdout
        tar.stdout.close()
        zst.stdout.close()
        if age.wait() or zst.wait() or tar.wait() not in (0, 1):  # tar 1: a file changed while read
            raise RuntimeError(f"{name}: archiving failed")
    parts: list[Path] = []
    with open(out, "rb") as f:
        for i, chunk in enumerate(iter(lambda: f.read(PART), b"")):
            part = workdir / f"{out.name}.{i:03d}"
            part.write_bytes(chunk)
            parts.append(part)
    out.unlink()
    return parts


def api(token: str, method: str, fields: dict, file: Path | None = None) -> dict:
    """A Bot API call; with a file it is sent as multipart document. The URL (with the token) is never printed."""
    if file is None:
        data, ctype = json.dumps(fields).encode(), "application/json"
    else:
        b = uuid.uuid4().hex
        chunks = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
        chunks.append(
            f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{file.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        data = b"".join(chunks) + file.read_bytes() + f"\r\n--{b}--\r\n".encode()
        ctype = f"multipart/form-data; boundary={b}"
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, headers={"Content-Type": ctype})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except (ValueError, OSError):
                body = {"description": f"HTTP {e.code}"}
            wait = (body.get("parameters") or {}).get("retry_after")
            if not wait or attempt == 2:
                return {"ok": False, "description": body.get("description", f"HTTP {e.code}")}
            time.sleep(int(wait) + 1)
        except (urllib.error.URLError, OSError) as e:
            if attempt == 2:
                return {"ok": False, "description": type(e).__name__}
            time.sleep(10)
    return {"ok": False, "description": "unreachable"}


def backups_topic(env: dict[str, str]) -> int | None:
    value = env.get("TELEGRAM_TOPIC_BACKUPS", "")
    if value.isdigit():
        return int(value)
    r = api(env["TELEGRAM_BOT_TOKEN"], "createForumTopic", {"chat_id": env["TELEGRAM_CHAT_ID"], "name": TOPIC_NAME, "icon_color": 0x6FB9F0})
    if not r.get("ok"):
        print(f"cannot create the topic ({r.get('description')}); sending to the general chat", file=sys.stderr)
        return None
    thread = int(r["result"]["message_thread_id"])
    write_env({**env, "TELEGRAM_TOPIC_BACKUPS": str(thread)})
    return thread


def upload(env: dict[str, str], thread: int | None, name: str, parts: list[Path]) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    for i, part in enumerate(parts, 1):
        digest = hashlib.sha256(part.read_bytes()).hexdigest()[:16]
        fields = {
            "chat_id": env["TELEGRAM_CHAT_ID"],
            "caption": f"{name} {stamp} · частина {i}/{len(parts)} · sha256 {digest}",
            "disable_notification": "true",
        }
        if thread:
            fields["message_thread_id"] = str(thread)
        r = api(env["TELEGRAM_BOT_TOKEN"], "sendDocument", fields, part)
        if not r.get("ok"):
            raise RuntimeError(f"{name} part {i}: {r.get('description')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="send the private archive even if unchanged")
    args = ap.parse_args(argv)
    if not PUBKEY.is_file() or not AGE.is_file():
        print(f"not set up: need {PUBKEY} and {AGE} (scripts/backup_key.sh)", file=sys.stderr)
        return 1
    recipient = PUBKEY.read_text().strip()
    state = json.loads(STATE.read_text()) if STATE.is_file() else {}
    jobs = [("core", core_paths())]
    private_fp = fingerprint(PRIVATE) if PRIVATE.is_dir() else ""
    if private_fp and (args.force or state.get("private") != private_fp):
        jobs.append(("private", [PRIVATE]))
    env = {} if args.dry_run else load_env()
    if not args.dry_run and not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        print("telegram is not configured", file=sys.stderr)
        return 1
    thread = None if args.dry_run else backups_topic(env)
    sent: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="offsite-") as tmp:
            for name, paths in jobs:
                parts = build(name, paths, recipient, Path(tmp))
                size = sum(p.stat().st_size for p in parts)
                print(f"{name}: {len(parts)} part(s), {size / 1e6:.1f} MB")
                if not args.dry_run:
                    upload(env, thread, name, parts)
                    sent.append(f"{name} {size / 1e6:.0f} МБ ({len(parts)} ч.)")
                for p in parts:
                    p.unlink()
    except (RuntimeError, OSError) as e:
        print(f"backup failed: {e}", file=sys.stderr)
        if not args.dry_run:
            send(f"🛑 Бекап поза сервером не вдався: {e}", topic="status")
        return 1
    if not args.dry_run:
        if private_fp:
            state["private"] = private_fp if any(n == "private" for n, _ in jobs) else state.get("private", "")
        state["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
        print("sent: " + ", ".join(sent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
