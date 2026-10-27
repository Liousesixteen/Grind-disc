#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题材/龙头 口径审计器（一次性工具，保留可回溯）
==================================================
背景（2026-10-24 用户报错）：看板补涨表里「题材龙头」列几乎全是「上海九百」，
与同一行的「今日归因」对不上。追查发现**三套题材口径混用**：

  ① x['题材']   = code2themes[c][0]       东财标签第一个（常是「长江三角」「一般零售」这类）
  ② x['焦点']   = role_of[c]['theme']      主归属（该票标签中涨停家数最多的那个）
  ③ 画像.theme  = TA.attribute 五因子归因   当日最相关题材（看板「今日归因」显示这个）

看板一行里同时出现 ②③，读者必然误读。本脚本量化三者的一致率、龙头命中率、
以及「大桶虚高」（标签口径涨停数 vs 主归属口径涨停数），用于改前改后对比。

用法
  .venv/bin/python scripts/_audit_theme_head.py --build     # 拉一次快照 + 涨停池 → 存 fixture
  .venv/bin/python scripts/_audit_theme_head.py             # 读 fixture，跑审计
  .venv/bin/python scripts/_audit_theme_head.py > cache/_replay/audit_after.txt   # 存一份留档
"""
import argparse, json, os, sys, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_pipeline as P
import theme_attr as TA
import stock_profile as SP
import pankou as PK
import intraday_scan as IS

ROOT = P.ROOT
FX_DIR = os.path.join(ROOT, "cache", "_replay")


def fixture_path(day):
    return os.path.join(FX_DIR, f"replay_{day}.json")


def build(day):
    os.makedirs(FX_DIR, exist_ok=True)
    snap, src, warn, _ = IS.take_snapshot(False, "")
    zt, zb, dtp = IS.take_pools(day, False)
    if not snap or not zt:
        raise SystemExit(f"取数失败：snap={len(snap or [])} zt={len(zt or [])}")
    d = {"day": day, "src": src, "taken_at": dt.datetime.now().strftime("%F %T"),
         "snap": snap, "zt": zt, "zb": zb, "dtp": dtp}
    json.dump(d, open(fixture_path(day), "w", encoding="utf-8"), ensure_ascii=False)
    print(f"fixture 已存 {fixture_path(day)}（{len(snap)} 只快照 / {len(zt)} 涨停 / 源 {src}）")


def _bucket_members(name, focus_idx, t2codes, seg2codes, main_theme, ladder=None):
    """场景名 → (成员 code 集合, 口径, 层级)。口径一律取 ladder 里记的（唯一定义在 pankou）。"""
    basis = ((ladder or {}).get(name) or {}).get("口径") or ""
    if not basis:
        tier = next((e[0] for e in focus_idx if e[1] == name), "")
        basis = "环节" if (tier == "环节" or ("·" in (name or "")
                                         and name.split("·")[0].endswith("链"))) else "题材"
    mem = PK.bucket_members(name, basis, main_theme, seg2codes) if name else set()
    return set(mem), basis, ("环节" if basis == "环节" else "题材")


def audit(day):
    d = json.load(open(fixture_path(day), encoding="utf-8"))
    snap, zt, zb, dtp, src = d["snap"], d["zt"], d["zb"], d["dtp"], d["src"]
    cfg = json.load(open(P.CFG_PATH, encoding="utf-8"))
    pool = IS.latest_pool_dir(None)
    rows = json.load(open(os.path.join(pool, "pool_final.json"), encoding="utf-8"))
    summ = json.load(open(os.path.join(pool, "summary.json"), encoding="utf-8"))
    focus_idx = summ["focus_index"]
    kprev = IS.load_kprev(pool)

    R = IS.compute_readings(snap, {}, rows, focus_idx, cfg, src,
                            pools=(zt, zb, dtp), kprev=kprev)

    # ── 复算桶成员（与 compute_readings 内部同口径）──
    t2codes, seg2codes = {}, {}
    for r in rows:
        for t in (r.get("题材") or []):
            t2codes.setdefault(t, []).append(r["code"])
            ch, sg = P.chain_of(t, cfg.get("chains", {}))
            if ch and sg:
                seg2codes.setdefault((ch, sg), set()).add(r["code"])
    seg2codes = {k: list(v) for k, v in seg2codes.items()}
    zt_codes = {z["c"] for z in zt}
    rows_of = {r["code"]: r for r in rows}
    # 主归属全量（下游 bucket 成员要用）——**必须与 compute_readings 同参数**，
    # 否则审计自己算一份旧规则的归属，会误报「龙头不在桶内」。
    _AO = (cfg.get("intraday") or {}).get("attr")
    _CO = lambda t: P.chain_of(t, cfg.get("chains", {}))   # noqa: E731
    _r2, _l2, _s2, main_theme = PK.roles_ladders(
        zt, zb, dtp, rows, focus_idx, t2codes, seg2codes,
        live_pct={r["code"]: r.get("pct") for r in rows},
        fmcap={r["code"]: (r.get("fmcap") or 0) for r in rows},
        attr_opt=_AO, chain_of=_CO)

    print(f"═══ 题材/龙头 口径审计 · fixture {day}（源 {src}，抓取 {d['taken_at']}）═══")
    print(f"快照 {len(snap)} 只 · 涨停 {len(zt)} · 炸板 {len(zb)} · 跌停 {len(dtp)} · "
          f"股池 {os.path.basename(pool)}（{len(rows)} 只，焦点 {len(focus_idx)} 条）")

    pk = R.get("pankou") or []
    cu = R.get("catchup") or []
    candidates = R.get("candidates") or []
    ladder = R.get("ladder") or {}

    # ── A 盘口表：题材 vs 焦点 ──
    same = sum(1 for z in pk if z.get("题材") == z.get("焦点"))
    no_lad = sum(1 for z in pk if not z.get("梯队"))
    print(f"\n── A 盘口表（{len(pk)} 只）──")
    print(f"  题材(主归属) 非空 ： {sum(1 for z in pk if z.get('题材'))}/{len(pk)}")
    print(f"  梯队取不到（ladder 无该主归属题材）： {no_lad}/{len(pk)}")
    old_wrong = [(z["name"], (z.get("题材标签") or ["—"])[0], z.get("题材"))
                 for z in pk if (z.get("题材标签") or [""])[0] != z.get("题材")]
    print(f"  若仍用旧口径「标签[0]」当题材，会显示错题材： {len(old_wrong)}/{len(pk)}")
    for n, a, b in old_wrong[:6]:
        print(f"    {n:<7} 旧显示={a or '—':<10} 实际主归属={b or '—'}")

    # ── B 补涨表：场景 vs 主归属 vs 归因 ──
    print(f"\n── B 补涨表（{len(cu)} 只）──")
    if cu:
        no_head = [x for x in cu if not x.get("龙头") or x.get("龙头") == "—"]
        print(f"  龙头 =「—」（查不到）： {len(no_head)}/{len(cu)}（{len(no_head) / len(cu):.0%}）")
        # 场景 vs 本票主归属（应当 100% 相同——补涨表只从「本票主归属」那个桶里取票）
        dis_main = [x for x in cu if (x.get("主归属") or "") != (x.get("场景") or "")]
        print(f"  场景 ≠ 本票主归属： {len(dis_main)}/{len(cu)}")
        for a in dis_main[:6]:
            print(f"    {a['name']:<7} 场景={a.get('场景')} 主归属={a.get('主归属')}")
        # 龙头是否在场景桶内、是否为桶内最高板
        outside, not_top = [], []
        for x in cu:
            mem, basis, tier = _bucket_members(x.get("场景") or "", focus_idx, t2codes,
                                              seg2codes, main_theme, ladder)
            head = x.get("龙头")
            hz = next((z for z in zt if z.get("n") == head), None)
            if head and head != "—":
                if hz is None or hz["c"] not in mem:
                    outside.append((x["name"], x.get("场景") or "—", head))
                else:
                    mx = max((float(z.get("lbc") or 1) for z in zt if z["c"] in mem), default=0)
                    if (float(hz.get("lbc") or 1)) < mx:
                        not_top.append((x["name"], x.get("场景") or "—", head,
                                        hz.get("lbc"), mx))
        print(f"  龙头不在场景桶内： {len(outside)}/{len(cu)}")
        for a in outside[:8]:
            print(f"    {a[0]:<7} 场景={a[1]:<12} 龙头={a[2]}")
        print(f"  龙头不是桶内最高板： {len(not_top)}/{len(cu)}")
        for a in not_top[:6]:
            print(f"    {a[0]:<7} 场景={a[1]:<12} 龙头={a[2]}({a[3]}板) 桶内最高{a[4]}板")
        # 场景口径分布
        cnt = {}
        for x in cu:
            k = x.get("口径") or "?"
            cnt[k] = cnt.get(k, 0) + 1
        print("  场景口径分布：", " · ".join(f"{k} {v}只" for k, v in sorted(cnt.items())))
        # 通过门槛的场景清单（解释为什么只有这么几只）
        passed = [(k, v) for k, v in ladder.items()
                  if (v.get("涨停") or 0) >= 2 and v.get("判定") != "独木难支"
                  and (v.get("龙头板") or 0) >= 3]
        print(f"  过门槛场景（涨停≥2 且 龙头≥3板）： {len(passed)} 个 → "
              + "、".join(f"{k}({v['涨停']}家/{v['龙头']}{v['龙头板']}板)" for k, v in
                          sorted(passed, key=lambda kv: -kv[1]["涨停"])[:12]))

    # ── C 机会候选：题材(标签[0]) 是否是主归属 ──
    if candidates:
        print(f"\n── C 机会候选（{len(candidates)} 只）──")
        print(f"  场景非空： {sum(1 for x in candidates if x.get('场景'))}/{len(candidates)}"
              f" · 梯队非空： {sum(1 for x in candidates if x.get('梯队'))}/{len(candidates)}")
        print(f"  场景 ≠ 本票主归属： "
              f"{sum(1 for x in candidates if x.get('场景') != x.get('题材'))}/{len(candidates)}")

    # ── D 大桶虚高：标签口径 vs 主归属口径（直接读 ladder 两列）──
    print("\n── D 梯队家数：标签口径 vs 主归属口径（差 ≥2 家才列）──")
    diffs = sorted(((k, v.get("涨停标签") or 0, v.get("涨停") or 0)
                    for k, v in ladder.items()
                    if (v.get("口径") or "题材") == "题材"),
                   key=lambda x: -(x[1] - x[2]))
    n_tag = sum(1 for d in diffs if d[1])
    n_main = sum(1 for d in diffs if d[2])
    print(f"  标签口径有涨停的题材 {n_tag} 个；主归属口径有票的题材 {n_main} 个")
    for t, a, b in [d for d in diffs if d[1] - d[2] >= 2][:10]:
        print(f"    {t:<14} 标签{a}家 → 主归属{b}家  （虚高 {a - b}）")

    # ── E 用户报的那两行 ──
    print("\n── E 用户截图两行原样（固态电池 / 存储芯片）──")
    for x in cu:
        base = (x.get("场景") or "")
        r = rows_of.get(x["code"]) or {}
        tg = [t for t in (r.get("题材") or []) if any(k in t for k in ("固态", "存储"))]
        if any(k in base for k in ("固态", "存储")) or tg:
            print(f"  {x['name']:<7} 场景={base:<12} 龙头={x.get('龙头')}:<7 "
                  f"梯队={x.get('梯队')} 标签含={tg or ['（无）']}")
    if not any(any(k in (x.get("场景") or "") for k in ("固态", "存储")) for x in cu):
        print("  （补涨表里已没有「固态电池/存储芯片」场景的行）")

    # ── F 不变量汇总（改后应全为 0）──
    print("\n── F 不变量（改后应清零）──")
    print(f"  F1 盘口「梯队」取不到            : {no_lad}")
    if cu:
        print(f"  F2 补涨「龙头」=—                : "
              f"{sum(1 for x in cu if not x.get('龙头') or x.get('龙头') == '—')}")
        print(f"  F2b 补涨「龙头」不在该场景桶内    : {len(outside) if cu else '—'}")
        print(f"  F2c 补涨「场景」≠ 本票主归属      : {len(dis_main)}")
    print(f"  F3 ladder 里 key 与 主归属 无交集: "
          f"{sum(1 for k in ladder if k not in t2codes and '·' not in k)}")
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--day", default="")
    a = ap.parse_args()
    day = a.day or dt.date.today().strftime("%Y%m%d")
    if a.build:
        build(day)
        return
    if not os.path.exists(fixture_path(day)):
        raise SystemExit(f"缺 fixture：{fixture_path(day)}，先 --build")
    audit(day)


if __name__ == "__main__":
    main()
