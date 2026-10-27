#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""层1 固定指数读数器（熔炼系统 3.10 指数环境观测 的取数口径）

用途
    一次取齐层1 必读的全部指数，并对「锚板块（证券）」自动做前低比对。
    起因：2026-10-20 复盘用「非银金融」当证券的代理，把 3.10③ 的锚板块读数
    写成「缺数据」——其实证券指数随时可取，是取数清单里没它。
    → 本脚本把「该取哪些」变成代码，不靠记性。

对应关系（方法见 熔炼系统.md，本脚本只执行不定义）
    3.10①  市值轴三段：上证50 / 沪深300 / 中证1000 + 国证2000（+ 北证50）
    3.10①b 属性轴：科创50 × 创业板指（必须并列读，含板块内涨股比）
    3.10③  锚板块：证券公司 399975（主）× 证券龙头 399437（交叉验证），须与前低比对
    3.10⑨  中证1000 − 中证500 价差

数据源
    新浪 hq.sinajs.cn（实时/收盘）、新浪 K 线（前低）；两处同源但接口独立。
    腾讯 qt.gtimg.cn 可作交叉验证（本脚本不内置，需人工抽查时用）。

输出
    控制台表格 + cache/level1/<数据日期>.json

用法
    .venv/bin/python scripts/level1_indices.py            # 最近交易日
    .venv/bin/python scripts/level1_indices.py --date 20261020
    .venv/bin/python scripts/level1_indices.py --no-breadth   # 跳过板块内宽度（较慢）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

# ── 固定清单（顺序即输出顺序；改这里＝改取数口径）────────────────────────────
MARKET_AXIS = [
    ("sh000016", "上证50", "权重"),
    ("sh000300", "沪深300", "中盘"),
    ("sh000905", "中证500", "中盘"),
    ("sh000852", "中证1000", "小盘"),
    ("sz399303", "国证2000", "小盘(中证2000代理)"),
    ("bj899050", "北证50", "微盘"),
]
ATTRIBUTE_AXIS = [
    ("sh000688", "科创50", "属性·硬科技"),
    ("sz399006", "创业板指", "属性·成长权重"),
]
ANCHOR = [
    ("sz399975", "证券公司", "锚板块·主"),
    ("sz399437", "证券龙头", "锚板块·交叉验证"),
]
REFERENCE = [
    ("sh000001", "上证指数", "基准"),
]
# 板块内宽度（新浪市场中心节点）
BREADTH_NODES = [("kcb", "科创板"), ("cyb", "创业板"), ("hs_a", "沪深A股")]


def _get(url: str, timeout: int = 20, encoding: str = "gbk") -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


def quote(sym: str) -> dict | None:
    """新浪实时/收盘快照。返回 close/prev/high/low/amount(亿)/ts。"""
    try:
        body = _get("https://hq.sinajs.cn/list=" + sym).split('"')
        if len(body) < 2 or not body[1]:
            return None
        p = body[1].split(",")
        return {
            "close": float(p[3]), "prev": float(p[2]),
            "high": float(p[4]), "low": float(p[5]),
            "amount": float(p[9]) / 1e8 if p[9] else 0.0,
            "date": p[30] if len(p) > 30 else "",
            "ts": p[31] if len(p) > 31 else "",
        }
    except Exception:
        return None


def kline(sym: str, n: int = 140) -> list[dict]:
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    try:
        return json.loads(_get(url, encoding="utf-8"))
    except Exception:
        return []


