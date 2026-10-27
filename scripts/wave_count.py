#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""波浪候选生成器 —— 艾略特否证器（**不是波哥口径**）

## ⚠ 口径声明（2026-10-23 补，用户追问后）
本脚本用的是**通用艾略特**的三条硬规则，**与波哥体系 v3.0 的口径不同**：
  · 波哥 4.2：ZigZag 阈值应为 **ATR 的 0.5—1 倍**（上证约 0.76%—1.51%），本脚本用固定百分比；
  · 波哥 4.3：**放弃命名**，只输出「级别＋方向＋失效位」，本脚本却输出浪 1—5 的命名
    ——而"浪型命名反复"正是波哥自己列出的**失败模式第 4 条**。

**因此**：本脚本**只适合做否证**——当别人说"这是第 3 浪"时，用它检查是否违反 R1/R2/R3。
**日常判断请用波哥口径**（时间 2 倍律＋交替原则＋失效位），实现在 `level1_indices.py --wave`。

---
原始说明：波浪候选生成器（能做什么 / 不能做什么，写在最前面）

## 能做的
1. 用 ZigZag 的波段序列作为「1-2-3-4-5」的原料；
2. 枚举所有可能的推动浪起点，**用艾略特的三条硬规则筛**：
   - R1 浪2 不得跌破浪1 起点
   - R2 浪3 不得是 1/3/5 三浪中最短的
   - R3 浪4 不得进入浪1 的价格区域
3. 输出**所有通过硬规则的方案**，以及每个方案下的调整目标位。

## 做不到的（必须说清）
- **无法唯一确定"现在是第几浪"。** 同一段走势通常有 2—4 个方案同时通过硬规则；
- **无法处理延伸浪/失败浪/复合调整（WXY）**——这些需要人眼判断；
- **无法消除起点主观性**：起点选在哪，结论就变。本脚本的做法是"全部枚举，让你看见有几个"。

## 为什么仍值得做
- 把"数浪"从**拍直觉**变成**可复核的筛选**：哪几条通过了、哪几条被规则否掉，一目了然；
- 输出是**方案集合**而非单一答案，天然避免"我早就说过"式的后见之明。

用法
    .venv/bin/python scripts/wave_count.py                 # 上证，8% 波段
    .venv/bin/python scripts/wave_count.py --th 0.15 --since 2024-01-01
    .venv/bin/python scripts/wave_count.py --symbol sz399006
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

FIB = (0.236, 0.382, 0.5, 0.618)
TOL = 0.5   # R3 重叠容差（%），由 --tol 覆盖


def build_legs(piv):
    out = []
    for j in range(1, len(piv)):
        d1, v1, _, i1 = piv[j - 1]
        d2, v2, t2, i2 = piv[j]
        out.append({"from": d1, "to": d2, "v1": v1, "v2": v2,
                    "bars": i2 - i1, "chg": (v2 / v1 - 1) * 100, "type": t2})
    return out


def check_impulse(seg):
    """seg = [L,H,L,H,L,H] 六点五段。返回 (是否通过, 规则明细)。"""
    p0, p1, p2, p3, p4, p5 = [x["v"] for x in
                              [{"v": seg[0]["v1"]}] + [{"v": s["v2"]} for s in seg]]
    w1, w2, w3, w4, w5 = seg
    rules = []
    # R1：浪2 低点不得低于浪1 起点
    r1 = p2 > p0
    rules.append(("R1 浪2 不破浪1 起点", r1, f"浪2 低 {p2:.2f} vs 浪1 起点 {p0:.2f}"))
    # R2：浪3 不是 1/3/5 中最短
    m1, m3, m5 = abs(w1["chg"]), abs(w3["chg"]), abs(w5["chg"])
    r2 = m3 > min(m1, m5)
    rules.append(("R2 浪3 不是最短", r2,
                  f"浪1 {m1:.2f}% / 浪3 {m3:.2f}% / 浪5 {m5:.2f}%"))
    # R3：浪4 不进入浪1 区域（允许 tol% 的噪声重叠）
    over = (p1 - p4) / p1 * 100 if p4 <= p1 else 0.0
    r3 = over <= TOL
    note = "不重叠" if over == 0 else f"重叠 {over:.2f}%{'（噪声级）' if over <= TOL else '（实质违反）'}"
    rules.append(("R3 浪4 不进浪1 区域", r3, f"浪4 低 {p4:.2f} vs 浪1 高 {p1:.2f}　{note}"))
    return all(x[1] for x in rules), rules


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="sh000001")
    ap.add_argument("--th", type=float, default=0.08, help="波段阈值（默认 8%%）")
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--tol", type=float, default=0.5,
                    help="R3 重叠容差（%%，默认 0.5）：小于此值视为收盘噪声，允许通过")
    args = ap.parse_args()

    global TOL
    TOL = args.tol

    bars = L1.kline(args.symbol, 3000)
    P = [(b["day"][:10], float(b["close"])) for b in bars if b["day"][:10] >= args.since]
    if len(P) < 100:
        print("数据不足")
        return 2
    L = build_legs(zigzag(P, args.th))
    print("=" * 92)
    print(f"波浪候选生成　{args.symbol}　阈值 {args.th*100:.0f}%　起点后 {len(P)} 根　波段 {len(L)} 段")
    print("=" * 92)

    passed = []
    # 枚举所有"从 L 起、连续 5 段"的组合
    for i in range(len(L) - 4):
        seg = L[i:i + 5]
        if [s["type"] for s in seg] != ["H", "L", "H", "L", "H"]:
            continue
        ok, rules = check_impulse(seg)
        if ok:
            passed.append((i, seg, rules))

    if not passed:
        print("没有方案通过三条硬规则 —— 说明这段走势不能被解释为单一推动浪。")
        print("（这本身是有用的结论：可能处于复合调整或非推动结构中）")
        return 0

    print(f"通过硬规则的方案：{len(passed)} 个\n")
    for k, (i, seg, rules) in enumerate(passed, 1):
        p0 = seg[0]["v1"]; p5 = seg[-1]["v2"]
        print(f"【方案 {k}】起点 {seg[0]['from']}（{p0:.2f}）→ 五浪终点 {seg[-1]['to']}（{p5:.2f}）")
        print("   分段：" + " ｜ ".join(
            f"{s['from']}→{s['to']} {s['bars']}日 {s['chg']:+.2f}%" for s in seg))
        for name, ok, detail in rules:
            print(f"   {'✓' if ok else '✗'} {name}：{detail}")
        span = p5 - p0
        print(f"   若此后为 ABC 调整，常见目标（对整段 {span:.2f} 点的回撤）：")
        for r in FIB:
            print(f"      {r:.3f} 回撤 → {p5 - span*r:.2f}")
        last = P[-1][1]
        print(f"   当前 {last:.2f}，距五浪终点 {(last/p5-1)*100:+.2f}%")
        print()

    print("【必须说清的局限】")
    print("  ① 通过的方案不唯一 —— 以上每个方案在硬规则下都成立，**无法判定哪个对**；")
    print("  ② 未处理延伸浪 / 失败浪 / 复合调整（WXY）—— 这些需人眼判断，无法编码；")
    print("  ③ 起点选在哪，结论就变 —— 本脚本的做法是「全部枚举」，让你看见有几个可能；")
    print("  ④ 因此本工具的输出是**方案集合**，不是「现在是第几浪」的答案。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
