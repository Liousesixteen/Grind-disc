"""盘中四时点扫描 · 个股画像版（2026-10-24 用户要求重做分类粒度）

用户诉求：「分类的粒度不够细啊，你像启明星辰最相关的就是网络安全的啊，一个股票会有很多的概念的，
每天最相关的不一样，还有每个公司最核心的业务是什么，这些都是需要你去考虑的，全面综合考虑」

本报告 = 旧版（焦点题材 × 时点对照 / 盘口质量 / 在动候选 / 潜伏候选）＋ 三层新结构：
  ① 个股画像：**核心业务（主营构成占比）** → **主业链·环节** → **今日归因题材（打分）** → **错位度**
  ② 环节强度榜：把全市场 5576 只按 链·环节 重新聚合，看今天资金买的是哪个环节，而不是哪个大标签
  ③ 外溢结构：今日涨停里多少是「主业驱动」，多少是「跨链蹭概念」——情绪票占比即风险温度

口径全部 C 级草案，读数不产判断；每个数字可复算（脚本 stock_profile.py / theme_attr.py）。
用法：python profile_report.py [--intraday cache/intraday/20261024/1358.json] [--pool cache/pool_20261023]
"""
import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import pool_pipeline as P
import theme_attr as TA
from stock_profile import load as load_prof, core, brief, main_seg

LAYER_CLS = {"主业驱动": "l-main", "链内外溢": "l-spill", "跨链蹭概念": "l-chase",
             "题材未归链": "l-none", "主业未归链": "l-none", "无题材读数": "l-none",
             "行业代位": "l-sub", "主业代位": "l-sub"}


def latest(path_dir, pattern_end=".json"):
    fs = [f for f in os.listdir(path_dir) if f.endswith(pattern_end) and f[:4].isdigit()]
    return os.path.join(path_dir, sorted(fs)[-1]) if fs else None


def fmt(v, unit="", nd=1):
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return f"{v:.{nd}f}{unit}"
    return str(v)


