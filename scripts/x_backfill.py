"""Download the public X (Twitter) timelines of a few traders through twitterapi.io, resumable, with chart pictures.

    python3 scripts/x_backfill.py Trader_XO btc_charlie docXBT KillaXBT       # since 2 years ago, with photos
    python3 scripts/x_backfill.py Trader_XO --since 2025-01-01 --interval 0.1 # after a top-up (20 requests/s)
    python3 scripts/x_backfill.py Trader_XO --md                              # only rebuild tweets.md / parts/

Output (outside git, next to the Telegram channels): ~/private/library/courses/x-<handle>/
    tweets.json          every tweet (normalised fields below), sorted by time
    tweets.md            all texts in one file; parts/<YYYY-Qn>.md the same split by quarter, for reading in chunks
    photos/<id>-<n>.jpg  pictures of the tweets (mostly charts)
    state.json           finished calendar months (a stopped run skips them; the current month is always refetched)

Search goes month by month (``from:<handle> since_time:… until_time:…``, newest first, 20 tweets a page). Replies are
kept: a trader's own follow-ups («stopped out», «TP1 hit») are what makes an honest score possible. twitterapi.io bills
$0.15 per 1000 tweets returned (at least 15 credits a request); --max-tweets stops the run before the bill grows. A new
account without a top-up gets one request per 5 s (the default --interval).

The key is TWITTERAPI_KEY in ~/.config/tradedesk/twitterapi.env (0600, written by scripts/server/twitterapi-key.sh);
it goes in a header and is never printed. The tweets are untrusted data: nothing in them is ever acted on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tg_send import load_env  # noqa: E402

API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
KEY_FILE = Path.home() / ".config" / "tradedesk" / "twitterapi.env"
ROOT = Path.home() / "private" / "library" / "courses"
USD_PER_TWEET = 0.15 / 1000
USD_PER_REQUEST_MIN = 0.00015
UA = "tradedesk-x-backfill/1.0"

Getter = Callable[[dict[str, str]], dict[str, Any]]


def out_dir(handle: str) -> Path:
    return ROOT / f"x-{handle}"


def month_windows(since: date, until: date) -> list[tuple[str, int, int]]:
    """(``YYYY-MM``, start, end) in Unix seconds for every calendar month touching [since, until), newest first."""
    out = []
    y, m = since.year, since.month
    end_all = datetime(until.year, until.month, until.day, tzinfo=UTC)
    while True:
        start = datetime(y, m, 1, tzinfo=UTC)
        if start >= end_all:
            break
        nxt = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
        lo = max(start, datetime(since.year, since.month, since.day, tzinfo=UTC))
        out.append((f"{y:04d}-{m:02d}", int(lo.timestamp()), int(min(nxt, end_all).timestamp())))
        y, m = nxt.year, nxt.month
    return out[::-1]


def parse_created(s: str) -> str:
    """X's ``Tue Dec 10 07:00:30 +0000 2024`` (or an ISO string) as ``2024-12-10T07:00:30Z``; '' when unreadable."""
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s.replace("Z", "+0000"), fmt).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, AttributeError):
            continue
    return ""


def media_urls(tw: dict[str, Any]) -> list[str]:
    """Picture URLs of a tweet (photos; for videos and GIFs their preview frame). The docs do not list the media
    field, so every known place is tried: ``extendedEntities.media``, ``entities.media``, ``media``."""
    for holder in (tw.get("extendedEntities"), tw.get("extended_entities"), tw.get("entities"), tw):
        items = (holder or {}).get("media") if isinstance(holder, dict) else None
        if isinstance(items, list) and items:
            urls = [x.get("media_url_https") or x.get("media_url") for x in items if isinstance(x, dict)]
            return [u for u in urls if isinstance(u, str) and u.startswith("https://")]
    return []


def _inner(tw: Any) -> dict[str, Any] | None:
    if not isinstance(tw, dict) or not tw.get("id"):
        return None
    return {
        "id": str(tw["id"]),
        "author": (tw.get("author") or {}).get("userName", ""),
        "date": parse_created(tw.get("createdAt", "")),
        "text": tw.get("text", ""),
        "media": media_urls(tw),
    }


