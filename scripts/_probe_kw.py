"""候选关键词的「误伤体检」：加一个词之前，先看它会把谁一起拽进来。

做法：把每个候选词挂到目标（链,环节）上，然后扫**全池 1037 只的全部主营项**
（不只最大项），列出所有会被这个词命中、但当前归属不同的票。
误伤为 0 或可解释的，才允许进池。

用法：.venv/bin/python scripts/_probe_kw.py
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

CAND = [
    ("汽车齿轮",   "新能源车链", "零部件"),
    ("媒体代理",   "传媒内容链", "广告营销"),
    ("新能源汽车", "新能源车链", ""),
    ("线性驱动",   "机器人链",   "零部件"),
    ("电磁制动器", "机器人链",   "零部件"),
    ("轴承保持架", "机器人链",   "零部件"),
    ("滚动体",     "机器人链",   "零部件"),
    ("纺纱设备",   "轻工纺服链", "纺织设备"),
    ("针织横机",   "轻工纺服链", "纺织设备"),
    ("加弹",       "轻工纺服链", "纺织设备"),
    ("气体储运",   "光储风链",   "氢能"),
    ("环境监测",   "公用事业链", "环境监测"),
    ("油气工程",   "资源链",     "石油石化"),
    ("船舶与海洋工程", "军工链", "船舶海工"),
    ("锂电池循环", "新能源车链", "电池"),
]


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
    profs = []
    for f in files:
        p = json.load(open(f, encoding="utf-8"))
        code = p.get("code") or os.path.basename(f)[:-5]
        ch, sg, ev = SP.main_seg(p, CHAINS, IND.get(code) or [])
        profs.append((code, (pool.get(code) or {}).get("name", ""), p, ch, sg, ev))

    print(f"体检池：{len(profs)} 只（有 F10 画像）\n")
    for kw, tgt_c, tgt_s in CAND:
        hit = []
        for code, name, p, ch, sg, ev in profs:
            for it in (p.get("items") or []):
                nm = it.get("name") or ""
                if kw in nm:
                    hit.append((code, name, nm, it.get("ratio"), ch or "—", sg or "—"))
                    break
        new = [h for h in hit if h[4] == "—"]                     # 当前未归链 → 加了词就能归
        other = [h for h in hit if h[4] != "—" and h[4] != tgt_c]  # 已归别的链 → 潜在冲突
        same = [h for h in hit if h[4] == tgt_c]
        print(f"── 加「{kw}」→ {tgt_c}{('·' + tgt_s) if tgt_s else ''}")
        print(f"    命中 {len(hit)} 只 ｜ 现未归链 {len(new)}（净收益）｜ 已在同链 {len(same)} ｜ 归到别链 {len(other)}（冲突）")
        for h in new[:6]:
            print(f"      + {h[0]} {h[1]:<7} {h[2][:18]:<18}{h[3]}%")
        for h in other[:6]:
            print(f"      ! {h[0]} {h[1]:<7} {h[2][:18]:<18}{h[3]}%  现属 {h[4]}·{h[5]}")
        print()

    # 额外：林州重机「煤炭机械」为什么没进资源链
    print("── 排障：林州重机「煤炭机械」")
    for code, name, p, ch, sg, ev in profs:
        if name.startswith("林州"):
            for it in (p.get("items") or []):
                print("   项:", it.get("name"), it.get("ratio"), "→", SP.deep_match(it.get("name") or "", CHAINS))
            print("   现判:", ch, sg, ev)


if __name__ == "__main__":
    main()
