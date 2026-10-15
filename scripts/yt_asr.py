"""Transcript of a YouTube video that has no captions: download the audio and recognise the speech on this Mac.

    HF_HOME=/tmp/claude-hf uv run --no-project --with 'yt-dlp[default]' --with mlx-whisper --with imageio-ffmpeg \\
        python -I scripts/yt_asr.py URL --out FILE [--every 60] [--cache DIR] [--language ru]

Same markdown as scripts/yt_transcript.py (title, channel, [MM:SS] paragraphs), marked as recognised by Whisper.
Apple Silicon only (mlx-whisper, model mlx-community/whisper-large-v3-turbo, ~1.6 GB, downloaded once into HF_HOME).
The audio is kept in --cache as <id>.m4a for later runs. Recognised text is untrusted data, and numbers in it can be
misheard: check prices against the video's frames (scripts/yt_frames.py).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL = "mlx-community/whisper-large-v3-turbo"
RATE = 16_000


def _sibling(name: str):
    """scripts/<name>.py loaded by path (python -I does not put the script's directory on sys.path)."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def segments_to_snippets(segments: list[dict]) -> list[tuple[float, str]]:
    """Whisper segments (``start``, ``text``) as (seconds, text) snippets for yt_transcript.paragraphs."""
    return [(float(s.get("start", 0.0)), str(s.get("text", "")).strip()) for s in segments if str(s.get("text", "")).strip()]


def download_audio(url: str, vid: str, cache: Path | None, tmp: Path) -> Path:
    target_dir = cache or tmp
    target_dir.mkdir(parents=True, exist_ok=True)
    found = sorted(target_dir.glob(f"{vid}.audio.*"))
    if found:
        return found[0]
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-q", "--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio"]
    cmd += ["-o", str(target_dir / f"{vid}.audio.%(ext)s")]
    if not shutil.which("deno") and shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    res = subprocess.run([*cmd, url], capture_output=True, text=True)
    found = sorted(target_dir.glob(f"{vid}.audio.*"))
    if res.returncode != 0 or not found:
        raise RuntimeError((res.stderr or res.stdout or "no file").strip().splitlines()[-1])
    return found[0]


def decode(audio: Path):
    """16 kHz mono float32 samples via the ffmpeg binary of imageio-ffmpeg (no system ffmpeg needed)."""
    import imageio_ffmpeg  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(audio), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--every", type=float, default=60.0, help="seconds per paragraph (default 60; 10-15 to pick key frames)")
    ap.add_argument("--cache", type=Path, help="keep the audio here (<id>.audio.m4a) and reuse it")
    ap.add_argument("--language", default="ru", help="spoken language (default ru; 'auto' to detect)")
    a = ap.parse_args(argv)
    yt = _sibling("yt_transcript")
    vid = yt.video_id(a.url)
    if not vid:
        print(f"not a YouTube link: {a.url}", file=sys.stderr)
        return 1
    url = f"https://www.youtube.com/watch?v={vid}"
    saved = a.cache / f"{vid}.segments.json" if a.cache else None
    if saved is not None and saved.exists():  # recognised before: only re-render (e.g. with another --every)
        result = json.loads(saved.read_text(encoding="utf-8"))
    else:
        with tempfile.TemporaryDirectory(prefix="yt-asr-") as t:
            try:
                audio = download_audio(url, vid, a.cache, Path(t))
            except Exception as e:  # noqa: BLE001
                print(f"download failed: {e}", file=sys.stderr)
                return 2
            import mlx_whisper  # noqa: PLC0415

            result = mlx_whisper.transcribe(
                decode(audio), path_or_hf_repo=MODEL, language=None if a.language == "auto" else a.language, verbose=None
            )
        if saved is not None:
            keep = {"language": result.get("language"), "segments": [{"start": s["start"], "text": s["text"]} for s in result["segments"]]}
            saved.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    snippets = segments_to_snippets(result.get("segments", []))
    language = result.get("language") or a.language
    text = yt.render(yt.oembed(url), url, f"{language}, Whisper large-v3-turbo", True, snippets, a.every)
    a.out.write_text(text, encoding="utf-8")
    print(f"{a.out}: {len(snippets)} segments, {len(text)} characters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
