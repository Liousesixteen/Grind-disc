# -*- coding: utf-8 -*-
"""一次性：处理被归属绑定「掏空」的 3 个环节。

自检报出 3 个环节的词全部被绑定挪走、已经名存实亡。逐个用 chain_of 抽查后分两类处理：
  · 消费链·服装家纺：4 个词（服装/家纺/纺织/鞋帽）与 轻工纺服链·品牌服饰·纺织制造
    完全重复登记，链的设计上就是同一件事。**删掉**——留着空壳只会让后来的人以为
    消费链还管服装。
  · 软件与数据链·云与SaaS、机器人链·应用集成：锚词（云计算/边缘计算、系统集成）被绑到
    更专指的一侧，剩下的词在题材层仍然活跃（SaaS/物流机器人/焊接机器人）。**保留**，
    但把理由写进配置，否则下次自检又报、又得重新判断一遍。
"""
import collections
import json
import os
import shutil

P = "/Users/ccdemac/Finance/AI顶级交易员/scripts/pool_config.json"
BAK = P + ".bak20261024_hollow"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
ch = cfg["chains"]

# ① 删除冗余环节
dead = ch["消费链"]["环节"].pop("服装家纺", None)
print(f"  删除 消费链·服装家纺（原词表 {dead}）—— 4 个词全部与轻工纺服链重复登记，"
      "票已全部改归 品牌服饰/纺织制造")

# ② 保留 + 写明理由
ch["_环节保留"] = collections.OrderedDict([
    ("软件与数据链·云与SaaS",
     "锚词 云计算/边缘计算 已绑定到 AI算力链·算力服务（基础设施属性更强）；"
     "本环节仍有 SaaS 一词在题材层生效，云与SaaS 的语义边界（应用层云服务）与"
     "算力服务不同，不应合并"),
    ("机器人链·应用集成",
     "锚词 系统集成 已绑定到 软件与数据链·行业IT（A股叫『系统集成』的业务几乎都是IT服务）；"
     "本环节仍有 物流机器人/焊接机器人 在题材层生效，对应机器人产线集成的独立业务"),
])

ver = cfg.get("version")
if isinstance(ver, (int, float)):
    nv = round(ver + 0.1, 1)
    cfg["version"] = nv
    cfg.setdefault("_changelog", collections.OrderedDict())[str(nv)] = (
        "Step1 收尾：①删除 消费链·服装家纺（4 词全部与轻工纺服链重复登记，绑定后已成空壳）；"
        "②新增 _环节保留 段，记录 云与SaaS / 应用集成 两个环节锚词被绑定挪走但仍保留的理由，"
        "避免每次自检重复报警、重复判断。环节总数 109 → 108。"
    )
    print("版本号 →", nv)

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
n = sum(len(v.get("环节") or {}) for k, v in ch.items() if not k.startswith("_"))
print(f"环节总数 → {n}")
