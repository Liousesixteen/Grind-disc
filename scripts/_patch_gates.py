# -*- coding: utf-8 -*-
"""一次性：根据全量 diff 暴露的 3 只退化票，修补门槛与会环节词。

diff 结果（旧规则 vs 新规则，38 只变化）里只有 3 只是被新规则弄坏的，逐只核过主营项名：
  603968 醋化股份  食品饲料添加剂28%/医药农药中间体20%/农化类16% → 精细化工，
           被『饲料』拐到 农业链·养殖（原答案是 消费·休闲食品，也不对，但更不离谱）
  600318 新力金融  德润租赁70%/德善小贷20% → 被『租赁』白名单挡住，掉回金融链级（原答案对）
  301390 经纬股份  电力工程建设42%/电力咨询设计27%/电力设备销售21% → 被『工程』门槛挡住，
           掉到公用事业链级（原答案 地产基建·建筑施工）
另修 002242 九阳（营养煲37%/食品加工机34%/西式电器22%）：门槛纠正后落到消费链级，
  消费链本来就有『小家电与清洁』环节，缺的是具体词。
"""
import collections
import json
import os
import shutil

P = "/Users/ccdemac/Finance/AI顶级交易员/scripts/pool_config.json"
BAK = P + ".bak20261024_gate2"
if not os.path.exists(BAK):
    shutil.copy2(P, BAK)
    print("备份 →", os.path.basename(BAK))

cfg = json.load(open(P, encoding="utf-8"), object_pairs_hook=collections.OrderedDict)
chains = cfg["chains"]
gates = chains["_共现门槛"]

# ① 工程：补『工程建设』——电力工程建设/市政工程建设 这类确实是施工
if "工程建设" not in gates["工程"]["必须含"]:
    gates["工程"]["必须含"].append("工程建设")

# ② 租赁：白名单改成黑名单。租赁公司大多就叫『XX租赁』（德润租赁），白名单会误伤正主；
#    真正该挡的是不动产/设备服务类（商铺租赁、施工机械租赁、房地产租赁及物业）。
gates["租赁"] = collections.OrderedDict([("除非含", [
    "商铺", "房屋", "房地产", "经营场所", "场地", "厂房", "物业",
    "机械", "施工", "车辆", "柜台", "柜台出租",
])])

# ③ 饲料：补门槛。『食品饲料添加剂』含『饲料』但它是化工品，不是养殖。
if "饲料" not in gates:
    gates["饲料"] = collections.OrderedDict([("必须含", [
        "畜禽", "水产", "配合", "预混", "销售", "生产", "加工", "养殖", "蛋白",
    ])])

# ④ 补词条：小家电的具体品类名（df=1，但服务的是关键票——准入标准里允许）
seg = chains["消费链"]["环节"]["小家电与清洁"]
for w in ["食品加工机", "营养煲", "西式电器", "炊具", "厨房电器"]:
    if w not in seg:
        seg.append(w)

ver = cfg.get("version")
if isinstance(ver, (int, float)):
    nv = round(ver + 0.1, 1)
    cfg["version"] = nv
    cfg.setdefault("_changelog", collections.OrderedDict())[str(nv)] = (
        "Step1 修补（全量 diff 驱动）：①工程门槛补『工程建设』，救回 301390 经纬股份（电力工程建设）；"
        "②租赁门槛改『除非含』黑名单，救回 600318 新力金融（德润租赁 70%）——白名单会误伤『XX租赁』式正主；"
        "③新增饲料门槛，修 603968 醋化股份（食品饲料添加剂被『饲料』拐到养殖）；"
        "④消费链·小家电与清洁 补 食品加工机/营养煲/西式电器/炊具/厨房电器，修 002242 九阳。"
        "diff 全量 38 只变化，逐只核过，无其他退化。"
    )
    print("版本号 →", nv)

json.dump(cfg, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("已修补：工程/租赁/饲料 门槛 + 小家电 5 个词条")
