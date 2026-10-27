"""2026-10-24 Step3 补丁：直连表回调 3 条 + 新增 统一大市场。

实测发现（重启看板后）：把「物联网」补进 _题材直连 之后，海洋王的主归属从军工翻到了物联网——
主归属规则是「能归链的题材里，涨停家数最多者」（prefer_chain，pankou._main_of）：
  改前：海洋王的标签里只有 军工(8) 和 核能核电 能归链 → 主归属=军工 ✓
  改后：物联网(11) 也能归链了，家数压过军工 → 主归属=物联网 ✗（6板空间龙成了泛概念桶的龙头）

教训：_题材直连 不只是「给归因补链」，它会**改变主归属的竞争格局**。泛生态/平台家族标签
（物联网/智慧灯杆/ETC——成分横跨所有链、挂了很多年）不配参与主归属竞争，必须从直连表拿掉，
并在归因候选里剔除（exclude_words 只影响 load_tag_map 的概念标签，不动股池静态题材列表，
所以主归属/梯队/龙头完全不受影响——这正是两层分离的意义）。

用法：.venv/bin/python scripts/_patch_theme_direct2.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_P = os.path.join(ROOT, "scripts", "pool_config.json")

REMOVE = ["物联网", "智慧灯杆", "ETC"]          # 泛生态家族：从直连表撤下
ADD_EXCLUDE = ["物联网", "智慧灯杆", "ETC"]      # 同时从归因候选剔除
ADD_DIRECT = {
    "统一大市场": "交运链·物流",                 # 密尔克卫：归因=统一大市场，题材未归链
}


def main():
    cfg = json.load(open(CFG_P, encoding="utf-8"))
    chains = cfg["chains"]
    direct = chains.get("_题材直连") or {}
    removed = []
    for t in REMOVE:
        if t in direct:
            removed.append(f"{t}（原 {direct.pop(t)}）")
    direct["_泛生态豁免_20261024"] = ("物联网/智慧灯杆/ETC 不进直连表：直连会让它们获得 prefer_chain 资格、"
                                   "参与主归属竞争——实测把海洋王的主归属从军工(8家)翻成了物联网(11家)。"
                                   "这类挂了很多年的泛生态标签只许在归因候选里被剔除，不许拿主归属。")
    for t, loc in ADD_DIRECT.items():
        if t not in direct:
            direct[t] = loc
    chains["_题材直连"] = direct

    g = cfg["granularity"]
    ex = g.get("exclude_words") or []
    added = [w for w in ADD_EXCLUDE if w not in ex]
    g["exclude_words"] = ex + added

    json.dump(cfg, open(CFG_P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("直连表撤下：", "、".join(removed) if removed else "（无）")
    print("直连表新增：", "、".join(f"{t} → {v}" for t, v in ADD_DIRECT.items()))
    print("归因候选剔除：", "、".join(added) if added else "（无）")
    print("直连表总条数：", len([k for k in direct if not k.startswith('_')]))


if __name__ == "__main__":
    main()