def prior_low(sym: str, lookback: int = 60) -> dict | None:
    """锚板块前低：近 lookback 根内、排除最后一根之后的盘中最低。

    3.10③ 的口径是「破不破前低」，所以用**盘中最低**而不是收盘最低——
    收盘价会把「插破又收回」的情况判成未破，低估警报强度。
    """
    bars = kline(sym, max(lookback + 10, 80))
    if len(bars) < 6:
        return None
    rows = [(b["day"][:10], float(b["low"]), float(b["close"])) for b in bars]
    body, last = rows[:-1], rows[-1]
    seg = body[-lookback:]
    lo_day, lo_val, _ = min(seg, key=lambda x: x[1])          # 近端低（近 lookback 根最低）
    # 波段低：3% 阈值 ZigZag 的最后一个确认低点（口径见波哥体系 v3.0 的代理替换）
    piv, trend, ref, refd = [], 0, body[0][2], body[0][0]
    for d, _lo, c in body[1:]:
        if trend >= 0:
            if c > ref: ref, refd = c, d
            elif c <= ref * 0.97: piv.append((refd, ref, "H")); trend, ref, refd = -1, c, d
        if trend <= 0:
            if c < ref: ref, refd = c, d
            elif c >= ref * 1.03: piv.append((refd, ref, "L")); trend, ref, refd = 1, c, d
    lows = [(d, c) for d, c, t in piv if t == "L"]
    sw_day, sw_close = lows[-1] if lows else (lo_day, lo_val)
    # 波段低取该转折日附近的盘中最低（转折点用收盘识别，价位用盘中）
    idx = {x[0]: k for k, x in enumerate(body)}
    k0 = idx.get(sw_day, len(body) - 1)
    sw_low = min((body[k][1] for k in range(max(0, k0 - 3), min(len(body), k0 + 4))),
                 default=sw_close)
    return {"low_date": lo_day, "low": lo_val,
            "swing_date": sw_day, "swing_low": sw_low,
            "today_low": last[1], "today_close": last[2],
            "today_date": last[0]}


def _zigzag(prices: list[tuple[str, float]], th: float) -> list[tuple[str, float, str, int]]:
    """严谨状态机 ZigZag：返回 [(日期, 价格, 'H'|'L', 序号)]。

    坑（2026-10-20 踩过）：初版用 `if trend>=0` 与 `if trend<=0` 两个独立分支，
    在 trend==0 时两段都会跑、互相覆盖 ref，导致 8% 阈值只剩 1 个转折。
    此处改为显式状态（0 未定 / 1 上 / -1 下），未定态先等「首个超过 th 的方向」。
    """
    piv: list[tuple[str, float, str, int]] = []
    d0, p0 = prices[0]
    hi_i, hi_p, lo_i, lo_p = 0, p0, 0, p0
    dirn, ei, ep = 0, 0, p0
    for i in range(1, len(prices)):
        c = prices[i][1]
        if dirn == 0:
            if c > hi_p: hi_i, hi_p = i, c
            if c < lo_p: lo_i, lo_p = i, c
            if c >= lo_p * (1 + th):
                piv.append((prices[lo_i][0], lo_p, "L", lo_i)); dirn, ei, ep = 1, i, c
            elif c <= hi_p * (1 - th):
                piv.append((prices[hi_i][0], hi_p, "H", hi_i)); dirn, ei, ep = -1, i, c
        elif dirn == 1:
            if c > ep: ei, ep = i, c
            elif c <= ep * (1 - th):
                piv.append((prices[ei][0], ep, "H", ei)); dirn, ei, ep = -1, i, c
        else:
            if c < ep: ei, ep = i, c
            elif c >= ep * (1 + th):
                piv.append((prices[ei][0], ep, "L", ei)); dirn, ei, ep = 1, i, c
    piv.append((prices[ei][0], ep, "H" if dirn == 1 else "L", ei))
    return piv


# 级别 ↔ 周期 ↔ 数据量 ↔ 阈值（2026-10-20 修订：**按周期定级别**，不再用同一周期切三档冒充三级）
WAVE_LEVELS = [
    ("周线", 1200, 600, 0.03),   # 大级别：周线 600 根 ≈ 11 年
    ("日线", 240, 1600, 0.03),   # 中级别：日线 1600 根 ≈ 6.5 年
    ("60分钟", 60, 800, 0.015),  # 小级别：60 分钟 800 根 ≈ 10 个月
]
# 根数→交易日的粗略换算（只用于跨级别比较，不用于精确计时）
_TO_TDAY = {"周线": 5.0, "日线": 1.0, "60分钟": 0.25}


