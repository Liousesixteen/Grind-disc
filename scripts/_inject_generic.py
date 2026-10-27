# -*- coding: utf-8 -*-
"""一次性：往 chains 里补 `_泛词不参与环节` 与 `_泛词放行`。

为什么需要这两段：
  audit 报「df>25 泛词」时并不知道哪些词本来就不参与环节判断（GENERIC_KW），
  也不知道哪些词虽然 df 高但语义是收敛的（软件/营销，逐条核过项名）。
  把这两类判断也写进配置，自检才能区分「真风险」和「高 df 但没问题」，
  否则黄框永远清不零，人就会开始无视黄框——那比没有自检更糟。
"""
import collections
import json
import os
import shutil

P = "/Users/ccdemac/Finance/AI顶级交易员/scripts/pool_config.json"
BAK = P + ".bak20261024_generic"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
chains = cfg["chains"]

GENERIC = ["设备", "材料", "应用", "服务", "设计", "制造", "测试", "存储", "功率",
           "控制", "感知", "本体", "零部件", "化工", "煤炭"]
PASS = collections.OrderedDict([
    ("软件", "41 条项名逐条核过：政务软件/EDA软件/工业自动化软件/应用软件服务…全是软件主业，语义收敛"),
    ("营销", "11 条全是数字营销/整合营销/品牌营销/红人营销…语义一致，df 高只因写法五花八门"),
])

out = collections.OrderedDict()
for k, v in chains.items():
    out[k] = v
out["_泛词不参与环节"] = GENERIC
out["_泛词放行"] = PASS
cfg["chains"] = out

cf_ver = cfg.get("version")
if isinstance(cf_ver, (int, float)):
    newver = round(cf_ver + 0.1, 1)
    cfg["version"] = newver
    cfg.setdefault("_changelog", collections.OrderedDict())[str(newver)] = (
        "身份识别 Step1：chains 新增 4 个下划线段——_归属绑定（18 个跨环节重复词的唯一主口径，"
        "消除『归属由 JSON 书写顺序决定』的不可复现）、_共现门槛（工程/租赁/电力/商业/食品 5 个"
        "df>25 泛词必须与限定词共现才生效）、_泛词不参与环节（原 GENERIC_KW 配置化）、"
        "_泛词放行（软件/营销 等高 df 但语义收敛的词，留理由备查）。"
        "配套 scripts/audit_config.py 词表自检；改词表后须 --strict 退出 0。"
    )
    print("版本号 →", newver)

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"已注入：泛词不参与环节 {len(GENERIC)} 个，泛词放行 {len(PASS)} 个")
