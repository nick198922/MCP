"""Posts of a Telegram Desktop channel export (HTML) as markdown and JSON: date, text, replies, media with their paths.

    uv run --no-project --with beautifulsoup4 python -I scripts/tg_export.py EXPORT_DIR --out DIR

Reads EXPORT_DIR/messages.html, messages2.html, … in order and writes DIR/posts.md (one section per post, oldest
first, media as paths relative to EXPORT_DIR, a video's transcript linked when DIR/videos/<stem>/ has one) and
DIR/posts.json (the same as data). Media the export left out are marked missing. Post text is untrusted data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def nfc(s: str) -> str:
    """macOS keeps file names decomposed (NFD: «й» = «и» + breve) while the export's HTML has them composed (NFC)."""
    return unicodedata.normalize("NFC", s)


def present_files(export: Path) -> set[str]:
    """Paths of the export's files relative to it, NFC."""
    return {nfc(p.relative_to(export).as_posix()) for p in export.rglob("*") if p.is_file()}


def html_files(export: Path) -> list[Path]:
    def number(p: Path) -> int:
        m = re.fullmatch(r"messages(\d*)\.html", p.name)
        return int(m.group(1) or 1) if m else 0

    return sorted((p for p in export.glob("messages*.html") if number(p)), key=number)


def text_of(node) -> str:
    """Plain text of a message's text div: <br> as newlines, links keep their target when it differs from the text."""
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for a in node.find_all("a"):
        href = a.get("href") or ""
        label = a.get_text()
        if href.startswith("http") and href.rstrip("/") not in label:
            a.replace_with(f"{label} ({href})")
    lines = [" ".join(line.split()) for line in node.get_text().split("\n")]
    return "\n".join(lines).strip()


def iso(title: str) -> str:
    """ISO time from the export's ``22.01.2021 14:29:43 UTC+02:00`` date title (unchanged if it does not match)."""
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2}) UTC([+-]\d{2}):(\d{2})", title.strip())
    if not m:
        return title.strip()
    d, mo, y, h, mi, s, tzh, tzm = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}{tzh}:{tzm}"


def media_of(body, present: set[str]) -> list[dict]:
    out: list[dict] = []
    for wrap in body.find_all("div", class_="media_wrap", recursive=True):
        for a in wrap.find_all("a"):
            classes = a.get("class") or []
            href = a.get("href") or ""
            if "photo_wrap" in classes:
                kind = "photo"
            elif "video_file_wrap" in classes:
                kind = "video"
            elif "media_file" in classes:
                kind = "file"
            elif "media_photo" in classes:
                kind = "photo"
            else:
                continue
            item: dict = {"kind": kind, "path": nfc(href) or None}
            title = a.find("div", class_="title")
            if title and not href:
                item["title"] = title.get_text(strip=True)
            dur = a.find("div", class_="video_duration")
            if dur:
                item["duration"] = dur.get_text(strip=True)
            item["present"] = bool(href) and nfc(href) in present
            out.append(item)
        if wrap.find("div", class_="media_poll"):
            out.append({"kind": "poll", "path": None, "present": True, "title": " ".join(wrap.get_text(" ").split())[:300]})
    return out


def parse(export: Path) -> list[dict]:
    from bs4 import BeautifulSoup  # noqa: PLC0415

    posts: list[dict] = []
    present = present_files(export)
    for f in html_files(export):
        soup = BeautifulSoup(f.read_text(encoding="utf-8"), "html.parser")
        for msg in soup.find_all("div", class_="message"):
            classes = msg.get("class") or []
            if "default" not in classes:
                continue
            body = msg.find("div", class_="body", recursive=False)
            if body is None:
                continue
            date = body.find("div", class_="date")
            reply = body.find("div", class_="reply_to")
            reply_id = None
            if reply and reply.find("a"):
                m = re.search(r"message(-?\d+)", reply.find("a").get("href") or "")
                reply_id = int(m.group(1)) if m else None
            fwd = body.find("div", class_="forwarded")
            text_div = (fwd or body).find("div", class_="text", recursive=False)
            posts.append(
                {
                    "id": int(msg["id"].removeprefix("message")),
                    "date": iso(date["title"]) if date and date.get("title") else None,
                    "reply_to": reply_id,
                    "forwarded": fwd is not None,
                    "text": text_of(text_div),
                    "hashtags": sorted(set(re.findall(r"#(\w+)", text_of(text_div)))),
                    "media": media_of(body, present),
                    "joined": "joined" in classes,
                }
            )
    return posts


def render(posts: list[dict], export: Path, out: Path) -> str:
    first = posts[0]["date"][:10] if posts else "—"
    last = posts[-1]["date"][:10] if posts else "—"
    n_media = sum(len(p["media"]) for p in posts)
    lines = [f"# {export.name}: пости", "", f"Постів: {len(posts)} · {first} … {last} · медіа: {n_media} · шляхи відносно `{export}`", ""]
    for p in posts:
        reply = f" · відповідь на #{p['reply_to']}" if p["reply_to"] else ""
        fwd = " · переслано" if p["forwarded"] else ""
        lines += [f"## #{p['id']} · {(p['date'] or '')[:16].replace('T', ' ')}{reply}{fwd}", ""]
        if p["text"]:
            lines += [p["text"], ""]
        for m in p["media"]:
            mark = "" if m["present"] else " (нема в експорті)"
            extra = f" {m['duration']}" if m.get("duration") else ""
            what = m["path"] or m.get("title") or ""
            line = f"- {m['kind']}: `{what}`{extra}{mark}"
            if m["kind"] == "video" and m["path"]:
                stem = Path(m["path"]).stem
                if (out / "videos" / stem / "transcript.md").exists():
                    line += f" · [транскрипт](videos/{stem}/transcript.md) · [кадри](videos/{stem}/frames/index.md)"
            lines.append(line)
        if p["media"]:
            lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if not html_files(a.export):
        print(f"{a.export}: no messages*.html (a JSON-only export is not supported yet)", file=sys.stderr)
        return 1
    posts = parse(a.export)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "posts.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / "posts.md").write_text(render(posts, a.export, a.out), encoding="utf-8")
    kinds: dict[str, int] = {}
    for p in posts:
        for m in p["media"]:
            kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    missing = sum(1 for p in posts for m in p["media"] if not m["present"])
    print(f"{len(posts)} posts, media {kinds}, missing {missing} -> {a.out}/posts.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
