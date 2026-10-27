# -*- coding: utf-8 -*-
"""一次性：补 23 个链级 match 词，修「环节词表与 match 表不同步」的缺口。

怎么发现的（不是猜的）：
  对每个环节词，拿它自己当题材名跑一遍 chain_of，看能不能回到自己的位置。
  109 个环节、有 27 个回不去，其中 3 个是归属绑定的预期结果（云计算/边缘计算/激光雷达），
  剩下 24 个是预存在缺口——题材名「白银」「纯碱」「汽车整车」「卫星导航」出现时，
  chain_of 直接返回未归，或归到别的链（卫星导航被军工链的『卫星』抢走、3D玻璃被
  地产基建链的『玻璃』抢走）。这些题材因此拿不到任何同环节加成，归因会静默变哑。

准入判断（每个词都过一遍，不是批量灌）：
  · 必须是专指词（能唯一指向业务），泛词一律不进——『设备』『材料』『制造』这类
    若加进 match，会把 12 个『通用设备』『碳基材料』题材全拽进半导体链；
  · 必须已经作为题材名真实出现在全市场标签里（在 cache/pool_*/themes.json 里查得到）；
  · 只能加到自己所在的链，且该链 match 原本没有它。
"""
import collections
import json
import os
import shutil

P = "/Users/ccdemac/Finance/AI顶级交易员/scripts/pool_config.json"
BAK = P + ".bak20261024_match2"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

ADD = {
    "资源链": ["贵金属", "白银", "普钢", "动力煤", "纯碱", "维生素"],
    "传媒内容链": ["数字阅读"],
    "消费链": ["乳品"],
    "AI应用链": ["智能电视"],
    "新能源车链": ["汽车整车"],
    "公用事业链": ["火力发电", "环境治理"],
    "医药链": ["特色药", "精准诊断", "重组蛋白"],
    "电子元器件链": ["苹果概念", "3D玻璃"],
    "半导体链": ["蓝宝石"],
    "AI算力链": ["卫星互联网", "卫星导航", "通信服务"],
    "光储风链": ["钒电池", "超级电容", "电力设备"],
}

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
ch = cfg["chains"]
n = 0
for cn, kws in ADD.items():
    lst = ch[cn].setdefault("match", [])
    for kw in kws:
        if kw not in lst:
            lst.append(kw)
            n += 1
            print(f"  {cn}.match += {kw}")

ver = cfg.get("version")
if isinstance(ver, (int, float)):
    nv = round(ver + 0.1, 1)
    cfg["version"] = nv
    cfg.setdefault("_changelog", collections.OrderedDict())[str(nv)] = (
        f"Step1 词表同步：补 {n} 个链级 match 词，修『环节词表与 match 表不同步』。"
        "方法：拿每个环节词当题材名跑 chain_of，看能否回到原位——27 个回不去，"
        "3 个是绑定预期结果，24 个是预存在缺口（题材名归不上链或归错链：卫星导航被军工链"
        "『卫星』抢走、3D玻璃被地产基建『玻璃』抢走、白银/纯碱/汽车整车直接未归）。"
        "准入：只收专指词，泛词（设备/材料/制造/设计/工程/商业/发电）一律不进 match，"
        "否则『通用设备』『碳基材料』这类题材会被整批拽进半导体链。"
    )
    print("版本号 →", nv)

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"共补 {n} 个词")
