#!/usr/bin/env python3
"""Inventory and optionally download recoverable images from the 波哥 archive."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import socket
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


IMAGE_RE = re.compile(r"!\[(?P<alt>[^]]*)\]\((?P<url>https://mmbiz\.qpic\.cn/[^)]+)\)")


def inventory(source: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(source.glob("*.md")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in IMAGE_RE.finditer(line):
                url = match.group("url")
                records.append(
                    {
                        "number": len(records) + 1,
                        "file": path.name,
                        "line": line_number,
                        "alt": match.group("alt"),
                        "url": url,
                        "url_hash": hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
                    }
                )
    return records


def extension(content_type: str, url: str) -> str:
    mapping = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    if content_type.split(";", 1)[0] in mapping:
        return mapping[content_type.split(";", 1)[0]]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".img"


def download_one(record: dict[str, object], output: Path) -> dict[str, object]:
    existing = list(output.glob(f"*_{record['url_hash']}.*"))
    if existing:
        record.update(
            {"downloaded": True, "local_file": existing[0].name, "bytes": existing[0].stat().st_size, "cached": True}
        )
        return record
    try:
        request = urllib.request.Request(
            str(record["url"]), headers={"User-Agent": "Mozilla/5.0 image coverage audit"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
        suffix = extension(content_type, str(record["url"]))
        filename = (
            f"{int(record['number']):03d}_{record['file']}_{int(record['line'])}_"
            f"{record['url_hash']}{suffix}"
        )
        (output / filename).write_bytes(data)
        record.update(
            {"downloaded": True, "local_file": filename, "bytes": len(data), "content_type": content_type}
        )
    except Exception as exc:  # The manifest must retain failed URLs for audit.
        record.update({"downloaded": False, "error": f"{type(exc).__name__}: {exc}"})
    return record


def download(records: list[dict[str, object]], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    socket.setdefaulttimeout(15)
    first_by_url: dict[str, dict[str, object]] = {}
    for record in records:
        first_by_url.setdefault(str(record["url"]), record)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(download_one, record, output) for record in first_by_url.values()]
        concurrent.futures.wait(futures)
    results = {str(record["url"]): record for record in first_by_url.values()}
    for record in records:
        primary = results[str(record["url"])]
        for key in ("downloaded", "local_file", "bytes", "content_type", "cached", "error"):
            if key in primary:
                record[key] = primary[key]
    (output / "manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--download", type=Path)
    args = parser.parse_args()
    records = inventory(args.source)
    print(f"recoverable_qpic_references={len(records)}")
    print(f"unique_urls={len({record['url'] for record in records})}")
    if args.download:
        download(records, args.download)
        succeeded = sum(bool(record.get("downloaded")) for record in records)
        print(f"downloaded={succeeded}")
        print(f"failed={len(records) - succeeded}")


if __name__ == "__main__":
    main()
