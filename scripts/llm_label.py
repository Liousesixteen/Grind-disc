#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_label.py — LLM 情感标注器 v1.0

为什么要有它：词表分类器在首轮对比里一致率仅 60.2%、方向反转 6/93、
系统性低估悲观约 20 个百分点（"回购就是骗局"被判正面）。LLM 能读语境。

两种后端（按可用性自动选）：
  1) API 后端：任何 OpenAI 兼容接口（OpenAI / DeepSeek / 通义 / 本地 vLLM…）
     环境变量：LLM_API_KEY（必需）、LLM_BASE_URL（默认 https://api.openai.com/v1）、
              LLM_MODEL（默认 gpt-4o-mini）
     用法：python scripts/llm_label.py --corpus <corpus.json> --limit 200
  2) 标注缓存：`cache/llm_labels/<text_hash>.json` 里已有的标签会被复用（不重复花钱）
     用法：python scripts/llm_label.py --list --corpus <corpus.json>   # 导出待标注清单
           python scripts/llm_label.py --apply <labels.json>            # 回填人工/会话标注

标注结果统一存 `cache/llm_labels/<sha1(text)[:16]>.json`：
  {"text": "...", "cat": "正面|中性|负面", "backend": "api|manual", "ts": "..."}

sentiment_scan.py / sentiment_model.py 分类时自动优先读该缓存，没有才退回词表。
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_DIR = os.path.join(ROOT, "cache", "llm_labels")
PROMPT = """你是A股股吧/研报文本的情感分类器。判断下面对该股票/板块的态度。
只输出一个词：正面 / 中性 / 负面。
判定规则：
- 正面：明确看多、看好、建议买入/加仓、业绩或行业利好背书
- 负面：看空、担忧、抱怨亏损、质疑公司、提示风险、讽刺性唱空
- 中性：客观陈述、数据/新闻转述、提问、无法判断方向
注意：出现"回购""增长""利好"等词不代表正面——要看语义（如"回购就是骗局"是负面）。
文本：{text}
标签："""


