"""审查 `_行业定环节`：每条映射里，有多少票的**主营项名**支持这个环节。只读。

判据：某只票被行业表判进「链·环节」后，看它的主营项名里有没有出现**该环节的关键词**。
  support=0 —— 环节完全是行业表硬塞的，主营项名一点支撑都没有 → 最可疑
  support>0 —— 主营项名至少沾了一点边

不是"support=0 就一定错"（有些票的项名就是笼统），但 support=0 且只数多的，
大概率是把一个宽行业塞进了一个窄环节。
"""
import glob
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import pool_pipeline as P  # noqa: E402
import stock_profile as SP  # noqa: E402
import theme_attr as TA  # noqa: E402

cfg = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
C = cfg["chains"]
_, IND, _ = TA.load_tag_map(os.path.join(ROOT, "cache", "pool_20261023"), cfg, P.classify_tags)

rows = defaultdict(list)      # (键, 目标) → [(code, 是否支持, 主营前两项)]
for f in glob.glob(os.path.join(ROOT, "cache", "profile", "*.json")):
    p = json.load(open(f, encoding="utf-8"))
    c = p.get("code")
    ch, sg, ev = SP.main_seg(p, C, IND.get(c, []))
    if "+行业:" in ev:
        key = ev.split("+行业:", 1)[1]
    elif ev.startswith("行业:"):
        key = ev[3:]
    else:
        continue
    kws = (C.get(ch, {}).get("环节") or {}).get(sg) or []
    names = " ".join((x.get("name") or "") for x in (p.get("items") or []))
    ok = any(k and k in names for k in kws)
    rows[(key, f"{ch}·{sg}")].append((c, ok, names[:46]))

risk = [(k, t, v) for (k, t), v in rows.items() if not any(o for _, o, _ in v)]
print(f"行业表生效映射 {len(rows)} 条，其中 **主营项名零支撑** 的 {len(risk)} 条\n")
for key, tgt, v in sorted(risk, key=lambda x: -len(x[2])):
    print(f"[{key}] → {tgt}  {len(v)} 只（项名全无环节词支撑）")
    for c, _, n in v[:5]:
        print(f"    {c} {n}")
