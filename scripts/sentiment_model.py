#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sentiment_model.py — 三组情绪追踪模型 v1.0

原型来源：博主「小豆小瓜」的情绪模型——分「散户 / 机构 / 自媒体」三组，
各组统计 正面 / 中性 / 负面 占比，核心观察是**看空比例随时间的变化**
（例：散户看空 19%→13.5%、机构 2%→0%、自媒体 16%→6%，叠加「逢利空不跌」）。

数据源与代理（v1.0 口径，含诚实声明）：
  机构   = 东财研报列表：评级字段直接映射 + 标题词表分类
  散户   = 东方财富股吧帖子标题（微信群不可达，以股吧为散户代理，口径偏差已声明）
  自媒体 = 东财资讯搜索标题（代理口径偏杂，含媒体/机构转载，需持续优化）

边界：只产情绪读数与变化量，不产买卖判断。词表为 C 级草案，待回测/升级 LLM。

用法：
  python scripts/sentiment_model.py --code 300750 --name 宁德时代          # 个股模式
  python scripts/sentiment_model.py --theme 半导体                          # 主题模式（仅资讯+可选研报）
  python scripts/sentiment_model.py --code 300750 --name 宁德时代 --pages 3 # 股吧多翻页
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "scripts", "sentiment_config.json"), encoding="utf-8"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA})
P = CFG["params"]
CATS = ["正面", "中性", "负面"]


def log(m):
    print(m, flush=True)


def getj(url, params=None, tries=3, timeout=15):
    for i in range(tries):
        try:
            return S.get(url, params=params, timeout=timeout).json()
        except Exception as e:
            if i == tries - 1:
                log(f"  ⚠ FAIL {url.split('/')[2][:30]} :: {str(e)[:60]}")
                return None
            time.sleep(1 + i)


def get_jsonp(url, params=None, tries=3, timeout=15):
    """jsonp 响应：cb({...}) → dict。"""
    for i in range(tries):
        try:
            t = S.get(url, params=params, timeout=timeout).text
            return json.loads(t[t.find("(") + 1:t.rfind(")")])
        except Exception as e:
            if i == tries - 1:
                log(f"  ⚠ FAIL {url.split('/')[2][:30]} :: {str(e)[:60]}")
                return None
            time.sleep(1 + i)


def jstr(s):
    """股吧标题内含 \\uXXXX 转义时正确还原。"""
    if s is None:
        return ""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s


# ═════════════ 分类器 v1：词表 + 否定前缀（C 级草案） ═════════════
LEX = {c: set(w for w in CFG["lexicon"].get(c, [])) for c in CATS}
NEG = set(CFG.get("negation_prefix", []))


def classify(text):
    """规则分类：命中负面词→负面；命中正面词（且无否定前缀）→正面；否则中性。
    同时命中正负时取距句首更近者（近似标题重心）。"""
    if not text:
        return "中性", []
    t = text
    hits = []
    for w in LEX["负面"]:
        i = t.find(w)
        if i >= 0:
            hits.append((i, "负面", w))
    for w in LEX["正面"]:
        i = t.find(w)
        if i >= 0:
            pre = t[max(0, i - 1):i]
            if pre in NEG:
                hits.append((i, "负面", "不+" + w))
            else:
                hits.append((i, "正面", w))
    if not hits:
        return "中性", []
    hits.sort()
    return hits[0][1], [h[2] for h in hits]


# ═════════════ 源 1：机构 = 研报 ═════════════
def fetch_reports(code, days, qtype="0", industry_filter=None, max_pages=None):
    """qtype=0 个股研报；qtype=1 行业研报（全行业列表后按行业名过滤）。"""
    end = dt.date.today()
    beg = end - dt.timedelta(days=days)
    out = []
    page = 1
    cap = P["report_max"]
    while len(out) < cap:
        d = getj(CFG["sources"]["report_list"], {
            "pageNo": page, "pageSize": "50",
            "code": code if qtype == "0" else "",
            "industryCode": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": beg.isoformat(), "endTime": end.isoformat(),
            "fields": "", "qType": qtype})
        if not d or not d.get("data"):
            break
        batch = d["data"]
        if qtype != "0" and industry_filter:
            batch = [r for r in batch
                     if industry_filter in (str(r.get("industryName") or "") + str(r.get("title") or ""))]
        out.extend(batch)
        if len(d["data"]) < 50:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        time.sleep(0.15)
    recs = []
    for r in out:
        rating = str(r.get("sRatingName") or r.get("emRatingName") or r.get("rating") or "")
        change = str(r.get("sRatingChange") or r.get("ratingChange") or "")
        cat, kw = classify(rating + " " + change)
        if rating:
            joined = rating + change
            if any(x in joined for x in CFG["rating_map"]["负面"]):
                cat = "负面"
            elif any(x in joined for x in CFG["rating_map"]["正面"]):
                cat = "正面"
            elif any(x in joined for x in CFG["rating_map"]["中性"]):
                cat = "中性"
        recs.append({"date": (r.get("publishDate") or "")[:10], "org": r.get("orgSName"),
                     "title": r.get("title"), "rating": rating, "change": change,
                     "lastRating": str(r.get("lastEmRatingName") or r.get("lastEmRatingValue") or ""),
                     "changeCode": str(r.get("ratingChange") or "").strip(),
                     "cat": cat, "kw": kw})
    return recs


