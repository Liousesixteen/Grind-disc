#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘熔炼定位 · 报告生成器（读数块自动填充，判断块留待人工）

设计原则（2026-10-21 定）
    **读数块脚本化，判断块人工写。**
    - 读数块：指数行情、宽度、锚板块前低、波段层级、涨停/炸板/跌停、连板分布、大小盘价差 → 本脚本填
    - 判断块：相位、主线判定、题材四问、梯队、双通道、仓位、复盘 verdict → **不自动生成**
      （这些是判断，不是计算；自动生成只会制造"看起来严谨"的空话）
    未填的判断块渲染为醒目的 .todo 黄块，交付前必须逐个替换。

与体系文件的关系
    结构：`熔炼系统.md` **九 固定输出模板** ＋ `作业节奏.md` **五 固定报告结构**
    检查表：`作业节奏.md` **九 全面使用熔炼系统 强制检查表（19 项）**
    读数口径：`熔炼系统.md` **3.10 指数环境观测**（① 市值轴 / ①b 属性轴 / ③ 锚板块 / ⑤ 波段 / ⑨ 价差）
    本脚本**只产读数，不产判断，也不改动任何体系文件**。

用法
    .venv/bin/python scripts/report_gen.py                 # 最近交易日
    .venv/bin/python scripts/report_gen.py --date 20261020
    .venv/bin/python scripts/report_gen.py --no-breadth    # 跳过板块内宽度（省时间）
    .venv/bin/python scripts/report_gen.py --strict        # 存在未填判断块时以非零码退出（交付前用）

产出
    reports/<YYYY-MM-DD>/大盘熔炼定位-<YYYYMMDD>.html
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import glob
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import level1_indices as L1  # noqa: E402

TEMPLATE = os.path.join(ROOT, "templates", "大盘熔炼定位.html")
CSS = os.path.join(ROOT, "templates", "_base.css")

SYSTEM_VER = "熔炼系统 v1.20"

# 判断块占位文案：写清"该写什么"，不是写结论
TODO = {
    "J_VERDICT": "写 2—4 句：今天是什么相、主导矛盾是什么、给不给仓位。必须落到"
                 "「层1 相位 + 层2 主线 + 层3 双通道 + 层4 仓位」四者的结论上，不许只写行情描述。",
    "J_PHASE": "写相位判定链：引用 3.1 相位表的具体一行，说明为何落在该相、为何不能升级。",
    "J_MAINLINE": "写主线判定：连续性 / 资金 / 集中度 / 宽度四条逐条给证据，结论是「有主线」或「无主线」。",
    "J_CH_A": "通道甲（核心长线）：基本面四周期是否支持、估值是否透支、均线结构是否允许新建。",
    "J_CH_B": "通道乙（233 股池）：股池数据日、池层分布、池内是否有低吸机会、是否触发「池内无标的默认不追」。",
    "J_CROSS": "双通道互检：甲是否通过、乙是否通过、结论是出手还是只登记。",
    "J_DEFENSE": "若已有持仓：上证防守线、个股防守线、纠错处置。不许用指数防守线替代个股。",
    "J_RISK": "列 3—5 条主要风险 ＋ 本节判断的失效条件（触发任一条则结论作废重做）。",
}


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def _cls(v: float) -> str:
    return "up" if v > 0 else ("dn" if v < 0 else "fl")


def _sign(v: float) -> str:
    return f"{v:+.2f}%"


