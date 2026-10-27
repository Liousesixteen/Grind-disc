# -*- coding: utf-8 -*-
"""233战法 全A盘前股池 数据采集脚本（数据源直连，遵循 DATA_SOURCE_POLICY.md）
产出: cache/pool_<date>/ 下的结构化 JSON + CSV，供工作簿刷新使用。
数据截止: 最近一个交易日收盘。
"""
import json, os, re, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
OUT = sys.argv[1] if len(sys.argv) > 1 else "/Users/ccdemac/Finance/AI顶级交易员/cache/pool_run"
os.makedirs(OUT, exist_ok=True)
LOG = []

def log(msg):
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)

def get_json(url, params=None, tries=3, timeout=15):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            return r.json()
        except Exception as e:
            if i == tries - 1:
                log(f"FAIL {url[:80]} {e}")
                return None
            time.sleep(1 + i)

# ---------- 1. 交易日历（上证指数日线） ----------
def fetch_trade_days(n=140):
    cached = f"{OUT}/trade_days.json"
    if os.path.exists(cached):
        try:
            d = json.load(open(cached))
            if len(d) > 100:
                log(f"交易日历复用缓存 {len(d)} 天: {d[0]} .. {d[-1]}")
                return d
        except Exception:
            pass
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    p = {"secid": "1.000001", "klt": "101", "fqt": "1", "lmt": str(n),
         "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
         "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    days = []
    for i in range(4):
        d = get_json(url, p)
        try:
            ks = d["data"]["klines"]
            days = [k.split(",")[0].replace("-", "") for k in ks]
            break
        except Exception:
            time.sleep(2 + 2 * i)
    if not days:  # 回退：腾讯指数日线
        try:
            r = S.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,140,qfq", timeout=15)
            node = r.json()["data"]["sh000001"]
            arr = node.get("qfqday") or node.get("day")
            days = [b[0].replace("-", "") for b in arr]
            log("交易日历：东财失败，回退腾讯指数日线")
        except Exception as e:
            raise SystemExit(f"交易日历获取失败: {e}")
    log(f"交易日历 {len(days)} 天: {days[0]} .. {days[-1]}")
    return days

# ---------- 2. 全A快照（东财 clist） ----------
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f12,f14,f2,f3,f5,f6,f8,f10,f20,f21,f22,f24,f25,f100,f9,f23"

def fetch_all_a():
    rows, pn = [], 1
    while True:
        p = {"pn": pn, "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
             "fid": "f12", "fs": FS_ALL, "fields": FIELDS}
        d = get_json("https://push2.eastmoney.com/api/qt/clist/get", p)
        if not d or not d.get("data") or not d["data"].get("diff"):
            break
        diff = d["data"]["diff"]
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows.extend(diff)
        total = d["data"]["total"]
        if pn * 100 >= total:
            break
        pn += 1
        time.sleep(0.12)
    log(f"全A快照 {len(rows)} 只（页面 {pn}）")
    return rows

def norm_snap(rows):
    out = []
    for r in rows:
        def num(x):
            try:
                v = float(x)
                return v
            except Exception:
                return None
        out.append({
            "code": r.get("f12"), "name": r.get("f14"),
            "price": num(r.get("f2")), "pct": num(r.get("f3")),
            "vol_hand": num(r.get("f5")), "amount": num(r.get("f6")),
            "turnover": num(r.get("f8")), "liangbi": num(r.get("f10")),
            "mcap": num(r.get("f20")), "fmcap": num(r.get("f21")),
            "speed": num(r.get("f22")), "pct60": num(r.get("f24")),
            "pct_ytd": num(r.get("f25")), "industry": r.get("f100") or "",
            "pe": num(r.get("f9")), "pb": num(r.get("f23")),
        })
    return [x for x in out if x["code"]]

# ---------- 3. 涨停池历史（push2ex） ----------
def fetch_zt_pool(date8):
    p = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
         "Pageindex": "0", "pagesize": "10000", "sort": "fbt:asc", "date": date8}
    d = get_json("https://push2ex.eastmoney.com/getTopicZTPool", p)
    if not d or not d.get("data") or not d["data"].get("pool"):
        return []
    return d["data"]["pool"]