def build(args):
    cfg = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
    chains = cfg["chains"]
    pool = args.pool or os.path.join(ROOT, "cache", "pool_20261023")
    snap = json.load(open(os.path.join(pool, "all_a_snapshot.json"), encoding="utf-8"))
    med = sorted([r["pct"] for r in snap if r.get("pct") is not None])
    med = med[len(med) // 2]
    con, ind, ind1 = TA.load_tag_map(pool, cfg)
    stats = TA.build_theme_stats(snap, con, med, cfg.get("intraday", {}).get("zt_threshold", 9.7))
    big = TA.big_label_set(cfg)
    ipath = args.intraday or latest(os.path.join(ROOT, "cache", "intraday", "20261024"))
    I = json.load(open(ipath, encoding="utf-8"))

    rows = {}
    for k in ("pankou", "candidates", "movers", "lurkers", "catchup"):
        for x in (I.get(k) or []):
            if x.get("code"):
                rows.setdefault(x["code"], {}).update({"_" + k: x})
    codes = list(rows)
    prof = load_prof(codes, workers=8)

    def chain_of(t):
        return P.chain_of(t, chains)

    pics = {}
    for c in codes:
        p = prof.get(c)
        cr = core(p)
        mc, ms, ev = main_seg(p, chains, ind.get(c, []),
                              industry_to_chain=cfg.get("industry_to_chain"))
        main_approx = ev.startswith(("行业:", "关键词:"))   # 兜底推的 = 近似，不能当精确环节用（经营范围兜底 2026-10-24 已删）
        r = TA.attribute(con.get(c, []), stats, chain_of, {}, mc, ms, ind.get(c, []), big,
                         main_approx=main_approx)
        pics[c] = {"code": c, "name": (rows[c].get("_pankou") or rows[c].get("_candidates")
                                       or rows[c].get("_movers") or rows[c].get("_lurkers")
                                       or rows[c].get("_catchup"))["name"],
                   "core": cr, "core_txt": brief(p), "main_chain": mc, "main_seg": ms,
                   "main_ev": ev, "main_approx": main_approx, **r}

    # ── 环节强度榜（题材口径，全市场）──
    seg_members, seg_themes = defaultdict(set), defaultdict(set)
    for t, st in stats.items():
        ch, sg = chain_of(t)
        if ch and sg:
            seg_themes[(ch, sg)].add(t)
    t2codes = defaultdict(list)
    for c, tags in con.items():
        for t in tags:
            t2codes[t].append(c)
    px = {r["code"]: r for r in snap}
    seg_rows = []
    for key, themes in seg_themes.items():
        mem = set()
        for t in themes:
            mem.update(t2codes[t])
        ps = [px[c]["pct"] for c in mem if c in px and px[c].get("pct") is not None]
        if len(ps) < 8:
            continue
        eq = sum(ps) / len(ps)
        zt = sum(1 for x in ps if x >= cfg.get("intraday", {}).get("zt_threshold", 9.7))
        seg_rows.append({"chain": key[0], "seg": key[1], "n": len(ps), "eq": round(eq, 2),
                         "exc": round(eq - med, 2), "zt": zt,
                         "ztr": round(zt / len(ps) * 100, 1),
                         "ups": round(sum(1 for x in ps if x > 0) / len(ps) * 100, 0),
                         "themes": sorted(themes)[:6]})
    # 综合分（0—100）：0.7×超额 + 0.3×涨停率，两项各自先归一到 0—1，避免量纲打架
    for x in seg_rows:
        x["comp"] = round((0.7 * x["exc"] / 5.0 + 0.3 * x["ztr"] / 12.0) * 100, 1)
    seg_rows.sort(key=lambda x: -x["comp"])
    seg_rank = {(x["chain"], x["seg"]): i + 1 for i, x in enumerate(seg_rows)}

    # 归因票数：多少只画像票归到该环节
    seg_cnt = Counter((v["main"]["chain"], v["main"]["seg"]) for v in pics.values()
                      if v.get("main") and v["main"]["seg"])
    return dict(cfg=cfg, I=I, pics=pics, rows=rows, stats=stats, med=med, seg_rows=seg_rows,
                seg_rank=seg_rank, seg_cnt=seg_cnt, snaptime=I.get("taken_at", ""),
                data_date=I.get("data_date", ""), ipath=ipath, pool=pool)


def render(D):
    cfg, I, pics, rows = D["cfg"], D["I"], D["pics"], D["rows"]
    se = I.get("sentiment") or {}
    focus = I.get("focus") or []
    pankou = I.get("pankou") or []
    seg_rows, seg_rank = D["seg_rows"], D["seg_rank"]

    def pic_cells(c, show_role=True):
        p = pics.get(c) or {}
        core_txt = p.get("core_txt", "—")
        mc, ms = p.get("main_chain", ""), p.get("main_seg", "")
        main = p.get("main")
        layer = p.get("layer", "")
        approx = "≈" if p.get("main_approx") else ""
        # 「未归链」分两种：没查过 / 查过并确认 19 链覆盖不到（override 写空串＝结论）
        biz = ("未归链（已核实）" if str(p.get("main_ev", "")).startswith("人工:")
               else "未归链") if not mc else f'{approx}{mc}·{ms}'
        if ms == "" and mc:
            biz = f"{approx}{mc}·—"
        a = ""
        if main:
            a = (f'<b>{main["theme"]}</b> <small class="sc">{main["score"]}</small><br>'
                 f'<small class="mt">{main["chain"]}·{main["seg"] or "—"}｜超额{main["exc"]:+}%'
                 f'｜涨停{main["zt"]}只({main["ztr"]}%)</small>')
        else:
            a = "—"
        alt = "、".join(f'{x["theme"]}({x["score"]})' for x in (p.get("alts") or [])[:3]) or "—"
        role = ""
        if show_role:
            x = rows.get(c, {}).get("_pankou") or rows.get(c, {}).get("_movers") or {}
            if x.get("角色") and x["角色"] != "—":
                role = f'<span class="rl">{x["角色"]}</span>'
        return (f'<td class="nm">{role}{p.get("name", "")}<br><small class="cd">{c}</small></td>'
                f'<td class="core">{core_txt}</td>'
                f'<td class="biz">{biz}</td>'
                f'<td class="attr">{a}</td>'
                f'<td><span class="lay {LAYER_CLS.get(layer, "")}">{layer or "—"}'
                f'{"≈" if p.get("approx") else ""}</span></td>'
                f'<td class="alt">{alt}</td>')

    # ── 个股画像主表：涨停池，按 归因环节 的当日排名排 ──
    order = {c: (seg_rank.get((v["main"]["chain"], v["main"]["seg"]), 999) if v.get("main") else 998)
             for c, v in pics.items()}
    pk_sorted = sorted([z for z in pankou],
                       key=lambda z: (order.get(z["code"], 999), -(z.get("成色") or 0)))
    pk_trs = []
    for z in pk_sorted:
        c = z["code"]
        pk_trs.append(
            f'<tr>{"".join(pic_cells(c))}'
            f'<td>{z["连板"]}</td><td>{z["首封"]}</td>'
            f'<td>{fmt(z["封单亿"])}</td><td>{fmt(z["封单占流通%"])}</td>'
            f'<td>{z["炸板次数"]}</td><td><b>{z["涨幅"]}</b></td></tr>')

    # ── 外溢结构 ──
    lay_cnt = Counter(pics[c]["layer"] for c in pics if c in {z["code"] for z in pankou})
    zt_codes = {z["code"] for z in pankou}
    chase = [pics[c] for c in zt_codes if pics.get(c) and pics[c]["layer"] == "跨链蹭概念"]
    chase.sort(key=lambda x: -(x["main"]["score"] if x.get("main") else 0))
    chase_rows = "".join(
        f'<tr><td>{x["name"]}<br><small class="cd">{x["code"]}</small></td>'
        f'<td>{x["core_txt"]}</td><td>{x["main_chain"]}·{x["main_seg"] or "—"}</td>'
        f'<td><b>{x["main"]["theme"]}</b>({x["main"]["score"]})</td>'
        f'<td>{x["main"]["chain"]}·{x["main"]["seg"] or "—"}</td></tr>' for x in chase)

    # ── 环节强度榜 ──
    seg_trs = []
    for i, s in enumerate(seg_rows[:28], 1):
        cnt = D["seg_cnt"].get((s["chain"], s["seg"]), 0)
        seg_trs.append(
            f'<tr><td>{i}</td><td><b>{s["chain"]}</b></td><td>{s["seg"]}</td>'
            f'<td>{s["n"]}</td><td><b>{s["comp"]:.0f}</b></td><td>{s["eq"]:+.2f}%</td>'
            f'<td class="{"hi" if s["exc"] >= 1.5 else ""}">{s["exc"]:+.2f}%</td>'
            f'<td>{s["zt"]}</td><td>{s["ztr"]}%</td><td>{s["ups"]:.0f}%</td>'
            f'<td>{cnt or "—"}</td>'
            f'<td class="alt">{"、".join(s["themes"][:5])}</td></tr>')

    # ── 焦点题材（复用旧口径，定位换成新链）──
    fo_trs = "".join(
        f'<tr class="{"t-zd" if f["tier"]=="重点" else ("t-gc" if f["tier"]=="观察" else "t-hj")}">'
        f'<td><span class="tag">{f["tier"]}</span>{f["name"]}</td>'
        f'<td>{f["cluster"]}</td><td>{f["pre_rank"]}</td>'
        f'<td><b>{fmt(f.get("exc"), "%")}</b></td><td>{f.get("zt")}</td>'
        f'<td>{f.get("ups")}</td><td>{f.get("amt_yi")}亿</td>'
        f'<td>{fmt(f.get("zljlr_yi"), "亿")}</td>'
        f'<td>{f.get("rank")}({f.get("delta"):+d})</td></tr>'
        for f in sorted([x for x in focus if x.get("rank") is not None],
                        key=lambda x: x["rank"]))

    # ── 候选（在动/潜伏）画像 ──
    def cand_trs(key, pctcol=True):
        out = []
        for x in (I.get(key) or []):
            c = x["code"]
            p = pics.get(c) or {}
            out.append(
                f'<tr>{"".join(pic_cells(c, show_role=False))}'
                f'<td>{x.get("池层")}</td><td><b>{x.get("pct")}%</b></td>'
                f'<td>{fmt(x.get("量比")) if not x.get("量比注") else x["量比注"]}</td>'
                f'<td>{fmt(x.get("ret5"), "%")}</td><td>{fmt(x.get("额亿"), "亿")}</td></tr>')
        return "".join(out)

    covered = sorted({(I.get("slot"), D["snaptime"][11:16])})
    sent = (f'涨停 {se.get("涨停","—")} · 炸板 {se.get("炸板","—")}（率 {se.get("炸板率","—")}%）'
            f' · 跌停 {se.get("跌停","—")}（封死 {se.get("封死跌停","—")}）'
            f' · 最高 {int(se.get("最高连板") or 0)} 板 · 连板家数 {se.get("连板家数","—")}')

    tot = len(zt_codes)
    l_main = lay_cnt.get("主业驱动", 0)
    l_spill = lay_cnt.get("链内外溢", 0)
    l_chase = lay_cnt.get("跨链蹭概念", 0)
    l_none = tot - l_main - l_spill - l_chase

    dd = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>个股画像版 · 盘中扫描 {D['data_date']}</title>
<style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;margin:22px auto;max-width:1580px;color:#1a1a1a;line-height:1.55}}
h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:16px;margin:30px 0 8px;padding-left:8px;border-left:4px solid #A32D2D}}
h3{{font-size:14px;margin:18px 0 6px;color:#444}}
.meta{{color:#666;font-size:12.5px;margin:2px 0}}
table{{border-collapse:collapse;font-size:12px;width:100%;margin:8px 0}}
td,th{{border:1px solid #dcdcdc;padding:4px 6px;vertical-align:top}}
th{{background:#f4f4f2;font-weight:600;font-size:11.5px;color:#333;position:sticky;top:0;z-index:2}}
tbody tr:nth-child(even){{background:#fbfbfa}}
.nm{{white-space:nowrap;font-weight:600}}
.cd{{color:#999;font-weight:400;font-size:10.5px}}
.core{{color:#1f3864;font-size:11.5px;max-width:210px}}
.biz{{color:#555;white-space:nowrap;font-size:11.5px}}
.attr{{min-width:190px}}
.sc{{color:#A32D2D;font-weight:700}}
.mt{{color:#888;font-size:10.5px}}
.alt{{color:#777;font-size:10.5px;max-width:190px}}
.lay{{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;color:#fff;white-space:nowrap}}
.l-main{{background:#0F6E56}} .l-spill{{background:#BA7517}} .l-chase{{background:#A32D2D}} .l-none{{background:#8a8a8a}} .l-sub{{background:none;border:1px dashed #8a8a8a;color:#666}}
.rl{{display:inline-block;font-size:9.5px;padding:0 4px;margin-right:4px;border-radius:3px;background:#A32D2D;color:#fff;font-weight:400}}
.tag{{display:inline-block;font-size:10.5px;padding:0 5px;margin-right:5px;border-radius:3px;color:#fff}}
.t-zd .tag{{background:#A32D2D}} .t-gc .tag{{background:#185FA5}} .t-hj .tag{{background:#534AB7}}
.t-zd td{{background:#FCEBEB}} .t-gc td{{background:#E6F1FB}} .t-hj td{{background:#EEEDFE}}
.hi{{color:#A32D2D;font-weight:700}}
.box{{background:#faf7ee;border:1px solid #e6dcc0;padding:10px 14px;margin:14px 0;font-size:12.5px}}
.box b{{color:#7a5c00}}
.grid{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}}
.card{{flex:1;min-width:150px;border:1px solid #e0e0e0;border-radius:6px;padding:8px 10px;background:#fff}}
.card .k{{font-size:11px;color:#777}} .card .v{{font-size:20px;font-weight:700}}
.warn{{color:#a33;font-size:12px}}
.legend{{font-size:12px;color:#555;margin:6px 0}}
code{{background:#f2f2f2;padding:0 3px;border-radius:3px;font-size:11px}}
</style></head><body>
<h1>个股画像版 · 盘中扫描（数据日 {D['data_date']}）</h1>
<p class="meta">读数时点 {D['snaptime']}（{I.get('slot','')} 档{'·非标' if not I.get('standard') else ''}） · 生成 {dd}
· 全市场 {I.get('n_snap','—')} 只 · 实时中位 {fmt(D['med'],'%')} · 源 {I.get('src','')}
· 股池 {os.path.basename(D['pool'])}</p>
<p class="legend">本页回答三个问题：<b>这家公司靠什么赚钱</b>（主营构成）／<b>今天资金买的是它的生意吗</b>（归因＋错位度）／<b>整条链今天动的是哪个环节</b>（环节强度榜）。
错位度：<span class="lay l-main">主业驱动</span> 归因＝主业环节 ·
<span class="lay l-spill">链内外溢</span> 同链不同环节 ·
<span class="lay l-chase">跨链蹭概念</span> 主业与归因不同链（情绪票） ·
<span class="lay l-none">未归链</span> 匹配不上，不硬编。</p>

<div class="box">
<b>这一版改了什么（针对「粒度不够细」）</b><br>
① <b>链 11 → 19 条、环节 53 → 108 个</b>：补上原来完全缺失的<b>软件与数据链</b>（网络安全/信创/工业软件/行业 IT/数据要素/云与 SaaS）、
传媒内容、金融、地产基建、公用事业、交运、农业、轻工纺服——网络安全此前无链可归，只能被「人工智能／DeepSeek概念」这类 300+ 只的大标签吸走，
所以启明星辰被标成「AI 应用链·模型」。<br>
② <b>个股身份不再取「标签第 1 个」</b>：旧代码取 <code>题材[0]</code>，而标签串顺序是数据源原样排列——
启明星辰的 DeepSeek概念 恰好在网络安全 前面。现在身份来自<b>主营构成</b>（东财 F10，占比最高项）。<br>
③ <b>今日归因用打分，不用「涨停家数最多」</b>：大桶成分多，涨停家数天然多，环节轮动被稀释。
现在 = 0.42×当日超额 + 0.18×<b>涨停率</b>（家数÷成分数）+ 0.12×广度 + 0.10×焦点位 + 0.18×特异度，
再叠「主业同环节 +35 / 同链 +15 / 泛标签 −12」；且<b>两段式</b>——主业环节自己动了（≥55 分）就是答案，没动才允许归因到链外并标红。<br>
④ <b>行业标签不再冒充题材</b>：东财标签串前 3 词固定是申万一级/二级/三级，以第一个「…板块」为界切分，
「横向通用软件」这种 34 只的三级行业不会再抢走题材榜首。<br>
<b>证据等级：C 级草案</b>（链与环节的划分、归因权重、55 分阈值都是本体系新增，非 233 原文）。
</div>

<div class="grid">
<div class="card"><div class="k">今日涨停</div><div class="v">{tot}</div><div class="k">主板+创业+科创+北交</div></div>
<div class="card"><div class="k">主业驱动</div><div class="v" style="color:#0F6E56">{l_main}</div><div class="k">涨的是自己的生意</div></div>
<div class="card"><div class="k">链内外溢</div><div class="v" style="color:#BA7517">{l_spill}</div><div class="k">同链换环节</div></div>
<div class="card"><div class="k">跨链蹭概念</div><div class="v" style="color:#A32D2D">{l_chase}</div><div class="k">情绪票，非主业</div></div>
<div class="card"><div class="k">未归链</div><div class="v" style="color:#8a8a8a">{l_none}</div><div class="k">匹配不上，不硬编</div></div>
</div>
<p class="meta">情绪条（{I.get('slot','')}）：{sent}</p>

<h2>① 个股画像 × 今日归因（涨停池 {len(pankou)} 只，按「归因环节的当日强弱」分组）</h2>
<p class="meta">「核心业务」＝最新报告期主营构成里占比最高的项（按产品优先，其次按行业；括号内为收入占比与毛利率）。
「主业环节」标 ≈ 表示主营项名太粗、用申万行业兜底得到，属近似。
「今日归因」＝该票所有概念里当日最相关的那个，分数 0—100；括号内是它当日的超额与涨停率，用来判断是「环节级资金」还是「个股独走」。</p>
<table>
<thead><tr><th rowspan="2">名称</th><th rowspan="2">核心业务（占比·毛利）</th><th rowspan="2">主业链·环节</th>
<th rowspan="2">今日归因题材（分）</th><th rowspan="2">错位度</th><th rowspan="2">次相关概念（分）</th>
<th colspan="3">涨停结构</th><th colspan="3">封板质量</th></tr>
<tr><th>连板</th><th>首封</th><th>涨幅%</th><th>封单亿</th><th>占流通%</th><th>炸板</th></tr></thead>
<tbody>{''.join(pk_trs) or '<tr><td colspan="12">（涨停池为空）</td></tr>'}</tbody></table>

<h2>② 环节强度榜（全市场 5576 只 × 108 环节，题材口径聚合）</h2>
<p class="meta">口径：同一环节下所有概念的成分股<b>去重</b>后等权（不是概念简单相加，避免一票多标签重复计入）；
超额＝环节等权涨 − 全市场当日中位。「归因票」＝本页涨停池里归因落到该环节的只数——它是「资金读数」与「个股身份」对上的地方，
两边同时靠前的环节才是今天真正的主线环节。⚠ 未含主力净流入（个股快照无该字段，题材级加总会重复计成分）。</p>
<table><thead><tr><th>#</th><th>链</th><th>环节</th><th>成分(去重)</th><th>综合分</th><th>等权涨</th><th>超额</th>
<th>涨停数</th><th>涨停率</th><th>上涨占比</th><th>归因票</th><th>主要题材</th></tr></thead>
<tbody>{''.join(seg_trs)}</tbody></table>

<h2>③ 跨链蹭概念清单（涨停里「涨的不是自己生意」的那批）</h2>
<p class="meta">判定：今日归因题材与主业不在同一条链。这类票的上涨由外部叙事驱动，<b>主业并没有对应的量价改善</b>；
持有它的理由只能是情绪与身位，一旦题材退潮，缺乏基本面承接。反过来，名单之外的主业驱动票，才是「主题有产业支撑」的部分。</p>
<table><thead><tr><th>名称</th><th>核心业务</th><th>主业链·环节</th><th>今日归因（分）</th><th>归因链·环节</th></tr></thead>
<tbody>{chase_rows or '<tr><td colspan="5">今日涨停全部归因于自己主业所在链（无跨链票）</td></tr>'}</tbody></table>

<h2>④ 焦点题材 × 当日强弱（盘前 A0 焦点，按实时超额排序）</h2>
<table><thead><tr><th>焦点（档位）</th><th>簇/链</th><th>盘前序</th><th>超额</th><th>涨停</th><th>涨股比</th><th>成交额</th><th>主力(亿)</th><th>实时序</th></tr></thead>
<tbody>{fo_trs or '<tr><td colspan="9">（无焦点数据）</td></tr>'}</tbody></table>

<h2>⑤ 在动候选（价格已表态）</h2>
<table><thead><tr><th>名称</th><th>核心业务</th><th>主业链·环节</th><th>今日归因（分）</th><th>错位度</th><th>次相关</th>
<th>池层</th><th>涨幅</th><th>量比/换手</th><th>5日</th><th>成交额</th></tr></thead>
<tbody>{cand_trs("movers") or '<tr><td colspan="11">无</td></tr>'}</tbody></table>

<h2>⑥ 潜伏候选（量能先行 · 价格未跟）</h2>
<table><thead><tr><th>名称</th><th>核心业务</th><th>主业链·环节</th><th>今日归因（分）</th><th>错位度</th><th>次相关</th>
<th>池层</th><th>涨幅</th><th>量比/换手</th><th>5日</th><th>成交额</th></tr></thead>
<tbody>{cand_trs("lurkers") or '<tr><td colspan="11">无</td></tr>'}</tbody></table>

<h2>⑦ 口径、缺口与免责</h2>
<ul class="warn">
<li>主营构成来自东财 F10（{os.path.basename(D['pool'])}/themes.json 提供概念全集），最新报告期以 2026 中报为主；
季报未披露分产品的公司只有年报口径，时滞可达 10 个月——「核心业务」是<b>结构性身份</b>，不是当期景气度。</li>
<li>概念全集来自东财「所属板块」。同一只票平均挂 17 个标签，其中大量是风格/资金/地域词，已按 4 类规则剔除；
剩余标签仍可能包含过泛的大标签（人工智能 725 只、机器人概念 747 只），已用特异度折价，但无法根除。</li>
<li>「今日归因」是当日横截面读数，<b>每天会变</b>——这正是它和「核心业务」分开列的原因：身份不变，归因天天变。</li>
<li>未含：主力净流入（个股级无字段）、涨停原因原始标注（东财该字段未开放，改由概念强度反推）、
北向个股、龙虎榜当日（盘中未出）。缺什么写什么，不编造。</li>
<li>{cfg.get('disclaimer','')}</li>
</ul>
<p class="meta" style="margin-top:26px;color:#999">脚本：<code>stock_profile.py</code>（静态层：主营/概念）、
<code>theme_attr.py</code>（动态层：题材强度+归因）、<code>profile_report.py</code>（本页）。
配置：<code>pool_config.json</code> 19 链 / 108 环节。读数可复算。</p>
</body></html>"""
    out_dir = os.path.join(ROOT, "reports", dt.date.today().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"盘中四时点扫描-{D['data_date']}-个股画像版.html")
    open(path, "w", encoding="utf-8").write(html)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--intraday", default=None)
    ap.add_argument("--pool", default=None)
    a = ap.parse_args()
    D = build(a)
    print("画像", len(D["pics"]), "只 · 环节榜", len(D["seg_rows"]), "行")
    print("已生成", render(D))
