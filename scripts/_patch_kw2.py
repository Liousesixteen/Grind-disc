"""2026-10-24 Step2：把「主营项名认不出」的 5 个词补进环节词表 + 3 只人工 override。

依据（全部来自 `scripts/_probe_kw.py` 的全池 1037 只误伤体检）：
  每条候选词命中数 → 净收益（现未归链）/ 冲突（已归别的链）。下列 5 条**冲突 0**。

  汽车齿轮    → 新能源车链·零部件   命中 2（1 净收益 1 已在同链）
  动力总成    → 新能源车链·零部件   覆盖华依科技「新能源汽车动力总成测试服务」
  媒体代理    → 传媒内容链·广告营销 命中 1（净收益 1）
  气体储运    → 光储风链·氢能与核电 命中 1（净收益 1）
  环境监测    → 公用事业链·环保     命中 1（净收益 1）

不补的（有意留空，理由同「机械设备→机器人链」）：
  轴承保持架 / 滚动体 / 电磁制动器 → 机器人链·零部件
    —— 这三样是「给机器人供应链上游供货」，不是「这条链里的东西」。补进去等于
       把工业轴承厂、制动器厂整批算成机器人票，就是刚删掉的那类错误的近亲。

用法：.venv/bin/python scripts/_patch_kw2.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_P = os.path.join(ROOT, "scripts", "pool_config.json")
OV_P = os.path.join(ROOT, "scripts", "identity_override.json")

KW = [
    ("汽车齿轮", "新能源车链", "零部件"),
    ("动力总成", "新能源车链", "零部件"),
    ("媒体代理", "传媒内容链", "广告营销"),
    ("气体储运", "光储风链", "氢能与核电"),
    ("环境监测", "公用事业链", "环保"),
]

OV = {
    "002353": {
        "name": "杰瑞股份", "chain": "资源链", "seg": "石油石化",
        "reason": "高端装备制造54.8%+油气工程及技术服务32.5%+油气田开发3.9%——油服，不是通用机械。"
                  "最大项『高端装备制造』是泛词，把有信息的第二项(32.5%)挡在 top×85% 门槛外；"
                  "19链里油气工程正是资源链·石油石化",
        "by": "人工", "conf": 0.9, "asof": "2026-10-24",
    },
    "002483": {
        "name": "润邦股份", "chain": "军工链", "seg": "船舶海工",
        "reason": "物料起重搬运装备54.6%+船舶与海洋工程装备25.0%+环保14.5%；"
                  "船舶海工是19链里的明确环节，被最大项的 85% 门槛挡住",
        "by": "人工", "conf": 0.8, "asof": "2026-10-24",
    },
    "002009": {
        "name": "天奇股份", "chain": "新能源车链", "seg": "电池",
        "reason": "汽车智能装备47.0%+锂电池循环业务36.7%（电池回收/梯次利用）；"
                  "锂电池循环被 85% 门槛挡在门外，而它是这条链的核心业务而非副业",
        "by": "人工", "conf": 0.85, "asof": "2026-10-24",
    },
}


def main():
    cfg = json.load(open(CFG_P, encoding="utf-8"))
    added = []
    for kw, ch, sg in KW:
        segs = cfg["chains"][ch]["环节"]
        if sg not in segs:
            raise SystemExit(f"环节不存在：{ch}·{sg}")
        if kw in segs[sg]:
            print(f"  已存在，跳过：{ch}·{sg} ← {kw}")
            continue
        segs[sg].append(kw)
        added.append(f"{ch}·{sg} ← {kw}")
    json.dump(cfg, open(CFG_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    ov = json.load(open(OV_P, encoding="utf-8"))
    n_ov = 0
    for code, rec in OV.items():
        if code in ov:
            print(f"  override 已存在，跳过：{code} {rec['name']}")
            continue
        ov[code] = rec
        n_ov += 1
    json.dump(ov, open(OV_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"补词 {len(added)} 条：")
    for a in added:
        print("   ", a)
    print(f"新增 override {n_ov} 条：{'、'.join(r['name'] for r in OV.values())}")
    tot = sum(len(v) for v in cfg["chains"].values() if isinstance(v, dict)
              for k, v in (v.get("环节") or {}).items())
    print(f"环节词总数：{tot}")


if __name__ == "__main__":
    main()