def build_zt_history(days):
    hist_days = days[-121:]
    per_code = {}
    daily_cnt = []
    for d8 in hist_days:
        pool = fetch_zt_pool(d8)
        daily_cnt.append({"date": d8, "cnt": len(pool)})
        for it in pool:
            c = str(it.get("c"))
            rec = per_code.setdefault(c, {
                "code": c, "name": it.get("n", ""), "hybk": it.get("hybk", ""),
                "zt_days": [], "max_lb": 0, "last_lb": 0,
            })
            lb = int(it.get("lbc") or 1)
            rec["zt_days"].append(d8)
            rec["max_lb"] = max(rec["max_lb"], lb)
            if d8 >= rec.get("last_zt", ""):
                rec["last_zt"] = d8
                rec["last_lb"] = lb
        time.sleep(0.15)
    log(f"涨停历史聚合 {len(per_code)} 只（{hist_days[0]}..{hist_days[-1]}）")
    return per_code, daily_cnt

# ---------- 4. 龙虎榜历史（辨识度代理） ----------
def fetch_lhb_date(d8):
    p = {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
         "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION,BILLBOARD_NET_AMT,ACCUM_AMOUNT",
         "filter": f"(TRADE_DATE='{d8[:4]}-{d8[4:6]}-{d8[6:]}')",
         "pageNumber": "1", "pageSize": "500",
         "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1",
         "source": "WEB", "client": "WEB"}
    d = get_json("https://datacenter-web.eastmoney.com/api/data/v1/get", p)
    if not d or not d.get("result") or not d["result"].get("data"):
        return []
    return d["result"]["data"]

def build_lhb_history(days):
    hist_days = days[-61:-1]  # 龙虎榜 T+1 披露，去掉最新一天
    per_code = {}
    for d8 in hist_days:
        rows = fetch_lhb_date(d8)
        seen = set()
        for r in rows:
            c = str(r.get("SECURITY_CODE") or "")
            if not c or c in seen:
                continue
            seen.add(c)
            rec = per_code.setdefault(c, {"code": c, "name": r.get("SECURITY_NAME_ABBR", ""),
                                          "lhb_days": [], "reasons": set(), "net_amt_sum": 0.0})
            rec["lhb_days"].append(d8)
            rec["reasons"].add(r.get("EXPLANATION") or "")
            try:
                rec["net_amt_sum"] += float(r.get("BILLBOARD_NET_AMT") or 0)
            except Exception:
                pass
        time.sleep(0.15)
    for rec in per_code.values():
        rec["reasons"] = sorted(rec["reasons"])
    log(f"龙虎榜聚合 {len(per_code)} 只（{hist_days[0]}..{hist_days[-1]}）")
    return per_code

# ---------- 5. 红肥绿瘦（腾讯日线，仅子集） ----------
def prefix(code):
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code

def fetch_kline(code, n=140):
    """新浪日线（DATA_SOURCE_POLICY 允许回退源；不复权）。
    腾讯 fqkline 与东财 push2his 在本次运行时被 WAF/代理阻断，故走新浪。"""
    sym = prefix(code)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    try:
        r = S.get(url, timeout=15)
        arr = r.json()
        if not isinstance(arr, list) or len(arr) < 25:
            return code, []
        bars = [[b["day"], b["open"], b["close"], b["high"], b["low"], b["volume"]] for b in arr]
        return code, bars
    except Exception:
        return code, []

def limit_pct(code, name):
    """按板块/ST 判定涨停阈值（%）"""
    if name and ("ST" in name.upper() or "退" in name):
        return 4.8
    if code.startswith(("30", "68")):
        return 19.7
    if code.startswith(("4", "8", "92")):
        return 29.7
    return 9.7

