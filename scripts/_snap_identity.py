"""全池身份快照：把 1037 只的 (链, 环节, 证据) 存成 JSON，用于改词表前后的逐只比对。

用法：.venv/bin/python scripts/_snap_identity.py <输出名>
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import stock_profile as SP      # noqa: E402
import pool_pipeline as P       # noqa: E402
import theme_attr as TA         # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))


def load_pool():
    for d in ("pool_20261023", "pool_20261022", "pool_20261021"):
        pd = os.path.join(ROOT, "cache", d)
        if os.path.exists(os.path.join(pd, "pool_final.json")):
            rows = {r["code"]: r for r in json.load(open(os.path.join(pd, "pool_final.json"), encoding="utf-8"))}
            _, ind, _ = TA.load_tag_map(pd, CFG, P.classify_tags)
            return rows, ind
    return {}, {}


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "snap"
    pool, IND = load_pool()
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "cache", "profile", "*.json"))):
        p = json.load(open(f, encoding="utf-8"))
        code = p.get("code") or os.path.basename(f)[:-5]
        ch, sg, ev = SP.main_seg(p, CFG["chains"], IND.get(code) or [])
        out[code] = {"name": (pool.get(code) or {}).get("name", ""),
                     "chain": ch, "seg": sg, "ev": ev,
                     "items": [(x.get("name"), x.get("ratio")) for x in (p.get("items") or [])[:4]]}
    p = os.path.join(ROOT, "cache", "_replay", f"identity_{tag}.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n_none = sum(1 for v in out.values() if not v["chain"])
    n_noseg = sum(1 for v in out.values() if v["chain"] and not v["seg"])
    print(f"{tag}: {len(out)} 只 ｜ 未归链 {n_none} ｜ 有链无环节 {n_noseg}")
    print(f"已写 {p}")


if __name__ == "__main__":
    main()
