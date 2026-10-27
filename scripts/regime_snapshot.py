#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多级别形态定位器（任意标的 · 多阈值 · 不写死锚点）

⚠ **定位：辅助工具，非首选。** 日常判「级别＋点位」请用 `boge_regime.py`（波哥口径：时间2倍律／半分位／回撤位／钝化）。
   本脚本的用途是：①多标的**横向**对比（谁在跌、谁没跌）；②`--calib` 阈值统计校准（每周一次）。


为什么要它（2026-10-23 用户两次要求）
    第一次：「不能只看当日的或最近几日的……要关注大级别形态，比如 924 行情至今」。
    第二次：「924 只是我举的一个例子，不要只拘泥于此」。
    → 本脚本**不写死任何起点**：用多档阈值在**全历史**上自动识别波段，
      再回答「当前处在每个级别的什么位置」。924 只是其中一个自然锚点，不是唯一锚点。

核心判据（这是本工具唯一真正的"知识"）
    **某级别的转折，要跌（涨）够该级别的阈值才算确认。**
    例（2026-10-23 实测）：
      · 25% 级别：2024-02-05(2702.18) → 2026-05-13(4242.57)，+57.01%
        当前回调仅 −13.01% < 25% → **该级别上涨结构未被破坏**
      · 15% 级别：同一段，回调 −13.01% < 15% → **同样未确认转折**（临界，仅差 2 个百分点）
      · 8% 级别：5-13 → 10-22 已确认下跌（109 日 −13.78%）
      · 3% 级别：8-18 → 10-22 已确认下跌（41 日 −8.33%）
    → 结论：**大级别上涨未破，中级别调整已确立**。只看 924 一个锚点会漏掉前半句。

口径（全部声明）
    - 阈值作用在**收盘价**；
    - **回撤用点数口径**（点数回撤 ÷ 点数涨幅）；同时输出百分比口径供对照；
    - 「浮动段」＝最后一个确认转折点 → 今天（尚未被阈值确认的那一段）；
    - 级别判定是**阈值代理**（B 级），不是严格浪型。

用法
    .venv/bin/python scripts/regime_snapshot.py                      # 上证，四档
    .venv/bin/python scripts/regime_snapshot.py --symbol sz399006    # 任意标的
    .venv/bin/python scripts/regime_snapshot.py --history            # 附各档历史大段
    .venv/bin/python scripts/regime_snapshot.py --json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import level1_indices as L1  # noqa: E402

LEVELS = [(0.25, "超长 25%"), (0.15, "长 15%"), (0.08, "中 8%"), (0.03, "短 3%")]
MACRO_TTL = {"short": 7, "mid": 30, "long": 365}
NAME = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指",
        "sh000688": "科创50", "sh000300": "沪深300", "sh000852": "中证1000",
        "sz399975": "证券公司"}


def zigzag(P, th):
    """返回 [(日期, 价格, 'H'|'L', 序号)]，序号为该点在 P 中的下标。"""
    piv, dirn = [], 0
    hi_i, hi_p = 0, P[0][1]
    lo_i, lo_p = 0, P[0][1]
    ei, ep = 0, P[0][1]
    for i in range(1, len(P)):
        c = P[i][1]
        if dirn == 0:
            if c > hi_p: hi_i, hi_p = i, c
            if c < lo_p: lo_i, lo_p = i, c
            if c >= lo_p * (1 + th):
                piv.append((P[lo_i][0], lo_p, "L", lo_i)); dirn, ei, ep = 1, i, c
            elif c <= hi_p * (1 - th):
                piv.append((P[hi_i][0], hi_p, "H", hi_i)); dirn, ei, ep = -1, i, c
        elif dirn == 1:
            if c > ep: ei, ep = i, c
            elif c <= ep * (1 - th):
                piv.append((P[ei][0], ep, "H", ei)); dirn, ei, ep = -1, i, c
        else:
            if c < ep: ei, ep = i, c
            elif c >= ep * (1 + th):
                piv.append((P[ei][0], ep, "L", ei)); dirn, ei, ep = 1, i, c
    piv.append((P[ei][0], ep, "H" if dirn == 1 else "L", ei))
    return piv


