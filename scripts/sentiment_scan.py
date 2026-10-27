#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sentiment_scan.py — 全市场题材情绪扫描 v1.0

与 sentiment_model.py（单目标）互补：本脚本一次扫描**全市场多个题材/板块/赛道**，
输出「题材情绪榜」。核心设计是**一次抓取、多题材复用**：

- 题材池与热度：读 watchlists/233战法盘前股池_<date>.xlsx 窗口A（题材强度榜，含主力净流入/成分股数/涨幅）
- 成分股：本地 cache/pool_*/themes.json 反查（题材名 → 股票代码）
- 股吧：按股票读缓存 cache/guba/<code>_<date>.json；缺的核心成分股在预算内补抓（1.2s 间隔、风控即停）
  一只票的帖可同时计入多个题材 → 抓一次服务多题材
- 研报：一次扫全市场个股研报（标题命中题材关键词即归属该题材）
- 资讯：对前 N 个热门题材各搜 1 页（可 --no-news 关闭）

用法：
  .venv/bin/python scripts/sentiment_scan.py --top 30 --budget 80
  .venv/bin/python scripts/sentiment_scan.py --top 20 --budget 0 --no-news   # 只用已有缓存，0 请求
产物：
  cache/sentiment/scan/<date>.json
  reports/<date>/sentiment_scan_<date>.html
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys
import time

import openpyxl
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sentiment_model as SM  # 复用分类器/抓取/风控状态

CFG = SM.CFG
CATS = SM.CATS


import llm_label as LLM
_llm_hit = {"n": 0}


def classify_cached(text):
    """优先用 LLM 标注缓存（cache/llm_labels），没有才退回词表。"""
    t = (text or "").strip()
    if t:
        c = LLM.load_cached(t)
        if c:
            _llm_hit["n"] += 1
            return c, ["llm"]
    return SM.classify(text)



def log(m):
    print(m, flush=True)


# ═════════ 1. 题材池（来自盘前股池 xlsx） ═════════
def load_theme_board(top_n):
    files = sorted(glob.glob(os.path.join(ROOT, "watchlists", "233战法盘前股池_*.xlsx")))
    if not files:
        raise SystemExit("未找到 watchlists/233战法盘前股池_*.xlsx，请先跑 pool_pipeline")
    path = files[-1]
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = None
    for sn in wb.sheetnames:
        if "题材强度榜" in sn:
            ws = wb[sn]
            break
    if ws is None:
        raise SystemExit(f"{path} 未找到「题材强度榜」工作表")
    rows = list(ws.iter_rows(values_only=True))
    head = [str(x or "") for x in rows[0]]
    idx = {h: i for i, h in enumerate(head)}

    def val(r, key, default=None):
        i = idx.get(key)
        return r[i] if i is not None and i < len(r) else default

    out = []
    for r in rows[1:]:
        name = val(r, "题材")
        if not name:
            continue
        try:
            flow = float(val(r, "主力净流入(亿)") or 0)
        except Exception:
            flow = 0.0
        try:
            n_mem = int(val(r, "成分股数") or 0)
        except Exception:
            n_mem = 0
        out.append({"题材": str(name).strip(), "主力净流入(亿)": flow,
                    "成分股数": n_mem,
                    "当日涨幅%": val(r, "当日涨幅%"),
                    "5日涨幅%": val(r, "5日涨幅%"),
                    "涨股/总数": val(r, "涨股/总数"),
                    "主线簇": val(r, "主线簇"),
                    "连续性": val(r, "连续性")})
    # 剔除明显非赛道标签
    DROP = ("融资融券", "沪股通", "深股通", "MSCI", "HS300", "中证500", "上证380",
            "标普", "转债标的", "次新股", "百元股", "破净股", "微盘股", "小盘股",
            "大盘股", "中盘股", "预增", "预减", "扭亏", "高送转", "股权转让",
            "机构重仓", "基金重仓", "QFII", "社保", "私募", "昨日", "股权激励",
            "参股", "举牌", "并购", "重组", "摘帽", "ST", "同花顺")
    keep = [t for t in out if not any(d in t["题材"] for d in DROP)]
    keep.sort(key=lambda x: -x["主力净流入(亿)"])
    log(f"题材池：{path.split('/')[-1]} 共 {len(out)} 个 → 过滤属性类标签后 {len(keep)} 个，取前 {top_n}")
    return path, keep[:top_n]


