#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中四时点扫描（233 日内资金选择时钟 · 熔炼系统 3.5 日内资金选择时钟 的复核工具）
================================================================
定位
  「确认 1（板块确认）/ 确认 2（龙头确认）」的**复核器**，不是选股器：
  盘前股池已经把全市场压到 A0 的 48 条焦点与核心/备选候选池，
  本脚本在盘中任一时点跑一条命令，输出这三块——
    ① 焦点实时榜：48 条此刻的等权涨 / 超额 / 涨停 / 量能（相对全市场实时中位）
    ② 在动候选：核心+备选池里涨幅或量比过阈值的票
    ③ 同向性检查：盘前焦点序 vs 此刻实时序的重合度（异向 = 派发嫌疑读数）
  四时点各落一份 JSON，天然形成日内序列，盘后复盘直接引用。

边界（必读）
  1. 免费源 3—5 秒级延迟 + 手动触发 → 做「确认复核」，**做不了「抢单」**；
     半路下单那一下仍以行情软件为准。
  2. 只输出池内。池外今天涨得再猛也不出现——这是股池前置纪律（3.6 出口规则二），
     不是漏；池外异动只在同向性检查里以汇总数字出现。
  3. 程序只产读数，不产生任何买卖判断；含免责声明输出。

用法
  python scripts/intraday_scan.py                  # 自动吸附最近时点
  python scripts/intraday_scan.py --time 1030      # 显式时点
  python scripts/intraday_scan.py --html           # 顺带出四时点累积对照 HTML
  python scripts/intraday_scan.py --pool-dir cache/pool_20261019
  python scripts/intraday_scan.py --dry-run        # 用缓存收盘数据模拟（盘后/周末自测）

原则
  取不到就留空并标注（DATA_SOURCE_POLICY），绝不编造；失败写 ⚠ 降级行。
