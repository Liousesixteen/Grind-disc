"""把「补词 + 放宽占比阈值」两套改法放进沙盘，量出真实收益与真实误伤。

不改生产配置、不改生产代码——配置改在内存副本，代码用 `inspect` 取 main_seg 原文后
**只替换那一行阈值判断**再 exec，保证除门槛外每一行逻辑都和生产逐字一致
（第一版手抄整个函数，漏掉「行业兜底」分支，未归链虚涨到 508 只——假的，已废弃）。

  A 补词（不动阈值）：只救「占比最大那一项的名字里就有这个词」的票
  B 放宽阈值：最大项是泛词（高端装备制造/工业产品/销售商品…）时，允许第二项 ≥25% 参与定链

用法：.venv/bin/python scripts/_probe_recover.py
"""
import copy
import glob
import inspect
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import stock_profile as SP      # noqa: E402
import pool_pipeline as P       # noqa: E402
import theme_attr as TA         # noqa: E402

CFG = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))

NEW_KW = [
    ("汽车齿轮",        "新能源车链", "零部件"),
    ("媒体代理",        "传媒内容链", "广告营销"),
    ("线性驱动",        "机器人链",   "零部件"),
    ("电磁制动器",      "机器人链",   "零部件"),
    ("轴承保持架",      "机器人链",   "零部件"),
    ("滚动体",          "机器人链",   "零部件"),
    ("气体储运",        "光储风链",   "氢能与核电"),
    ("环境监测",        "公用事业链", "环保"),
    ("油气工程",        "资源链",     "石油石化"),
    ("船舶与海洋工程",  "军工链",     "船舶海工"),
    ("锂电池循环",      "新能源车链", "电池"),
    ("动力总成",        "新能源车链", "零部件"),
]

VAGUE = ("高端装备制造", "工业产品", "工业产品销售", "销售商品", "整机业务",
         "四大主力产品", "生产线", "智能装备及生产线业务", "销售机器设备和辅助产品",
         "备品配件供应管理", "核心备件", "精密零部件", "机械类产品", "智能仪表",
         "解决方案", "其他", "其他业务")


def build_cfg():
    c = copy.deepcopy(CFG)
    for kw, ch, sg in NEW_KW:
        segs = c["chains"][ch]["环节"]
        if sg not in segs:
            raise SystemExit(f"环节不存在：{ch}·{sg}")
        if kw not in segs[sg]:
            segs[sg].append(kw)
    return c


def make_relaxed():
    """取 main_seg 原文，只改阈值那一行，其余逐字不动。"""
    src = inspect.getsource(SP.main_seg)
    old = "        if r < max(20.0, top * 0.85):\n            continue\n"
    new = ("        if r < max(20.0, top * 0.85) and not (\n"
           "                r >= 25.0 and any(v in ((its[0].get('name') or '')) for v in _VAGUE)):\n"
           "            continue\n")
    if old not in src:
        raise SystemExit("main_seg 的阈值行改了，同步本脚本后再跑")
    ns = dict(SP.__dict__)
    ns["_VAGUE"] = VAGUE
    exec(compile(src.replace(old, new), "<main_seg_relaxed>", "exec"), ns)
    return ns["main_seg"]


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
    C2 = build_cfg()
    relaxed = make_relaxed()
    files = sorted(glob.glob(os.path.join(ROOT, "cache", "profile", "*.json")))

    rows = []
    for f in files:
        p = json.load(open(f, encoding="utf-8"))
        code = p.get("code") or os.path.basename(f)[:-5]
        nm = (pool.get(code) or {}).get("name", "")
        inds = IND.get(code) or []
        b = SP.main_seg(p, CFG["chains"], inds)
        k = SP.main_seg(p, C2["chains"], inds)
        v = relaxed(p, C2["chains"], inds)
        rows.append(dict(code=code, name=nm, inds=inds, items=(p.get("items") or [])[:4],
                         now=b, kw=k, v2=v))

    n0 = sum(1 for r in rows if not r["now"][0])
    n1 = sum(1 for r in rows if not r["kw"][0])
    n2 = sum(1 for r in rows if not r["v2"][0])
    print(f"未归链：现状 {n0} → 只补词 {n1} → 补词+放宽阈值 {n2}")
    print(f"（收益：补词 -{n0 - n1}；再放宽 -{n1 - n2}）\n")

    print("=== A. 只补词就救回 ===")
    for r in rows:
        if not r["now"][0] and r["kw"][0]:
            print(f"  {r['code']} {r['name']:<7} → {r['kw'][0]}·{r['kw'][1]}  [{r['kw'][2]}]")

    print("\n=== B. 补词后仍未归、再放宽阈值才救回 ===")
    for r in rows:
        if not r["kw"][0] and r["v2"][0]:
            its = " | ".join(f"{x.get('name')}{x.get('ratio')}%" for x in r["items"])
            print(f"  {r['code']} {r['name']:<7} → {r['v2'][0]}·{r['v2'][1]}  [{r['v2'][2]}]")
            print(f"      主营: {its}")

    flip = [r for r in rows if r["now"][0] and r["v2"][:2] != r["now"][:2]]
    print(f"\n=== 误伤检查：原本已归好的、被改成别的（{len(flip)} 只）===")
    for r in flip:
        its = " | ".join(f"{x.get('name')}{x.get('ratio')}%" for x in r["items"])
        print(f"  {r['code']} {r['name']:<7} {r['now'][0]}·{r['now'][1]} → {r['v2'][0]}·{r['v2'][1]}")
        print(f"      主营: {its}")

    a = sum(1 for r in rows if r["now"][0] and not r["now"][1])
    b2 = sum(1 for r in rows if r["v2"][0] and not r["v2"][1])
    print(f"\n有链无环节：{a} → {b2}")

    out = os.path.join(ROOT, "cache", "_recover_probe.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"逐只明细：{out}")


if __name__ == "__main__":
    main()