# ═════════ 2. 成分股（本地题材库反查） ═════════
def members_of(name, limit=80):
    codes = SM.theme_members(name) if hasattr(SM, "theme_members") else []
    if not codes:
        # 退化为：从全A快照的题材标签里模糊匹配（themes.json 反查已在 SM 内）
        codes = []
    if not codes:
        return []
    amt = {r["code"]: (r.get("amount") or 0) for r in SM._snap_rows()}
    return sorted(codes, key=lambda c: -(amt.get(c) or 0))[:limit]


# ═════════ 3. 股吧（缓存优先 + 预算内补抓） ═════════
def guba_pool(codes, budget, a_sapi=0):
    """股吧数据池：①缓存 → ②直连补抓（免费，受日配额） → ③Search API 补缺（付费）。
    返回 code → [posts]。"""
    pool, fetched, miss, cached = {}, 0, [], 0
    for c in codes:
        cp = SM._guba_cache_path(c)
        if os.path.isfile(cp):
            try:
                pool[c] = json.load(open(cp, encoding="utf-8")).get("posts", [])
                cached += 1
                continue
            except Exception:
                pass
        miss.append(c)

    # ② 直连补抓（免费优先；风控则跳过）
    guba_daily = 160
    quota = max(0, guba_daily - rate_used("guba"))
    if SM.GUBA_STATE["blocked"]:
        log("  股吧直连已触发风控，跳直连")
    elif quota <= 0:
        log(f"  股吧直连今日配额已用尽（{guba_daily}），跳直连")
    elif budget > 0:
        for c in miss:
            if fetched >= budget or fetched >= quota or SM.GUBA_STATE["blocked"]:
                break
            posts, _ = SM.fetch_guba(c, 400, None, pages=1)
            fetched += 1
            rate_add("guba", 1)
            if posts:
                pool[c] = posts

    # ③ Search API 补缺（付费；仅在还有缺口且请求/风控时）
    need = [c for c in codes if c not in pool]
    if (a_sapi or SM.GUBA_STATE["blocked"]) and need:
        sapi_daily = 40
        used = rate_used("search_api")
        n = min(a_sapi or 6, 10, max(0, sapi_daily - used))
        if n <= 0:
            log(f"  Search API 今日配额已用尽（{used}/{sapi_daily}）")
        else:
            log(f"  Search API 补 {n} 只（今日已用 {used}/{sapi_daily}）")
            for c in need[:n]:
                recs = SM.guba_via_search_api(c, None, max_queries=3)
                rate_add("search_api", 1)
                if recs:
                    pool[c] = recs
    log(f"股吧：缓存 {cached} / 直连 {fetched} / 合计覆盖 {len(pool)} 只，"
        f"共 {sum(len(v) for v in pool.values())} 条"
        f"（今日 直连 {rate_used('guba')}/160｜SAPI {rate_used('search_api')}/40）")
    return pool