def kpi_cards(date_cn: str, date: str = "") -> tuple[str, dict]:
    """四张 KPI 卡：上证 / 两市额 / 宽度 / 涨停结构。"""
    out, ctx = [], {}
    sh = L1.quote("sh000001")
    sz = L1.quote("sz399001")
    if sh:
        ctx["sh_close"] = sh["close"]
        ctx["sh_chg"] = _pct(sh["close"], sh["prev"])
        ctx["sh_amt"] = sh["amount"]
        out.append(
            f'  <div class="kpi"><div class="t">上证指数</div>'
            f'<div class="v {_cls(ctx["sh_chg"])}">{sh["close"]:.2f}</div>'
            f'<div class="s"><span class="{_cls(ctx["sh_chg"])}">{_sign(ctx["sh_chg"])}</span>'
            f' · 高 {sh["high"]:.2f} / 低 {sh["low"]:.2f} · 额 {sh["amount"]:.0f} 亿</div></div>')
    if sh and sz:
        tot = sh["amount"] + sz["amount"]
        ctx["total_amt"] = tot
        ctx["total_amt_yi"] = tot / 10000
        out.append(
            f'  <div class="kpi"><div class="t">两市成交额</div>'
            f'<div class="v">{tot/10000:.2f} 万亿</div>'
            f'<div class="s">上证 {sh["amount"]:.0f} + 深证 {sz["amount"]:.0f} 亿</div></div>')
    b = L1.breadth("hs_a")
    if b:
        ctx["breadth"] = b
        out.append(
            f'  <div class="kpi"><div class="t">个股上涨占比</div>'
            f'<div class="v up">{b["up_ratio"]:.1f}%</div>'
            f'<div class="s">{b["up"]} / {b["rows"]}（沪深A股实测）</div></div>')
    zt = zt_pool(date)
    if zt:
        ctx["zt"] = zt
        out.append(
            f'  <div class="kpi"><div class="t">涨停 / 炸板 / 跌停</div>'
            f'<div class="v">{zt["zt"]} / {zt["zb"]} / {zt["dt"]}</div>'
            f'<div class="s">炸板率 <b>{zt["zb_rate"]:.1f}%</b> · 连板高 {zt["max_lb"]}</div></div>')
    return "\n".join(out), ctx


def zt_pool(date: str = "") -> dict | None:
    """东财涨停/炸板/跌停池。返回计数与连板分布；取不到返回 None（不编造）。"""
    def one(kind: str, size: str = "300"):
        m = {"ZT": "getTopicZTPool", "DT": "getTopicDTPool", "ZB": "getTopicZBPool"}[kind]
        try:
            d = L1._get("https://push2ex.eastmoney.com/" + m + "?" + L1.urllib.parse.urlencode({
                "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                "Pageindex": "0", "pagesize": size, "sort": "fbt:asc", "date": date,
            }), 20, "utf-8")
            j = json.loads(d)
            return j.get("data") or {}
        except Exception:
            return None
    z, b, dd = one("ZT"), one("ZB"), one("DT", "5")
    if not z:
        return None
    pool = z.get("pool") or []
    lb = {}
    for x in pool:
        k = x.get("lbc") or 1
        lb[k] = lb.get(k, 0) + 1
    zt_n, zb_n = z.get("tc") or 0, (b or {}).get("tc") or 0
    return {"zt": zt_n, "zb": zb_n, "dt": (dd or {}).get("tc") or 0,
            "zb_rate": (zb_n / (zt_n + zb_n) * 100) if (zt_n + zb_n) else 0.0,
            "max_lb": max(lb) if lb else 0,
            "lb_dist": dict(sorted(lb.items())),
            "industries": _top_industries(pool)}


def _top_industries(pool: list, n: int = 10) -> list[tuple[str, int]]:
    c = {}
    for x in pool:
        k = x.get("hybk") or "—"
        c[k] = c.get(k, 0) + 1
    return sorted(c.items(), key=lambda kv: -kv[1])[:n]


# ── 板块榜（腾讯 proxy；只有 sort_type=price|turnover 可用，资金榜需本地排序）──
SKIP_TAG = ("昨日", "连板", "涨停", "触板", "新高", "新低", "破净", "转融券", "融资融券",
            "MSCI", "富时", "标普", "重仓", "预盈", "预增", "高送转", "次新", "低价股",
            "高价股", "百元股", "ST", "股权转让", "举牌", "回购", "增持", "减持",
            "解禁", "摘帽", "填权", "成分股", "标的")


def _noisy(name: str) -> bool:
    return any(k in name for k in SKIP_TAG)


def boards(board_type: str, limit: int = 900) -> list[dict]:
    """腾讯板块榜全量。board_type: gn(概念) / hy(行业)。失败返回空列表。"""
    out = []
    for off in range(0, limit, 100):
        try:
            raw = L1._get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?"
                          + L1.urllib.parse.urlencode(
                              {"board_type": board_type, "sort_type": "price",
                               "direct": "down", "offset": str(off), "count": "100"}),
                          20, "utf-8")
            rl = (json.loads(raw).get("data") or {}).get("rank_list") or []
        except Exception:
            break
        if not rl:
            break
        out.extend(rl)
    return out


