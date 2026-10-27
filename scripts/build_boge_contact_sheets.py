#!/usr/bin/env python3
"""Build labeled contact sheets from the recoverable 波哥 archive images."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def fit_image(path: Path, width: int, height: int) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGB")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), "white")
    canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    records = json.loads((args.image_dir / "manifest.json").read_text(encoding="utf-8"))
    unique: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("downloaded"):
            unique.setdefault(str(record["local_file"]), record)
    by_month: dict[str, list[dict[str, object]]] = {}
    for record in unique.values():
        by_month.setdefault(str(record["file"]), []).append(record)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    columns, rows = 3, 4
    tile_width, image_height, label_height = 500, 315, 55
    page_size = columns * rows
    font = ImageFont.load_default(size=18)
    for month, group in sorted(by_month.items()):
        group.sort(key=lambda item: (int(item["line"]), int(item["number"])))
        for page_number in range(math.ceil(len(group) / page_size)):
            page = group[page_number * page_size : (page_number + 1) * page_size]
            sheet = Image.new(
                "RGB", (columns * tile_width, rows * (image_height + label_height)), "#dddddd"
            )
            draw = ImageDraw.Draw(sheet)
            for slot, record in enumerate(page):
                x = (slot % columns) * tile_width
                y = (slot // columns) * (image_height + label_height)
                tile = fit_image(args.image_dir / str(record["local_file"]), tile_width - 8, image_height - 8)
                sheet.paste(tile, (x + 4, y + 4))
                label = f"#{record['number']}  {record['file']}:{record['line']}"
                draw.rectangle((x, y + image_height, x + tile_width, y + image_height + label_height), fill="white")
                draw.text((x + 10, y + image_height + 12), label, fill="black", font=font)
            output = args.output_dir / f"{month.removesuffix('.md')}_p{page_number + 1}.jpg"
            sheet.save(output, quality=92)
            print(output)


if __name__ == "__main__":
    main()