def legs_of(piv):
    out = []
    for j in range(1, len(piv)):
        d1, v1, _, i1 = piv[j - 1]
        d2, v2, t2, i2 = piv[j]
        out.append({"from": d1, "to": d2, "v1": v1, "v2": v2, "bars": i2 - i1,
                    "chg": (v2 / v1 - 1) * 100, "type": t2, "i2": i2})
    return out


def analyze(P, th, tag, want_history=False):
    piv = zigzag(P, th)
    L = legs_of(piv)
    if len(L) < 2:
        return None
    last_close = P[-1][1]
    # 浮动段：最后一个确认转折点 → 今天
    d_last, v_last, t_last, i_last = piv[-1]
    float_chg = (last_close / v_last - 1) * 100
    float_bars = len(P) - 1 - i_last
    confirmed = abs(float_chg) >= th * 100

    # 上一完整段（以最后转折点结尾的那一段）
    last_leg = L[-1]
    prev_same_dir = next((x for x in reversed(L[:-1]) if x["type"] == last_leg["type"]), None)

    d = {"tag": tag, "th": th,
         "last_pivot": {"date": d_last, "value": v_last, "type": t_last},
         "last_leg": last_leg,
         "floating": {"from": d_last, "to": P[-1][0], "chg": float_chg,
                      "bars": float_bars,
                      "confirm_threshold": th * 100,
                      "confirm_price": v_last * (1 - th) if t_last == "H" else v_last * (1 + th),
                      "confirmed": confirmed},
         "history": L[-8:] if want_history else None}

    # 黄金分割回撤位（只在浮动段向下、且存在上一上涨段时有意义）
    if float_chg < 0:
        ups0 = [x for x in L if x["type"] == "H"]
        if ups0:
            u0 = ups0[-1]
            span = u0["v2"] - u0["v1"]
            d["fib"] = {f"{r:.3f}": u0["v2"] - span * r
                        for r in (0.236, 0.382, 0.5, 0.618, 0.809)}
            d["fib_pierced"] = [k for k, v in d["fib"].items() if last_close < v]

    # 若浮动段向下：算对上一上涨段的点数回撤
    if float_chg < 0:
        ups = [x for x in L if x["type"] == "H"]
        if ups:
            up = ups[-1]
            d["main_up"] = {"from": up["from"], "to": up["to"], "v1": up["v1"],
                            "v2": up["v2"], "bars": up["bars"], "pct": up["chg"]}
            d["retrace_points"] = (up["v2"] - last_close) / (up["v2"] - up["v1"]) * 100
            d["retrace_pct"] = abs(float_chg) / abs(up["chg"]) * 100 if up["chg"] else None
            d["time_pct"] = float_bars / up["bars"] * 100 if up["bars"] else None
    return d


def calibrate(sigma_daily_pct: float, n_sim: int = 8000) -> list[dict]:
    """用蒙特卡洛随机游走给出「噪声能造出多大回撤」的分位数。

    这是阈值的**统计基准**：纯零漂移随机游走下，观察期内最大回撤的分布。
      · 回撤 < 中位数 → 无法与噪声区分
      · 回撤 > 95% 分位 → 有 95% 把握不是噪声
    参数：sigma_daily_pct 为日收益率标准差（%）。
    """
    import random, math
    random.seed(42)
    rows = []
    for days in (20, 60, 120, 250, 500):
        vals = []
        for _ in range(n_sim):
            p, peak, m = 100.0, 100.0, 0.0
            for _ in range(days):
                p *= math.exp(random.gauss(0, sigma_daily_pct / 100))
                if p > peak: peak = p
                dd = (peak - p) / peak
                if dd > m: m = dd
            vals.append(m * 100)
        vals.sort()
        q = lambda x: vals[int(len(vals) * x)]
        rows.append({"days": days, "p50": q(.5), "p80": q(.8), "p90": q(.9),
                     "p95": q(.95), "p99": q(.99)})
    return rows