def _f(x: dict, k: str, d: float = 0.0) -> float:
    try:
        return float(x.get(k) or d)
    except Exception:
        return d


def board_table(rows: list[dict], title: str, n: int = 10) -> str:
    """涨幅前 n / 资金前 n 双口径表。资金单位：万元 → 亿。"""
    clean = [x for x in rows if not _noisy(x.get("name", ""))]
    if not clean:
        return f'<div class="todo">{title}：板块榜取数失败，请补录（不编造）。</div>'
    up = sorted(clean, key=lambda x: -_f(x, "zdf"))[:n]
    money = sorted(clean, key=lambda x: -_f(x, "zljlr"))[:n]

    def tr(x: dict, show_money: bool = True) -> str:
        zdf = _f(x, "zdf")
        d5 = _f(x, "zdf_d5")
        money_yi = _f(x, "zljlr") / 10000
        lead = (x.get("lzg") or {}).get("name", "")
        return (f"<tr><td>{x.get('name','')[:12]}</td>"
                f"<td class='num {_cls(zdf)}'>{_sign(zdf)}</td>"
                f"<td class='num {_cls(d5)}'>{_sign(d5)}</td>"
                f"<td class='num {_cls(money_yi)}'>{money_yi:+.2f} 亿</td>"
                f"<td class='num'>{x.get('zgb','')}</td>"
                f"<td>{lead}</td></tr>")

    head = ("<table><thead><tr><th>板块</th><th class='num'>当日</th><th class='num'>5日</th>"
            "<th class='num'>主力净额</th><th class='num'>涨股比</th><th>领涨</th></tr></thead><tbody>")
    t1 = head + "".join(tr(x) for x in up) + "</tbody></table>"
    t2 = head + "".join(tr(x) for x in money) + "</tbody></table>"
    return (f"<h4>{title} · 涨幅榜前 {n}（钱在炒什么）</h4>{t1}"
            f"<h4>{title} · 资金榜前 {n}（钱在谁手里）</h4>{t2}"
            f'<p class="src">口径：腾讯板块榜全量（{len(rows)} 个，剔除结果类/属性类标签后 {len(clean)} 个）；'
            f"资金为万元折算亿元；涨股比＝上涨家数/成分数。</p>")


def t_attribute(wave: dict | None = None) -> str:
    """3.10①b 属性轴：科创50 × 创业板指 ＋ 板块内涨股比（两指数背离时必须单列）。"""
    kc, cy = L1.quote("sh000688"), L1.quote("sz399006")
    if not (kc and cy):
        return '<div class="todo">3.10①b 属性轴：科创50 / 创业板指读数缺失。</div>'
    dk = _pct(kc["close"], kc["prev"])
    dc = _pct(cy["close"], cy["prev"])
    bk = L1.breadth("kcb")
    bc = L1.breadth("cyb")
    diverge = "【背离：成长内部换轨，须单列说明资金从哪边切到哪边】" if dk * dc < 0 else ""
    rows = [
        ("科创50 / 科创板", f"{dk:+.2f}%", bk, kc),
        ("创业板指 / 创业板", f"{dc:+.2f}%", bc, cy),
    ]
    body = ""
    for name, chg, b, q in rows:
        ratio = f"{b['up_ratio']:.1f}%（{b['up']}/{b['rows']}）" if b else "缺数据"
        body += (f"<tr><td>{name}</td><td class='num {_cls(float(chg[:-1]))}'>{chg}</td>"
                 f"<td class='num'>{ratio}</td>"
                 f"<td class='num'>{(q['high']-q['low'])/q['prev']*100:.2f}%</td></tr>")
    return ("<table><thead><tr><th>属性轴</th><th class='num'>当日</th>"
            "<th class='num'>板块内涨股比</th><th class='num'>振幅</th></tr></thead><tbody>"
            + body + "</tbody></table>"
            + (f"<p><b>{diverge}</b> 差 {dk - dc:+.2f} pp。</p>" if diverge else "")
            + '<div class="todo">按 3.10①b 四格判读：真涨／假强／被权重砸／换轨，并给出结论与证据等级。</div>')


