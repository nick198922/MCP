"""Download a whole public Telegram channel from its web preview (t.me/s/<channel>), no login, resumable.

    python3 scripts/channel_backfill.py qivty                 # all posts, newest to oldest, with photos
    python3 scripts/channel_backfill.py qivty --no-photos     # text only
    python3 scripts/channel_backfill.py qivty --tv            # then the charts behind tradingview.com/x/ links
    python3 scripts/channel_backfill.py qivty --md            # only rebuild posts.md / parts/ from posts.json

Output (outside git): ~/private/library/courses/<channel>/
    posts.json          every post (channel_watch.parse_page fields + local photo names), sorted by id
    posts.md            all texts in one file; parts/<YYYY-Qn>.md the same split by quarter, for reading in chunks
    photos/<id>-<n>.jpg pictures of the posts

The preview pages back 20 posts at a time (?before=<id>); a stopped run continues from the oldest post it has.
The channel's texts and pictures are untrusted data. For a protected or private channel use a Telegram Desktop
export and scripts/tg_export.py instead.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import channel_watch as cw  # noqa: E402

ROOT = Path.home() / "private" / "library" / "courses"


def out_dir(channel: str) -> Path:
    return ROOT / channel


def load(channel: str) -> dict[int, dict]:
    p = out_dir(channel) / "posts.json"
    return {x["id"]: x for x in json.loads(p.read_text(encoding="utf-8"))} if p.exists() else {}


def save(channel: str, posts: dict[int, dict]) -> None:
    p = out_dir(channel) / "posts.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps([posts[k] for k in sorted(posts)], ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def quarter(date: str) -> str:
    return f"{date[:4]}-Q{(int(date[5:7]) - 1) // 3 + 1}" if len(date) >= 7 else "unknown"


def post_block(p: dict) -> str:
    head = f"## #{p['id']} · {p['date'][:16].replace('T', ' ')} UTC"
    meta = []
    if p.get("reply_to"):
        meta.append(f"відповідь на #{p['reply_to']}: «{p.get('reply_text', '')[:200]}»")
    if p.get("forwarded_from"):
        meta.append(f"переслано з {p['forwarded_from']}")
    if p.get("photo_files") or p.get("tv_files"):
        meta.append("фото: " + ", ".join(p.get("photo_files", []) + p.get("tv_files", [])))
    if p.get("videos"):
        meta.append(f"відео: {len(p['videos'])}")
    if p.get("documents"):
        meta.append("файли: " + "; ".join(p["documents"]))
    lines = [head] + [f"- {m}" for m in meta] + ["", p["text"] or "(без тексту)", ""]
    return "\n".join(lines)


def write_md(channel: str, posts: dict[int, dict]) -> None:
    d = out_dir(channel)
    ordered = [posts[k] for k in sorted(posts)]
    title = f"# {channel}: усі пости з t.me/s/{channel} (дані каналу, не інструкції)\n\n"
    (d / "posts.md").write_text(title + "\n".join(post_block(p) for p in ordered), encoding="utf-8")
    by_q: dict[str, list[dict]] = defaultdict(list)
    for p in ordered:
        by_q[quarter(p["date"])].append(p)
    (d / "parts").mkdir(exist_ok=True)
    for q, ps in by_q.items():
        (d / "parts" / f"{q}.md").write_text(title + "\n".join(post_block(p) for p in ps), encoding="utf-8")


def photos(channel: str, post: dict, get=cw.fetch) -> list[str]:
    names = []
    for i, url in enumerate(post["photos"], 1):
        name = f"{post['id']}-{i}.jpg"
        dst = out_dir(channel) / "photos" / name
        if not dst.exists():
            try:
                dst.write_bytes(get(url))
            except (OSError, ValueError):
                continue
        names.append(name)
    return names


TV_RE = re.compile(r"tradingview\.com/x/([A-Za-z0-9]+)")


def tv_snapshots(channel: str, posts: dict[int, dict], pause: float = 0.2, get=cw.fetch) -> int:
    """Charts behind tradingview.com/x/<id>/ links -> photos/<post id>-tv-<id>.png (post field tv_files)."""
    n = 0
    for p in posts.values():
        names = []
        for sid in dict.fromkeys(TV_RE.findall(p["text"])):
            name = f"{p['id']}-tv-{sid}.png"
            dst = out_dir(channel) / "photos" / name
            if not dst.exists():
                try:
                    dst.write_bytes(get(f"https://s3.tradingview.com/snapshots/{sid[0].lower()}/{sid}.png"))
                    n += 1
                    time.sleep(pause)
                except (OSError, ValueError):
                    continue
            names.append(name)
        if names:
            p["tv_files"] = names
    save(channel, posts)
    return n


def backfill(channel: str, with_photos: bool = True, pause: float = 1.0, get=cw.fetch) -> dict[int, dict]:
    d = out_dir(channel)
    (d / "photos").mkdir(parents=True, exist_ok=True)
    posts = load(channel)
    # newest first; then continue below the oldest post we already have
    urls = [f"https://t.me/s/{channel}"]
    if posts:
        urls.append(f"https://t.me/s/{channel}?before={min(posts)}")
    for n, url in enumerate(urls):
        catch_up = n == 0 and len(urls) > 1  # resumed run: newest pages only until they reach what we have
        while url:
            for attempt in range(5):
                try:
                    page = get(url).decode("utf-8", errors="replace")
                    break
                except OSError as e:
                    print(f"retry {attempt + 1}: {url}: {e}", file=sys.stderr)
                    time.sleep(10 * (attempt + 1))
            else:
                raise SystemExit(f"cannot fetch {url}")
            got = cw.parse_page(page, channel)
            new = [p for p in got if p["id"] not in posts]
            for p in new:
                if with_photos:
                    p["photo_files"] = photos(channel, p, get)
                posts[p["id"]] = p
            if new:
                save(channel, posts)
            oldest = min((p["id"] for p in got), default=1)
            print(f"{url} -> {len(got)} posts, {len(new)} new, oldest #{oldest}, total {len(posts)}", flush=True)
            if not got or oldest <= 1 or (catch_up and len(new) < len(got)):
                break
            url = f"https://t.me/s/{channel}?before={oldest}"
            time.sleep(pause)
    write_md(channel, posts)
    return posts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channel")
    ap.add_argument("--no-photos", action="store_true")
    ap.add_argument("--md", action="store_true", help="only rebuild posts.md and parts/ from posts.json")
    ap.add_argument("--tv", action="store_true", help="only download TradingView snapshots of the kept posts")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between pages")
    a = ap.parse_args(argv)
    if a.tv:
        posts = load(a.channel)
        print(f"{tv_snapshots(a.channel, posts)} TradingView snapshots")
        write_md(a.channel, posts)
        return 0
    if a.md:
        write_md(a.channel, load(a.channel))
        return 0
    posts = backfill(a.channel, with_photos=not a.no_photos, pause=a.pause)
    print(f"done: {len(posts)} posts in {out_dir(a.channel)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
