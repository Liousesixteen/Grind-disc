#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双闸对照（v1.0 · C 级草案，2026-10-25）—— 闸A 盘口质量 × 闸B 方向强度
================================================================
方案与判读规则全文：docs/方案-双闸对照-盘口质量x方向强度-v1.md（先读它再用本模块）。

体系出处（不另起口径）：
  · 熔炼系统 3.5「日内方向登记与盘后日频双口径榜互为校验」＋环境总闸「谁在极端谁一票否决」；
  · 肉肉体系 4.2 封板质量 / 5.1 梯队完整性（已熔入 pankou.py，情绪条唯一口径在那里）；
  · 233 板块确认四时点（B 级）；风轻云淡「主升后期才博弈补涨龙」；波哥「末浪只做滞涨补涨」；
  · 外部调研四层指标（赚钱效应/容错率/亏钱效应/结构）与六种背离形态，阈值全部为经验统计。

数据源全部复用既有缓存，**零网络请求**：
  sentiment / ladder —— pankou.roles_ladders 唯一口径（调用方传入）；
  今日涨停池 —— take_pools 的 pools[0]（含 连板lbc/炸板zbc）；
  全市场快照 —— take_snapshot 的 snap（拿昨日涨停票的今日涨幅算溢价/大面）；
  昨日涨停集合 —— cache/pool_YYYYMMDD 盘后全市场快照逐日回溯（连板数＝连续涨停天数）。