def report_index(theme_names, days=90, max_pages=30):
    """扫全市场个股研报，返回 list[(title, org, rating, date)]；归属在后面按关键词做。"""
    recs = []
    seen = set()
    for page in range(1, max_pages + 1):
        d = SM.getj(CFG["sources"]["report_list"], {
            "pageNo": page, "pageSize": "50", "code": "", "industryCode": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": (dt.date.today() - dt.timedelta(days=days)).isoformat(),
            "endTime": dt.date.today().isoformat(), "fields": "", "qType": "0"})
        rows = (d or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            t = str(r.get("title") or "")
            k = (t, r.get("orgSName"))
            if k in seen:
                continue
            seen.add(k)
            rating = str(r.get("sRatingName") or r.get("emRatingName") or "")
            cat, _ = SM.classify(rating + " " + str(r.get("sRatingChange") or ""))
            if rating:
                joined = rating + str(r.get("sRatingChange") or "")
                if any(x in joined for x in CFG["rating_map"]["负面"]):
                    cat = "负面"
                elif any(x in joined for x in CFG["rating_map"]["正面"]):
                    cat = "正面"
                elif any(x in joined for x in CFG["rating_map"]["中性"]):
                    cat = "中性"
            recs.append({"title": t, "org": r.get("orgSName"), "rating": rating,
                         "cat": cat, "date": (r.get("publishDate") or "")[:10]})
        if len(rows) < 50:
            break
        time.sleep(0.12)
    log(f"研报：全市场扫描 {len(recs)} 篇（近 {days} 天）")
    return recs


# ═════════ 5. 聚合 ═════════
def ratio(counter, n):
    if not n:
        return {c + "%": None for c in CATS} | {"样本": 0}
    return {c + "%": round(counter.get(c, 0) / n * 100, 1) for c in CATS} | {"样本": n}




def stock_news_batch(codes, sleep=0.4):
    """个股新闻（东财 stock_news_em，每只约 10 条含正文）。与股吧不同源、不占股吧额度。"""
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("TQDM_DISABLE", "1")
    import akshare as ak
    out, fail = {}, 0
    for c in codes:
        try:
            df = ak.stock_news_em(symbol=c)
            recs = []
            for _, r in df.iterrows():
                t = str(r.get("新闻标题") or "").strip()
                if not t:
                    continue
                cat, kw = classify_cached(t + " " + str(r.get("新闻内容") or "")[:60])
                recs.append({"title": t, "time": str(r.get("发布时间") or ""),
                             "cat": cat, "kw": kw, "src": c})
            out[c] = recs
        except Exception:
            fail += 1
        time.sleep(sleep)
    log(f"个股新闻：{len(out)} 只成功 / {fail} 只失败，累计 {sum(len(v) for v in out.values())} 条")
    return out


def global_flash():
    """全市场快讯（东财 200 条 + 财联社 20 条）。"""
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("TQDM_DISABLE", "1")
    import akshare as ak
    rows = []
    try:
        df = ak.stock_info_global_em()
        for _, r in df.iterrows():
            t = str(r.get("标题") or "").strip()
            if t:
                cat, kw = classify_cached(t)
                rows.append({"title": t, "time": str(r.get("发布时间") or ""), "cat": cat, "kw": kw})
    except Exception as e:
        log(f"  ⚠ 东财快讯失败 {str(e)[:40]}")
    try:
        df = ak.stock_info_global_cls()
        for _, r in df.iterrows():
            t = str(r.get("标题") or "").strip() or str(r.get("内容") or "").strip()[:40]
            if t:
                cat, kw = classify_cached(t)
                rows.append({"title": t, "time": str(r.get("发布时间") or ""), "cat": cat, "kw": kw})
    except Exception as e:
        log(f"  ⚠ 财联社快讯失败 {str(e)[:40]}")
    log(f"全市场快讯：{len(rows)} 条")
    return rows



def xueqiu_posts(code, count=30):
    """雪球个股讨论（免费，但需登录 cookie）。cookie 放 data/xq_cookie.txt（一行）。
    无文件或无 xq_a_token 时静默跳过。返回 [(title/text)]。"""
    cp = os.path.join(ROOT, "data", "xq_cookie.txt")
    if not os.path.isfile(cp):
        return []
    cookie = open(cp, encoding="utf-8").read().strip()
    if "xq_a_token" not in cookie:
        return []
    sym = ("SH" if code.startswith(("6", "5", "9")) else "SZ") + code
    try:
        r = requests.get("https://xueqiu.com/query/v1/symbol/search/status.json",
                         params={"symbol": sym, "count": count, "source": "all",
                                 "sort": "time", "page": 1},
                         headers={"User-Agent": "Mozilla/5.0 Chrome/124",
                                  "Referer": f"https://xueqiu.com/S/{sym}",
                                  "Cookie": cookie}, timeout=15)
        data = r.json()
        out = []
        for it in (data.get("list") or []):
            txt = (it.get("text") or it.get("title") or "").strip()
            txt = re.sub(r"<.*?>", "", txt)
            if txt:
                cat, kw = SM.classify(txt)
                out.append({"title": txt[:80], "cat": cat, "kw": kw, "src": code,
                            "time": it.get("created_at")})
        return out
    except Exception:
        return []



def irm_stream(pages=6, size=20):
    """互动易（深交所投资者关系平台）最新提问流。免费、无风控、JSON。
    接口不支持按股票过滤，只给全市场最新流水 → 作为散户提问文本源，按题材关键词归属。"""
    rows = []
    for pg in range(1, pages + 1):
        try:
            r = requests.post("https://irm.cninfo.com.cn/newircs/index/search",
                              json={"pageNo": pg, "pageSize": size},
                              headers={"User-Agent": "Mozilla/5.0 Chrome/124",
                                       "Referer": "https://irm.cninfo.com.cn/",
                                       "Content-Type": "application/json"}, timeout=15)
            d = r.json()
            for it in (d.get("results") or []):
                q = re.sub(r"<.*?>", "", str(it.get("mainContent") or it.get("attachedContent") or "")).strip()
                if not q:
                    continue
                cat, kw = classify_cached(q)
                rows.append({"title": q[:90], "cat": cat, "kw": kw,
                             "company": it.get("companyShortName"),
                             "time": it.get("updateDate")})
        except Exception:
            break
        time.sleep(0.2)
    log(f"互动易：{len(rows)} 条最新提问（全市场）")
    return rows



def auto_label(texts, limit=0, workers=6):
    """增量 LLM 标注：只标未缓存的文本，结果进 cache/llm_labels，下次扫描自动用上。"""
    import concurrent.futures as _cf
    todo = [t for t in dict.fromkeys(x.strip() for x in texts if x and x.strip())
            if not LLM.load_cached(t)]
    if limit:
        todo = todo[:limit]
    if not todo:
        log("标注：无新增文本需要标注")
        return 0
    key = LLM._read_key() if hasattr(LLM, "_read_key") else None
    if not key:
        log("标注：未找到 LLM key（data/llm_key.txt），跳过")
        return 0
    base, model = LLM._read_endpoint()
    ok = fail = 0

    def work(t):
        try:
            return t, LLM.call_api(t, key, base, model)
        except Exception:
            return t, None
    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for t, cat in ex.map(work, todo):
            if cat:
                LLM.save_label(t, cat, "api")
                ok += 1
            else:
                fail += 1
    log(f"标注：新增 {ok} 条（失败 {fail}），累计 "
        f"{len([f for f in os.listdir(LLM.LABEL_DIR)]) if os.path.isdir(LLM.LABEL_DIR) else 0} 条")
    return ok



def _rate_file():
    d = os.path.join(ROOT, "cache", "rate")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{dt.date.today().isoformat()}.json")


def rate_used(key):
    try:
        return json.load(open(_rate_file(), encoding="utf-8")).get(key, 0)
    except Exception:
        return 0


def rate_add(key, n=1):
    p = _rate_file()
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        d = {}
    d[key] = d.get(key, 0) + n
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)