def kline_metrics(code, bars, name=""):
    if len(bars) < 25:
        return None
    closes = [float(b[2]) for b in bars]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append((closes[i] / closes[i - 1] - 1) * 100)
    # 涨停统计（由日线直算，覆盖全 A；东财涨停池仅回溯约 20 日，不敷使用）
    lp = limit_pct(code, name)
    flags = [1 if x >= lp else 0 for x in rets]
    n = len(flags)
    zt20 = sum(flags[-20:]) if n >= 20 else sum(flags)
    zt60 = sum(flags[-60:]) if n >= 60 else sum(flags)
    zt120 = sum(flags[-120:]) if n >= 120 else sum(flags)
    run = best = 0
    for f in flags[-120:]:
        run = run + 1 if f else 0
        best = max(best, run)
    last_zt = ""
    for i in range(n - 1, -1, -1):
        if flags[i]:
            last_zt = bars[i + 1][0].replace("-", "")
            break
    rets60 = rets[-60:]
    ups = [x for x in rets60 if x > 0]
    downs = [x for x in rets60 if x < 0]
    avg_up = sum(ups) / len(ups) if ups else 0.0
    avg_dn = sum(downs) / len(downs) if downs else 0.0
    hf = (avg_up / abs(avg_dn)) if avg_dn else None
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    c = closes[-1]
    return {"code": code, "bars": len(bars), "last_date": bars[-1][0].replace("-", ""),
            "zt20": zt20, "zt60": zt60, "zt120": zt120, "max_lb": best, "last_zt": last_zt,
            "avg_up": round(avg_up, 2), "avg_dn": round(avg_dn, 2),
            "hongfei": round(hf, 2) if hf is not None else None,
            "up_ratio": round(len(ups) / max(len(rets60), 1), 3),
            "ret20": round((c / closes[-21] - 1) * 100, 2) if len(closes) > 21 else None,
            "ret60": round((c / closes[-61] - 1) * 100, 2) if len(closes) > 61 else None,
            "above_ma20": bool(c > ma20), "above_ma60": (bool(c > ma60) if ma60 else None)}

def build_kline_metrics(codes, names=None):
    res = {}
    done = 0
    names = names or {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_kline, c): c for c in codes}
        for f in as_completed(futs):
            code, bars = f.result()
            m = kline_metrics(code, bars, names.get(code, ""))
            if m:
                res[code] = m
            done += 1
            if done % 1000 == 0:
                log(f"K线进度 {done}/{len(codes)}")
    log(f"K线指标 {len(res)}/{len(codes)} 只")
    return res

# ---------- 6. 汇总成池 ----------
def assemble(snap, zt, lhb, km, days):
    last = days[-1]
    d20 = days[-21] if len(days) >= 21 else days[0]
    d60 = days[-61] if len(days) >= 61 else days[0]
    rows = []
    for s in snap:
        c = s["code"]
        z = zt.get(c, {})
        l = lhb.get(c, {})
        k = km.get(c, {})
        # 涨停/连板一律取日线直算值（全 A 覆盖），东财涨停池仅作板块归属与交叉校验
        zt120 = k.get("zt120", 0)
        zt60 = k.get("zt60", 0)
        zt20 = k.get("zt20", 0)
        last_zt = k.get("last_zt", "")
        since = (dt.datetime.strptime(last, "%Y%m%d") - dt.datetime.strptime(last_zt, "%Y%m%d")).days if last_zt else None
        lhb60 = len([x for x in l.get("lhb_days", []) if x >= d60])
        max_lb = k.get("max_lb", 0)
        hf = k.get("hongfei")
        # 三筛选判定（233战法口径，量化草案，B/C 级待回测）
        c1 = (zt60 >= 2) or (max_lb >= 3)                # 股性：涨停基因
        c2 = (lhb60 >= 2) or (max_lb >= 5) or (zt60 >= 4)  # 辨识度：市场共识
        c3 = bool(hf and hf >= 1.2) and bool((k.get("ret20") or 0) > -8)  # 量价：红肥绿瘦
        score = sum([c1, c2, c3])
        tier = "核心" if score == 3 else ("备选" if score == 2 else ("观察" if score == 1 else ""))
        if s["name"] and ("ST" in s["name"].upper() or "退" in s["name"]):
            tier = "剔除(ST/退)"
        rows.append({**s, "zt20": zt20, "zt60": zt60, "zt120": zt120,
                     "max_lb": max_lb, "last_lb": z.get("last_lb", 0),
                     "last_zt": last_zt, "days_since_zt": since,
                     "hybk": z.get("hybk", ""), "lhb60": lhb60,
                     "lhb_days": len(l.get("lhb_days", [])),
                     "hongfei": hf, "avg_up": k.get("avg_up"), "avg_dn": k.get("avg_dn"),
                     "up_ratio": k.get("up_ratio"), "ret20": k.get("ret20"),
                     "ret60": k.get("ret60"), "above_ma20": k.get("above_ma20"),
                     "above_ma60": k.get("above_ma60"),
                     "筛1_股性": c1, "筛2_辨识度": c2, "筛3_量价": c3,
                     "达标数": score, "池层": tier})
    # 行业聚合
    ind = {}
    for r in rows:
        key = r["hybk"] or r["industry"] or "其他"
        g = ind.setdefault(key, {"板块": key, "池内个股数": 0, "核心": 0, "备选": 0, "观察": 0,
                                 "当日涨停家数": 0, "近20日涨停家数": 0, "当日平均涨跌幅": [], "当日成交额": 0.0})
        if r["池层"] in ("核心", "备选", "观察"):
            g["池内个股数"] += 1
            g[r["池层"]] += 1
        if r["pct"] is not None and r["pct"] >= 9.7:
            g["当日涨停家数"] += 1
        g["近20日涨停家数"] += r["zt20"]
        if r["pct"] is not None:
            g["当日平均涨跌幅"].append(r["pct"])
        g["当日成交额"] += r["amount"] or 0
    for g in ind.values():
        arr = [x for x in g["当日平均涨跌幅"] if x is not None]
        g["当日平均涨跌幅"] = round(sum(arr) / len(arr), 2) if arr else None
        g["当日成交额亿"] = round(g.pop("当日成交额") / 1e8, 1)
    log(f"行业聚合 {len(ind)} 个板块")
    return rows, ind