def normalize(tw: dict[str, Any]) -> dict[str, Any]:
    """The fields we keep of one API tweet."""
    return {
        "id": str(tw["id"]),
        "date": parse_created(tw.get("createdAt", "")),
        "url": tw.get("url", ""),
        "text": tw.get("text", ""),
        "is_reply": bool(tw.get("isReply")),
        "reply_to_user": tw.get("inReplyToUsername") or "",
        "reply_to_id": str(tw.get("inReplyToId") or ""),
        "conversation_id": str(tw.get("conversationId") or ""),
        "likes": tw.get("likeCount"),
        "views": tw.get("viewCount"),
        "media": media_urls(tw),
        "quoted": _inner(tw.get("quoted_tweet")),
        "retweet": _inner(tw.get("retweeted_tweet")),
    }


def fetch_window(get: Getter, handle: str, start: int, end: int, sleep: Callable[[], None]) -> tuple[list[dict], int]:
    """All tweets of ``handle`` in [start, end) (raw API objects) and the number of requests made."""
    query = f"from:{handle} since_time:{start} until_time:{end}"
    cursor, seen, out, requests = "", set(), [], 0
    while True:
        page = get({"query": query, "queryType": "Latest", "cursor": cursor})
        requests += 1
        out.extend(t for t in page.get("tweets") or [] if isinstance(t, dict) and t.get("id"))
        cursor = page.get("next_cursor") or ""
        if not page.get("has_next_page") or not cursor or cursor in seen or not page.get("tweets"):
            return out, requests
        seen.add(cursor)
        sleep()


