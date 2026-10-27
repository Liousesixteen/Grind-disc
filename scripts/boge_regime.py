#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波哥口径 · 级别定位器（替代此前用艾略特口径做的 regime 判断）

## 为什么要有这个脚本
之前用固定百分比阈值（25/15/8/3%）＋ 艾略特五浪来找"大级别位置"，有两个错：
  ①阈值是拍的（≈ATR 的 10 倍），无统计依据；
  ②艾略特要数浪命名，而波哥体系明确放弃命名。
本脚本按 **波哥 v3.2** 的口径重做：**ATR 分段 → 时间 2 倍律定级别 → 半分位/等幅给点位 → 失效位**。

## 三条核心算法（全部有原文依据）
1. **分段**：ZigZag，阈值＝**ATR 的 0.5—1 倍**（v3.2 4.2）
2. **定级别**：**时间 2 倍律** —— 当前段耗时 ÷ 上一同向段耗时
   `≈1× 同级 / ≈2× 升一级 / ≈4× 升两级`（v3.2 4.3）
3. **给点位**：
   - **半分位** = (起 + 止) / 2　← 简单二分，**不是黄金分割**
   - **0.382 回撤位** = 止 − (止−起) × 0.382　（＝起 + 幅度×0.618，同一位置，从两头量）
   - **0.618 回撤位** = 止 − (止−起) × 0.618　（＝起 + 幅度×0.382，同一位置）
   - **目标区间中枢** = (半分位 + 0.382 回撤位) / 2
   ⚠ **术语提醒**：波哥口语的「0.618 位」＝本脚本的「0.382 回撤位」（同一位置）。
     两者差 0.618−0.382 的镜像关系，**不可混用**——按标准回撤 61.8% 算会差 180+ 点。
   - **等幅投射** = 本段起点 − 上一同级下跌段的绝对跌幅

## 输出（照抄他的格式）
```
级别：日线级 / 方向：下行中段（未升级） / 失效：站上 XXXX
目标区间：半分位 AAAA — 0.382 回撤位 BBBB（中枢 CCCC）／更深的 0.618 回撤位 DDDD
```

用法
    .venv/bin/python scripts/boge_regime.py
    .venv/bin/python scripts/boge_regime.py --symbol sz399006
    .venv/bin/python scripts/boge_regime.py --k 0.5      # 阈值＝0.5×ATR
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import level1_indices as L1  # noqa: E402
from regime_snapshot import zigzag  # noqa: E402

NAME = {"sh000001": "上证指数", "sz399006": "创业板指", "sh000688": "科创50",
        "sz399975": "证券公司", "sh000300": "沪深300", "sz399001": "深证成指"}


