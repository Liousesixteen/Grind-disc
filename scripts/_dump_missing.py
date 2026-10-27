"""列出「缺环节」的票——Step2 后半的攻击目标清单（只读，不改任何东西）。

用法：
  python3 scripts/_dump_missing.py            # 只列缺环节
  python3 scripts/_dump_missing.py --all      # 连链级的一起列
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import stock_profile as SP  # noqa: E402
import pool_pipeline as P  # noqa: E402
import theme_attr as TA  # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
CHAINS = CFG["chains"]


def load_pool():
    """行业标签必须走 theme_attr.load_tag_map（themes.json 三层全），
    pool_final.json 里的「行业标签」多数只留了一级名——拿它查表等于没查。"""
    for d in ("pool_20261023", "pool_20261022", "pool_20261021"):
        pd = os.path.join(ROOT, "cache", d)
        if os.path.exists(os.path.join(pd, "pool_final.json")):
            rows = {r["code"]: r for r in json.load(open(os.path.join(pd, "pool_final.json"), encoding="utf-8"))}
            _, ind, _ = TA.load_tag_map(pd, CFG, P.classify_tags)
            return rows, ind
    return {}, {}


def main():
    show_all = "--all" in sys.argv
    pool, IND = load_pool()
    files = sorted(glob.glob(os.path.join(ROOT, "cache", "profile", "*.json")))
    miss, chainonly = [], []
    for f in files:
        p = json.load(open(f, encoding="utf-8"))
        code = p.get("code") or os.path.basename(f)[:-5]
        row = pool.get(code) or {}
        inds = IND.get(code) or []
        ch, sg, ev = SP.main_seg(p, CHAINS, inds)
        if not ch:
            miss.append((code, row.get("name", ""), inds, p, ev))
        elif not sg:
            chainonly.append((code, row.get("name", ""), inds, p, ev))

    print(f"总 {len(files)} 只 ｜ 无链 {len(miss)} ｜ 只有链无环节 {len(chainonly)}")
    print("\n=== A. 只有链、无环节 ===")
    for code, name, inds, p, ev in chainonly:
        its = " / ".join(f"{x['name']}{x.get('ratio')}%" for x in (p.get("items") or [])[:4])
        print(f"{code} {name:<6} | {ev:<28} | 行业={'/'.join(inds) or '—'} | {its}")
    print("\n=== B. 完全没归链 ===")
    for code, name, inds, p, ev in miss:
        its = " / ".join(f"{x['name']}{x.get('ratio')}%" for x in (p.get("items") or [])[:4])
        print(f"{code} {name:<6} | 行业={'/'.join(inds) or '—'} | {its}")

    out = os.path.join(ROOT, "cache", "step2_targets.json")
    json.dump([
        {"code": c, "name": n, "inds": i, "seg": ev,
         "items": p.get("items") or [], "basis": p.get("basis"), "asof": p.get("asof"),
         "scope": p.get("scope") or ""}
        for c, n, i, p, ev in chainonly + miss
    ], open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写 {out}")


if __name__ == "__main__":
    main()
