# -*- coding: utf-8 -*-
"""全市场个股概念全集采集器
================================================================
背景
----
scripts/pool_pipeline.py::src_themes() 现用的东财
`RPT_F10_CORETHEME_CONTENT`(取 KEYWORD=="所属板块")有三个已知缺陷：
  1) secid 规则 bug：startswith("6","9") 先命中，把 920xxx 判成 .SH
     → 北交所全部返回空串（实测 920091.SH rows=0，920091.BJ rows=13）
  2) 串兼并emic地把「行业/地域/概念/风格/技术标签」混成一个字符串，无结构
  3) 概念不全：东财自家就没有「智能物流」这个板块（见 REPORT）

方案（双源，都实测可用）
----------------------
  A. EM   datacenter-web RPT_F10_CORETHEME_BOARDTYPE
     → 结构化板块行：BOARD_TYPE∈{行业,板块,None} / BOARD_LEVEL∈{1,2,3}
       IS_PRECISE / SELECTED_BOARD_REASON（AI 精准概念及其入选依据）
     → 行业 L1/L2/L3、地域板块、概念/风格标签全都能分开
  B. THS  basic.10jqka.com.cn/{code}/concept.html （GBK）
     → 同花顺「所属概念」纯净概念列表 + clid 概念板块 ID
     → 这是东财缺失概念的补充来源（实测 密尔克卫「智能物流」只有 THS 有）

落盘 cache/theme_universe/raw_concepts.json
断点续跑：已有且 src_ok 非空的 key 直接跳过；--force 强制重抓
失败不静默丢弃：写 cache/theme_universe/_failed.json

用法
----
  .venv/bin/python scripts/theme_universe.py                # 全量（双源）
  .venv/bin/python scripts/theme_universe.py --workers 16
  .venv/bin/python scripts/theme_universe.py --only-em      # 只抓东财
  .venv/bin/python scripts/theme_universe.py --codes 603713,920091
  .venv/bin/python scripts/theme_universe.py --verify       # 只打印抽样验证
  .venv/bin/python scripts/theme_universe.py --force        # 忽略断点，重抓
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_SCRIPTS = os.path.join(ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
OUT_DIR = os.path.join(ROOT, "cache", "theme_universe")
OUT_FILE = os.path.join(OUT_DIR, "raw_concepts.json")
FAILED_FILE = os.path.join(OUT_DIR, "_failed.json")
UNIVERSE_FILE = os.path.join(OUT_DIR, "universe.json")
POOL_DIR = os.path.join(ROOT, "cache", "pool_20261023_intraday")
CFG_PATH = os.path.join(ROOT, "scripts", "pool_config.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")

EM_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
THS_URL = "http://basic.10jqka.com.cn/{code}/concept.html"

EM_SRC = "eastmoney_f10_boardtype"
THS_SRC = "10jqka_concept"

_print_lock = threading.Lock()
# 全局轻量限流：两次请求之间的最小间隔（秒），避免把接口打挂
# 注意：同花顺 basic.10jqka.com.cn 有 nginx 层 IP 封禁，实测连跑约 1800 只后
# 开始返回 403 "Nginx forbidden."，且换 UA/Referer 都无效 → THS 间隔必须放慢
_MIN_GAP = {"em": 0.045, "ths": 0.80}
_last_at = {"em": 0.0, "ths": 0.0}
_rate_lock = threading.Lock()
# THS 熔断：连续 N 次非 200 就停止后续 THS 请求，避免空转和加剧封禁
_ths_state = {"consec_err": 0, "banned": False, "lock": threading.Lock()}


def log(m):
    with _print_lock:
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _throttle(src):
    gap = _MIN_GAP.get(src, 0.05)
    with _rate_lock:
        now = time.time()
        wait = gap - (now - _last_at[src])
        _last_at[src] = max(now, now + max(wait, 0.0))
    if wait > 0:
        time.sleep(wait)


def secid(code):
    """修正后的 secid 规则：北交所前缀必须优先于 '9'→SH
    (旧实现 src_themes() 先判 ('6','9')，导致 920xxx 被判成 .SH → 全空)"""
    c = str(code).strip()
    if c.startswith(("92", "4", "8", "43")):
        return c + ".BJ"
    if c.startswith(("6", "9")):
        return c + ".SH"
    return c + ".SZ"


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


_LOCAL = threading.local()


def _sess():
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = _session()
        _LOCAL.s = s
    return s


# ══════════════ 源 A：东财 RPT_F10_CORETHEME_BOARDTYPE ══════════════
def fetch_em(code, tries=4, timeout=20):
    """返回 (name, {"industry": [l1,l2,l3], "region": [...], "concepts": [...],
                     "boards_all": [...], "precise": {...}}) 或 None"""
    params = {"reportName": "RPT_F10_CORETHEME_BOARDTYPE", "columns": "ALL",
              "filter": f'(SECUCODE="{secid(code)}")',
              "pageNumber": "1", "pageSize": "200",
              "source": "WEB", "client": "WEB"}
    data = None
    for i in range(tries):
        try:
            _throttle("em")
            r = _sess().get(EM_API, params=params, timeout=timeout)
            d = r.json()
            if d.get("result") is not None:
                data = d["result"].get("data") or []
                break
            if d.get("message") == "返回数据为空":
                return ("", {"industry": [], "region": [], "concepts": [],
                             "boards_all": [], "precise": {}})
        except Exception:
            pass
        time.sleep(0.6 * (i + 1) + (os.getpid() % 5) * 0.02)
    if data is None:
        return None

    name, boards_all, ind, reg, con, precise = "", {}, {}, [], [], {}
    for x in data:
        bn = (x.get("BOARD_NAME") or "").strip()
        if not bn:
            continue
        if not name:
            name = (x.get("SECURITY_NAME_ABBR") or "").strip()
        bt, lv = x.get("BOARD_TYPE"), x.get("BOARD_LEVEL")
        boards_all[bn] = x.get("BOARD_CODE")
        if bt == "行业":
            try:
                ind[int(lv)] = bn
            except (TypeError, ValueError):
                pass
        elif bt == "板块":
            reg.append(bn)
        else:
            con.append(bn)
            if str(x.get("IS_PRECISE")) == "1":
                precise[bn] = (x.get("SELECTED_BOARD_REASON") or "").strip()
    return (name, {"industry": [ind.get(1, ""), ind.get(2, ""), ind.get(3, "")],
                   "region": reg, "concepts": con,
                   "boards_all": list(boards_all.keys()), "precise": precise})


# ══════════════ 源 B：同花顺 所属概念 ══════════════
_THS_RE = re.compile(r'class="gnName"\s+clid="(\d+)"[^>]*>\s*([^<]+?)\s*</td>')


def fetch_ths(code, tries=2, timeout=20):
    """返回 (names, clids, status)；status ∈ ok / empty / http_error / skipped_banned

    ok         = 200 且解析到概念
    empty      = 200 但页面无概念区块（个股真的没有概念）
    http_error = 非 200（多为 403 IP 封禁）→ 明确失败，绝不当成「没有概念」
    """
    with _ths_state["lock"]:
        if _ths_state["banned"]:
            return None, None, "skipped_banned"
    url = THS_URL.format(code=code)
    for i in range(tries):
        try:
            _throttle("ths")
            r = _sess().get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                with _ths_state["lock"]:
                    _ths_state["consec_err"] = 0
                try:
                    html = r.content.decode("gbk", "ignore")
                except Exception:
                    html = r.content.decode("utf-8", "ignore")
                hits = _THS_RE.findall(html)
                # 页面存在但没有 gnName 区块时仍要确认确实是概念页，而不是反爬页
                names = [n.strip() for _, n in hits]
                return names, [c for c, _ in hits], ("ok" if names else "empty")
            break
        except Exception:
            pass
        time.sleep(0.8 * (i + 1))
    with _ths_state["lock"]:
        _ths_state["consec_err"] += 1
        if _ths_state["consec_err"] >= 40:
            _ths_state["banned"] = True
            log("THS 熔断：连续 40 次非 200，判定 IP 被封，本次任务停止 THS 请求")
    return None, None, "http_error"


# ══════════════ 合并 ══════════════
def merge(code, em, ths, use_em=True, use_ths=True):
    rec = {"name": "", "concepts": [], "concepts_em": [], "concepts_ths": [],
           "concepts_ths_code": [], "industry_sw": [], "industry_src": "",
           "region": [], "boards_em_all": [], "precise_em": {},
           "ths_status": "", "src": "", "src_ok": []}
    if use_em and em is not None:
        nm, e = em
        rec["name"] = nm
        rec["concepts_em"] = e["concepts"]
        rec["industry_sw"] = [x for x in e["industry"] if x]
        rec["industry_src"] = "eastmoney_boardtype_L1L2L3" if rec["industry_sw"] else ""
        rec["region"] = e["region"]
        rec["boards_em_all"] = e["boards_all"]
        rec["precise_em"] = e["precise"]
        rec["src_ok"].append("em")
    if use_ths:
        if ths is None:
            rec["ths_status"] = "not_fetched"        # 明确标注：未取到，不是「没有概念」
        else:
            names, clids, st = ths
            rec["ths_status"] = st
            rec["concepts_ths"] = names or []
            rec["concepts_ths_code"] = clids or []
            # 只有真正拿到 HTTP 200（ok / empty）才算 THS 源成功
            rec["src_ok"].append("ths" if st in ("ok", "empty") else "ths:FAILED")

    merged, seen = [], set()
    for n in rec["concepts_em"] + rec["concepts_ths"]:
        if n and n not in seen:
            seen.add(n)
            merged.append(n)
    rec["concepts"] = merged
    if use_em and use_ths:
        rec["src"] = f"{EM_SRC}+{THS_SRC}"
    else:
        rec["src"] = EM_SRC if use_em else THS_SRC
    rec["ts"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return rec


def load_codes():
    """优先 all_a_snapshot.json ∩ themes.json 并集（实测两者都是 5576）"""
    codes, names = [], {}
    snap = os.path.join(POOL_DIR, "all_a_snapshot.json")
    if os.path.exists(snap):
        try:
            for x in json.load(open(snap, encoding="utf-8")):
                c = str(x.get("code", "")).zfill(6)
                if c:
                    codes.append(c)
                    names[c] = x.get("name") or ""
        except Exception:
            pass
    th = os.path.join(POOL_DIR, "themes.json")
    if os.path.exists(th):
        for c in json.load(open(th, encoding="utf-8")).keys():
            codes.append(str(c).zfill(6))
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out, names


def verify(store, samples):
    print("\n" + "=" * 86)
    print("抽样验证")
    print("=" * 86)
    for c in samples:
        r = store.get(c)
        if not r:
            print(f"  {c}: 缺失")
            continue
        chk = ["智能物流", "机器视觉", "机器人概念", "AI智能体", "人形机器人"]
        has = [k for k in chk if k in r["concepts"]]
        print(f"\n  {c} {r['name']}  概念数={len(r['concepts'])}  "
              f"EM={len(r['concepts_em'])} THS={len(r['concepts_ths'])} src={r['src_ok']}")
        print(f"      industry={r['industry_sw']}  region={r['region']}")
        print(f"      命中={has}")
        print(f"      THS: {' '.join(r['concepts_ths'][:14])}")
        print(f"      EM : {' '.join(r['concepts_em'][:14])}")

    tot = len(store)
    withc = sum(1 for v in store.values() if v.get("concepts"))
    withe = sum(1 for v in store.values() if v.get("concepts_em"))
    witht = sum(1 for v in store.values() if v.get("concepts_ths"))
    ths_okk = sum(1 for v in store.values() if v.get("ths_status") == "ok")
    ths_bad = sum(1 for v in store.values()
                  if v.get("ths_status") not in ("ok", "empty"))
    named = sum(1 for v in store.values() if v.get("name"))
    bj = [c for c in store if c.startswith(("92", "4", "8", "43"))]
    bj_ok = sum(1 for c in bj if store[c].get("concepts"))
    bj_em = sum(1 for c in bj if store[c].get("concepts_em"))
    print("\n" + "-" * 86)
    print(f"  全市场 {tot} 只：有概念 {withc} ({withc/max(tot,1):.1%}) | "
          f"EM有 {withe} ({withe/max(tot,1):.1%}) | THS有 {witht} ({witht/max(tot,1):.1%}) | "
          f"有名称 {named}")
    print(f"  THS 状态：成功 {ths_okk} | 真实无固定概念 "
          f"{sum(1 for v in store.values() if v.get('ths_status')=='empty')} | "
          f"未取到(含被封/熔断) {ths_bad}")
    print(f"  北交所 {len(bj)} 只：有概念 {bj_ok} ({bj_ok/max(len(bj),1):.1%}) | EM有 {bj_em}")
    print("-" * 86)


# ══════════════ 个股大表（universe.json）══════════════════════════════
# 把「概念全集 raw_concepts.json」+「东财板块串 themes.json」+「股池静态池 pool_final」
# +「F10 主营画像 cache/profile」合成一张每票一行的大表，供主归属/归因链路当**唯一概念源**。
#
# ★ 噪声过滤不另起一套：直接复用 pool_config.json::granularity.exclude_words /
#   exclude_suffix / exclude_regex ＋ theme_attr.REGION_TOKENS（34 个省级名）。
#   与 theme_attr.load_tag_map 的 keep() 判据逐字一致，两处永远同口径。
def load_cfg():
    return json.load(open(CFG_PATH, encoding="utf-8"))


def region_tokens():
    """省级地域名集合。唯一来源 theme_attr.REGION_TOKENS（它自己 re-export kwrule 的同名常量）。"""
    try:                       # theme_attr 是归因侧的唯一口径定义处，优先复用它导出的常量
        from theme_attr import REGION_TOKENS as _RT
    except Exception:          # pragma: no cover - 只在 theme_attr 不可导入时兜底
        from kwrule import REGION_TOKENS as _RT
    return set(_RT)


def make_keep(cfg):
    """返回 keep(t) → bool。判据来源唯一：pool_config.granularity + REGION_TOKENS。"""
    _RT = region_tokens()
    G = (cfg or {}).get("granularity", {})
    ex, suf = set(G.get("exclude_words", [])), tuple(G.get("exclude_suffix", []))
    regs = [re.compile(x) for x in G.get("exclude_regex", [])]
    region = set(_RT)

    def keep(t):
        if not t or t in ex:
            return False
        if suf and t.endswith(suf):
            return False
        if t in region:        # 「黑龙江」「内蒙古」不带『板块』后缀，只能靠地域名表滤
            return False
        return not any(r.match(t) for r in regs)
    return keep


_UNI_CACHE = {"mtime": 0.0, "data": None}


def load_universe(path=UNIVERSE_FILE):
    """{code: {…大表记录}}（不含 _meta）。带 mtime 缓存，看板每 15s 刷也不重读盘。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if _UNI_CACHE["data"] is not None and _UNI_CACHE["mtime"] == mt:
        return _UNI_CACHE["data"]
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}
    d.pop("_meta", None)
    _UNI_CACHE["mtime"], _UNI_CACHE["data"] = mt, d
    return d


