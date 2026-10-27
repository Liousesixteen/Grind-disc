#!/usr/bin/env python3
"""Build a coverage audit and readable article stream for the 波哥 Markdown archive.

The source archive is read-only. Article boundaries are anchored on the absolute
publication timestamp because a few source titles are not consistently marked as
Markdown headings.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DATE_RE = re.compile(
    r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}))?"
)
IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)")
LINK_RE = re.compile(r"(?<!!)\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\)")
META_PREFIXES = ("原创", "波之浪公众号为", "提示：")


@dataclass
class Article:
    file: Path
    title: str
    date: str
    time: str
    start: int
    end: int
    raw: list[str]

    @property
    def body(self) -> str:
        return normalize_body(self.raw, self.date)

    @property
    def image_count(self) -> int:
        return sum(len(IMAGE_RE.findall(line)) for line in self.raw)

    @property
    def link_count(self) -> int:
        return sum(len(LINK_RE.findall(line)) for line in self.raw)

    @property
    def fingerprint(self) -> str:
        canonical = re.sub(r"\s+", "", self.body)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def clean_line(line: str) -> str:
    return line.replace("\u00a0", " ").replace("\u200b", "").rstrip()


def title_for(lines: list[str], date_index: int) -> tuple[str, int]:
    lower = max(0, date_index - 12)
    for index in range(date_index - 1, lower - 1, -1):
        candidate = clean_line(lines[index]).strip()
        if "# " in candidate:
            return candidate.rsplit("# ", 1)[1].strip().rstrip("#").strip(), index

    for index in range(date_index - 1, lower - 1, -1):
        candidate = clean_line(lines[index]).strip().strip("#").strip()
        if not candidate or candidate.startswith(META_PREFIXES):
            continue
        if DATE_RE.search(candidate) or candidate.startswith("_"):
            continue
        return candidate, index
    return "（标题未识别）", date_index


def normalize_body(lines: list[str], date: str) -> str:
    output: list[str] = []
    image_number = 0
    blank = False
    for raw_line in lines:
        line = clean_line(raw_line).strip()
        if not line:
            if output and not blank:
                output.append("")
            blank = True
            continue
        blank = False
        if line.startswith("# ") or DATE_RE.search(line):
            continue
        if line.startswith(META_PREFIXES):
            continue
        if re.fullmatch(r"_?\s*(?:[\u4e00-\u9fff]{2,}|\d+人)\s*_?", line):
            continue

        def image_token(match: re.Match[str]) -> str:
            nonlocal image_number
            image_number += 1
            url = match.group("url")
            host = urlparse(url).netloc or "embedded"
            return f"[图像{image_number}:{host}]"

        line = IMAGE_RE.sub(image_token, line)
        output.append(line)
    while output and not output[-1]:
        output.pop()
    return "\n".join(output)


def parse_file(path: Path) -> list[Article]:
    lines = path.read_text(encoding="utf-8").splitlines()
    anchors: list[tuple[int, re.Match[str], str, int]] = []
    for index, line in enumerate(lines):
        match = DATE_RE.search(clean_line(line))
        # Publication metadata always contains a clock time. Historical dates
        # mentioned inside an article must not become false article boundaries.
        if not match or not match.group("time"):
            continue
        title, start = title_for(lines, index)
        anchors.append((index, match, title, start))

    articles: list[Article] = []
    for position, (date_index, match, title, start) in enumerate(anchors):
        end = anchors[position + 1][3] if position + 1 < len(anchors) else len(lines)
        if end <= start:
            end = anchors[position + 1][0] if position + 1 < len(anchors) else len(lines)
        date = f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
        articles.append(
            Article(
                file=path,
                title=title,
                date=date,
                time=match.group("time") or "--:--",
                start=start + 1,
                end=end,
                raw=lines[start:end],
            )
        )
    return articles


def load_archive(source: Path) -> list[Article]:
    articles: list[Article] = []
    for path in sorted(source.glob("*.md")):
        articles.extend(parse_file(path))
    return sorted(articles, key=lambda item: (item.date, item.time, item.file.name, item.start))


def emit_summary(articles: list[Article]) -> None:
    fingerprints: dict[str, list[Article]] = {}
    months: dict[str, list[Article]] = {}
    for article in articles:
        fingerprints.setdefault(article.fingerprint, []).append(article)
        months.setdefault(article.date[:7], []).append(article)

    print("| 月份 | 时间戳 | 独立日期 | 正文字符 | 图片 | 链接 |")
    print("|---|---:|---:|---:|---:|---:|")
    for month, group in months.items():
        print(
            f"| {month} | {len(group)} | {len({item.date for item in group})} | "
            f"{sum(len(item.body) for item in group)} | "
            f"{sum(item.image_count for item in group)} | "
            f"{sum(item.link_count for item in group)} |"
        )

    duplicate_groups = [group for group in fingerprints.values() if len(group) > 1]
    duplicate_entries = sum(len(group) - 1 for group in duplicate_groups)
    print()
    print(f"文章时间戳：{len(articles)}")
    print(f"独立日期：{len({article.date for article in articles})}")
    print(f"不同正文指纹：{len(articles) - duplicate_entries}")
    print(f"完全重复组：{len(duplicate_groups)}（额外重复条目 {duplicate_entries}）")
    print(f"原始正文字符：{sum(len(article.body) for article in articles)}")
    print()
    print("完全重复明细：")
    for group in duplicate_groups:
        locations = "；".join(
            f"{item.date} {item.time} {item.file.name}:{item.start}" for item in group
        )
        print(f"- {group[0].title}：{locations}")


def emit_index(articles: list[Article]) -> None:
    fingerprints: dict[str, list[Article]] = {}
    for article in articles:
        fingerprints.setdefault(article.fingerprint, []).append(article)

    print("| # | 日期时间 | 标题 | 文件:行 | 正文字符 | 图片 | 链接 | 指纹 |")
    print("|---:|---|---|---|---:|---:|---:|---|")
    for number, article in enumerate(articles, start=1):
        duplicate = " ↺" if len(fingerprints[article.fingerprint]) > 1 else ""
        print(
            f"| {number} | {article.date} {article.time} | {article.title} | "
            f"{article.file.name}:{article.start} | {len(article.body)} | "
            f"{article.image_count} | {article.link_count} | "
            f"`{article.fingerprint}`{duplicate} |"
        )

    unique_dates = {article.date for article in articles}
    duplicates = [group for group in fingerprints.values() if len(group) > 1]
    print()
    print(f"文章时间戳：{len(articles)}")
    print(f"独立日期：{len(unique_dates)}")
    print(f"完全重复组：{len(duplicates)}")
    print(f"原始正文字符：{sum(len(article.body) for article in articles)}")


def emit_report(articles: list[Article], source: Path) -> None:
    image_urls = [
        url
        for article in articles
        for line in article.raw
        for _, url in IMAGE_RE.findall(line)
    ]
    qpic_urls = [url for url in image_urls if url.startswith("https://mmbiz.qpic.cn/")]
    print("# 波哥帖子逐篇覆盖审计")
    print()
    print(f"- 来源：`{source}`")
    print("- 边界：以含时分的发布日期为文章锚点；完全重复内容保留并标记 `↺`。")
    print(
        f"- 图像：共 {len(image_urls)} 个引用；其中 {len(qpic_urls)} 个微信图床引用、"
        f"{len(set(qpic_urls))} 个唯一 URL 已下载核看，其余 {len(image_urls) - len(qpic_urls)} 个为 1×1 SVG 占位符。"
    )
    print("- 指纹：对规范化正文计算 SHA-256 前 16 位，用于发现完全重复正文。")
    print()
    print("## 汇总")
    print()
    emit_summary(articles)
    print()
    print("## 逐篇索引")
    print()
    emit_index(articles)


def emit_month(
    articles: list[Article], month: str, first: int | None = None, last: int | None = None
) -> None:
    selected = [article for article in articles if article.date.startswith(month)]
    numbered = list(enumerate(selected, start=1))
    if first is not None:
        numbered = [item for item in numbered if item[0] >= first]
    if last is not None:
        numbered = [item for item in numbered if item[0] <= last]
    for number, article in numbered:
        print(
            f"\n===== {month} / {number:02d} | {article.date} {article.time} | "
            f"{article.title} | {article.file.name}:{article.start}-{article.end} | "
            f"fingerprint={article.fingerprint} =====\n"
        )
        print(article.body)
    print(f"\n===== {month} 合计 {len(selected)} 个时间戳 =====")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--month", help="Emit normalized full text for YYYY-MM")
    parser.add_argument("--first", type=int, help="First monthly article number")
    parser.add_argument("--last", type=int, help="Last monthly article number")
    parser.add_argument("--summary", action="store_true", help="Emit compact audit totals")
    parser.add_argument("--report", action="store_true", help="Emit a Markdown audit report")
    args = parser.parse_args()
    articles = load_archive(args.source)
    if args.month:
        emit_month(articles, args.month, args.first, args.last)
    elif args.report:
        emit_report(articles, args.source)
    elif args.summary:
        emit_summary(articles)
    else:
        emit_index(articles)


if __name__ == "__main__":
    main()