def xq_browser_batch(codes, per=20, per_run_max=8, daily_max=40, delay=(4, 7)):
    """雪球讨论（Playwright 真实浏览器绕 WAF）。
    限流：单次最多 per_run_max 只、每日 daily_max 只、每只之间随机 delay 秒。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import xueqiu_browser as XB
    except Exception as e:
        log(f"  ⚠ xueqiu_browser 导入失败 {str(e)[:50]}")
        return {}
    if not XB.load_cookie():
        log("雪球：未配置 cookie，跳过")
        return {}
    import random
    used_today = rate_used("xueqiu")
    quota = max(0, daily_max - used_today)
    if quota <= 0:
        log(f"雪球：今日配额已用尽（{used_today}/{daily_max}），跳过")
        return {}
    codes = codes[:min(per_run_max, quota)]
    log(f"  雪球限流：本次 {len(codes)} 只（今日已用 {used_today}/{daily_max}），"
        f"间隔 {delay[0]}—{delay[1]}s")
    out = {}
    for i, c in enumerate(codes, 1):
        posts = XB.grab(c, per, headed=False)
        rate_add("xueqiu", 1)
        if posts:
            for x in posts:
                x["cat"], x["kw"] = classify_cached(x.get("title") or "")
                x["src"] = c
            out[c] = posts
        log(f"      雪球 {i}/{len(codes)}：{len(posts)} 条")
        if i < len(codes):
            time.sleep(random.uniform(*delay))
    return out



def _rate_file():
    d = os.path.join(ROOT, "cache", "rate")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{dt.date.today().isoformat()}.json")


def rate_used(key):
    try:
        return json.load(open(_rate_file(), encoding="utf-8")).get(key, 0)
    except Exception:
        return 0


def rate_add(key, n=1):
    p = _rate_file()
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        d = {}
    d[key] = d.get(key, 0) + n
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)


def xq_browser_batch(codes, per=20, per_run_max=8, daily_max=40, delay=(4, 7)):
    """雪球讨论（Playwright 真实浏览器绕 WAF）。
    限流：单次最多 per_run_max 只、每日 daily_max 只、每只之间随机 delay 秒。"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import xueqiu_browser as XB
    except Exception as e:
        log(f"  ⚠ xueqiu_browser 导入失败 {str(e)[:50]}")
        return {}
    if not XB.load_cookie():
        log("雪球：未配置 cookie，跳过")
        return {}
    import random
    used_today = rate_used("xueqiu")
    quota = max(0, daily_max - used_today)
    if quota <= 0:
        log(f"雪球：今日配额已用尽（{used_today}/{daily_max}），跳过")
        return {}
    codes = codes[:min(per_run_max, quota)]
    log(f"  雪球限流：本次 {len(codes)} 只（今日已用 {used_today}/{daily_max}），"
        f"间隔 {delay[0]}—{delay[1]}s")
    out = {}
    for i, c in enumerate(codes, 1):
        posts = XB.grab(c, per, headed=False)
        rate_add("xueqiu", 1)
        if posts:
            for x in posts:
                x["cat"], x["kw"] = classify_cached(x.get("title") or "")
                x["src"] = c
            out[c] = posts
        log(f"      雪球 {i}/{len(codes)}：{len(posts)} 条")
        if i < len(codes):
            time.sleep(random.uniform(*delay))
    return out


