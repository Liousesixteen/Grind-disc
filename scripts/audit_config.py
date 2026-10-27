#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""词表自检器（身份识别「三层架构」第 1 层的守门工具）

做什么：
  在**不改配置**的前提下，把 `pool_config.json` 里四类结构性问题一次性摊出来，
  并对「归属绑定」「共现门槛」这两类新机制做一致性校验。

     ERROR  —— 配置自身矛盾（绑定指向不存在的链/环节、绑了词表里没有的词），必须修。
     WARN   —— 会导致归属不可复现或明显跑偏（歧义词未绑定、df 超阈泛词、孤儿长词），应修。
     INFO   —— 只做提示（孤儿词、环节覆盖率），不必每轮处理。

四张核心表：
  A 跨环节歧义词  同一个词出现在 ≥2 个「环节」里 → 归属由 JSON 书写顺序决定，不可复现
  B df 超阈泛词   在主营项名语料里扩散过广的词 → 单独一句话不能证明身份
  C 语义倒挂      len 长但 df 极低（看着具体只服务一两家）／len 短但 df 极高（看着具体实为泛词）
  D 孤儿词        词表里有、语料里一次都没命中 → 只增加维护成本，不产生任何判断

用法：
  python3 scripts/audit_config.py              # 全表
  python3 scripts/audit_config.py --brief      # 只打黄框/红框，不打明细表
  python3 scripts/audit_config.py --strict     # 有 ERROR 或未绑定歧义词 → 退出码 1
  python3 scripts/audit_config.py --word 设备  # 查某个词的全部命中项名
  python3 scripts/audit_config.py --chain 机器人链

退出码：0 = 无 ERROR（--strict 下还要求 0 个未绑定歧义词）；1 = 存在 ERROR。

约定：改任何环节词表/归属绑定后必须跑本脚本，`--strict` 退出 0 才算改完。
      df 基准语料 = cache/profile/ 下的 F10 主营构成（增量缓存，越跑越准）。