# ═════════════ 源 2：散户 = 股吧（微信群不可达的代理） ═════════════
def fetch_theme_reports(industry, days, max_pages=20, max_hits=80, aliases=None):
    """按关键词扫全市场个股研报（qType=0, 无 code），用于补足机构样本量。"""
    if aliases is None:
        aliases = ((CFG.get("industry_aliases") or {}).get(industry) or [industry]) if industry else []
    if not aliases:
        return []
    end = dt.date.today()
    beg = end - dt.timedelta(days=days)
    hits, seen = [], set()
    for page in range(1, max_pages + 1):
        d = getj(CFG["sources"]["report_list"], {
            "pageNo": page, "pageSize": "50", "code": "", "industryCode": "*",
            "rating": "*", "ratingChange": "*",
            "beginTime": beg.isoformat(), "endTime": end.isoformat(),
            "fields": "", "qType": "0"})
        rows = (d or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            title = str(r.get("title") or "")
            if not any(a in title for a in aliases):
                continue
            k = (title, r.get("orgSName"))
            if k in seen:
                continue
            seen.add(k)
            rating = str(r.get("sRatingName") or r.get("emRatingName") or "")
            change = str(r.get("sRatingChange") or r.get("ratingChange") or "")
            cat, kw = classify(rating + " " + change)
            joined = rating + change
            if rating:
                if any(x in joined for x in CFG["rating_map"]["负面"]):
                    cat = "负面"
                elif any(x in joined for x in CFG["rating_map"]["正面"]):
                    cat = "正面"
                elif any(x in joined for x in CFG["rating_map"]["中性"]):
                    cat = "中性"
            hits.append({"date": (r.get("publishDate") or "")[:10], "org": r.get("orgSName"),
                         "title": title, "rating": rating, "change": change,
                         "lastRating": str(r.get("lastEmRatingName") or ""),
                         "changeCode": str(r.get("ratingChange") or "").strip(),
                         "cat": cat, "kw": kw, "scope": "产业链"})
            if len(hits) >= max_hits:
                return hits
        if len(rows) < 50:
            break
        time.sleep(0.15)
    return hits


def guess_industry(code):
    """从最近一次股池全A快照缓存里查该股行业（只读，不写）。"""
    try:
        import glob as _glob
        for c in reversed(sorted(_glob.glob(os.path.join(ROOT, "cache", "pool_*", "all_a_snapshot.json")))):
            rows = json.load(open(c, encoding="utf-8"))
            for r in rows:
                if str(r.get("code")) == str(code) and r.get("industry"):
                    return r["industry"]
    except Exception:
        pass
    return None


GUBA_STATE = {"blocked": False, "requests": 0, "max_requests": 40}


def _guba_cache_path(code, for_write=False):
    """读：返回该票最近的缓存文件（跨天也能复用，有效期由 mtime 控制）；
    写：返回当天的文件路径（保留按日历史，便于回测）。"""
    d = os.path.join(ROOT, "cache", "guba")
    os.makedirs(d, exist_ok=True)
    today = dt.date.today().strftime("%Y%m%d")
    if for_write:
        return os.path.join(d, f"{code}_{today}.json")
    import glob as _g
    cands = sorted(_g.glob(os.path.join(d, f"{code}_*.json")))
    return cands[-1] if cands else os.path.join(d, f"{code}_{today}.json")




def guba_via_search_api(code, name=None, max_queries=4, per=50, keep_days=10):
    """股吧帖子：走 DeepSeek Search 工具检索 site:guba.eastmoney.com。
    优势：走厂商服务器，**不受本地 IP 封禁影响**；标题/日期/摘要完整。
    代价：按次调用（每票 max_queries 次），非封禁期应优先用免费的列表页。"""
    key = None
    kp = os.path.join(ROOT, "data", "llm_key.txt")
    if os.path.isfile(kp):
        for line in open(kp, encoding="utf-8"):
            v = line.strip()
            if v and not v.startswith("#"):
                key = v
                break
    if not key:
        return []
    base = "https://api.deepseek.com"
    bp = os.path.join(ROOT, "data", "llm_base.txt")
    if os.path.isfile(bp):
        for line in open(bp, encoding="utf-8"):
            v = line.strip()
            if v and not v.startswith("#"):
                base = v.rstrip("/").replace("/v1", "")
                break
    qs = [f"site:guba.eastmoney.com {code}"]
    if name:
        qs += [f"site:guba.eastmoney.com {name} 股价",
               f"site:guba.eastmoney.com {name} 业绩"]
    qs = qs[:max_queries]
    seen, out = set(), []
    for q in qs:
        try:
            r = requests.post(f"{base}/v1/tools/search",
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              json={"query": q, "count": per}, timeout=40)
            for x in (r.json().get("results") or []):
                u = str(x.get("url") or "").split("#")[0]
                t = re.sub(r"<.*?>", "", str(x.get("title") or "")).strip()
                if not u or not t or u in seen:
                    continue
                seen.add(u)
                out.append({"title": t, "time": str(x.get("date") or "")[:10],
                            "snippet": str(x.get("snippet") or "")[:120],
                            "url": u, "bar": "search-api"})
        except Exception:
            continue
        time.sleep(0.3)
    beg = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    out = [x for x in out if x["time"] >= beg]
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


def guba_search_fallback(code, max_pages=6, per=50, keep_days=7):
    """股吧搜索接口兜底（与列表页是不同通道，IP 封禁期仍可用）。
    局限：按相关度排序、时间乱序 → 多翻页后客户端按时间倒序，只保留近 keep_days 天。"""
    t = "gubaArticleWeb"
    items, seen = [], set()
    for p in range(1, max_pages + 1):
        param = {"uid": "", "keyword": code, "type": [t], "client": "web",
                 "clientType": "web", "clientVersion": "curr",
                 "param": {t: {"searchScope": "default", "sort": "time",
                               "pageIndex": p, "pageSize": per,
                               "preTag": "", "postTag": ""}}}
        try:
            d = get_jsonp(CFG["sources"]["search_api"] if CFG["sources"].get("search_api")
                          else "https://search-api-web.eastmoney.com/search/jsonp",
                          {"cb": "cb", "param": json.dumps(param, ensure_ascii=False)})
            rows = (d or {}).get("result", {}).get(t) or []
        except Exception:
            break
        if not rows:
            break
        for x in rows:
            i = x.get("id")
            if i in seen:
                continue
            seen.add(i)
            title = re.sub(r"<.*?>", "", str(x.get("title") or ""))
            title = re.sub(r"\$[^$]*\$", "", title).strip()
            if not title:
                continue
            items.append({"title": title, "time": str(x.get("createTime") or "")[:16],
                          "url": x.get("url"), "bar": "search"})
        time.sleep(0.3)
    beg = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    recent = [x for x in items if x["time"][:10] >= beg]
    recent.sort(key=lambda x: x["time"], reverse=True)
    return recent


def fetch_guba(code, max_posts, stock_name=None, pages=1):
    """股吧帖（含风控防护）：
    - 按「代码+日期」缓存，TTL 6 小时，当日重跑不再请求；
    - 响应含「身份核实」或长度异常 → 判定风控，全局停止且不重试（越试越封）；
    - 单次运行请求总数上限 GUBA_STATE['max_requests']。
    返回 (本吧帖, 财富号帖)。
    """
    cp = _guba_cache_path(code)
    if os.path.isfile(cp) and time.time() - os.path.getmtime(cp) < 6 * 3600:
        try:
            d = json.load(open(cp, encoding="utf-8"))
            return d.get("posts", []), d.get("cfh", [])
        except Exception:
            pass
    if GUBA_STATE["blocked"]:
        fb = guba_via_search_api(code, stock_name)
        if fb:
            log(f"      股吧降级（Search API）：{len(fb)} 条（近 10 天，标题/日期完整）")
            return fb, []
        fb = guba_search_fallback(code)
        if fb:
            log(f"      股吧降级（本地搜索接口）：{len(fb)} 条")
        return fb, []
    posts, caifuhao, seen = [], [], set()
    for pg in range(1, max(1, pages) + 1):
        if GUBA_STATE["requests"] >= GUBA_STATE["max_requests"]:
            log("  ⚠ 已达单次股吧请求上限，停止抓取（样本受限，请分次运行）")
            break
        url = (CFG["sources"]["guba_page"] if pg == 1
               else CFG["sources"]["guba_page"].replace(".html", f"_{pg}.html")).format(code=code)
        try:
            h = S.get(url, timeout=15).text
            GUBA_STATE["requests"] += 1
        except Exception as e:
            log(f"  ⚠ 股吧第{pg}页抓取失败（{str(e)[:50]}）")
            continue
        if "身份核实" in h or len(h) < 8000:
            GUBA_STATE["blocked"] = True
            log("  ⚠⚠ 股吧触发风控（身份核实页）——立即停止全部股吧抓取，避免加重封禁；"
                "请隔 2—4 小时再跑，或改用缓存")
            break
        idx = [m.start() for m in re.finditer(r'"post_title"', h)] + [len(h)]

        def grab(seg, key):
            m = re.search(r'"%s":"?(.*?)"?(,"|\})' % key, seg)
            return m.group(1).rstrip('"') if m else None

        for a, b in zip(idx[:-1], idx[1:]):
            seg = h[a:b]
            m = re.match(r'"post_title":"(.*?)"', seg)
            if not m:
                continue
            title = jstr(m.group(1))
            bar = grab(seg[len(m.group(0)):], "stockbar_code")
            rec = {"title": title, "bar": bar,
                   "time": grab(seg, "post_last_time") or grab(seg, "post_publish_time"),
                   "click": int(grab(seg, "post_click_count") or 0) or None,
                   "comment": int(grab(seg, "post_comment_count") or 0) or None}
            dkey = (title, rec["time"])
            if dkey in seen:
                continue
            seen.add(dkey)
            if bar == "cfhpl":
                caifuhao.append(rec)
            elif bar == code:
                posts.append(rec)
            if len(posts) >= max_posts:
                break
        if len(posts) >= max_posts or GUBA_STATE["blocked"]:
            break
        time.sleep(CFG.get("params", {}).get("guba_sleep", 1.2))
    if posts:
        json.dump({"posts": posts, "cfh": caifuhao},
                  open(_guba_cache_path(code, for_write=True), "w", encoding="utf-8"),
                  ensure_ascii=False)
    elif not GUBA_STATE["blocked"]:
        log(f"  ⚠ 股吧未解析出本吧帖（{pages} 页），尝试搜索接口降级")
        fb = guba_search_fallback(code)
        if fb:
            log(f"      股吧降级（搜索接口）：{len(fb)} 条")
            return fb, []
    return posts, caifuhao


def fetch_news(kw, max_n, days, must_contain=None, pages=1):
    """资讯搜索翻页取量；个股模式按股票名/代码做相关性过滤。"""
    end = dt.date.today()
    beg = end - dt.timedelta(days=days)
    out, seen = [], set()
    for pi in range(1, max(1, pages) + 1):
        param = {"uid": "", "keyword": kw, "type": ["cmsArticleWebOld"], "client": "web",
                 "clientType": "web", "clientVersion": "curr",
                 "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                                "pageIndex": pi, "pageSize": 50,
                                                "preTag": "", "postTag": ""}}}
        d = get_jsonp(CFG["sources"]["news_search"],
                      {"cb": "cb", "param": json.dumps(param, ensure_ascii=False)})
        try:
            items = d["result"]["cmsArticleWebOld"]
        except Exception:
            if pi == 1:
                log("  ⚠ 资讯搜索无结果")
            break
        if not items:
            break
        for it in items:
            t = (it.get("date") or "")[:10]
            if t and t < beg.isoformat():
                continue
            title = re.sub(r"<.*?>", "", it.get("title") or "")
            head = (it.get("content") or "")[:80]
            if must_contain and not any(x in title or x in head for x in must_contain):
                continue
            if title in seen:
                continue
            seen.add(title)
            out.append({"date": t, "title": title, "content_head": head})
            if len(out) >= max_n:
                return out
        time.sleep(0.25)
    return out


