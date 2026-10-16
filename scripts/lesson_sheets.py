"""Contact sheets of the images embedded in course lessons (2x2 per sheet, labelled [IMG n]), local only.
Usage: uv run --no-project --with pillow python -I scripts/lesson_sheets.py OUT_DIR IMGS_JSON...
IMGS_JSON: a JSON list of the image paths of one saved lesson page, in page order (as [IMG n] in its extracted text).
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

CELL_W, CELL_H = 960, 600


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for js in map(Path, sys.argv[2:]):
        paths = [Path(p) for p in json.loads(js.read_text())]
        stem = js.name.replace(".imgs.json", "")
        cells = []
        for n, p in enumerate(paths, 1):
            if not p.exists() or p.name in ("buy.png",) or p.stat().st_size < 8000:
                continue
            try:
                im = Image.open(p).convert("RGB")
            except Exception:  # noqa: BLE001
                continue
            im.thumbnail((CELL_W, CELL_H - 30))
            cell = Image.new("RGB", (CELL_W, CELL_H), "white")
            cell.paste(im, (0, 30))
            ImageDraw.Draw(cell).text((8, 8), f"[IMG {n}] {stem[:60]}", fill="black")
            cells.append(cell)
        for i in range(0, len(cells), 4):
            sheet = Image.new("RGB", (CELL_W * 2, CELL_H * 2), "white")
            for k, c in enumerate(cells[i : i + 4]):
                sheet.paste(c, ((k % 2) * CELL_W, (k // 2) * CELL_H))
            sheet.save(out / f"{stem}-{i // 4 + 1:02d}.jpg", quality=85)
        print(f"{stem}: {len(cells)} images, {(len(cells) + 3) // 4} sheets")


if __name__ == "__main__":
    main()
