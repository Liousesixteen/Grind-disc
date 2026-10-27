#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xueqiu_browser.py — 雪球讨论抓取（浏览器方案，绕过阿里云 WAF）

为什么需要它：雪球全站受阿里云 WAF 保护，返回 _waf_ 挑战页，需要执行 JS 才能过。
纯 requests / curl_cffi 都过不去（实测），只能用真实浏览器。

用法：
  .venv/bin/python scripts/xueqiu_browser.py --codes 300750,002475 --count 30
  .venv/bin/python scripts/xueqiu_browser.py --codes 300750 --headed   # 打开窗口观察

产物：
  cache/xq/<code>_<yyyymmdd>.json   {"code":..., "posts":[{title, time}], "source":"xueqiu"}
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))


def load_cookie():
    p = os.path.join(ROOT, "data", "xq_cookie.txt")
    if not os.path.isfile(p):
        return None
    ck = open(p, encoding="utf-8").read().strip()
    return ck if "xq_a_token" in ck else None


def to_cookie_list(cookie_str, domain=".xueqiu.com"):
    out = []
    for item in cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        out.append({"name": k.strip(), "value": v.strip(),
                    "domain": domain, "path": "/"})
    return out


def grab(code, count=30, headed=False, timeout_ms=30000):
    from playwright.sync_api import sync_playwright
    sym = ("SH" if code.startswith(("6", "5", "9")) else "SZ") + code
    cookie_str = load_cookie()
    posts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900}, locale="zh-CN")
        if cookie_str:
            try:
                ctx.add_cookies(to_cookie_list(cookie_str))
            except Exception:
                pass
        page = ctx.new_page()

        # 拦截讨论接口：页面自身会带着 WAF 通过后的 cookie 发起请求
        captured = {}

        def on_resp(resp):
            if "symbol/search/status.json" in resp.url and resp.status == 200:
                try:
                    captured["json"] = resp.json()
                except Exception:
                    pass

        page.on("response", on_resp)
        try:
            page.goto(f"https://xueqiu.com/S/{sym}", wait_until="domcontentloaded",
                      timeout=timeout_ms)
        except Exception as e:
            print(f"  ⚠ 打开页面失败 {str(e)[:60]}")
        # 等 WAF 通过 + 数据加载
        for _ in range(12):
            if captured.get("json"):
                break
            time.sleep(1)
        # 若页面没自动请求，手动触发一次（页面内 fetch 带上真实 cookie）
        if not captured.get("json"):
            try:
                r = page.evaluate(
                    """async (sym) => {
                        const u = `/query/v1/symbol/search/status.json?symbol=${sym}&count=30&source=all&sort=time&page=1`;
                        const r = await fetch(u, {headers: {"Accept": "application/json"}});
                        return await r.text();
                    }""", sym)
                if r and "_waf" not in r:
                    captured["json"] = json.loads(r)
            except Exception:
                pass

        if captured.get("json"):
            for it in (captured["json"].get("list") or []):
                txt = re.sub(r"<.*?>", "", str(it.get("text") or it.get("title") or "")).strip()
                if txt:
                    posts.append({"title": txt[:200],
                                  "time": str(it.get("created_at") or ""),
                                  "user": (it.get("user") or {}).get("screen_name")})
        else:
            # 兜底：解析 DOM 里的讨论文本
            try:
                texts = page.eval_on_selector_all(
                    "div.status-item div.content, div[class*=status] p",
                    "els => els.map(e => e.innerText).filter(t => t && t.length > 4)")
                for t in (texts or [])[:count]:
                    posts.append({"title": re.sub(r"\s+", " ", t)[:200], "time": "", "user": None})
            except Exception:
                pass
        browser.close()
    return posts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="股票代码，逗号分隔")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少只")
    a = ap.parse_args()

    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    if a.limit:
        codes = codes[:a.limit]
    if not load_cookie():
        print("⚠ 未找到 data/xq_cookie.txt（或缺少 xq_a_token）——无 cookie 可能取不到内容")
    d = os.path.join(ROOT, "cache", "xq")
    os.makedirs(d, exist_ok=True)
    today = dt.date.today().strftime("%Y%m%d")
    total = 0
    for code in codes:
        t0 = time.time()
        posts = grab(code, a.count, a.headed)
        if posts:
            json.dump({"code": code, "posts": posts, "source": "xueqiu",
                       "fetched_at": dt.datetime.now().isoformat(timespec="seconds")},
                      open(os.path.join(d, f"{code}_{today}.json"), "w", encoding="utf-8"),
                      ensure_ascii=False)
        total += len(posts)
        print(f"  {code}: {len(posts)} 条（{time.time()-t0:.1f}s）"
              + (f" :: {posts[0]['title'][:40]}" if posts else " ⚠ 无数据"))
    print(f"\n合计 {total} 条 → cache/xq/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