def _load(sym: str, scale: int, n: int) -> list[tuple[str, float]]:
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={n}")
    try:
        bars = json.loads(_get(url, 30, encoding="utf-8"))
    except Exception:
        return []
    return [(b["day"], float(b["close"])) for b in bars]


def _legs(P: list[tuple[str, float]], th: float) -> list[dict]:
    pv = _zigzag(P, th)
    out = []
    for k in range(1, len(pv)):
        d1, v1, _, i1 = pv[k - 1]
        d2, v2, t2, i2 = pv[k]
        out.append({"from": d1, "to": d2, "v1": v1, "v2": v2,
                    "bars": i2 - i1, "chg": (v2 / v1 - 1) * 100, "type": t2})
    return out


def _contains(outer: dict, inner: dict) -> bool:
    """父段必须在时间上**完全包含**子段。不满足就不是父子关系，不得拿来做辈分校验。"""
    return outer["from"] <= inner["from"] and outer["to"] >= inner["to"]


def wave_check(sym: str = "sh000001") -> dict | None:
    """3.10⑤ 可执行件：按周期定级别 ＋ 倍数律 ＋ 辈分校验 ＋ 两项自检。

    ## 为什么要改（2026-10-20）
    初版用「日线 + 8%/3%/1.5% 三档阈值」冒充大/中/小三个级别。**阈值不等于级别**——
    三档只是同一周期的三个尺度。真去核周线就露馅：周线 3% 的「父浪」是 8-28→10-16（7 周），
    而日线本波是 8-18→10-15，**起点差 10 天、不构成包含关系**。原因是周线用**周末收盘**采样，
    把 8-18 那周的日线高点漏掉了（8-18 当周收 3905.20，低于 8-28 当周 3952.18）。
    → 故本版改为：**级别＝周期**，父浪优先取「同周期更大阈值且真正包含本波」的段；
      周线段只有在**包含**本波时才当父浪，否则明确报「级别未对齐」，不强行校验。

    ## 两项自检（都不过就不能出结论）
    1. **窗口敏感性**：同一级别用 n 与 n/2 根各跑一次，最后一段的起止日期必须一致；
    2. **父浪包含性**：父段起止必须包住本段，否则判「级别未对齐」。

    口径：阈值作用在**收盘价**；耗时按**根数**计，跨级别比较时按 `_TO_TDAY` 粗换算成交易日。
    """
    lv = {}
    for name, scale, n, th in WAVE_LEVELS:
        P = _load(sym, scale, n)
        if len(P) < 80:
            continue
        legs = _legs(P, th)
        if not legs:
            continue
        half = _legs(P[-max(80, n // 2):], th)
        stable = bool(half) and legs[-1]["from"] == half[-1]["from"] and legs[-1]["to"] == half[-1]["to"]
        lv[name] = {"legs": legs, "th": th, "bars": len(P), "stable": stable,
                    "span": f"{P[0][0]}~{P[-1][0]}"}
    if "日线" not in lv:
        return None

    daily = lv["日线"]["legs"]
    cur = daily[-1]
    prev = next((x for x in reversed(daily[:-1]) if x["type"] == cur["type"]), None)

    # 父浪候选一：同周期更大阈值（8%），必须包含本波
    parent = None
    coarse = _legs(_load(sym, 240, max(3200, 1600)), 0.08)
    for x in reversed(coarse):
        if _contains(x, cur) and x["bars"] > cur["bars"]:
            parent = dict(x, src="日线8%")
            break

    # 父浪候选二：周线段（仅当包含本波）
    wk = lv.get("周线", {}).get("legs") or []
    wk_parent = next((x for x in reversed(wk) if _contains(x, cur)), None)
    wk_aligned = wk_parent is not None

    # 子浪：60 分钟落在本波内的段
    mn = lv.get("60分钟", {}).get("legs") or []
    kids = [x for x in mn if _contains(cur, x)]
    max_kid = max(kids, key=lambda x: x["bars"]) if kids else None

    # 辈分校验（统一换算成交易日）
    def tday(x):
        return x["bars"] * _TO_TDAY.get(x.get("lv", "日线"), 1.0)
    bonds = []
    if parent:
        bonds.append(("父(日线8%) vs 本波(日线3%)", parent["bars"], cur["bars"],
                      parent["bars"] >= cur["bars"]))
    if wk_aligned:
        bonds.append(("父(周线3%) vs 本波(日线3%)", wk_parent["bars"] * 5, cur["bars"],
                      wk_parent["bars"] * 5 >= cur["bars"]))
    if max_kid:
        bonds.append(("本波(日线3%) vs 子(60min)", cur["bars"], round(max_kid["bars"] * 0.25, 1),
                      cur["bars"] >= max_kid["bars"] * 0.25))

    ratio = None
    if prev and prev["bars"]:
        ratio = {"时间比": cur["bars"] / prev["bars"],
                 "空间比": abs(cur["chg"]) / abs(prev["chg"]) if prev["chg"] else None}

    return {"levels": lv, "本波": cur, "前波": prev, "父浪": parent,
            "周线父浪": wk_parent, "周线对齐": wk_aligned,
            "最长子浪": max_kid, "倍数律": ratio, "辈分": bonds,
            "不稳定级别": [k for k, v in lv.items() if not v["stable"]]}


def breadth(node: str) -> dict | None:
    """板块内涨跌家数：全量逐页，不用户数估算（3.10①b 数据口径）。"""
    try:
        cnt = int(re.sub(r"\D", "", _get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"Market_Center.getHQNodeStockCountSimple?node={node}")) or 0)
    except Exception:
        return None
    if not cnt:
        return None
    up = dn = fl = rows = 0
    for pg in range(1, cnt // 100 + 2):
        try:
            arr = json.loads(_get(
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"Market_Center.getHQNodeData?page={pg}&num=100&sort=changepercent&asc=0"
                f"&node={node}&symbol=&_s_r_a=page"))
        except Exception:
            continue
        if not arr:
            break
        for x in arr:
            try:
                v = float(x.get("changepercent") or 0)
            except Exception:
                continue
            rows += 1
            up += v > 0
            dn += v < 0
            fl += v == 0
    if not rows:
        return None
    return {"total": cnt, "rows": rows, "up": up, "down": dn, "flat": fl,
            "up_ratio": up / rows * 100}


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="数据日期 YYYYMMDD；留空取最新")
    ap.add_argument("--no-breadth", action="store_true", help="跳过板块内宽度（省时间）")
    ap.add_argument("--wave", action="store_true", help="附 3.10⑤ 波段层级＋倍数律＋辈分校验（多取 800 根日线）")
    args = ap.parse_args()

    rows, quotes = [], {}
    for group, items in (("市值轴", MARKET_AXIS), ("属性轴", ATTRIBUTE_AXIS),
                         ("锚板块", ANCHOR), ("基准", REFERENCE)):
        for sym, name, role in items:
            q = quote(sym)
            if not q:
                rows.append({"组": group, "指数": name, "代码": sym, "角色": role,
                             "状态": "缺数据"})
                continue
            quotes[sym] = q
            bars = kline(sym, 12)
            cl = [float(b["close"]) for b in bars]
            d5 = pct(cl[-1], cl[-6]) if len(cl) >= 6 else None
            rows.append({
                "组": group, "指数": name, "代码": sym, "角色": role,
                "收盘": q["close"], "涨跌%": pct(q["close"], q["prev"]),
                "最高": q["high"], "最低": q["low"],
                "振幅%": (q["high"] - q["low"]) / q["prev"] * 100 if q["prev"] else 0,
                "5日%": d5, "额亿": q["amount"], "时点": q["ts"], "状态": "ok",
            })

    date = args.date or quotes.get("sh000001", {}).get("date", "").replace("-", "")
    print("=" * 96)
    print(f"层1 固定指数读数　数据日期 {date}　（熔炼系统 3.10 指数环境观测 口径）")
    print("=" * 96)
    print(f"{'组':6s}{'指数':10s}{'收盘':>10s}{'涨跌':>8s}{'5日':>8s}{'振幅':>7s}{'额(亿)':>9s}  角色")
    for r in rows:
        if r.get("状态") != "ok":
            print(f"{r['组']:6s}{r['指数']:10s}{'—':>10s}{'缺数据':>8s}")
            continue
        d5 = f"{r['5日%']:+7.2f}%" if r["5日%"] is not None else "      —"
        print(f"{r['组']:6s}{r['指数']:10s}{r['收盘']:10.2f}{r['涨跌%']:+7.2f}%{d5}"
              f"{r['振幅%']:6.2f}%{r['额亿']:9.0f}  {r['角色']}")

    # ── 3.10③ 锚板块前低比对（本脚本存在的首要理由）──
    print("\n【3.10③ 锚板块前低比对】证券破前低 ⇒ 结构性警报；不破 ⇒ 上升结构未坏")
    anchor_out = {}
    for sym, name, _ in ANCHOR:
        pl = prior_low(sym)
        if not pl:
            print(f"  {name}: 缺数据（K 线取不到）")
            continue
        g_near = (pl["today_low"] / pl["low"] - 1) * 100
        g_swing = (pl["today_low"] / pl["swing_low"] - 1) * 100
        broke = pl["today_low"] < pl["swing_low"]
        anchor_out[name] = dict(pl, broke=broke, gap_near=g_near, gap_swing=g_swing)
        mark = lambda g: "✗破" if g < 0 else "✓未破"
        print(f"  {name}({sym}): 今日 {pl['today_date']} 低 {pl['today_low']:.2f} 收 {pl['today_close']:.2f}")
        print(f"      近端低 {pl['low_date']} {pl['low']:.2f} → {g_near:+.2f}% {mark(g_near)}"
              f"　｜　波段低 {pl['swing_date']} {pl['swing_low']:.2f} → {g_swing:+.2f}% {mark(g_swing)}")

    # 对照：上证50 是否同步破位（分化 = 结构性，而非系统性）
    pl50 = prior_low("sh000016")
    if pl50:
        g50 = (pl50["today_low"] / pl50["swing_low"] - 1) * 100
        b50 = pl50["today_low"] < pl50["swing_low"]
        note = "✗ 同破位（系统性）" if b50 else "✓ 未破（锚先破、权重未破＝结构性分化）"
        print(f"  对照 上证50: 今日低 {pl50['today_low']:.2f} ｜ 波段低 {pl50['swing_date']} "
              f"{pl50['swing_low']:.2f} → {g50:+.2f}% ｜ {note}")
        anchor_out["上证50"] = dict(pl50, broke=b50, gap_swing=g50)

    # ── 3.10①b 属性轴：科创50 × 创业板指 ──
    print("\n【3.10①b 属性轴】科创50 × 创业板指必须并列读（背离＝成长内部换轨）")
    kc, cy = quotes.get("sh000688"), quotes.get("sz399006")
    bd = {}
    if kc and cy:
        print(f"  科创50 {pct(kc['close'], kc['prev']):+.2f}%  vs  创业板指 {pct(cy['close'], cy['prev']):+.2f}%  "
              f"→ 差 {pct(kc['close'], kc['prev']) - pct(cy['close'], cy['prev']):+.2f} pp"
              f"{'　【背离：成长内部换轨，须单列说明资金从哪边切到哪边】' if (pct(kc['close'], kc['prev']) * pct(cy['close'], cy['prev']) < 0) else ''}")
    if not args.no_breadth:
        for node, label in BREADTH_NODES:
            b = breadth(node)
            if b:
                bd[label] = b
                print(f"  {label} 内部宽度: 涨 {b['up']} / 跌 {b['down']} / 平 {b['flat']}"
                      f"　涨占比 {b['up_ratio']:.1f}%（{b['rows']}/{b['total']} 只已取）")
            else:
                print(f"  {label} 内部宽度: 缺数据")

    # ── 3.10⑨ 中证1000 − 中证500 ──
    i1000, i500 = quotes.get("sh000852"), quotes.get("sh000905")
    if i1000 and i500:
        print(f"\n【3.10⑨ 大小盘价差】中证1000 − 中证500 = "
              f"{i1000['close'] - i500['close']:+.2f} 点"
              f"（{'倒挂' if i1000['close'] < i500['close'] else '正挂'}）")

    # ── 3.10⑤ 波段层级 / 倍数律 / 辈分校验 ──
    wave = None
    if args.wave:
        wave = wave_check("sh000001")
        print("\n【3.10⑤ 波段层级 · 倍数律 · 辈分校验】（上证；级别＝周期，收盘价口径）")
        if not wave:
            print("  缺数据（日线不足或接口失败）")
        else:
            for name, v in wave["levels"].items():
                mark = "✓" if v["stable"] else "✗ 窗口敏感（n 与 n/2 结果不一致，结论不可用）"
                print(f"  {name:6s} {v['bars']:4d} 根  阈值 {v['th']*100:.1f}%  "
                      f"区间 {v['span']}  稳定性 {mark}")
            c, pv_, pr = wave["本波"], wave["前波"], wave["父浪"]
            print(f"\n  本波（日线 3%）：{c['from']} → {c['to']}　{c['bars']} 日　{c['chg']:+.2f}%")
            if pv_:
                print(f"  前波（同向）：　　{pv_['from']} → {pv_['to']}　{pv_['bars']} 日　{pv_['chg']:+.2f}%")
            if wave["倍数律"]:
                m = wave["倍数律"]
                up = (m["时间比"] or 0) > 2 and (m["空间比"] or 0) > 1
                print(f"  倍数律：时间 {m['时间比']:.2f}×　空间 {m['空间比']:.2f}×　"
                      f"→ {'构成升级' if up else '不构成升级'}（需时间 >2× 且 空间 >1×）")
            if pr:
                print(f"  父浪（日线 8%）：{pr['from']} → {pr['to']}　{pr['bars']} 日　{pr['chg']:+.2f}%")
            wk = wave.get("周线父浪")
            if wave.get("周线对齐") and wk:
                print(f"  周线父浪：{wk['from']} → {wk['to']}　{wk['bars']} 周 ≈ {wk['bars']*5} 交易日")
            else:
                print("  周线父浪：**不存在包含本波的周线段** → 级别未对齐（阈值代理与周线采样边界错位），不参与辈分校验")
            k = wave.get("最长子浪")
            if k:
                print(f"  最长子浪（60min）：{k['from']} → {k['to']}　{k['bars']} 根 ≈ {k['bars']*0.25:.1f} 交易日")
            print("  辈分校验：")
            for tag, dad, son, ok in wave["辈分"]:
                print(f"    {tag}：父 {dad} vs 子 {son} → {'✓ 正常' if ok else '✗ 颠倒（级别判定有误，须重划）'}")
            if wave["不稳定级别"]:
                print(f"  ⚠ 不稳定级别：{'、'.join(wave['不稳定级别'])}——该级别结论不得使用")
            print("  ⚠ 两项自检：①窗口敏感性（上表）②父浪包含性（父段必须包住本段）。任一不过，辈分校验作废。")
            print("  ⚠ 定性：本项为**阈值代理**（波哥原文的浪型级别靠人眼嵌套，无公开算法），B 级代理、C 级结论。")

    print("\n【声明】只产读数不产判断；阈值与判读见 熔炼系统 3.10 指数环境观测 ①/①b/③/⑤/⑨。")

    out_dir = os.path.join(ROOT, "cache", "level1")
    os.makedirs(out_dir, exist_ok=True)
    out = {"date": date, "rows": rows, "anchor": anchor_out, "breadth": bd, "wave": wave}
    path = os.path.join(out_dir, f"{date or 'latest'}.json")
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"已存 {os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