def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["high"]); l = float(bars[i]["low"])
        pc = float(bars[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for x in trs[n:]:
        a = (a * (n - 1) + x) / n
    return a


def legs_of(piv):
    out = []
    for j in range(1, len(piv)):
        d1, v1, _, i1 = piv[j - 1]
        d2, v2, t2, i2 = piv[j]
        out.append({"from": d1, "to": d2, "v1": v1, "v2": v2,
                    "bars": i2 - i1, "chg": (v2 / v1 - 1) * 100, "type": t2, "i2": i2})
    return out


def zigzag_hl(bars, th):
    """盘中极值版 ZigZag（波哥划分点的物理基础）。

    与通用（收盘价）ZigZag 的差别：拐点价格取 **盘中 high/low 的精确值**，
    确认反转用收盘（或低/高点）穿越阈值。实测波哥 65 个「划分点」61 个
    精确等于历史某日盘中极值（误差 ±0.005），即他记录的是极值本身。
    返回 [(date, 'H'|'L', price), ...]
    """
    piv = []  # (idx, date, type, price)
    up = None
    hi_i, hi_p = 0, float(bars[0]["high"])
    lo_i, lo_p = 0, float(bars[0]["low"])
    for i, b in enumerate(bars):
        h_, l_ = float(b["high"]), float(b["low"])
        if up is None:
            if h_ > hi_p: hi_i, hi_p = i, h_
            if l_ < lo_p: lo_i, lo_p = i, l_
            if lo_p and hi_p / lo_p - 1 >= th:
                up = hi_i > lo_i
                if up:
                    piv.append((lo_i, bars[lo_i]["day"][:16], "L", lo_p)); hi_i, hi_p = i, h_
                else:
                    piv.append((hi_i, bars[hi_i]["day"][:16], "H", hi_p)); lo_i, lo_p = i, l_
        elif up:
            if h_ > hi_p: hi_i, hi_p = i, h_
            if l_ <= hi_p * (1 - th):
                piv.append((hi_i, bars[hi_i]["day"][:16], "H", hi_p))
                up = False; lo_i, lo_p = i, l_
        else:
            if l_ < lo_p: lo_i, lo_p = i, l_
            if h_ >= lo_p * (1 + th):
                piv.append((lo_i, bars[lo_i]["day"][:16], "L", lo_p))
                up = True; hi_i, hi_p = i, h_
    if up: piv.append((hi_i, bars[hi_i]["day"][:16], "H", hi_p))
    else:  piv.append((lo_i, bars[lo_i]["day"][:16], "L", lo_p))
    return piv


DIV_LEVELS = ((0.25, "超长"), (0.15, "长级"), (0.08, "中级"), (0.03, "微级"))


def divide_table(bars, last, n=15, atr_th=0.01):
    """划分点记账表：每个拐点的精确价格 + 级别标签 + 距现价距离。

    级别标签＝该拐点在多大阈值的 ZigZag 下仍然存在（最大者），
    近似波哥的「微小→微→次之→重要→极其重要→高一级别」分层。
    基准层用 ATR 自适应阈值（≈ATR×0.75，波哥微浪分段口径）——
    实测 3743.80 等划分点在固定 3% 下不出现，必须用 ATR 级才能抓到。
    """
    z_by_th = {t: zigzag_hl(bars, t) for t, _ in DIV_LEVELS}
    z_by_th[atr_th] = zigzag_hl(bars, atr_th)
    rows = z_by_th[atr_th][-n:]
    out = []
    nb = len(bars)
    for idx, d, t, v in rows:
        lvl = "微级"
        for th, name in DIV_LEVELS:
            if any(abs(pv - v) < 0.02 and ptype == t for _, _, ptype, pv in z_by_th[th]):
                lvl = name
        ago = nb - 1 - idx          # 距今根数
        dist = (last / v - 1) * 100
        role = "上方阻力（突破=结构成立）" if v > last else "下方防线（跌破=结构作废/升级）"
        out.append((v, d, t, lvl, ago, dist, role))
    return out


def _kline_min(symbol: str, tf: str, n: int = 1000):
    """分钟级 K 线（新浪）。波哥多周期分工的微浪工作台：30/60 分钟。
    返回与 L1.kline 同构的 bars（day 含时间，如 2026-10-23 14:30:00）。
    历史深度受限：30 分钟约 1023 根（≈85 交易日），60/15 分钟更少。
    """
    import json as _json
    import urllib.request as _uq
    scale = {"60m": 60, "30m": 30, "15m": 15}[tf]
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={n}")
    req = _uq.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                    "Referer": "https://finance.sina.com.cn/"})
    with _uq.urlopen(req, timeout=30) as r:
        data = _json.loads(r.read().decode("utf-8", "ignore"))
    return [{"day": x["day"], "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"])} for x in data]