def main():
    t0 = time.time()
    days = fetch_trade_days(140)
    json.dump(days, open(f"{OUT}/trade_days.json", "w"))
    reuse = os.path.exists(f"{OUT}/all_a_snapshot.json")
    if reuse:
        log("检测到缓存，复用已生成的快照/涨停/龙虎榜数据")
        snap = json.load(open(f"{OUT}/all_a_snapshot.json"))
        ztj = json.load(open(f"{OUT}/zt_history.json"))
        zt, daily = ztj["per_code"], ztj["daily"]
        lhb = json.load(open(f"{OUT}/lhb_history.json"))
    else:
        snap = norm_snap(fetch_all_a())
        json.dump(snap, open(f"{OUT}/all_a_snapshot.json", "w"), ensure_ascii=False)
        zt, daily = build_zt_history(days)
        json.dump({"per_code": zt, "daily": daily}, open(f"{OUT}/zt_history.json", "w"), ensure_ascii=False)
        lhb = build_lhb_history(days)
        json.dump(lhb, open(f"{OUT}/lhb_history.json", "w"), ensure_ascii=False)
    cand = [s["code"] for s in snap]
    log(f"K线全量 {len(cand)} 只（涨停/红肥绿瘦均由日线直算，覆盖全 A）")
    km = build_kline_metrics(cand, {s["code"]: s["name"] for s in snap})
    json.dump(km, open(f"{OUT}/kline_metrics.json", "w"), ensure_ascii=False)
    rows, ind = assemble(snap, zt, lhb, km, days)
    json.dump(rows, open(f"{OUT}/pool_final.json", "w"), ensure_ascii=False)
    json.dump(ind, open(f"{OUT}/sector_agg.json", "w"), ensure_ascii=False)
    meta = {"run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": days[-1], "trade_days": len(days),
            "snapshot_n": len(snap), "zt_codes": len(zt), "lhb_codes": len(lhb),
            "kline_n": len(km), "tier_core": sum(1 for r in rows if r["池层"] == "核心"),
            "tier_backup": sum(1 for r in rows if r["池层"] == "备选"),
            "tier_watch": sum(1 for r in rows if r["池层"] == "观察"),
            "elapsed_s": round(time.time() - t0, 1)}
    json.dump(meta, open(f"{OUT}/summary.json", "w"), ensure_ascii=False)
    open(f"{OUT}/run.log", "w").write("\n".join(LOG))
    log(f"完成 {meta}")

if __name__ == "__main__":
    main()
