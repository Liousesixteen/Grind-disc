#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""概念库每日增量（2026-10-25）—— 防「新概念漏网」
================================================================
每天盘后跑一次（或随看板收盘任务跑）：
  1) 取当日涨停池（pool_<date> 快照 + themes.json）里出现的全部概念；
  2) 与 concept_map.json 比对 → **新概念**（库里没有的）写入
     journal/待归类_新增_<YYYY-MM>.md，并打印；
  3) 维护 cache/theme_universe/concept_stats.json：
     {概念: {first_seen, last_seen, zt_days, zt_today}}——记录每个概念的
     首见日/最近活跃日/累计涨停日数/当日涨停数，供「新热点是否持续」判断；
  4) --check 只体检不写盘。

设计边界：本脚本**只发现、不自动归类**——新概念要不要进某条链由人拍板（自动归类会污染坐标系）。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pankou import _limit_th
from theme_attr import REGION_TOKENS

CFG = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
UNI = os.path.join(ROOT, "cache", "theme_universe", "universe.json")

MAP = os.path.join(ROOT, "cache", "theme_universe", "concept_map.json")
STATS = os.path.join(ROOT, "cache", "theme_universe", "concept_stats.json")
JOURNAL = os.path.join(ROOT, "journal")


def load_map():
    return json.load(open(MAP, encoding="utf-8")) if os.path.exists(MAP) else {}


def latest_pool():
    base = os.path.join(ROOT, "cache")
    ds = [(n, os.path.join(base, n)) for n in os.listdir(base)
          if re.fullmatch(r"pool_\d{8}", n) and os.path.isfile(os.path.join(base, n, "all_a_snapshot.json"))]
    return sorted(ds)[-1] if ds else (None, None)


def _industry_names():
    """申万三级行业名全集（含「板块」后缀的写法）——行业不是概念，增量比对时必须剔除"""
    u = json.load(open(UNI, encoding="utf-8"))
    out = set()
    for code, v in u.items():
        if code.startswith("_"):
            continue
        for k in ("申万一级", "申万二级", "申万三级"):
            if v.get(k):
                out.add(v[k])
    return out


def day_concepts(pool_dir):
    """当日涨停池出现的新概念候选：剔除 行业名 / 地域 / 排除词 / 「xx板块」——
    与 universe 建库同一套过滤规则（不另起口径），剩下的才是「概念」"""
    snap = json.load(open(os.path.join(pool_dir, "all_a_snapshot.json"), encoding="utf-8"))
    zt = {r["code"] for r in snap
          if r.get("pct") is not None and r["pct"] <= 30.5
          and r["pct"] >= _limit_th(str(r["code"]), r.get("name"))}
    themes = {}
    p = os.path.join(pool_dir, "themes.json")
    if os.path.exists(p):
        themes = json.load(open(p, encoding="utf-8"))
    ind3 = _industry_names()
    exc = set(CFG["granularity"].get("exclude_words") or []) | set(REGION_TOKENS) | ind3
    cnt = Counter()
    for code in zt:
        for t in (themes.get(code) or "").split():
            if t in exc or t.endswith("板块") or t.endswith("Ⅱ") or t.endswith("Ⅲ"):
                continue
            cnt[t] += 1
    return zt, cnt


def run(check=False, date=None):
    mp = load_map()
    d8, pool = latest_pool()
    if not pool:
        raise SystemExit("找不到盘后池（cache/pool_YYYYMMDD）")
    zt, cnt = day_concepts(pool)
    new = [t for t in cnt if t not in mp]
    stats = json.load(open(STATS, encoding="utf-8")) if os.path.exists(STATS) else {}
    today = date or dt.date.today().strftime("%Y-%m-%d")
    for t, n in cnt.items():
        s = stats.setdefault(t, {"first_seen": today, "last_seen": today, "zt_days": 0, "zt_today": 0})
        if s.get("last_seen") != today:
            s["last_seen"] = today
            s["zt_days"] = int(s.get("zt_days") or 0) + 1
        s["zt_today"] = n
    print(f"池 {os.path.basename(pool)}｜涨停 {len(zt)} 只｜当日概念 {len(cnt)} 个")
    print(f"库内概念 {len(mp)} 个｜新概念 {len(new)} 个" + ("（未入库）" if new else "（无新增，库已全覆盖）"))
    if new:
        print("新概念：", "、".join(new[:30]))
        if not check:
            os.makedirs(JOURNAL, exist_ok=True)
            jf = os.path.join(JOURNAL, f"待归类_新增_{today[:7]}.md")
            head = "" if os.path.exists(jf) else f"# 新概念待归类（{today[:7]}）\n\n> 由 concept_daily.py 自动记录；确认后写入 concept_map.py 对应词表，跑 `concept_map.py --build --csv`。\n\n"
            with open(jf, "a", encoding="utf-8") as f:
                if head:
                    f.write(head)
                f.write(f"- {today}（池 {os.path.basename(pool)}，涨停 {len(zt)} 只）：" + "、".join(new) + "\n")
            print("已记入", jf)
    if not check:
        json.dump(stats, open(STATS, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        print("stats 已更新：", STATS)
    return new


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检不落盘")
    ap.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（默认今天）")
    a = ap.parse_args()
    run(a.check, a.date)