def h16(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def label_path(text):
    return os.path.join(LABEL_DIR, f"{h16(text)}.json")


def load_cached(text):
    p = label_path(text)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8")).get("cat")
        except Exception:
            return None
    return None


def save_label(text, cat, backend):
    os.makedirs(LABEL_DIR, exist_ok=True)
    json.dump({"text": text[:200], "cat": cat, "backend": backend,
               "ts": dt.datetime.now().isoformat(timespec="seconds")},
              open(label_path(text), "w", encoding="utf-8"), ensure_ascii=False)


def call_api(text, key, base, model, timeout=30):
    import requests
    r = requests.post(f"{base.rstrip('/')}/chat/completions",
                      headers={"Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"},
                      json={"model": model, "temperature": 0,
                            "max_tokens": 64,
                            "messages": [{"role": "user",
                                          "content": PROMPT.format(text=text[:400])}]},
                      timeout=timeout)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    out = (msg.get("content") or "").strip()
    if not out:                      # 推理模型可能把 token 用在 reasoning 上
        out = (msg.get("reasoning_content") or "").strip()
    for c in ("负面", "正面", "中性"):
        if c in out:
            return c
    return None                      # 空/异常 → 不写标注，避免假标签


def iter_texts(corpus_path):
    d = json.load(open(corpus_path, encoding="utf-8"))
    items = d.get("items") if isinstance(d, dict) else d
    for it in items or []:
        t = (it.get("text") or it.get("title") or "").strip()
        if t:
            yield t



def _read_key():
    k = os.environ.get("LLM_API_KEY")
    if k:
        return k
    p = os.path.join(ROOT, "data", "llm_key.txt")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            s2 = line.strip()
            if s2 and not s2.startswith("#"):
                return s2
    return None


def _read_endpoint():
    def first(p):
        if not os.path.isfile(p):
            return None
        for line in open(p, encoding="utf-8"):
            s2 = line.strip()
            if s2 and not s2.startswith("#"):
                return s2
        return None
    base = os.environ.get("LLM_BASE_URL") or first(os.path.join(ROOT, "data", "llm_base.txt")) or "https://api.openai.com/v1"
    model = os.environ.get("LLM_MODEL") or first(os.path.join(ROOT, "data", "llm_model.txt")) or "gpt-4o-mini"
    return base, model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", help="语料 JSON（corpus/<key>/*.json 或 scan/*.json）")
    ap.add_argument("--limit", type=int, default=200, help="本次最多标注条数")
    ap.add_argument("--list", action="store_true", help="只导出待标注清单（不调用 API）")
    ap.add_argument("--apply", help="回填标注文件（{text: cat} 或 [{text,cat}]）")
    ap.add_argument("--no-cache", action="store_true", help="忽略已有缓存重新标注")
    ap.add_argument("--workers", type=int, default=4, help="API 并发数")
    ap.add_argument("--stats", action="store_true", help="统计已标注数量与分布")
    a = ap.parse_args()

    if a.stats:
        files = [f for f in os.listdir(LABEL_DIR)] if os.path.isdir(LABEL_DIR) else []
        from collections import Counter
        c = Counter()
        for f in files:
            try:
                c[json.load(open(os.path.join(LABEL_DIR, f), encoding="utf-8"))["cat"]] += 1
            except Exception:
                pass
        print(f"已标注 {sum(c.values())} 条：{dict(c)}")
        return 0

    if a.apply:
        d = json.load(open(a.apply, encoding="utf-8"))
        rows = d if isinstance(d, list) else [{"text": k, "cat": v} for k, v in d.items()]
        n = 0
        for r in rows:
            t, cat = (r.get("text") or "").strip(), r.get("cat")
            if t and cat in ("正面", "中性", "负面"):
                save_label(t, cat, "manual")
                n += 1
        print(f"回填 {n} 条 → {LABEL_DIR}")
        return 0

    if not a.corpus:
        ap.error("需要 --corpus 或 --apply/--stats")

    texts = list(dict.fromkeys(iter_texts(a.corpus)))
    todo = []
    for t in texts:
        if not a.no_cache and load_cached(t):
            continue
        todo.append(t)
        if len(todo) >= a.limit:
            break

    if a.list:
        out = os.path.join(ROOT, "cache", "llm_todo.json")
        json.dump([{"text": t} for t in todo], open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"待标注 {len(todo)} 条（总语料 {len(texts)} 条，已缓存 "
              f"{len(texts)-len(todo)} 条）→ {out}")
        return 0

    # key 读取顺序：环境变量 → data/llm_key.txt（一行）→ 配置
    key = os.environ.get("LLM_API_KEY")
    key_file = os.path.join(ROOT, "data", "llm_key.txt")
    if not key and os.path.isfile(key_file):
        for line in open(key_file, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                key = line
                break
    def _first_line(path):
        if not os.path.isfile(path):
            return None
        for line in open(path, encoding="utf-8"):
            s2 = line.strip()
            if s2 and not s2.startswith("#"):
                return s2
        return None

    base = os.environ.get("LLM_BASE_URL") or _first_line(os.path.join(ROOT, "data", "llm_base.txt"))
    model = os.environ.get("LLM_MODEL") or _first_line(os.path.join(ROOT, "data", "llm_model.txt"))
    base = base or "https://api.openai.com/v1"
    model = model or "gpt-4o-mini"
    if not key:
        print("未找到 LLM key。最省事：把 key 写进 data/llm_key.txt（一行），可选 data/llm_base.txt / data/llm_model.txt")
        print("  也可用环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
        print("  2) 用 --list 导出清单，人工/会话标注后用 --apply 回填")
        print(f"（当前待标注 {len(todo)} 条）")
        return 1

    ok = fail = 0
    def work(t):
        try:
            return t, call_api(t, key, base, model)
        except Exception:
            return t, None
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for t, cat in ex.map(work, todo):
            if cat:
                save_label(t, cat, "api")
                ok += 1
            else:
                fail += 1
    print(f"标注完成 {ok} 条（失败 {fail}）→ {LABEL_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