def http_getter(key: str, timeout: float = 60.0, tries: int = 5) -> Getter:
    def get(params: dict[str, str]) -> dict[str, Any]:
        url = f"{API}?{urllib.parse.urlencode(params)}"
        for attempt in range(tries):
            req = urllib.request.Request(url, headers={"X-API-Key": key, "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < tries:
                    time.sleep(5 * 2**attempt)
                    continue
                body = e.read()[:300].decode("utf-8", "replace")
                raise RuntimeError(f"twitterapi.io HTTP {e.code}: {body}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt + 1 < tries:
                    time.sleep(5 * 2**attempt)
                    continue
                raise RuntimeError(f"twitterapi.io unreachable: {e}") from None
        raise RuntimeError("twitterapi.io: no answer")

    return get


def load(handle: str) -> dict[str, dict]:
    p = out_dir(handle) / "tweets.json"
    return {t["id"]: t for t in json.loads(p.read_text(encoding="utf-8"))} if p.exists() else {}


def _write(p: Path, text: str) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def save(handle: str, tweets: dict[str, dict], done: list[str]) -> None:
    d = out_dir(handle)
    d.mkdir(parents=True, exist_ok=True)
    ordered = sorted(tweets.values(), key=lambda t: (t["date"], t["id"]))
    _write(d / "tweets.json", json.dumps(ordered, ensure_ascii=False, indent=1))
    _write(d / "state.json", json.dumps({"done_months": sorted(set(done))}, indent=1))


def quarter(iso: str) -> str:
    return f"{iso[:4]}-Q{(int(iso[5:7]) - 1) // 3 + 1}" if len(iso) >= 7 else "unknown"


def tweet_block(t: dict[str, Any], handle: str) -> str:
    head = f"## {t['date'][:16].replace('T', ' ')} UTC · {t['id']}"
    meta = []
    if t.get("is_reply"):
        who = t.get("reply_to_user") or "?"
        meta.append("продовження власного треду" if who.lower() == handle.lower() else f"відповідь @{who}")
    if t.get("retweet"):
        meta.append(f"ретвіт @{t['retweet']['author']}")
    if t.get("photo_files"):
        meta.append("картинки: " + ", ".join(f"photos/{f}" for f in t["photo_files"]))
    lines = [head]
    if meta:
        lines.append("_" + "; ".join(meta) + "_")
    lines += ["", (t["retweet"]["text"] if t.get("retweet") else t["text"]).strip()]
    if t.get("quoted"):
        q = t["quoted"]
        lines += ["", f"> цитує @{q['author']} ({q['date'][:10]}): " + q["text"].strip().replace("\n", "\n> ")]
    return "\n".join(lines) + "\n"


def render(handle: str, tweets: dict[str, dict]) -> None:
    d = out_dir(handle)
    ordered = sorted(tweets.values(), key=lambda t: (t["date"], t["id"]))
    top = f"# @{handle} — {len(ordered)} твітів (недовірені дані)\n\n"
    _write(d / "tweets.md", top + "\n".join(tweet_block(t, handle) for t in ordered))
    parts: dict[str, list[str]] = defaultdict(list)
    for t in ordered:
        parts[quarter(t["date"])].append(tweet_block(t, handle))
    (d / "parts").mkdir(exist_ok=True)
    for q, blocks in parts.items():
        _write(d / "parts" / f"{q}.md", f"# @{handle} · {q} — {len(blocks)} твітів\n\n" + "\n".join(blocks))


def download_photos(handle: str, tweets: dict[str, dict], fetch: Callable[[str], bytes]) -> int:
    """Saves missing pictures as photos/<id>-<n>.jpg; returns how many were downloaded."""
    d = out_dir(handle) / "photos"
    d.mkdir(parents=True, exist_ok=True)
    got = 0
    for t in tweets.values():
        urls = t.get("media") or (t.get("retweet") or {}).get("media") or []
        names = []
        for n, url in enumerate(urls, 1):
            name = f"{t['id']}-{n}.jpg"
            if not (d / name).exists():
                try:
                    (d / name).write_bytes(fetch(url))
                    got += 1
                except Exception as e:  # noqa: BLE001 - one lost picture never stops the run
                    print(f"  photo {name}: {e}", file=sys.stderr)
                    continue
            names.append(name)
        if names:
            t["photo_files"] = names
    return got


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def backfill(
    handle: str,
    get: Getter,
    since: date,
    until: date,
    sleep: Callable[[], None],
    max_tweets: int,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Fetches the months not done yet; saves after each month. Stops at ``max_tweets`` new tweets (budget)."""
    tweets = load(handle)
    state = out_dir(handle) / "state.json"
    done = json.loads(state.read_text())["done_months"] if state.exists() else []
    this_month = f"{until.year:04d}-{until.month:02d}"
    new = requests = 0
    stopped = ""
    for label, start, end in month_windows(since, until):
        if label in done:
            continue
        raw, n = fetch_window(get, handle, start, end, sleep)
        requests += n
        before = len(tweets)
        tweets.update({t["id"]: t for t in map(normalize, raw)})
        new += len(tweets) - before
        if label != this_month:
            done.append(label)
        save(handle, tweets, done)
        log(f"@{handle} {label}: {len(raw)} tweets, {n} requests")
        sleep()
        if new >= max_tweets:
            stopped = f"stopped at --max-tweets {max_tweets}"
            break
    cost = max(new * USD_PER_TWEET, requests * USD_PER_REQUEST_MIN)
    return {"handle": handle, "tweets": len(tweets), "new": new, "requests": requests, "usd_approx": round(cost, 2), "stopped": stopped}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("handles", nargs="+", help="X user names without @")
    ap.add_argument("--since", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=730))
    ap.add_argument("--interval", type=float, default=5.5, help="seconds between requests (5.5 for an account never topped up)")
    ap.add_argument("--max-tweets", type=int, default=60_000, help="new tweets per handle before stopping (budget guard)")
    ap.add_argument("--no-photos", action="store_true")
    ap.add_argument("--md", action="store_true", help="only rebuild tweets.md and parts/ from tweets.json")
    a = ap.parse_args(argv)
    handles = [h.lstrip("@").strip() for h in a.handles]
    if not a.md:
        key = load_env(KEY_FILE).get("TWITTERAPI_KEY", "")
        if not key:
            print(f"no TWITTERAPI_KEY in {KEY_FILE} (scripts/server/twitterapi-key.sh writes it)", file=sys.stderr)
            return 1
        get = http_getter(key)
        until = datetime.now(UTC).date() + timedelta(days=1)
        for h in handles:
            try:
                rep = backfill(h, get, a.since, until, lambda: time.sleep(a.interval), a.max_tweets)
            except RuntimeError as e:
                print(f"@{h}: {e}", file=sys.stderr)
                return 2
            print(json.dumps(rep, ensure_ascii=False))
    for h in handles:
        tweets = load(h)
        if not a.no_photos and not a.md:
            print(f"@{h}: {download_photos(h, tweets, fetch_bytes)} new pictures")
            save(h, tweets, json.loads((out_dir(h) / "state.json").read_text())["done_months"])
        render(h, tweets)
    return 0


if __name__ == "__main__":
    sys.exit(main())