# ═════════════ 聚合 ═════════════
def agg(recs, target=None):
    n = len(recs)
    base = {"样本": n, "目标": target}
    if target:
        base["达标"] = n >= target
    if n == 0:
        base.update({"正面%": None, "中性%": None, "负面%": None})
        return base
    c = Counter(r["cat"] for r in recs)
    base.update({"正面%": round(c["正面"] / n * 100, 1),
                 "中性%": round(c["中性"] / n * 100, 1),
                 "负面%": round(c["负面"] / n * 100, 1)})
    return base


def weighted_agg(recs):
    """按阅读数加权的散户读数（v1 保留未加权为主读数，加权作对照）。"""
    recs = [r for r in recs if r.get("click")]
    n = len(recs)
    if n < 10:
        return None
    tot = {c: 0 for c in CATS}
    for r in recs:
        tot[r["cat"]] += r["click"]
    s = sum(tot.values())
    return {c: round(tot[c] / s * 100, 1) for c in CATS} | {"样本": n}


def prev_snapshot(key):
    d = os.path.join(ROOT, "cache", "sentiment", key)
    if not os.path.isdir(d):
        return None, None
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        return None, None
    return json.load(open(os.path.join(d, files[-1]), encoding="utf-8")), files[-1]


def delta(cur, prev):
    out = {}
    for g in cur:
        if not isinstance(cur[g], dict) or cur[g].get("负面%") is None:
            continue
        p = (prev or {}).get(g)
        if isinstance(p, dict) and p.get("负面%") is not None:
            out[g] = {"负面%变化": round(cur[g]["负面%"] - p["负面%"], 1),
                      "正面%变化": round(cur[g]["正面%"] - p["正面%"], 1),
                      "上次": p}
    return out


# ═════════════ 报告 v2（2026-10-18 重新设计） ═════════════
DISP = CFG["display"]          # 正面红 / 中性灰 / 负面绿（A 股惯例）


def _badge_pos(chg, unit="pct"):
    """看多方向徽章（红）：数值下降视为偏多。"""
    if chg is None or chg == 0:
        return ""
    return f'<span class="badge b-pos">▼{abs(chg):g}{unit}</span>'


def _badge_neg(chg, unit="pct"):
    """看空方向徽章（绿）：数值上升视为偏空。"""
    if chg is None or chg == 0:
        return ""
    return f'<span class="badge b-neg">▲{abs(chg):g}{unit}</span>'


def _badge_neu(chg, unit=""):
    """中性数值徽章：升红降绿（A股涨跌惯例）。"""
    if chg is None or chg == 0:
        return ""
    arrow = "▲" if chg > 0 else "▼"
    cls = "b-pos" if chg > 0 else "b-neg"
    return f'<span class="badge {cls}">{arrow}{abs(chg):g}{unit}</span>'


def _stack(vals):
    """三段堆叠条：中性灰弱化为底色。"""
    segs = []
    for c in CATS:
        v = vals.get(c + "%")
        if v is None:
            continue
        segs.append(f'<div class="seg" style="width:{v}%;background:{DISP[c]}"></div>')
    return f'<div class="stack">{"".join(segs) or ""}</div>'


def group_card(group, d, chg_info, inst_panel=None):
    """情绪组卡片。机构组走评级迁移呈现（A股评级右偏，看空比例无信息量）。"""
    n = d.get("样本", 0)
    tgt = d.get("目标")
    ok = d.get("达标")
    sub = f'样本 {n}'
    if tgt:
        sub += f' / 目标 ≥{tgt}'
        if ok is False:
            sub += ' ⚠未达标'
    neg = d.get("负面%")
    warn = f'<div class="mini warn-t">⚠ {d["_warn"]}</div>' if d.get("_warn") else ""
    note = f'<div class="mini">{d["_note"]}</div>' if d.get("_note") else ""

    if group == "机构":
        panel = inst_panel or {}
        if panel:
            pills = "".join(
                f'<span class="pill {"p-up" if k in ("首次覆盖", "上调") and panel.get(k) else ""}'
                f'{"p-dn" if k == "下调" and panel.get(k) else ""}">{k} {panel.get(k, 0)}</span>'
                for k in ("维持", "首次覆盖", "上调", "下调"))
            body = (f'<div class="pills">{pills}</div>'
                    f'<div class="mini">覆盖研报 {panel.get("研报数", 0)} 篇（近 30 天）。'
                    f'A 股机构几乎不写卖出，看空 0% 属常态、<b>无信息量</b>；机构态度看'
                    f'<b>迁移方向</b>（首次覆盖/上调=催化，下调=警报）。</div>')
            head_num = f'<span class="dim-t">看迁移</span>'
        else:
            body = _stack(d) + f'<div class="nums">' + "".join(
                f'<span class="dot" style="background:{DISP[c]}"></span>{c} {d.get(c+"%","—")}%'
                for c in CATS) + '</div>'
            head_num = f'<span class="dim-t">样本 {n}</span>'
    else:
        body = _stack(d)
        nums = "".join(f'<span class="dot" style="background:{DISP[c]}"></span>{c} <b>{d.get(c+"%","—")}%</b>'
                       for c in CATS)
        body += f'<div class="nums">{nums}</div>'
        head_num = f'<span class="big">{neg if neg is not None else "—"}<i>%</i></span><span class="dim-t">看空</span>'
        if "_按阅读数加权" in d:
            w = d["_按阅读数加权"]
            body += (f'<div class="mini">按阅读数加权：正 {w["正面%"]}% / 中 {w["中性%"]}% / '
                     f'负 {w["负面%"]}%（{w["样本"]}）</div>')

    delta = ""
    if chg_info:
        delta = _badge_neg(chg_info.get("负面%变化")) + _badge_pos(-chg_info.get("正面%变化", 0))
    return f'''<div class="gcard">
  <div class="ghead"><div><div class="gname">{group}</div><div class="gsub{" gwarn" if ok is False else ""}">{sub}</div></div>
    <div class="gright">{head_num}{delta}</div></div>
  {body}{warn}{note}</div>'''