def make_scan_html(res, path):
    m, rows = res["meta"], res["rows"]
    def cell(v, suffix=""):
        return "—" if v is None else f"{v}{suffix}"
    trs = ""
    for i, r in enumerate(rows, 1):
        short = "样本不足" if (r.get("散户样本") or 0) < 30 else ""
        def num(v):
            try:
                return float(v)
            except Exception:
                return -999
        trs += (f'<tr class="{ "dim-row" if short else "" }"'
                f' data-idx="{i}" data-name="{r["题材"]}"'
                f' data-flow="{num(r["热度·主力净流入(亿)"])}"'
                f' data-chg="{num(r["当日涨幅%"])}" data-chg5="{num(r["5日涨幅%"])}"'
                f' data-members="{r["成分股数"]}" data-cover="{r["股吧覆盖股数"]}"'
                f' data-sample="{r["散户样本"]}" data-neg="{num(r["散户负面%"])}"'
                f' data-neu="{num(r["散户中性%"])}" data-pos="{num(r["散户正面%"])}"'
                f' data-inst="{r["机构样本"]}" data-instneg="{num(r["机构负面%"])}"'
                f' data-news="{r["自媒体样本"]}">'
                f'<td class="num">{i}</td><td class="name">{r["题材"]}</td>'
                f'<td class="num">{cell(r["热度·主力净流入(亿)"])}</td>'
                f'<td class="num">{cell(r["当日涨幅%"], "%")}</td>'
                f'<td class="num">{cell(r["5日涨幅%"], "%")}</td>'
                f'<td class="num">{r["成分股数"]}</td>'
                f'<td class="num">{r["股吧覆盖股数"]}/{r["取样股数"]}</td>'
                f'<td class="num">{r["散户样本"]}</td>'
                f'<td class="num neg">{cell(r["散户负面%"], "%")}</td>'
                f'<td class="num">{cell(r["散户中性%"], "%")}</td>'
                f'<td class="num pos">{cell(r["散户正面%"], "%")}</td>'
                f'<td class="num">{r["机构样本"]}</td>'
                f'<td class="num">{cell(r["机构负面%"], "%")}</td>'
                f'<td class="num">{r["自媒体样本"]}</td></tr>')
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>全市场题材情绪扫描 · {m["run_at"][:10]}</title>
<style>
:root{{--bg:#0a0d12;--panel:#12161d;--line:#232b36;--txt:#e8edf4;--dim:#8fa0b3;--dim2:#5b6779;
--pos:#f0524f;--neg:#34a853;--warn:#e2a336}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:-apple-system,"PingFang SC",system-ui,sans-serif;
padding:26px 18px 50px;font-variant-numeric:tabular-nums;line-height:1.55}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:21px;font-weight:600}}
.meta{{color:var(--dim);font-size:12.5px;margin:8px 0 16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}}
.c{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:11px 14px}}
.c .k{{font-size:11.5px;color:var(--dim)}} .c .v{{font-size:19px;font-weight:600;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--panel);
border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th,td{{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{background:#181e27;color:var(--dim);font-weight:400;font-size:11px;position:sticky;top:0}}
td.num{{text-align:right}} td.name{{font-weight:500}}
td.neg{{color:#5bd079}} td.pos{{color:#ff8a87}}
tr.dim-row td{{color:var(--dim2)}}
tr:last-child td{{border-bottom:none}}
.note{{margin-top:14px;font-size:11.5px;color:var(--dim2)}}
.sortbar{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 10px;font-size:12px;color:var(--dim)}}
.sbtn{{background:var(--panel);border:1px solid var(--line);color:var(--dim);border-radius:8px;
padding:4px 10px;cursor:pointer;font-size:11.5px}}
.sbtn.on{{color:var(--txt);border-color:#4a5568;background:#1d2531}}
.sorthint{{color:var(--dim2);font-size:11px;margin-left:6px}}
th{{cursor:pointer;user-select:none}} th:hover{{color:var(--txt)}}
</style></head><body><div class="wrap">
<h1>全市场题材情绪扫描</h1>
<div class="meta">数据 {m["run_at"].replace("T"," ")}　·　题材池 {m["theme_source"].split("/")[-1]}（{m["themes"]} 个题材）　·　
覆盖 {m["codes_covered"]}/{m["codes_union"]} 只成分股　·　股吧请求 {m["guba_budget_used"]} 次　·　用时 {m["elapsed_s"]}s</div>
<div class="cards">
<div class="c"><div class="k">扫描题材</div><div class="v">{m["themes"]}</div></div>
<div class="c"><div class="k">取样成分股</div><div class="v">{m["codes_union"]}</div></div>
<div class="c"><div class="k">股吧已覆盖</div><div class="v">{m["codes_covered"]}</div></div>
<div class="c"><div class="k">有样本题材</div><div class="v">{sum(1 for r in rows if (r.get("散户样本") or 0) >= 30)}</div></div>
</div>
<div class="sortbar">排序：
  <button class="sbtn on" data-k="neg" data-t="num">看空比例↓</button>
  <button class="sbtn" data-k="sample" data-t="num">散户样本↓</button>
  <button class="sbtn" data-k="flow" data-t="num">主力净流入↓</button>
  <button class="sbtn" data-k="chg" data-t="num">当日涨幅↓</button>
  <button class="sbtn" data-k="inst" data-t="num">机构篇数↓</button>
  <button class="sbtn" data-k="name" data-t="str">题材名 A→Z</button>
  <span class="sorthint">样本&lt;30 的行置灰（仅方向参考）。点击任意表头也可排序。</span>
</div>
<table id="tbl"><thead><tr><th data-k="idx" data-t="num">#</th><th data-k="name" data-t="str">题材</th>
<th data-k="flow" data-t="num">主力净流入(亿)</th><th data-k="chg" data-t="num">当日%</th><th data-k="chg5" data-t="num">5日%</th>
<th data-k="members" data-t="num">成分</th><th data-k="cover" data-t="num">覆盖/取样</th>
<th data-k="sample" data-t="num">散户样本</th><th data-k="neg" data-t="num">看空%</th><th data-k="neu" data-t="num">中性%</th>
<th data-k="pos" data-t="num">正面%</th><th data-k="inst" data-t="num">机构篇</th>
<th data-k="instneg" data-t="num">机构看空%</th><th data-k="news" data-t="num">资讯</th></tr></thead><tbody>{trs}</tbody></table>
<div class="note">排序：散户样本≥30 的在前，按看空比例降序。灰色行＝样本＜30（仅作方向参考，不作结论）。<br>
股吧为稀缺资源：白天建议 --budget 0（只用缓存）、盘后 ≥16:30 再补抓；触发风控时自动停止。<br>
本页只产读数，不产买卖判断。</div>
</div></body></html>"""
    SORT_JS = """
<script>
(function(){
  var tbl=document.getElementById('tbl'); if(!tbl) return;
  var tbody=tbl.tBodies[0];
  function sortBy(k,type,asc){
    var rows=Array.prototype.slice.call(tbody.rows);
    rows.sort(function(a,b){
      var x=a.dataset[k], y=b.dataset[k];
      if(type==='num'){ x=parseFloat(x); y=parseFloat(y);
        if(isNaN(x))x=-999; if(isNaN(y))y=-999; return asc? x-y : y-x; }
      return asc? String(x).localeCompare(String(y),'zh') : String(y).localeCompare(String(x),'zh');
    });
    rows.forEach(function(r,i){ tbody.appendChild(r); r.cells[0].textContent=i+1; });
    var btns=document.querySelectorAll('.sbtn');
    for(var i=0;i<btns.length;i++){ btns[i].className='sbtn'+(btns[i].dataset.k===k?' on':''); }
  }
  var btns=document.querySelectorAll('.sbtn');
  for(var i=0;i<btns.length;i++){
    (function(b){ b.onclick=function(){ sortBy(b.dataset.k, b.dataset.t, false); }; })(btns[i]);
  }
  var ths=tbl.querySelectorAll('th');
  for(var j=0;j<ths.length;j++){
    (function(th){ th.onclick=function(){ sortBy(th.dataset.k||'idx', th.dataset.t||'num', false); }; })(ths[j]);
  }
  sortBy('neg','num',false);
})();
</script>
"""
    html = html.replace("</body></html>", SORT_JS + "</body></html>")
    open(path, "w", encoding="utf-8").write(html)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50, help="题材数量（按主力净流入取前 N）")
    ap.add_argument("--budget", type=int, default=80, help="本次允许的新股吧请求数")
    ap.add_argument("--sapi", type=int, default=0, metavar="N", help="股吧风控时改用 Search API 补 N 只票（每票3次调用）")
    ap.add_argument("--no-news", action="store_true", help="不抓资讯")
    ap.add_argument("--news-batch", type=int, default=0, help="个股新闻批抓数量（免费增量，不占股吧额度）")
    ap.add_argument("--no-flash", action="store_true", help="不抓全市场快讯")
    ap.add_argument("--xueqiu", action="store_true", help="启用雪球讨论源（需 data/xq_cookie.txt）")
    ap.add_argument("--no-irm", action="store_true", help="不抓互动易提问流")
    ap.add_argument("--llm-label", type=int, default=0, metavar="N", help="跑完后增量标注本次新语料（0=不标，300=最多标300条）")
    ap.add_argument("--xq-browser", type=int, default=0, metavar="N", help="用浏览器抓 N 只票的雪球讨论（每只约14s）")
    ap.add_argument("--no-report", action="store_true", help="不扫研报")
    ap.add_argument("--report-days", type=int, default=180, help="研报回溯天数（默认180，越长样本越多但时效递减）")
    a = ap.parse_args()

    t0 = time.time()
    today = dt.date.today().isoformat()
    src, themes = load_theme_board(a.top)
    theme_names = [t["题材"] for t in themes]

    # 成分股并集（预算友好：每个题材取成交额前 25 只）
    t2codes = {}
    need = []
    for t in themes:
        cs = members_of(t["题材"], limit=25)
        t2codes[t["题材"]] = cs
        need += cs
    uniq = list(dict.fromkeys(need))
    log(f"成分股：{len(themes)} 个题材覆盖 {len(uniq)} 只（去重后）")

    # 贪心增益排序：优先抓「能同时补多个题材」的票（同一只票属多个题材）
    code2themes = collections.defaultdict(set)
    for tn, cs in t2codes.items():
        for c in cs:
            code2themes[c].add(tn)
    def _cached(c):
        return os.path.isfile(SM._guba_cache_path(c))
    pending = [c for c in uniq if not _cached(c)]
    pending.sort(key=lambda c: -len(code2themes[c]))
    ordered = [c for c in uniq if _cached(c)] + pending
    top_gain = [(c, len(code2themes[c])) for c in pending[:5]]
    if top_gain:
        log("  待抓票的题材覆盖增益前 5：" + "、".join(f"{c}({n}题材)" for c, n in top_gain))

    # 股吧（按增益排序，预算内优先抓覆盖最广的票）
    gp = guba_pool(ordered, a.budget, a.sapi)
    for c, posts in gp.items():
        for p in posts:
            p["cat"], p["kw"] = classify_cached(p.get("title") or "")

    # 研报
    reps = [] if a.no_report else report_index(theme_names, days=a.report_days,
                                               max_pages=max(30, a.report_days // 3))
    def _kws(t):
        base = SM.theme_keywords(t)
        out = list(base)
        # 中文复合题材名常以「前2字」出现在研报标题里（水利建设→水利、智慧城市→智慧）
        for k in base:
            if len(k) >= 4:
                out.append(k[:2])
            if len(k) >= 5:
                out.append(k[:3])
        return list(dict.fromkeys([x for x in out if len(x) >= 2]))
    t2kw = {t: _kws(t) for t in theme_names}
    t2rep = collections.defaultdict(list)
    for r in reps:
        for t, kws in t2kw.items():
            if any(k in r["title"] for k in kws):
                t2rep[t].append(r)

    # 资讯三层：题材关键词搜索 + 个股新闻批抓 + 全市场快讯
    t2news = collections.defaultdict(list)
    if not a.no_news:
        for t in theme_names[:10]:
            kws = t2kw[t]
            items = SM.fetch_news(kws[0], 60, 30, must_contain=None, pages=1)
            for it in items:
                it["cat"], it["kw"] = classify_cached(it["title"])
            t2news[t] = items
            time.sleep(0.25)
        log("资讯①：前 10 个题材各取 60 条（关键词搜索）")

    if a.news_batch > 0:
        pick = [c for c in ordered if not _cached(c)][:a.news_batch]
        if not pick:
            pick = ordered[:a.news_batch]
        sn = stock_news_batch(pick)
        for c, recs in sn.items():
            for t in code2themes.get(c, ()):
                t2news[t].extend(recs)
        log(f"资讯②：个股新闻归属 {len(pick)} 只 → 已并入对应题材")

    xq = collections.defaultdict(list)
    for c in (ordered[:60] if a.xueqiu else []):
        recs = xueqiu_posts(c, 30)
        if recs:
            xq[c] = recs
        time.sleep(0.35)
    if a.xueqiu:
        log(f"雪球(cookie)：{len(xq)} 只成功，累计 {sum(len(v) for v in xq.values())} 条")
    if a.xq_browser > 0:
        pick_xq = [c for c in ordered if c not in xq][:a.xq_browser]
        xqb = xq_browser_batch(pick_xq, per=20)
        for c, recs in xqb.items():
            xq[c] = recs
        log(f"雪球(浏览器)：{len(xqb)} 只成功，累计 {sum(len(v) for v in xqb.values())} 条")

    if not a.no_flash:
        fl = global_flash()
        hit = 0
        for r in fl:
            for t, kws in t2kw.items():
                if any(k in r["title"] for k in kws):
                    t2news[t].append(r)
                    hit += 1
        log(f"资讯③：全市场快讯 {len(fl)} 条 → 命中题材 {hit} 次")

    # 互动易提问流（散户直接表态，免费无风控）
    t2irm = collections.defaultdict(list)
    if not a.no_irm:
        irm = irm_stream()
        h2 = 0
        for r in irm:
            for t, kws in t2kw.items():
                if any(k in r["title"] for k in kws):
                    t2irm[t].append(r)
                    h2 += 1
        log(f"互动易：命中的题材归属 {h2} 次")

    if _llm_hit["n"]:
        log(f"分类器：{_llm_hit['n']} 条使用 LLM 标注缓存，其余走词表")
    # 聚合
    rows = []
    for t in themes:
        name = t["题材"]
        codes = t2codes[name]
        posts = [p for c in codes for p in gp.get(c, [])]
        posts += [p for c in codes for p in xq.get(c, [])]
        posts += t2irm.get(name, [])
        cc = collections.Counter(p["cat"] for p in posts)
        rp = t2rep.get(name, [])
        rc = collections.Counter(r["cat"] for r in rp)
        nw = t2news.get(name, [])
        nc = collections.Counter(x["cat"] for x in nw)
        rows.append({
            "题材": name,
            "热度·主力净流入(亿)": t["主力净流入(亿)"],
            "当日涨幅%": t["当日涨幅%"], "5日涨幅%": t["5日涨幅%"],
            "成分股数": t["成分股数"], "取样股数": len(codes),
            "股吧覆盖股数": sum(1 for c in codes if c in gp),
            **{f"散户{k}": v for k, v in ratio(cc, len(posts)).items()},
            **{f"机构{k}": v for k, v in ratio(rc, len(rp)).items()},
            **{f"自媒体{k}": v for k, v in ratio(nc, len(nw)).items()},
        })
    # 排序：有足够样本的按看空比例降序（情绪最悲观在前），样本不足的置后
    rows.sort(key=lambda r: (-(r["散户样本"] >= 30), -(r["散户负面%"] or 0)))

    if a.llm_label:
        all_texts = []
        for r in rows:
            pass
        for c, posts in gp.items():
            all_texts += [p.get("title") or "" for p in posts]
        for recs in t2news.values():
            all_texts += [x.get("title") or "" for x in recs]
        for recs in t2irm.values():
            all_texts += [x.get("title") or "" for x in recs]
        for r in reps:
            all_texts.append(r.get("title") or "")
        auto_label(all_texts, limit=a.llm_label)

    out_d = os.path.join(ROOT, "cache", "sentiment", "scan")
    os.makedirs(out_d, exist_ok=True)
    p = os.path.join(out_d, f"{today}_{dt.datetime.now().strftime('%H%M')}.json")
    res = {"meta": {"tool": "sentiment_scan", "version": "1.0", "run_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "theme_source": os.path.relpath(src, ROOT), "themes": len(themes),
                    "codes_union": len(uniq), "codes_covered": len(gp),
                    "guba_budget_used": SM.GUBA_STATE["requests"], "elapsed_s": round(time.time() - t0, 1)},
           "rows": rows}
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"\n落盘 {p}  用时 {round(time.time()-t0,1)}s")
    print("SCAN_JSON=" + p)
    rep_d = os.path.join(ROOT, "reports", today)
    os.makedirs(rep_d, exist_ok=True)
    hp = os.path.join(rep_d, f"sentiment_scan_{today}.html")
    make_scan_html(res, hp)
    log(f"榜单 {hp}")

    # 控制台榜单（前 15）
    log("\n题材情绪榜（按散户看空比例，样本≥30 优先）")
    log(f"{'题材':<12}{'样本':>5}{'看空%':>7}{'正面%':>7}{'机构篇':>6}{'主力亿':>8}")
    for r in rows[:15]:
        log(f"{r['题材'][:11]:<12}{r['散户样本']:>5}{str(r['散户负面%'] or '—'):>7}"
            f"{str(r['散户正面%'] or '—'):>7}{r['机构样本']:>6}{r['热度·主力净流入(亿)']:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
