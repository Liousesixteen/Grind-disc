# -*- coding: utf-8 -*-
"""一次性：给链级 match 补 3 个词，救回被绑定「架空」的题材归属。

起因（chain_of 实测）：
  硅料    → 未归（不在任何链的 match 里，题材名「硅料」根本归不上链）
  北斗    → 未归（同上）
  激光雷达 → 军工链·军工电子（漏到军工链的『雷达』！）
           原因：绑定把『激光雷达』指向 新能源车链·智能化，但新能源车链的 match
           里没有这个词，只有环节里有 → chain_of 要求 match 命中才进链 → 目标链被 skip，
           机器人链的原位置又被绑定挡掉 → 两边失效 → 掉给军工链的『雷达』。

教训：**绑定的目标链必须在链级 match 里有这个词**，否则绑定等于把这个词从题材层删掉。
deep_match 是"先扫环节再扫链"，不受影响；chain_of 是"先扫链再扫环节"，必须有 match。
"""
import collections
import json
import os
import shutil

P = "/Users/ccdemac/Finance/AI顶级交易员/scripts/pool_config.json"
BAK = P + ".bak20261024_match"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
ch = cfg["chains"]

ADD = [("光储风链", "硅料"), ("AI算力链", "北斗"), ("新能源车链", "激光雷达")]
for cn, kw in ADD:
    lst = ch[cn].setdefault("match", [])
    if kw not in lst:
        lst.append(kw)
        print(f"  {cn}.match += {kw}")

ver = cfg.get("version")
if isinstance(ver, (int, float)):
    nv = round(ver + 0.1, 1)
    cfg["version"] = nv
    cfg.setdefault("_changelog", collections.OrderedDict())[str(nv)] = (
        "Step1 修补（chain_of 实测驱动）：给链级 match 补 硅料→光储风链 / 北斗→AI算力链 / "
        "激光雷达→新能源车链。发现两类问题：①硅料、北斗 本来就不在任何链的 match 里，"
        "作为题材名归不上链（预存在缺口）；②激光雷达 被绑定指向新能源车链·智能化，"
        "但该链 match 无此词 → chain_of 两条路都被堵 → 掉给军工链的『雷达』。"
        "结论：绑定的目标链必须在 match 里有该词，否则绑定等于把词从题材层删掉。"
    )
    print("版本号 →", nv)

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
