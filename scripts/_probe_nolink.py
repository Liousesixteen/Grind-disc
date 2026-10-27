"""未归链 73 只的「可不可以归」体检（只读，不改任何配置）

回答一个具体问题：这 73 只里，有多少是**真不属于 19 链**，有多少是**规则没覆盖**？
拆成三种病因，各自可归的数量：

  A. 阈值连锁误杀：占比最大那一项名字很泛（『高端装备制造』『工业产品』『销售商品』），
     真正能定链的第二项被 `max(20%, top×0.85)` 砍掉。
  B. 关键词缺失：主营项名说的是链内东西，但词表里没那个词（『汽车齿轮』『新能源汽车』）。
  C. 真不属于：工业锅炉 / 冷却塔 / 缝纫机 / 割草机 / 电梯部件——19 链是**题材链**，
     这些是传统制造，本来就没有题材归属。

用法：.venv/bin/python scripts/_probe_nolink.py
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
CHAINS = CFG["chains"]


def load_pool():
    for d in ("pool_20261023", "pool_20261022", "pool_20261021"):
        pd = os.path.join(ROOT, "cache", d)
        if os.path.exists(os.path.join(pd, "pool_final.json")):
            rows = {r["code"]: r for r in json.load(open(os.path.join(pd, "pool_final.json"), encoding="utf-8"))}
            _, ind, _ = TA.load_tag_map(pd, CFG, P.classify_tags)
            return rows, ind
    return {}, {}


def main():
    pool, IND = load_pool()
    files = sorted(glob.glob(os.path.join(ROOT, "cache", "profile", "*.json")))
    A, B, C = [], [], []
    for f in files:
        p = json.load(open(f, encoding="utf-8"))
        code = p.get("code") or os.path.basename(f)[:-5]
        inds = IND.get(code) or []
        ch, sg, ev = SP.main_seg(p, CHAINS, inds)
        if ch:
            continue                                  # 已归链，不体检

        its = [x for x in (p.get("items") or []) if (x.get("ratio") or 0) > 0]
        top = max([x.get("ratio") or 0 for x in its], default=0)
        want = max(20.0, top * 0.85)

        used = [x for x in its if (x.get("ratio") or 0) >= want]        # 现行规则看了哪几项
        rest = [x for x in its if 20.0 <= (x.get("ratio") or 0) < want]  # 被 85% 门槛砍掉的

        hit_used = [(x["name"], x.get("ratio"), SP.deep_match(x["name"], CHAINS)) for x in used]
        hit_rest = [(x["name"], x.get("ratio"), SP.deep_match(x["name"], CHAINS)) for x in rest]
        good_rest = [h for h in hit_rest if h[2][0]]

        name = (pool.get(code) or {}).get("name", "")
        rec = dict(code=code, name=name, inds=inds, top=top,
                   used=[(h[0], h[1]) for h in hit_used],
                   good_rest=[(h[0], h[1], h[2][0], h[2][1]) for h in good_rest],
                   items=[(x["name"], x.get("ratio")) for x in its[:4]])

        if good_rest:
            A.append(rec)                             # 阈值砍掉了能定链的项
        else:
            # 现行规则看的项能不能被某个「差一个词」的解释救活？交给人工列候选（B/C 粗分）
            C.append(rec)

    def dump(tag, rows):
        print(f"\n=== {tag}（{len(rows)} 只） ===")
        for r in rows:
            print(f"{r['code']} {r['name']:<7} 行业={'/'.join(r['inds']) or '—'}")
            print(f"    主营: {' | '.join(f'{n}{v}%' for n, v in r['items'])}")
            if r['good_rest']:
                print(f"    被阈值砍掉但能定链: {r['good_rest']}")

    print(f"未归链体检：A 阈值误杀 + B 关键词缺 + C 真不属于")
    dump("A. 阈值连锁误杀——放宽后立刻能归", A)
    print(f"\n剩 {len(C)} 只：现行规则看到的项全都匹配不上（关键词缺 or 真不属于）")
    json.dump({"A": A, "C": C}, open(os.path.join(ROOT, "cache", "_nolink_probe.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"明细已写 cache/_nolink_probe.json")


if __name__ == "__main__":
    main()
