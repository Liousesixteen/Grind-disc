# -*- coding: utf-8 -*-
"""全市场概念 → 链·环节 映射审计（2026-10-25 用户点名的收尾工程）

目的
----
对 universe.json 全量 5576 只逐票复核「概念 → 链·环节」映射：
  a) 每个「有效概念」→ (链, 环节) / 链级 / 未映射（复用生产逻辑 kwrule.chain_of，
     与主归属/归因完全同口径，绝不另写一套相似但不同的匹配）；
  b) 汇总覆盖率 + 未映射概念频次表（按覆盖股票数排序）；
  c) 交叉核对：主业链·环节（F10 口径，universe.json 已算好）vs 概念映射链——
     统计「主业与概念链错位」的票数与分布。

产物（全部落 cache/theme_universe/）
----
  audit_mapping.json(.gz)   {code: {概念: [链,环节]|null}}   ← 链级环节写作 [链,"（链级·需下钻）"] 或 [链,""]
  audit_summary.json        覆盖率 / 频次表 / 错位分布
  audit_unmapped_freq.json  未映射概念全量频次（修复缺口用，报告只引 top300）

铁律
----
  · 只读不改：本脚本不修改 universe.json / pool_config.json（改词表是人工决策，见报告）；
  · 取不到就标注，绝不编造；不静默丢弃。
"""
import argparse
import collections
import datetime as dt
import gzip
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from kwrule import chain_of                      # noqa: E402  生产逻辑唯一入口
import theme_universe as TU                      # noqa: E402  load_universe（带缓存）

OUT_DIR = TU.OUT_DIR
MAPPING_FILE = os.path.join(OUT_DIR, "audit_mapping.json.gz")
SUMMARY_FILE = os.path.join(OUT_DIR, "audit_summary.json")
UNMAPPED_FILE = os.path.join(OUT_DIR, "audit_unmapped_freq.json")


