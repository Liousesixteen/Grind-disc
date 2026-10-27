#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日强度：每只票「当日涨的最相关概念」。

口径（项目铁律：相对超额，不编造）：
  概念层：成分股当日 pct → 家数 / 涨停数(pct>=9.8) / 中位 / 均值 / 上涨占比 /
          相对超额 = 概念中位 − 全市场涨停前中位。只保留家数 >= min_members（pool_config.json::granularity.min_members）。
  个股层：当日 pct>0 的票，在其「有效概念」∩ 强概念里选相对超额最高者；
          并列时取涨停数多者，再取家数少者（更聚焦）。
          自身涨幅在该概念内的百分位 = 概念内 pct 低于自身的比例。
  取不到强概念 → best_concept 留空，计入落空。

只读 universe.json / pool_config.json / 快照，不写这三个文件。支持 --date/--pool 每日重跑。
"""
import argparse
import json
import os
import shutil
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZT_PCT = 9.8  # 简化涨停口径


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def backup_if_exists(path):
    if os.path.exists(path):
        bak = path + ".bak"
        shutil.copy2(path, bak)
        print(f"[backup] {path} -> {bak}")


def main():
    ap = argparse.ArgumentParser(description="每日个股最相关概念强度")
    ap.add_argument("--pool", default=os.path.join(ROOT, "cache/pool_20261023_intraday"))
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD，默认从 pool 目录名推断")
    ap.add_argument("--min-members", type=int, default=None, help="概念最小家数门槛，默认读 pool_config.json")
    args = ap.parse_args()

    # 日期：优先参数，否则从 pool 目录名 pool_YYYYMMDD_* 提取
    date = args.date
    if not date:
        base = os.path.basename(os.path.normpath(args.pool))
        import re
        m = re.search(r"(\d{8})", base)
        if m:
            d = m.group(1)
            date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        else:
            sys.exit("[err] 无法从 pool 目录推断日期，请用 --date YYYY-MM-DD")

    snap_path = os.path.join(args.pool, "all_a_snapshot.json")
    uni_path = os.path.join(ROOT, "cache/theme_universe/universe.json")
    cfg_path = os.path.join(ROOT, "scripts/pool_config.json")
    snap = load_json(snap_path)
    universe = load_json(uni_path)
    universe.pop("_meta", None)
    cfg = load_json(cfg_path)
    min_members = args.min_members or int(cfg.get("granularity", {}).get("min_members", 3))

    # ---- 全市场基准 ----
    pcts_all = [s["pct"] for s in snap if isinstance(s.get("pct"), (int, float))]
    med_all = statistics.median(pcts_all)
    print(f"[基准] 全市场 n={len(pcts_all)} 中位={med_all:.3f}% min_members={min_members}")

    # ---- 概念层聚合 ----
    concept_pcts = defaultdict(list)  # concept -> [pct]
    for s in snap:
        d = universe.get(s["code"])
        if not d:
            continue
        p = s.get("pct")
        if not isinstance(p, (int, float)):
            continue
        for c in d.get("有效概念", []):
            concept_pcts[c].append(p)

    concepts = {}
    for c, ps in concept_pcts.items():
        n = len(ps)
        if n < min_members:
            continue
        med = statistics.median(ps)
        concepts[c] = {
            "n": n,
            "zt": sum(1 for p in ps if p >= ZT_PCT),
            "med": round(med, 3),
            "mean": round(sum(ps) / n, 3),
            "up_ratio": round(sum(1 for p in ps if p > 0) / n, 4),
            "excess": round(med - med_all, 3),
        }
    ranked = sorted(concepts.items(), key=lambda kv: (-kv[1]["excess"], -kv[1]["zt"], kv[1]["n"]))
    print(f"[概念层] 候选概念={len(concept_pcts)} 入选(≥{min_members}家)={len(concepts)}")

    # ---- 个股层 ----
    out = {}
    n_up = n_hit = n_miss = 0
    miss_codes = []
    for s in snap:
        code = s["code"]
        p = s.get("pct")
        if not isinstance(p, (int, float)) or p <= 0:
            continue
        n_up += 1
        d = universe.get(code)
        if not d:
            n_miss += 1
            miss_codes.append(code)
            out[code] = {"name": s.get("name", ""), "pct": p, "best_concept": ""}
            continue
        cands = []
        for c in d.get("有效概念", []):
            info = concepts.get(c)
            if not info:
                continue  # 未入选强概念 → 不算
            # 自身涨幅在该概念内的百分位
            ps = concept_pcts[c]
            below = sum(1 for x in ps if x < p)
            cands.append((info["excess"], info["zt"], info["n"], c, info["med"], below / len(ps)))
        if not cands:
            n_miss += 1
            miss_codes.append(code)
            out[code] = {"name": d.get("name", s.get("name", "")), "pct": p, "best_concept": ""}
            continue
        # 相对超额最高 → 涨停数多 → 家数少（更聚焦）
        cands.sort(key=lambda x: (-x[0], -x[1], x[2]))
        excess, zt, n, c, med, rank = cands[0]
        n_hit += 1
        out[code] = {
            "name": d.get("name", s.get("name", "")),
            "pct": p,
            "best_concept": c,
            "concept_med": med,
            "excess": excess,
            "concept_zt": zt,
            "rank_in_concept": round(rank, 4),
        }
    print(f"[个股层] 上涨票={n_up} 命中={n_hit} 落空={n_miss} 落空率={n_miss/n_up*100:.1f}%")

    # ---- 落盘（同名先备份）----
    uni_dir = os.path.join(ROOT, "cache/theme_universe")
    out_stock = os.path.join(uni_dir, f"strength_{date}.json")
    out_concept = os.path.join(uni_dir, f"strength_{date}_concepts.json")
    backup_if_exists(out_stock)
    backup_if_exists(out_concept)
    with open(out_stock, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    with open(out_concept, "w", encoding="utf-8") as f:
        json.dump(
            {"date": date, "med_all": round(med_all, 3), "min_members": min_members,
             "n_concepts": len(concepts), "concepts": [{"concept": c, **info} for c, info in ranked]},
            f, ensure_ascii=False, indent=1)
    print(f"[out] {out_stock}\n[out] {out_concept}")

    # ---- 核验 6 只票 ----
    focus = ["603280", "603028", "603713", "002009", "603838", "920091"]
    print("\n[核验6票]")
    for c in focus:
        r = out.get(c, {})
        print(f"  {c} {r.get('name','')} pct={r.get('pct')} -> {r.get('best_concept','(空)')} "
              f"excess={r.get('excess')} concept_zt={r.get('concept_zt')} rank={r.get('rank_in_concept')}")

    print("\n[top20 概念]")
    for c, info in ranked[:20]:
        print(f"  {c}: 超额={info['excess']} 中位={info['med']} 家数={info['n']} 涨停={info['zt']}")


if __name__ == "__main__":
    main()
