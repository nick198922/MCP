"""Batch: transcript-driven key frames for the owner's course videos (local only, paid material).

For each transcript (header line with the YouTube URL), pick up to MAX_AT moments where the speaker points at the
chart or a slide, then run scripts/yt_frames.py --at ... into <course>/frames/<slug>/. Skips videos already done.
Usage: python3 -I scripts/course_frames.py ORDER_FILE   (ORDER_FILE: one transcript path per line, most chart-heavy first)
Transcripts come from yt_transcript.py or yt_asr.py (header line with the YouTube URL). Videos up to 30 min get a frame
every ~15 s (repeats dropped), longer ones frames at the moments picked from the transcript. Frames stay next to the
paid course files (gitignored): look at the contact sheets, not only the transcript.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_AT = 30
MIN_GAP = 20  # seconds between chosen moments
STRONG = re.compile(
    r"вот здесь|вот тут|вот так|вот это|смотрите|посмотр|на графике|графическ|отмеча|отмечен|отмечу|слайд|таблиц|"
    r"на скрин|схематич|выделя|обратите внимание|давайте (перейд|рассмотр|посмотр)|видим|можем заметить|ось тут|ось тут",
    re.I,
)
PARA = re.compile(r"^\[(\d+):(\d{2})(?::(\d{2}))?\]\s*(.*)$")
ID_RE = re.compile(r"watch\?v=([A-Za-z0-9_-]{11})")


def moments(text: str) -> list[int]:
    scored = []
    for line in text.splitlines():
        m = PARA.match(line)
        if not m:
            continue
        h_or_m, mm, ss, body = m.groups()
        t = int(h_or_m) * 3600 + int(mm) * 60 + int(ss) if ss else int(h_or_m) * 60 + int(mm)
        score = len(STRONG.findall(body))
        if score:
            scored.append((score, t))
    chosen: list[int] = []
    for _score, t in sorted(scored, key=lambda x: (-x[0], x[1])):
        if all(abs(t - c) >= MIN_GAP for c in chosen):
            chosen.append(t)
        if len(chosen) >= MAX_AT:
            break
    return sorted(chosen)


def main() -> None:
    order = [Path(p.strip()) for p in Path(sys.argv[1]).read_text().splitlines() if p.strip()]
    for tr in order:
        text = tr.read_text(encoding="utf-8", errors="replace")
        m = ID_RE.search(text[:600])
        if not m:
            print(f"{tr.name}: no video id", flush=True)
            continue
        vid = m.group(1)
        course_dir = tr.parent.parent if tr.parent.name == "transcripts" else tr.parent
        out = course_dir / "frames" / tr.stem
        if (out / "index.md").exists():
            print(f"{tr.stem}: done before", flush=True)
            continue
        at = moments(text)
        if out.exists() and not any(out.iterdir()):
            out.rmdir()
        args = [
            "uv",
            "run",
            "--no-project",
            "--with",
            "yt-dlp[default]",
            "--with",
            "imageio-ffmpeg",
            "--with",
            "pillow",
            "python",
            "-I",
            str(REPO / "scripts/yt_frames.py"),
            f"https://www.youtube.com/watch?v={vid}",
            "--out",
            str(out),
            "--transcript",
            str(tr),
        ]
        times = [int(a) * 60 + int(b) for a, b in re.findall(r"^\[(\d+):(\d{2})\]", text, re.M)]
        duration = max(times) if times else 0
        if duration and duration <= 1800:
            every = max(15, duration // 80)
            args += ["--every", str(every), "--max", "80"]
        elif len(at) >= 4:
            args += ["--at", ",".join(f"{t // 60}:{t % 60:02d}" for t in at)]
        else:
            args += ["--every", "30", "--max", "60"]
        for _attempt in range(2):
            r = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
            if r.returncode == 0:
                n = len(list(out.glob("t*.jpg")))
                print(f"{tr.stem}: {n} frames ({len(at)} moments)", flush=True)
                break
            if out.exists() and not any(out.iterdir()):
                out.rmdir()
        else:
            print(f"{tr.stem}: FAILED {r.stderr.strip().splitlines()[-1:] if r.stderr else ''}", flush=True)
    print("ALL-DONE", flush=True)


if __name__ == "__main__":
    main()
