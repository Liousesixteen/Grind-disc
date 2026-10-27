"""2026-10-24 Step4：用户拍板后的三项落地。

① 轻工纺服链新增「纺织设备」环节、交运链新增「轨交设备」环节（108 → 110）。
   救回 8 只：泰坦股份/慈星股份/越剑智能/杰克科技（纺机缝纫）＋
   众合科技/康尼机电/铁科轨道/祥和实业（轨交装备）。
   为什么现在敢加：这三只缝纫机/纺纱/横机公司卖的就是纺织产业链的设备，
   轨交四家卖的就是铁路装备——主营项名与环节一一对应，不是硬编。
   卓郎智能仍留空（F10 主营项名只写『销售机器设备和辅助产品』，无信息可依）。
② 主归属候选剔除行业名：申万行业名（一级/二/三级）永远不当题材候选。
   实测缺陷：大鹏工业 主归属=专用设备、四通股份 主归属=其他家居用品——
   都是行业名冒充题材（北交所标签串为空、部分票行业词漏进题材列表）。

用法：.venv/bin/python scripts/_patch_step4.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_P = os.path.join(ROOT, "scripts", "pool_config.json")

NEW_SEG = {
    "轻工纺服链": ("纺织设备", ["纺纱", "织造", "针织横机", "加弹", "经编", "纺织机械", "缝纫"]),
    "交运链": ("轨交设备", ["轨道交通", "轨交", "铁路装备", "扣件", "信号系统", "车辆装备"]),
}


def main():
    cfg = json.load(open(CFG_P, encoding="utf-8"))
    n0 = sum(len(v.get("环节") or {}) for k, v in cfg["chains"].items()
             if not k.startswith("_") and isinstance(v, dict))
    for cn, (sn, kws) in NEW_SEG.items():
        segs = cfg["chains"][cn].setdefault("环节", {})
        if sn in segs:
            print(f"  已存在，跳过：{cn}·{sn}")
            continue
        segs[sn] = kws
        print(f"  新增环节：{cn}·{sn} ← {kws}")
    n1 = sum(len(v.get("环节") or {}) for k, v in cfg["chains"].items()
             if not k.startswith("_") and isinstance(v, dict))
    json.dump(cfg, open(CFG_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"环节总数：{n0} → {n1}")


if __name__ == "__main__":
    main()
