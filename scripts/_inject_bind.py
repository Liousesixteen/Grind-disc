# -*- coding: utf-8 -*-
"""一次性：往 pool_config.json 的 chains 里注入「_归属绑定」与「_共现门槛」两个下划线段。
下划线段的约定：所有遍历 chains 的代码都已 `if cn.startswith("_"): continue`，因此
把规则挂在 chains 里可以让 deep_match / chain_of 零改签名就拿到，不必改 8 处调用点。
"""
import json
import os
import shutil
import collections

ROOT = "/Users/ccdemac/Finance/AI顶级交易员"
P = os.path.join(ROOT, "scripts", "pool_config.json")
BAK = P + ".bak20261024_bind"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
chains = cfg["chains"]

# ── 归属绑定：同一个词出现在多个环节时的唯一主口径 ─────────────────────
# 判定原则（逐词查过语料再定，不是拍脑袋）：
#   A 链设计重叠类 → 绑到专管该业务的那条链（消费链的服装/养殖本就是重复登记）
#   B 语义真两可类 → 绑到该词最专指的那一侧，另一侧保留近义词不空
BIND = collections.OrderedDict([
    ("服装", "轻工纺服链·品牌服饰"),
    ("家纺", "轻工纺服链·品牌服饰"),
    ("鞋帽", "轻工纺服链·品牌服饰"),
    ("纺织", "轻工纺服链·纺织制造"),
    ("玩具", "轻工纺服链·文娱用品"),
    ("潮玩", "传媒内容链·IP与潮玩"),
    ("医美", "医药链·医美"),
    ("养殖", "农业链·养殖"),
    ("水产", "农业链·养殖"),
    ("饲料", "农业链·养殖"),
    ("种业", "农业链·种业"),
    ("硅料", "光储风链·光伏"),
    ("北斗", "AI算力链·运营与卫星"),
    ("激光雷达", "新能源车链·智能化"),
    ("农药", "农业链·农化"),
    ("系统集成", "软件与数据链·行业IT"),
    ("云计算", "AI算力链·算力服务"),
    ("边缘计算", "AI算力链·算力服务"),
])

# ── 共现门槛：15 个 df>25 的泛词里，只有 5 个真的语义不收敛 ────────────
# 其余 10 个判为「高 df 但语义一致」放行：
#   设备/材料/制造/设计 已在 stock_profile.GENERIC_KW 里（不参与环节判断）
#   软件/营销 命中项名逐条看过，全是软件公司/营销公司，是准的，不能一刀切
#   电池/医药/芯片/医疗 是链级词，本身语义准确
# 门槛语义：项名含该词但**不含任一限定词**时，该词不生效（继续找别的候选）；
#           项名恰好等于该词时视为通过（『租赁』『商业』这类裸项名就是主业本身）。
GATE = collections.OrderedDict([
    ("工程", {"必须含": ["建筑", "施工", "总包", "土木", "市政", "幕墙", "岩土",
                        "水利", "园林", "路桥", "项目建设", "爆破", "工程勘察"]}),
    ("租赁", {"必须含": ["融资", "金融", "飞机", "船舶", "租赁服务", "租赁业务",
                        "设备租赁", "汽车租赁"]}),
    ("电力", {"必须含": ["销售", "发电", "火电", "水电", "供电", "售电", "热电",
                        "生产", "收入", "电厂"]}),
    ("商业", {"必须含": ["零售", "贸易", "百货", "商业经营", "批发", "电商", "商品销售"]}),
    ("食品", {"必须含": ["饮品", "休闲", "保健", "冲饮", "零食", "食品销售", "食品生产",
                        "乳制品", "调味"]}),
])

new = collections.OrderedDict()
new["_归属绑定"] = BIND
new["_共现门槛"] = GATE
for k, v in chains.items():
    new[k] = v
cfg["chains"] = new

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"已注入：归属绑定 {len(BIND)} 条，共现门槛 {len(GATE)} 条")
print("版本号 1.8 →", end=" ")