def market_html(res):
    m = res.get("市场面")
    if not m:
        return ""
    md = res.get("市场面变化") or {}

    def dn(key):
        return _badge_neu(md[key]["变化"]) if key in md else ""

    b, s, p = m.get("广度", {}), m.get("结构", {}), m.get("溢价", {})
    prem = f'{p["平均溢价%"]}%' if isinstance(p, dict) and p.get("平均溢价%") is not None else "—"
    prem_med = f'{p["中位溢价%"]}%' if isinstance(p, dict) and p.get("中位溢价%") is not None else "—"
    rows = [
        ("涨跌家数比", b.get("涨跌家数比", "—"),
         f'{b.get("上涨", "—")} 涨 / {b.get("下跌", "—")} 跌', dn("广度.涨跌家数比")),
        ("涨跌幅中位数", f'{b.get("涨跌幅中位数%", "—")}%', f'成交 {b.get("两市成交额(亿)", "—")} 亿',
         dn("广度.涨跌幅中位数%")),
        ("封板率", f'{s.get("封板率%", "—")}%', f'涨停 {s.get("涨停家数", "—")} / 炸板 {s.get("炸板家数", "—")}',
         dn("结构.封板率%")),
        ("最高连板", f'{s.get("最高连板", "—")} 板', f'二板以上 {s.get("二板及以上占比%", "—")}%',
         dn("结构.最高连板")),
        ("昨涨停溢价", prem, f'中位 {prem_med} ｜ 红盘率 {p.get("红盘率%", "—") if isinstance(p, dict) else "—"}%',
         dn("溢价.平均溢价%")),
    ]
    cards = "".join(
        f'<div class="mcard"><div class="k">{k}{badge}</div><div class="v">{v}</div><div class="s">{s2}</div></div>'
        for k, v, s2, badge in rows)
    return (f'<div class="sect">市场水位 <span class="dim-t">· {m.get("数据日期", "")} 收盘'
            f' · 吸纳自微观温度计</span></div><div class="mrow">{cards}</div>')


def attention_html(att):
    if not att:
        return ""
    cells = []
    rank, total = att.get("人气排名"), att.get("全市场样本数")
    if rank:
        chg = att.get("排名日变化") or 0
        arr = "▲" if chg > 0 else ("▼" if chg < 0 else "—")
        cells.append(("东财人气", f'{rank} <span class="dim-t">/ {total}</span>',
                      f'日变化 {arr}{abs(chg)}'))
    for k, f2 in (("机构参与度", "{:.0%}"), ("综合得分", "{:.1f}"), ("关注指数", "{:.1f}")):
        v = att.get(k)
        if v is not None:
            cells.append((k, f2.format(float(v)), f'千股千评 {att.get("数据日期", "")}'))
    cards = "".join(f'<div class="mcard sm"><div class="k">{k}</div><div class="v">{v}</div>'
                    f'<div class="s">{s}</div></div>' for k, v, s in cells)
    return f'<div class="sect">关注度 <span class="dim-t">· 热度读数，非多空比例</span></div><div class="mrow">{cards}</div>'


def panel_html(res):
    panel = res.get("机构面板")
    if not panel:
        return ""
    pd = res.get("面板变化") or {}

    def dn(key):
        return _badge_neu(pd[key]["变化"]) if key in pd else ""

    rf, cj, aj = panel.get("评级面", {}), panel.get("共识面", []), panel.get("行动面", {})
    h = ['<div class="sect">机构面板 <span class="dim-t">· 评级迁移 + 盈利预测共识 + 用脚/用钱投票</span></div>',
         '<div class="panel">']
    h.append('<div class="pline"><span class="plab">评级迁移</span>'
             f'研报 {rf.get("研报数", 0)} 篇：'
             f'<span class="pill p-up">首次 {rf.get("首次覆盖", 0)}</span>'
             f'<span class="pill p-up">上调 {rf.get("上调", 0)}</span>'
             f'<span class="pill p-dn">下调 {rf.get("下调", 0)}</span>'
             f'<span class="pill">维持 {rf.get("维持", 0)}</span>{dn("评级面.研报数")}</div>')
    if cj:
        rows = ""
        for r in cj:
            y = str(r.get("年度"))
            dkey = f"共识面.{y}年预测机构数"
            rows += (f'<tr><td>{y}年</td><td><b>{r.get("预测机构数")}</b> 家{dn(dkey)}</td>'
                     f'<td>{r.get("净利润均值")}</td>'
                     f'<td class="dim-t">{r.get("最小值")} ~ {r.get("最大值")}</td></tr>')
        h.append('<div class="pline"><span class="plab">盈利共识</span>'
                 '<table class="t2"><tr><th>年度</th><th>预测机构</th><th>净利润均值(亿)</th>'
                 '<th>分歧区间</th></tr>' + rows + '</table></div>')
    if aj:
        h.append('<div class="pline"><span class="plab">行动验证</span>'
                 f'近 90 天调研 <b>{aj.get("近90天调研次数", 0)}</b> 次（机构 {aj.get("接待机构数合计", 0)} 家）｜ '
                 f'近 30 天龙虎榜 <b>{aj.get("龙虎榜上榜次数", 0)}</b> 次、'
                 f'机构席位净买 <b>{aj.get("机构席位净买额(亿)", 0)}</b> 亿'
                 f'{dn("行动面.近90天调研次数")}{dn("行动面.龙虎榜上榜次数")}</div>')
    h.append('<div class="mini">评级映射实测反推：1=下调 / 2=首次覆盖 / 3=维持 / 4=上调（C级）。'
             '共识＝同花顺汇总；龙虎榜＝机构席位口径。</div></div>')
    return "".join(h)


def make_html(res, out_path):
    m = res["meta"]
    groups = [g for g in ("机构", "散户", "微信群", "财富号", "自媒体") if g in res]
    cards = "".join(group_card(g, res[g], (res.get("变化") or {}).get(g),
                            (res.get("机构面板") or {}).get("评级面")) for g in groups)

    # 顶部摘要 chips
    chips = []
    for g in groups:
        d = res[g]
        if isinstance(d, dict) and d.get("负面%") is not None:
            dv = (res.get("变化") or {}).get(g) or {}
            badge = _badge_neg(dv.get("负面%变化"))
            chips.append(f'<div class="chip"><span class="dim-t">{g}</span>'
                         f'<b>{d["负面%"]}%</b>{badge}</div>')

    # 对比表
    rows = ""
    for g, dv in (res.get("变化") or {}).items():
        rows += (f'<tr><td>{g}</td><td>{dv["上次"].get("负面%","—")}%</td>'
                 f'<td><b>{res[g].get("负面%","—")}%</b></td>'
                 f'<td class="{"t-up" if dv["负面%变化"]>0 else "t-dn" if dv["负面%变化"]<0 else ""}">'
                 f'{dv["负面%变化"]:+.1f}pct</td>'
                 f'<td>{dv["上次"].get("正面%","—")}% → {res[g].get("正面%","—")}%'
                 f'（{dv["正面%变化"]:+.1f}）</td></tr>')
    attention_section = attention_html(res.get("热度"))
    panel_section = panel_html(res)
    market_section = market_html(res)
    delta_block = (f'<table class="t1"><thead><tr><th>组</th><th>上次看空</th><th>本次看空</th>'
                   f'<th>变化</th><th>正面变化</th></tr></thead><tbody>{rows}</tbody></table>') if rows else \
        '<div class="hint">无历史快照 —— 本次为建档基线。核心读数是这张表，隔几日重跑即出变化。</div>'
    pd_rows = ""
    for k, dv in (res.get("面板变化") or {}).items():
        cls = "t-up" if dv["变化"] > 0 else ("t-dn" if dv["变化"] < 0 else "")
        pd_rows += f'<tr><td>{k}</td><td>{dv["上次"]}</td><td><b>{dv["本次"]}</b></td><td class="{cls}">{dv["变化"]:+g}</td></tr>'
    for k, dv in (res.get("市场面变化") or {}).items():
        cls = "t-up" if dv["变化"] > 0 else ("t-dn" if dv["变化"] < 0 else "")
        pd_rows += f'<tr><td>市场面.{k}</td><td>{dv["上次"]}</td><td><b>{dv["本次"]}</b></td><td class="{cls}">{dv["变化"]:+g}</td></tr>'
    if pd_rows:
        delta_block += (f'<table class="t1" style="margin-top:12px"><thead><tr><th>面板/市场读数</th>'
                        f'<th>上次</th><th>本次</th><th>变化</th></tr></thead><tbody>{pd_rows}</tbody></table>')

    html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>情绪追踪 · {m["label"]}</title><style>
