"""Extract the text of saved course lesson pages with [IMG n] markers, plus the image list for lesson_sheets.py.
Usage: python3 -I scripts/lesson_text.py OUT_DIR PAGE.html...
Writes OUT_DIR/<slug>.md (lesson text, [IMG n] where the page shows image n) and OUT_DIR/<slug>.imgs.json.
Local only: course pages are paid material (gitignored), so is the output.
"""

import html
import json
import re
import sys
from pathlib import Path

SKIP_IMG = re.compile(r"(logo|avatar|icon|flag|buy\.png|\.svg$)", re.I)
MAIN = re.compile(r"<main\b.*?</main>", re.S | re.I)


def slugify(name: str) -> str:
    name = re.sub(r"\s*[-–]\s*Education_DoubleTOP$", "", name)
    s = re.sub(r"[^\w]+", "-", name.lower()).strip("-")
    return s[:60]


def extract(page: Path) -> tuple[str, list[str]]:
    raw = page.read_text(encoding="utf-8", errors="replace")
    m = MAIN.search(raw)
    body = m.group(0) if m else raw
    body = re.sub(r"<(script|style|nav|header|footer)\b.*?</\1>", "", body, flags=re.S | re.I)
    imgs: list[str] = []

    def img(mt: re.Match) -> str:
        src = re.search(r'src="([^"]+)"', mt.group(0))
        if not src or SKIP_IMG.search(src.group(1)):
            return ""
        p = (page.parent / html.unescape(src.group(1))).resolve()
        imgs.append(str(p))
        return f"\n[IMG {len(imgs)}]\n"

    body = re.sub(r"<img\b[^>]*>", img, body, flags=re.I)
    body = re.sub(r"</?(p|div|li|h\d|br|tr)\b[^>]*>", "\n", body, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text, imgs


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for page in map(Path, sys.argv[2:]):
        slug = slugify(page.stem)
        text, imgs = extract(page)
        (out / f"{slug}.md").write_text(f"# {page.stem}\n\n{text}\n", encoding="utf-8")
        (out / f"{slug}.imgs.json").write_text(json.dumps(imgs, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"{slug}: {len(text)} chars, {len(imgs)} images")


if __name__ == "__main__":
    main()
