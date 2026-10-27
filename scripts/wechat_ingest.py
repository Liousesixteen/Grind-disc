#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat_ingest.py — 微信群情绪通道（合规版）v1.0

原理：不连微信、不爬微信。用户把自己群里的聊天记录手动导出（合并转发给自己
→ 复制粘贴）存成 txt，放到 data/wechat_exports/<key>/<日期>.txt，本工具负责
解析 → 分类 → 聚合，并落盘为情绪模型可读的「微信群」组快照。

合规边界：只处理用户自愿提供的、用户自己所在群的内容；工具不接入微信协议、
不做任何自动化采集。样本是自选群，代表性有限，读数仅供对照，不单独成信号。

用法：
  python scripts/wechat_ingest.py --key 300750 --date 2026-10-17
  python scripts/wechat_ingest.py --key 300750 --date 2026-10-17 --file 某路径.txt
输出：
  cache/sentiment/wechat_<key>/<date>.json   （sentiment_model.py 自动读取近3天内的最新一份）
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from sentiment_model import classify, CATS  # 复用同一套词表，保证组间可比

CFG = json.load(open(os.path.join(ROOT, "scripts", "sentiment_config.json"), encoding="utf-8"))
EXPORT_DIR = os.path.join(ROOT, "data", "wechat_exports")
TIME_RE = re.compile(r"(?:\d{4}年)?(?:\d{1,2}月)?(?:\d{1,2}日)?\s*(?:上午|下午|晚上|凌晨|中午)?\s*\d{1,2}:\d{2}(?::\d{2})?")


def parse_records(text):
    """块式解析：发言人行 + 时间行 + 正文（可多行），空行分块。
    解不出时间头时退化为「一行一条」。以 # 开头的行视为注释跳过。"""
    lines_all = [l for l in text.splitlines()]
    body = "\n".join(l for l in lines_all if not l.strip().startswith("#"))
    blocks = [b for b in re.split(r"\n\s*\n", body) if b.strip()]
    recs = []
    for b in blocks:
        lines = [l.strip() for l in b.splitlines() if l.strip()]
        if not lines:
            continue
        tm, spk, rest = None, None, lines
        for i, l in enumerate(lines):
            m = TIME_RE.search(l)
            if m and len(l) <= 40:
                tm = m.group(0)
                if i > 0 and not TIME_RE.search(lines[i - 1]):
                    spk = lines[i - 1]
                rest = lines[i + 1:]
                break
        content = " ".join(rest).strip()
        if content:
            recs.append({"speaker": spk, "time": tm, "content": content})
    if not recs:
        recs = [{"speaker": None, "time": None, "content": l.strip()}
                for l in body.splitlines()
                if l.strip() and not l.strip().startswith("#")]
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="股票代码或主题标识，如 300750 / theme_半导体")
    ap.add_argument("--date", required=True, help="聊天内容对应的日期 YYYY-MM-DD")
    ap.add_argument("--file", help="显式指定 txt；缺省则找 data/wechat_exports/<key>/<date>.txt")
    a = ap.parse_args()

    path = a.file or os.path.join(EXPORT_DIR, a.key, f"{a.date}.txt")
    if not os.path.isfile(path):
        print(f"未找到导出文件：{path}")
        print("请把群聊记录（合并转发→复制粘贴）存到上述路径，格式见 data/wechat_exports/_格式模板.txt")
        return 1

    text = open(path, encoding="utf-8", errors="ignore").read()
    recs = parse_records(text)
    if not recs:
        print("解析结果为空：检查文件内容与格式模板是否一致")
        return 1

    for r in recs:
        r["cat"], r["kw"] = classify(r["content"])
    n = len(recs)
    c = Counter(r["cat"] for r in recs)
    speakers = Counter((r["speaker"] or "匿名") for r in recs)
    out = {
        "meta": {"tool": "wechat_ingest", "version": "1.0", "key": a.key,
                 "data_date": a.date, "source_file": os.path.relpath(path, ROOT),
                 "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "note": "微信群样本=用户自选群手动导出，代表性有限，仅供对照，不单独成信号"},
        "微信群": {"样本": n,
                 "正面%": round(c["正面"] / n * 100, 1),
                 "中性%": round(c["中性"] / n * 100, 1),
                 "负面%": round(c["负面"] / n * 100, 1),
                 "发言人数": len(speakers),
                 "活跃前5": speakers.most_common(5),
                 "_records": recs[:30]},
    }
    if n < CFG["params"]["min_sample_warn"]:
        out["微信群"]["_warn"] = f"样本 < {CFG['params']['min_sample_warn']}，比例波动大，仅作方向参考"

    out_d = os.path.join(ROOT, "cache", "sentiment", f"wechat_{a.key}")
    os.makedirs(out_d, exist_ok=True)
    p = os.path.join(out_d, f"{a.date}.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"解析 {n} 条 / {len(speakers)} 位发言人 → {p}")
    print("  " + " / ".join(f"{cat}{out['微信群'][cat + '%']}%" for cat in CATS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
