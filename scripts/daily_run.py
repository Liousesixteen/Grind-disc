#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_run.py — 情绪模型「每日跑法」一键入口

为什么需要它：股吧有 IP 级风控（每轮约 40 请求、需间隔）、雪球有每日配额、
LLM 标注按量计费。散着跑容易撞墙，所以把一天切成三个固定时相，各给不同配比。

用法：
  .venv/bin/python scripts/daily_run.py --phase intraday   # 盘中：只读缓存+快讯+少量雪球
  .venv/bin/python scripts/daily_run.py --phase close      # 盘后 16:30：主力跑（股吧40+雪球8+全资讯）
  .venv/bin/python scripts/daily_run.py --phase night      # 夜间 21:30：二轮补抓（股吧40+雪球8）
  .venv/bin/python scripts/daily_run.py --phase status     # 只看今日配额用量，不发请求

设计原则：
- 盘中绝不碰股吧（白天最易触发风控，昨晚就是这样被封的）；
- 股吧每日总配额 160，拆成盘后 40 + 夜间 40 + 余量 80 应对补跑；
- 雪球每日 40 只，拆成盘中 5 + 盘后 8 + 夜间 8，余量留给补；
- LLM 标注每次跑完增量 300 条，只标新语料。
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
SCAN = os.path.join(ROOT, "scripts", "sentiment_scan.py")
RATE_DIR = os.path.join(ROOT, "cache", "rate")


def rate_path():
    return os.path.join(RATE_DIR, f"{dt.date.today().isoformat()}.json")


def show_status():
    p = rate_path()
    if not os.path.isfile(p):
        print("今日尚无请求记录")
        return
    d = json.load(open(p, encoding="utf-8"))
    print(f"日期 {dt.date.today().isoformat()}")
    print(f"  股吧  {d.get('guba', 0):>4} / 160")
    print(f"  雪球  {d.get('xueqiu', 0):>4} / 40")
    print(f"  SearchAPI {d.get('search_api', 0):>4} / 40（票）")
    labels = os.path.join(ROOT, "cache", "llm_labels")
    n = len(os.listdir(labels)) if os.path.isdir(labels) else 0
    print(f"  LLM 标注累计 {n} 条")


PHASES = {
    # phase: (args, 说明)
    "intraday": (
        ["--top", "50", "--budget", "0", "--xq-browser", "5", "--sapi", "6",
         "--llm-label", "100"],
        "盘中（不碰股吧直连：缓存 + Search API 补 6 只 + 雪球 5 + 快讯 + 互动易）"),
    "close": (
        ["--top", "50", "--budget", "40", "--xq-browser", "8", "--sapi", "8",
         "--llm-label", "300"],
        "盘后主力跑（股吧直连 40 + Search API 8 + 雪球 8 + 全资讯 + 标注 300）"),
    "night": (
        ["--top", "50", "--budget", "40", "--xq-browser", "8", "--sapi", "8",
         "--llm-label", "300", "--no-flash"],
        "夜间二轮（股吧再 40 + Search API 8 + 雪球 8，跳过快讯）"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=list(PHASES) + ["status"])
    ap.add_argument("--top", type=int, default=50)
    a = ap.parse_args()

    if a.phase == "status":
        show_status()
        return 0

    args, desc = PHASES[a.phase]
    args = list(args)
    if "--top" in args:
        args[args.index("--top") + 1] = str(a.top)

    print(f"══ 情绪模型每日跑 · {a.phase} ══")
    print(f"   {desc}\n")
    show_status()
    print()
    cmd = [PY, SCAN] + args
    print("执行:", " ".join(x for x in cmd if x != PY))
    print("-" * 60)
    rc = subprocess.call(cmd)
    print("-" * 60)
    show_status()
    return rc


if __name__ == "__main__":
    sys.exit(main())