"""
import argparse, json, os, sys, time, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_pipeline as P          # 复用取数源 / chain_of / 免责声明
import pankou as PK                # 角色/梯队/情绪/定位 唯一口径定义处
import theme_attr as TA            # 行业三级名全集（主归属候选剔行业名用）
import theme_universe as TU        # 个股大表（2026-10-25：主归属/归因的概念覆盖源）

ROOT = P.ROOT


def _ind_names(rows, pool_dir, cfg, universe=None):
    """申万行业名全集（一级+二级+三级）——主归属候选的排除名单。
    rows['行业标签'] 只存一级名（实测：四通股份=['轻工制造']，漏掉 家居用品/其他家居用品），
    所以行业名全集必须从 themes.json 的三层结构取（theme_attr.load_tag_map 的 ind 字典）。
    universe（2026-10-25）：大表的申万一二三级——北交所串口径全空，只有大表补得上。"""
    names = {x for r in rows for x in (r.get("行业标签") or []) if x}
    try:
        _, ind, _ = TA.load_tag_map(pool_dir, cfg, P.classify_tags, universe=universe)
        for v in (ind or {}).values():
            names.update(x for x in (v or []) if x)
    except Exception:
        pass                          # 取不到就用 rows 兜底——宁少剔不误剔
    if universe:
        for v in universe.values():
            if isinstance(v, dict):
                names.update(x for x in (v.get("申万") or []) if x)
    return names

# ── 时点定义（3.5 日内资金选择时钟）───────────────────────────
SLOTS = [
    ("0925", "9:25 最初选择 —— 集合竞价给出的当天第一个资金方向（竞价读数，量比字段可能异常）",
     (9 * 60 + 15, 9 * 60 + 40)),
    ("1030", "10:30 扛住分歧 —— 第一轮博弈后谁还在榜上，谁被摁下去了",
     (10 * 60 + 15, 10 * 60 + 45)),
    ("1300", "13:00 回流或新方向 —— 午后资金是延续上午的选择，还是切了方向",
     (12 * 60 + 50, 13 * 60 + 15)),
    ("1430", "14:30 尾盘选择与次日预期 —— 决定持仓过不过夜的时点",
     (14 * 60 + 15, 14 * 60 + 45)),
]


def slot_of(hhmm):
    """分钟数 → (slot名, 提示语, 是否标准时点)。窗口外吸附最近的标准时点。"""
    best, bd = None, 10 ** 9
    for name, hint, (a, b) in SLOTS:
        if a <= hhmm <= b:
            return name, hint, True
        for p in (a, b):
            d = abs(hhmm - p)
            if d < bd:
                best, bd = (name, hint), d
    return best[0], best[1], False


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def med(vals):
    vs = sorted(v for v in vals if isinstance(v, (int, float)))
    return vs[len(vs) // 2] if vs else None


def strip_suffix(name, suffixes):
    for s in suffixes:
        if name.endswith(s) and len(name) > len(s) + 1:
            return name[: -len(s)]
    return name


def board_zljlr(bname, boards, suffixes):
    """题材名 → 板块榜主力净流入(亿)。三级匹配，匹配方式入表；匹配不上留空。"""
    if not boards:
        return None, "（板块榜不可用）"
    for how, cand in (("精确", (bname,)),
                      ("归一", (strip_suffix(bname, suffixes),)),
                      ("包含", tuple(x for x in boards if x and (x in bname or bname in x)))):
        for c in cand:
            if c in boards and isinstance(boards[c], (int, float)):
                return round(boards[c] / 1e4, 2), how
    return None, "无读数"


def latest_pool_dir(pool_dir):
    """未显式指定时，取 cache/pool_* 里 (data_date, run_at) 最新的一个。"""
    if pool_dir:
        return pool_dir if os.path.isabs(pool_dir) else os.path.join(ROOT, pool_dir)
    cdir = os.path.join(ROOT, "cache")
    best, key = None, ""
    for d in sorted(os.listdir(cdir)):
        p = os.path.join(cdir, d)
        if not (d.startswith("pool_") and os.path.isdir(p)):
            continue
        if d.endswith("_intraday"):
            continue      # 盘中缓存（--intraday）不是正式股池；其 data_date/run_at 更新，会被误当「最新」
        try:
            s = json.load(open(os.path.join(p, "summary.json"), encoding="utf-8"))
        except Exception:
            continue
        # A concurrent pool job writes summary before focus_index. Do not select
        # half-published directories as a complete board input.
        if not s.get('focus_index') or not os.path.isfile(os.path.join(p, 'pool_final.json')):
            continue
        k = f"{s.get('data_date','')}/{s.get('run_at','')}"
        if k > key:
            best, key = p, k
    return best


def take_snapshot(dry_run, pool_dir):
    """实时全A快照。dry-run 用缓存收盘数据模拟（明确标注，不冒充实时）。"""
    if dry_run:
        f = os.path.join(pool_dir, "all_a_snapshot.json")
        rows = json.load(open(f, encoding="utf-8"))
        return rows, "模拟（缓存收盘数据，非实时）", [], True
    # 2026-10-27：腾讯 qt 升为主源——东财 push2 经常连不上，降到新浪后没有 OHLC，
    # 一字板/T字板/地天板全判不出来。腾讯 0.8s 拉全 5,576 只且带开高低。
    from product_clock import market_coverage
    expected = P._universe_codes()
    rejected = []
    for src in ("tencent_qt", "eastmoney_push2", "eastmoney_push2_mirror", "sina_market_center"):
        rows = P.src_snapshot(src)
        coverage = market_coverage(rows or [], expected, src)
        if rows and coverage['complete']:
            rows = [{**r, 'src': src} for r in rows]
            warn = list(rejected)
            if src == "tencent_qt":
                warn.append("腾讯源：无行业/涨速（行业取知识库，涨速列留空）")
            if src == "sina_market_center":
                warn.append("⚠ 降级源：无 OHLC/行业/量比——一字板等形态判不出")
            return rows, src, warn, False
        rejected.append(f"{src}覆盖不足：{coverage['returned']}/{coverage['expected']}，未用于全市场统计")
        time.sleep(1)
    return [], "", rejected + ["⚠ 全部快照源失败——本次无实时读数，只有盘前静态数据"], False


def take_boards(cfg, dry_run):
    if dry_run:
        return {}, ["模拟模式：板块资金列留空"]
    for src in cfg.get("sources", {}).get("boards", ["tencent_proxy"]):
        b = P.src_boards(src, cfg)
        if b and any(b.values()):
            out = {}
            for layer, arr in b.items():
                for x in arr:
                    nm = x.get("name")
                    if nm:
                        out[nm] = fnum(x.get("zljlr"))
            return out, []
    return {}, ["⚠ 板块榜不可用：主力净流入列留空（不编造）"]


INDEX_LIST = [("sh000001", "上证"), ("sz399001", "深成"), ("sz399006", "创业板"),
              ("sh000688", "科创50"), ("sh000852", "中证1000"), ("bj899050", "北证50")]


def take_indices(dry_run=False):
    """大盘指数条（新浪 hq.sinajs，一次请求多标的）。返回 [{name, code, price, pct}]。
    取不到就返回空——不编造。"""
    if dry_run:
        return []
    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(c for c, _ in INDEX_LIST)
        r = P.S.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        out = []
        for line, (code, name) in zip(r.text.strip().split("\n"), INDEX_LIST):
            body = line.split('="', 1)[-1].rstrip('";')
            parts = body.split(",")
            if len(parts) < 4:
                continue
            try:
                prev, now = float(parts[2]), float(parts[3])
            except (ValueError, IndexError):
                continue
            if prev <= 0 or now <= 0:
                continue
            out.append({"code": code, "name": name, "price": round(now, 2),
                        "pct": round((now - prev) / prev * 100, 2)})
        return out
    except Exception:
        return []


def take_pools(date8, dry_run=False):
    """当日涨停/炸板/跌停池（盘中实时可用）。dry-run 返回空（不给假数据）。"""
    if dry_run:
        return [], [], []
    zt = P.src_zt_pool("eastmoney_ztpool", date8, "zt")
    zb = P.src_zt_pool("eastmoney_ztpool", date8, "zb")
    dtp = P.src_zt_pool("eastmoney_ztpool", date8, "dt")
    return zt, zb, dtp


def fmt_fbt(v):
    """东财首封时间 HHMMSS → HH:MM:SS"""
    s = str(int(v or 0)).zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:]}" if s != "000000" else "—"


def load_kprev(pool_dir):
    """从 kline_metrics.json 读昨日高/量（backfill 写入），供「破昨高」判据。
    返回 {code: {prev_high, prev_vol}}；文件缺失返回空 dict（判据退化为不可用，不编造）。"""
    p = os.path.join(pool_dir, "kline_metrics.json")
    if not os.path.exists(p):
        return {}
    try:
        km = json.load(open(p, encoding="utf-8"))
        return {c: {"prev_high": v.get("prev_high"), "prev_vol": v.get("prev_vol")}
                for c, v in km.items() if v.get("prev_high")}
    except Exception:
        return {}


def breakout_tags(L, kp):
    """突破标记（233 四共振之「股价在突破」的量化，C 级草案）：
    日内新高 / 破昨高 / 破昨收 / 高开走强。缺什么不臆造——字段空则不出该标签。"""
    p, hi, op, pc = (L.get("price"), L.get("high"), L.get("open"), L.get("prev_close"))
    tags = []
    if p and hi and abs(p - hi) < 1e-6:
        tags.append("日内新高")
    ph = (kp or {}).get("prev_high")
    if p and ph and p > ph:
        tags.append("破昨高")
    if p and pc and p > pc:
        tags.append("破昨收")
    if p and op and pc and op > pc and p > op:
        tags.append("高开走强")
    return tags


def _pankou_block(zt, zb, dtp, live, rows, focus_idx, cfg, t2codes, seg2codes, ind_names=None,
                  attr_main=None, uni_con=None):
    """盘口质量 + 角色标签 + 梯队 + 情绪条（2026-10-23 熔入肉肉体系判据）。
    量化自：肉肉 4.1 涨停启动时机（fbt）/ 4.2 封板质量（fund、zbc）/ 5.1 梯队完整性 /
    5.2 卡位竞争（总龙头 vs 卡位龙）/ 5.3 中军配合 / 六② 情绪周期（连板高度、炸板率）。
    全部 C 级草案：读数是「竞争结构的证据」，不是买卖建议。"""
    IC = cfg.get("intraday", {})
    chains = cfg.get("chains", {})          # 题材定位（链·环节）用
    code2themes = {}
    _IND_NAMES = set(ind_names or ()) | {x for r in rows for x in (r.get("行业标签") or []) if x}
    for r in rows:
        # 主归属候选源：大表「有效概念」优先，取不到回退 rows['题材']（串口径）
        _u = (uni_con or {}).get(r["code"])
        for t in (_u if _u else (r.get("题材") or [])):
            if t in _IND_NAMES:      # 行业名不当题材候选（同 compute_readings，Step4）
                continue
            code2themes.setdefault(r["code"], []).append(t)
    fmcap = {r["code"]: (r.get("fmcap") or 0) for r in rows}
    # 角色/梯队/情绪：唯一口径在 pankou.py（盘后股池共用）
    # block＝行业名全集 ∪ 归因排除词——主归属候选同样不收行业名与事件噪声（Step4）
    role_of, ladder, sentiment, main_theme = PK.roles_ladders(
        zt, zb, dtp, rows, focus_idx, t2codes, seg2codes,
        live_pct={c: v.get("pct") for c, v in live.items()}, fmcap=fmcap, live_rows=live,
        attr_opt=IC.get("attr"), chain_of=lambda t: P.chain_of(t, chains),
        block=_IND_NAMES | set(cfg.get("granularity", {}).get("exclude_words") or []),
        main_attr=attr_main, uni_con=uni_con)

    # ── 盘口质量表：池内涨停 + 全市场高标（连板≥3）+ 焦点龙头/卡位龙 ──
    pool_codes = {r["code"] for r in rows if r.get("池层") in ("核心", "备选")}
    picked = {}
    for z in zt:
        c = z.get("c")
        # 2026-10-24 用户要求：**全部涨停票都列**（此前只列池内/连板≥3/焦点龙头，
        # 导致中基健康这类「不在池内、连板不高、题材非焦点」的涨停票被漏掉）
        picked[c] = z
    pankou = []
    for c, z in picked.items():
        b = role_of.get(c) or {}
        mt = b.get("theme", "")
        L = ladder.get(mt) or {}
        _mc, _ms = P.chain_of(mt, chains) if mt else ("", "")
        pankou.append({
            "code": c, "name": z.get("n"), "池内": c in pool_codes,
            "连板": z.get("lbc") or 1, "首封": fmt_fbt(z.get("fbt")),
            "首封值": z.get("fbt") or 999999,
            "封单亿": round((z.get("fund") or 0) / 1e8, 2),
            "封单占流通%": round(z["_ratio"], 2),
            "炸板次数": z.get("zbc") or 0,
            # 角色＝**多标签**（空间龙·总龙头·早盘板 可并存，2026-10-23 第2版）
            "角色": "·".join(b.get("tags", ["跟风"])[:3]),
            # K 线形态（2026-10-27 用户要求）：一字板/T字板/地天板/天地板/回封/竞价板/尾盘板
            #   ——与角色分开存，前端用「空心标签」区分；两者可同时成立，不许互相覆盖。
            "形态": "·".join(b.get("form") or []),
            "形态标签": b.get("form") or [],
            "成色": b.get("score"),
            "标签": b.get("tags", ["跟风"]),
            # 2026-10-24 用户报错：这里原来是 code2themes[c][0]（东财标签第一个，实测与主归属
            # 一致率仅 2%），而同一行的「梯队」按主归属算 → 一行两个题材，龙头必然张冠李戴。
            # 现在「题材」= 主归属（唯一），标签全集另存「题材标签」只作参考。
            "题材": mt,
            # 主归属题材的链（2026-10-24）：梯队/龙头/角色全在主归属桶里，给一个「主链」字段
            # 供看板按梯队分组——归因链分组会把同一梯队的人拆到不同组（归因≠主归属）。
            "主链": _mc or (mt or ""),
            "题材标签": code2themes.get(c) or [],
            # 题材定位（2026-10-24 用户要求细分）：链·环节·题材·行业，缺项自动省略——
            # 「人形机器人」这一行变成「机器人链·本体·人形机器人·通用设备」，一眼看清它在这波里的层级
            # 定位：四层优先级（主归属归链 → 活跃题材归链 → 任意归链 → 题材·行业）
            "定位": PK.locate_theme(code2themes.get(c) or [], mt, set(ladder.keys()),
                                    lambda t: P.chain_of(t, chains), z.get("hybk") or ""),
            "焦点": mt,                              # 旧字段名，值同「题材」（保留兼容已落档 JSON）
            # 梯队/龙头/涨停家数**全部取自同一个桶**（mt）——同一行不再跨口径拼装
            "梯队": L.get("判定", ""),
            "梯队涨停": L.get("涨停"),
            "梯队涨停标签": L.get("涨停标签"),
            "龙头": L.get("龙头", ""),
            "龙头板": L.get("龙头板"),
            "备注": b.get("note", ""),
            "涨幅": round(z.get("zdp") or 0, 2)})
    RANK = {"空间龙": 0, "总龙头": 1, "板块龙头": 2, "首板龙头": 3, "日内龙": 4, "早盘板": 5,
            "助攻": 6, "补涨": 7, "换手龙": 8, "一字龙": 9, "天量换手": 10, "独苗": 11, "跟风": 12}
    # 排序（2026-10-24 用户要求）：**按主归属题材分组**，同题材的票排在一起——
    # 组间：A0 焦点题材优先（按 A0 排名）→ 其余按该题材涨停家数降序 → 题材名稳定；
    # 组内：角色 → 连板 → 首封。此前只按角色排，同题材的票被打散。
    # 分组粒度（2026-10-25 用户指出）：按**链**归位——「人形机器人/机器人概念」同属机器人链，
    # 按题材标签分组会把同一条产业链切成两组。链名取自定位第一段，无链的题材自成一组。
    from collections import Counter as _C
    def _chain_of(x):
        loc = x.get("定位") or ""
        return loc.split("·")[0] if "·" in loc else (x["焦点"] or x["题材"])
    for x in pankou:
        x["_grp"] = _chain_of(x)
    theme_cnt = _C(x["_grp"] for x in pankou)
    # 组间主键＝**链内最高成色**降序，次键＝链内涨停家数
    grp_best, grp_cnt = {}, {}
    for x in pankou:
        g = x["_grp"]
        grp_best[g] = max(grp_best.get(g, 0), x.get("成色") or 0)
        grp_cnt[g] = grp_cnt.get(g, 0) + 1
    pankou.sort(key=lambda x: (
        -(grp_best[x["_grp"]]),            # ★ 组间主键＝链内最高成色降序
        -grp_cnt[x["_grp"]],               # 次键：链内涨停家数
        x["_grp"],
        -(x.get("成色") or 0),          # 组内按**成色**降序（比角色更能量化强弱，2026-10-24 用户要求）
        RANK.get((x.get("标签") or ["跟风"])[0], 11),
        -(x["连板"]), x["首封值"]))
    pankou = pankou[:IC.get("pankou_top", 25)]
    for x in pankou:
        x.pop("首封值", None)
    # ── 下一棒候选（2026-10-27）：每行挂载同桶「未涨停但在动」的票 ──
    # 用户原话：看到时都已经涨停了，买不进去——要同链/环节/题材里还能上车的。
    # 口径：233 的「半路」+ 共振四条件；肉肉的「板块容量 / 低位优先」。
    try:
        _zt_codes = {z.get("c") for z in zt if z.get("c")}
        _cache = {}
        for x in pankou:
            _mt = x.get("题材") or ""
            if not _mt:
                x["候选"] = []
                continue
            if _mt not in _cache:
                _L = ladder.get(_mt) or {}
                _basis = _L.get("口径") or "题材"
                try:
                    _mem = PK.bucket_members(_mt, _basis, main_theme, seg2codes)
                except Exception:
                    _mem = set()
                _eligible={r["code"] for r in rows if r.get("池层") in ("核心","备选")}
                _mem &= _eligible
                _cache[_mt] = PK.follow_candidates(_mem, live, _zt_codes, limit=4)
            x["候选"] = _cache[_mt]
    except Exception:
        for x in pankou:
            x.setdefault("候选", [])
    return pankou, ladder, sentiment, role_of, main_theme


def compute_readings(snap, boards, rows, focus_idx, cfg, src, pools=None, kprev=None,
                     ind_names=None, attr_main=None, uni_con=None):
    """盘中读数的**唯一定义处**：焦点实时榜 / 在动候选 / 潜伏候选 / 同向性。
    main（单次扫描落档）与 intraday_board（常驻看板，2026-10-22）共用本函数——两边口径永不漂移。
    纯计算、不落盘、不打印。返回 dict：mkt_med / focus(全部) / ranked(排序后) /
    movers / lurkers / alignment / warns / 阈值(供打印层复述)。"""
    IC = cfg.get("intraday", {})
    zt_th = IC.get("zt_threshold", 9.7)
    mv_pct, mv_lb = IC.get("moving_pct", 2.0), IC.get("moving_lb", 1.5)
    top_show = IC.get("top_show", 10)
    live = {r["code"]: r for r in snap if r.get("code")}
    mkt_med = med([r.get("pct") for r in snap])
    suffixes = tuple(cfg.get("granularity", {}).get("normalize_strip_suffix", []))
    chains = cfg.get("chains", {})
    warns = []

    t2codes, seg2codes = {}, {}
    # 行业名不配当题材候选（2026-10-24 Step4）：大鹏工业 主归属=专用设备、四通股份=其他家居用品——
    # 都是申万行业名漏进了题材列表，被主归属规则当成了题材。
    # 行业名全集＝调用方注入的 ind_names（theme_attr.load_tag_map 的三级名并集，比 rows['行业标签']
    # 只存一级名更全）＋rows 行业标签兜底。
    IND_NAMES = set(ind_names or ()) | {x for r in rows for x in (r.get("行业标签") or []) if x}
    for r in rows:
        for t in (r.get("题材") or []):
            if t in IND_NAMES:
                continue
            t2codes.setdefault(t, []).append(r["code"])
            ch, sg = P.chain_of(t, chains)
            if ch and sg:
                seg2codes.setdefault((ch, sg), set()).add(r["code"])
    seg2codes = {k: list(v) for k, v in seg2codes.items()}

    def codes_of(entry):
        tier, name = entry[0], entry[1]
        if tier == "环节" and "·" in name:
            ch, sg = name.split("·", 1)
            return seg2codes.get((ch, sg), []), f"{ch}·{sg}"
        return t2codes.get(name, []), name

    focus = []
    for i, e in enumerate(focus_idx, 1):
        tier, name, cluster, pre_rank, ch, sg = e[0], e[1], e[2], e[3], e[4], e[5]
        codes, disp = codes_of(e)
        ps = [live[c].get("pct") for c in codes if c in live and live[c].get("pct") is not None]
        lbs = [live[c].get("liangbi") for c in codes if c in live and live[c].get("liangbi") is not None]
        amt = sum((live[c].get("amount") or 0) for c in codes if c in live) / 1e8
        zt = sum(1 for p in ps if p >= zt_th)
        eq = round(sum(ps) / len(ps), 2) if ps else None
        exc = round(eq - mkt_med, 2) if (eq is not None and mkt_med is not None) else None
        zj, how = board_zljlr(disp, boards, suffixes)
        focus.append({"tier": tier, "name": disp, "cluster": cluster, "pre_rank": i,
                      "codes": len(codes), "eq_pct": eq, "exc": exc, "zt": zt,
                      "lb": round(sum(lbs) / len(lbs), 2) if lbs else None,
                      "ups": f'{sum(1 for p in ps if p > 0)}/{len(ps)}' if ps else None,
                      "amt_yi": round(amt, 1), "zljlr_yi": zj, "match": how})

    ranked = sorted([f for f in focus if f["exc"] is not None], key=lambda x: -x["exc"])
    for j, f in enumerate(ranked, 1):
        f["rank"] = j
        f["delta"] = f["pre_rank"] - j          # 正 = 比盘前靠前（盘中走强）
    pre_top = {e[1].split("·", 1)[-1] for e in focus_idx[:top_show]}
    pre_all = {e[1].split("·", 1)[-1] for e in focus_idx}
    now_top = {f["name"].split("·", 1)[-1] for f in ranked[:top_show]}
    overlap = len(pre_top & now_top)
    overlap_all = len(pre_all & now_top)
    v_same = IC.get("align_same", 0.6)
    v_diff = IC.get("align_diff", 0.3)
    verdict = ("同向" if overlap >= top_show * v_same
               else "内部重排（方向未变：实时前排全在盘前焦点内，强弱重排属正常轮动——看簇内谁接棒，不做派发解读）"
               if overlap_all >= len(now_top) - 2
               else "异向·派发嫌疑（盘前热点与盘中实际资金不一致，复核量能）"
               if overlap_all <= top_show * v_diff else "部分同向")

    # ── 盘口质量 / 梯队 / 情绪（2026-10-23 肉肉体系判据熔入，C 级草案）──
    pankou, ladder, sentiment = [], {}, {}
    catchup = []                      # 补涨候选（pools 可用时在块内填充；降级时保持为空）
    role_of, main_theme = {}, {}
    if pools and len(pools) == 3 and any(pools):
        pankou, ladder, sentiment, role_of, main_theme = _pankou_block(
            pools[0], pools[1], pools[2], live, rows, focus_idx, cfg, t2codes, seg2codes,
            ind_names=IND_NAMES, attr_main=attr_main, uni_con=uni_con)
        for f in focus:
            f["ladder"] = ladder.get(f["name"]) or {}

        # ── 补涨候选（2026-10-25 用户需求）：前排已起，找后排大概率补涨的 ──
        # 逻辑：233「主线赚钱效应后资金溢出」＋ 肉肉「补涨＝龙头打出高度后，资金挖掘的低位同逻辑票」
        # 2026-10-24 用户报错后重写：**场景、成员、梯队、龙头全部取自同一个桶**。
        #   旧版场景 key 来自 ladder（标签全集），成员也按标签全集，龙头却按主归属查 theme_head
        #   → 30 行里 20 行龙头「—」，其余张冠李戴（固态电池的票挂着长江三角的龙头上海九百）。
        #   现在：桶成员一律走 PK.bucket_members()，与 ladder 同一口径，不可能再串。
        zt_by_code = {r.get("c"): r for r in pools[0] if r.get("c")}   # 补涨判据用（前排=已涨停）
        ret5_of = {r["code"]: r.get("ret5") for r in rows}
        def _med(vs):
            vs = sorted(v for v in vs if v is not None)
            return vs[len(vs)//2] if vs else None
        catchup = []
        for scene, L in ladder.items():
            # 补涨场景**只用题材口径**：环节桶（如「机器人链·本体」36 家）跨多个题材聚合，
            # 把不同炒作逻辑混成一张表，正是「场景一个、龙头另一个」的同类错误。
            # 题材桶一票一桶（主归属），一个场景＝一条炒作逻辑，读者不需要分辨粒度。
            if (L.get("口径") or "题材") != "题材":
                continue
            if L.get("涨停", 0) < 2 or L.get("判定") == "独木难支":
                continue
            # 行业口径补涨前提：「龙头已打出高度」。桶内最高连板 <3 = 高度不够，
            # 谈不上「龙头打出高度后的低位挖掘」（C级补充——肉肉原文未给补涨量化判据）
            if (L.get("龙头板") or 0) < 3:
                continue
            basis = L.get("口径") or "题材"
            codes = PK.bucket_members(scene, basis, main_theme, seg2codes)
            vals = [ret5_of.get(c) for c in codes]
            med5 = _med(vals)
            for r in rows:
                c = r["code"]
                if c not in codes or r.get("池层") not in ("核心", "备选") or c in zt_by_code:
                    continue
                Lv = live.get(c, {})
                p = Lv.get("pct")
                if p is None or not (-1 <= p < 3):
                    continue
                ret5 = r.get("ret5")
                if ret5 is None or (med5 is not None and ret5 > med5):
                    continue           # 只留跑输桶内中位的（低位）
                b = (role_of.get(c) or {})
                # 题材定位（全员详细）：优先从该票题材标签找能归链的（2026-10-25 补涨表同步）
                _loc = None
                for t in (r.get("题材") or []):
                    _ch, _sg = P.chain_of(t, chains)
                    if _ch:
                        _loc = "·".join(x for x in (_ch, _sg, t, r.get("板块") or "") if x)
                        break
                if not _loc:
                    _loc = "·".join(x for x in (scene, r.get("板块") or "") if x) or scene
                catchup.append({"code": c, "name": r["name"], "池层": r["池层"],
                                # 场景＝本行所属的那个桶（必须显示出来，否则读者只能猜）
                                "场景": scene, "口径": basis,
                                "场景链": (P.chain_of(scene, chains)[0] or ""),
                                "定位": _loc, "梯队": L.get("判定", ""),
                                "场景涨停": L.get("涨停"), "场景中军": L.get("中军"),
                                # 龙头＝该桶内连板最高者（与梯队同源，不会是别的题材的票）
                                "龙头": L.get("龙头") or "", "龙头连板": L.get("龙头板") or 0,
                                "龙头炸板": L.get("龙头炸板") or 0,
                                "角色": "·".join(b.get("tags", [])[:2]) or "—",
                                "成色": b.get("score"),
                                "主归属": main_theme.get(c, ""),
                                "pct": p, "ret5": ret5, "换手": Lv.get("turnover"),
                                "量比": Lv.get("liangbi"),
                                "额亿": round((Lv.get("amount") or 0) / 1e8, 2)})
        seen = set()
        catchup = [x for x in catchup if x["code"] not in seen and not seen.add(x["code"])]
        # 2025→30 的正确姿势：**每个场景取 5 日最低的 3 只**（组内最优），
        # 场景间按涨停家数（前排强度）降序——否则全局 ret5 排序会被单一题材垄断
        by_theme = {}
        for x in catchup:
            by_theme.setdefault(x["场景"], []).append(x)
        merged = []
        for theme, xs in sorted(by_theme.items(),
                                key=lambda kv: -(ladder.get(kv[0], {}).get("涨停") or 0)):
            xs.sort(key=lambda x: (x["ret5"] if isinstance(x["ret5"], (int, float)) else 0,
                                   -(x.get("换手") or 0)))
            merged.extend(xs[:3])
        merged.sort(key=lambda x: (x["ret5"] if isinstance(x["ret5"], (int, float)) else 0,
                                   -(x.get("换手") or 0)))
        catchup = merged[:IC.get("catchup_top", 30)]

    movers = sorted([r for r in rows
                     if r.get("池层") in ("核心", "备选")
                     and ((live.get(r["code"], {}).get("pct") or -99) >= mv_pct
                          or (live.get(r["code"], {}).get("liangbi") or -1) >= mv_lb)],
                    key=lambda r: -(live.get(r["code"], {}).get("pct") or -99))[:20]
    def _detail(c, r):
        """在动/潜伏候选的详细身份：题材定位（四层优先级）+ 角色 + 成色 + **主归属场景/梯队/龙头**。
        2026-10-24：候选行原来显示 code2themes[0] 当「题材」、梯队却按标签全集 next() 扫，
        同一行两个题材。现在题材/场景/梯队/龙头全部取主归属这一个桶。"""
        b = (role_of.get(c) or {})
        hy = r.get("板块") or ""
        mt = b.get("theme") or main_theme.get(c) or ""
        L = ladder.get(mt) or {}
        loc = PK.locate_theme(r.get("题材") or [], mt,
                              set(ladder.keys()) if ladder else set(),
                              lambda t: P.chain_of(t, chains), hy)
        return {"定位": loc, "角色": "·".join(b.get("tags", [])[:2]) or "—",
                "成色": b.get("score"), "题材": mt, "场景": mt,
                "场景链": (P.chain_of(mt, chains)[0] or "") if mt else "",
                # 非涨停票的题材可能整天没进 ladder（既非焦点、也无可补的涨停）——这时写「未成梯队」，
                # 不写「—」（像没查到），也不写「0家」（ladder 不含全部题材，说 0 家是没根据的断言）
                "梯队": L.get("判定") or ("未成梯队" if mt else ""), "梯队涨停": L.get("涨停"),
                "梯队涨停标签": L.get("涨停标签"),
                "龙头": L.get("龙头", ""), "龙头板": L.get("龙头板")}
    movers_out = [{"code": r["code"], "name": r["name"], "池层": r["池层"],
                   "pct": live[r["code"]].get("pct"), "涨速": live[r["code"]].get("speed"),
                   "量比": live[r["code"]].get("liangbi"),
                   "额亿": round((live[r["code"]].get("amount") or 0) / 1e8, 2),
                   "突破": breakout_tags(live[r["code"]], (kprev or {}).get(r["code"])),
                   **_detail(r["code"], r)}
                  for r in movers if r["code"] in live]

    lo = IC.get("lurk_pct_lo", 0.0)
    hi = IC.get("lurk_pct_hi", 6.0)
    llb = IC.get("lurk_lb", 2.0)
    lurkers = []
    for r in rows:
        if r.get("池层") not in ("核心", "备选") or r["code"] not in live:
            continue
        L = live[r["code"]]
        p, lb = L.get("pct"), L.get("liangbi")
        if p is None or not (lo <= p < hi):
            continue
        if lb is None:
            # 降级源无量比 → 改用**换手率**（百分比，不受成交量单位影响；换手率是盘中最可靠的活跃度读数）。
            # ⚠ 不用「今日量/昨日量」：prev_vol 单位随源变化（腾讯=手、新浪=股），乘 100 会放大百倍（实测 267 倍）。
            tv = L.get("turnover")
            if tv is None or tv < IC.get("lurk_turnover", 3.0):
                continue
            lb_note = f"换手{tv:.1f}%"
        else:
            lb_note = ""
            if lb < llb:
                continue
        ret5 = r.get("ret5")
        pos = "低位" if (isinstance(ret5, (int, float)) and ret5 < -3) else \
              "中位" if isinstance(ret5, (int, float)) and ret5 <= 0 else "高位"
        lurkers.append({"code": r["code"], "name": r["name"], "池层": r["池层"],
                        "pct": p, "量比": lb,
                        "量比注": lb_note,
                        "换手": L.get("turnover"), "涨速": L.get("speed"),
                        "额亿": round((L.get("amount") or 0) / 1e8, 2),
                        "ret5": ret5, "位置": pos,
                        "突破": breakout_tags(L, (kprev or {}).get(r["code"])),
                        **_detail(r["code"], r)})
    lurkers.sort(key=lambda x: (0 if x["位置"] == "低位" else 1 if x["位置"] == "中位" else 2,
                                -(x["量比"] or 0), -(x.get("换手") or 0)))
    lurkers = lurkers[:IC.get("lurk_top", 30)]
    if src == "sina_market_center" and not lurkers:
        warns.append("⚠ 降级源无量比字段 → 潜伏候选用「量能进度」（今日量/昨日全天量）替代，非量比")

    # ── 机会候选＝在动 + 潜伏 合并（2026-10-25 用户要求二合一）──
    # 状态「已动」＝价格已表态（涨幅≥2%）；「潜伏」＝量能已表态、价格未跟（<2%）。
    # 排序：梯队成立优先 → 已动在前 → 涨幅降序。
    for m in movers_out:
        m["状态"] = "已动"
    for x in lurkers:
        x["状态"] = "潜伏"
    candidates = movers_out + lurkers
    candidates.sort(key=lambda x: (0 if x.get("梯队") == "成立" else 1,
                                   0 if x.get("状态") == "已动" else 1,
                                   -(x.get("pct") or 0)))
    return {"mkt_med": mkt_med, "focus": focus, "ranked": ranked,
            "movers": movers_out, "lurkers": lurkers, "candidates": candidates,
            "pankou": pankou, "ladder": ladder, "sentiment": sentiment,
            "main_theme": main_theme,
            "catchup": catchup,
            "alignment": {"overlap": overlap, "overlap40": overlap_all, "verdict": verdict},
            "warns": warns, "n_live": len(live),
            "th": {"mv_pct": mv_pct, "mv_lb": mv_lb, "lo": lo, "hi": hi, "llb": llb,
                   "top_show": top_show}}


def main():
    ap = argparse.ArgumentParser(description="盘中四时点扫描（只产读数，不产判断）")
    ap.add_argument("--time", default=None, help="925/1030/1300/1430 或 HHMM")
    ap.add_argument("--pool-dir", default=None, help="指定股池缓存目录（默认取最新）")
    ap.add_argument("--html", action="store_true", help="顺带出四时点累积对照 HTML")
    ap.add_argument("--html-only", action="store_true",
                    help="只重建 HTML（读已有 JSON，不采数、不落快照）——盘后改样式/补表用，不会覆盖时点读数")
    ap.add_argument("--dry-run", action="store_true", help="用缓存收盘数据模拟（盘后/周末自测）")
    ap.add_argument("--config", default=P.CFG_PATH)
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding="utf-8"))
    IC = cfg.get("intraday", {})
    mv_pct = IC.get("moving_pct", 2.0)
    mv_lb = IC.get("moving_lb", 1.5)
    top_show = IC.get("top_show", 10)
    zt_th = IC.get("zt_threshold", 9.7)

    pool = latest_pool_dir(a.pool_dir)
    if not pool:
        raise SystemExit("找不到股池缓存目录（cache/pool_*），先跑 pool_pipeline.py")
    summ = json.load(open(os.path.join(pool, "summary.json"), encoding="utf-8"))
    rows = json.load(open(os.path.join(pool, "pool_final.json"), encoding="utf-8"))
    focus_idx = summ.get("focus_index")
    if not focus_idx:
        raise SystemExit("summary.json 无 focus_index（需 pool_pipeline v1.8+ 重跑一次股池）")
    data_date = summ.get("data_date", "")

    if a.html_only:
        print(f"已重建 HTML {build_html(dt.datetime.now().strftime('%Y%m%d'))}")
        return

    now = dt.datetime.now()
    if a.time:
        v = a.time.strip().replace(":", "").zfill(4)   # "925" → "0925"，否则 9:25 会被解析成 92:25
        hhmm = int(v[:2]) * 60 + int(v[2:4])
    else:
        hhmm = now.hour * 60 + now.minute
    slot, hint, standard = slot_of(hhmm)

    snap, src, warn, simulated = take_snapshot(a.dry_run, pool)
    boards, bwarn = take_boards(cfg, a.dry_run)
    warn = warn + bwarn
    if not snap:
        raise SystemExit("实时快照取数失败：" + "；".join(warn))

    pools = take_pools(now.strftime("%Y%m%d"), a.dry_run)
    indices = take_indices(a.dry_run)
    kprev = load_kprev(pool)
    UNI = TU.load_universe()                       # 个股大表（没建过就返回 {}，全线回退旧口径）
    R = compute_readings(snap, boards, rows, focus_idx, cfg, src, pools=pools, kprev=kprev,
                         ind_names=_ind_names(rows, pool, cfg, UNI),
                         uni_con=TU.concept_map())
    warn = warn + R["warns"]
    focus, ranked = R["focus"], R["ranked"]
    movers_out, lurkers = R["movers"], R["lurkers"]
    mkt_med = R["mkt_med"]
    overlap, overlap_all = R["alignment"]["overlap"], R["alignment"]["overlap40"]
    verdict = R["alignment"]["verdict"]
    top_show = R["th"]["top_show"]
    mv_pct, mv_lb = R["th"]["mv_pct"], R["th"]["mv_lb"]
    lo, hi, llb = R["th"]["lo"], R["th"]["hi"], R["th"]["llb"]
    pankou, ladder, sentiment = R["pankou"], R["ladder"], R["sentiment"]

    scan_date = now.strftime("%Y%m%d")          # 读数属于扫描发生日（盘中），不是股池数据日
    out = {"slot": slot, "clock_hint": hint, "standard": standard,
           "taken_at": now.strftime("%Y-%m-%d %H:%M:%S"),
           "scan_date": scan_date, "data_date": data_date, "src": src, "simulated": simulated,
           "n_snap": len(snap), "mkt_med_pct": mkt_med,
           "alignment": {"overlap": overlap, "overlap40": overlap_all, "verdict": verdict},
           "sentiment": sentiment, "ladder": ladder, "pankou": pankou, "indices": indices,
           "catchup": R.get("catchup", []), "candidates": R.get("candidates", []),
           "warn": warn, "focus": focus, "movers": movers_out, "lurkers": lurkers}

    # ── 终端输出 ──
    tag = " ⚠ 模拟模式（缓存收盘数据，非实时）" if simulated else ""
    print(f"=== 盘中扫描 {slot} {'（非标准时点，吸附最近时点）' if not standard else ''} ==={tag}")
    print(hint)
    print(f"数据日 {data_date} · 快照源 {src} · {len(snap)} 只 · 全市场实时中位 {mkt_med}%")
    for w in warn:
        print(f"  {w}")
    if indices:
        print("\n── 大盘指数 ──")
        print("  " + " · ".join(f'{x["name"]} {x["price"]}（{x["pct"]:+.2f}%）' for x in indices))
    if sentiment:
        print(f"\n── 情绪条（肉肉 六② 情绪周期的量化输入）──")
        print(f"  涨停 {sentiment['涨停']} · 炸板 {sentiment['炸板']}"
              f"（率 {sentiment['炸板率']}% = 炸板池 {sentiment.get('炸板池','—')}"
              f" + 回封 {sentiment.get('回封','—')}） · 跌停 {sentiment['跌停']}"
              f"（封死 {sentiment.get('封死跌停','—')}） · 最高 {int(sentiment['最高连板'] or 0)} 板"
              f" · 连板家数 {sentiment['连板家数']}")
    print(f"\n── 焦点实时榜（按实时超额；盘前序→实时序，正数=走强）──")
    for f in ranked[:top_show]:
        zj = f'{f["zljlr_yi"]}亿' if isinstance(f["zljlr_yi"], (int, float)) else "—"
        lb = f'{f["lb"]}' if isinstance(f["lb"], (int, float)) else "—"
        ld = ladder.get(f["name"]) or {}
        lt = f'{ld.get("涨停","—")}家/{ld.get("中军","—")}中军·{ld.get("判定","—")}' if ld.get("涨停") else "—"
        print(f'  #{f["rank"]:>2} [{f["tier"]}] {f["name"]:<14} 涨{f["eq_pct"]:>6}% '
              f'超额{f["exc"]:>6}% 涨停{f["zt"]:>3} 量比{lb:>5} 资金{zj:>8} '
              f'梯队{lt} #{f["pre_rank"]}→{"↑" if (f["delta"] or 0) > 0 else "↓" if (f["delta"] or 0) < 0 else "＝"}')
    if pankou:
        print(f"\n── 盘口质量（显示 {len(pankou)} 只 · 池内涨停+连板≥3+焦点龙头；"
              f"全市场涨停 {sentiment.get('涨停','—')} 只 · 肉肉 4.1/4.2/5.2）──")
        for z in pankou[:15]:
            # 场景＝主归属题材；括号里给「主归属家数/判定」，标签口径家数只在差异大时提示
            tag = (f'/标签{z["梯队涨停标签"]}'
                   if (z.get("梯队涨停标签") or 0) != (z.get("梯队涨停") or 0) else "")
            lt = f'{z.get("题材") or "—"}({z.get("梯队涨停") or 0}家{tag}·{z.get("梯队") or "—"})'
            print(f'  [{z["角色"]}·成色{z.get("成色") or "—"}<{z["连板"]}板] {z["name"]:<8} '
                  f'{z.get("定位") or "—":<18} 场景{lt} 首封{z["首封"]} 封单{z["封单亿"]:>5}亿'
                  f'({z["封单占流通%"]:>4}%) 炸{z["炸板次数"]} {z["涨幅"]:>6}%'
                  f'{" ·池内" if z["池内"] else ""}')
    catchup = R.get("catchup", [])
    if catchup:
        print(f"\n── 补涨候选（前排已起·后排未涨 · 池内低位优先 · {len(catchup)} 只）──")
        for x in catchup[:15]:
            print(f'  [{x["池层"]}] {x["name"]:<8} 场景{x["场景"][:10]:<11} 涨{x["pct"]:>5}% '
                  f'5日{x["ret5"]:>6}% 换手{x.get("换手") or "—":>4} '
                  f'龙头:{x["龙头"] or "—"}({x.get("龙头连板") or 0}板) 梯队{x["梯队"]}')
    cand = R.get("candidates", [])
    print(f"\n── 机会候选（在动=价格已表态≥{mv_pct}% · 潜伏=量能先行价格未跟 · 共 {len(cand)} 只）──")
    for x in cand[:22]:
        sp = x.get("涨速") if isinstance(x.get("涨速"), (int, float)) else "—"
        bt = "/".join(x.get("突破") or []) or "—"
        r5 = x.get("ret5"); r5 = f"{r5:>6}%" if isinstance(r5, (int, float)) else "     —"
        vl = f"量比{x['量比']}" if (not x.get("量比注") and isinstance(x.get("量比"), (int, float))) \
            else (x.get("量比注") or "量比—")
        st = "🔥已动" if x["状态"] == "已动" else "💤潜伏"
        print(f'  [{st}] [{x["池层"]}] {x["name"]:<8} {x["pct"]:>6}% 梯队{x.get("梯队") or "—":<6} '
              f'{vl:>9} 5日{r5} {bt} 额{x.get("额亿", "—"):>6} {x["题材"]}')
    print(f"\n── 同向性检查 ──\n  盘前 top{top_show} 与此刻 top{top_show} 重合 {overlap} 个"
          f"（在盘前全部焦点内 {overlap_all}/{top_show}）→ {verdict}")

    # ── 落盘 JSON ──
    # 目录用**扫描发生日**：盘中读数属于今天，不是股池的数据日——否则明天用旧股池跑会覆盖今天的读数。
    # 文件名用**实际抓取时间 HHMM**：想跑几次跑几次、每次都留档互不覆盖（2026-10-20 用户需求）；
    # slot 只作语义标注（落在哪个日内钟窗口），不再决定文件名——否则 11:30 跑会被吸附成 1030 覆盖上午那份。
    out_dir = os.path.join(ROOT, "cache", "intraday", scan_date)
    os.makedirs(out_dir, exist_ok=True)
    hhmm_tag = now.strftime("%H%M")                # 实际抓取时间命名；hhmm 是分钟数，:04d 会把 11:41 写成 0701
    jf = os.path.join(out_dir, f"{hhmm_tag}.json")
    json.dump(out, open(jf, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n已落盘 {jf}")

    if a.html:
        html_out = build_html(scan_date)
        print(f"已出 HTML {html_out}")


def load_pre_focus(data_date):
    """从盘前股池 xlsx 的窗口A0 读焦点档案（静态盘前读数，盘中任何时刻都能读，真实不编造）。
    返回 name → dict；文件缺失返回空 dict（列显示 —，不编造）。"""
    x = os.path.join(ROOT, "watchlists", f"233战法盘前股池_{data_date}.xlsx")
    if not os.path.exists(x):
        return {}
    try:
        import openpyxl
        ws = openpyxl.load_workbook(x, read_only=True)["窗口A0·盘前焦点"]
        out = {}
        for r in ws.iter_rows(min_row=2, values_only=True):
            out[str(r[1])] = {"pre_rank": r[3], "pre_scores": r[4], "pre_zljlr": r[11],
                              "pre_cont": r[13], "pre_lc": r[19], "pre_ret5": r[25],
                              "pre_run": r[26], "pre_lead": r[22]}
        return out
    except Exception:
        return {}


def _lad_txt(L):
    """焦点表「梯队」格＝**篮子口径**（挂该标签的全部涨停）——它回答的是「这条主线今天有多少兵、
    结构全不全」，不是「谁是谁的兵」。括号里附主归属家数：差额＝这些兵被更具体的题材认领了
    （一票一桶的必然结果），但主线的兵数并不因此减少。个股视图（盘口/候选/补涨）才用主归属。"""
    L = L or {}
    # 兼容旧快照：2026-10-24 20:00 前后的 JSON 里字段不齐（有的只有「涨停/中军/判定」旧名，
    # 有的只有「涨停标签」而缺「中军标签/判定标签」）。**绝不允许两套口径混着拼**——
    # 混拼会拼出「4家(主归属0)/0中军·无涨停」这种自相矛盾的格子（4 家涨停却判定无涨停）。
    # 篮子判定缺失时只报家数，中军/判定宁可空着，也不借主归属的那一套。
    n = L.get("涨停标签")
    if n is not None:
        own = L.get("涨停")
        tg = f'(主归属{own})' if own not in (None, n) else ""
        if L.get("判定标签") is None:
            return f"{n}家{tg}"
        return f'{n}家{tg}/{L.get("中军标签")}中军·{L.get("判定标签")}'
    if L.get("判定标签") is None:
        n2 = L.get("涨停")
        if not n2:
            return "—"
        return f'{n2}家/{L.get("中军")}中军·{L.get("判定") or ""}'
    return "—"


def build_html(data_date):
    """四时点累积对照（读当日全部 JSON + 盘前 xlsx 档案）。无内联 JS；读数工具仍附免责声明。"""
    cfg = json.load(open(P.CFG_PATH, encoding="utf-8"))
    d = os.path.join(ROOT, "cache", "intraday", data_date)
    if not os.path.isdir(d):
        return f"（无快照目录 {d}）"
    snaps = [json.load(open(os.path.join(d, f), encoding="utf-8"))
             for f in sorted(os.listdir(d)) if f.endswith(".json")]
    order = {s: i for i, (s, _, _) in enumerate(SLOTS)}
    snaps.sort(key=lambda s: s["taken_at"])          # 按实际抓取时间排——快照不限次数，全保留
    pre = load_pre_focus(snaps[0]["data_date"]) if snaps else {}

    def fmt(v, suf=""):
        return "—" if v is None or v == "" else f"{v}{suf}"

    rows = {}
    for s in snaps:
        for f in s["focus"]:
            r = rows.setdefault(f["name"], {"tier": f["tier"], "cluster": f["cluster"],
                                            "chain": "", "lead": ""})
            r[f'{s["slot"]}·超额'] = f["exc"]
            r[f'{s["slot"]}·涨停'] = f["zt"]
            r[f'{s["slot"]}·排名'] = f.get("rank")
            r[f'{s["slot"]}·涨股比'] = f.get("ups")
            r[f'{s["slot"]}·量比'] = f.get("lb")
            r["_lad"] = f.get("ladder") or {}
            r[f'{s["slot"]}·资金'] = f.get("zljlr_yi")
    cols = [c for s in snaps for c in (f'{s["slot"]}·超额', f'{s["slot"]}·涨停',
                                       f'{s["slot"]}·涨股比', f'{s["slot"]}·排名')]

    def sort_key(name):
        r = rows[name]
        for c in reversed(rk_cols_src := [c for c in cols if c.endswith("·排名")]):
            if r.get(c) is not None:
                return r[c]
        return 999

    # 档位着色：重点=暖红 / 观察=蓝 / 环节=紫
    TIER_CLS = {"重点": "t-zd", "观察": "t-gc", "环节": "t-hj"}
    trs = []
    for name in sorted(rows, key=sort_key):
        r = rows[name]
        p = pre.get(name) or pre.get(name.split("·", 1)[-1]) or {}
        tds = "".join(f"<td>{fmt(r.get(c))}</td>" for c in cols)
        cls = TIER_CLS.get(r["tier"], "")
        trs.append(
            f'<tr class="{cls}"><td><span class="tag">{r["tier"]}</span>{name}</td>'
            f'<td>{r["cluster"]}</td>'
            f'<td class="pre">{fmt(p.get("pre_rank"))}</td>'
            f'<td class="pre">{fmt(p.get("pre_zljlr"))}</td>'
            f'<td class="pre">{fmt(p.get("pre_ret5"), "%")}</td>'
            f'<td class="pre">{fmt(p.get("pre_run"))}</td>'
            f'<td class="pre">{fmt(p.get("pre_cont"))}<br>{fmt(p.get("pre_lc"))}</td>'
            # 0 主归属但标签有涨停 → 必须写「0家(标签N)·无主归属」，不能只写「—」（会被当成数据缺失）
            f'<td>{_lad_txt(r.get("_lad"))}</td>'
            f'{tds}</tr>')
    head_pre = '<th colspan="5">盘前档案（昨夜股池窗口A0）</th>'   # 盘前 5 列 + 焦点/簇 rowspan2 共 7 列，colspan=6 会错位一列
    head_live = "".join(
        f'<th colspan="4">{s["slot"]}档 <small>{s["taken_at"][11:16]}'
        f'{"" if s.get("standard") else "·非标"}</small></th>' for s in snaps)
    sub_pre = ('<tr><th rowspan="2">焦点（档位）</th><th rowspan="2">簇/链</th>'
               '<th>综合排名</th><th>主力(亿)</th><th>自算5日</th><th>连胜</th><th>连续性/生命周期</th>'
               '<th>梯队（篮子口径）</th>')
    head_pre = head_pre.replace('colspan="5"', 'colspan="6"')
    sub_live = '</tr><tr>' + "".join('<th>超额</th><th>涨停</th><th>涨股比</th><th>排名</th>' for _ in snaps)
    # 在动候选（取最新时点快照）
    latest = snaps[-1]
    mv = latest.get("movers") or []
    # 兼容旧快照：2026-10-24 20:00 前写入的「无主归属涨停」＝该票题材当天没进梯队；
    # 新口径叫「未成梯队」（不写「0家」——ladder 不含全部题材，断言 0 家没有根据）
    for m in mv:
        if m.get("梯队") == "无主归属涨停":
            m["梯队"] = "未成梯队"
    mtrs = "".join(
        f'<tr class="{"m-core" if m["池层"] == "核心" else "m-bak"}">'
        f'<td><span class="tag {"tg-core" if m["池层"] == "核心" else "tg-bak"}">{m["池层"]}</span>{m["name"]}</td>'
        f'<td>{m["题材"] or "—"}</td><td>{fmt(m.get("梯队"))}'
        f'{"（" + str(m["梯队涨停"]) + "家）" if m.get("梯队涨停") is not None else ""}</td>'
        f'<td><b>{m["pct"]}%</b></td><td>{m["额亿"]}亿</td>'
        f'<td>{fmt(m.get("涨速"))}</td><td>{fmt(m.get("量比"))}</td></tr>'
        for m in mv)
    # 潜伏候选（未大涨 · 量能异动）
    lk = latest.get("lurkers") or []
    POS_CLS = {"低位": "pos-lo", "中位": "pos-md", "高位": "pos-hi"}
    def _lbcell(x):
        """量比列：降级源没有量比 → 置空并说明（不拿换手率冒充量比，右列已有换手率）"""
        v = x.get("量比")
        if isinstance(v, (int, float)):
            return f"<b>{fmt(v)}</b>"
        return ('<span style="color:#8a8580" title="量比缺（东财降级源）；本行按换手率≥3% 入表，换手率见右列">—</span>')
    ltrs = "".join(
        f'<tr class="{"m-core" if x["池层"] == "核心" else "m-bak"}">'
        f'<td><span class="tag {"tg-core" if x["池层"] == "核心" else "tg-bak"}">{x["池层"]}</span>{x["name"]}</td>'
        f'<td>{x["题材"]}</td><td><b>{x["pct"]}%</b></td>'
        f'<td>{_lbcell(x)}</td><td>{fmt(x.get("换手"), "%")}</td><td>{fmt(x.get("涨速"))}</td>'
        f'<td>{x["额亿"]}亿</td>'
        f'<td class="{POS_CLS.get(x["位置"], "")}">{fmt(x.get("ret5"), "%")} <span class="pos">{x["位置"]}</span></td></tr>'
        for x in lk)
    se = (snaps[-1].get("sentiment") or {}) if snaps else {}
    sent_line = (f'情绪条（{snaps[-1]["slot"]}）：涨停 {se.get("涨停","—")} · '
                 f'炸板 {se.get("炸板","—")}（率 {se.get("炸板率","—")}% = 炸板池 {se.get("炸板池","—")}'
                 f' + 回封 {se.get("回封","—")}）· 跌停 {se.get("跌停","—")}'
                 f'（封死 {se.get("封死跌停","—")}）· 最高 {int(se.get("最高连板") or 0)} 板 · '
                 f'连板家数 {se.get("连板家数","—")}') if se else ""
    pk = (snaps[-1].get("pankou") or []) if snaps else []
    zt_n = (snaps[-1].get("sentiment") or {}).get("涨停", "—") if snaps else "—"
    def _scene(z):
        """盘口「题材（主归属）·梯队」格：场景名 + 主归属家数(/标签家数) + 判定 + 龙头，全在一个桶里出。
        场景名必须写在格子里——否则读者只能拿同一行的「定位/今日归因」去对，就会把
        别的题材的龙头看成这一行的（2026-10-24 用户报错根因）。"""
        th = z.get("题材") or "—"
        n, nt = z.get("梯队涨停"), z.get("梯队涨停标签")
        if n is None:
            return th
        tag = f"/标{nt}" if (nt is not None and nt != n) else ""
        s = f'{th}（{n}家{tag}·{z.get("梯队") or "—"}）'
        if z.get("龙头") and z.get("龙头") != z.get("name"):
            s += f'<br><small>龙头 {z["龙头"]}</small>'
        return s
    pk_rows = "".join(
        f'<tr><td><span class="rl">{z["角色"]}</span>{"<br><small>" + z["备注"] + "</small>" if z.get("备注") else ""}</td>'
        f'<td><b>{z.get("成色") if z.get("成色") is not None else "—"}</b></td><td>{z["name"]}</td>'
        f'<td>{z.get("定位") or z.get("题材") or "—"}</td>'
        f'<td>{_scene(z)}</td>'
        f'<td>{z["连板"]}</td><td>{z["首封"]}</td>'
        f'<td>{z["封单亿"]}</td><td>{z["封单占流通%"]}</td><td>{z["炸板次数"]}</td><td>{z["涨幅"]}</td></tr>'
        for z in pk)
    align = " / ".join(f'{s["slot"]} {s["alignment"]["verdict"].split("（")[0]}' for s in snaps)
    align_detail = "".join(
        f'<div class="ad"><b>{s["slot"]}</b>（抓取 {s["taken_at"][11:19]} · 源 {s["src"]} · '
        f'{s.get("n_snap", "—")} 只 · 市场中位 {fmt(s.get("mkt_med_pct"), "%")}）<br>'
        f'{s["clock_hint"]}<br>同向性：盘前 top10 重合 <b>{s["alignment"]["overlap"]}</b>，'
        f'在盘前全部焦点内 <b>{s["alignment"].get("overlap40", "—")}</b>/10 → '
        f'{s["alignment"]["verdict"]}</div>' for s in snaps)
    covered = "、".join(f'{s["slot"]}({s["taken_at"][11:16]})' for s in snaps)
    hit_slots = {s["slot"] for s in snaps if s.get("standard")}
    missing = "、".join(x for x, _, _ in SLOTS if x not in hit_slots)
    warn = "".join(f"<li>{w}</li>" for s in snaps for w in s.get("warn", []))
    dd = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    out_dir = os.path.join(ROOT, "reports", dt.date.today().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"盘中四时点扫描-{data_date}.html")   # 文件名带股池数据日，便于对照盘前版本
    open(path, "w", encoding="utf-8").write(f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>盘中四时点扫描 {data_date}</title>
<style>body{{font-family:-apple-system,sans-serif;margin:24px;color:#222}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px}}
table{{border-collapse:collapse;font-size:12.5px}} td,th{{border:1px solid #ccc;padding:4px 7px}}
th{{background:#f0f0f0}} .meta{{color:#666;font-size:13px}}
.tag{{display:inline-block;font-size:11px;padding:1px 6px;margin-right:6px;border-radius:4px;color:#fff}}
tr.t-zd td{{background:#FCEBEB}} tr.t-zd .tag{{background:#A32D2D}}
tr.t-gc td{{background:#E6F1FB}} tr.t-gc .tag{{background:#185FA5}}
tr.t-hj td{{background:#EEEDFE}} tr.t-hj .tag{{background:#534AB7}}
.legend{{font-size:12px;margin:8px 0}} .legend span{{margin-right:14px}}
.align{{background:#faf6ec;border:1px solid #e5d9b8;padding:10px 14px;margin:14px 0}}
.ad{{border-left:3px solid #d9c98a;padding:6px 10px;margin:8px 0;font-size:13px}}
td.pre{{color:#666;font-size:12px}}
.rl{{display:inline-block;font-size:10.5px;padding:0 5px;border-radius:4px;background:#A32D2D;color:#fff}}
.small{{font-weight:400;color:#888;font-size:11px}}
table.wrap{{display:block;overflow-x:auto;white-space:nowrap;max-width:100%}}
tr.m-core td{{background:#FCEBEB}} tr.m-bak td{{background:#E6F1FB}}
.tg-core{{background:#A32D2D}} .tg-bak{{background:#185FA5}}
.pos{{font-size:11px;padding:1px 5px;border-radius:4px;color:#fff;margin-left:4px}}
.pos-lo .pos{{background:#3B6D11}} .pos-md .pos{{background:#888780}} .pos-hi .pos{{background:#BA7517}}
.pos-lo{{font-weight:500}}
.warn{{color:#a33}}</style></head><body>
<h1>盘中四时点扫描 · 股池数据日 {data_date}</h1>
<p class="meta">生成 {dd} · 四时点读数复核（233 日内钟 · 熔炼系统 3.5 日内资金选择时钟） · 只产读数不产判断 · 行序 = 最新时点实时排名</p>
<p class="legend">档位：<span style="color:#A32D2D">■ 重点</span><span style="color:#185FA5">■ 观察</span><span style="color:#534AB7">■ 环节</span>（行底色同色系；排名 = 该时点在 48 条焦点内的实时强弱序；超额 = 题材等权涨 − 全市场实时中位）</p>
<p class="legend">时点覆盖：<b>{covered or '（无）'}</b>；未跑：{missing or '无'}</p>
<h2>同向性检查（日内 vs 日频）</h2>
{align_detail or '<p class="meta">（当日无快照）</p>'}
{f'<div class="align"><b>{sent_line}</b></div>' if sent_line else ''}
<h2>焦点题材 × 时点对照</h2>
<table class="wrap"><tr>{head_pre}{head_live}</tr>{sub_pre}{sub_live}
{''.join(trs)}</table>
<h2>盘口质量（{latest['slot']} 快照 · 显示 {len(pk)} / 全市场涨停 {zt_n} 只）</h2>
<p class="meta">入选范围：池内涨停 ∪ 全市场连板≥3 ∪ 焦点题材龙头/卡位龙（其余涨停不列——它们不在池内也不构成焦点竞争）。</p>
<table><tr><th>角色</th><th>成色</th><th>名称</th><th>题材定位（链·环节·题材·行业）</th><th>题材（主归属）·梯队</th><th>连板</th><th>首封</th><th>封单(亿)</th><th>占流通%</th><th>炸板</th><th>涨幅%</th></tr>
{pk_rows or '<tr><td colspan="11">（涨停池未取到）</td></tr>'}</table>
<p class="meta">角色（多标签可并存，C 级草案）：空间龙＝全市场连板最高；总龙头/板块龙头/首板龙头＝题材内连板最高（≥5/2–4/1 板分级）；日内龙＝题材内最先涨停；早盘板＝首封≤10:30；助攻＝龙头封板后 30 分钟内跟进；补涨＝龙头≥5 板时题材内首板；换手龙＝连板≥2 且换手 15–30%；一字龙＝换手&lt;3% 且封单厚；天量换手＝换手&gt;20%（出货嫌疑）；独苗/跟风＝兜底。成色＝身位32+封单24+炸板扣16+换手14+首封10+带动性4。<b>题材/梯队/龙头同源</b>：一只票当日只归<b>一个</b>主归属题材（该票标签中、今日有涨停且能归 19 链者优先，再比涨停家数），梯队＝该题材<b>主归属</b>涨停家数·判定（中军＝流通前3红盘不涨停）。括号里出现「/标N」时，N＝挂着该标签的涨停票总数（含主归属在别处的）——差额越大，说明这个题材越是一堆票顺带挂了个标签。本表的家数口径与「焦点表」不同：焦点表问「这条主线有多少兵」（篮子口径），本表问「这只票算在谁的梯队里」（主归属口径），同一张表里不会混用。卡位龙需前日断板数据，暂不标。</p>
<h2>在动候选（核心+备选池 · 取自 {latest['slot']} 快照 · 共 {len(mv)} 只）</h2>
<table><tr><th>池层 · 名称</th><th>题材（主归属）</th><th>梯队（家数·判定）</th><th>涨幅</th><th>成交额</th><th>涨速</th><th>量比</th></tr>
{mtrs or '<tr><td colspan="7">该时点无达阈标的</td></tr>'}</table>
<h2>潜伏候选（未大涨 · 量能异动 · 取自 {latest['slot']} 快照 · 共 {len(lk)} 只）</h2>
<p class="meta">判据（C级草案）：池内核心+备选 · 涨幅 0—6%（未大涨）· 量比 ≥2（放量但价格未表态 = 资金进场痕迹）。排序：低位优先（5日涨幅 &lt; −3% 记「低位」——低位放量优于高位放量），再按量比降序。⚠ 东财降级时无量比字段：量比列置空，改按「换手率 ≥3%」入表（换手率见右列）。</p>
<table><tr><th>池层 · 名称</th><th>题材（主归属）</th><th>涨幅</th><th>量比</th><th>换手</th><th>涨速</th><th>成交额</th><th>5日涨幅 · 位置</th></tr>
{ltrs or '<tr><td colspan="8">该时点无达阈标的（或快照源无量比）</td></tr>'}</table>
<p class="meta">涨速/量比列：东财 push2 盘中不可用时降级新浪源（无此二字段，显示 —）——缺什么显示什么，不编造。</p>
<h2>降级与异常</h2><ul class="warn"><li>{warn or '无'}</li></ul>
<p class="meta" style="margin-top:32px">{cfg.get('disclaimer','')}</p>
</body></html>""")
    return path


if __name__ == "__main__":
    main()