铁律：缺数据留空标注（note 写明缺什么），绝不编造；本模块只出读数与 C 级组合判定灯，
不产生买卖指令；A 闸极端弱时按熔炼环境总闸对 B 闸一票否决（在动作倾向里注明）。
"""
import json
import os
import re

from pankou import _limit_th

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIG_SNAP = "all_a_snapshot.json"


def _num(v, d=None):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return d


def _pool_dirs():
    """全部盘后池目录（cache/pool_YYYYMMDD，排除 _intraday），按日期升序。"""
    out = []
    base = os.path.join(ROOT, "cache")
    if not os.path.isdir(base):
        return out
    for n in os.listdir(base):
        m = re.fullmatch(r"pool_(\d{8})", n)
        if m and os.path.isfile(os.path.join(base, n, _BIG_SNAP)):
            out.append((m.group(1), os.path.join(base, n)))
    return sorted(out)


def _limit_up_codes(snap_rows):
    """当日涨停集合 {code: name}——口径与 pankou._limit_th 一致（含 ST 4.8/创业科创 19.7/北交 29.7）。
    排除两类噪音：① 名字 N 开头（上市首日无涨跌幅，+171% 也会混进来，把溢价打成天文数字）；
    ② pct > 30.5（超过所有板别涨停上限＝无限制股）。pct 缺失的票跳过（不臆测）。"""
    out = {}
    for r in snap_rows or []:
        code = r.get("code") or r.get("c")
        pct = _num(r.get("pct"))
        nm = r.get("name") or ""
        if not code or pct is None:
            continue
        if pct > 30.5 or nm.upper().startswith("N"):
            continue
        if pct >= _limit_th(str(code), nm):
            out[str(code)] = nm
    return out


_PREV_CACHE = {}      # prev_limit_map 结果缓存（quote_day 为键；历史盘后池日内不变，15s 刷新不必反复解析 6 份快照）
_DAY_CACHE = {}       # {日期: 涨停家数}（纵向对比用，逐日缓存）


def _day_limit_counts(dirs):
    """[(日期, 目录)] → {日期: 涨停家数}；解析结果缓存（历史日不变）。读取失败记 None 不编造。"""
    out = {}
    for d, p in dirs:
        if d in _DAY_CACHE:
            out[d] = _DAY_CACHE[d]
            continue
        try:
            rows = json.load(open(os.path.join(p, _BIG_SNAP), encoding="utf-8"))
            n = len(_limit_up_codes(rows))
        except Exception:
            n = None
        _DAY_CACHE[d] = n
        out[d] = n
    return out


def prev_limit_map(today, max_back=10):
    """昨日涨停集合 + 连板回溯。
    返回 (昨日日期, {code: 昨日收盘连板数}, note)。
    参数 today＝**行情所属交易日**（quote_day，由调用方定，不是无脑真实今天）：
      · 盘中/盘后（交易日）→ quote_day=真实今天 → 昨日=它的前一盘后池目录；
      · 周末/节假日复盘 → quote_day=股池 data_date（看板显示的那个交易日）
        → 昨日=它的前一交易日。否则溢价会退化成「昨日涨停票当日涨幅」（恒≈+10%，无意义）。
    连板数＝截至昨日的连续涨停天数：从昨日起逐日向前，该码当日仍涨停则 +1，断即止；
    已断的码**冻结在断前的天数**（不从结果里丢掉）。
    遇到盘后池目录缺失（间隔 >4 天视为断档）→ 连板回溯到此为止（保守记法，note 说明）。"""
    _k = str(today)
    if _k in _PREV_CACHE:          # 日内历史不变：命中缓存不再解析 6 份全市场快照
        return _PREV_CACHE[_k]
    dirs = [d for d in _pool_dirs() if d[0] < str(today)]
    if not dirs:
        return None, {}, "无更早的盘后池目录，昨日涨停/晋级/溢价不可算"
    prev_date, prev_dir = dirs[-1]
    try:
        rows = json.load(open(os.path.join(prev_dir, _BIG_SNAP), encoding="utf-8"))
    except Exception as e:
        return None, {}, f"昨日池读取失败（{e}）"
    cur = _limit_up_codes(rows)
    if not cur:
        return prev_date, {}, f"{prev_date} 盘后池无涨停记录（疑似降级源），晋级/溢价不可算"
    final = {c: 1 for c in cur}          # 结果：每个昨日涨停码的最终连板数（断板即冻结）
    walk = {c: 1 for c in cur}           # 回溯游标：只含仍在连续涨停的码
    notes = []
    import datetime as _dt
    d0 = _dt.datetime.strptime(prev_date, "%Y%m%d").date()
    for dstr, pdir in reversed(dirs[:-1]):
        gap = (d0 - _dt.datetime.strptime(dstr, "%Y%m%d").date()).days
        if gap > 4:
            notes.append(f"{dstr} 与前一档间隔 {gap} 天（疑缺交易日目录），连板回溯到此为止")
            break
        try:
            rows = json.load(open(os.path.join(pdir, _BIG_SNAP), encoding="utf-8"))
        except Exception:
            notes.append(f"{dstr} 池读取失败，连板回溯到此为止")
            break
        day_set = _limit_up_codes(rows)
        still = {c: s + 1 for c, s in walk.items() if c in day_set}
        if not still:
            break
        for c, s in still.items():
            final[c] = s
        walk = still
        d0 = _dt.datetime.strptime(dstr, "%Y%m%d").date()
        if max(final.values()) >= 6:
            break                      # 回溯够用即可，省时
    _res = (prev_date, final, "；".join(notes))
    _PREV_CACHE[_k] = _res
    return _res


def compute(sentiment, ladder, zt_rows, snap, con, quote_day, pk_rows=None):
    """双闸读数 + 2×2 组合判定灯 + 背离细检。纯函数，缺项留空。
    参数：sentiment/ladder（pankou 口径）、zt_rows（今日涨停池）、snap（全市场快照）、
         con（code→题材标签，扩散度用）、pk_rows（涨停行，含画像：闸B 一票一归属计数）、
         quote_day（行情所属交易日 YYYYMMDD：
         交易日传真实今天，周末/节假日复盘传 data_date——见 prev_limit_map 说明）。"""
    sent = sentiment or {}
    if not sent:
        return {"等级": "C级草案", "note": "sentiment 缺失（快照降级），双闸不可算"}
    zt_rows = zt_rows or []
    code_of = lambda z: z.get("c") or z.get("code")
    zt_codes = {code_of(z) for z in zt_rows if code_of(z)}
    lbc = {code_of(z): int(_num(z.get("lbc"), 1) or 1) for z in zt_rows if code_of(z)}
    pct = {}
    for r in snap or []:
        c = r.get("code") or r.get("c")
        p = _num(r.get("pct"))
        if c and p is not None:
            pct[c] = p

    notes = []
    # ── 闸A：盘口质量 ─────────────────────────────
    zt_n = sent.get("涨停")
    zb_rate = sent.get("炸板率")
    broke, refill = _num(sent.get("炸板"), 0), _num(sent.get("回封"), 0)
    refill_rate = round(refill / broke * 100, 1) if broke else None

    prev_date, prev_map, prev_note = prev_limit_map(quote_day)
    if prev_note:
        notes.append(prev_note)
    promo, 首板溢价, 连板溢价, 大面 = {}, None, None, None
    if prev_map:
        g1 = [c for c, s in prev_map.items() if s == 1]
        g2 = [c for c, s in prev_map.items() if s >= 2]
        up1 = sum(1 for c in g1 if c in zt_codes)
        up2 = sum(1 for c in g2 if c in zt_codes)
        p1 = [pct[c] for c in g1 if c in pct]
        p2 = [pct[c] for c in g2 if c in pct]
        promo = {"昨日涨停": len(prev_map), "昨日首板": len(g1), "昨日连板": len(g2),
                 "一进二": round(up1 / len(g1) * 100, 1) if g1 else None,
                 "一进二样本": len(g1),
                 "二进三以上": round(up2 / len(g2) * 100, 1) if g2 else None,
                 "连板晋级样本": len(g2)}
        # 溢价＝昨日涨停票今日收盘等权涨幅（相对全市场中位的超额另列）。
        # 样本 <5 不出均值（小样本噪音大），留空标注。
        if len(p1) >= 5:
            首板溢价 = round(sum(p1) / len(p1), 2)
        if len(p2) >= 5:
            连板溢价 = round(sum(p2) / len(p2), 2)
        大面 = sum(1 for c in prev_map if pct.get(c) is not None and pct[c] <= -8)
        if len(p1) < 5:
            notes.append(f"昨日首板样本 {len(p1)}<5，首板溢价不可算")

    # 纵向对比（用户 2026-10-25：只给最强太片面）——近 5 个盘后池的涨停家数均值 vs 今日
    _dirs5 = [d for d in _pool_dirs() if d[0] < str(quote_day)][-5:]
    _cnt = _day_limit_counts(_dirs5)
    _seq = [n for _d, n in _cnt.items() if n is not None]
    avg5 = round(sum(_seq) / len(_seq)) if _seq else None
    mult = round(_num(zt_n, 0) / avg5, 2) if (avg5 and zt_n) else None

    # 梯队纵向形状（首板/2板/3板+ 家数）
    f1 = sum(1 for v in lbc.values() if v == 1)
    f2 = sum(1 for v in lbc.values() if v == 2)
    f3 = sum(1 for v in lbc.values() if v >= 3)
    if f3 >= 2 and f2 < f3:
        shape = "菱形（中位塌）"
    elif f2 > f1:
        shape = "倒三角"
    elif f1 >= f2 >= f3:
        shape = "金字塔"
    else:
        shape = "过渡"

    # ── 连板梯队分布（调研口径：淘股吧/韭研复盘表＝逐板列家数，6板以上合并）──
    _dist = {}
    for v in lbc.values():
        _dist[v] = _dist.get(v, 0) + 1
    ladder_dist = [[("首板" if b == 1 else f"{b}板"), n]
                   for b, n in sorted(_dist.items()) if 1 <= b < 6]
    _over = sum(n for b, n in _dist.items() if b >= 6)
    if _over:
        ladder_dist.append(["6板+", _over])

    # ── 快速上板：首封时间分布（7 段＋上午/下午合计；调研口径＝淘股吧/韭研复盘/打板客节奏）──
    # 段界：竞价(<9:30)｜9:30-9:40｜9:40-10:00｜10:00-11:30｜13:00-14:00｜14:00-14:30｜14:30后
    SEGS = [("竞价", 0, 93000), ("9:30-9:40", 93000, 94000), ("9:40-10:00", 94000, 100000),
            ("10:00-11:30", 100000, 113000), ("13:00-14:00", 130000, 140000),
            ("14:00-14:30", 140000, 143000), ("14:30后", 143000, 240000)]
    fbt_cnt = {nm: 0 for nm, _lo, _hi in SEGS}
    fbt_unk = 0
    for z in zt_rows:
        f = _num(z.get("fbt"))
        if not f or f <= 0:
            fbt_unk += 1
            continue
        hit = False
        for nm, lo, hi in SEGS:
            if lo <= f < hi:
                fbt_cnt[nm] += 1
                hit = True
                break
        if not hit:
            fbt_unk += 1
    am_n = sum(fbt_cnt[k] for k in ("竞价", "9:30-9:40", "9:40-10:00", "10:00-11:30"))
    pm_n = sum(fbt_cnt[k] for k in ("13:00-14:00", "14:00-14:30", "14:30后"))
    fbt_list = [[nm, fbt_cnt[nm]] for nm, _lo, _hi in SEGS]

    # ── 明细（用户 2026-10-25：梯队/首封的每个桶要能展开具体个股并跳盘口表）──
    _zt_name = {}
    for z in zt_rows:
        c = code_of(z)
        if c:
            _zt_name[c] = z.get("n") or z.get("name") or ""
    ladder_detail, fbt_detail, fbt_unk_list = {}, {nm: [] for nm, _lo, _hi in SEGS}, []
    for c, v in lbc.items():
        lab = ("首板" if v == 1 else f"{v}板") if v < 6 else "6板+"
        ladder_detail.setdefault(lab, []).append([c, _zt_name.get(c, "")])
    for z in zt_rows:
        c = code_of(z)
        if not c:
            continue
        f = _num(z.get("fbt"))
        if not f or f <= 0:
            fbt_unk_list.append([c, _zt_name.get(c, "")])
            continue
        placed = False
        for nm, lo, hi in SEGS:
            if lo <= f < hi:
                fbt_detail[nm].append([c, _zt_name.get(c, "")])
                placed = True
                break
        if not placed:
            fbt_unk_list.append([c, _zt_name.get(c, "")])

    a_score, why = 0, []
    if zt_n is not None:
        if zt_n >= 80:
            a_score += 2; why.append(f"涨停{zt_n}≥80(+2)")
        elif zt_n >= 50:
            a_score += 1; why.append(f"涨停{zt_n}≥50(+1)")
        elif zt_n < 30:
            a_score -= 2; why.append(f"涨停{zt_n}<30(−2)")
    if zb_rate is not None:
        if zb_rate <= 20:
            a_score += 1; why.append(f"炸板率{zb_rate}%≤20(+1)")
        elif zb_rate > 40:
            a_score -= 2; why.append(f"炸板率{zb_rate}%>40(−2)")
        elif zb_rate > 30:
            a_score -= 1; why.append(f"炸板率{zb_rate}%>30(−1)")
    if 首板溢价 is not None:
        if 首板溢价 >= 3:
            a_score += 1; why.append(f"首板溢价+{首板溢价}%≥3(+1)")
        elif 首板溢价 <= -2:
            a_score -= 2; why.append(f"首板溢价{首板溢价}%≤−2(−2)")
        elif 首板溢价 < 0:
            a_score -= 1; why.append(f"首板溢价{首板溢价}%<0(−1)")
    if 大面 is not None and 大面 >= 3:
        a_score -= 1; why.append(f"大面{大面}家(−1)")
    g12 = promo.get("一进二")
    if g12 is not None and (promo.get("一进二样本") or 0) >= 10:
        if g12 >= 50:
            a_score += 1; why.append(f"一进二{g12}%≥50(+1)")
        elif g12 < 30:
            a_score -= 1; why.append(f"一进二{g12}%<30(−1)")
    a_judge = "强" if a_score >= 2 else ("弱" if a_score <= -2 else "中")

    # ── 闸B：方向强度（2026-10-25 修口径：一票一归属的「归因」计数，与盘口表默认分组同源）──
    # 旧版用 ladder 挂靠计数（一票挂多标签重复计）：机器人链 45 家，而盘口表归因链只有 20 只 —
    # 两处数字打架；且「扩散」拿合成环节键（机器人链·本体）去概念表找成员，永远 0 只。
    # 现在：链/环节/题材全部对涨停行画像计数，每票恰好一次；ladder 不再参与方向榜。
    prof = lambda r: (r.get("画像") or {})
    def _cnt(keyfn):
        c2 = {}
        for r in (pk_rows or []):
            k = keyfn(r)
            if k:
                c2[k] = c2.get(k, 0) + 1
        return c2
    c_chain = _cnt(lambda r: prof(r).get("chain_only"))
    c_seg = _cnt(lambda r: prof(r).get("chain"))
    c_theme = _cnt(lambda r: prof(r).get("theme"))
    chain_top = [[k, v] for k, v in sorted(c_chain.items(), key=lambda kv: -kv[1])[:5]]
    seg_top = [[k, v] for k, v in sorted(c_seg.items(), key=lambda kv: -kv[1])[:5]]
    thm_top = [[k, v] for k, v in sorted(c_theme.items(), key=lambda kv: -kv[1])[:5]]
    top_chain = chain_top[0][0] if chain_top else ""
    t1 = chain_top[0][1] if chain_top else 0
    top_seg = seg_top[0][0] if seg_top else ""
    top_thm = thm_top[0][0] if thm_top else ""
    total = _num(zt_n, 0) or len(pk_rows or [])
    conc = round(t1 / total * 100, 1) if total else None
    # 阈值按新口径重标定（C 级）：top1 归因链 ≥22% 且 ≥8 只＝集中；<10% 或 ≤2 只＝分散
    if conc is None:
        b_judge = "—"
    elif conc >= 22 and t1 >= 8:
        b_judge = "集中"
    elif conc < 10 or t1 <= 2:
        b_judge = "分散"
    else:
        b_judge = "中"
    top3_list = [f'{k} {v}' for k, v in chain_top[:3]]
    # 龙头＝top 链内连板最高（并列取首封最早）；梯队＝该链涨停家数（肉肉：≥3 成立/2 偏薄/≤1 独木）
    ldr, ldr_key = None, None
    for r in (pk_rows or []):
        if prof(r).get("chain_only") != top_chain:
            continue
        key = (-int(_num(r.get("连板"), 1) or 1), str(r.get("首封") or "99:99:99"))
        if ldr is None or key < ldr_key:
            ldr, ldr_key = r, key
    ldr_name = (ldr or {}).get("name") or ""
    ldr_lb = int(_num((ldr or {}).get("连板"), 0) or 0)
    tier_txt = "成立" if t1 >= 3 else ("偏薄" if t1 == 2 else "独木难支")
    # 扩散度：top 归因题材的成员里今日涨超 5%（用真题材名查概念表，不再用合成环节键）
    diff_txt = ""
    if top_thm and con:
        members = [c2 for c2, ts in con.items() if top_thm in (ts or [])]
        dn = sum(1 for c2 in members if pct.get(c2, -99) > 5)
        diff_txt = f"成员{len(members)}只·涨超5% {dn}只"
    lonely = t1 <= 1 and ldr_lb >= 3

    # ── 组合判定灯（2×2，C 级）────────────────────
    if a_judge == "强" and b_judge == "集中":
        lamp = ("共振", "#4e7a33", "做主线核心／半路；仓位按熔炼相位表（发酵相唯一重仓窗口），A0 硬顶不动")
    elif a_judge == "强":
        lamp = ("群龙无首", "#a4742a", "普涨无方向：只做辨识度最高＋等收敛；涨停多≠赚钱，先看溢价")
    elif a_judge == "弱" and b_judge == "集中":
        lamp = ("孤军抱团", "#b4551f", "新周期候选：新题材/次日一进二有溢价/有中军容量，三条件全中才小仓试错(1–2成)；否则只登记不追")
    elif a_judge == "弱":
        lamp = ("退潮", "#a32d2d", "空仓／≤1成（熔炼：退潮≤10%）；逆势板块只登记不追")
    else:
        lamp = ("过渡", "#5a6270", "方向未决：等 10:30/13:00 时点确认；仓位保持防守")
    if a_judge == "弱":
        lamp = (lamp[0], lamp[1], lamp[2] + "。A 闸弱——熔炼环境总闸：极端时对 B 闸一票否决")

    # ── 背离细检（叠加在灯上）────────────────────
    flags = []
    if shape == "菱形（中位塌）":
        flags.append(f"菱形结构：高度升中位塌（3板+{f3}家 > 2板{f2}家），禁中位接力（肉肉：孤军奋战难成气候）")
    if 首板溢价 is not None and 连板溢价 is not None and 首板溢价 >= 1 and 连板溢价 <= -1:
        flags.append(f"溢价分离：首板+{首板溢价}%／连板{连板溢价}% → 只做首板试错，不做中位接力")
    if lonely:
        flags.append(f"龙头独走：{top_chain} 龙头{ldr_name}（{ldr_lb}板）但全链涨停仅 {int(t1)} 只——只持股不新开（独木难支）")
    if 大面 is not None and 大面 >= 3:
        flags.append(f"昨日涨停今日 ≤−8% 共 {大面} 家：打板容错差")

    return {
        "等级": "C级草案",
        "A": {"判定": a_judge, "分": a_score, "构成": "；".join(why),
              "涨停": zt_n, "炸板率": zb_rate, "回封率": refill_rate,
              "最高连板": sent.get("最高连板"), "连板家数": sent.get("连板家数"),
              "昨日涨停": promo.get("昨日涨停"), "昨日日期": prev_date,
              "一进二": g12, "一进二样本": promo.get("一进二样本"),
              "二进三以上": promo.get("二进三以上"), "连板晋级样本": promo.get("连板晋级样本"),
              "首板溢价": 首板溢价, "连板溢价": 连板溢价, "大面": 大面,
              "跌停": sent.get("跌停"), "封死跌停": sent.get("封死跌停"),
              "近5日均涨停": avg5, "今日倍数": mult,
              "梯队长": ladder_dist, "首封分布": fbt_list,
              "梯队明细": ladder_detail, "首封明细": fbt_detail, "首封未知明细": fbt_unk_list,
              "上午封板": am_n, "下午封板": pm_n, "首封未知": fbt_unk,
              "梯队形状": shape, "形状": [f1, f2, f3]},
        "B": {"判定": b_judge, "top": top_chain, "top涨停": int(t1) if t1 else 0,
              "集中度": conc, "top3": top3_list,
              "top链": f"{top_chain}（{t1}只）" if top_chain else "",
              "top环节": top_seg, "top题材": top_thm,
              "链榜": chain_top, "环节榜": seg_top, "题材榜": thm_top,
              "龙头": ldr_name, "龙头板": ldr_lb,
              "梯队判定": tier_txt, "中军": "",
              "扩散": diff_txt},
        "灯": {"名": lamp[0], "色": lamp[1], "动作": lamp[2]},
        "背离": flags,
        "note": "；".join(notes) if notes else "",
    }


if __name__ == "__main__":
    # 离线自检：直接用最近一次看板 state 复算，不联网。
    import urllib.request
    import datetime as _dt
    try:
        st = json.load(urllib.request.urlopen("http://127.0.0.1:6060/api/state", timeout=5))
    except Exception:
        import glob
        cands = sorted(glob.glob(os.path.join(ROOT, "cache", "intraday", "*", "*.json")))
        st = json.load(open(cands[-1], encoding="utf-8")) if cands else {}
    pool_dir = os.path.join(ROOT, "cache", f"pool_{st.get('data_date','')}")
    try:
        snap = json.load(open(os.path.join(pool_dir, _BIG_SNAP), encoding="utf-8"))
    except Exception:
        snap = []
    d = compute(st.get("sentiment"), st.get("ladder"), [], snap, {},
                _dt.datetime.now().strftime("%Y%m%d"))
    print(json.dumps(d, ensure_ascii=False, indent=1)[:2000])