# ── 股池块（读本地 pipeline 产出，不走网络）──
def pool_block(report_date: str) -> str:
    """读最近一个 <= 报告日的股池快照。**必须标滞后天数**，禁止按当日涨幅倒填。"""
    best, best_date = None, ""
    for f in sorted(glob.glob(os.path.join(ROOT, "cache", "pool_*", "summary.json"))):
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        dd = str(d.get("data_date") or "")
        if dd and dd <= report_date and dd > best_date:
            best, best_date = d, dd
    if not best:
        return ('<div class="todo">股池：cache/pool_*/summary.json 未找到，'
                '请先跑 pool_pipeline 或标注「缺数据（股池未更新）」。</div>')
    lag = _trade_days_between(best_date, report_date)
    fi = best.get("focus_index") or []
    rows = ""
    for x in fi[:10]:
        if isinstance(x, (list, tuple)):
            tier = x[0] if len(x) > 0 else ""
            theme = x[1] if len(x) > 1 else ""
            rank = x[3] if len(x) > 3 else ""
            chain = x[4] if len(x) > 4 else ""
            link = x[5] if len(x) > 5 else ""
            rows += (f"<tr><td>{tier}</td><td>{theme}</td><td class='num'>{rank}</td>"
                     f"<td>{chain}</td><td>{link}</td></tr>")
    warn = "" if lag <= 0 else (
        f'<p class="src" style="color:#c0392b"><b>⚠ 股池数据日 {best_date}，'
        f'晚于报告日 {lag} 个交易日——只能作「池子选了什么方向」的参考，'
        f'<b>不得据此提取当日标的（禁止按当日涨幅倒填）</b>。</b></p>')
    return ("<table><thead><tr><th>项</th><th>读数</th></tr></thead><tbody>"
            f"<tr><td>数据日</td><td><b>{best_date}</b>（运行 {best.get('run_at','')}）</td></tr>"
            f"<tr><td>全A 快照</td><td>{best.get('snapshot_n','—')} 只</td></tr>"
            f"<tr><td>池层分布</td><td>核心 <b>{best.get('tier_core','—')}</b> / "
            f"备选 {best.get('tier_backup','—')} / 观察 {best.get('tier_watch','—')}</td></tr>"
            "</tbody></table>"
            + (f"<table><thead><tr><th>档位</th><th>题材/环节</th><th class='num'>排名</th>"
               f"<th>所在簇</th><th>环节</th></tr></thead><tbody>{rows}</tbody></table>" if rows else "")
            + warn)


def _trade_days_between(d1: str, d2: str) -> int:
    """粗算两个 YYYYMMDD 之间的交易日数（去掉周末；不含节假日，仅作滞后提示）。"""
    try:
        a = dt.date(int(d1[:4]), int(d1[4:6]), int(d1[6:]))
        b = dt.date(int(d2[:4]), int(d2[4:6]), int(d2[6:]))
    except Exception:
        return 0
    if b <= a:
        return 0
    n, cur = 0, a
    while cur < b:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


# ── 宏观块（不取数，只吃标准输入 JSON ＋ 校验口径）──
MACRO_KEYS = {
    "asof": "数据月份（YYYY-MM）",
    "capacity_util": "产能利用率（%，季频）",
    "inventory_yoy": "产成品存货累计同比（%）",
    "profit_yoy": "规上工业利润累计同比（%）",
    "ppi_index": "PPI 同比指数（上年=100，如 105.0）",
    "m1_yoy": "M1 同比（%）",
    "afre_yoy": "社融存量同比（%）",
}