:root{{--bg:#0a0d12;--panel:#12161d;--panel2:#181e27;--line:#232b36;--txt:#e8edf4;
--dim:#8fa0b3;--dim2:#5b6779;--pos:#f0524f;--neg:#34a853;--neu:#39424f;--warn:#e2a336;--info:#5aa7ff}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:-apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
line-height:1.6;padding:28px 18px 56px;font-variant-numeric:tabular-nums}}
.wrap{{max-width:920px;margin:0 auto}}
header{{padding-bottom:14px;margin-bottom:18px;border-bottom:1px solid var(--line)}}
h1{{font-size:20px;font-weight:600}}
.meta{{color:var(--dim);font-size:12px;margin-top:5px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 4px}}
.chip{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:7px 12px;
font-size:13px;display:flex;align-items:center;gap:7px}}
.chip b{{font-size:16px}}
.badge{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;margin-left:4px}}
.b-pos{{background:rgba(240,82,79,.14);color:#ff8a87}}
.b-neg{{background:rgba(52,168,83,.15);color:#5bd079}}
.sect{{font-size:12px;color:var(--dim);letter-spacing:1.5px;margin:26px 0 10px;
display:flex;align-items:center;gap:10px}}
.sect:after{{content:"";flex:1;height:1px;background:var(--line)}}
.dim-t{{color:var(--dim);font-size:11.5px;font-weight:400}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:680px){{.grid2{{grid-template-columns:1fr}}}}
.gcard{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 17px}}
.ghead{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:11px}}
.gname{{font-size:14.5px;font-weight:600}}
.gsub{{font-size:11px;color:var(--dim2)}}
.gright{{text-align:right}}
.gright .big{{font-size:24px;font-weight:700}}
.gright .big i{{font-style:normal;font-size:13px;color:var(--dim);font-weight:400}}
.gright .dim-t{{display:block;margin-top:1px}}
.stack{{display:flex;height:9px;border-radius:99px;overflow:hidden;gap:2px;background:var(--panel2)}}
.seg{{height:100%;border-radius:99px}}
.nums{{font-size:12px;color:var(--dim);margin-top:9px}}
.nums b{{color:var(--txt);font-weight:600;margin-right:10px}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:2px;margin:0 4px 0 0;vertical-align:1px}}
.mini{{font-size:11.5px;color:var(--dim);margin-top:8px}}
.warn-t{{color:var(--warn)}}
.gsub.gwarn{{color:var(--warn)}}
.pills{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px}}
.pill{{font-size:12px;padding:3px 10px;border-radius:8px;background:var(--panel2);
border:1px solid var(--line);color:var(--dim)}}
.pill.p-up{{color:#ff8a87;border-color:rgba(240,82,79,.4)}}
.pill.p-dn{{color:#5bd079;border-color:rgba(52,168,83,.4)}}
.mrow{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.mcard{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}}
.mcard .k{{font-size:11.5px;color:var(--dim)}}
.mcard .v{{font-size:19px;font-weight:600;margin-top:3px}}
.mcard .s{{font-size:11px;color:var(--dim2);margin-top:2px}}
.mcard.sm .v{{font-size:17px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 17px}}
.pline{{font-size:13px;padding:7px 0;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}}
.pline+.pline{{border-top:1px dashed var(--line)}}
.plab{{font-size:11.5px;color:var(--dim2);min-width:64px}}
.pline b{{font-weight:600}}
.t1,.t2{{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:4px}}
.t1 th,.t1 td,.t2 th,.t2 td{{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line)}}
.t1 th,.t2 th{{color:var(--dim);font-weight:400;font-size:11px;background:var(--panel2)}}
.t1 tr:last-child td,.t2 tr:last-child td{{border-bottom:none}}
.t2{{background:transparent;border:none}}
.t-up{{color:#ff8a87}} .t-dn{{color:#5bd079}}
.hint{{color:var(--dim);font-size:13px;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:13px 16px}}
details{{margin-top:22px}}
summary{{cursor:pointer;color:var(--dim);font-size:12.5px}}
.note{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 17px;
font-size:12px;color:var(--dim);margin-top:10px}}
.note ul{{margin-left:15px}} .note li{{margin:4px 0}}
footer{{margin-top:22px;padding-top:13px;border-top:1px solid var(--line);color:var(--dim2);font-size:11.5px}}
</style></head><body><div class="wrap">
<header><h1>情绪追踪 · {m["label"]}</h1>
<div class="meta">{m.get("layer","")}层　·　成分 {m.get("members_total","—")} 只{("　·　已覆盖 " + str(m.get("members_covered_after"))) if m.get("members_covered_after") else ""}　·　
{m["run_at"]}　·　模型 v{m["version"]}<br>机构=研报 ｜ 散户=股吧 ｜ 财富号=大V帖 ｜ 自媒体=资讯 ｜ 微信群=手动导出通道（默认关）</div>
<div class="chips">{''.join(chips)}</div></header>

{market_section}

<div class="sect">三组情绪构成 <span class="dim-t">· 机构为评级迁移，其余为语气占比</span></div>
<div class="grid2">{cards}</div>

{attention_section}
{panel_section}

<div class="sect">与上次快照对比 <span class="dim-t">· 模型核心读数：看空比例的变化</span></div>
{delta_block}

<details><summary>口径与限制（点开查看）</summary><div class="note"><ul>
<li><b>散户</b>：微信群不可自动采集（合规），以股吧本吧帖为代理；财富号帖已拆出单列。</li>
<li><b>机构</b>：研报评级直接映射。A 股评级天然右偏（几乎无卖出），看空比例无信息量，
已改用<b>评级迁移</b>呈现（见机构面板）。</li>
<li><b>自媒体</b>：资讯搜索为代理口径，混有媒体/机构转载。</li>
<li><b>分类器</b>：词表规则（C 级）。首轮 LLM 对比：一致率 60.2%、方向反转 6/93、
负面占比 13% vs 33% —— 词表系统性低估悲观，LLM 切换在路线图中。</li>
<li><b>市场面</b>：吸纳自微观温度计；快照限流时自动复用股池缓存（数据日期匹配才可用）。</li>
<li><b>本页只产读数，不产判断</b>；情绪读数须与价格行为、量价结构互证后由交易体系裁定。</li>
</ul></div></details>

<footer>工具 scripts/sentiment_model.py v{m["version"]}（.venv）　·　
数据源 东方财富（研报/股吧/资讯/人气榜/千股千评/龙虎榜）+ 同花顺（盈利共识）　·　
本页为数据整理与研究分析，不构成投资建议。</footer></div></body></html>'''
    open(out_path, "w", encoding="utf-8").write(html)
    log(f"报告 {out_path}")



def fetch_market_panel():
    """市场面（吸纳自 micro_sentiment 微观温度计）：广度 + 涨停池结构 + 昨涨停溢价。
    个股/板块情绪读数的市场背景层。快照按交易日缓存（盘后口径）。"""
    if not CFG.get("market_enabled", False):
        return None
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import micro_sentiment as MS
        import pool_pipeline as PP
    except Exception as e:
        log(f"  ⚠ micro_sentiment/pool_pipeline 导入失败，市场面跳过（{str(e)[:60]}）")
        return None
    tds = PP.src_trade_days("eastmoney_push2his", 10) or PP.src_trade_days("sina_index", 10)
    if not tds:
        log("  ⚠ 交易日历获取失败，市场面跳过")
        return None
    today = dt.date.today().strftime("%Y%m%d")
    date8 = max(d for d in tds if d <= today)
    prev8_list = [d for d in tds if d < date8]
    prev8 = prev8_list[-1] if prev8_list else None

    mkt_d = os.path.join(ROOT, "cache", "sentiment", "market")
    os.makedirs(mkt_d, exist_ok=True)
    snap_p = os.path.join(mkt_d, f"snap_{date8}.json")
    if os.path.isfile(snap_p):
        snap = json.load(open(snap_p, encoding="utf-8"))["rows"]
        log(f"      市场快照复用缓存（{date8}，{len(snap)} 只）")
    else:
        snap = MS.snapshot()
        if len(snap) < 3000:
            # 铁律：不拿旧数据冒充当日读数。只接受「本交易日」的缓存，否则本次市场面缺席。
            log(f"  ⚠ 全A快照接口不可用（仅取到 {len(snap)} 只）——市场面本次缺席，"
                f"不以历史快照冒充（取不到 ≠ 存在）")
            return None
        json.dump({"rows": snap}, open(snap_p, "w", encoding="utf-8"),
                  ensure_ascii=False)
    pctmap = {r["code"]: r["pct"] for r in snap}
    zt, zb, dtp = MS.pool("zt", date8), MS.pool("zb", date8), MS.pool("dt", date8)
    return {"数据日期": date8,
            "广度": MS.breadth(snap),
            "结构": MS.structure(zt, zb, dtp),
            "溢价": MS.premium(prev8, pctmap) if prev8 else {"status": "无前交易日"}}


def market_delta(prev, cur):
    """市场面数值字段与上次快照对比。"""
    p = (prev or {}).get("市场面")
    c = cur.get("市场面")
    if not p or not c:
        return {}
    out = {}
    for face in ("广度", "结构", "溢价"):
        for k, v in (c.get(face) or {}).items():
            if k.startswith("__") or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            pv = (p.get(face) or {}).get(k)
            if isinstance(pv, (int, float)) and pv != v:
                out[f"{face}.{k}"] = {"上次": pv, "本次": v, "变化": round(v - pv, 2)}
    return out


def fetch_institution_panel(code, reports):
    """机构面板 v1：评级迁移（研报API）+ 盈利预测共识（同花顺）+ 机构调研（东财）。
    ratingChange 实测映射：1=下调、2=首次覆盖、3=维持；4=上调（样本未观测到，C级约定）。"""
    panel = {"评级面": {}, "共识面": {}, "行动面": {}}
    cc = collections.Counter(str(r.get("changeCode") or "") for r in reports)
    panel["评级面"] = {"研报数": len(reports),
                    "上调": cc.get("4", 0) + cc.get("5", 0),
                    "下调": cc.get("1", 0),
                    "首次覆盖": cc.get("2", 0),
                    "维持": cc.get("3", 0)}
    cons_d = os.path.join(ROOT, "cache", "sentiment", "consensus")
    os.makedirs(cons_d, exist_ok=True)
    cons_p = os.path.join(cons_d, f"{code}.json")
    ttl = CFG.get("params", {}).get("consensus_ttl_days", 3) * 86400
    rows = None
    if os.path.isfile(cons_p) and time.time() - os.path.getmtime(cons_p) < ttl:
        try:
            rows = json.load(open(cons_p, encoding="utf-8"))["rows"]
        except Exception:
            rows = None
    if rows is None:
        try:
            import warnings
            warnings.filterwarnings("ignore")
            os.environ.setdefault("TQDM_DISABLE", "1")
            import akshare as ak
            df = ak.stock_profit_forecast_ths(indicator="预测年报净利润", symbol=code)
            rows = df.to_dict("records")
            json.dump({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "rows": rows}, open(cons_p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:
            log(f"  ⚠ 盈利预测共识读取失败（{str(e)[:60]}）")
    if rows:
        panel["共识面"] = [{"年度": str(r.get("年度")), "预测机构数": r.get("预测机构数"),
                        "净利润均值": r.get("均值"), "最小值": r.get("最小值"),
                        "最大值": r.get("最大值")} for r in rows]
    jgdy_d = os.path.join(ROOT, "cache", "sentiment", "jgdy")
    os.makedirs(jgdy_d, exist_ok=True)
    today = dt.date.today().isoformat().replace("-", "")
    jgdy_p = os.path.join(jgdy_d, f"jgdy_{today}.json")
    jgdy_ttl = CFG.get("params", {}).get("jgdy_ttl_days", 1) * 86400
    rows_j = None
    if os.path.isfile(jgdy_p) and time.time() - os.path.getmtime(jgdy_p) < jgdy_ttl:
        try:
            rows_j = json.load(open(jgdy_p, encoding="utf-8"))["rows"]
        except Exception:
            rows_j = None
    if rows_j is None:
        try:
            import warnings
            warnings.filterwarnings("ignore")
            os.environ.setdefault("TQDM_DISABLE", "1")
            import akshare as ak
            df = ak.stock_jgdy_tj_em()
            rows_j = df[["代码", "名称", "接待机构数量", "接待日期"]].to_dict("records")
            json.dump({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "rows": rows_j}, open(jgdy_p, "w", encoding="utf-8"),
                      ensure_ascii=False, default=str)
        except Exception as e:
            log(f"  ⚠ 机构调研读取失败（{str(e)[:60]}）")
    if rows_j:
        beg90 = (dt.date.today() - dt.timedelta(days=CFG.get("params", {}).get("jgdy_window_days", 90))).isoformat()
        mine = [r for r in rows_j if r.get("代码") == code
                and str(r.get("接待日期") or "")[:10] >= beg90]
        n_inst = 0
        for r in mine:
            try:
                n_inst += int(r.get("接待机构数量") or 0)
            except Exception:
                pass
        dates = sorted(str(r.get("接待日期") or "")[:10] for r in mine)
        panel["行动面"] = {"近90天调研次数": len(mine), "接待机构数合计": n_inst,
                        "最近调研": dates[-1] if dates else None}
    win = CFG.get("params", {}).get("lhb_window_days", 30)
    beg = (dt.date.today() - dt.timedelta(days=win)).strftime("%Y%m%d")
    end = dt.date.today().strftime("%Y%m%d")
    lhb_d = os.path.join(ROOT, "cache", "sentiment", "lhb")
    os.makedirs(lhb_d, exist_ok=True)
    lhb_p = os.path.join(lhb_d, f"jgmmtj_{beg}_{end}.json")
    lhb_ttl = CFG.get("params", {}).get("lhb_ttl_days", 1) * 86400
    rows_l = None
    if os.path.isfile(lhb_p) and time.time() - os.path.getmtime(lhb_p) < lhb_ttl:
        try:
            rows_l = json.load(open(lhb_p, encoding="utf-8"))["rows"]
        except Exception:
            rows_l = None
    if rows_l is None:
        try:
            import warnings
            warnings.filterwarnings("ignore")
            os.environ.setdefault("TQDM_DISABLE", "1")
            import akshare as ak
            df = ak.stock_lhb_jgmmtj_em(start_date=beg, end_date=end)
            rows_l = df.to_dict("records")
            json.dump({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                       "rows": rows_l}, open(lhb_p, "w", encoding="utf-8"),
                      ensure_ascii=False, default=str)
        except Exception as e:
            log(f"  ⚠ 龙虎榜机构席位读取失败（{str(e)[:60]}）")
    if rows_l is not None:
        mine_l = [r for r in rows_l if str(r.get("代码") or "") == code]
        net = 0.0
        for r in mine_l:
            try:
                net += float(r.get("机构买入净额") or 0)
            except Exception:
                pass
        dates_l = sorted(str(r.get("上榜日期") or "")[:10] for r in mine_l)
        panel["行动面"]["龙虎榜上榜次数"] = len(mine_l)
        panel["行动面"]["机构席位净买额(亿)"] = round(net / 1e8, 2)
        panel["行动面"]["最近上榜"] = dates_l[-1] if dates_l else None
    return panel


def panel_delta(prev, cur):
    """机构面板数值字段的与上次对比。"""
    p = (prev or {}).get("机构面板")
    if not p:
        return {}
    out = {}
    for face in ("评级面", "行动面"):
        for k, v in (cur.get("机构面板", {}).get(face) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pv = (p.get(face) or {}).get(k)
                if isinstance(pv, (int, float)):
                    out[f"{face}.{k}"] = {"上次": pv, "本次": v, "变化": round(v - pv, 3)}
    pc = {r.get("年度"): r for r in (p.get("共识面") or []) if isinstance(r, dict)}
    for r in (cur.get("机构面板", {}).get("共识面") or []):
        y = str(r.get("年度"))
        if y in pc and pc[y].get("预测机构数") is not None and r.get("预测机构数") is not None:
            try:
                out[f"共识面.{y}年预测机构数"] = {"上次": pc[y]["预测机构数"],
                                          "本次": r["预测机构数"],
                                          "变化": int(r["预测机构数"]) - int(pc[y]["预测机构数"])}
            except Exception:
                pass
    return out


def load_wechat(key, max_age_days=3):
    """读取 wechat_ingest.py 落盘的微信群快照（近 N 天内最新一份），作为第四组并入。"""
    d = os.path.join(ROOT, "cache", "sentiment", f"wechat_{key}")
    if not os.path.isdir(d):
        return None
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    if not files:
        return None
    snap = json.load(open(os.path.join(d, files[-1]), encoding="utf-8"))
    data_date = snap.get("meta", {}).get("data_date", "")
    try:
        age = (dt.date.today() - dt.date.fromisoformat(data_date)).days
    except Exception:
        return None
    if age > max_age_days:
        return None
    g = dict(snap.get("微信群", {}))
    g["_note"] = f"微信群样本（{data_date} 导出，自选群手动导出，代表性有限）"
    return g

# ═════════════ 主流程（四层：大盘 / 行业 / 题材 / 个股） ═════════════
def _snap_rows():
    """最近一次股池全A快照（只读）。"""
    import glob as _glob
    for c in reversed(sorted(_glob.glob(os.path.join(ROOT, "cache", "pool_*", "all_a_snapshot.json")))):
        try:
            rows = json.load(open(c, encoding="utf-8"))
            if len(rows) > 3000:
                return rows
        except Exception:
            continue
    return []


_THEME_INV = None


def _theme_inv():
    """题材名 → 成分股（本地 themes.json 反查，不依赖网络）。"""
    global _THEME_INV
    if _THEME_INV is not None:
        return _THEME_INV
    import glob as _glob
    src = None
    for p in sorted(_glob.glob(os.path.join(ROOT, "cache", "pool_*", "themes.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
            if isinstance(d, dict) and d:
                src = d
        except Exception:
            continue
    inv = collections.defaultdict(list)
    for code, tags in (src or {}).items():
        if isinstance(tags, str):
            for t in tags.split():
                inv[t].append(code)
    _THEME_INV = inv
    return inv


def theme_members(name):
    inv = _theme_inv()
    if name in inv:
        return list(inv[name])
    cand = [k for k in inv if name in k or k in name]
    if not cand:
        return []
    cand.sort(key=lambda k: -len(inv[k]))
    log(f"      题材名模糊匹配：{name} → {cand[0]}（{len(inv[cand[0]])} 只）")
    return list(inv[cand[0]])


def industry_members(name):
    return [r["code"] for r in _snap_rows()
            if name and name in str(r.get("industry") or "")]


def top_members(codes, n):
    amt = {r["code"]: (r.get("amount") or 0) for r in _snap_rows()}
    return sorted(codes, key=lambda c: -(amt.get(c) or 0))[:n]


def _guba_cached(code):
    cp = _guba_cache_path(code)
    return os.path.isfile(cp) and time.time() - os.path.getmtime(cp) < 6 * 3600


def rotate_members(members, batch, rank_top=300):
    """轮转取样：在成交额前 rank_top 只里，优先抓未缓存的（多次运行渐进覆盖）。"""
    ranked = top_members(members, rank_top)
    fresh = [c for c in ranked if not _guba_cached(c)]
    covered = sum(1 for c in members if _guba_cached(c))
    return fresh[:batch], covered



def fetch_guba_multi(codes, pages_per=2, max_total=5000):
    """多只成分股股吧聚合（题材/行业层）。"""
    posts, cfh = [], []
    for i, c in enumerate(codes, 1):
        gp, ch = fetch_guba(c, 400, None, pages=pages_per)
        for r in gp:
            r["src"] = c
        posts.extend(gp)
        cfh.extend(ch)
        if i % 5 == 0:
            log(f"      股吧聚合 {i}/{len(codes)} 只，累计 {len(posts)} 帖")
        if len(posts) >= max_total:
            break
        if GUBA_STATE["blocked"]:
            log("      已触发风控，终止多股聚合（改用缓存/稍后重跑）")
            break
        time.sleep(0.2)
    return posts, cfh


def theme_keywords(name):
    """题材/行业名 → 搜索关键词组：去「概念/板块/指数」后缀 + 行业别名扩展。"""
    base = re.sub(r"(概念|板块|指数|主题)$", "", str(name or "")).strip()
    kws = [base]
    kws += list((CFG.get("industry_aliases") or {}).get(base) or [])
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def report_bundle(layer, code=None, industry=None, theme=None):
    """按层级取研报：个股层=个股+行业+产业链；行业/题材层=行业研报+关键词扫全市场。"""
    days = P["report_window_days"]
    parts, notes = [], []
    if layer == "个股" and code:
        rp = fetch_reports(code, days)
        parts.append(("个股研报", rp))
        if industry:
            ind_rp = fetch_reports("", days, qtype="1", industry_filter=industry,
                                   max_pages=P.get("industry_report_pages", 3))
            parts.append(("行业研报", ind_rp))
            parts.append(("产业链研报", fetch_theme_reports(industry, days,
                                                          max_pages=P.get("theme_report_pages", 20))))
    elif layer == "行业" and industry:
        parts.append(("行业研报", fetch_reports("", days, qtype="1", industry_filter=industry,
                                               max_pages=P.get("industry_report_pages", 3))))
        parts.append(("产业链研报", fetch_theme_reports(industry, days,
                                                      max_pages=P.get("theme_report_pages", 20))))
    elif layer == "题材" and theme:
        kws = theme_keywords(theme)
        parts.append(("题材研报", fetch_theme_reports(theme, days, aliases=kws,
                                                    max_pages=P.get("theme_report_pages", 30))))
        log(f"      题材关键词：{'/'.join(kws[:6])}")
    seen, merged = set(), []
    for tag, rows in parts:
        for r in rows:
            k = (r.get("title"), r.get("org"))
            if k in seen:
                continue
            seen.add(k)
            r["scope"] = tag
            merged.append(r)
        notes.append(f"{tag} {len(rows)} 篇")
    return merged, " + ".join(notes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="个股代码（个股层）")
    ap.add_argument("--name", help="个股名称（资讯/标签用）")
    ap.add_argument("--industry", help="行业名（行业层，如 半导体 / 电池）")
    ap.add_argument("--theme", help="题材名（题材层，如 半导体概念 / 机器人概念）")
    ap.add_argument("--members", type=int, help="覆盖成分股取样数（缺省按配置）")
    ap.add_argument("--wechat", choices=["auto", "on", "off"], default="auto",
                    help="微信群组开关：auto=按配置（默认 off）")
    ap.add_argument("--no-attention", action="store_true", help="跳过关注度源")
    ap.add_argument("--no-market", action="store_true", help="跳过市场面")
    a = ap.parse_args()
    if not (a.code or a.theme or a.industry):
        ap.error("需要 --code / --industry / --theme 之一")

    t0 = time.time()
    # ── 层级与目标集合 ──
    if a.code:
        layer, key, label = "个股", a.code, (a.name or a.code)
        members = [a.code]
        ind = guess_industry(a.code)
        kw = a.name
    elif a.industry:
        layer, key, label = "行业", "ind_" + a.industry, a.industry
        members = industry_members(a.industry)
        ind = a.industry
        kw = a.industry
    else:
        layer, key, label = "题材", "theme_" + a.theme, a.theme
        members = theme_members(a.theme)
        ind = None
        kw = a.theme
    targets = (CFG.get("layer_targets") or {}).get(layer) or CFG.get("sample_targets") or {}
    log(f"══ 情绪模型 v{CFG['version']} · [{layer}层] {label}（{key}） ══")
    log(f"   成分股 {len(members)} 只" + (f"｜行业：{ind}" if ind else ""))
    res = {"meta": {"version": CFG["version"], "key": key, "label": label, "layer": layer,
                    "members_total": len(members),
                    "run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": layer}}

    # ── 1. 机构：研报 ──
    log("[1/4] 机构源：研报 …")
    rp, rp_note = report_bundle(layer, code=a.code, industry=ind, theme=a.theme)
    res["机构"] = agg(rp, targets.get("机构"))
    res["机构"]["_records"] = rp[:20]
    res["机构"]["_note"] = f"{layer}层口径：{rp_note}"
    log(f"      {len(rp)} 篇（{rp_note}）→ "
        + " / ".join(f"{c}{res['机构'][c+'%']}%" for c in CATS)
        + f"　目标 ≥{targets.get('机构')}")

    if a.code:
        log("[1b] 机构面板：评级迁移 + 盈利预测共识 + 机构调研 …")
        panel = fetch_institution_panel(a.code, rp)
        if panel:
            res["机构面板"] = panel
            rj = panel["评级面"]
            log(f"      评级面：研报{rj['研报数']}篇 上调{rj['上调']} 下调{rj['下调']} 首次{rj['首次覆盖']}")

    # ── 2. 散户 / 财富号：股吧 ──
    if layer == "个股":
        log("[2/4] 散户源：股吧（本吧帖 + 财富号拆组）…")
        gp, cfh = fetch_guba(a.code, P["guba_max_posts"], a.name,
                             pages=P.get("guba_pages", 12))
    else:
        n_sample = a.members or P.get("member_sample", 30)
        sample, covered = rotate_members(members, n_sample)
        log(f"[2/4] 散户源：股吧聚合（{layer}层成分 {len(members)} 只，已覆盖 {covered} 只，"
            f"本次轮转 {len(sample)} 只 × {P.get('pages_per_member', 1)} 页）…")
        res["meta"]["members_covered_before"] = covered
        gp, cfh = fetch_guba_multi(sample, pages_per=P.get("pages_per_member", 2))
        res["meta"]["members_sampled"] = len(sample)
        res["meta"]["members_covered_after"] = covered + len(sample)
        res["meta"]["members_sample_list"] = sample[:40]
    for r in gp:
        r["cat"], r["kw"] = classify(r["title"])
    for r in cfh:
        r["cat"], r["kw"] = classify(r["title"])
    res["散户"] = agg(gp, targets.get("散户"))
    res["散户"]["_records"] = sorted(gp, key=lambda x: -(x.get("click") or 0))[:15]
    res["散户"]["_all"] = gp
    res["财富号"] = agg(cfh, targets.get("财富号"))
    res["财富号"]["_note"] = "股吧财富号帖＝平台大V/机构号表态"
    res["财富号"]["_records"] = sorted(cfh, key=lambda x: -(x.get("click") or 0))[:15]
    res["财富号"]["_all"] = cfh
    log(f"      本吧 {len(gp)} 帖 → " + " / ".join(f"{c}{res['散户'][c+'%']}%" for c in CATS)
        + f"　目标 ≥{targets.get('散户')}")
    log(f"      财富号 {len(cfh)} 帖 → " + " / ".join(f"{c}{res['财富号'][c+'%']}%" for c in CATS)
        + f"　目标 ≥{targets.get('财富号')}")

    # ── 3. 自媒体：资讯 ──
    log("[3/4] 自媒体源：资讯搜索（代理）…")
    if layer in ("行业", "题材"):
        kw = theme_keywords(label)[0]
    must = [a.name, a.code] if (a.code and a.name) else None
    nw = fetch_news(kw, P["news_max"], P["news_window_days"], must_contain=must,
                    pages=P.get("news_pages", 3))
    for r in nw:
        r["cat"], r["kw"] = classify(r["title"])
    res["自媒体"] = agg(nw, targets.get("自媒体"))
    res["自媒体"]["_records"] = nw[:15]
    res["自媒体"]["_all"] = nw
    log(f"      {len(nw)} 条 → " + " / ".join(f"{c}{res['自媒体'][c+'%']}%" for c in CATS)
        + f"　目标 ≥{targets.get('自媒体')}")

    # ── 语料库 ──
    try:
        cor_d = os.path.join(ROOT, "cache", "sentiment", "corpus", key)
        os.makedirs(cor_d, exist_ok=True)
        corpus = {"meta": {"key": key, "label": label, "layer": layer,
                           "run_at": res["meta"]["run_at"],
                           "members_total": len(members),
                           "members_sampled": res["meta"].get("members_sampled"),
                           "lexicon_version": CFG["version"]},
                  "items": []}
        for grp in ("散户", "财富号", "自媒体"):
            for r in res.get(grp, {}).get("_all", []):
                corpus["items"].append({"group": grp, "text": r.get("title") or r.get("content_head", ""),
                                        "lexicon_cat": r.get("cat"), "src": r.get("src"),
                                        "time": r.get("time") or r.get("date"),
                                        "click": r.get("click")})
        json.dump(corpus, open(os.path.join(
            cor_d, f"{dt.date.today().isoformat()}_{dt.datetime.now().strftime('%H%M')}.json"),
            "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"      语料库 +{len(corpus['items'])} 条")
    except Exception as e:
        log(f"  ⚠ 语料库落盘失败 :: {str(e)[:60]}")

    # ── 微信群（默认关闭）──
    if (a.wechat if a.wechat != "auto" else ("on" if CFG.get("wechat_enabled") else "off")) == "on" and a.code:
        wg = load_wechat(key)
        if wg:
            res["微信群"] = wg
            log(f"[+]  微信群组并入：{wg['样本']} 条")

    # ── 关注度（仅个股层）──
    if a.code and not a.no_attention:
        att = fetch_attention(a.code)
        if att:
            res["热度"] = att
            log(f"[+]  关注度：人气第 {att.get('人气排名')}/{att.get('全市场样本数')}")

    # ── 4. 市场面（各层通用）──
    if not a.no_market:
        mp = fetch_market_panel()
        if mp:
            res["市场面"] = mp
            b, sj = mp["广度"], mp["结构"]
            log(f"[4/4] 市场面（{mp['数据日期']}）：涨跌 {b.get('上涨')}/{b.get('下跌')} "
                f"封板率 {sj.get('封板率%')}% 最高 {sj.get('最高连板')} 板")

    # ── 对比上次 ──
    prev, prev_f = prev_snapshot(key)
    res["变化"] = delta(res, prev)
    res["面板变化"] = panel_delta(prev, res)
    res["市场面变化"] = market_delta(prev, res)
    res["meta"]["prev_file"] = prev_f
    for g, dv in res["变化"].items():
        log(f"      △ {g} 负面 {dv['上次']['负面%']}% → {res[g]['负面%']}%"
            f"（{dv['负面%变化']:+.1f}pct）")
    if not res["变化"]:
        log("      △ 无历史快照，本次为建档基线")

    # ── 落盘 + 报告 ──
    out_d = os.path.join(ROOT, "cache", "sentiment", key)
    os.makedirs(out_d, exist_ok=True)
    today = dt.date.today().isoformat()
    p = os.path.join(out_d, f"{today}_{dt.datetime.now().strftime('%H%M')}.json")
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"\n落盘 {p}  用时 {round(time.time()-t0,1)}s")

    rep_d = os.path.join(ROOT, "reports", today)
    os.makedirs(rep_d, exist_ok=True)
    make_html(res, os.path.join(rep_d, f"sentiment_{key}_{today}.html"))
    print("REPORT_JSON=" + p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