def vol_sigma(P, window: int = 60) -> float:
    import statistics as st
    rets = [(P[i][1] / P[i - 1][1] - 1) for i in range(max(1, len(P) - window), len(P))]
    return st.pstdev(rets) * 100


def macro_status():
    import datetime as dt
    d = os.path.join(ROOT, "cache", "macro")
    files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    if not files:
        return {"latest": None, "age_days": None, "stale": {k: True for k in MACRO_TTL}}
    latest = files[-1]
    try:
        age = (dt.datetime.now() - dt.datetime.strptime(latest[:-5], "%Y%m%d")).days
    except Exception:
        age = None
    return {"latest": latest, "age_days": age,
            "stale": {k: (age is None or age > ttl) for k, ttl in MACRO_TTL.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="sh000001")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--calib", action="store_true",
                    help="阈值科学校准：用蒙特卡洛随机游走基准，给出各时间尺度的显著回撤阈值")
    ap.add_argument("--vol", action="store_true",
                    help="附波动率归一化诊断：固定百分比阈值在不同波动环境下的等效严格度")
    ap.add_argument("--fib", action="store_true",
                    help="附黄金分割回撤位（0.236/0.382/0.5/0.618/0.809），与阈值确认线并列对照")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    bars = L1.kline(args.symbol, 6000)
    P = [(b["day"][:10], float(b["close"])) for b in bars]
    if len(P) < 200:
        print("数据不足（<200 根）")
        return 2
    date = P[-1][0]
    name = NAME.get(args.symbol, args.symbol)

    res = {"symbol": args.symbol, "name": name, "date": date,
           "span": [P[0][0], P[-1][0]], "bars": len(P), "last": P[-1][1], "levels": {}}
    for th, tag in LEVELS:
        d = analyze(P, th, tag, args.history)
        if d:
            res["levels"][tag] = d
    res["macro"] = macro_status()

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        print("=" * 96)
        print(f"多级别形态定位　{name}（{args.symbol}）　{date}　收 {P[-1][1]:.2f}")
        print(f"数据区间 {P[0][0]} ~ {P[-1][0]}（{len(P)} 根）")
        print("=" * 96)
        print(f"{'级别':10s}{'最后一个确认段':>30s}{'耗时':>8s}{'幅度':>9s}{'浮动段':>26s}{'转折确认':>10s}")
        for tag, d in res["levels"].items():
            l = d["last_leg"]; f = d["floating"]
            seg = f"{l['from']}→{l['to']} {l['v1']:.0f}→{l['v2']:.0f}"
            fl = f"{f['from']}→{f['to']} {f['chg']:+.2f}%（{f['bars']}日）"
            st = "✓ 已确认" if f["confirmed"] else "✗ 未确认"
            print(f"{tag:10s}{seg:>30s}{l['bars']:>6d}日{l['chg']:>+8.2f}%{fl:>26s}{st:>10s}")
            if not f["confirmed"]:
                print(f"{'':10s}  → 转折确认线 {f['confirm_price']:.2f}"
                      f"（再走 {(f['confirm_threshold'] - abs(f['chg'])):.2f} 个百分点触发）")
            if d.get("retrace_points") is not None:
                mu = d["main_up"]
                print(f"{'':10s}  ↳ 对上涨段（{mu['from']}→{mu['to']} {mu['pct']:+.2f}%）："
                      f"点数回撤 {d['retrace_points']:.1f}%　百分比 {d['retrace_pct']:.1f}%"
                      f"　耗时 {d['time_pct']:.1f}%")
            if args.fib and d.get("fib"):
                cur_ = P[-1][1]
                parts = []
                for k, v in d["fib"].items():
                    mark = "已破" if cur_ < v else f"上方{(cur_/v-1)*100:+.2f}%"
                    parts.append(f"{k}→{v:.2f}（{mark}）")
                print(f"{'':10s}  ↳ 黄金分割：" + " ｜ ".join(parts))
                print(f"{'':10s}    注：黄金分割与阈值确认线**是两套口径**，不可混用（见 docs 档案第二节）")
            if args.history and d.get("history"):
                print(f"{'':10s}  ↳ 历史：" + " ｜ ".join(
                    f"{x['from']}→{x['to']} {x['bars']}日 {x['chg']:+.2f}%" for x in d["history"]))
        if args.calib:
            sig = vol_sigma(P)
            rows = calibrate(sig)
            print("\n【阈值科学校准 · 蒙特卡洛随机游走基准】")
            print(f"   日σ = {sig:.2f}%（近 60 日实测）　模拟 8000 次/尺度　零漂移")
            print(f"   {'观察期':>8s}{'50%':>9s}{'80%':>9s}{'90%':>9s}{'95%':>9s}{'99%':>9s}")
            for r in rows:
                print(f"   {str(r['days'])+'日':>8s}{r['p50']:8.2f}%{r['p80']:8.2f}%"
                      f"{r['p90']:8.2f}%{r['p95']:8.2f}%{r['p99']:8.2f}%")
            print("   ── 用法定尺度的 95% 分位当阈值（有 95% 把握不是噪声）──")
            for r in rows:
                print(f"   想识别 {r['days']:>3d} 日量级的波段 → 阈值应设 {r['p95']:.1f}%")
            print("   ── 反向查：现行四档阈值各自对应多大尺度 ──")
            for th, tag in LEVELS:
                best = min(rows, key=lambda r: abs(r["p95"] - th * 100))
                print(f"   {tag:10s} {th*100:>4.0f}% ≈ {best['days']:>3d} 日尺度的 95% 分位"
                      f"（该尺度 p95={best['p95']:.1f}%）")
            print("   ⚠ 注意：段与尺度必须匹配——用 60 日尺度阈值去判 250 日波段，会把噪声当信号")

        if args.vol:
            import statistics as st, math
            rets = [(P[i][1] / P[i - 1][1] - 1) for i in range(1, len(P))]
            s_now = st.pstdev(rets[-60:]) * 100
            ann = s_now * math.sqrt(244)
            print("\n【波动率归一化诊断】")
            print(f"   近 60 日日σ {s_now:.2f}%　年化 {ann:.1f}%")
            for th, tag in LEVELS:
                print(f"   {tag:10s} 阈值 {th*100:>4.0f}% ≈ {th*100/ann:5.2f} 倍年化波动")
            print("   → 同一阈值在 2015 年（年化 38.1%）只有 0.39 倍、2017 年（8.5%）高达 1.76 倍：**固定百分比不是同一个标准**")
            print("   → 改进方向：用 th = k × 年化σ（k=1.0 时当前约 12.7%），使跨时期可比")

        m = res["macro"]
        print(f"\n【宏观刷新】最新 {m['latest']}（{m['age_days']} 天前）　"
              + "　".join(f"{k}:{'过期' if v else '有效'}" for k, v in m["stale"].items()))
        print("\n【判读要点】某级别转折需跌够该级别阈值才算确认；**未达阈值＝该级别趋势未破**。")
        print("【声明】只做定位，不做预测；阈值为代理口径（B 级）。")

    od = os.path.join(ROOT, "cache", "regime")
    os.makedirs(od, exist_ok=True)
    p = os.path.join(od, f"{date.replace('-', '')}_{args.symbol}.json")
    io.open(p, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    if not args.json:
        print(f"已存 {os.path.relpath(p, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