def macro_block(path: str) -> str:
    """读宏观 JSON 并渲染四周期表。**只做口径校验与渲染，不取数、不判方向。**"""
    if not path or not os.path.isfile(path):
        return ('<div class="todo">基本面钟：未提供宏观输入（--macro）。'
                '请用宏观库取数后按 schema 落盘，再重跑；<b>不得用估算值补齐</b>。</div>')
    try:
        d = json.load(io.open(path, encoding="utf-8"))
    except Exception as e:
        return f'<div class="todo">宏观输入解析失败：{e}</div>'
    miss = [k for k in MACRO_KEYS if k not in d]
    ppi = d.get("ppi_index")
    ppi_pct = None
    if isinstance(ppi, (int, float)):
        # 口径陷阱：105.0 是「上年=100」指数 → +5.0%；若误传 5.0 则原样视为百分比
        ppi_pct = ppi - 100 if ppi > 20 else ppi
    rows = [
        ("超长（10 年+）", "技术革命 / 人口 / 制度",
         "背景约束，不取日频读数（3.3 口径）", "不计方向"),
        ("长（3—5 年）", "产能周期",
         f"产能利用率 {d.get('capacity_util','缺')}%", ""),
        ("中（1—2 年）", "库存、盈利周期",
         f"存货 {d.get('inventory_yoy','缺')}%　利润 {d.get('profit_yoy','缺')}%　"
         f"PPI {'+' + format(ppi_pct, '.1f') + '%' if ppi_pct is not None else '缺'}",
         ""),
        ("短（季度）", "情绪 / 流动性 / 季节性",
         f"M1 {d.get('m1_yoy','缺')}%　社融 {d.get('afre_yoy','缺')}%　"
         f"（情绪与季节性见 1.4②）", ""),
    ]
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td>"
                   f"<td class='part'>{e or '待判断'}</td></tr>" for a, b, c, e in rows)
    warn = (f'<p class="src" style="color:#c0392b"><b>⚠ 缺字段：{miss}</b>，已按「缺」渲染，不用估值补齐。</p>'
            if miss else "")
    return ("<table><thead><tr><th>周期（v4.5 口径）</th><th>观察对象</th>"
            "<th>读数</th><th>方向</th></tr></thead><tbody>"
            + body + "</tbody></table>"
            + f'<p class="src">输入：<code>{os.path.relpath(path, ROOT)}</code>　'
              f"asof {d.get('asof','—')}　源 {d.get('source','—')}。"
              f"<b>PPI 已按「上年=100」换算为同比百分点</b>。</p>" + warn
            + '<div class="todo">四周期方向与同向计数需人工判定（超长可判方向，非日频）。</div>')


def t_310(wave: dict | None) -> str:
    """3.10 ①②③⑤⑨ 的读数表（判断留白）。①b 需要宽度，单独由 ctx 提供。"""
    rows = []
    order = ["上证50", "沪深300", "中证500", "中证1000", "国证2000", "北证50",
             "科创50", "创业板指", "证券公司", "证券龙头"]
    code = {n: s for s, n, _ in (L1.MARKET_AXIS + L1.ATTRIBUTE_AXIS + L1.ANCHOR)}
    for n in order:
        s = code.get(n)
        if not s:
            continue
        q = L1.quote(s)
        if not q:
            rows.append(f"<tr><td>{n}</td><td colspan='4' class='miss'>缺数据（接口失败）</td></tr>")
            continue
        chg = _pct(q["close"], q["prev"])
        bars = L1.kline(s, 12)
        cl = [float(x["close"]) for x in bars]
        d5 = _pct(cl[-1], cl[-6]) if len(cl) >= 6 else None
        rows.append(
            f"<tr><td>{n}</td><td class='num {_cls(chg)}'>{_sign(chg)}</td>"
            f"<td class='num'>{'—' if d5 is None else _sign(d5)}</td>"
            f"<td class='num'>{(q['high']-q['low'])/q['prev']*100:.2f}%</td>"
            f"<td class='num'>{q['amount']:.0f}亿</td></tr>")
    tbl = ("<table><thead><tr><th>指数</th><th class='num'>当日</th><th class='num'>5日</th>"
           "<th class='num'>振幅</th><th class='num'>成交额</th></tr></thead><tbody>"
           + "".join(rows) + "</tbody></table>")

    extra = ""
    if wave:
        c, pv, pr = wave["本波"], wave["前波"], wave["父浪"]
        m = wave["倍数律"] or {}
        bonds = "；".join(f"{t} 父{d} vs 子{s} → {'✓' if ok else '✗ 颠倒'}"
                          for t, d, s, ok in wave["辈分"])
        extra = ("<table><thead><tr><th>3.10⑤ 波段层级</th><th>读数</th></tr></thead><tbody>"
                 f"<tr><td>本波（日线 3%）</td><td>{c['from']} → {c['to']}，{c['bars']} 日，{c['chg']:+.2f}%</td></tr>"
                 + (f"<tr><td>前波（同向）</td><td>{pv['from']} → {pv['to']}，{pv['bars']} 日，{pv['chg']:+.2f}%</td></tr>" if pv else "")
                 + (f"<tr><td>倍数律</td><td>时间 {m.get('时间比',0):.2f}×　空间 {m.get('空间比',0):.2f}×</td></tr>" if m else "")
                 + (f"<tr><td>父浪（日线 8%）</td><td>{pr['from']} → {pr['to']}，{pr['bars']} 日，{pr['chg']:+.2f}%</td></tr>" if pr else "")
                 + f"<tr><td>辈分校验</td><td>{bonds}</td></tr>"
                 "</tbody></table>")
    todo = ('<div class="todo">3.10 ①（哑铃/橄榄）、②（强弱共振）、⑧（钝化）、'
            '⑫（3 浪否定）为<b>判断块</b>：请按读数逐条给出结论与证据等级。</div>')
    return tbl + extra + todo


