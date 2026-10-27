"""地域锚点修复的全量 diff：新代码 vs 修复前代码（只读）。

比两层：
  1. 行业标签 / 概念标签（IND、CON）——影响所有 5576 只的归因
  2. 主业环节（main_seg）——影响 1036 只的身份
"""
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = r'''
import json, sys, glob, os
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])   # 依赖在 scripts/，被比较的模块必须留在 path[0]
import theme_attr as TA, pool_pipeline as P
cfg = json.load(open(sys.argv[3], encoding="utf-8"))
con, ind, _ = TA.load_tag_map(sys.argv[4], cfg, P.classify_tags)
json.dump({"con": con, "ind": ind}, open(sys.argv[5], "w", encoding="utf-8"), ensure_ascii=False)
'''


def run(moddir, cfg, out):
    code = SCRIPT
    subprocess.run([sys.executable, "-c", code, moddir, os.path.join(ROOT, "scripts"),
                    cfg, os.path.join(ROOT, "cache", "pool_20261023"), out], check=True)


def main():
    os.makedirs("/tmp/diffout", exist_ok=True)
    run("/tmp/oldmod", "/tmp/oldmod/pool_config.json", "/tmp/diffout/old.json")
    run(os.path.join(ROOT, "scripts"), os.path.join(ROOT, "scripts", "pool_config.json"),
        "/tmp/diffout/new.json")
    old = json.load(open("/tmp/diffout/old.json", encoding="utf-8"))
    new = json.load(open("/tmp/diffout/new.json", encoding="utf-8"))

    ci = [c for c in old["ind"] if old["ind"][c] != new["ind"].get(c)]
    cc = [c for c in old["con"] if old["con"][c] != new["con"].get(c)]
    print(f"行业标签变化 {len(ci)} 只 ｜ 概念标签变化 {len(cc)} 只")
    raw = json.load(open(os.path.join(ROOT, "cache", "pool_20261023", "themes.json"), encoding="utf-8"))
    for c in ci[:80]:
        print(f"  {c} 行业: {old['ind'][c]} → {new['ind'][c]}")
    print("\n概念标签变化（前 20 只，看是否只少了地域/行业名）:")
    for c in cc[:20]:
        o, n = set(old["con"][c]), set(new["con"][c])
        print(f"  {c} 少={sorted(o-n)[:5]} 多={sorted(n-o)[:5]}")

    # 主业环节 diff
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import stock_profile as SP
    C_old = json.load(open("/tmp/oldmod/pool_config.json", encoding="utf-8"))["chains"]
    C_new = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))["chains"]
    ch = []
    for f in glob.glob(os.path.join(ROOT, "cache", "profile", "*.json")):
        p = json.load(open(f, encoding="utf-8"))
        c = p.get("code") or os.path.basename(f)[:-5]
        a = SP.main_seg(p, C_old, old["ind"].get(c, []))
        b = SP.main_seg(p, C_new, new["ind"].get(c, []))
        if a[:2] != b[:2]:
            ch.append((c, a, b, p))
    print(f"\n主业环节变化 {len(ch)} 只")
    for c, a, b, p in sorted(ch):
        its = " / ".join(f"{x['name']}{x.get('ratio')}%" for x in (p.get("items") or [])[:2])
        print(f"  {c} {a[0]}·{a[1] or '-'} → {b[0]}·{b[1] or '-'}  ({b[2]}) | {its}")


if __name__ == "__main__":
    main()