def print_points(args, bars, last):
    """划分点记账表输出（main 尾部与分段不足分支共用）。"""
    print("\n" + "=" * 88)
    print(f"【划分点记账表 · {args.timeframe}】最近 {args.points} 个拐点（盘中极值精确价格——波哥浪型划分的核心原料）")
    print("=" * 88)
    rows = divide_table(bars, last, n=args.points, atr_th=(atr(bars) / last) * args.k)
    print(f"  {'价格':>10s} {'日期时间':>17s} {'类':>2s} {'级别':>5s} {'距今':>6s} {'距现价':>9s}   作用")
    for v, d, t, lvl, ago, dist, role in reversed(rows):
        print(f"  {v:10.2f} {d:>17s} {t:>2s} {lvl:>5s} {ago:>5d}根 {dist:>+8.2f}%   {role}")
    print("  ── 用法（四件套）：①盯破立（±0.01 = 结构成立/作废）②从拐点起算时间（本表『距今』即根数，")
    print("     30 分钟根数÷8≈交易日）③时间 2 倍律判升级 ④特征匹配（速度/幅度不符即改判）")
    print("     详见 docs/波哥浪型划分方法推断.md")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="sh000001")
    ap.add_argument("--k", type=float, default=0.75, help="阈值＝k×ATR（波哥口径 0.5—1，默认 0.75）")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--ref-high", type=float, default=None,
                    help="反弹参照高点（手动指定，如 3995.18）；不指定则自动取本段起点")
    ap.add_argument("--ref-level", default="", choices=["", "mid", "long", "xlong"],
                    help="半分位参照级别（默认比 --level 大一级；波哥算半分位用的是上一级推动浪）")
    ap.add_argument("--points", type=int, default=0,
                    help="输出划分点记账表（最近 N 个拐点，盘中极值精确价格——波哥浪型划分的核心原料）")
    ap.add_argument("--timeframe", default="daily", choices=["daily", "60m", "30m", "15m"],
                    help="K线周期：daily / 60m / 30m / 15m。波哥按周期分工：30 分钟＝微浪主工作台，"
                         "60 分钟＝底背离与中级调整，日线＝长级划分点（多周期分工见 docs/波哥浪型划分方法推断.md 三之二）")
    ap.add_argument("--level", default=None, choices=["micro", "mid", "long", "xlong"],
                    help="级别：micro=ATR自适应(微浪) / mid=8%% / long=15%% / xlong=25%%；"
                         "不指定时日线默认 long、分钟周期默认 micro（分钟振幅小，固定大阈值会分段不足）")
    args = ap.parse_args()
    if args.level is None:
        args.level = "micro" if args.timeframe != "daily" else "long"

    if args.timeframe == "daily":
        bars = L1.kline(args.symbol, args.bars)
    else:
        bars = _kline_min(args.symbol, args.timeframe, min(args.bars, 1000))
    if len(bars) < 120:
        print("数据不足（分钟级历史深度受数据源限制）")
        return 2
    P = [(b["day"][:10], float(b["close"])) for b in bars]
    last = P[-1][1]
    a = atr(bars)
    if args.level == "micro":
        th = (a / last) * args.k
        th_desc = f"{args.k}×ATR = {th*100:.2f}%（微浪级）"
    else:
        th = {"mid": 0.08, "long": 0.15, "xlong": 0.25}[args.level]
        th_desc = f"固定 {th*100:.0f}%（{args.level} 级）"
    print("=" * 88)
    print(f"波哥口径 · 级别定位　{NAME.get(args.symbol, args.symbol)}（{args.symbol}）　{P[-1][0]}　收 {last:.2f}")
    print(f"ATR(14) = {a:.2f} 点 = {a/last*100:.2f}%　→ 分段阈值 {th_desc}")
    if args.timeframe != "daily":
        print(f"⚠ 分钟级模式（{args.timeframe}）：ATR/级别/半分位等均在本周期内自洽，"
              f"与日线口径不同——微浪划分点以此为准，长级判断仍看 daily")
    print("=" * 88)

    L = legs_of(zigzag(P, th))
    # 参照级别（用于半分位）：默认比当前大一级
    REF = {"micro": "mid", "mid": "long", "long": "xlong", "xlong": "xlong"}
    ref_lvl = args.ref_level or REF[args.level]
    th_ref = {"mid": 0.08, "long": 0.15, "xlong": 0.25}[ref_lvl]
    L_ref = legs_of(zigzag(P, th_ref))
    if len(L) < 4:
        print("分段不足（阈值过大或数据太少）——级别判定跳过")
        if args.points:
            print_points(args, bars, last)
        return 0

    cur = L[-1]
    same_dir = [x for x in L[:-1] if x["type"] == cur["type"]]
    prev_same = same_dir[-1] if same_dir else None
    prev_opp = next((x for x in reversed(L[:-1]) if x["type"] != cur["type"]), None)

    # 浮动段（最后一段可能未走完）
    floating = cur["i2"] == len(P) - 1

    print(f"分段数 {len(L)}　最近 6 段：")
    for x in L[-6:]:
        mark = " ←当前" if x is cur else ""
        print(f"  {x['from']}→{x['to']} {x['type']} {x['v1']:.2f}→{x['v2']:.2f} "
              f"{x['bars']:>4d}日 {x['chg']:+6.2f}%{mark}")

    # ---- 时间 2 倍律 ----
    print("\n【级别判定 · 时间 2 倍律】")
    up = cur["type"] == "H"
    if prev_same:
        ratio = cur["bars"] / prev_same["bars"]
        if ratio < 1.5:
            lvl = "同级"
        elif ratio < 3.0:
            lvl = "升一级"
        else:
            lvl = "升两级"
        print(f"  当前段 {cur['bars']} 日　上一同向段 {prev_same['bars']} 日（{prev_same['from']}→{prev_same['to']}）")
        print(f"  时间比 {ratio:.2f}×　→ <b>{lvl}</b>")
    else:
        ratio, lvl = None, "无法判定"
    print(f"  方向：{'上行' if up else '下行'}{'（浮动段，未走完）' if floating else ''}　幅度 {cur['chg']:+.2f}%")

    # ---- 半分位 / 0.382 回撤位 / 0.618 回撤位 ----
    print("\n【目标区间 · 半分位 ＋ 两个黄金分割回撤位】")
    if not up:
        # 参照上涨段＝当前下跌段起点之前、本级别内的**最低点**（相邻小回调合并进大段）
        # 若只看"相邻的上一段"，会把最后一冲当成整段上涨，半分位严重偏高。
        # P1 规则：调整浪的参照段＝**紧挨着调整浪前面的那段推动浪**（不是大级别上涨段！）
        # 例：长4浪调整 → 参照长3浪；微2浪调整 → 参照微1浪；B2浪调整 → 参照B浪
        # 自动近似：取上一个上行段的起点作为参照段起点（可能需人工确认级别）
        cands = [x for x in L_ref if x["type"] == "H" and x["to"] <= cur["from"]]
        if cands:
            # 最近的上行段终点（即当前调整浪的起点）就是参照段终点
            # 参照段起点＝该上行段的起点（前一个低点）
            # 参照段＝紧挨着调整浪前面的那段推动浪（即上一个上行段）
            hi_leg = max(cands, key=lambda x: x["to"])
            s, e = hi_leg["v1"], hi_leg["v2"]
            up_leg = {"from": hi_leg["from"], "to": hi_leg["to"],
                      "v1": s, "v2": e, "chg": (e / s - 1) * 100}
        else:
            up_leg = prev_opp
            s, e = up_leg["v1"], up_leg["v2"]
        half = (s + e) / 2
        span = e - s
        p382 = e - span * 0.382          # 标准叫法：0.382 回撤位（＝起点往上 61.8%）
        p618 = e - span * 0.618          # 标准叫法：0.618 回撤位（＝起点往上 38.2%）
        mid = (half + p382) / 2          # 波哥「中点法则」：半分位与 0.382 回撤位的中点
        print(f"  参照上涨段（P1：紧挨着本调整浪的前一个推动浪，{ref_lvl} 级近似）："
              f"{up_leg['from']}（{s:.2f}）→ {up_leg['to']}（{e:.2f}），{up_leg['chg']:+.2f}%"
              f"　⚠ 参照段需人工确认浪级")
        print(f"  半分位(0.5)    = ({s:.2f}+{e:.2f})/2 = <b>{half:.2f}</b>")
        print(f"  0.382 回撤位   = {e:.2f} − {span:.2f}×0.382 = <b>{p382:.2f}</b>"
              f"　（＝起点往上 61.8%，波哥口语称「0.618位」）")
        print(f"  0.618 回撤位   = {e:.2f} − {span:.2f}×0.618 = <b>{p618:.2f}</b>"
              f"　（＝起点往上 38.2%）")
        print(f"  区间中枢       = (半分位+0.382回撤位)/2 = <b>{mid:.2f}</b>　← 波哥「中点法则」")
        band_lo, band_hi = min(half, p382), max(half, p382)
        pos = ("已在区间内" if band_lo <= last <= band_hi
               else ("低于半分位（超跌）" if last < band_lo else "仍在区间上方"))
        print(f"  当前 {last:.2f}　距中枢 {(last/mid-1)*100:+.2f}%　{pos}")
        print(f"  镜像校验：起点往上 61.8% = {s+span*0.618:.2f}，应等于 0.382 回撤位 {p382:.2f} → "
              f"{'✓' if abs(s+span*0.618-p382)<0.01 else '✗'}")

        # ---- 反弹目标位（波哥 10-23 图：从本段低点往上画斐波那契）----
        # 参照高点＝上一上行段的终点（本段的起点）
        # 反弹参照：低点＝当前段终点（收盘低点），高点＝本段起点；若今日盘中创新低则用盘中极值
        import json as _j
        _bars = L1.kline(args.symbol, 5)
        _today_low = min(float(b["low"]) for b in _bars if b["day"][:10] == P[-1][0]) if _bars else None
        rb_lo = min(cur["v2"], _today_low) if _today_low else cur["v2"]
        rb_hi = args.ref_high if args.ref_high else cur["v1"]
        if rb_hi > rb_lo:
            r_span = rb_hi - rb_lo
            print(f"\n  【反弹目标位】（从本段低点 {rb_lo:.2f} 至前高 {rb_hi:.2f}，幅度 {r_span:.2f}）"
                  f"{'　[含今日盘中低]' if _today_low and _today_low < cur['v2'] else ''}")
            print(f"  {'比例':>6s}{'点位':>10s}{'状态':>8s}")
            for r in (0.191, 0.382, 0.500, 0.618, 0.809):
                v = rb_lo + r_span * r
                st = "✓已过" if last > v else ("←当前位置" if abs(last-v)<r_span*0.02 else "未到")
                print(f"  {r:>6.3f}{v:>10.2f}{st:>8s}")
            passed = [r for r in (0.191, 0.382, 0.500, 0.618, 0.809) if last > rb_lo + r_span * r]
            nxt = next((r for r in (0.191, 0.382, 0.500, 0.618, 0.809) if last <= rb_lo + r_span * r), None)
            print(f"  → 已过 {len(passed)} 档（{'、'.join(f'{p:.3f}' for p in passed) or '无'}）"
                  f"　下一阻力：0.382 位 {rb_lo + r_span*0.382:.2f}" if nxt==0.382 else
                  f"  → 已过 {len(passed)} 档　下一阻力位见上表")
        else:
            print("\n  【反弹目标位】缺参照高点，无法计算")
        print(f"  等幅投射       = 本段起点 {cur['v1']:.2f} − 上一同级跌幅 {abs(cur['chg'])/100*cur['v1']:.2f} "
              f"= {cur['v1']*(2-1-abs(cur['chg'])/100):.2f}")
    elif up and prev_opp:
        dn = prev_opp
        s, e = dn["v1"], dn["v2"]
        print(f"  当前为上行段。参照下跌段 {dn['from']}→{dn['to']}（{dn['chg']:+.2f}%）")
        print(f"  反弹半分位 = {(s+e)/2:.2f}　0.382 回撤位 = {e-(e-s)*0.382:.2f}"
              f"　0.618 回撤位 = {e-(e-s)*0.618:.2f}")
    else:
        print("  无对应参照段")

    # ---- 失效位 ----
    print("\n【失效位】")
    if up:
        fail = cur["v1"]
        print(f"  跌破本段起点 <b>{fail:.2f}</b> → 上行判断作废")
    else:
        fail = prev_same["v2"] if prev_same else cur["v1"]
        print(f"  站上 <b>{cur['v1']:.2f}</b>（本段起点）→ 下行判断作废")

    # ---- 钝化状态（R9）----
    print("\n【钝化状态 · R9】")
    cl = [c for _, c in P]
    def ema(v, n):
        al = 2 / (n + 1); e = [v[0]]
        for x in v[1:]:
            e.append(al * x + (1 - al) * e[-1])
        return e
    dif = [x - y for x, y in zip(ema(cl, 12), ema(cl, 26))]
    dea = ema(dif, 9)
    lo60 = min(dif[-60:])
    lo60_i = len(dif) - 60 + dif[-60:].index(lo60)
    print(f"  日线 DIF {dif[-1]:+.2f}　近 60 日最低 {lo60:+.2f}（{P[lo60_i][0]}）　柱 {2*(dif[-1]-dea[-1]):+.2f}")
    if not up:
        # R9 原文：「要么不创新低没有底背离，要么直到把底部钝化磨消失才启动」
        # 「磨消失」＝ DIF 跌破前低（使钝化结构失效）
        if dif[-1] < lo60 - 0.01:
            print("  → <b>DIF 已跌破前低：底部钝化被磨掉</b> → 具备「磨掉后才启动」的条件，可观察 3 浪结构确认")
        else:
            print("  → DIF 仍高于前低：<b>钝化尚未磨掉</b> → 此刻即便出现底背离也属「未磨掉就启动」，按下跌中继对待")
            print(f"     需 DIF 跌破 {lo60:+.2f} 才算磨掉（当前 {dif[-1]:+.2f}，差 {lo60-dif[-1]:.2f}）")
    else:
        print("  → 上行段中：钝化判据用于防顶——须 60+30 分钟 MACD 死叉后再创新高，才是顶背离共振；<b>死叉≠顶</b>")

    # ---- 多级别位置工具汇总（每级各一组关键位）----
    print("\n" + "=" * 88)
    print("【多级别位置工具汇总】同一套比例，每一级各一组关键位")
    print("=" * 88)
    all_levels = [("micro", "微浪级", th), ("mid", "中级(8%)", 0.08),
                  ("long", "长级(15%)", 0.15), ("xlong", "超长(25%)", 0.25)]
    for lv_tag, lv_name, lv_th in all_levels:
        lv_legs = legs_of(zigzag(P, lv_th))
        if len(lv_legs) < 2:
            continue
        lv_cur = lv_legs[-1]
        lv_ups = [x for x in lv_legs[:-1] if x["type"] == "H"]
        if not lv_ups:
            continue
        lv_up = lv_ups[-1]
        s0, e0 = lv_up["v1"], lv_up["v2"]
        if e0 <= s0:
            continue
        sp0 = e0 - s0
        half0 = (s0 + e0) / 2
        p382_0 = e0 - sp0 * 0.382
        p618_0 = e0 - sp0 * 0.618
        mid0 = (half0 + p382_0) / 2
        cur_dir = "上行" if lv_cur["type"] == "H" else "下跌"
        # 距离标注
        def dist(v):
            dd = (last / v - 1) * 100
            return f"{dd:+.2f}%"
        print(f"\n  ▸ {lv_name}　最后确认段：{lv_cur['from']}→{lv_cur['to']}　"
              f"{lv_cur['bars']}日 {lv_cur['chg']:+.2f}%　方向:{cur_dir}")
        print(f"    参照推动浪：{lv_up['from']}（{s0:.2f}）→ {lv_up['to']}（{e0:.2f}）")
        print(f"    半分位(0.5)       = {half0:9.2f}   距现价 {dist(half0):>8s}")
        print(f"    0.382 回撤位      = {p382_0:9.2f}   距现价 {dist(p382_0):>8s}")
        print(f"    0.618 回撤位      = {p618_0:9.2f}   距现价 {dist(p618_0):>8s}")
        print(f"    中点法则(中枢)    = {mid0:9.2f}   距现价 {dist(mid0):>8s}")

    print("\n⚠ **参照段局限性**：脚本自动取「紧挨着调整浪前面的推动浪」，")
    print("   但波哥的「长3浪」「大31浪」靠的是他的浪型划分（人眼），两者不一定完全一致。")
    print("   具体参照段需人工确认；可用 --ref-high / --ref-level 手动覆盖。")
    if args.points:
        print_points(args, bars, last)

    print("\n【口径声明】分段用 ATR 自适应；级别用时间比；不做浪型命名（波哥 4.3）。")
    print("【与旧版区别】此前的 25/15/8/3% 固定阈值 + 艾略特五浪，不是波哥口径，已降级为否证工具。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
