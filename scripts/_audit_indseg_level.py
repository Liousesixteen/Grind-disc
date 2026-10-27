"""审查 `_行业定环节`：哪些键不是申万三级名（层级错配的入口）。只读。"""
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import pool_pipeline as P  # noqa: E402
import theme_attr as TA  # noqa: E402

cfg = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
_, IND, _ = TA.load_tag_map(os.path.join(ROOT, "cache", "pool_20261023"), cfg, P.classify_tags)
L1, L2, L3 = set(), set(), set()
for v in IND.values():
    if len(v) >= 1:
        L1.add(v[0])
    if len(v) >= 2:
        L2.add(v[1])
    if len(v) >= 3:
        L3.add(v[2])
print(f"一级 {len(L1)} ｜ 二级 {len(L2)} ｜ 三级 {len(L3)}")

t = cfg["chains"]["_行业定环节"]
SUF = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")


def level(k):
    b = SUF.sub("", k).strip()
    if b in L3 or k in L3:
        return "三级"
    if b in L2 or k in L2:
        return "二级"
    if b in L1 or k in L1:
        return "一级"
    return "非行业名"


lv = {k: level(k) for k in t}
print(Counter(lv.values()))
print("\n非三级键（层级错配的入口，需要逐条核对）:")
for k in sorted(t, key=lambda x: (lv[x], x)):
    if lv[k] != "三级":
        # 该键下有多少只票
        n = sum(1 for v in IND.values() if any(SUF.sub("", x).strip() == k or x == k for x in v))
        print(f"  [{lv[k]}] {k:<14} → {t[k]:<18} 影响 {n} 只")