def t_sentiment(ctx: dict) -> str:
    z = ctx.get("zt") or {}
    if not z:
        return '<div class="todo">情绪钟读数缺失（涨停池接口失败），请补录后填写。</div>'
    lb = "、".join(f"{k} 板 {v}" for k, v in (z.get("lb_dist") or {}).items())
    ind = "、".join(f"{k} {v}" for k, v in (z.get("industries") or []))
    return ("<table><thead><tr><th>读数</th><th>今日</th><th>说明</th></tr></thead><tbody>"
            f"<tr><td>涨停 / 炸板 / 跌停</td><td>{z['zt']} / {z['zb']} / {z['dt']}</td>"
            f"<td>炸板率 {z['zb_rate']:.1f}%</td></tr>"
            f"<tr><td>连板高度 / 分布</td><td>最高 {z['max_lb']} 板</td><td>{lb}</td></tr>"
            f"<tr><td>涨停行业分布</td><td colspan='2'>{ind}</td></tr>"
            "</tbody></table>"
            '<div class="todo">相位（蓄势/启动/拉升/派发/退潮）与「昨日涨停今日表现」需人工判定并填证据等级。</div>')


def t_budget(ctx: dict) -> str:
    return ('<div class="todo">风险预算三步：①环境系数 × 风格基准仓（基准仓为个人参数，须显式声明假设值）；'
            '②证据系数 × 计划仓（来自层3 双通道）；③硬顶（A0）。三者取 min 得当日可开仓上限，'
            '并说明<b>是环境压到 0 还是证据压到 0</b>。</div>')


def t_checklist() -> str:
    items = [
        ("1", "三层耦合分层", "熔炼系统 一 世界观：三层耦合振荡模型"),
        ("2", "公理对照 A0—A5", "熔炼系统 二 六条公理（全体系的公共不变量）"),
        ("3", "相位三列 ＋ 龙头上限", "熔炼系统 3.1 相位语言"),
        ("4", "量价六象限", "熔炼系统 3.2 量价六象限"),
        ("5", "时间尺度 ＋ 两铁律", "熔炼系统 3.3 时间尺度谱系"),
        ("6", "板块双口径 ＋ 宽度 ＋ 连续性", "熔炼系统 3.4 板块双口径观测"),
        ("7", "日内钟四时点", "熔炼系统 3.5 日内资金选择时钟"),
        ("8", "股池口径", "熔炼系统 3.6 股池三筛选量化口径与股池分层"),
        ("9", "翻译表（跨体系才用）", "熔炼系统 3.7 体系翻译表"),
        ("10", "题材主口径 ＋ 四问 ＋ 梯队", "熔炼系统 3.8 板块主口径＝题材"),
        ("11", "五条数据铁律", "熔炼系统 3.9 五条数据铁律"),
        ("12", "3.10 ①—⑬ ＋ ①b 全查", "熔炼系统 3.10 指数环境观测"),
        ("13", "六层漏斗逐层结论", "熔炼系统 四 统一引擎（层0—层5，层号引用天然稳定）"),
        ("14", "仓位方程 ＋ 分账 ＋ 防火墙", "熔炼系统 五 仓位总方程"),
        ("15", "离场四档 ＋ 防守线", "熔炼系统 六 离场统一理论（含被套处置矩阵）"),
        ("16", "本能反应全表", "熔炼系统 七 本能反应总表"),
        ("17", "证据等级逐条标注", "熔炼系统 八 证据等级与自身验证清单"),
        ("18", "输出模板 9 条", "熔炼系统 九 固定输出模板"),
        ("19", "体系选择声明", "熔炼系统 十一 体系选择协议"),
    ]
    rows = "".join(
        f"<tr><td>{n}</td><td>{name}</td><td class='part'>待过</td>"
        f"<td>{ref}</td></tr>" for n, name, ref in items)
    return ("<table><thead><tr><th>#</th><th>检查项</th><th>状态</th><th>熔炼系统位置</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            '<div class="todo">19 项逐项打勾：过＝✓、部分＝◐、未过＝✗、不适用＝未用（原因）、取不到＝缺数据（原因）。'
            '未填满不得交付。</div>')