def audit(universe_path=TU.UNIVERSE_FILE, top_n=300, verbose=True):
    chains = TU.load_cfg().get("chains") or {}
    uni = TU.load_universe(universe_path)
    if not uni:
        raise SystemExit(f"universe 为空：{universe_path}")

    mapping = {}
    tot_con = hit_con = chain_only_con = 0        # 概念级计数
    stock_hit = 0                                 # 票级：≥1 概念上链
    stock_with_eff = 0
    unmapped_cnt = collections.Counter()          # 未映射概念 → 覆盖股票数
    mapped_cnt = collections.Counter()            # 已映射概念 → 覆盖股票数（对照用）
    mismatch = []                                 # (code, name, 主业链, 概念链明细)
    main_nochain = 0                              # 主业本身未归链的票
    chain_dist = collections.Counter()            # 错位票的 主业链 → 概念最强链 分布

    for code, rec in uni.items():
        eff = rec.get("有效概念") or []
        if eff:
            stock_with_eff += 1
        m = {}
        code_chains = []                          # 概念映射到的链（按映射顺序去重）
        for t in eff:
            ch, sg = chain_of(t, chains)
            if ch:
                m[t] = [ch, sg or ""]
                hit_con += 1
                mapped_cnt[t] += 1
                if not sg or sg == "（链级·需下钻）":
                    chain_only_con += 1
                else:
                    pass
                if ch not in code_chains:
                    code_chains.append(ch)
            else:
                m[t] = None
                unmapped_cnt[t] += 1
            tot_con += 1
        if code_chains:
            stock_hit += 1
        mapping[code] = m

        # ── 交叉核对：主业链 vs 概念链 ──
        mc = rec.get("链") or ""
        if not mc:
            if eff:
                main_nochain += 1
            continue
        if not code_chains:
            continue                              # 无概念可对照，单列在覆盖率里
        if mc not in code_chains:
            # 概念最强链：环节级映射里出现次数最多的链（并列取先出现）；无环节级则取首个链级
            seg_chains = [v[0] for v in m.values()
                          if v and v[1] and v[1] != "（链级·需下钻）"]
            strongest = (collections.Counter(seg_chains or code_chains)
                         .most_common(1)[0][0])
            mismatch.append({"code": code, "name": rec.get("name") or "",
                             "主业链": mc, "概念最强链": strongest,
                             "概念链": code_chains})
            chain_dist[f"{mc} → {strongest}"] += 1

    n = len(mapping)
    summary = {
        "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe": universe_path,
        "n_stocks": n,
        "概念级": {
            "有效概念总数": tot_con,
            "映射上链": hit_con,
            "其中链级(未下钻)": chain_only_con,
            "其中环节级": hit_con - chain_only_con,
            "未映射": tot_con - hit_con,
            "覆盖率(上链含链级)": round(hit_con / max(tot_con, 1), 4),
            "环节级映射率": round((hit_con - chain_only_con) / max(tot_con, 1), 4),
        },
        "票级": {
            "总数": n,
            "有有效概念": stock_with_eff,
            "≥1概念上链": stock_hit,
            "票覆盖率": round(stock_hit / max(n, 1), 4),
            "主业未归链(有概念)": main_nochain,
            "主业与概念链错位": len(mismatch),
        },
        "错位分布_top50": dict(chain_dist.most_common(50)),
        "未映射top": [{"concept": k, "n_stocks": v}
                     for k, v in unmapped_cnt.most_common(top_n)],
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with gzip.open(MAPPING_FILE, "wt", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)
    json.dump(summary, open(SUMMARY_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(dict(unmapped_cnt.most_common()), open(UNMAPPED_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    if verbose:
        c = summary["概念级"]
        s = summary["票级"]
        print(f"审计 {n} 只 · 有效概念 {c['有效概念总数']} 个")
        print(f"  概念覆盖率(上链含链级) {c['覆盖率(上链含链级)']:.1%}  "
              f"(环节级 {c['环节级映射率']:.1%}，链级 {c['其中链级(未下钻)']})")
        print(f"  未映射 {c['未映射']} 个概念")
        print(f"  票覆盖率 {s['票覆盖率']:.1%}（{s['≥1概念上链']}/{n}）")
        print(f"  主业未归链 {s['主业未归链(有概念)']} 只 · 主业与概念链错位 {s['主业与概念链错位']} 只")
        print(f"  产物：{MAPPING_FILE}")
    return summary, mapping, unmapped_cnt


def show_top(n=50):
    d = json.load(open(UNMAPPED_FILE, encoding="utf-8"))
    for i, (k, v) in enumerate(list(d.items())[:n], 1):
        print(f"{i:3d}. {k}  覆盖 {v} 只")


# ══════════════ 全市场核对总表（CSV，utf-8-sig） ══════════════
def build_csv(out_path, universe_path=TU.UNIVERSE_FILE):
    """列：代码,名称,申万一级,申万二级,申万三级,地域,概念数,有效概念数,
    全部概念(丨),有效概念(丨),概念映射到的链(丨),主业链·环节,未映射概念(丨),备注"""
    chains = TU.load_cfg().get("chains") or {}
    uni = TU.load_universe(universe_path)
    import csv
    n_err = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "申万一级", "申万二级", "申万三级", "地域",
                    "概念数", "有效概念数", "全部概念(丨分隔)", "有效概念(丨分隔)",
                    "概念映射到的链(丨分隔)", "主业链·环节", "未映射概念(丨分隔)", "备注"])
        for code, rec in uni.items():
            eff = rec.get("有效概念") or []
            allc = rec.get("全部概念") or []
            rows, code_chains, unmapped = [], [], []
            for t in eff:
                ch, sg = chain_of(t, chains)
                if ch:
                    rows.append(f"{t}={ch}·{sg or '链级'}")
                    if ch not in code_chains:
                        code_chains.append(ch)
                else:
                    unmapped.append(t)
            mc, ms = rec.get("链") or "", rec.get("环节") or ""
            main = f"{mc}·{ms}" if (mc and ms) else (mc or "")
            notes = []
            if not eff:
                notes.append("无有效概念")
            if mc and code_chains and mc not in code_chains:
                notes.append(f"错位：主业{mc}，概念链{'/'.join(code_chains)}")
            elif mc and not code_chains and eff:
                notes.append("概念全部未上链")
            if not mc:
                notes.append("主业未归链" if rec.get("主营项名") is not None else "主业未归链(无画像)")
            try:
                w.writerow([code, rec.get("name") or "", rec.get("申万一级") or "",
                            rec.get("申万二级") or "", rec.get("申万三级") or "",
                            "丨".join(rec.get("地域") or []), len(allc), len(eff),
                            "丨".join(allc), "丨".join(eff), "丨".join(code_chains),
                            main, "丨".join(unmapped), "；".join(notes)])
            except Exception:
                n_err += 1
    print(f"CSV 已写 {out_path}（{len(uni)} 行，写行失败 {n_err}）")


# ══════════════ 抽样核验明细（markdown 片段） ══════════════
def sample_details(codes, out_path=None):
    chains = TU.load_cfg().get("chains") or {}
    uni = TU.load_universe()
    lines = []
    for c in codes:
        rec = uni.get(c)
        if not rec:
            lines.append(f"- {c}: 不在大表")
            continue
        eff = rec.get("有效概念") or []
        pairs = []
        for t in eff:
            ch, sg = chain_of(t, chains)
            pairs.append(f"{t}→{ch}·{sg}" if ch else f"{t}→未映射")
        lines.append(
            f"### {c} {rec.get('name')}\n"
            f"- 申万：{'/'.join(rec.get('申万') or [])}｜主营项名：{'、'.join(rec.get('主营项名') or []) or '—'}"
            f"｜主业链·环节：{rec.get('主业链·环节') or '—'}\n"
            f"- 概念映射（{len(eff)} 个有效）：{'；'.join(pairs) or '（无）'}\n")
    txt = "\n".join(lines)
    if out_path:
        open(out_path, "w", encoding="utf-8").write(txt)
        print(f"抽样明细已写 {out_path}")
    else:
        print(txt)
    return txt


# ══════════════ 分层随机抽样 ══════════════
def pick_sample(n_per_none=30, seed=20261025):
    """覆盖 19 条链 + 北交所 ≥3 只。先按「主业链（缺则概念链）」分层随机，再补北交所。"""
    import random
    uni = TU.load_universe()
    chains = TU.load_cfg().get("chains") or {}
    by_chain = {}
    bse = []
    for c, rec in uni.items():
        if c.startswith(("92", "4", "8", "43")):
            bse.append(c)
        ch = rec.get("链") or ""
        if not ch:                       # 主业未归链 → 用首个概念链代分层
            for t in (rec.get("有效概念") or []):
                _ch, _ = chain_of(t, chains)
                if _ch:
                    ch = _ch
                    break
        if ch:
            by_chain.setdefault(ch, []).append(c)
    rng = random.Random(seed)
    chain_names = [k for k in chains if not k.startswith("_")]
    picked = []
    for ch in chain_names:               # 19 条链每链至少 1 只
        pool = by_chain.get(ch) or []
        if pool:
            picked.append(rng.choice(pool))
    rest = [c for cs in by_chain.values() for c in cs if c not in picked]
    rng.shuffle(rest)
    while len(picked) < n_per_none and rest:
        picked.append(rest.pop())
    for c in rng.sample(bse, min(3, len(bse))):
        if c not in picked:
            picked.append(c)
    return picked


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uni", default=TU.UNIVERSE_FILE)
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--show", type=int, default=0, help="只打印未映射频次表前 N 行")
    ap.add_argument("--csv", default="", help="写全市场核对总表 CSV（utf-8-sig）")
    ap.add_argument("--sample", action="store_true", help="打印分层随机抽样（19链+北交所）")
    ap.add_argument("--codes", default="", help="指定代码列表打抽样明细")
    ap.add_argument("--sample-out", default="", help="抽样明细写入文件")
    a = ap.parse_args()
    if a.show:
        show_top(a.show)
    elif a.csv:
        build_csv(a.csv, a.uni)
    elif a.sample or a.codes or a.sample_out:
        codes = ([c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
                 or pick_sample())
        sample_details(codes, a.sample_out or None)
    else:
        audit(a.uni, a.top)