def concept_map(path=UNIVERSE_FILE):
    """{code: [有效概念]}——主归属/归因候选的覆盖源。只回有概念的票，取不到的交给调用方回退。"""
    return {c: list(v.get("有效概念") or [])
            for c, v in load_universe(path).items() if v.get("有效概念")}


def sw_map(path=UNIVERSE_FILE):
    """{code: [申万一级, 二级, 三级]}——行业名全集（主归属候选剔行业名用）。"""
    return {c: [x for x in (v.get("申万") or []) if x]
            for c, v in load_universe(path).items() if v.get("申万")}


def build_universe(raw_path=OUT_FILE, pool_dir=POOL_DIR, out_path=UNIVERSE_FILE,
                   with_profile=True, verbose=True):
    """合成大表并落盘。字段见模块文档与 REPORT_大表接线.md。"""
    cfg = load_cfg()
    keep = make_keep(cfg)
    _REGION = region_tokens()
    raw = json.load(open(raw_path, encoding="utf-8"))

    themes = {}
    tp = os.path.join(pool_dir, "themes.json")
    if os.path.exists(tp):
        themes = json.load(open(tp, encoding="utf-8"))
    names_from_pool, ind_from_pool = {}, {}
    fp = os.path.join(pool_dir, "pool_final.json")
    if os.path.exists(fp):
        for r in json.load(open(fp, encoding="utf-8")):
            c = str(r.get("code", "")).zfill(6)
            if not c:
                continue
            if r.get("name"):
                names_from_pool[c] = r["name"]
            if r.get("行业标签"):
                ind_from_pool[c] = list(r["行业标签"])

    SP = None
    if with_profile:
        try:
            import stock_profile as SP        # noqa: N806
        except Exception as e:
            if verbose:
                print(f"  ⚠ stock_profile 不可用，主营项名/主业链留空：{e}")
            SP = None

    uni, n_con, n_eff, n_prof, n_ind = {}, 0, 0, 0, 0
    for c, v in raw.items():
        code = str(c).zfill(6)
        sw = [x for x in (v.get("industry_sw") or []) if x]
        if not sw and code in ind_from_pool:                 # 兜底：股池静态行业标签（只有一级）
            sw = list(ind_from_pool[code])
        reg = [x for x in (v.get("region") or []) if x]
        allc = [x for x in (v.get("concepts") or []) if x]
        # themes.json 的板块串里若有大表没抓到的概念，并进来（只补不删，覆盖更全）。
        # ⚠ 东财板块串的前几个词是申万一二三级行业名、中间夹地域板块——这些**不是概念**，
        #   并进「有效概念」会让行业名冒充题材（实测四通股份并出 家居用品/其他家居用品）。
        #   所以补串时先剔掉：申万三级名、地域名（含『…板块』后缀）、大表已给的地域。
        _swset, _regset = set(sw), set(reg)
        for t in (themes.get(code) or "").split():
            if not t or t in allc:
                continue
            if t in _swset or t in _regset or t.endswith("板块") or t in _REGION:
                continue
            allc.append(t)
        eff = [t for t in allc if keep(t)]
        items, mc, ms = [], "", ""
        if SP is not None:
            p = None
            try:
                pth = SP._path(code)
                if os.path.exists(pth):
                    p = json.load(open(pth, encoding="utf-8"))
            except Exception:
                p = None
            if p:
                items = [str(x.get("name") or "") for x in (p.get("items") or [])
                         if x.get("name")]
                try:
                    mc, ms, _ev = SP.main_seg(p, cfg.get("chains") or {}, sw,
                                              industry_to_chain=cfg.get("industry_to_chain"))
                except Exception:
                    mc, ms = "", ""
        if eff:
            n_eff += 1
        if allc:
            n_con += 1
        if items:
            n_prof += 1
        if sw:
            n_ind += 1
        uni[code] = {
            "code": code,
            "name": (v.get("name") or names_from_pool.get(code) or ""),
            "申万一级": sw[0] if len(sw) > 0 else "",
            "申万二级": sw[1] if len(sw) > 1 else "",
            "申万三级": sw[2] if len(sw) > 2 else "",
            "申万": sw,
            "地域": reg,
            "全部概念": allc,
            "有效概念": eff,
            "噪声数": len(allc) - len(eff),
            "主营项名": items,
            "主业链·环节": (f"{mc}·{ms}" if mc and ms else (mc or "")),
            "链": mc, "环节": ms,
            "src": v.get("src", ""),
            "src_ok": v.get("src_ok", []),
            "ts": v.get("ts", ""),
        }
    meta = {
        "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n": len(uni),
        "sources": ["cache/theme_universe/raw_concepts.json (eastmoney F10 BOARDTYPE + 10jqka concept)",
                    "cache/pool_20261023_intraday/themes.json",
                    "cache/pool_20261023_intraday/pool_final.json",
                    "cache/profile/<code>.json (F10 主营构成)"],
        "filter": "pool_config.json::granularity.exclude_words/exclude_suffix/exclude_regex "
                  "+ theme_attr.REGION_TOKENS（与 theme_attr.load_tag_map 同一判据）",
        "coverage": {"有概念": n_con, "有有效概念": n_eff, "有申万行业": n_ind, "有主营画像": n_prof},
        "fields": ["code", "name", "申万一级", "申万二级", "申万三级", "申万", "地域",
                   "全部概念", "有效概念", "噪声数", "主营项名", "主业链·环节", "链", "环节",
                   "src", "src_ok", "ts"],
    }
    out = {"_meta": meta}
    out.update(uni)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    if verbose:
        print(f"大表已写 {out_path}（{len(uni)} 只）")
        print(f"  有概念 {n_con} · 有有效概念 {n_eff} · 有申万 {n_ind} · 有主营画像 {n_prof}")
    return out