def t_source(ctx: dict) -> str:
    rows = [
        ("指数行情 / 成交额", "新浪 hq.sinajs（与腾讯 qt.gtimg 交叉验证）", ctx.get("data_date", ""), "✓"),
        ("个股涨跌家数", "新浪 Market_Center node=hs_a 全量逐页", ctx.get("data_date", ""), "✓"),
        ("涨停 / 炸板 / 跌停", "东财 push2ex 涨停池", ctx.get("data_date", ""),
         "✓" if ctx.get("zt") else "缺数据"),
        ("科创50 / 创业板内宽度", "新浪 Market_Center node=kcb / cyb", ctx.get("data_date", ""), "✓"),
        ("板块双口径榜", "腾讯板块榜（行业＋概念）", "—", "缺数据（本脚本未接入）"),
        ("基本面四周期", "宏观库（产能利用率/存货/利润/PPI/M1/社融）", "—", "缺数据（本脚本未接入）"),
        ("股池", "本地 pipeline", "—", "缺数据（本脚本未接入）"),
    ]
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{'✓' if d=='✓' else '◐/'+d if d!='缺数据' else d}</td></tr>"
                   for a, b, c, d in rows)
    return ("<table><thead><tr><th>数据</th><th>来源</th><th>数据日</th><th>状态</th></tr></thead>"
            f"<tbody>{body}</tbody></table>"
            '<div class="todo">补齐「板块榜 / 宏观四周期 / 股池」三项来源，并在缺口处写明原因（3.9 第①条：取不到≠不存在）。</div>')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="数据日期 YYYYMMDD；留空取最新")
    ap.add_argument("--no-breadth", action="store_true")
    ap.add_argument("--strict", action="store_true", help="存在未填判断块时以非零码退出")
    ap.add_argument("--write-canonical", action="store_true",
                    help="写正式文件名（大盘熔炼定位-<date>.html）；默认写 -模板生成.html，避免覆盖已交付稿")
    ap.add_argument("--force", action="store_true", help="目标已存在时允许覆盖（覆盖前自动备份）")
    ap.add_argument("--macro", default="",
                    help="宏观输入 JSON 路径（不取数，只校验口径并渲染四周期表）")
    args = ap.parse_args()

    if not os.path.isfile(TEMPLATE):
        print("模板缺失：", TEMPLATE)
        return 2
    tpl = io.open(TEMPLATE, encoding="utf-8").read()
    css = io.open(CSS, encoding="utf-8").read() if os.path.isfile(CSS) else ""

    q = L1.quote("sh000001") or {}
    date = args.date or (q.get("date", "").replace("-", ""))
    if not date:
        print("无法确定数据日期，请用 --date 指定")
        return 2
    date_cn = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    ctx = {"data_date": date}
    cards, kctx = kpi_cards(date_cn, date)
    ctx.update(kctx)
    wave = L1.wave_check("sh000001")
    print("  · 取板块榜（行业/概念）…", end="", flush=True)
    hy, gn = boards("hy"), boards("gn")
    print(f" 行业 {len(hy)} 个 / 概念 {len(gn)} 个")

    rep = {
        "TITLE": f"大盘熔炼定位 · {date_cn}",
        "DATE_CN": date_cn,
        "SYSTEM_VER": SYSTEM_VER,
        "GENERATED_AT": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "CSS": css,
        "KPI_CARDS": cards,
        "PHASE": "待判断",
        "PHASE_LEVEL": "—",
        "T_310": t_310(wave) + t_attribute(wave),
        "T_INDUSTRY": board_table(hy, "行业层（31 个）"),
        "T_CONCEPT": board_table(gn, "概念层（805 个）"),
        "T_POOL": pool_block(date),
        "T_MACRO": macro_block(getattr(args, "macro", "")),
        "T_SENTIMENT": t_sentiment(ctx),
        "T_BUDGET": t_budget(ctx),
        "T_CHECKLIST": t_checklist(),
        "T_SOURCE": t_source(ctx),
    }
    # 判断块 → 醒目占位
    for k, hint in TODO.items():
        rep[k] = f'<div class="todo" data-slot="{k}">{hint}</div>'
    # 未接入的读数块 → 同样占位（不编造）
    for k, hint in {
        "T_QUADRANT": "1.2 量价六象限：按 3.2 给每个指数的象限（缩量涨/缩量跌/放量涨…），需人工判定。",
        "T_ENVCOEF": "双钟联合查表：按 五② 原表逐档对照，命中哪一档、是否触发一票否决。",
        "T_SUCK": "吸血检查三项：事件能级 × 拥挤度 × 对手盘吸引力，各给评级与证据。",
        "T_CONTINUITY": "连续性检验（相对超额 ≥2 日），需接入板块榜 5 日数据。",
        "T_4Q": "题材判定四问：级别／催化硬核度／板块容量／预期差。",
        "T_LADDER": "梯队三角色：龙头／中军／跟风，中军是否倒。",
        "T_SUBLINE": "支线题材：催化＋龙头＋连续天数＋位置，只跟踪不追高。",
        "T_EVOLUTION": "板块演化链四态定位 ＋ 总市值排名前二（易位需连续 3 回合）。",
        "T_POSITION": "仓位方程三层取小：硬顶 0 / 环境系数 × 基准仓 / 证据系数 × 计划仓。",
        "LEVEL_CARDS": "三张关键位卡：防守线、小右侧门槛、右侧确认。",
        "T_REVIEW": "层5 复盘表：上一交易日每条判断的验证结果（成立／修订／作废）。",
        "T_PLAN": "次日预案：2—3 条二值可验证的情景与对应动作。",
    }.items():
        rep[k] = f'<div class="todo" data-slot="{k}">{hint}</div>'

    out = tpl
    for k, v in rep.items():
        out = out.replace("{{%s}}" % k, str(v))

    missing = [k for k in ("{{%s}}" % x for x in rep) if k in out]
    todo_n = out.count('class="todo"')

    out_dir = os.path.join(ROOT, "reports", date_cn)
    os.makedirs(out_dir, exist_ok=True)
    suffix = "" if args.write_canonical else "-模板生成"
    path = os.path.join(out_dir, f"大盘熔炼定位-{date}{suffix}.html")

    # ★ 防覆盖（2026-10-21 事故后加）：默认写草稿名；正式名需显式 --write-canonical；
    #   任何已存在的目标，覆盖前一律先备份到 .workbuddy/backups/reports/
    if os.path.exists(path):
        if not args.force:
            print("=" * 78)
            print(f"✗ 目标已存在，拒绝覆盖：{os.path.relpath(path, ROOT)}")
            print("  已交付稿不得被生成器冲掉。可选：")
            print("    · 加 --force（覆盖前自动备份到 .workbuddy/backups/reports/）")
            print("    · 或去掉 --write-canonical，生成到 -模板生成.html 草稿")
            print("=" * 78)
            return 3
        bak = os.path.join(ROOT, ".workbuddy", "backups", "reports")
        os.makedirs(bak, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(bak, os.path.basename(path) + f".{stamp}.bak")
        io.open(dst, "w", encoding="utf-8").write(io.open(path, encoding="utf-8").read())
        print(f"  ↳ 已备份原文件 → {os.path.relpath(dst, ROOT)}")

    io.open(path, "w", encoding="utf-8").write(out)

    print("=" * 78)
    print(f"报告已生成：{os.path.relpath(path, ROOT)}")
    print(f"  数据日 {date_cn}　体积 {len(out.encode())/1024:.1f} KB")
    print(f"  读数块已填：KPI / 3.10 指数表 / 3.10⑤ 波段 / 情绪池 / 附录A 骨架 / 附录B")
    print(f"  ⚠ 待人工填写：{todo_n} 个判断块（渲染为黄色虚线框，交付前必须替换）")
    if missing:
        print(f"  ⚠ 未替换占位符：{missing}")
    print("  下一步：填判断块 → 附录A 打勾 → node --check（如有 JS）→ 交付")
    print("=" * 78)
    if args.strict and (todo_n or missing):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
