#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
micro_sentiment.py — 微观情绪温度计 v1.0

定位：把「市场的微小变化与情绪状态」翻译成可核对读数。
边界：只产观测读数与分档，不产买卖/仓位判断。任何结论须由交易体系（熔炼系统）另行裁定。

用法：
  python micro_sentiment.py --date 20261016                 # 全市场情绪温度计
  python micro_sentiment.py --date 20261016 --prev 20261015 # 附昨日涨停今日溢价
  python micro_sentiment.py --codes 000573,002815 --micro   # 单票逐笔 + 分钟资金流

输出：
  cache/micro/<date>.json      结构化读数
  stdout                       人读版摘要
"""

import argparse
import collections
import datetime as dt
import json
import os
import statistics as st
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "scripts", "micro_config.json"), encoding="utf-8"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
EP = CFG["endpoints"]
TH = CFG["thresholds"]


def log(m):
    print(m, flush=True)


def getj(url, params, tries=3, timeout=15):
    for i in range(tries):
        try:
            return S.get(url, params=params, timeout=timeout).json()
        except Exception as e:
            if i == tries - 1:
                log(f"  ⚠ FAIL {url.split('/')[-1][:40]} :: {str(e)[:60]}")
                return None
            time.sleep(1 + i)


def fnum(x):
    try:
        v = float(x)
        return v
    except Exception:
        return None


# ═════════════ 1. 全A快照（广度层） ═════════════
HOSTS = ["https://push2.eastmoney.com", "https://82.push2.eastmoney.com"]


def snap_page(pn):
    """单页快照。主源失败自动换镜像源，仍失败则整页重试——绝不因单页失败截断样本。"""
    params = {"pn": pn, "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
              "fid": "f12", "fs": CFG["fs_all"], "fields": CFG["fields"]}
    for attempt in range(3):
        for host in HOSTS:
            try:
                d = S.get(host + "/api/qt/clist/get", params=params, timeout=15).json()
                if d and d.get("data") and d["data"].get("diff"):
                    return d["data"]
            except Exception as e:
                log(f"  ⚠ 快照 p{pn} {host.split('//')[1][:14]} :: {str(e)[:40]}")
        time.sleep(1.5)
    return None


def snapshot():
    rows, pn, total, lost = [], 1, None, []
    while True:
        data = snap_page(pn)
        if not data:
            lost.append(pn)
            if total and pn * 100 < total:
                pn += 1
                continue
            break
        total = data.get("total") or total
        diff = data["diff"]
        rows.extend(list(diff.values()) if isinstance(diff, dict) else diff)
        if total and pn * 100 >= total:
            break
        pn += 1
        time.sleep(0.12)
    if lost:
        log(f"  ⚠ 缺页 {lost} —— 样本不完整，广度读数请降级使用")
    out = []
    for r in rows:
        if not r.get("f12"):
            continue
        out.append({
            "code": r.get("f12"), "name": r.get("f14"),
            "pct": fnum(r.get("f3")), "amount": fnum(r.get("f6")),
            "turnover": fnum(r.get("f8")), "liangbi": fnum(r.get("f10")),
            "fmcap": fnum(r.get("f21")), "speed": fnum(r.get("f22")),
            "pct60": fnum(r.get("f24")), "industry": r.get("f100") or "",
        })
    return out


def limit_pct(code):
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("8", "4", "920")):
        return 30.0
    return 10.0


def breadth(snap):
    n = len(snap)
    up = sum(1 for r in snap if (r["pct"] or 0) > 0)
    dn = sum(1 for r in snap if (r["pct"] or 0) < 0)
    flat = n - up - dn
    wide_up = sum(1 for r in snap if (r["pct"] or 0) >= TH["wide_rise_pct"])
    wide_dn = sum(1 for r in snap if (r["pct"] or 0) <= TH["wide_fall_pct"])
    hot = sum(1 for r in snap if (r["liangbi"] or 0) >= TH["liangbi_hot"])
    amt = sum((r["amount"] or 0) for r in snap) / 1e8
    med_pct = st.median([r["pct"] for r in snap if r["pct"] is not None]) if n else None
    return {
        "样本数": n, "上涨": up, "下跌": dn, "平盘": flat,
        "涨跌家数比": round(up / dn, 2) if dn else None,
        "涨幅≥5%": wide_up, "跌幅≤-5%": wide_dn,
        "量比≥1.5家数": hot, "量比≥1.5占比%": round(hot / n * 100, 1) if n else None,
        "两市成交额(亿)": round(amt, 1),
        "涨跌幅中位数%": round(med_pct, 2) if med_pct is not None else None,
    }


# ═════════════ 2. 涨停/炸板/跌停池（结构层） ═════════════
def pool(kind, date8, pagesize=300):
    url = {"zt": EP["zt_pool"], "zb": EP["zb_pool"], "dt": EP["dt_pool"]}[kind]
    sort = "fbt:asc" if kind in ("zt", "zb") else "fund:asc"
    d = getj(url, {"ut": CFG["ut"], "dpt": "wz.ztzt", "Pageindex": "0",
                   "pagesize": str(pagesize), "sort": sort, "date": date8})
    try:
        return d["data"]["pool"] or []
    except Exception:
        return []


def structure(zt, zb, dt):
    nz, nb, nd = len(zt), len(zb), len(dt)
    tot = nz + nb
    lb = collections.Counter()
    for r in zt:
        k = r.get("lbc") or 1
        lb[min(k, 6)] += 1
    # 首封时段
    def bucket(t):
        if not t:
            return "未知"
        hh, mm = t // 10000, (t // 100) % 100
        m = hh * 60 + mm
        if m <= 9 * 60 + 30:
            return "竞价/开盘(≤9:30)"
        if m <= 10 * 60:
            return "早盘(9:30-10:00)"
        if m <= 11 * 60 + 30:
            return "上午(10:00-11:30)"
        if m <= 14 * 60:
            return "午后(13:00-14:00)"
        return "尾盘(>14:00)"
    tb = collections.Counter(bucket(r.get("fbt")) for r in zt)
    funds = [r.get("fund") / 1e8 for r in zt if r.get("fund")]
    turns = [r.get("hs") for r in zt if r.get("hs") is not None]
    zbc = collections.Counter(min(r.get("zbc") or 0, 4) for r in zt)
    big_seal = sum(1 for f in funds if f >= TH["seal_amount_big_yi"])
    n20 = sum(1 for r in zt if limit_pct(r["c"]) >= 20)
    return {
        "涨停家数": nz, "炸板家数": nb, "跌停家数": nd,
        "封板率%": round(nz / tot * 100, 1) if tot else None,
        "炸板率%": round(nb / tot * 100, 1) if tot else None,
        "最高连板": max([r.get("lbc") or 1 for r in zt], default=0),
        "连板梯队": {f"{k}板" if k < 6 else "6板+": v for k, v in sorted(lb.items())},
        "二板及以上占比%": round(sum(v for k, v in lb.items() if k >= 2) / nz * 100, 1) if nz else None,
        "首封时段分布": dict(tb.most_common()),
        "封单额中位数(亿)": round(st.median(funds), 2) if funds else None,
        "封单额≥3亿家数": big_seal,
        "封单最大(亿)": round(max(funds), 2) if funds else None,
        "涨停换手中位数%": round(st.median(turns), 1) if turns else None,
        "一字未炸(0次炸板)家数": zbc.get(0, 0),
        "炸板1次家数": zbc.get(1, 0),
        "20cm涨停家数": n20,
    }


def premium(prev_date, snap_pct):
    """昨日涨停今日溢价：昨日封板股在「当日」的平均涨跌幅。"""
    prev = pool("zt", prev_date)
    if not prev:
        return {"status": "无数据", "prev_date": prev_date}
    vals, win = [], 0
    for r in prev:
        p = snap_pct.get(r["c"])
        if p is None:
            continue
        vals.append(p)
        if p > 0:
            win += 1
    if not vals:
        return {"status": "无匹配"}
    return {
        "prev_date": prev_date, "样本": len(vals),
        "平均溢价%": round(sum(vals) / len(vals), 2),
        "中位溢价%": round(st.median(vals), 2),
        "红盘率%": round(win / len(vals) * 100, 1),
        "最差%": round(min(vals), 2), "最好%": round(max(vals), 2),
    }


# ═════════════ 3. 逐笔 / 分钟资金流（微观层） ═════════════
def micro(code):
    secid = ("1." if code.startswith(("6", "5", "9")) else "0.") + code
    d = getj(EP["details"], {"secid": secid, "fields1": "f1,f2,f3,f4",
                            "fields2": "f51,f52,f53,f54,f55", "pos": "-5000"})
    det = ((d or {}).get("data") or {}).get("details") or []
    if not det:
        return {"code": code, "status": "无逐笔"}
    buy_h = sell_h = big_buy = big_sell = 0
    auction_h = tail_h = 0
    total_h = 0
    for line in det:
        p = line.split(",")
        if len(p) < 5:
            continue
        t, hand, side = p[0], fnum(p[2]) or 0, p[4]
        total_h += hand
        if int(fnum(t.replace(":", "")) or 0) <= 92500:
            auction_h += hand
        if t >= "14:57":
            tail_h += hand
        if side == "1":
            buy_h += hand
            if hand >= TH["big_order_hand"]:
                big_buy += hand
        elif side == "2":
            sell_h += hand
            if hand >= TH["big_order_hand"]:
                big_sell += hand
    fl = getj(EP["fflow_min"], {"secid": secid, "fields1": "f1,f2,f3,f7",
                               "fields2": "f51,f52,f53,f54,f55,f56,f57",
                               "klt": "1", "lmt": "0"})
    kl = ((fl or {}).get("data") or {}).get("klines") or []
    zl = []
    for k in kl:
        q = k.split(",")
        if len(q) > 1 and fnum(q[1]) is not None:
            zl.append((q[0], fnum(q[1])))
    main_net = round(zl[-1][1] / 1e8, 2) if zl else None
    peak = max([v for _, v in zl], default=0)
    worst = min([v for _, v in zl], default=0)
    final = zl[-1][1] if zl else 0
    first_half = [v for t, v in zl if t[11:16] <= "11:30"]
    last_hour = [v for t, v in zl if t[11:16] >= "14:00"]
    return {
        "code": code, "笔数": len(det),
        "主动买量占比%": round(buy_h / (buy_h + sell_h) * 100, 1) if (buy_h + sell_h) else None,
        "大单(≥%d手)买量占比%%" % TH["big_order_hand"]:
            round(big_buy / total_h * 100, 1) if total_h else None,
        "大单卖量占比%": round(big_sell / total_h * 100, 1) if total_h else None,
        "竞价量占比%": round(auction_h / total_h * 100, 1) if total_h else None,
        "尾盘量占比%": round(tail_h / total_h * 100, 1) if total_h else None,
        "主力净流入(亿)": main_net,
        "主力最高净流入(亿)": round(peak / 1e8, 2) if zl else None,
        "主力最差净流入(亿)": round(worst / 1e8, 2) if zl else None,
        "上午末净流入(亿)": round(first_half[-1] / 1e8, 2) if first_half else None,
        "尾盘净流入(亿)": round(last_hour[-1] / 1e8, 2) if last_hour else None,
        "净流入回吐(亿)": round((peak - final) / 1e8, 2) if zl else None,
    }


# ═════════════ 4. 主流程 ═════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="数据日期 YYYYMMDD（与快照同日时广度才有效）")
    ap.add_argument("--prev", help="用于计算昨日涨停今日溢价的上一交易日 YYYYMMDD")
    ap.add_argument("--codes", help="逗号分隔，做逐笔微观读数")
    ap.add_argument("--micro", action="store_true", help="启用逐笔模块")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过全A快照（省时）")
    ap.add_argument("--snapshot-file", help="复用已落盘快照（只读，不写回），接口被限流时的兜底")
    a = ap.parse_args()

    t0 = time.time()
    out = {"meta": {"tool": "micro_sentiment", "version": CFG["version"],
                    "data_date": a.date, "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}

    snap, pctmap = [], {}
    if a.snapshot_file:
        log(f"[1/4] 复用快照 {a.snapshot_file}")
        raw = json.load(open(a.snapshot_file, encoding="utf-8"))
        snap = [{"code": r.get("code"), "name": r.get("name"), "pct": r.get("pct"),
                 "amount": r.get("amount"), "turnover": r.get("turnover"),
                 "liangbi": r.get("liangbi"), "fmcap": r.get("fmcap"),
                 "speed": r.get("speed"), "pct60": r.get("pct60"),
                 "industry": r.get("industry") or ""} for r in raw if r.get("code")]
        pctmap = {r["code"]: r["pct"] for r in snap}
        out["广度"] = breadth(snap)
        out["meta"]["snapshot_source"] = os.path.basename(a.snapshot_file)
        log(f"      {len(snap)} 只  上涨 {out['广度']['上涨']} / 下跌 {out['广度']['下跌']}  "
            f"成交 {out['广度']['两市成交额(亿)']} 亿")
    elif not a.no_snapshot:
        log(f"[1/4] 拉全A快照 …")
        snap = snapshot()
        pctmap = {r["code"]: r["pct"] for r in snap}
        out["广度"] = breadth(snap)
        log(f"      {len(snap)} 只  上涨 {out['广度']['上涨']} / 下跌 {out['广度']['下跌']}  "
            f"成交 {out['广度']['两市成交额(亿)']} 亿")

    log(f"[2/4] 拉涨停/炸板/跌停池 {a.date} …")
    zt, zb, dtp = pool("zt", a.date), pool("zb", a.date), pool("dt", a.date)
    out["结构"] = structure(zt, zb, dtp)
    out["结构"]["__首封样例"] = [
        {k: r.get(k) for k in ("n", "c", "lbc", "fbt", "fund", "zbc", "hs")}
        for r in sorted(zt, key=lambda x: x.get("fbt") or 999999)[:5]]
    log(f"      涨停 {len(zt)} 炸板 {len(zb)} 跌停 {len(dtp)}  "
        f"封板率 {out['结构']['封板率%']}%  最高 {out['结构']['最高连板']} 板")

    if a.prev:
        log(f"[3/4] 昨日涨停今日溢价（{a.prev} 封板股） …")
        out["溢价"] = premium(a.prev, pctmap)
        log(f"      {out['溢价']}")

    if a.micro and a.codes:
        log(f"[4/4] 逐笔微观 …")
        out["微观"] = {}
        for c in [x.strip() for x in a.codes.split(",") if x.strip()]:
            r = micro(c)
            out["微观"][c] = r
            log(f"      {c}: 主动买 {r.get('主动买量占比%')}%  "
                f"主力净流入 {r.get('主力净流入(亿)')} 亿  回吐 {r.get('净流入回吐(亿)')} 亿")

    os.makedirs(os.path.join(ROOT, "cache", "micro"), exist_ok=True)
    p = os.path.join(ROOT, "cache", "micro", f"{a.date}.json")
    out["meta"]["elapsed_s"] = round(time.time() - t0, 1)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"\n落盘 {p}  用时 {out['meta']['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