def verify_universe(samples, path=UNIVERSE_FILE):
    uni = load_universe(path)
    print("\n" + "=" * 86)
    print(f"大表抽样验证（{len(uni)} 只）")
    print("=" * 86)
    for c in samples:
        r = uni.get(c)
        if not r:
            print(f"  {c}: 不在大表内")
            continue
        print(f"\n  {c} {r['name']}  申万={r['申万']}  地域={r['地域']}")
        print(f"      全部概念 {len(r['全部概念'])} → 有效 {len(r['有效概念'])}（剔噪声 {r['噪声数']}）")
        print(f"      有效：{' '.join(r['有效概念'][:18])}")
        print(f"      主营项名：{r['主营项名']}  主业链·环节：{r['主业链·环节'] or '—'}")
    tot = max(len(uni), 1)
    eff = sum(1 for v in uni.values() if v.get("有效概念"))
    bj = [c for c in uni if c.startswith(("92", "4", "8", "43"))]
    print("\n" + "-" * 86)
    print(f"  全市场 {len(uni)} 只：有有效概念 {eff} ({eff/tot:.1%}) | "
          f"北交所 {len(bj)} 只（有有效概念 {sum(1 for c in bj if uni[c]['有效概念'])}）")
    print("-" * 86)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true",
                    help="合成个股大表 universe.json（不联网，只读本地缓存）")
    ap.add_argument("--verify-uni", action="store_true", help="只做大表抽样验证并退出")
    ap.add_argument("--uni-out", default=UNIVERSE_FILE)
    ap.add_argument("--no-profile", action="store_true", help="大表不读主营画像（更快）")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--codes", default="", help="逗号分隔，指定股票池")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 只（调试）")
    ap.add_argument("--checkpoint", type=int, default=200)
    ap.add_argument("--only-em", action="store_true")
    ap.add_argument("--only-ths", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略断点，全部重抓")
    ap.add_argument("--ths-backfill", action="store_true",
                    help="只补 THS 未拿到的票（ths_status 非 ok/empty），EM 部分留空不请求")
    ap.add_argument("--verify", action="store_true", help="只做抽样验证并退出")
    ap.add_argument("--out", default=OUT_FILE)
    a = ap.parse_args()

    if a.build:
        build_universe(raw_path=a.out, out_path=a.uni_out, with_profile=not a.no_profile)
        verify_universe(["603280", "603028", "603713", "002009", "603838", "920091"],
                        path=a.uni_out)
        return
    if a.verify_uni:
        verify_universe(["603280", "603028", "603713", "002009", "603838", "920091"],
                        path=a.uni_out)
        return

    use_em, use_ths = not a.only_ths, not a.only_em
    if a.ths_backfill:
        use_em = False          # 补齐模式不重复打东财
        use_ths = True
    os.makedirs(OUT_DIR, exist_ok=True)

    store = {}
    if os.path.exists(a.out):
        try:
            store = json.load(open(a.out, encoding="utf-8"))
        except Exception:
            store = {}
        log(f"断点：已载入 {len(store)} 条")

    if a.codes:
        codes = [c.strip().zfill(6) for c in a.codes.split(",") if c.strip()]
    else:
        codes, _ = load_codes()
    if a.limit:
        codes = codes[:a.limit]
    log(f"待处理 {len(codes)} 只  workers={a.workers}  use_em={use_em} use_ths={use_ths}")

    if a.verify:
        verify(store, ["603901", "603713", "920091", "603838", "603878", "002009"])
        return

    if a.ths_backfill:
        # 只挑 THS 未成功（含从未拿到 200）的票
        todo = [c for c in codes
                if store.get(c, {}).get("ths_status") not in ("ok", "empty")]
    else:
        todo = [c for c in codes
                if a.force or c not in store or not store[c].get("concepts")]
    log(f"实际需抓 {len(todo)} 只（跳过已完成 {len(codes) - len(todo)}）")

    lock = threading.Lock()
    failed, done = {}, [0]
    save_lock = threading.Lock()

    def save():
        with save_lock:
            tmp = a.out + ".tmp"
            json.dump(store, open(tmp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            os.replace(tmp, a.out)
            json.dump(failed, open(FAILED_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)

    def work(code):
        rec_parts = {}
        if use_em:
            rec_parts["em"] = fetch_em(code)
        if use_ths:
            rec_parts["ths"] = fetch_ths(code)
        rec = merge(code, rec_parts.get("em"), rec_parts.get("ths"), use_em, use_ths)

        if not use_em and code in store:
            # 补齐模式：保留 EM 侧已抓到的字段，只用新 THS 数据覆盖 THS 相关字段
            base = dict(store[code])
            ths_flag = "ths" if rec["ths_status"] in ("ok", "empty") else "ths:FAILED"
            base["ths_status"] = rec["ths_status"]
            base["concepts_ths"] = rec["concepts_ths"]
            base["concepts_ths_code"] = rec["concepts_ths_code"]
            base["src_ok"] = [x for x in base.get("src_ok", [])
                              if not str(x).startswith("ths")] + [ths_flag]
            merged, seen = [], set()
            for n in base.get("concepts_em", []) + base.get("concepts_ths", []):
                if n and n not in seen:
                    seen.add(n)
                    merged.append(n)
            base["concepts"] = merged
            base["ts"] = rec["ts"]
            base["src"] = EM_SRC + "+" + THS_SRC if "em" in base["src_ok"] else THS_SRC
            rec = base

        ok = bool(rec["concepts"]) or bool(rec["industry_sw"]) or bool(rec.get("name"))
        with lock:
            if ok:
                store[code] = rec
            failed_thsdetail = rec_parts.get("ths")
            _failed_note = {"em_ok": rec_parts.get("em") is not None,
                            "ths_ok": failed_thsdetail is not None,
                            "ths_status": rec.get("ths_status", "")}
            if not ok:
                failed[code] = _failed_note
            done[0] += 1
            if done[0] % a.checkpoint == 0:
                save()
                log(f"  进度 {done[0]}/{len(todo)}  成功入库 {len(store)}  失败 {len(failed)}")

    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(work, todo))
    except KeyboardInterrupt:
        log("中断：先落盘")
    save()
    log(f"完成 {done[0]} 只，用时 {time.time()-t0:.0f}s，入库 {len(store)}，失败 {len(failed)}")
    verify(store, ["603901", "603713", "920091", "603838", "603878", "002009"])


if __name__ == "__main__":
    main()
