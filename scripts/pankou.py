#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘口角色 / 梯队 / 情绪 —— **唯一口径定义处**（2026-10-23）
================================================================
被两处共用，改动必须只改这里：
  · scripts/intraday_scan.py  → 盘中（实时快照 + 涨停池）
  · scripts/pool_pipeline.py  → 盘后（收盘快照 + 当日涨停池）写「昨日盘口档案」

量化自（C 级草案，读数是竞争结构的证据，不构成买卖建议）：
  肉肉 4.1 涨停启动时机（首封时间）／4.2 封板质量（封单、炸板）／
  5.1 梯队完整性（龙头-中军-跟风）／5.2 卡位竞争（总龙头 vs 卡位龙）／
  5.3 中军配合（板块内大市值红盘）／六② 情绪周期（连板高度、炸板率）

角色判据：
  题材内仅 1 只涨停 → **独苗**（不是竞争出来的龙头）
  ≥2 只 → 连板最高中封单占流通比最大 = **题材龙头**；
          其余里首封最早且连板低于龙头 = **卡位龙**；其余 = 跟风
梯队判据：
  涨停≥3 且中军(流通前3且红盘)≥1 → 成立；≥3 缺中军 → 缺中军；2 家 → 偏薄；≤1 → 独木难支
"""
import math


def _limit_th(code, name):
    """个股涨跌幅限制（用于把「跌停」判定做成与各板块一致的口径）。"""
    nm = (name or "").upper()
    if "ST" in nm:
        return 4.8
    if code.startswith(("30", "68")):
        return 19.7
    if code.startswith(("4", "8", "92")):
        return 29.7
    return 9.7


def _num(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


# ── K 线形态标签（2026-10-27 用户要求）───────────────────────────────
# 与「角色」分开：角色答「它在梯队里是什么身份」，形态答「这根 K 线长什么样」。
# 两者可同时成立（一字板 + 空间龙），所以不能塞进同一个字符串里排序。
FORM_PRI = ["一字板", "T字板", "地天板", "天地板", "回封", "竞价板", "尾盘板"]


def bar_forms(r, lim):
    """只用 open/high/low/price/prev_close 判形态。降级源（新浪）缺字段时返回 []——
    宁可没有标签，也不许拿 pct 猜「是不是一字」。"""
    p = _num(r.get("price"))
    pc = _num(r.get("prev_close"))
    h, l, o = _num(r.get("high")), _num(r.get("low")), _num(r.get("open"))
    if not (p and pc and h and l):
        return []
    tol = max(round(p * 0.0015, 3), 0.01)
    up = round(pc * (1 + lim / 100.0), 2)
    dn = round(pc * (1 - lim / 100.0), 2)
    zt = p >= up - tol                    # 收在涨停（含 0.15% 容差：四舍五入到分）
    dt = p <= dn + tol                    # 收在跌停
    out = []
    if zt and abs(h - l) <= tol:
        out.append("一字板")               # 全天一个价 = 开盘即封死，没有波动
    elif zt and o is not None and abs(o - up) <= tol and l < up - tol:
        out.append("T字板")                # 开盘涨停 → 盘中打开 → 收盘回封
    if zt and l <= dn + tol:
        out.append("地天板")               # 最低摸跌停，收盘涨停
    if dt and h >= up - tol:
        out.append("天地板")               # 最高摸涨停，收盘跌停
    return out


def _fbt_forms(fbt, zbc):
    """时间形态：竞价封板 / 尾盘封板 / 回封（炸板后重新封上）。fbt 为 HHMMSS。"""
    out = []
    if zbc and _num(zbc, 0) > 0:
        out.append("回封")
    f = _num(fbt)
    if f and f > 0:
        if f <= 92500:
            out.append("竞价板")            # 09:25 集合竞价即封死
        elif f >= 143000:
            out.append("尾盘板")            # 14:30 后才封，接力意愿弱
    return out


def locate_theme(tags, theme, active_themes, chain_of, hy=""):
    """题材定位（2026-10-25 用户指出创维数字错标黑色家电后重构）——四层优先级：
      1. 主归属题材归链（这只票为什么涨停的答案）
      2. 票的题材标签里属于「活跃题材」（今天有涨停）且能归链的
      3. 任意能归链的标签
      4. 题材·行业（兜底；hy 行业可能与业务错位，如创维数字被归黑色家电但实际做 XR/机顶盒）
    返回 链·环节·题材·行业 字符串，缺项省略。"""
    tags = tags or []

    def join(ch, sg, t):
        # 「（链级·需下钻）」是环节字段的占位提示，不是真实环节——显示时省略
        if sg and sg.startswith("（"):
            sg = ""
        return "·".join(x for x in (ch, sg, t, hy) if x)
    if theme:
        ch, sg = chain_of(theme) or ("", "")
        if ch:
            return join(ch, sg, theme)
    for t in tags:
        if t in active_themes:
            ch, sg = chain_of(t) or ("", "")
            if ch:
                return join(ch, sg, t)
    for t in tags:
        ch, sg = chain_of(t) or ("", "")
        if ch:
            return join(ch, sg, t)
    t0 = theme or (tags[0] if tags else "")
    return "·".join(x for x in (t0, hy) if x) or "其他"


def roles_ladders(zt, zb, dtp, rows, focus_idx, t2codes, seg2codes, live_rows=None,
                  live_pct=None, fmcap=None, attr_opt=None, chain_of=None, block=(),
                  main_attr=None, uni_con=None):
    """角色 / 梯队 / 情绪（2026-10-23 第 2 版：**一票多标签**）。

    ★ 角色不是互斥单选——同一只票可以同时是「空间龙 + 板块龙头 + 早盘板 + 换手龙」。
    行业口径:空间龙(全市场高度)、板块总龙头(题材高度)、日内龙(最先涨停)、助攻(同批封板)、
    补涨(龙头高位后的低位首板)、换手龙(充分换手走出来)、一字龙(缩量一字,开板易崩)、
    跟风(被动跟随)。中军是大市值**不必涨停**的压舱石,单独在梯队里给名字。

    标签判据（全部可观测，C 级草案）：
      空间龙   全市场连板最高（并列取首封最早）
      总龙头   该题材连板最高（并列取首封最早）
      日内龙   该题材当日首封最早
      早盘板   首封 ≤ 10:30（龙头识别要素之一：越早封板资金共识越强）
      助攻     龙头封板后 30 分钟内跟进封板（最多 2 只）
      补涨     龙头 ≥5 板时题材内首板票
      换手龙   连板≥2 且 换手率 15%—30%（放量换手走出来，持续性更强）
      一字龙   换手 <3% 且 封单占流通比 >1%（筹码断层，开板即见顶风险）
      天量换手 换手 >20%（主力出货嫌疑，警示标签）
      独苗 / 跟风  兜底分类
      ★ 卡位龙（老龙头断板日逆势抢涨停夺位者）需前一交易日龙头断板数据，日频不可靠 → 不标。

    返回 (role_of, ladder, sentiment, main_theme)：
      role_of {code: {"tags":[...], "theme":主判定题材, "note":备注}}   tags 按优先级排序
      ladder  {场景名: {"涨停"(主归属口径)/"涨停标签"(标签口径,仅参考)/"中军"/"中军名"/
                       "判定"/"口径"(题材|环节)/"龙头"/"龙头板"/"龙头炸板"}}
      main_theme {code: 主归属题材}   **唯一**归属，非涨停票也有——下游一律用它建场景
      ⚠ t2codes 参数保留只为兼容既有调用方；2026-10-24 起桶成员不再用标签口径
        （标签口径会把一只票算进它挂的每个题材，是「龙头张冠李戴」的根源）。
    """
    live_pct = live_pct or {r["code"]: r.get("pct") for r in rows}
    fmcap = fmcap or {r["code"]: (r.get("fmcap") or 0) for r in rows}
    zt_by_code = {r.get("c"): r for r in zt if r.get("c")}
    for z in zt:
        z["_ratio"] = (_num(z.get("fund"), 0) / max(_num(z.get("ltsz"), 1), 1)) * 100

    # ── 情绪条 ──
    lbcs = [(_num(r.get("lbc"), 1) or 1) for r in zt]
    sealed_zb = len(zb)
    refill = sum(1 for z in zt if (_num(z.get("zbc"), 0) or 0) > 0)
    broke_all = sealed_zb + refill
    lim = {r["code"]: _limit_th(r["code"], r.get("name")) for r in rows}
    dn = sum(1 for c, pv in live_pct.items()
             if isinstance(pv, (int, float)) and pv <= -(lim.get(c, 9.7)))
    sentiment = {"涨停": len(zt), "炸板": broke_all, "炸板池": sealed_zb, "回封": refill,
                 "跌停": dn, "封死跌停": len(dtp),
                 "炸板率": round(broke_all / max(len(zt) + sealed_zb, 1) * 100, 1),
                 "最高连板": max(lbcs) if lbcs else 0,
                 "连板家数": sum(1 for x in lbcs if x >= 2)}

    # ── code → 题材（主归属候选源）──
    # uni_con（2026-10-25）：个股大表的「有效概念」{code: [概念...]}。传入时**优先用它**——
    #   rows['题材'] 来自东财串口径，北交所全空、且缺东财自家没有的板块（智能物流/机器视觉等）；
    #   大表是东财 F10 BOARDTYPE ＋ 同花顺概念的双源并集，5576 只 99.7% 有有效概念。
    #   该票不在大表（或有效概念为空）→ 回退 rows['题材']，盘后 pool_pipeline 不传 → 行为不变。
    code2themes = {}
    for r in rows:
        _u = (uni_con or {}).get(r["code"])
        for t in (_u if _u else (r.get("题材") or [])):
            if t in (block or ()):   # 行业名/事件噪声不配当主归属候选（2026-10-24 Step4）
                continue
            code2themes.setdefault(r["code"], []).append(t)
    # 旧兜底已删（2026-10-24 Step4）：题材为空时拿涨停行自带的 hybk 行业名当题材——
    # 实测把大鹏工业的主归属写成「专用设备」（申万行业名冒充题材，梯队/独苗全建立在假题材上）。
    # 行业名不是资金在炒的题材；没有题材就老实留空，主归属列显示「—」、梯队「未成梯队」。

    # ── 多标签容器 ──
    TAG_PRI = ["空间龙", "总龙头", "板块龙头", "首板龙头", "日内龙", "早盘板", "助攻", "补涨",
               "换手龙", "一字龙", "天量换手", "独苗", "跟风"]
    bags = {}

    def add(c, tag, theme=None, note=None):
        b = bags.setdefault(c, {"tags": [], "theme": "", "note": ""})
        if tag not in b["tags"]:
            b["tags"].append(tag)
        if theme and not b["theme"]:
            b["theme"] = theme
        if note:
            b["note"] = note

    # ── 空间龙（跨题材）──
    market_top = sorted(zt, key=lambda z: (-(_num(z.get("lbc"), 1) or 1),
                                           _num(z.get("fbt"), 999999)))
    space_lb = (_num(market_top[0].get("lbc"), 1) or 1) if market_top else 0
    space_codes = {z["c"] for z in market_top
                   if (_num(z.get("lbc"), 1) or 1) == space_lb}

    # ── 每个有涨停的题材内部判（可叠加多个标签）──
    theme_zts = {}
    for c, z in zt_by_code.items():
        for t in (code2themes.get(c) or []):
            theme_zts.setdefault(t, []).append(z)
    # ★ 主归属题材（2026-10-23 第3版）：一只票常挂 5—7 个题材标签，若在每个题材里都判身份，
    #   会同时得到「A 题材龙头 + B 题材跟风 + 独苗」一堆矛盾标签（实测独苗 51/跟风 57/总龙头 47 全泛滥）。
    #   行业做法是**只认一个主归属题材**：取该票所属题材中涨停家数最多的（信息量最大、梯队最完整），
    #   只在这个题材里判身份；跨题材的全局标签（空间龙）与资金属性标签（换手/一字/天量）另叠加。
    theme_size = {t: len(z) for t, z in theme_zts.items()}
    # ★ 主归属扩到「全票池」（2026-10-24 第4版，用户报龙头张冠李戴后修）
    #   原版只给**涨停票**算 main_theme，非涨停票没有归属 → 补涨表只能按「标签全集」建场景，
    #   于是一只票的 6 个标签各开一个场景，龙头取自别的场景（上海九百 成了固态电池/存储芯片的龙头）。
    #   现在同一只票无论是否涨停，都按同一条规则取**唯一**主归属：
    #     ① 候选＝该票标签中「当日有涨停」的题材（今日无涨停的题材不构成当下逻辑）
    #     ② 候选＝涨停 ≥min_n 家的题材（默认 1；1 家的桶照实标「独木难支」，且不进补涨场景）
    #     ③ **能归产业链的题材优先**（prefer_chain）：「固态电池」压过「长江三角」、
    #        「人形机器人」压过「央国企改革」；属地/政策/重组类泛标签不抢身份
    #     ④ 同级里取涨停家数最多者；并列取覆盖更窄的（更具体）、再按名字稳定序
    #   ③ 是关键：旧规则纯比家数，长江三角（13 家/408 只）永远压住固态电池（7 家/217 只），
    #   于是全市场的上海本地股都被判成长三角的兵——这正是用户看到「固态电池的票挂长江三角龙头」的原因。
    #   ⚠ 不用涨停率：实测会让「土地流转」这类 2 只涨停的小标签反超「人形机器人」。
    AO = {"prefer_chain": True, "min_n": 1}
    AO.update(attr_opt or {})
    min_n = max(int(AO.get("min_n") or 1), 1)
    _co = chain_of or (lambda t: ("", ""))          # 未注入 → prefer_chain 天然失效（不猜链）
    if chain_of is None:
        AO["prefer_chain"] = False
    breadth = {}
    for r in rows:
        for t in set(r.get("题材") or []):
            breadth[t] = breadth.get(t, 0) + 1

    def _main_of(code):
        cands = [t for t in (code2themes.get(code) or []) if theme_size.get(t, 0) >= min_n]
        if not cands:
            return ""
        if AO.get("prefer_chain"):
            hot = [t for t in cands if _co(t)[0]]
            cands = hot or cands
        # ── 链级大标签让位（2026-10-25，大表接入后新增）─────────────────────
        #   个股大表把概念补全了，候选里随之混进『人工智能』『新能源汽车』这类 1000+ 只的
        #   链级大标签（chain_of 返回 环节='（链级·需下钻）'）。它们成分最多、涨停家数天然最高，
        #   按家数排必然压过真环节——实测密尔克卫被判成『人工智能』，可它当天涨的是智能物流。
        #   这与 ③「属地/政策/重组类泛标签不抢身份」是同一条原则，只是把判据从「有链」收紧到
        #   「有真环节」：**只在没有任何环节级候选时，链级大标签才顶上来**（否则该票真没归属）。
        if AO.get("prefer_seg", True):
            seged = [t for t in cands
                     if _co(t)[1] and not str(_co(t)[1]).startswith("（链级")]
            if seged:
                cands = seged
        # ── 同链·同环节取更窄（2026-10-25，大表接入后新增）───────────────────
        #   大表补进了『机器人概念』（1290 只）这种宽桶，实测把赛福天/天奇股份的
        #   『人形机器人』（488 只）挤掉——同一条链同一个环节，窄桶的信息量更高。
        #   前提：窄桶自己的涨停家数 ≥ 同环节最宽桶的 50%，否则那是「没人炒的冷标签」抢身份。
        if AO.get("prefer_narrow", True) and len(cands) > 1:
            grp = {}
            for t in cands:
                grp.setdefault((_co(t)[0], _co(t)[1]), []).append(t)
            pick = []
            for ts in grp.values():
                if len(ts) == 1:
                    pick.append(ts[0])
                    continue
                top = max(theme_size.get(t, 0) for t in ts)
                wide = sorted(ts, key=lambda t: (-theme_size.get(t, 0), t))[0]
                narrow = sorted(ts, key=lambda t: (breadth.get(t, 0), t))[0]
                pick.append(narrow if (narrow != wide
                                       and theme_size.get(narrow, 0) >= top * 0.5) else wide)
            cands = pick
        # ── 归因校正（2026-10-25 修正版）：只在「归因题材也是当天的大桶」时才让位 ──
        #   第一版（已废弃）只要归因是「主业驱动 ≥55」就改判，实测把南方路机从机器人概念（26 家）
        #   按回工程机械概念（2 家）、赛福天从人形机器人按回 TOPCon——**用主业压当天的资金事实**，
        #   用户直接指出不对：南方路机今天最相关的是机器人和 AI 智能体。
        #   修正后的门槛：归因题材必须**同时**满足 ①主业驱动 ≥55 ②它的当日涨停家数不低于
        #   候选里最大桶，才允许改判。这样「自己的生意恰好也是当天主线」才生效（如医美线集体
        #   涨停时的朗姿），而「主业那条线今天没人炒」时仍按资金在哪里判。
        am = (main_attr or {}).get(code)
        if am and AO.get("attr_override") and (am.get("score") or 0) >= 55 \
                and am.get("layer") == "主业驱动" and am.get("theme") in cands:
            top_n = max(theme_size.get(t, 0) for t in cands)
            if theme_size.get(am["theme"], 0) >= top_n:
                return am["theme"]
        return sorted(cands, key=lambda t: (-theme_size[t], breadth.get(t, 0), t))[0]

    main_theme = {}
    for r in rows:
        mt = _main_of(r.get("code"))
        if mt:
            main_theme[r["code"]] = mt
    for theme, zts in theme_zts.items():
        # 只处理「本票主归属＝该题材」的票
        zts = [z for z in zts if main_theme.get(z["c"]) == theme]
        if not zts:
            continue
        top_lb = max((_num(z.get("lbc"), 1) or 1) for z in zts)
        heads = sorted([z for z in zts if (_num(z.get("lbc"), 1) or 1) == top_lb],
                       key=lambda z: _num(z.get("fbt"), 999999))
        head = heads[0] if heads else None
        first = min(zts, key=lambda z: _num(z.get("fbt"), 999999))
        head_fbt = _num(head.get("fbt"), 999999) if head else 999999
        if len(zts) == 1:
            add(zts[0]["c"], "独苗", theme)
        else:
            if head:
                # 高度分级（行业口径）：≥5 板＝总龙头，2—4 板＝板块龙头，1 板＝首板龙头
                htag = "总龙头" if top_lb >= 5 else ("板块龙头" if top_lb >= 2 else "首板龙头")
                add(head["c"], htag, theme)
                if _num(head.get("fbt"), 999999) <= 103000:      # 早盘板（≤10:30）
                    add(head["c"], "早盘板", theme)
            add(first["c"], "日内龙", theme)
            n_as = 0
            for z in sorted(zts, key=lambda z: _num(z.get("fbt"), 999999)):
                c = z["c"]
                if c == (head or {}).get("c") or c == first["c"] or n_as >= 2:
                    continue
                fbt = _num(z.get("fbt"), 999999)
                if head_fbt <= fbt <= head_fbt + 3000:
                    add(c, "助攻", theme)
                    n_as += 1
            if top_lb >= 5:
                for z in zts:
                    if (_num(z.get("lbc"), 1) or 1) == 1:
                        add(z["c"], "补涨", theme)
            for z in zts:
                if z["c"] not in bags:
                    add(z["c"], "跟风", theme)

    # ── 资金属性标签（对每只涨停票）：换手龙 / 一字龙 / 天量换手 ──
    for z in zt:
        c, hs = z.get("c"), _num(z.get("hs"), 0) or 0
        lb = _num(z.get("lbc"), 1) or 1
        if hs > 20:
            add(c, "天量换手", note=f"换手 {hs:.0f}% 出货嫌疑")
        elif lb >= 2 and 15 <= hs <= 30:
            add(c, "换手龙")
        if hs < 3 and z["_ratio"] > 1 and lb >= 2:
            add(c, "一字龙", note=f"换手仅 {hs:.1f}% 筹码断层")

    # ── K 线形态（2026-10-27）：与角色并列，不参与角色排序 ──
    #   一字板 ≠ 一字龙：前者是「开=高=低=收」的价格事实（首板也会出现，是隔夜资金一致预期），
    #   后者是「换手<3% + 封单厚 + 连板≥2」的资金属性判断。两者会同时成立，也会分开成立。
    # 形态要靠开高低——股池 rows 是静态画像，没有 OHLC；必须并入实时快照（live_rows）
    row_of = {r["code"]: dict(r) for r in rows}
    for _c, _v in (live_rows or {}).items():
        if isinstance(_v, dict):
            row_of.setdefault(_c, {}).update(_v)
    forms = {}
    for z in zt:
        c = z.get("c")
        if not c:
            continue
        _lim = lim.get(c) or _limit_th(c, z.get("n"))
        fs = bar_forms(row_of.get(c) or {}, _lim) + _fbt_forms(z.get("fbt"), z.get("zbc"))
        if fs:
            forms[c] = sorted(set(fs), key=lambda t: FORM_PRI.index(t) if t in FORM_PRI else 9)

    # ── 龙头成色分（0—100，多维度，只作读数）：区分「6 板但很虚」与「2 板但很实」──
    def score_of(z, theme_n):
        lb = _num(z.get("lbc"), 1) or 1
        hs = _num(z.get("hs"), 0) or 0
        fbt = _num(z.get("fbt"), 999999)
        sc = 0
        sc += min(lb * 8, 32)                                   # 身位（高度）32
        sc += min(z["_ratio"] * 8, 24)                          # 封单占流通比（3% 满分）24
        sc += max(0, 16 - int(_num(z.get("zbc"), 0) or 0) * 3)   # 炸板扣分 16
        sc += 14 if 5 <= hs <= 15 else (8 if 15 < hs <= 20 else (5 if hs < 5 else 0))  # 换手健康 14
        sc += 10 if fbt <= 100000 else (6 if fbt <= 103000 else 2)  # 首封时间 10
        sc += min(theme_n * 2, 4)                               # 板块带动性（题材涨停家数）4
        return int(min(sc, 100))
    for z in zt:
        c = z.get("c")
        if c in bags:
            bags[c]["score"] = score_of(z, theme_size.get(main_theme.get(c), 0))

    # ── 空间龙最后加（优先级最高），并按 PRI 排序 ──
    for c in space_codes:
        add(c, "空间龙", note=f"全市场最高 {int(space_lb)} 板")
    role_of = {c: {"tags": sorted(b["tags"], key=lambda t: TAG_PRI.index(t) if t in TAG_PRI else 9),
                   "theme": b["theme"], "note": b["note"], "score": b.get("score"),
                   "form": forms.get(c, [])}
               for c, b in bags.items()}

    # ── 梯队（含中军/龙头）：**一桶一口径**（2026-10-24 第2版，用户报龙头张冠李戴后重写）──
    #   旧版两个致命问题，合起来就是「题材龙头全变成上海九百」：
    #     ① 家数用**标签口径**（t2codes）、龙头用**主归属口径**（theme_head）——两个分母。
    #        题材 A 的涨停家数里混着主归属在 B 的票，A 的龙头却只从「主归属=A」里挑。
    #     ② 补涨表按 ladder 的 key 取场景（标签全集 key），theme_head 的 key 是主归属
    #        → 交集极小：实测 30 行补涨里 20 行龙头「—」，剩下的来自别的题材。
    #   现在桶成员只有一种定义：
    #     · 题材桶 = **主归属该题材**的票（一票一桶，不重复计数）
    #     · 环节桶 = 该链·环节的成员（业务位置，按标签归链；一票可落多个环节）
    #   桶内一切读数（涨停/中军/龙头/判定）都从同一批成员出，不再跨口径拼装。
    name_of = {r["code"]: r["name"] for r in rows}

    def _bucket(name, basis=""):
        """场景名 → (成员 code 集合, 口径)。唯一入口，外部用 bucket_members() 复用。"""
        if basis == "环节" or (not basis and "·" in name and name.split("·")[0].endswith("链")):
            ch, sg = name.split("·", 1)
            return set(seg2codes.get((ch, sg), [])), "环节"
        return {c for c, t in main_theme.items() if t == name}, "题材"

    def _fill(name, basis=""):
        # 两套口径各管一个问题，**同一个格子里只出现一套**：
        #   · 主归属口径（codes）＝「这只票的涨停归谁解释」→ 盘口/候选/补涨（个股视图）
        #   · 篮子口径（tag_codes）＝「这条主线今天有多少兵」→ 焦点实时榜（主线视图）
        # 2026-10-24 二修：一修时把焦点表也换成主归属，结果 48 条焦点里 19 条显示
        # 「0家(标签3)·无主归属」（减速器/机器视觉…都是机器人主线的分支）——
        # 分支的兵被更宽的题材认领，不代表这条主线没兵。焦点表本来就该用篮子。
        codes, basis = _bucket(name, basis)
        tag_codes = set(t2codes.get(name, [])) if basis == "题材" else codes

        def _stat(cs):
            zts = [zt_by_code[c] for c in cs if c in zt_by_code]
            big = sorted([c for c in cs if c in live_pct],
                         key=lambda c: -(fmcap.get(c) or 0))[:3]
            mids = [name_of.get(c, c) for c in big
                    if c not in zt_by_code and (_num(live_pct.get(c), -1) or -1) > 0]
            n, nm = len(zts), len(mids)
            j = ("成立" if n >= 3 and nm >= 1 else
                 "缺中军" if n >= 3 else
                 "偏薄" if n == 2 else
                 "独木难支" if n == 1 else "无涨停")
            # 龙头＝桶内连板最高（并列取首封最早），与「总龙头/板块龙头/首板龙头」同一条规则
            hd = sorted(zts, key=lambda z: (-(_num(z.get("lbc"), 1) or 1),
                                            _num(z.get("fbt"), 999999)))[0] if zts else {}
            return zts, n, nm, mids, j, hd

        zts, n_zt, n_mid, mid_names, judg, head = _stat(codes)
        _z2, n_bt, n_bmid, bmid_names, bjudg, _h2 = _stat(tag_codes)
        base = {"涨停": n_zt, "涨停标签": n_bt,
                "中军": n_mid, "中军名": "、".join(mid_names),
                "中军标签": n_bmid, "中军名标签": "、".join(bmid_names),
                "判定": judg, "判定标签": bjudg, "口径": basis}
        if not zts:
            return dict(base, **{"龙头": "", "龙头板": 0, "龙头炸板": 0})
        n_follow = sum(1 for z in zts
                       if set(role_of.get(z["c"], {}).get("tags", [])) & {"跟风", "补涨", "独苗", "首板龙头"})
        return dict(base, **{"跟风": n_follow,
                             "龙头": head.get("n") or "",
                             "龙头板": int(_num(head.get("lbc"), 1) or 1),
                             "龙头炸板": int(_num(head.get("zbc"), 0) or 0)})

    ladder = {}
    for e in focus_idx:
        ladder[e[1]] = _fill(e[1], e[0])
    # ── 补全：所有**有涨停但不在焦点里**的题材也算梯队（2026-10-24 用户要求）──
    #   否则盘口表里非焦点题材（机器人概念/小米概念…）的「该题材梯队」全是「—」。
    #   只补**主归属口径有票**的：标签有 12 家、主归属 0 家的题材（如「虚拟现实」）
    #   是一个没有主体的空壳，补进 ladder 只会让补涨表按它开场景。
    for theme in theme_zts:
        if theme in ladder:
            continue
        L = _fill(theme)
        if not L["涨停"]:
            continue
        ladder[theme] = L
    return role_of, ladder, sentiment, main_theme


def bucket_members(name, basis, main_theme, seg2codes):
    """场景名 → 成员 code 集合。**梯队的唯一口径**，供补涨/候选等下游复用。

    题材桶＝主归属该题材的票（一票一桶）；环节桶＝该链·环节成员（按标签归链）。
    下游不得再用「标签全集」自建场景，否则又会出现「场景一个题材、龙头另一个题材」。
    """
    if basis == "环节" and "·" in name:
        ch, sg = name.split("·", 1)
        return set(seg2codes.get((ch, sg), []))
    return {c for c, t in main_theme.items() if t == name}

# ── 下一棒候选（2026-10-27 用户需求）────────────────────────────
# 问题：盘口表全是已涨停的票，看到时已买不到。需要同链/环节/题材里「还没涨停但在动」的。
# 口径来源（不自创）：
#   233战法：半路 > 低吸 > 埋伏；共振四条件＝板块在涨 + 核心在动 + 量能在放 + 股价在突破；
#            第一核心错过就看第二、第三核心。
#   肉肉：板块容量——只有一只涨是孤军奋战；低位启动优于高位接力；换手温和最佳。
# 因此打分只做四件事：桶内相对强度、量能、突破、成交额辨识度；不预测方向、不给买卖建议。
def follow_candidates(members, snap, zt_codes, lim_map=None, limit=4):
    """members: 桶内代码集合；snap: {code: 实时行}；zt_codes: 已涨停代码集"""
    cand = []
    for c in members:
        if c in zt_codes:
            continue
        r = snap.get(c)
        if not r or "ST" in str(r.get("name", "")).upper():
            continue
        pct = _num(r.get("pct"))
        amt = _num(r.get("amount"), 0) or 0
        lb = _num(r.get("liangbi"), 0) or 0
        price=_num(r.get("price"));prev=_num(r.get("prev_close"))
        up=_num((lim_map or {}).get(c)) or _num(r.get("up_limit")) or _num(r.get("limit_up"))
        if up and prev and prev>0:
            ceiling=(up/prev-1)*100*0.94
        else:
            # No real limit metadata: conservative compatibility cap, never claim exact distance.
            ceiling=9.4
        if price is None or price<=0 or (up and price>=up):continue
        if pct is None or pct < 1.5 or pct > ceiling:      # 没动(<1.5%) 或已接近涨停(>9.4%) 都不算"能上车的下一棒"
            continue
        if amt < 3e7 or lb < 1.2:
            continue
        hi, pc, op = _num(r.get("high")), _num(r.get("prev_close")), _num(r.get("open"))
        why = []
        if hi and pc and hi > pc:
            why.append("日内新高") if _num(r.get("price")) and abs(_num(r.get("price")) - hi) < 1e-6 else None
        if pc and _num(r.get("price")) and _num(r.get("price")) > pc:
            why.append("破昨收")
        if op and pc and op > pc:
            why.append("高开")
        if lb >= 2:
            why.append(f"量比{lb:.1f}")
        if amt >= 1e8:
            why.append(f"额{amt/1e8:.1f}亿")
        # 打分：相对涨幅(40) + 量比(25) + 突破(20) + 成交额(15)
        sc = min(pct / max(ceiling,1), 1) * 40 + min(lb / 3, 1) * 25 + (20 if "日内新高" in why else 0) + min(amt / 5e8, 1) * 15
        cand.append({"code": c, "name": r.get("name") or c, "price": _num(r.get("price")), "pct": round(pct, 2),
                     "liangbi": round(lb, 2), "amount": round(amt / 1e8, 2),
                     "quote_time": r.get("quote_time"), "rule_version":"candidate-observation-v2",
                     "why": "·".join(why[:3]), "score": round(sc, 1)})
    cand.sort(key=lambda x: -x["score"])
    return cand[:limit]
