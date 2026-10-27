#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F10 主营构成全量抓取（2026-10-25）——产品层 L4 的原料
读 universe 全部代码 → 复用 stock_profile.fetch_one/load → 写 cache/profile/
产出：cache/theme_universe/products.json  {产品名: {n_stocks, ratio_sum, seg_hint}}
"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import stock_profile as SP

u = json.load(open(os.path.join(ROOT, "cache/theme_universe/universe.json"), encoding="utf-8"))
codes = [c for c in u if not c.startswith("_")]
t0 = time.time()
got = SP.load(codes, workers=6, verbose=True)
prof = {c: v for c, v in got.items() if v}
ok = {c: v for c, v in prof.items() if v.get("items")}
print(f"画像 {len(prof)}/{len(codes)}｜有主营项 {len(ok)}｜耗时 {time.time()-t0:.0f}s")

prod = {}
for c, v in ok.items():
    for it in v["items"]:
        nm = (it.get("name") or "").strip()
        if not nm:
            continue
        d = prod.setdefault(nm, {"n_stocks": 0, "ratio_sum": 0.0, "stocks": []})
        d["n_stocks"] += 1
        d["ratio_sum"] = round(d["ratio_sum"] + (it.get("ratio") or 0), 1)
        if len(d["stocks"]) < 8:
            d["stocks"].append(c)
out = os.path.join(ROOT, "cache/theme_universe/products.json")
json.dump(prod, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"产品节点（去重）{len(prod)} → {out}")
top = sorted(prod.items(), key=lambda kv: -kv[1]["n_stocks"])[:15]
print("覆盖最广的产品项：", "、".join(f"{k}({v['n_stocks']})" for k, v in top))