"""

import argparse
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "scripts", "pool_config.json")
PROF_DIR = os.path.join(ROOT, "cache", "profile")

# 规则本体必须与执行层共用同一份，否则自检说通过、执行却是另一套。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kwrule import bind_ok, gate_ok, chain_of  # noqa: E402

DF_GENERIC = 25     # B 表阈值：语料命中票数超过此值即视为泛词
DF_ORPHAN = 0       # D 表：一次都没命中
DF_NARROW = 1       # C 表：长词但只服务 1 条，看着具体实则个案

ERR, WARN, INFO = [], [], []


def any_live(kw, cn, sn, bind, gates, corpus):
    """该词在该位置是否还能真的生效：绑定没把它挪走，且至少有一个项名过得了门槛。"""
    if not bind_ok(kw, cn, sn, bind):
        return False
    if kw not in (gates or {}):
        return True
    return any(gate_ok(nm, kw, gates) for nm in corpus if kw in nm)


def say(level, msg):
    tag = {"E": "[红]", "W": "[黄]", "I": "[·]"}[level]
    print(f"{tag} {msg}")
    {"E": ERR, "W": WARN, "I": INFO}[level].append(msg)


def load_cfg(path=CFG):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def iter_kw(chains):
    """产出 (词, 链, 环节)；环节为 '<链级>' 表示只出现在链的 match 里。"""
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        for kw in cv.get("match", []):
            if kw:
                yield kw, cn, "<链级>"
        for sn, kws in (cv.get("环节") or {}).items():
            for kw in kws:
                if kw:
                    yield kw, cn, sn


def load_corpus(prof_dir=PROF_DIR):
    """主营构成语料 → (全部项名 Counter, 主干项名 Counter, 票数)。

    主干项名＝真正撑起收入的项（占比 ≥20% 且 ≥最大项 85%），与 stock_profile.main_seg
    的取舍口径一致——只算主干项，才不会把 3% 的副业项当成身份依据。
    """
    allc, mainc, n = collections.Counter(), collections.Counter(), 0
    for fp in glob.glob(os.path.join(prof_dir, "*.json")):
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        its = [x for x in (d.get("items") or []) if (x.get("ratio") or 0) > 0]
        if not its:
            continue
        n += 1
        top = max((x.get("ratio") or 0) for x in its)
        for it in its:
            nm = (it.get("name") or "").strip()
            if not nm:
                continue
            allc[nm] += 1
            r = it.get("ratio") or 0
            if r >= max(20.0, top * 0.85):
                mainc[nm] += 1
    return allc, mainc, n


def df_of(kw, counter):
    return sum(v for nm, v in counter.items() if kw in nm)


def load_themes():
    """全市场题材名（从最近的 cache/pool_*/themes.json 里提取）。

    标签串的固定结构：前 3 个是申万一级/二级/三级，其后第一个含『板块』的词是地域板块，
    再往后才是概念与风格标签。这里只要概念部分的词表，用来判断
    「某个环节词在真实题材名里到底出没出现过」——没出现过的词，它归不上链也无所谓。
    """
    dirs = sorted(glob.glob(os.path.join(ROOT, "cache", "pool_*")), reverse=True)
    # 优先正式交易日目录（pool_YYYYMMDD）；盘中快照（pool_YYYYMMDD_intraday）下划线更多，
    # 排到后面兜底——它的标签串口径与正式目录不同，不该默认用它。
    dirs.sort(key=lambda d: os.path.basename(d).count("_"))
    for d in dirs:
        p = os.path.join(d, "themes.json")
        if not os.path.exists(p):
            continue
        try:
            raw = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        out = set()
        for v in raw.values():
            if not v:
                continue
            toks = [x for x in v.split() if x]
            idx = next((i for i, t in enumerate(toks) if "板块" in t), 0)
            out.update(toks[idx + 1:])
        return out, os.path.basename(d)
    return set(), ""


def build_index(cfg):
    """词 → 出现位置集合；用于判定歧义。"""
    idx = collections.defaultdict(set)
    for kw, cn, sn in iter_kw(cfg["chains"]):
        idx[kw].add((cn, sn))
    return idx


def ambiguous(cfg, idx):
    """A 表：同一个词出现在 ≥2 个不同「环节」里。

    只出现 「链级 + 自己那条链的环节」不算歧义（那是有意为之，帮助链级兜底）。
    """
    out = {}
    for kw, locs in idx.items():
        segs = {sn for _, sn in locs if sn != "<链级>"}
        if len(segs) > 1:
            out[kw] = sorted(locs)
    return out


def _ind2chain():
    """从 stock_profile.py 里取出 IND2CHAIN（不 import——那个模块要 requests）。

    `audit_config.py` 的设计前提是「系统 python3 也能跑」，所以这里用 ast 静态解析
    字面量字典，而不是 import。取不到就返回空表，不影响其他检查。
    """
    import ast
    p = os.path.join(ROOT, "scripts", "stock_profile.py")
    try:
        tree = ast.parse(open(p, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "IND2CHAIN" for t in node.targets):
            continue
        try:
            v = ast.literal_eval(node.value)
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}
    return {}


def _industry_universe():
    """全市场出现过的行业名集合（取自最新一期 pool_*/themes.json 的「所属板块」串）。

    用途：判断 industry_to_chain 的键**到底有没有机会被查到**。查表是精确匹配
    （main_seg → `if ind in m`），所以键名多一个 Ⅱ/Ⅲ 后缀、或少一个，就永远不命中。
    这里只取「前 3 个词」——theme_attr.load_tag_map 的同一口径（申万一二三级）。
    """
    dirs = sorted(glob.glob(os.path.join(ROOT, "cache", "pool_*")), reverse=True)
    for d in dirs:
        p = os.path.join(d, "themes.json")
        if not os.path.exists(p):
            continue
        try:
            th = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        uni = set()
        for s in th.values():
            if isinstance(s, str):
                uni.update(s.split()[:3])
        return uni
    return set()


def _override():
    """逐票 override（人工核实过的答案）。同样不 import stock_profile。"""
    p = os.path.join(ROOT, "scripts", "identity_override.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, dict)}


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--brief", action="store_true", help="只打黄框红框，不打明细表")
    ap.add_argument("--strict", action="store_true", help="未绑定歧义词也判失败")
    ap.add_argument("--word", default="", help="查单个词命中哪些主营项名")
    ap.add_argument("--chain", default="", help="只看某条链")
    ap.add_argument("--cfg", default=CFG)
    a = ap.parse_args()

    cfg = load_cfg(a.cfg)
    chains = cfg["chains"]
    # 两个新机制优先从 chains 的下划线段读（那里所有遍历都会自动跳过），
    # 也兼容放在顶层的老写法。
    binding = cfg.get("归属绑定") or (chains.get("_归属绑定") or {})
    cooc = cfg.get("共现门槛") or (chains.get("_共现门槛") or {})
    idx = build_index(cfg)
    allc, mainc, nstock = load_corpus()
    nitem = sum(allc.values())

    print("=" * 68)
    print(f"词表自检  config={os.path.relpath(a.cfg, ROOT)}")
    print(f"语料基准  {nstock} 只票 / {nitem} 条主营项名 / {len(allc)} 种写法")
    nchain = sum(1 for k in chains if not k.startswith("_"))
    nseg = sum(len(v.get("环节") or {}) for k, v in chains.items() if not k.startswith("_"))
    print(f"词表规模  {nchain} 链 / {nseg} 环节 / {len(idx)} 个不同词条")
    print(f"新机制    归属绑定 {len(binding)} 条 / 共现门槛 {len(cooc)} 条")
    print("=" * 68)

    # ---------- 单词语料查询 ----------
    if a.word:
        w = a.word
        print(f"\n【查词】{w}  全语料 df={df_of(w, allc)}  主干项 df={df_of(w, mainc)}")
        print(f"        位置 {sorted(idx.get(w, []))}")
        hits = [(v, nm) for nm, v in allc.items() if w in nm]
        hits.sort(reverse=True)
        for v, nm in hits[:30]:
            print(f"        {v:3d}  {nm}")
        if not hits:
            print("        （语料里没有命中）")
        return 0

    # ---------- ERROR：绑定指向不存在的目标 ----------
    print("\n—— 绑定表一致性 ——")
    for kw, tgt in binding.items():
        if kw not in idx:
            say("E", f"归属绑定「{kw}」绑了一个词表里不存在的词（绑定无效）")
            continue
        if tgt.count("·") != 1:
            say("E", f"归属绑定「{kw}」目标格式应为「链·环节」，实际是「{tgt}」")
            continue
        cn, sn = tgt.split("·")
        if cn not in chains or cn.startswith("_"):
            say("E", f"归属绑定「{kw}」指向不存在的链「{cn}」")
            continue
        if sn not in (chains[cn].get("环节") or {}):
            say("E", f"归属绑定「{kw}」指向「{cn}」下不存在的环节「{sn}」")
    for kw in cooc:
        if kw not in idx:
            say("E", f"共现门槛「{kw}」不在任何词表里（门槛无对象）")
    # 行业表：写错环节名的代价是整批票跨链（一个三级名常挂 10-30 只），必须校验
    segmap = chains.get("_行业定环节") or {}
    for ind, tgt in segmap.items():
        if "·" not in str(tgt):
            say("E", f"行业定环节「{ind}」目标格式应为「链·环节」，实际「{tgt}」")
            continue
        cn, sn = str(tgt).split("·", 1)
        if cn not in chains or cn.startswith("_"):
            say("E", f"行业定环节「{ind}」指向不存在的链「{cn}」")
        elif sn not in (chains[cn].get("环节") or {}):
            say("E", f"行业定环节「{ind}」指向「{cn}」下不存在的环节「{sn}」")
    # 行业 → 链的两张兜底表：目标链必须真实存在。
    # 反例（2026-10-24 实测）：stock_profile.IND2CHAIN 把「商贸零售/社会服务」映射到
    # 「商贸社服链」——19 条链里没有这条，票被静默写进空链，错位度算不出来也不报错。
    real = {k for k in chains if not k.startswith("_")}
    ghost = {}
    for name, tbl in (("industry_to_chain", cfg.get("industry_to_chain") or {}),
                      ("stock_profile.IND2CHAIN", _ind2chain())):
        for src, tgt in tbl.items():
            if tgt and tgt not in real:
                ghost.setdefault(tgt, []).append(f"{name}:{src}")
    for tgt, srcs in ghost.items():
        say("E", f"行业兜底指向不存在的链「{tgt}」← " + "、".join(srcs[:6]))
    # 从未命中的行业兜底键：留着不报错，但它是**没被检验过的规则**，不能当成生效中的兜底。
    # 2026-10-24 实证：industry_to_chain 里 23/54 个键命中数为 0（『通用设备』『专用设备』
    # 写成不带 Ⅱ 的名字，而申万二级叫『通用设备Ⅱ』）——它们从未生效，自检却一路绿灯。
    # 反例更要命：删掉排在它们前面的『机械设备→机器人链』后，这些键第一次生效就吐出 61 只错票。
    uni = _industry_universe()
    dead = []
    if uni:
        for src in (cfg.get("industry_to_chain") or {}):
            if src not in uni:
                dead.append(src)
        if dead:
            say("I", f"行业兜底 {len(dead)}/{len(cfg.get('industry_to_chain') or {})} 个键"
                     f"在行业名里从未出现（＝从未生效）：{'、'.join(dead[:12])}"
                     f"{'…' if len(dead) > 12 else ''}")
    # 逐票 override：写错链/环节名的代价是那一只票永远错，且不会被任何规则纠正
    ov = _override()
    for code, o in ov.items():
        cn, sn = o.get("chain") or "", o.get("seg") or ""
        if not cn and not sn:
            continue                       # 已核实「不归链」，合法
        if cn not in real:
            say("E", f"override {code} 指向不存在的链「{cn}」")
        elif sn and sn not in (chains[cn].get("环节") or {}):
            say("E", f"override {code} 指向「{cn}」下不存在的环节「{sn}」")
        if not o.get("reason"):
            say("W", f"override {code} 没写 reason（无法复核依据）")
        if float(o.get("conf") or 0) < 0.7:
            say("W", f"override {code} 置信度 {o.get('conf')} < 0.7，按准入标准不应采纳")
    if not ERR:
        print(f"  ✓ 绑定与门槛全部指向有效目标（行业定环节 {len(segmap)} 条、"
              f"行业兜底 {len(cfg.get('industry_to_chain') or {}) + len(_ind2chain())} 条、"
              f"逐票 override {len(ov)} 条一并校验）")

    # ---------- A 表：跨环节歧义词 ----------
    amb = ambiguous(cfg, idx)
    unbound = {k: v for k, v in amb.items() if k not in binding}
    print(f"\n—— A 跨环节歧义词（{len(amb)} 个，{len(unbound)} 个未绑定）——")
    if not a.brief:
        rows = []
        for kw, locs in sorted(amb.items(), key=lambda x: -len(x[1])):
            rows.append((df_of(kw, mainc), kw, locs, kw in binding))
        rows.sort(reverse=True)
        for dfm, kw, locs, bd in rows:
            mark = "已绑定" if bd else "**未绑定**"
            print(f"  {dfm:3d}票  {kw:8s} {mark:8s} " +
                  " / ".join(f"{c}·{s if s != '<链级>' else '链级'}" for c, s in locs))
    for kw, locs in sorted(unbound.items()):
        say("W", f"歧义词「{kw}」未绑定，归属由 JSON 书写顺序决定 → " +
            " / ".join(f"{c}·{s}" for c, s in locs if s != "<链级>"))

    # ---------- B 表：df 超阈泛词 ----------
    # 只有「能决定环节 + 不在泛词白名单 + 无共现门槛」才是真风险。
    # 只看 df 会把 设备/材料（本就不参与环节判断）、电池/医药（链级词，语义准确）
    # 一起报出来，黄框永远清不零，人就会开始无视黄框——那比没有自检更糟。
    generic = set(chains.get("_泛词不参与环节") or [])
    passed = chains.get("_泛词放行") or {}
    print(f"\n—— B df>{DF_GENERIC} 泛词 ——")
    rows, risk = [], []
    for kw in idx:
        dfa = df_of(kw, allc)
        if dfa <= DF_GENERIC:
            continue
        locs = sorted(idx[kw])
        segs = [sn for _, sn in locs if sn != "<链级>"]
        if not segs:
            why = "链级词（只定链，语义准确）"
        elif kw in generic:
            why = "在 _泛词不参与环节 里（不参与环节判断）"
        elif kw in cooc:
            why = "已有共现门槛"
        elif kw in passed:
            why = "已核对放行"
        else:
            why = "**真风险：能单独决定环节**"
            risk.append((dfa, kw, locs))
        rows.append((dfa, kw, why, locs))
    rows.sort(reverse=True)
    if not a.brief:
        for dfa, kw, why, locs in rows:
            print(f"  df={dfa:4d} len={len(kw)} {kw:8s} {why}")
    for dfa, kw, locs in risk:
        say("W", f"泛词「{kw}」df={dfa} 无共现门槛，会单独决定环节 → " +
            " / ".join(f"{c}·{s if s != '<链级>' else '链级'}" for c, s in locs))
    if not risk:
        print("  ✓ 没有一个泛词能单独决定环节")

    # ---------- C 表：语义倒挂 ----------
    print(f"\n—— C 语义倒挂 ——")
    narrow = [(len(kw), kw, df_of(kw, allc), sorted(idx[kw]))
              for kw in idx if len(kw) >= 4 and df_of(kw, allc) <= DF_NARROW]
    narrow.sort(reverse=True)
    if not narrow:
        print("  ✓ 没有长而极窄的词")
    elif not a.brief:
        for ln, kw, dfa, locs in narrow[:20]:
            print(f"  len={ln} df={dfa}  {kw:10s} " +
                  " / ".join(f"{c}·{s if s != '<链级>' else '链级'}" for c, s in locs))
        if len(narrow) > 20:
            print(f"  …另有 {len(narrow) - 20} 个")
    if narrow:
        say("I", f"{len(narrow)} 个长词（len≥4）只服务 ≤{DF_NARROW} 条项名——"
                "看着具体，实为个案，长度优势不代表判断力（不强制处理）")

    # ---------- D 表：孤儿词 ----------
    orphans = [(kw, sorted(locs)) for kw, locs in idx.items() if df_of(kw, allc) == 0]
    print(f"\n—— D 孤儿词（{len(orphans)} 个，语料零命中）——")
    if not a.brief and orphans:
        for kw, locs in orphans[:30]:
            print(f"  {kw:10s} " +
                  " / ".join(f"{c}·{s if s != '<链级>' else '链级'}" for c, s in locs))
        if len(orphans) > 30:
            print(f"  …另有 {len(orphans) - 30} 个")
    if orphans:
        say("I", f"{len(orphans)} 个词在 {nitem} 条主营项名里零命中——"
                "保留成本低，但别把它们当生效规则")

    # ---------- E 表：题材层归位 ----------
    # 环节词表是给「主营项名」设计的，链级 match 是给「题材名」设计的，两者长期不同步。
    # 一个环节词如果真是题材名、却归不到自己的位置，说明它要么没进 match（归不上），
    # 要么被别的链更短的父词抢走（卫星导航被军工链的『卫星』抢、3D玻璃被地产基建的『玻璃』抢）。
    # 这类缺口不会报错，只会让那个题材静默地拿不到同环节加成——归因变哑，很难发现。
    themes, pool_tag = load_themes()
    print(f"\n—— E 题材层归位（{len(themes)} 个标签，源 {pool_tag or '无'}）——")
    gap, expected = [], []
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        for sn, kws in (cv.get("环节") or {}).items():
            for kw in kws:
                if kw not in themes:
                    continue
                got = chain_of(kw, chains)
                if got == (cn, sn):
                    continue
                if binding.get(kw) and binding[kw] != f"{cn}·{sn}":
                    expected.append(kw)
                else:
                    gap.append((kw, cn, sn, got))
    if not themes:
        print("  跳过：找不到 cache/pool_*/themes.json，无法判断题材名是否真实存在")
    elif not gap:
        print(f"  ✓ 真实题材名里的环节词全部归位（{len(expected)} 个按归属绑定改归他处，属预期）")
    else:
        for kw, cn, sn, got in gap:
            g = f"{got[0]}·{got[1]}" if got[0] else "未归链"
            print(f"  {kw:10s} 应在 {cn}·{sn}  实际 {g}")
            say("W", f"题材名「{kw}」归不到 {cn}·{sn}（实际 {g}）——"
                    "该题材拿不到同环节加成，需补链级 match 词")
    # ---------- 环节覆盖率 ----------
    # 两种「零覆盖」必须分开：①语料里根本没这些词（纯题材环节，正常）；
    # ②词在语料里有命中，但全被归属绑定挪走了 → 这个环节已经名存实亡，下版该删。
    # 只看 ① 会漏掉 ②：消费链·服装家纺 的词全绑到轻工纺服链后，语料统计仍是"有覆盖"。
    # 配置里的 `_环节保留` 是已经判断过、决定留下的（写明了理由），不再重复报警。
    kept = chains.get("_环节保留") or {}
    print(f"\n—— 环节词覆盖 ——")
    hollow, pure = [], []
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        if a.chain and cn != a.chain:
            continue
        for sn, kws in (cv.get("环节") or {}).items():
            key = f"{cn}·{sn}"
            raw = [(k, df_of(k, allc)) for k in kws]
            live = [(k, d) for k, d in raw
                    if d and any_live(k, cn, sn, binding, cooc, allc)]
            if any(d for _, d in raw) and not live:
                if key in kept:
                    pure.append(f"{key}(保留)")
                    continue
                moved = [f"{k}→{binding[k]}" for k, d in raw
                         if d and k in binding and binding[k] != key]
                hollow.append(key + (f"（锚词 {', '.join(moved)} 已绑走）" if moved else ""))
            elif not any(d for _, d in raw):
                pure.append(key)
    if hollow:
        for h in hollow:
            print(f"  ⚠ {h}")
        say("W", f"{len(hollow)} 个环节的词全部被归属绑定挪走，环节已名存实亡，"
                "建议下版删除或补专指词")
    if pure:
        print(f"  零命中环节 {len(pure)} 个（纯题材，正常）：" + "、".join(pure[:12]) +
              ("…" if len(pure) > 12 else ""))
    if not hollow and not pure:
        print("  ✓ 每个环节至少命中 1 条项名")

    # ---------- 汇总 ----------
    print("\n" + "=" * 68)
    print(f"汇总  红框 {len(ERR)} ｜ 黄框 {len(WARN)} ｜ 提示 {len(INFO)}")
    if ERR:
        print("结论  存在配置矛盾，必须修（绑定表指向了不存在的目标）")
        return 1
    if a.strict and unbound:
        print(f"结论  --strict：仍有 {len(unbound)} 个歧义词未绑定，归属不可复现")
        return 1
    if WARN:
        print("结论  无硬错误，但有黄框待处理（歧义词绑定 / 泛词门槛）")
    else:
        print("结论  通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
