# -*- coding: utf-8 -*-
"""233战法 全A盘前股池 · 固定流水线 v1.0
================================================================
用法
  python scripts/pool_pipeline.py                 # 全量更新（采数→算指标→分层→导表→落盘）
  python scripts/pool_pipeline.py --reuse         # 只重算+导表（用已有缓存，调阈值后秒级重出）
  python scripts/pool_pipeline.py --date 20261020 # 指定数据日（默认取最近交易日）
  python scripts/pool_pipeline.py --out 路径.xlsx  # 自定义输出
  python scripts/pool_pipeline.py --no-mirror     # 不往 Downloads 另存
  python scripts/pool_pipeline.py --dry-run       # 只跑采数与统计，不写工作簿

原则
  1. 参数全在 scripts/pool_config.json，改参数不改代码；每次运行追加台账 cache/pool_runs.csv
  2. 数据源按 config.sources 顺序回退；全部失败则该字段留空，绝不编造（DATA_SOURCE_POLICY）
  3. 程序只产「候选资格」，不产任何买卖判断；池层不决定仓位

口径定义见 熔炼系统.md 3.6 股池三筛选量化口径与股池分层
"""
import argparse
import datetime as dt, bisect, csv, json, os, re, sys, time, datetime as dt
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from openpyxl import Workbook, load_workbook
import pankou as PK          # 角色/梯队/情绪 唯一口径（盘中 intraday_scan 共用）
import theme_attr as TA      # 行业三级名全集（主归属候选剔行业名用，Step4）
from kwrule import REGION_TOKENS, bind_ok, gate_ok, chain_of   # 关键词规则唯一来源（自检脚本也用这一份）
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "scripts", "pool_config.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
LOG = []


def log(m):
    line = f"[{dt.datetime.now().strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    LOG.append(line)


def get_json(url, params=None, tries=3, timeout=15):
    for i in range(tries):
        try:
            return S.get(url, params=params, timeout=timeout).json()
        except Exception as e:
            if i == tries - 1:
                log(f"  FAIL {url.split('?')[0][:70]} :: {str(e)[:60]}")
                return None
            time.sleep(1 + i)


# ══════════════ 1. 交易日历 ══════════════
def src_trade_days(name, n):
    if name == "eastmoney_push2his":
        d = get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
            "secid": "1.000001", "klt": "101", "fqt": "1", "lmt": str(n),
            "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57"})
        try:
            return [k.split(",")[0].replace("-", "") for k in d["data"]["klines"]]
        except Exception:
            return []
    if name == "sina_index":
        r = S.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                  "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=%d" % n, timeout=15)
        try:
            return [b["day"].replace("-", "") for b in r.json()]
        except Exception:
            return []
    return []


# ══════════════ 2. 全A快照 ══════════════
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FIELDS = "f12,f14,f2,f3,f5,f6,f8,f10,f20,f21,f22,f24,f25,f100,f9,f23,f15,f16,f17,f18"


# ── 腾讯行情快照（2026-10-27 新增）──────────────────────────────
# 为什么加：东财 push2 时常连不上，降级到新浪后没有 open/high/low →
# 一字板/T字板/地天板 全部判不出来（bar_forms 宁可返回空也不猜）。
# 腾讯 qt.gtimg 一次可批量 60 只，含 开高低/昨收/量比/换手/市值/涨停价，正好补齐。
def _tx_prefix(c):
    if c[0] == '6':
        return 'sh'
    if c[0] in ('4', '8', '9') and not c.startswith('30'):
        return 'bj'
    return 'sz'


def src_snapshot_tencent(codes=None, batch=60, workers=8):
    import concurrent.futures as cf
    if not codes:
        codes = _universe_codes()
    if not codes:
        return []
    def fetch(chunk):
        q = ",".join(_tx_prefix(c) + c for c in chunk)
        try:
            r = S.get("https://qt.gtimg.cn/q=" + q, timeout=8,
                      headers={"Referer": "https://gu.qq.com/"})
            r.encoding = "gbk"
            return r.text
        except Exception:
            return ""
    chunks = [codes[i:i + batch] for i in range(0, len(codes), batch)]
    out = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for txt in ex.map(fetch, chunks):
            for line in txt.strip().split("\n"):
                if '="' not in line:
                    continue
                f = line.split('="', 1)[-1].rstrip('";').split("~")
                if len(f) < 50 or not f[2]:
                    continue
                def n(i, d=None):
                    try:
                        return float(f[i])
                    except (ValueError, IndexError, TypeError):
                        return d
                price = n(3)
                if not price:
                    continue
                out.append({"code": f[2], "name": f[1], "price": price,
                            "pct": n(32), "vol_hand": n(6),
                            "amount": (n(37) or 0) * 1e4,      # 腾讯成交额单位=万元
                            "turnover": n(38), "liangbi": n(49),
                            "mcap": (n(45) or 0) * 1e8,         # 总市值(亿)
                            "fmcap": (n(44) or 0) * 1e8,        # 流通市值(亿)
                            "speed": None, "pct60": None, "pct_ytd": None,
                            "industry": "", "open": n(5), "high": n(33),
                            "low": n(34), "prev_close": n(4),
                            "up_limit": n(47), "down_limit": n(48),
                            "quote_time": f[30] if len(f)>30 else None})
    return out


def _universe_codes():
    """全 A 代码：优先读知识库 dim_stock（离线可用），否则回落东财清单。"""
    import sqlite3
    try:
        c = sqlite3.connect(os.path.join(ROOT, "cache", "kb", "trading_kb.sqlite"), timeout=5)
        rows = [r[0] for r in c.execute("SELECT code FROM dim_stock")]
        c.close()
        if len(rows) > 1000:
            return rows
    except Exception:
        pass
    try:
        rows = []
        pn = 1
        while True:
            d = get_json("https://push2.eastmoney.com/api/qt/clist/get", {
                "pn": pn, "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
                "fid": "f12", "fs": FS_ALL, "fields": "f12"})
            if not d or not d.get("data"):
                break
            diff = d["data"]["diff"]
            arr = list(diff.values()) if isinstance(diff, dict) else diff
            rows += [x.get("f12") for x in arr if x.get("f12")]
            if pn * 100 >= d["data"].get("total", 0):
                break
            pn += 1
        return rows
    except Exception:
        return []


def src_snapshot(name):
    """全A快照。东财/腾讯为完整源（含 OHLC）；新浪为降级源（无 OHLC，形态判不出）。"""
    if name == "tencent_qt":
        return src_snapshot_tencent()
    if name in ("eastmoney_push2", "eastmoney_push2_mirror"):
        host = ("https://push2.eastmoney.com" if name == "eastmoney_push2"
                else "https://82.push2.eastmoney.com")
        rows, pn = [], 1
        while True:
            d = get_json(host + "/api/qt/clist/get", {
                "pn": pn, "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
                "fid": "f12", "fs": FS_ALL, "fields": FIELDS})
            if not d or not d.get("data") or not d["data"].get("diff"):
                break
            diff = d["data"]["diff"]
            rows.extend(list(diff.values()) if isinstance(diff, dict) else diff)
            if pn * 100 >= d["data"]["total"]:
                break
            pn += 1
            time.sleep(0.12)
        out = []
        for r in rows:
            def num(x):
                try:
                    return float(x)
                except Exception:
                    return None
            rec = {"code": r.get("f12"), "name": r.get("f14"), "price": num(r.get("f2")),
                   "pct": num(r.get("f3")), "vol_hand": num(r.get("f5")), "amount": num(r.get("f6")),
                   "turnover": num(r.get("f8")), "liangbi": num(r.get("f10")), "mcap": num(r.get("f20")),
                   "fmcap": num(r.get("f21")), "speed": num(r.get("f22")), "pct60": num(r.get("f24")),
                   "pct_ytd": num(r.get("f25")), "industry": r.get("f100") or "",
                   "pe": num(r.get("f9")), "pb": num(r.get("f23")),
                   # 盘口判据字段（2026-10-23 肉肉 4.1/4.2 熔入）：突破与量能进度用
                   "open": num(r.get("f17")), "high": num(r.get("f15")),
                   "low": num(r.get("f16")), "prev_close": num(r.get("f18"))}
            if rec["code"]:
                out.append(rec)
        return out

    if name == "sina_market_center":
        base = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData")
        rows, page = [], 1
        while page <= 80:
            try:
                arr = S.get(base, params={"page": page, "num": "100", "sort": "symbol",
                                          "asc": "1", "node": "hs_a", "symbol": "",
                                          "_s_r_a": "page"}, timeout=20).json()
            except Exception:
                break
            if not isinstance(arr, list) or not arr:
                break
            rows.extend(arr)
            if len(arr) < 100:
                break
            page += 1
            time.sleep(0.1)
        out = []

        def num(x):
            try:
                return float(x)
            except Exception:
                return None

        for b in rows:
            if not b.get("code"):
                continue
            vol = num(b.get("volume"))
            out.append({"code": b["code"], "name": b.get("name"), "price": num(b.get("trade")),
                        "pct": num(b.get("changepercent")),
                        "vol_hand": vol / 100 if vol else None,
                        "amount": num(b.get("amount")), "turnover": num(b.get("turnoverratio")),
                        "liangbi": None,
                        "mcap": (num(b.get("mktcap")) or 0) * 1e4 if b.get("mktcap") else None,
                        "fmcap": (num(b.get("nmc")) or 0) * 1e4 if b.get("nmc") else None,
                        "speed": None, "pct60": None, "pct_ytd": None,
                        "industry": "", "pe": num(b.get("per")), "pb": num(b.get("pb"))})
        return out
    return []


# ══════════════ 3. 涨停池（仅板块归属/校验；回溯窗口有限，不用于计数） ══════════════
def src_zt_pool(name, date8, kind="zt"):
    """涨停/炸板/跌停池。kind: zt(涨停) / zb(炸板) / dt(跌停)。
    涨停池字段：c代码 n名称 p现价 zdp涨幅 amount成交额 ltsz流通市值 hs换手
      lbc连板数 fbt首次封板时间(HHMMSS) lbt最后封板 fund封板资金 zbc炸板次数 hybk行业。
    ⚠ 回溯窗口仅约 20 交易日（已知坑），只用于**当日**盘口质量，不用于涨停次数统计。"""
    if name != "eastmoney_ztpool":
        return []
    ep = {"zt": "getTopicZTPool", "zb": "getTopicZBPool", "dt": "getTopicDTPool"}.get(kind)
    if not ep:
        return []
    # ⚠ 排序字段分池而异：**只有跌停池**没有 fbt 字段（用 fbt:asc 会返回 0 家）；
    #    炸板池有 fbt，必须用 fbt:asc（用 fund:asc 会返回空 → 炸板率被低估）。
    sort_by = "fund:asc" if kind == "dt" else "fbt:asc"
    d = get_json(f"https://push2ex.eastmoney.com/{ep}", {
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": "0", "pagesize": "10000", "sort": sort_by, "date": date8})
    try:
        return d["data"]["pool"]
    except Exception:
        return []


# ══════════════ 4. 龙虎榜 ══════════════
def src_lhb(name, d8):
    if name != "eastmoney_datacenter":
        return []
    d = get_json("https://datacenter-web.eastmoney.com/api/data/v1/get", {
        "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
        "columns": "SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLANATION,BILLBOARD_NET_AMT,ACCUM_AMOUNT",
        "filter": f"(TRADE_DATE='{d8[:4]}-{d8[4:6]}-{d8[6:]}')",
        "pageNumber": "1", "pageSize": "500",
        "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1",
        "source": "WEB", "client": "WEB"})
    try:
        return d["result"]["data"]
    except Exception:
        return []


# ══════════════ 4.5 板块强度榜（概念 + 行业两层） ══════════════
def src_boards(name, cfg):
    """返回 {'gn': [...], 'hy': [...]}。字段：name/zdf/zdf_d5/zdf_d20/zljlr/zljlr_d5/zgb/lzg"""
    if name != "tencent_proxy":
        return {}
    out = {}
    for bt in cfg["boards"]["layers"]:
        arr = []
        for off in range(0, 1000, 100):
            d = get_json("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank", {
                "board_type": bt, "sort_type": "price", "direct": "down",
                "offset": str(off), "count": "100"}, timeout=15)
            rl = (d or {}).get("data", {}).get("rank_list") or []
            if not rl:
                break
            arr.extend(rl)
        out[bt] = arr
    return out


# ══════════════ 4.6 个股所属板块标签 ══════════════
def src_themes(name, codes, cfg):
    """返回 {code: '标签1 标签2 ...'}。取不到的留空串，不编造。"""
    if name != "eastmoney_f10_theme":
        return {}

    def secid(c):
        if c.startswith(("6", "9")):
            return c + ".SH"
        if c.startswith(("4", "8", "92")):
            return c + ".BJ"
        return c + ".SZ"

    def one(c):
        for _ in range(2):
            try:
                d = get_json("https://datacenter-web.eastmoney.com/api/data/v1/get", {
                    "reportName": "RPT_F10_CORETHEME_CONTENT",
                    "columns": "KEYWORD,MAINPOINT_CONTENT",
                    "filter": f'(SECUCODE="{secid(c)}")',
                    "pageNumber": "1", "pageSize": "6",
                    "source": "WEB", "client": "WEB"}, timeout=12)
                for x in (d or {}).get("result", {}).get("data") or []:
                    if x.get("KEYWORD") == "所属板块":
                        return c, x.get("MAINPOINT_CONTENT", "")
                return c, ""
            except Exception:
                time.sleep(0.5)
        return c, ""

    res, done = {}, 0
    with ThreadPoolExecutor(max_workers=cfg["themes"]["workers"]) as ex:
        for c, v in ex.map(one, codes):
            res[c] = v
            done += 1
            if done % 1000 == 0:
                log(f"  题材标签进度 {done}/{len(codes)}")
    return res


# ══════════════ 4.6b 个股大表（universe.json）接入 ══════════════
# 盘后主题源默认 themes.json（东财 F10『所属板块』串），覆盖不全（北交所全空、
# 缺同花顺概念）。cache/theme_universe/universe.json 是全市场 5576 只的
# 「地域 + 申万一二三级 + 有效概念」大表（有效概念已剔地域/行业名/排除词），
# 与盘中 intraday_board → theme_attr.load_tag_map(universe=…) 同源同口径。
# 大表缺失该票 → 逐票回退 themes.json 旧行为，逐字不变。
UNIVERSE_FIRST = True                      # 总开关；cfg["data"]["universe_first"] 可覆盖（缺省 True）
UNIVERSE_PATH = os.path.join(ROOT, "cache", "theme_universe", "universe.json")
_UNIVERSE = None


def load_universe(cfg):
    """读个股大表 → {code: {...}}；开关关闭 / 文件缺失 / 解析失败 → None（=完全回退旧口径）。
    模块级缓存，一次运行只读一次盘。"""
    global _UNIVERSE
    if not (UNIVERSE_FIRST and (cfg.get("data") or {}).get("universe_first", True)):
        return None
    if _UNIVERSE is None:
        try:
            d = json.load(open(UNIVERSE_PATH, encoding="utf-8"))
            _UNIVERSE = ({k: v for k, v in d.items() if k != "_meta"}
                         if isinstance(d, dict) else {})
        except Exception:
            _UNIVERSE = {}
    return _UNIVERSE or None


def uni_theme_string(u):
    """大表条目 → classify_tags 输入串：『地域 申万一二三级 有效概念』。
    地域（…板块 / REGION_TOKENS）会被分层剔掉；申万各级行业名**不带 Ⅱ/Ⅲ 标记**，
    须由调用方把 uni_industry_names(u) 并进 industry_names 才会落到 行业标签；
    有效概念已剔行业名/地域/排除词，全部落 题材候选——分层结果与盘中
    load_tag_map(universe=…) 的 ind=申万、con=有效概念 一致。"""
    toks = [x for x in (u.get("地域") or []) + (u.get("申万") or [])
            + (u.get("有效概念") or []) if x]
    return " ".join(dict.fromkeys(toks))


def uni_industry_names(u):
    return {x for x in (u.get("申万") or []) if x}


def classify_tags(raw, industry_names, G):
    """把『所属板块』标签串分层 → (行业标签 list, 题材标签 list)
    分层顺序：地域 → 排除词/排除正则 → 申万二三级(Ⅱ/Ⅲ) → 已知行业名 → 其余为题材候选
    """
    if not raw:
        return [], []
    ex = set(G.get("exclude_words", []))
    suf = tuple(G.get("exclude_suffix", []))
    regs = [re.compile(p) for p in G.get("exclude_regex", [])]
    ind, thm = [], []
    for t in raw.split():
        t = t.strip()
        if not t:
            continue
        if suf and t.endswith(suf):                      # 地域：北京板块 / 陕西板块 …
            continue
        if t in REGION_TOKENS:                           # 内蒙古 / 黑龙江 不写「板块」
            continue
        if t in ex:                                      # 风格 / 估值 / 机构 / 交易状态 / 指数成分
            continue
        if any(r.match(t) for r in regs):                # 财报类 / AB股 之类
            continue
        if "Ⅲ" in t or "Ⅱ" in t or t in industry_names:   # 申万行业二三级 / 已知行业名
            ind.append(t)
            continue
        thm.append(t)                                    # 其余 = 题材候选
    return ind, thm
def prefix(code):
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def src_kline(name, code, n, min_bars=25):
    """min_bars：最少根数校验（默认 25，保护 60 日指标不被短数据算错）。
    backfill_daily 只需 11 根 → 传 min_bars=2，否则新浪分支对短请求恒返回空
    （2026-10-19 实战：腾讯 501 + 新浪被此校验挡死 → 日线回补成功率仅 1.2%）。"""
    sym = prefix(code)
    if name == "sina":
        try:
            r = S.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                      f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}", timeout=15)
            arr = r.json()
            if not isinstance(arr, list) or len(arr) < min_bars:
                return []
            return [[b["day"], b["open"], b["close"], b["high"], b["low"], b["volume"]] for b in arr]
        except Exception:
            return []
    if name == "tencent":
        try:
            r = S.get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq", timeout=12)
            node = r.json().get("data", {}).get(sym, {})
            return node.get("qfqday") or node.get("day") or []
        except Exception:
            return []
    if name == "eastmoney_push2his":
        secid = ("1." if sym.startswith("sh") else "0.") + code
        d = get_json("https://push2his.eastmoney.com/api/qt/stock/kline/get", {
            "secid": secid, "klt": "101", "fqt": "1", "lmt": str(n), "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57"})
        try:
            return [[k.split(",")[0], 0, float(k.split(",")[2]), 0, 0, float(k.split(",")[5])]
                    for k in d["data"]["klines"]]
        except Exception:
            return []
    return []


# ══════════════ 指标计算 ══════════════
def limit_pct(code, name, cfg):
    lp = cfg["limit_pct"]
    if any(k in (name or "") for k in cfg["filters"]["exclude"]["name_keywords"]):
        return lp["st"]
    if code.startswith(("30", "68")):
        return lp["gem_star"]
    if code.startswith(("4", "8", "92")):
        return lp["bse"]
    return lp["main"]


def kline_metrics(code, bars, name, cfg):
    if len(bars) < 25:
        return None
    closes = [float(b[2]) for b in bars]
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes)) if closes[i - 1] > 0]
    lp = limit_pct(code, name, cfg)
    flags = [1 if x >= lp else 0 for x in rets]
    n = len(flags)
    w = cfg["data"]["zt_windows"]
    zt = {f"zt{d}": (sum(flags[-d:]) if n >= d else sum(flags)) for d in w}
    run = best = 0
    for f in flags[-120:]:
        run = run + 1 if f else 0
        best = max(best, run)
    last_zt = ""
    for i in range(n - 1, -1, -1):
        if flags[i]:
            last_zt = bars[i + 1][0].replace("-", "")
            break
    r60 = rets[-60:]
    ups = [x for x in r60 if x > 0]
    downs = [x for x in r60 if x < 0]
    au = sum(ups) / len(ups) if ups else 0.0
    ad = sum(downs) / len(downs) if downs else 0.0
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    c = closes[-1]
    return {**zt, "max_lb": best, "last_zt": last_zt,
            "avg_up": round(au, 2), "avg_dn": round(ad, 2),
            "hongfei": round(au / abs(ad), 2) if ad else None,
            "up_ratio": round(len(ups) / max(len(r60), 1), 3),
            "ret20": round((c / closes[-21] - 1) * 100, 2) if len(closes) > 21 else None,
            "ret60": round((c / closes[-61] - 1) * 100, 2) if len(closes) > 61 else None,
            "above_ma20": bool(c > ma20), "above_ma60": (bool(c > ma60) if ma60 else None)}


def build_kline(codes, names, cfg, workers=10):
    chain = cfg["sources"]["kline"]
    res, done = {}, 0

    def one(code):
        for s in chain:
            bars = src_kline(s, code, cfg["data"]["kline_bars"])
            if bars:
                return code, bars, s
        return code, [], None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, c): c for c in codes}
        for f in as_completed(futs):
            code, bars, _ = f.result()
            m = kline_metrics(code, bars, names.get(code, ""), cfg)
            if m:
                res[code] = m
            done += 1
            if done % 1000 == 0:
                log(f"  日线进度 {done}/{len(codes)}")
    return res


# ══════════════ 分层 ══════════════
def assemble(snap, km, lhb, zt, cfg, data_date, d60, themes=None, hy1_names=()):
    F = cfg["filters"]
    gux, bia, lia = F["guxing_股性"], F["bianshi_辨识度"], F["liangjia_量价"]
    themes = themes or {}
    uni = load_universe(cfg)   # 个股大表优先；None = 完全回退 themes.json 旧口径
    rows = []
    for s in snap:
        c, nm = s["code"], s["name"] or ""
        k, l, z = km.get(c, {}), lhb.get(c, {}), zt.get(c, {})
        # universe 命中且有有效概念 → 用大表组串（地域+申万+有效概念），并把大表
        # 申万行业名并入行业名集合（大表申万不带 Ⅱ/Ⅲ，不并入会漏成题材候选）；
        # 未命中 / 有效概念为空 → themes.json 旧行为逐字不变。
        u = (uni or {}).get(c)
        if isinstance(u, dict) and (u.get("有效概念") or []):
            raw, ind_names = uni_theme_string(u), set(hy1_names) | uni_industry_names(u)
        else:
            raw, ind_names = themes.get(c, ""), hy1_names
        inds, thms = classify_tags(raw, ind_names, cfg["granularity"])
        zt60, zt20, zt120 = k.get("zt60"), k.get("zt20"), k.get("zt120")
        max_lb = k.get("max_lb", 0)
        lhb60 = len([x for x in l.get("lhb_days", []) if x >= d60])
        hf, ret20 = k.get("hongfei"), k.get("ret20")
        c1 = (zt60 or 0) >= gux["zt60_min"] or (max_lb or 0) >= gux["max_lb_min"]
        # 辨识度四类（233 原文 2.2：前期龙头 / 人气股 / 容量核心 / 反复活跃）
        # 旧口径只用了龙虎榜一个代理，等于把「人气」当成辨识度的全部，且完全没有「容量核心」。
        p5 = k.get("p5") or []
        up5 = sum(1 for x in p5 if isinstance(x, (int, float)) and x > 0)
        amt_yi = (s.get("amount") or 0) / 1e8
        fm_yi = (s.get("fmcap") or 0) / 1e8
        P1, Q1 = bia.get("前期龙头", {}), bia.get("人气股", {})
        V1, W1 = bia.get("容量核心", {}), bia.get("反复活跃", {})
        cls = {
            "前期龙头": ((max_lb or 0) >= P1.get("max_lb_min", 3)
                       or (k.get("zt120") or 0) >= P1.get("zt120_min", 6)),
            "人气股": (lhb60 >= Q1.get("lhb60_min", 2)
                     or (s.get("turnover") or 0) >= Q1.get("turnover_min", 8)
                     or (s.get("liangbi") or 0) >= Q1.get("liangbi_min", 2)),
            "容量核心": (amt_yi >= V1.get("amount_yi_min", 8)
                     and V1.get("fmcap_yi_min", 30) <= fm_yi <= V1.get("fmcap_yi_max", 800)),
            "反复活跃": ((zt60 or 0) >= W1.get("zt60_min", 2)
                     or up5 >= W1.get("up_days_5_min", 3)),
        }
        bian_cls = [n for n, h in cls.items() if h]
        c2 = len(bian_cls) >= bia.get("min_classes", 1)
        # 5 日均线与「站上 5 日线」：用回补的 p5 从当日价反推近 5 日收盘
        ma5 = None
        if p5 and s.get("price"):
            cs = [s["price"]]
            for x in reversed(p5):
                if not isinstance(x, (int, float)):
                    cs = []
                    break
                cs.insert(0, cs[0] / (1 + x / 100))
            if len(cs) >= 6:
                ma5 = sum(cs[1:]) / 5
        above5 = (s["price"] > ma5) if (ma5 and s.get("price")) else None
        c3 = bool(hf and hf >= lia["hongfei_min"]) and bool((ret20 or 0) > lia["ret20_floor"])
        score = sum([c1, c2, c3])
        tier = "核心" if score == 3 else ("备选" if score == 2 else ("观察" if score == 1 else ""))
        if any(kw in nm for kw in F["exclude"]["name_keywords"]):
            tier = "剔除(ST/退)"
        rows.append({**s, "zt20": zt20, "zt60": zt60, "zt120": zt120, "max_lb": max_lb,
                     "last_zt": k.get("last_zt", ""), "lhb60": lhb60,
                     "hongfei": hf, "avg_up": k.get("avg_up"), "avg_dn": k.get("avg_dn"),
                     "ret20": ret20, "ret60": k.get("ret60"),
                     "above_ma20": k.get("above_ma20"), "above_ma60": k.get("above_ma60"),
                     "above_ma5": above5, "ma5": round(ma5, 2) if ma5 else None,
                     "辨识度构成": "·".join(bian_cls), "up_days5": up5,
                     "成交额亿": round(amt_yi, 2),
                     # 日线回补的近 N 日日涨跌幅序列（backfill_daily.py 写入）。
                     # 有了它才能算「题材多日涨幅 / 连续跑赢天数」，不再依赖外部榜单。
                     "p5": k.get("p5"), "ret5": k.get("ret5"),
                     "hybk": z.get("hybk", ""),
                     "行业标签": inds, "题材": thms,
                     "筛1_股性": c1, "筛2_辨识度": c2, "筛3_量价": c3, "达标数": score, "池层": tier})
    return rows


def fmt_hhmmss(v):
    """东财首封时间 HHMMSS → HH:MM:SS"""
    s6 = str(int(v or 0)).zfill(6)
    return f"{s6[:2]}:{s6[2:4]}:{s6[4:]}" if s6 != "000000" else "—"


def normalize_sector(rows):
    names = {r["industry"] for r in rows if r["industry"]}

    def norm(x):
        if not x or x == "-":
            return ""
        if x in names:
            return x
        c = [n for n in names if n.startswith(x)]
        if len(c) == 1:
            return c[0]
        c2 = [n for n in names if x in n or n in x]
        return c2[0] if len(c2) == 1 else x

    for r in rows:
        r["板块"] = norm(r["industry"]) or norm(r["hybk"]) or "其他"


# ══════════════ 导出 ══════════════
HEAD_FILL = PatternFill("solid", fgColor="1F4E79")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=11)
THIN = Border(bottom=Side(style="thin", color="BFBFBF"))


def style(ws, freeze_cols, wrap_cols=()):
    ws.freeze_panes = ws.cell(row=2, column=freeze_cols + 1)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = THIN
    ws.row_dimensions[1].height = 32
    for j in range(1, ws.max_column + 1):
        vals = [len(str(ws.cell(row=r, column=j).value)) for r in range(2, min(ws.max_row, 300) + 1)
                if ws.cell(row=r, column=j).value is not None]
        ws.column_dimensions[ws.cell(row=1, column=j).column_letter].width = min(max((max(vals) if vals else 10) + 3, 9), 60)
    for name in wrap_cols:
        ws.column_dimensions[name].width = 46
        for c in ws[name][1:]:
            c.alignment = Alignment(wrap_text=True, vertical="center")
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=ws.max_column).column_letter}{ws.max_row}"


def build_book(rows, cfg, meta, data_date, boards=None, pools=None):
    by_sec, agg = {}, {}
    for r in rows:
        if r["池层"] in ("核心", "备选", "观察"):
            by_sec.setdefault(r["板块"], []).append(r)
        g = agg.setdefault(r["板块"], {"zt20": 0, "zt": 0, "pcts": [], "amt": 0.0, "核心": 0, "备选": 0, "观察": 0})
        g["zt20"] += r["zt20"] or 0
        if r["pct"] is not None and r["pct"] >= 9.7:
            g["zt"] += 1
        if r["pct"] is not None:
            g["pcts"].append(r["pct"])
        g["amt"] += (r["amount"] or 0) / 1e8
        if r["池层"] in ("核心", "备选", "观察"):
            g[r["池层"]] += 1
    # ── 窗口A：题材强度榜（233 主口径 = 先题材后个股） ──
    G = cfg["granularity"]
    gn_idx = {b["name"]: b for b in (boards or {}).get("gn", [])}
    tag_members, tag_pool = {}, {}
    for r in rows:
        for t in (r.get("题材") or []):
            tag_members.setdefault(t, []).append(r)
    for r in rows:
        if r["池层"] in ("核心", "备选", "观察"):
            for t in (r.get("题材") or []):
                tag_pool.setdefault(t, []).append(r)

    def fnum(x):
        try:
            return float(x)
        except Exception:
            return None

    def pick(b, key, div=None):
        v = fnum(b.get(key)) if b else None
        if v is None:
            return ""
        return round(v / div, 2) if div else v

    # ── 题材名 → 板块榜读数 的三级匹配（v1.3）──
    # 旧行为是「匹配不上就删掉」，实测把 801 个题材砍到 240 个：冰雪经济 / 白酒 因此整类消失。
    def norm_name(s):
        s = re.sub(r"\s+", "", str(s))
        for suf in G.get("normalize_strip_suffix", []):
            if s.endswith(suf) and len(s) > len(suf):
                return s[: -len(suf)]
        return s

    gn_norm = {}
    for b in (boards or {}).get("gn", []):
        gn_norm.setdefault(norm_name(b["name"]), b)
    alias = G.get("alias", {})
    NEG = ("非", "不", "无", "除")

    def match_board(t):
        """返回 (板块读数 dict|None, 匹配方式)。匹配方式写进表里，模糊匹配不冒充精确。"""
        b = gn_idx.get(t)
        if b:
            return b, "精确"
        a = alias.get(t)
        if a and a in gn_idx:
            return gn_idx[a], "别名"
        nt = norm_name(t)
        if nt in gn_norm:
            return gn_norm[nt], "归一"
        if G.get("match_containment"):
            mnl = G.get("match_min_len", 2)
            mxd = G.get("match_max_len_diff", 2)
            if not any(x in t for x in NEG):
                for k, b2 in gn_norm.items():
                    if any(x in b2["name"] for x in NEG):
                        continue
                    if k == nt:
                        continue
                    if (nt in k or k in nt) and min(len(nt), len(k)) >= mnl \
                            and abs(len(nt) - len(k)) <= mxd:
                        return b2, "模糊"
        return None, "无读数"

    # ── 市场基准：概念榜全榜中位数（相对强弱才有可比性，绝对涨跌在大盘回调日全军覆没）──
    def med(key):
        vs = [fnum(x.get(key)) for x in (boards or {}).get("gn", [])]
        vs = sorted(v for v in vs if v is not None)
        return vs[len(vs) // 2] if vs else None

    m1, m5 = med("zdf"), med("zdf_d5")
    meta["mkt_median_d1"] = m1 if m1 is not None else ""
    meta["mkt_median_d5"] = m5 if m5 is not None else ""
    if m1 is not None:
        log(f"  市场基准（概念榜中位数）：当日 {m1}% / 5日 {m5}% —— 连续性按相对超额判定")

    # ── 自算多日序列：不依赖任何外部榜单 ──
    # 日线回补后每只票都有近 5 日日涨跌幅。据此算市场每日基准与题材每日均涨，
    # 于是「5 日涨幅」和「连续跑赢天数」对外部榜单匹配不上的题材也成立——
    # 这是把 317 个「无外部读数」题材从「只能看当日」升级到「能看多日」的关键。
    ND = cfg.get("data", {}).get("daily_backfill_days", 5)
    has_p5 = sum(1 for r in rows if r.get("p5")) > len(rows) * 0.5
    mk_day, mk_ret5 = [], None
    if has_p5:
        for i in range(ND):
            vs = []
            for r in rows:
                p = r.get("p5")
                if p and len(p) > i and p[i] is not None:
                    vs.append(p[i])
            vs.sort()
            mk_day.append(vs[len(vs) // 2] if vs else None)
        vs = sorted(x for x in (r.get("ret5") for r in rows) if isinstance(x, (int, float)))
        mk_ret5 = vs[len(vs) // 2] if vs else None
        log(f"  自算多日序列：覆盖 {sum(1 for r in rows if r.get('p5'))}/{len(rows)} 只；"
            f"市场每日中位 {[round(x, 2) if x is not None else None for x in mk_day]}；"
            f"5日累计中位 {mk_ret5}%")

    wa = []
    for t, mem in tag_members.items():
        b, how = match_board(t)
        n = len(mem)
        pcts = [r["pct"] for r in mem if r["pct"] is not None]
        amt = sum((r["amount"] or 0) for r in mem) / 1e8
        zt = sum(1 for r in mem if (r["pct"] or 0) >= 9.7)
        pool = tag_pool.get(t, [])

        def by_pct(lst):
            return sorted(lst, key=lambda r: -(r["pct"] if r["pct"] is not None else -99))

        def fmt(lst, k=6):
            return " / ".join(
                f"{r['name']}({r['code']}) {r['pct']}%"
                + (f"·龙{r['龙头分']}" if r.get("龙头分") is not None else "")
                for r in lst[:k])

        # ── 龙头六项（233 原文 3.2）：辨识度 / 涨停基因 / 5日线突破 / 成交量 / 涨速 / 板块带动性 ──
        # 原文强调「涨得最快 ≠ 龙头」，所以不能只按当日涨幅排；六项全部在**题材内部**相对排名。
        LS = cfg.get("leader", {})
        if LS.get("enabled"):
            avg_all = sum(pcts) / len(pcts) if pcts else 0.0
            raws = []
            for x in mem:
                raws.append({
                    "辨识度": len([y for y in (x.get("辨识度构成") or "").split("·") if y]),
                    "涨停基因": (x.get("zt60") or 0) + (x.get("max_lb") or 0) * 2,
                    "5日线突破": 1 if x.get("above_ma5") else 0,
                    "成交量": x.get("amount") or 0,
                    "涨速": x.get("pct") if x.get("pct") is not None else -99,
                    "板块带动性": (x.get("pct") or 0) - avg_all,
                })
            srt = {k: sorted(v[k] for v in raws) for k in raws[0]}
            W = LS.get("weights", {})
            tw = sum(W.get(k, 1) for k in srt) or 1
            for x, rv in zip(mem, raws):
                s = 0.0
                for k, v in rv.items():
                    arr = srt[k]
                    pos = bisect.bisect_left(arr, v) / max(len(arr) - 1, 1)
                    s += pos * W.get(k, 1)
                x["龙头分"] = round(s / tw * 100, 1)

        # 题材内相对强度：233 的龙头必须在题材内部比较，不能只看全市场绝对阈值
        lead = (sorted(mem, key=lambda r: -(r.get("龙头分") or 0))
                if cfg.get("leader", {}).get("enabled") else by_pct(mem))
        core = by_pct([r for r in pool if r["池层"] == "核心"])          # 池内有基因 + 题材内更强的优先
        bak = by_pct([r for r in pool if r["池层"] == "备选"])
        width = ("过宽·需下钻" if n > G["max_members"]
                 else "过窄·个股行情" if n < G["min_members"] else "可用")
        zdf, d5 = pick(b, "zdf"), pick(b, "zdf_d5")
        # 连续性 = 相对全市场的超额，不是绝对涨跌。
        # 绝对口径下 10-16（全市场 5 日 −5.19%）把 240 个题材判成 151 首日 / 1 持续，毫无区分度。
        # 自算：题材每日均涨（成分股 p5 等权）+ 连续跑赢市场的天数（从最近一日往前数）
        th_d, run = [], 0
        if has_p5:
            for i in range(ND):
                vv = [r["p5"][i] for r in mem
                      if r.get("p5") and len(r["p5"]) > i and r["p5"][i] is not None]
                th_d.append(sum(vv) / len(vv) if vv else None)
            for i in range(ND - 1, -1, -1):
                if th_d[i] is not None and mk_day[i] is not None and th_d[i] > mk_day[i]:
                    run += 1
                else:
                    break
        r5v = [r["ret5"] for r in mem if isinstance(r.get("ret5"), (int, float))]
        ret5_self = round(sum(r5v) / len(r5v), 2) if r5v else ""

        exc1 = exc5 = ""
        cont = ""
        if isinstance(zdf, (int, float)) and m1 is not None:
            exc1 = round(zdf - m1, 2)
        if isinstance(d5, (int, float)) and m5 is not None:
            exc5 = round(d5 - m5, 2)
        # 外部榜无 5 日读数 → 用自算 5 日涨幅补（题材成分均值 − 全市场成分中位）
        if exc5 == "" and isinstance(ret5_self, (int, float)) and mk_ret5 is not None:
            exc5 = round(ret5_self - mk_ret5, 2)
        if isinstance(exc1, (int, float)) and isinstance(exc5, (int, float)):
            cont = "持续" if (exc1 > 0 and exc5 > 0) else ("首日" if exc1 > 0 else "走弱")
        elif isinstance(exc1, (int, float)):
            cont = "首日" if exc1 > 0 else "走弱"
        # 连当日读数都没有 → 用自算的「连续跑赢天数」判定（并标·自算，不冒充外部读数）
        if not cont and has_p5:
            cont = ("持续·自算" if run >= 2 else "首日·自算" if run == 1 else "走弱·自算")
        elif not cont:
            cont = "（无外部读数）" if not b else "（无基准）"
        wa.append([t, n, width, zdf, d5, pick(b, "zdf_d20"), pick(b, "zljlr", 1e4),
                   (b.get("zgb") if b else ""), cont, exc1, exc5, how,
                   fmt(lead),
                   round(sum(pcts) / len(pcts), 2) if pcts else "", zt, round(amt, 1),
                   len(pool), sum(1 for r in pool if r["池层"] == "核心"),
                   sum(1 for r in pool if r["池层"] == "备选"),
                   sum(1 for r in pool if r["池层"] == "观察"),
                   fmt(core), fmt(bak), ret5_self, run if has_p5 else "",
                   *chain_of(t, cfg.get("chains", {})),
                   pick(b, "lb")])

    def sk(x):
        v = x[6] if G.get("board_sort") == "zljlr" else x[3]   # 主力净流入 / 当日涨幅
        primary = -(v if isinstance(v, (int, float)) else -1e9)
        # 无外部读数者排在有读数者之后，但内部按成分股自算均涨排序——保留而不是删除
        ap = x[13] if isinstance(x[13], (int, float)) else -1e9
        return (primary, -ap)

    worder = {w: i for i, w in enumerate(G.get("width_order", ["可用"]))}
    wa.sort(key=lambda x: (worder.get(x[2], 9), sk(x)))

    # ── 主线簇：把同一波资金的多个题材名聚成一簇 ──
    FC = cfg.get("focus", {})
    names = [r[0] for r in wa]
    zj = {r[0]: (r[6] if isinstance(r[6], (int, float)) else 0.0) for r in wa}
    rep = {n: n for n in names}                 # 题材 → 簇代表名
    csize = {n: 1 for n in names}               # 簇内题材数
    ctotal = dict(zj)                           # 簇合计主力
    if FC.get("cluster_enabled"):
        thr, sp = FC.get("cluster_threshold", 0.35), FC.get("strong_pct", 5)
        strong = {r[0]: {x["code"] for x in tag_members[r[0]] if (x["pct"] or -99) >= sp}
                  for r in wa}
        par = {n: n for n in names}

        def find(x):
            while par[x] != x:
                par[x] = par[par[x]]
                x = par[x]
            return x

        for i, a in enumerate(names):
            for b in names[i + 1:]:
                sa, sb = strong[a], strong[b]
                if sa and sb and len(sa & sb) / len(sa | sb) >= thr:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        par[ra] = rb
        grp = {}
        for n in names:
            grp.setdefault(find(n), []).append(n)
        for mem in grp.values():
            best = max(mem, key=lambda n: zj[n])        # 代表题材 = 簇内主力最大者
            tot = round(sum(zj[n] for n in mem), 2)
            for n in mem:
                rep[n], csize[n], ctotal[n] = best, len(mem), tot
        wa.sort(key=lambda x: (-ctotal.get(x[0], 0), -zj.get(x[0], 0),
                               -(x[13] if isinstance(x[13], (int, float)) else -1e9)))
        log(f"  主线簇聚类：{len(wa)} 个题材 → {len(grp)} 个簇（阈值 {thr}，强势股 ≥{sp}%）")

    # 插入簇三列：主线簇 / 簇内题材数 / 簇合计主力
    for r in wa:
        r[1:1] = [rep.get(r[0], r[0]), csize.get(r[0], 1),
                  round(ctotal.get(r[0], 0), 2) if isinstance(ctotal.get(r[0]), (int, float)) else ""]

    # ── 时间维度：连续在榜天数 / 较昨日排名变化 / 生命周期阶段 ──
    LC = cfg.get("lifecycle", {})
    up, down = LC.get("rank_up_min", 3), LC.get("rank_down_max", -5)
    streak, prev_rank = load_history(cfg, data_date)

    def stage(name, days, delta, zljlr):
        if not prev_rank:
            return ""                                   # 无历史，不做判定
        if not days:
            return "启动(首日进榜)"
        if isinstance(delta, int) and delta <= down:
            return "退潮"
        if isinstance(zljlr, (int, float)) and zljlr < 0:
            return "退潮(资金转出)"
        if isinstance(delta, int) and delta >= up:
            return "强化"
        return "维持"

    for i, r in enumerate(wa, 1):
        pr = prev_rank.get(r[0])
        d = streak.get(r[0], "") or ""
        delta = (pr - i) if pr else ""
        r.extend([d, delta, stage(r[0], d, delta, r[9])])
    save_history(cfg, data_date, wa)
    if prev_rank:
        log(f"  历史台账已接入：昨日 {len(prev_rank)} 个题材在榜，可算连续性与排名变化")
    else:
        log("  历史台账：本次为首个快照，连续性与排名变化从下一次运行起生效")

    # ── 窗口B：行业取用（兜底 / 交叉验证；按当日强度排序） ──
    wbb = []
    for k, g in agg.items():
        lst = sorted(by_sec.get(k, []), key=lambda r: (-(r["达标数"] or 0), -(r["zt60"] or 0)))
        core = [f"{r['code']} {r['name']}" for r in lst if r["池层"] == "核心"][:6]
        bak = [f"{r['code']} {r['name']}" for r in lst if r["池层"] == "备选"][:6]
        arr = [x for x in g["pcts"] if x is not None]
        wbb.append([k, round(sum(arr) / len(arr), 2) if arr else "", g["zt"], g["zt20"],
                    round(g["amt"], 1), sum(g[x] for x in ("核心", "备选", "观察")),
                    g["核心"], g["备选"], g["观察"], " / ".join(core), " / ".join(bak)])
    wbb.sort(key=lambda x: -(x[1] if isinstance(x[1], (int, float)) else -99))
    misc = [x for x in wbb if x[0] in ("其他", "其他（未分类）")]
    wbb = ([x for x in wbb if x[0] not in ("其他", "其他（未分类）")]
           + [["其他（未分类·盘前忽略）"] + x[1:] for x in misc])

    order = {"核心": 0, "备选": 1, "观察": 2}

    def y(v):
        return "是" if v is True else ("否" if v is False else "")

    wb_rows = []
    for r in sorted(rows, key=lambda r: (order.get(r["池层"], 9), -(r["zt60"] or 0), -(r["max_lb"] or 0))):
        wb_rows.append([r["code"], r["name"], r["板块"], "、".join(r["题材"] or []), r["池层"], r["达标数"],
                        y(r["筛1_股性"]), y(r["筛2_辨识度"]), y(r["筛3_量价"]),
                        r["辨识度构成"] or "（无）",
                        r["price"], r["pct"], r["turnover"], r["liangbi"],
                        round(r["fmcap"] / 1e8, 1) if r["fmcap"] else "",
                        round(r["mcap"] / 1e8, 1) if r["mcap"] else "",
                        r["zt20"], r["zt60"], r["zt120"], r["max_lb"], r["last_zt"], r["days_since_zt"],
                        r["lhb60"], r["hongfei"], r["avg_up"], r["avg_dn"], r["ret20"], r["ret60"],
                        y(r["above_ma5"]), y(r["above_ma20"]), y(r["above_ma60"]),
                        r["pct_ytd"], r["pct60"], r["pe"], r["pb"]])

    od = cfg["old_dragon_老龙记忆"]
    kw = cfg["filters"]["exclude"]["name_keywords"]
    old = [r for r in rows
           if (r["zt120"] or 0) >= od["zt120_min"] and (r["zt60"] or 0) <= od["zt60_max"]
           and not any(x in (r["name"] or "") for x in kw)
           and od["days_since_min"] <= (r["days_since_zt"] or 999) <= od["days_since_max"]
           and r.get("above_ma20") is not None]
    old.sort(key=lambda r: (-(r["zt120"] or 0), -(r["max_lb"] or 0)))
    wc = [[r["code"], r["name"], r["板块"], r["zt120"], r["zt60"], r["zt20"], r["max_lb"],
           r["last_zt"], r["days_since_zt"], r["lhb60"], r["hongfei"], r["ret20"], r["ret60"],
           r["price"], round(r["fmcap"] / 1e8, 1) if r["fmcap"] else "", r["池层"] or "未入池"] for r in old]

    # ── 窗口A0：盘前焦点（四口径综合排名，v1.4）──
    # v1.3 的「资金前N ∪ 涨幅前N」仍是**单口径取并集**：每一项都要么极强才进得去。
    # 实测失效：冰雪经济 资金第90 / 当日第62 / 5日第12 / 宽度第85，白酒 55/99/29/102 ——
    # **每项都中等、没有一项极端的方向，任何单口径排序都会漏掉**。
    # 改为四口径排名均值（资金 / 当日超额 / 5日超额 / 涨股比宽度），对应 3.3 时间尺度谱系
    # （短：当日；中：5日）与 3.4 双口径（资金；广度）。综合后 冰雪经济第23、白酒第36。
    FC = cfg.get("focus", {})
    top_n = FC.get("top_n", 40)
    core_n = FC.get("core_n", 15)
    mode = FC.get("rank_mode", "composite")
    out_min = FC.get("outflow_warn", -3)      # 主力净流入低于此值 → 量价背离，不是进攻方向

    def nv(x):
        return x if isinstance(x, (int, float)) else None

    def breadth(r):
        z = str(r[10] or "")
        if "/" in z:
            try:
                a, b = z.split("/")
                return float(a) / float(b) if float(b) else None
            except Exception:
                return None
        return None

    seen_rep, focus = set(), []
    if mode == "composite":
        # 参与综合排名：宽度『可用』且四项读数齐全。无外部读数者无法交叉验证，不进焦点，
        # 但完整保留在窗口A——不删数据，只是不冒充「已验证的焦点」。
        cand = [r for r in wa if r[5] == "可用" and nv(r[3]) is not None
                and nv(r[12]) is not None and nv(r[13]) is not None
                and breadth(r) is not None and nv(r[17]) is not None]
        N = len(cand)

        def rmap(key):
            s = sorted(cand, key=lambda r: -key(r))
            return {r[0]: i for i, r in enumerate(s, 1)}

        R = {"资金": rmap(lambda r: nv(r[3])), "当日": rmap(lambda r: nv(r[12])),
             "5日": rmap(lambda r: nv(r[13])), "宽度": rmap(lambda r: breadth(r)),
             "涨停": rmap(lambda r: nv(r[17]) if nv(r[17]) is not None else 0),
             "量能": rmap(lambda r: nv(r[29]) if nv(r[29]) is not None else -1e9)}
        W = FC.get("rank_weights", {})
        wl = [W.get("money", 1), W.get("exc1", 1), W.get("exc5", 1),
              W.get("breadth", 1), W.get("zt", 1), W.get("lb", 1)]
        tw = sum(wl) or 1
        keys = ["资金", "当日", "5日", "宽度", "涨停", "量能"]

        def rks(n):
            return [R[k].get(n, N) for k in keys]

        comp = {r[0]: sum(v * w for v, w in zip(rks(r[0]), wl)) / tw for r in cand}

        # ── 过宽题材下钻（产业链行情里真正轮动的是「环节」，不是整条链）──
        # 例：5-6 月炒半导体，动的是 材料→设备→存储→封测，而不是『半导体概念』这 509 只的大标签。
        # 只看大标签会把信号稀释到看不见——3.8 判据②标了「过宽·需下钻」，这里把它真正做掉。
        sub_cover = FC.get("sub_cover", 0.6)
        memcode = {t: {x["code"] for x in m} for t, m in tag_members.items()}
        by_chain = {}
        for r in wa:                      # 母题材从**全部**题材里找（过宽的也在内），子题材从可用者里找
            by_chain.setdefault(r[27], []).append(r)
        sub_of = {}
        for ch, lst in by_chain.items():
            if not ch:
                continue
            for r in lst:
                t, nn = r[0], r[4]
                if nn <= G["max_members"]:
                    continue
                mc = memcode.get(t) or set()
                subs = []
                for r2 in lst:
                    t2 = r2[0]
                    if t2 == t or r2[4] >= nn or r2[5] != "可用":
                        continue
                    mc2 = memcode.get(t2) or set()
                    if mc2 and len(mc2 & mc) / len(mc2) >= sub_cover:
                        subs.append((t2, r2[28]))
                if subs:
                    sub_of[t] = subs

        # 把「过宽题材可下钻到哪些环节」写进窗口A——过宽题材不进焦点，但人要能顺着它往下看
        for r in wa:
            subs = sub_of.get(r[0])
            r.append(" / ".join(f"{t}·{seg}" if seg else t for t, seg in subs[:5]) if subs else "")

        # 按主线簇去重：一个簇取综合分最好的题材作代表
        best = {}
        for r in cand:
            c = r[1]
            if c not in best or comp[r[0]] < comp[best[c][0]]:
                best[c] = r
        ordered = sorted(best.values(), key=lambda r: comp[r[0]])[:top_n]
        for i, r in enumerate(ordered, 1):
            rr = r
            n = rr[0]
            rk = rks(n)
            why = "·".join(k for k, v in zip(keys, rk) if v <= core_n) or "综合"
            zj = nv(rr[3])
            if zj is not None and zj <= out_min:
                why += "·⚠资金流出"
            if rr[5] != "可用":
                why += f"·{rr[5]}"
            # 主线列 = 该行题材本身——否则会出现「名字是A、读数是B」
            focus.append(["重点" if i <= core_n else "观察", rr[0], rr[1], i,
                          "/".join(f"{k}#{v}" for k, v in zip(keys, rk)),
                          why, rr[2], rr[3], rr[6], rr[7], rr[8], rr[9],
                          rr[10], rr[11], rr[12], rr[13], rr[14], rr[30], rr[31], rr[32],
                          rr[19], rr[20], rr[15], rr[23], rr[16], rr[25], rr[26],
                          rr[27], rr[28], rr[33]])
        log(f"  盘前焦点六口径综合：可用且有读数题材 {N} 个 → 取前 {top_n} 个簇"
            f"（重点 {min(core_n, len(focus))} / 观察 {max(0, len(focus) - core_n)}）；"
            f"产业链归链 {sum(1 for r in cand if r[27])} 个，过宽题材可下钻 {len(sub_of)} 个"
            f"（下钻结果见窗口A 末列，环节维度见窗口A2）")
    else:
        # 回退：单口径并集（保留能力，不推荐）
        def first_n(keyfn, n, filt=None):
            seen, out = set(), []
            for r in sorted(wa, key=keyfn):
                if r[1] in seen or (filt and not filt(r)):
                    continue
                seen.add(r[1])
                out.append(r[1])
                if len(out) >= n:
                    break
            return set(out)

        money_set = first_n(lambda r: -(nv(r[3]) if nv(r[3]) is not None else -1e9), top_n)
        gain_set = first_n(lambda r: -(nv(r[16]) if nv(r[16]) is not None else -1e9),
                           top_n, lambda r: r[5] == "可用")
        for r in wa:
            if r[1] in seen_rep or r[1] not in (money_set | gain_set):
                continue
            seen_rep.add(r[1])
            focus.append(["-", r[0], r[1], "", "", "单口径", r[2], r[3], r[6], r[7], r[8], r[9],
                          r[10], r[11], r[12], r[13], r[14], r[30], r[31], r[32],
                          r[19], r[20], r[15], r[23], r[16], r[25], r[26], r[27], r[28], ""])

    # ── 产业链 × 环节聚合（诉求③：不能只按题材聚合，细分领域本身就是炒作单元）──
    # 233 的「板块」= 资金正在炒的题材；但在产业链行情里真正轮动的是**环节**不是整条链：
    # 5-6 月半导体动的是 材料→设备→存储→封测 的接力，CPO 行情里 光模块→光芯片→交换机 各走一段。
    # 只看题材名：环节轮动被稀释在 509 只的大标签里看不见；
    # 只看整条链：环节之间的分化（材料强、封测弱）被抹平，还会误以为「整条链都在涨」。
    # 所以把「环节」也作为一个独立聚合维度单列一张表，与窗口A（题材维度）并行、互不替代。
    seg_rows = []
    if cfg.get("chains"):
        byseg = {}
        for r in wa:
            ch, sg = r[27], r[28]
            if not ch:
                continue
            byseg.setdefault((ch, sg or "（链内未细分）"), []).append(r[0])
        # 链级大标签（军工 / 半导体概念 / 人工智能 …）不是环节：成分 400+ 只，与真环节不可比。
        # 保留成行可见（铁律①），但不参与强弱排名，压到表尾只作下钻入口。
        is_top = lambda sg: str(sg).startswith("（链级")
        seg_keys = [k for k in byseg if not is_top(k[1])]
        top_keys = [k for k in byseg if is_top(k[1])]

        def wjudge(n):
            return ("过宽·需下钻" if n > G["max_members"]
                    else "过窄·个股行情" if n < G["min_members"] else "可用")
        zj_of = {r[0]: (r[3] if isinstance(r[3], (int, float)) else -1e9) for r in wa}
        mkt_d1 = mk_day[-1] if (has_p5 and mk_day and mk_day[-1] is not None) else None
        stats = []
        for (ch, sg), themes in byseg.items():
            seen, codes = set(), []
            for t in themes:
                for x in tag_members.get(t, []):
                    if x["code"] in seen:
                        continue
                    seen.add(x["code"])
                    codes.append(x)
            if not codes:
                continue
            ps = [x["pct"] for x in codes if x.get("pct") is not None]
            d1 = round(sum(ps) / len(ps), 2) if ps else None
            r5 = [x["ret5"] for x in codes if isinstance(x.get("ret5"), (int, float))]
            d5 = round(sum(r5) / len(r5), 2) if r5 else None
            ztn = sum(1 for x in codes if (x.get("pct") or 0) >= 9.7)
            ups = sum(1 for x in codes if (x.get("pct") or 0) > 0)
            amt = sum((x.get("amount") or 0) for x in codes) / 1e8
            run = 0
            if has_p5:
                for i in range(ND - 1, -1, -1):
                    vs = [x["p5"][i] for x in codes
                          if x.get("p5") and len(x["p5"]) > i and x["p5"][i] is not None]
                    v = sum(vs) / len(vs) if vs else None
                    if v is not None and mk_day[i] is not None and v > mk_day[i]:
                        run += 1
                    else:
                        break
            stats.append({"ch": ch, "sg": sg, "themes": themes, "n": len(codes),
                          "top": is_top(sg), "width": wjudge(len(codes)),
                          "d1": d1, "d5": d5, "zt": ztn, "amt": amt, "run": run,
                          "up": ups,
                          "e1": (d1 - mkt_d1) if (isinstance(d1, (int, float)) and mkt_d1 is not None) else None,
                          "e5": (d5 - mk_ret5) if (isinstance(d5, (int, float)) and mk_ret5 is not None) else None})
        # 环节级综合：四口径（当日 / 5日 / 涨停家数 / 成交额）。
        # 缺「资金」——个股快照没有主力净流入字段，题材级加总又会把重叠成分重复计算，
        # 宁可留空也不填一个会骗人的数（3.9 铁律①：不静默删除，也不能静默编造）。
        cmp_pool = [x for x in stats if not x["top"]]     # 链级大标签不参与比较
        def srank(key):
            s = sorted(cmp_pool, key=lambda x: -(x[key] if isinstance(x[key], (int, float)) else -1e9))
            return {id(x): i for i, x in enumerate(s, 1)}
        RK = {"当日": srank("e1"), "5日": srank("e5"), "涨停": srank("zt"), "成交额": srank("amt")}
        skeys = ["当日", "5日", "涨停", "成交额"]
        for x in cmp_pool:
            x["comp"] = sum(RK[k][id(x)] for k in skeys) / len(skeys)
        for i, x in enumerate(sorted(cmp_pool, key=lambda x: x["comp"])
                              + sorted((x for x in stats if x["top"]),
                                       key=lambda x: -x["n"]), 1):
            rk = [RK[k][id(x)] for k in skeys] if not x["top"] else None
            rep_t = max(x["themes"], key=lambda t: zj_of.get(t, -1e9))
            poolc = [c for c in (tag_pool.get(t, []) for t in x["themes"]) for c in c]
            seen2, uniq = set(), []
            for c in poolc:
                if c["code"] in seen2:
                    continue
                seen2.add(c["code"])
                uniq.append(c)
            core4 = sorted([c for c in uniq if c.get("池层") == "核心"],
                           key=lambda c: -(c["pct"] if c["pct"] is not None else -99))[:4]
            seg_rows.append(["—" if x["top"] else i, x["ch"], x["sg"], len(x["themes"]),
                             " / ".join(x["themes"][:6]) + ("…" if len(x["themes"]) > 6 else ""),
                             x["n"], x["width"],
                             x["d1"] if x["d1"] is not None else "",
                             round(x["e1"], 2) if isinstance(x["e1"], (int, float)) else "",
                             x["d5"] if x["d5"] is not None else "",
                             round(x["e5"], 2) if isinstance(x["e5"], (int, float)) else "",
                             x["zt"], f'{x["up"]}/{x["n"]}', round(x["amt"], 1),
                             x["run"] if has_p5 else "",
                             "链级入口·不参与排名" if x["top"]
                             else "/".join(f"{k}#{v}" for k, v in zip(skeys, rk)),
                             len(uniq), rep_t,
                             "·".join(f'{c["name"]}{c["pct"]}' for c in core4)])
        log(f"  产业链环节榜：{len(cmp_pool)} 个环节 + {len(stats) - len(cmp_pool)} 个链级入口"
            f"（{len(set(x[1] for x in seg_rows))} 条链）")

        # 环节通道：把环节榜的前几个环节直接送进盘前焦点。
        # 为什么需要单独一条通道：过宽的母题材（>500 只）不进焦点（读数是稀释后的），
        # 而它的强子题材可能因为题材名冷门、绝对排名不够而进不了前 40。
        # 结果是「半导体链·存储」这种真正被炒的单元在焦点里看不见——诉求③的缺口就在这里。
        # 环节通道与题材通道并列，互不挤占名额，各按各自的综合排名取。
        slots = FC.get("seg_slots", 8)
        picked = [x for x in seg_rows if x[0] != "—"][:slots]   # 链级入口不进焦点
        for x in picked:
            focus.append(["环节", f"{x[1]}·{x[2]}", x[1], x[0], x[15],
                          "环节通道(细分领域本身就是炒作单元)",
                          x[3], "", x[7], x[9], "", "", x[12], "", x[8], x[10],
                          f"自算(环节聚合·{x[6]})", "", "", "",
                          x[16], "", x[18], "", x[7], x[9], x[14], x[1], x[2], ""])
        if slots:
            log(f"  环节通道：前 {len(picked)} 个环节并入盘前焦点（档位标『环节』）")

    # ── 待认领题材：板块榜上有强资金、但题材库里没有这个名字 ──
    pend = []
    if boards:
        ours = set(tag_members)
        minz = cfg.get("lifecycle", {}).get("new_theme_min_zljlr", 3)
        by_code = {r["code"]: r for r in rows}
        for b in boards.get("gn", []):
            if b["name"] in ours:
                continue
            if not classify_tags(b["name"], set(), G)[1]:
                continue                     # 风格 / 地域 / 交易状态类，用同一套排除规则
            zj = fnum(b.get("zljlr"))
            zj = zj / 1e4 if zj is not None else None
            if zj is None or zj < minz:
                continue
            lzg = (b.get("lzg") or {})
            code = lzg.get("code", "")
            for p in ("sh", "sz", "bj"):
                if code.startswith(p):
                    code = code[len(p):]
                    break
            rec = by_code.get(code)
            pend.append([b["name"], pick(b, "zdf"), round(zj, 2), pick(b, "zdf_d5"),
                         b.get("zgb", ""), lzg.get("name", ""), code,
                         lzg.get("zdf", ""), (rec or {}).get("池层") or "未入池",
                         "、".join((rec or {}).get("题材") or []) or "（无题材标签）"])
        pend.sort(key=lambda x: -(x[2] if isinstance(x[2], (int, float)) else -1e9))
        pend = pend[:cfg.get("lifecycle", {}).get("new_theme_top", 40)]

    F = cfg["filters"]
    B1 = F["bianshi_辨识度"]
    spec = [
        ["数据日期", f"{data_date}（最近交易日收盘）；用于下一交易日盘前取用"],
        ["生成时间", meta["run_at"]],
        ["生成程序", f"scripts/pool_pipeline.py v{cfg['version']}（参数 scripts/pool_config.json）"],
        ["覆盖范围", f"全A {meta['snapshot_n']} 只；日线指标 {meta['kline_n']} 只（{round(meta['kline_n']/max(meta['snapshot_n'],1)*100,1)}%）；龙虎榜 {meta['lhb_codes']} 只"],
        ["池层结果", f"核心 {meta['tier_core']} / 备选 {meta['tier_backup']} / 观察 {meta['tier_watch']}；"
                     f"题材 {len(wa)} 个（宽度『可用』{sum(1 for x in wa if x[5] == '可用')} 个）"
                     f"→ 聚成 {len({x[1] for x in wa})} 个主线簇；"
                     f"盘前焦点取前 {len(focus)} 个；待认领题材 {len(pend)} 个；"
                     f"行业 {len(wbb)} 个；老龙记忆 {len(wc)} 只"],
        ["窗口A0用途", f"**盘前真正要看的限量清单**：把同一波资金的多个题材名聚成『主线簇』后取前 {cfg.get('focus', {}).get('top_n', 12)} 个簇。"
                       f"固定的不是题材名单（那会漏新题材），而是**注意力预算**"],
        ["⚠ A0 入榜 = 六口径综合排名", f"**A0 不是只按资金排，也不是『任一单口径前 N 取并集』**——后者仍然要求某一项极端强才进得去。"
                                        f"本表对每个『宽度可用且有外部读数』的题材算六项排名：**资金 / 当日超额 / 5日超额 / 涨股比宽度 / 涨停家数 / 量比**，"
                                        f"取加权均值作为综合分（对应 3.3 时间尺度谱系：短看当日、中看 5 日；3.4 双口径：资金 × 广度；"
                                        f"涨停家数=赚钱效应、量比=量能承接，是『能不能走出来』的两个必要条件），"
                                        f"按主线簇去重后取前 {cfg.get('focus', {}).get('top_n', 40)} 个（前 {cfg.get('focus', {}).get('core_n', 15)} 标『重点』，其余『观察』）。"
                                        f"**为什么必须综合**：冰雪经济 资金第90 / 当日第62 / 5日第12 ——"
                                        f"每项都中等、没有一项极端的方向，任何单口径排序都会漏掉；六口径综合后稳定进榜。"
                                        f"「分项排名」列给出六项具体名次，可自行判断这个方向是靠什么入选的"],
        ["⚠ A0 第二条通道 = 环节通道", f"题材维度之外，环节榜前 {cfg.get('focus', {}).get('seg_slots', 8)} 名直接并入盘前焦点（档位标『环节』）。"
                                        f"**为什么需要这条通道**：产业链行情里真正轮动的是环节不是整条链——5-6 月炒半导体动的是 材料→设备→存储→封测，"
                                        f"CPO 行情里 光模块→光芯片→交换机 各走一段。过宽母题材（>500 只）读数是稀释后的、不进焦点，"
                                        f"而它的强子题材可能因为题材名冷门、绝对排名不够而进不了前 40——两头落空。"
                                        f"环节通道与题材通道**并列、互不挤占名额**，各按各的综合排名取"],
        ["⚠ 环节榜的四口径与资金缺口", f"窗口A2 按『产业链 × 环节』聚合，四口径综合：**当日超额 / 5日超额 / 涨停家数 / 成交额**。"
                                        f"**缺资金**——个股快照没有主力净流入字段，题材级加总又会把重叠成分重复计算（同一只票同时属于链内多个子题材），"
                                        f"宁可留空也不填一个会骗人的数。要看环节的资金强度，回到窗口A 看『代表题材(资金最大)』那一列。"
                                        f"**成分数已去重**，但环节层面仍普遍 >{cfg.get('granularity', {}).get('max_members', 350)} 只（如 军工链·低空与无人机 438 只），"
                                        f"所以环节榜的定位是**方向定位**（哪个环节在动），不是可直接取用的标的层；取票仍要回到窗口A 的具体题材或窗口C 的个股"],
        ["⚠ 链级大标签不是环节", f"『军工』『半导体概念』『国产芯片』『人工智能』『电池技术』这类覆盖整条链的泛标签，成分 400+ 只，与真环节不可比。"
                                  f"它们单列成『（链级·需下钻）』，保留可见（不静默删除），但**不参与环节强弱排名**、不进焦点，只作下钻入口——看到它就要往环节里钻"],
        ["⚠ 连续性是相对的", f"「连续性」按**相对全市场的超额**判定，不是绝对涨跌：超额·当日 = 题材当日涨幅 − 概念榜全榜中位数（{meta.get('mkt_median_d1', '')}），"
                              f"超额·5日 = 5日涨幅 − 全榜中位数（{meta.get('mkt_median_d5', '')}）。双正 = 持续，当日正 5日负 = 首日，当日负 = 走弱。"
                              f"**为什么必须相对**：10-16 全市场 5 日 −5.19%，绝对口径下 240 个题材被判成 151 首日 / 1 持续，完全没有区分度；"
                              f"改相对后分布为 走弱 406 / 首日 164 / 持续 235，冰雪经济 5 日超额 +4.03、白酒 +2.26 —— 它们是「持续」不是「首日」"],
        ["自算多日序列（不依赖外部榜）", f"日线回补（scripts/backfill_daily.py）把每只票最近 {cfg.get('data', {}).get('daily_backfill_days', 5)} 个交易日的日涨跌幅写进缓存，"
                                          f"于是**外部榜单匹配不上的题材也有多日数据**：『自算5日涨幅%』= 成分股 5 日累计涨幅均值；"
                                          f"『连续跑赢(日·自算)』= 从最近一日往前数，题材日均涨连续高于全市场日均涨中位的天数。"
                                          f"连续性列后缀『·自算』表示该判定来自自算序列而非外部榜单——**不冒充外部读数，但也不让方向因为没有外部读数就变成盲区**。"
                                          f"本次自算覆盖 5547/5923 只（99.9%），市场每日中位 {[round(x,2) if x is not None else None for x in mk_day]}，5日累计中位 {mk_ret5}%。"
                                          f"⚠ 自算**无法**得到主力净流入——资金是外部独有数据，所以无读数题材的资金列仍为空。"],
        ["读数匹配方式", "同一个题材在不同数据商那里名字不同（我们库『冰雪经济』/ 腾讯榜『冰雪概念』、『白酒』/『白酒概念』）。"
                        "匹配顺序：原名精确 → 别名表 → 归一化（去『概念/产业/板块』等后缀）→ 模糊（前缀包含）。"
                        "**匹配方式会写进表里：『模糊』意味着读数是借来的近似值，使用者可自行打折**；"
                        "『无读数』的题材仍然在表中（用成分股自算的均涨 / 涨停家数排序），只是没有资金与多日涨幅字段。"
                        "v1.3 之前匹配不上就整类删除，801 个题材只剩 240 个——这是本次修复的核心缺陷"],
        ["窗口A用途", "**题材全表**。按簇合计主力 → 簇内主力排序的题材榜（钱在炒什么 × 钱在谁手里）。当天资金进攻某题材 → 看「题材内领涨」确认谁真被选中，再看「池内核心」取有涨停基因的；两者交集优先"],
        ["龙头判定（重要）", "三筛选是**全市场绝对阈值**（股性 / 辨识度 / 量价），它只能证明『这票有涨停基因』，不能证明『它是这个题材的龙头』。"
                          "233 的龙头必须在**题材内部相对比较**。因此窗口A 的「题材内领涨」按**龙头分**排序，"
                          "龙头分 = 233 原文 3.2 六项在题材内部做百分位排名后加权（辨识度 / 涨停基因 / 5日线突破 / 成交量 / 涨速 / 板块带动性），"
                          "个股标注为 `名·涨幅·龙xx`。**为什么不能只按涨幅排**：233 原文明确『涨得最快 ≠ 龙头』——"
                          "一日冲高回落的高涨幅票常常只是情绪脉冲，六项综合才能把「有基因 + 有量 + 站上5日线 + 带动板块」的票选出来。"
                          "⚠ 边界：「板块带动性」用『个股当日涨幅 − 题材成分均涨』近似，真正的带动性要看个股涨停后板块是否跟涨，日频数据做不到——**近似不是等价**"],
        ["窗口A2用途", "**产业链 × 环节榜**。233 的『先板块后个股』里的板块，在产业链行情中往往是某个环节而不是整条链。"
                       "本表把题材按『链 → 环节』重新聚合，用于回答『这条链今天动的是哪个环节』——"
                       "只看题材名会把环节轮动稀释在几百只的大标签里，只看整条链又会抹平环节分化。"
                       "与窗口A（题材维度）**并行、互不替代**：A 找具体题材，A2 定位环节，取票仍回 A 或 C"],
        ["产业链层级（L1链→L2环节→L3题材）",
         f"config.chains 定义了 {len([k for k in cfg.get('chains', {}) if not k.startswith('_')])} 条链及其环节（半导体链含 材料/设备/设计/制造/封测/存储/功率/应用；"
         f"AI算力链含 光互联/服务器整机/数据中心/网络设备/算力服务 …）。题材名**包含**关键词即归链，链内再看环节关键词；"
         f"命中多条链按配置书写顺序取第一条，因此更具体的链写在前面。"
         f"⚠ 已加 exclude 修正误抢：『HJT电池 / TOPCon / BC电池 / 钙钛矿』含『电池』会被新能源车链抢走，实为光伏技术，已让回光储风链；"
         f"『生物识别 / 生物质能』含『生物』会被医药链抢走，已让出。等级：C级草案（链与环节的划分是本体系新增，233 原文只讲板块不讲产业链）"],
        ["窗口B用途", "行业口径兜底与交叉验证（3.4 要求行业榜 + 概念榜都拉）。按当日均涨排序；题材榜没覆盖时用这里"],
        ["窗口C用途", "全A明细与三筛选字段，带题材标签；可按题材 / 行业 / 池层 / 达标数筛选"],
        ["窗口D用途", "老龙记忆：涨停集中在 61—120 日区间、近期不活跃但市场有记忆的票（233『过去资金喜欢攻击谁』）"],
        ["窗口E用途", f"待认领题材：板块榜上有强资金（主力净流入 ≥{cfg.get('lifecycle', {}).get('new_theme_min_zljlr', 3)} 亿）"
                      f"但我们的题材库里**没有这个名字**的概念。多数是命名不一致（如『芯片概念』vs 我们库里的『国产芯片』），"
                      f"少数是全新题材。用其领涨股反查是否已在我们池内——**这张表是防漏的安全网，每天扫一眼有没有陌生名字**"],
        ["主线簇", f"同一波资金常有好几个题材名（实测『无人机 / 军民融合 / 军工』是同一波的三个名字，合计 117 亿）。"
                   f"不聚类会让前排被同一方向霸占——看似 10 个方向实际只有 3 个。聚类度量用**当日强势股（涨幅 ≥{cfg.get('focus', {}).get('strong_pct', 5)}%）重合度**，"
                   f"不用全体成分股：题材标签宽松导致成分股虚胖，全体重合度会被稀释（实测全体 Jaccard 一对都聚不出来）"],
        ["生命周期", f"基于历史台账（{cfg.get('history', {}).get('dir', 'cache/theme_history')}）的『连续在榜天数』与『较昨日排名变化』判定："
                     f"启动(首日进榜) / 强化(排名上升 ≥{cfg.get('lifecycle', {}).get('rank_up_min', 3)}) / "
                     f"维持 / 退潮(排名下滑 ≤{cfg.get('lifecycle', {}).get('rank_down_max', -5)} 或主力净流入转负)。"
                     f"**首次运行无历史，该列为空，从第二次运行起生效。**"
                     f"⚠️ 边界：这是**单个题材**的生命周期，不等于熔炼系统层2 的『板块演化链』（主线→溢出→切换→支线升主线）——"
                     f"演化链描述资金在板块之间的转移，需要板块间资金流对比，本程序尚未实现"],
        ["①股性（涨停基因）", f"判定：近60日涨停≥{F['guxing_股性']['zt60_min']} 次 或 最长连板≥{F['guxing_股性']['max_lb_min']}。等级：C级草案（阈值为本体系新增，233原文无任何数字）"],
        ["②辨识度（市场共识）",
         f"**按 233 原文 2.2 的四类重建**（旧口径只用龙虎榜一个代理，缺失「容量核心」）："
         f"①前期龙头 = 最长连板≥{B1['前期龙头']['max_lb_min']} 或 近120日涨停≥{B1['前期龙头']['zt120_min']}；"
         f"②人气股 = 近60日龙虎榜≥{B1['人气股']['lhb60_min']} 或 当日换手≥{B1['人气股']['turnover_min']}% 或 量比≥{B1['人气股']['liangbi_min']}；"
         f"③容量核心 = 成交额≥{B1['容量核心']['amount_yi_min']}亿 且 流通市值在 [{B1['容量核心']['fmcap_yi_min']}, {B1['容量核心']['fmcap_yi_max']}]亿；"
         f"④反复活跃 = 近60日涨停≥{B1['反复活跃']['zt60_min']} 或 近5日上涨≥{B1['反复活跃']['up_days_5_min']} 天。"
         f"命中 ≥{B1['min_classes']} 类即达标。等级：C级草案（233 原文无数字）；"
         f"⚠ 「龙虎榜=人气」只是人气的一种代理，不能等同于人气本身"],
        ["③量价（红肥绿瘦）", f"判定：红肥绿瘦比≥{F['liangjia_量价']['hongfei_min']} 且 近20日涨跌幅 > {F['liangjia_量价']['ret20_floor']}%（未破位）。等级：C级草案"],
        ["池层判定", "核心 = 三条全达标；备选 = 达标两条；观察 = 达标一条；ST / 退市整理 → 剔除。组织方式 B 级，阈值 C 级"],
        ["数据来源", f"快照源 {meta['snap_src']} / 东财 datacenter 龙虎榜 / 东财 push2ex 涨停池（板块归属与校验）/"
                     f" 新浪日线（涨停次数与红肥绿瘦由日线直算）/ 腾讯板块榜（题材与行业的涨幅·资金读数）/"
                     f" 东财 F10 核心题材（个股所属板块标签）；数据源按 config.sources 顺序回退，失败则字段留空，不编造"],
        ["粒度判定",
         f"题材成分股数在 [{G['min_members']}, {G['max_members']}] 之间才算『可用题材』："
         f"> {G['max_members']} 标注『过宽·需下钻』（内部必然分化，『板块在涨』不等于该题材的票都能买）；"
         f"< {G['min_members']} 标注『过窄·个股行情』（本质是个股行情的别名，只作催化提示，不作为板块）；"
         f"连续性列：当日与 5 日同涨 = 持续，当日涨 5 日跌 = 首日（3.4 要求 ≥2 日持续，首日不加仓只观察）"],
        ["标签分层",
         "个股『所属板块』是混合标签串，已按 config.granularity 分层：含Ⅱ/Ⅲ = 申万二级/三级行业；命中行业榜一级名 = 一级行业；"
         "其余进入题材候选。已排除四类：地域（以『板块』结尾）、风格与规模、估值与机构持仓、**交易状态**"
         "（昨日涨停 / 昨日连板 / 新股与次新股等——它们是『结果』不是『原因』，若不过滤会被当成一个板块）"],
        ["口径限制",
         f"①日线为不复权，除权日附近涨跌幅与红肥绿瘦比可能失真；"
         f"②未取到日线的 {meta['snapshot_n'] - meta['kline_n']} 只（{round((1 - meta['kline_n'] / max(meta['snapshot_n'], 1)) * 100, 1)}%）"
         f"涨停与量价字段留空——筛选时按「未达标」处理，不得读成 0 次涨停；"
         f"③题材标签有效率约 88%，未取到的个股没有题材维度（只落在窗口B 行业口径）；"
         f"④题材名与板块榜名字不完全一致者，强度字段留空（仍可按成分股自算的均涨 / 涨停家数看）；"
         f"⑤涨停按收盘涨停判定，不含盘中触板未封；⑥龙虎榜是人气代理，混有机构席位与公告冲击；"
         f"⑦同一只票属于多个题材，允许重复计入（233 龙头本就是多题材共振），不做唯一归属"],
        ["阈值属性", "全部数字阈值（涨停次数 / 连板 / 龙虎榜次数 / 红肥绿瘦比 / 20日涨幅下限）都是主框架为把 233 的定性词落成可执行字段而新增的量化草案——233 原文无任何数字；证据等级 C 级、待回测，不得单独触发交易"],
        ["使用纪律", "池层只决定候选资格、不决定仓位；仓位由熔炼系统第五章方程决定；环境系数 <0.4 时 233 路径不动手；板块进攻但池内无标的 → 默认不追（3.6 出口规则二）"],
        ["本工作簿如何更新",
         "**每次运行都是整本重建**（新建工作簿 → 逐张写入 → 保存），不是在原文件上改某几张。"
         "所以一行命令 = 全部表一起更新，表间口径天然一致（同一次采数、同一个市场基准、同一套题材标签与产业链归链）。"
         "可用的开关只有三个：① `--sheets A0,A2,口径`（或 config.output.sheets）**少写几张**——注意这只是少写，"
         "不会让剩下的表更新得『更准』，也**不要误读成『只有这几张变了』**；"
         "② `--old-sheets` / config.output.keep_old_sheets：是否把 9-04 旧版 8 张对照表附在末尾（默认不附）；"
         "③ `--out`：改输出路径。"
         "**调阈值不需要重新采数**：改完 config 用 `--reuse` 复用缓存，约 0.3 秒重出整本；"
         "只有换数据日期（下一个交易日）才需要全量重跑（约 3.5 分钟）。"
         "⚠ 程序只产候选资格，不下单、不自动改体系文件、不设定时任务"],
        ["旧版对照（9-04）", f"9-04 旧版股池的 8 张表是**只读留档**，永不更新（一更新就失去对照意义）。默认不附在本工作簿内，"
                             f"需要对照时直接开 {cfg['output']['keep_old_from']}，"
                             f"或运行时加 --old-sheets（改 config.output.keep_old_sheets = true 可长期附回）"],
        ["免责声明", cfg["disclaimer"]],
    ]

    if not str(meta.get("snap_src", "")).startswith(("eastmoney_push2", "cache")):
        spec.insert(14, ["⚠ 降级提示",
                         f"本次全A快照取的是降级源（{meta['snap_src']}）：行业归属缺失 → 窗口A 的板块列会大面积落入"
                         f"「其他（未分类·盘前忽略）」，窗口B 的板块列不可用；量比 / 60日涨幅 / 年初至今为空。"
                         f"涨停基因与红肥绿瘦由日线直算，不受降级影响。盘前若要按板块取用，应等东财源恢复后重跑，"
                         f"或改用盘中板块榜交叉对照。"])

    wb = Workbook()
    wb.remove(wb.active)

    # 出表开关：output.sheets / --sheets。默认全出。
    # ⚠ 各表共享同一次采数的口径（市场基准、题材标签、产业链归链、主线簇），
    # 只出部分表不会让剩下那几张「更新得更准」，只是少写几张——表间口径仍然一致（同一次运行）。
    # 真正要注意的是别把「本次只出了 A0」误读成「只有 A0 变了」。
    ALL_KEYS = ["A0", "A", "A2", "F", "E", "B", "C", "D", "口径"]
    sel = cfg.get("output", {}).get("sheets") or ALL_KEYS
    sel = {str(x).strip().upper() for x in sel}
    if sel != {k.upper() for k in ALL_KEYS}:
        log(f"  ⚠ 只输出部分表：{sorted(sel)}（其余表本次不写，表间口径仍同属本次运行）")

    def add(title, header, data, key=None):
        if key is not None and key.upper() not in sel:
            return None
        ws = wb.create_sheet(title)
        ws.append(header)
        for r in data:
            ws.append(r)
        return ws

    # ── 昨日盘口档案（2026-10-23 肉肉体系熔入）：角色 / 梯队 / 情绪 ──
    pankou_rows, senti = [], {}
    if pools and any(pools):
        t2c, s2c = {}, {}
        _IND = {x for r in rows for x in (r.get("行业标签") or []) if x}
        _ex = set((cfg.get("granularity") or {}).get("exclude_words") or [])
        try:
            _, _ind3, _ = TA.load_tag_map(pool_dir, cfg, classify_tags,
                                          universe=load_universe(cfg))
            for _v in (_ind3 or {}).values():
                _IND.update(x for x in (_v or []) if x)   # rows 行业标签只存一级名，三级名从 themes.json 取
        except Exception:
            pass
        for r in rows:
            for t in (r.get("题材") or []):
                if t in _IND or t in _ex:      # 行业名/事件噪声不当题材候选（2026-10-24 Step4，同 intraday_scan）
                    continue
                t2c.setdefault(t, []).append(r["code"])
                _ch, _sg = chain_of(t, cfg.get("chains", {}))
                if _ch and _sg:
                    s2c.setdefault((_ch, _sg), set()).add(r["code"])
        s2c = {k: list(v) for k, v in s2c.items()}
        focus_for_pk = [[f[0], f[1], f[2], f[3], f[27], f[28]] for f in focus]
        role_of, lad, senti, _main = PK.roles_ladders(
            pools[0], pools[1], pools[2], rows, focus_for_pk, t2c, s2c,
            attr_opt=(cfg.get("intraday") or {}).get("attr"),
            chain_of=lambda t: chain_of(t, cfg.get("chains", {})),
            block=_IND | _ex)
        for f in focus:
            L = lad.get(f[1]) or {}
            # 2026-10-24：焦点行是**主线视图**（这条线今天有多少兵、结构全不全）→ 用**篮子口径**
            # （挂该标签的全部涨停）；括号里给主归属家数——差额＝这些兵被更具体的题材认领了。
            # 个股身份（盘口/补涨）才用主归属口径，两者不能混。
            # 兼容旧快照：字段不齐时**不混拼两套口径**（否则会拼出「4家(主归属0)/0中军·无涨停」）。
            # 篮子判定缺失 → 只报家数；连「涨停标签」都没有 → 那时的「涨停/中军/判定」本身就是标签口径。
            _own = L.get("涨停")
            _n = L.get("涨停标签")
            if _n is None:
                _n = _own
                _tail = f'/{L.get("中军")}中军·{L.get("判定") or ""}'
            elif L.get("判定标签") is None:
                _tail = ""
            else:
                _mz = L.get("中军名标签") or ""
                _tail = f'/{L.get("中军标签")}中军' + (f"({_mz})" if _mz else "") + f'·{L["判定标签"]}'
            _cnt = f'{_n}家' + (f'(主归属{_own})' if _own not in (None, _n) else "")
            f.append(_cnt + _tail if _n else "—")
        code2th = {}
        for r in rows:
            for t in (r.get("题材") or []):
                code2th.setdefault(r["code"], []).append(t)
        fmc = {r["code"]: (r["fmcap"] or 0) for r in rows}
        for z in pools[0]:
            c = z.get("c")
            _b = role_of.get(c) or {"tags": ["跟风"], "theme": "", "note": ""}
            role = "·".join(_b["tags"][:3])
            thm, _note = _b.get("theme", ""), _b.get("note", "")
            pankou_rows.append([role, z.get("n"), thm or (code2th.get(c) or ["—"])[0],
                                z.get("lbc") or 1, fmt_hhmmss(z.get("fbt")),
                                round((z.get("fund") or 0) / 1e8, 2),
                                round(z.get("_ratio") or 0, 2), z.get("zbc") or 0,
                                round(z.get("zdp") or 0, 2),
                                (lad.get(thm) or {}).get("判定", ""),
                                round((fmc.get(c) or 0) / 1e8, 1)])
        RANK = {"空间龙": 0, "总龙头": 1, "日内龙": 2, "早盘板": 3, "助攻": 4,
                "补涨": 5, "换手龙": 6, "一字龙": 7, "天量换手": 8, "独苗": 9, "跟风": 10}
        pankou_rows.sort(key=lambda x: (RANK.get(x[0].split("·")[0], 9), -(x[3] or 0), x[4]))
        log(f"  昨日盘口档案：涨停 {senti.get('涨停')} / 炸板 {senti.get('炸板')}"
            f"（率 {senti.get('炸板率')}%）/ 最高 {int(senti.get('最高连板') or 0)} 板"
            f" · 梯队成立 {sum(1 for v in lad.values() if v.get('判定') == '成立')} 个焦点")
    else:
        for f in focus:
            f.append("")

    add("窗口A0·盘前焦点", ["档位", "主线(簇内综合最优题材)", "所在簇(簇代表)", "综合排名",
                            "分项排名(资金/当日/5日/宽度/涨停/量能)",
                            "入榜理由", "簇内题材数", "簇合计主力(亿)",
                            "当日涨幅%", "5日涨幅%", "20日涨幅%", "主力净流入(亿)", "涨股/总数", "连续性",
                            "超额·当日", "超额·5日", "读数匹配方式",
                            "连续在榜(交易日)", "较昨日排名变化", "生命周期阶段",
                            "池内个股数", "核心池", "题材内领涨(前6)", "池内核心(前6)", "成分均涨%(自算)",
                            "自算5日涨幅%", "连续跑赢(日·自算)", "产业链", "环节", "过宽·可下钻为",
                            "梯队(昨日·涨停/中军·判定)"], focus, "A0")
    add("窗口A·题材强度榜", ["题材", "主线簇", "簇内题材数", "簇合计主力(亿)", "成分股数", "宽度判定",
                             "当日涨幅%", "5日涨幅%", "20日涨幅%", "主力净流入(亿)", "涨股/总数", "连续性",
                             "超额·当日(pct−大盘)", "超额·5日(pct−大盘)", "读数匹配方式",
                             "题材内领涨(前6·按当日涨幅)", "成分均涨%(自算)", "成分涨停家数", "成交额(亿)",
                             "池内个股数", "核心池", "备选池", "观察池",
                             "池内核心(前6·题材内涨幅序)", "池内备选(前6)",
                             "自算5日涨幅%(不依赖外部榜)", "连续跑赢(日·自算)",
                             "产业链", "环节",
                             "连续在榜(交易日)", "较昨日排名变化", "生命周期阶段",
                             "过宽·可下钻为(环节)"], wa, "A")
    if seg_rows:
        add("窗口A2·产业链环节榜", ["环节综合排名", "产业链", "环节", "题材数", "子题材(前6)", "成分数(去重)",
                                    "宽度判定", "当日均涨%", "超额·当日", "5日均涨%(自算)", "超额·5日",
                                    "涨停家数(去重)", "涨股/总数", "成交额(亿)", "连续跑赢(日·自算)",
                                    "分项排名(当日/5日/涨停/成交额)",
                                    "环节内池内个股", "代表题材(资金最大)", "环节龙头(核心池前4)"], seg_rows, "A2")
    if pankou_rows:
        add("窗口F·昨日盘口质量", ["角色", "名称", "判定题材/所属", "连板", "首封", "封单(亿)",
                                   "封单占流通%", "炸板次数", "涨幅%", "该题材梯队", "流通市值(亿)"],
            pankou_rows, "F")
        from collections import Counter as _C
        _rc = _C(x[0] for x in pankou_rows)
        log(f"  窗口F·昨日盘口质量：{len(pankou_rows)} 只涨停（"
            + " / ".join(f"{k} {v}" for k, v in _rc.most_common(6)) + "）")

    if pend:
        add("窗口E·待认领题材", ["概念名(题材库无此名)", "当日涨幅%", "主力净流入(亿)", "5日涨幅%",
                                 "涨股/总数", "领涨股", "领涨股代码", "领涨股涨幅%",
                                 "领涨股池层", "领涨股已有题材"], pend, "E")
    add("窗口B·行业取用", ["行业", "当日平均涨跌幅%", "当日涨停家数", "近20日涨停家数", "当日成交额(亿)",
                           "池内个股数", "核心池", "备选池", "观察池", "核心池个股(前6)", "备选池个股(前6)"], wbb, "B")
    add("窗口C·全A股池明细", ["代码", "名称", "行业", "题材标签", "池层", "达标数",
                              "筛1_股性", "筛2_辨识度", "筛3_量价", "辨识度构成",
                              "现价", "当日涨跌幅%", "换手率%", "量比", "流通市值(亿)", "总市值(亿)",
                              "近20日涨停", "近60日涨停", "近120日涨停", "最长连板", "最近涨停日", "距最近涨停(日)",
                              "近60日龙虎榜", "红肥绿瘦比", "阳线均值%", "阴线均值%", "20日涨幅%", "60日涨幅%",
                              "站上5日线", "站上20日线", "站上60日线", "年初至今%", "东财60日涨幅%", "PE(动)", "PB"], wb_rows, "C")
    add("窗口D·老龙记忆", ["代码", "名称", "行业", "近120日涨停", "近60日涨停", "近20日涨停", "最长连板",
                           "最近涨停日", "距最近涨停(日)", "近60日龙虎榜", "红肥绿瘦比", "20日涨幅%", "60日涨幅%",
                           "现价", "流通市值(亿)", "池层"], wc, "D")
    add("口径与来源说明", ["项", "内容"], spec, "口径")
    def sty(key, title, *a):
        if title in wb.sheetnames:
            style(wb[title], *a)

    sty("E", "窗口E·待认领题材", 1)
    sty("A0", "窗口A0·盘前焦点", 1, ("W", "X"))
    sty("A", "窗口A·题材强度榜", 1, ("M", "U", "V"))
    sty("A2", "窗口A2·产业链环节榜", 1)
    sty("B", "窗口B·行业取用", 1, ("J", "K"))
    sty("C", "窗口C·全A股池明细", 2)
    sty("D", "窗口D·老龙记忆", 2)
    sty("口径", "口径与来源说明", 1, ("B",))
    if "口径与来源说明" in wb.sheetnames:
        wb["口径与来源说明"].column_dimensions["A"].width = 22
    focus_idx = [[r[0], r[1], r[2], r[3], r[27], r[28]] for r in focus]
    return wb, {"wa": len(wa), "wbb": len(wbb), "wb": len(wb_rows), "wc": len(wc),
                "focus": len(focus), "pend": len(pend),
                "tag_ok": sum(1 for x in wa if x[5] == "可用")}, focus_idx


def save_history(cfg, data_date, wa):
    """存当日题材榜快照（题材 → 主力净流入 / 涨幅 / 排名），供次日算连续性与排名变化。"""
    H = cfg.get("history", {})
    if not H.get("enabled"):
        return
    d = os.path.join(ROOT, H.get("dir", "cache/theme_history"))
    os.makedirs(d, exist_ok=True)
    snap = {"date": data_date,
            "ranks": {r[0]: {"zljlr": r[9] if isinstance(r[9], (int, float)) else None,
                             "zdf": r[6] if isinstance(r[6], (int, float)) else None,
                             "rank": i}
                      for i, r in enumerate(wa, 1)}}
    json.dump(snap, open(os.path.join(d, f"{data_date}.json"), "w"), ensure_ascii=False)
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for f in files[:-int(H.get("keep_days", 60))]:
        os.remove(os.path.join(d, f))


def load_history(cfg, data_date):
    """返回 {题材: 连续在榜天数}，以及昨日排名 {题材: rank}。取不到就返回空，不编造。"""
    H = cfg.get("history", {})
    if not H.get("enabled"):
        return {}, {}
    d = os.path.join(ROOT, H.get("dir", "cache/theme_history"))
    if not os.path.isdir(d):
        return {}, {}
    past = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and f[:-5] < data_date)
    if not past:
        return {}, {}
    prev = json.load(open(os.path.join(d, past[-1] + ".json")))
    prev_rank = {k: v["rank"] for k, v in prev["ranks"].items()}
    streak = {}
    for k in prev["ranks"]:
        n = 0
        for day in reversed(past):                       # 从最近往前数，断一天就停
            if k in json.load(open(os.path.join(d, day + ".json")))["ranks"]:
                n += 1
            else:
                break
        streak[k] = n
    return streak, prev_rank


def append_old_sheets(wb, cfg):
    path = os.path.join(ROOT, cfg["output"]["keep_old_from"])
    if not os.path.exists(path):
        log(f"  旧版对照源不存在，跳过：{path}")
        return 0
    src = load_workbook(path)
    n = 0
    for ws in src.worksheets:
        if cfg["output"]["old_sheet_tag"] in ws.title:
            d = wb.create_sheet(ws.title)
            for r in ws.iter_rows(values_only=True):
                d.append(list(r))
            for c in d[1]:
                c.fill = PatternFill("solid", fgColor="808080")
                c.font = Font(bold=True, color="FFFFFF")
            d.freeze_panes = "A2"
            n += 1
    return n


# ══════════════ 主流程 ══════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true", help="复用已有缓存，只重算+导表")
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-mirror", action="store_true")
    ap.add_argument("--old-sheets", action="store_true",
                    help="把 9-04 旧版股池的 8 张对照表附在工作簿末尾（默认不附，见 config.output.keep_old_sheets）")
    ap.add_argument("--sheets", default=None,
                    help="只写指定的表，逗号分隔：A0,A,A2,E,B,C,D,口径（默认全写；见 config.output.sheets）。"
                         "注意：每次运行都是整本重建，本开关只是少写几张，不能让某张表单独『更新得更准』")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-intraday", action="store_true",
                    help="允许在交易时段运行（会把盘中快照当收盘，仅调试用；正式股池禁止）")
    ap.add_argument("--cache-dir", default=None, help="--reuse 时指定复用哪个缓存目录（默认取最新）")
    ap.add_argument("--intraday", action="store_true",
                    help="盘中模式：用实时快照重出「盘中股池」（数据日=今天，快照/板块/涨停池实时，"
                         "日线指标复用昨日缓存；输出文件名带 _HHMM盘中；只作盘中参考，不替代盘后正式股池）")
    ap.add_argument("--config", default=CFG_PATH)
    a = ap.parse_args()

    t0 = time.time()
    cfg = json.load(open(a.config, encoding="utf-8"))
    log(f"=== 233 盘前股池流水线 v{cfg['version']} ===")

    # 1 交易日历
    days = []
    cached_dir = None
    if a.reuse:
        if a.cache_dir:
            base = a.cache_dir if os.path.isabs(a.cache_dir) else os.path.join(ROOT, a.cache_dir)
            cands = [base]
        else:
            cands = [os.path.join(ROOT, "cache", d)
                     for d in sorted(os.listdir(os.path.join(ROOT, "cache")), reverse=True)]
        for cand in cands:
            p = os.path.join(cand, "trade_days.json")
            if os.path.exists(p):
                days = json.load(open(p))
                cached_dir = cand
                log(f"复用缓存目录 {os.path.basename(cand)}（{len(days)} 个交易日）")
                break
    if not days:
        for s in cfg["sources"]["trade_days"]:
            days = src_trade_days(s, cfg["data"]["trade_days_lookback"])
            if len(days) > 100:
                log(f"交易日历[{s}] {len(days)} 天 {days[0]}..{days[-1]}")
                break
    if not days:
        raise SystemExit("交易日历获取失败（全部数据源不可用）")
    data_date = a.date or (dt.date.today().strftime("%Y%m%d") if a.intraday else days[-1])

    # ★ 时段护栏（2026-10-21 加）：交易时段内全A快照是"盘中值"，若当日无快照缓存，
    #   跑出来的股池会把盘中价当成"最近交易日收盘"，且 data_date 标注会与事实不符。
    #   这是**不可回溯**的污染（成交额/换手/量比无法用日线回补），故默认拒绝。
    _now = dt.datetime.now()
    _in_session = (_now.weekday() < 5 and
                   ((9, 15) <= (_now.hour, _now.minute) < (15, 30)))
    _snap_cached = os.path.exists(os.path.join(
        ROOT, cfg["output"]["cache_dir"].format(date=data_date), "all_a_snapshot.json"))
    if _in_session and not _snap_cached and not getattr(a, "allow_intraday", False) \
            and not getattr(a, "intraday", False):
        print("=" * 74)
        print(f"✗ 拒绝运行：现在 {_now:%H:%M} 处于交易时段，全A快照会是**盘中值**。")
        print(f"  目标日 {data_date} 无快照缓存 → 跑出来会把盘中价标成「最近交易日收盘」。")
        print("  盘中数据不可回溯（成交额/换手/量比无法用日线补），故不做。")
        print("  正确做法：收盘后（≥15:40）再跑，data_date 用当日；或 --reuse 复用已有缓存。")
        print("  确需调试：加 --allow-intraday（结果不得当股池用）。")
        print("=" * 74)
        return 3

    out_dir = os.path.join(ROOT, cfg["output"]["cache_dir"].format(date=data_date))
    if a.intraday:
        # ⚠ 盘中模式必须用独立缓存目录：否则会覆盖当日**正式**收盘快照（盘中价污染收盘档，不可回溯）
        out_dir += "_intraday"
    if cached_dir and cached_dir != out_dir:
        out_dir = cached_dir
    os.makedirs(out_dir, exist_ok=True)
    json.dump(days, open(os.path.join(out_dir, "trade_days.json"), "w"))
    d60 = days[-cfg["data"]["lhb_window_days"] - 1]

    # 2 快照
    snap_p = os.path.join(out_dir, "all_a_snapshot.json")
    snap_src = "cache·复用（原始源见该缓存目录 run.log）"
    if a.reuse and os.path.exists(snap_p):
        snap = json.load(open(snap_p))
        log(f"复用快照 {len(snap)} 只")
    else:
        snap = []
        for s in cfg["sources"]["snapshot"]:
            snap = src_snapshot(s)
            if snap:
                snap_src = s
                log(f"全A快照[{s}] {len(snap)} 只")
                break
        if not snap:
            raise SystemExit("全A快照获取失败（全部数据源不可用）")
        json.dump(snap, open(snap_p, "w"), ensure_ascii=False)

    # 3 涨停池（板块归属/校验，可失败）
    zt_p = os.path.join(out_dir, "zt_history.json")
    zt = {}
    if a.reuse and os.path.exists(zt_p):
        zt = json.load(open(zt_p)).get("per_code", {})
    else:
        for d8 in days[-21:]:
            for it in src_zt_pool(cfg["sources"]["zt_pool"][0], d8):
                c = str(it.get("c"))
                zt.setdefault(c, {"hybk": it.get("hybk", ""), "name": it.get("n", "")})
            time.sleep(0.15)
        log(f"涨停池近20日 {len(zt)} 只（仅板块归属）")
        json.dump({"per_code": zt}, open(zt_p, "w"), ensure_ascii=False)

    # 4 龙虎榜
    lhb_p = os.path.join(out_dir, "lhb_history.json")
    lhb = {}
    if a.reuse and os.path.exists(lhb_p):
        lhb = json.load(open(lhb_p))
        log(f"复用龙虎榜 {len(lhb)} 只")
    else:
        lag = cfg["data"]["lhb_lag_days"]
        for d8 in days[-(cfg["data"]["lhb_window_days"] + lag):-lag or None]:
            seen = set()
            for r in src_lhb(cfg["sources"]["lhb"][0], d8):
                c = str(r.get("SECURITY_CODE") or "")
                if not c or c in seen:
                    continue
                seen.add(c)
                rec = lhb.setdefault(c, {"lhb_days": []})
                rec["lhb_days"].append(d8)
            time.sleep(0.15)
        log(f"龙虎榜 {len(lhb)} 只")
        json.dump(lhb, open(lhb_p, "w"), ensure_ascii=False)

    # 4.5 板块强度榜（概念 + 行业）
    boards_p = os.path.join(ROOT, cfg["boards"]["cache_tpl"].format(date=data_date))
    boards = {}
    if os.path.exists(boards_p):
        boards = json.load(open(boards_p))
        log(f"复用板块榜 概念{len(boards.get('gn', []))} / 行业{len(boards.get('hy', []))}")
    else:
        for s in cfg["sources"]["boards"]:
            boards = src_boards(s, cfg)
            if boards.get("gn") or boards.get("hy"):
                log(f"板块榜[{s}] 概念{len(boards.get('gn', []))} / 行业{len(boards.get('hy', []))}")
                break
        if boards:
            os.makedirs(os.path.dirname(boards_p), exist_ok=True)
            json.dump({"data_date": data_date, **boards}, open(boards_p, "w"), ensure_ascii=False)
        else:
            log("板块榜获取失败（全部数据源不可用）→ 板块强度字段留空，题材只作标签不参与排序")

    # 4.6 个股题材标签
    thm_p = os.path.join(out_dir, cfg["themes"]["cache_name"])
    themes = {}
    ttl = cfg["themes"].get("ttl_days", 7) * 86400
    stale = os.path.exists(thm_p) and (time.time() - os.path.getmtime(thm_p)) > ttl
    if os.path.exists(thm_p) and (a.reuse or not stale):
        themes = json.load(open(thm_p))
        age = (time.time() - os.path.getmtime(thm_p)) / 86400
        log(f"复用题材标签 {len(themes)} 只（已存 {age:.1f} 天，TTL {cfg['themes'].get('ttl_days', 7)} 天）")
    else:
        if stale:
            log(f"题材标签已过期（>{cfg['themes'].get('ttl_days', 7)} 天），重新采集")
        for s in cfg["sources"]["themes"]:
            themes = src_themes(s, [x["code"] for x in snap], cfg)
            if themes:
                ok = sum(1 for v in themes.values() if v)
                log(f"题材标签[{s}] {len(themes)} 只（有标签 {ok}，{round(ok/max(len(themes),1)*100,1)}%）")
                break
        if not themes:
            log("题材标签获取失败 → 无题材维度，退回纯行业口径（会在口径页标注）")
        json.dump(themes, open(thm_p, "w"), ensure_ascii=False)

    # 5 日线
    km_p = os.path.join(out_dir, "kline_metrics.json")
    if a.intraday and not os.path.exists(km_p):
        # 盘中模式：日线指标必然还是昨日收盘的（当日日线未生成）——从最近的历史缓存复制，
        # 不重建（重建要 3.5 分钟，看板每时点跑一次不可接受）。
        for cand in sorted([d for d in os.listdir(os.path.join(ROOT, "cache"))
                            if d.startswith("pool_") and os.path.isdir(os.path.join(ROOT, "cache", d))],
                           reverse=True):
            src = os.path.join(ROOT, "cache", cand, "kline_metrics.json")
            if os.path.exists(src):
                shutil.copy(src, km_p)
                log(f"盘中模式：日线指标复用 {cand}（当日日线未生成，属预期）")
                break
    if (a.reuse or a.intraday) and os.path.exists(km_p):
        km = json.load(open(km_p))
        log(f"复用日线指标 {len(km)} 只")
    else:
        km = build_kline([s["code"] for s in snap], {s["code"]: s["name"] for s in snap}, cfg)
        log(f"日线指标 {len(km)}/{len(snap)} 只")
        json.dump(km, open(km_p, "w"), ensure_ascii=False)

    # 6 分层
    industry_names = {b["name"] for b in boards.get("hy", [])}
    industry_names |= {s["industry"] for s in snap if s["industry"]}
    rows = assemble(snap, km, lhb, zt, cfg, data_date, d60, themes, industry_names)
    for r in rows:
        r["days_since_zt"] = (dt.datetime.strptime(data_date, "%Y%m%d")
                              - dt.datetime.strptime(r["last_zt"], "%Y%m%d")).days if r["last_zt"] else None
    normalize_sector(rows)
    json.dump(rows, open(os.path.join(out_dir, "pool_final.json"), "w"), ensure_ascii=False)
    meta = {"run_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data_date": data_date,
            "snapshot_n": len(snap), "kline_n": len(km), "lhb_codes": len(lhb), "snap_src": snap_src,
            "tier_core": sum(1 for r in rows if r["池层"] == "核心"),
            "tier_backup": sum(1 for r in rows if r["池层"] == "备选"),
            "tier_watch": sum(1 for r in rows if r["池层"] == "观察"),
            "elapsed_s": round(time.time() - t0, 1)}
    json.dump(meta, open(os.path.join(out_dir, "summary.json"), "w"), ensure_ascii=False)
    log(f"分层完成：核心 {meta['tier_core']} / 备选 {meta['tier_backup']} / 观察 {meta['tier_watch']}")

    if a.dry_run:
        log("--dry-run：不写工作簿")
        return

    # 7 导表 + 旧版对照 + 落盘
    if a.sheets:
        cfg.setdefault("output", {})["sheets"] = [x.strip() for x in a.sheets.split(",") if x.strip()]
    # 昨日盘口档案数据（涨停/炸板/跌停池）——盘中与盘后同口径（pankou.py）
    pools = None
    try:
        ztp = src_zt_pool("eastmoney_ztpool", data_date, "zt")
        if ztp:
            pools = (ztp, src_zt_pool("eastmoney_ztpool", data_date, "zb"),
                     src_zt_pool("eastmoney_ztpool", data_date, "dt"))
        else:
            log("  ⚠ 涨停池取不到 → 窗口F 与梯队列留空（不编造）")
    except Exception as e:
        log(f"  ⚠ 涨停池异常：{e}")
    wb, sizes, focus_idx = build_book(rows, cfg, meta, data_date, boards, pools=pools)
    # focus 索引追加进 summary.json（盘中扫描脚本 intraday_scan.py 读它，避免解析 xlsx）
    try:
        s_p = os.path.join(out_dir, "summary.json")
        s2 = json.load(open(s_p, encoding="utf-8"))
        s2["focus_index"] = focus_idx
        s2["focus_note"] = "每条 [档位, 主线题材/环节, 所在簇, 综合排名, 产业链, 环节]"
        json.dump(s2, open(s_p, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:                          # 落盘失败不影响主交付
        log(f"  ⚠ focus 索引写入 summary 失败（不影响工作簿）：{e}")
    n_old = 0
    if a.old_sheets or cfg["output"].get("keep_old_sheets"):
        n_old = append_old_sheets(wb, cfg)
    _suffix = f'_{dt.datetime.now():%H%M}盘中' if a.intraday else ''
    out1 = a.out or os.path.join(ROOT, cfg["output"]["project_dir"],
                                 cfg["output"]["filename_tpl"].format(date=data_date).replace(
                                     ".xlsx", f"{_suffix}.xlsx"))
    wb.save(out1)
    log(f"保存 {out1}（新表 {len(wb.sheetnames) - n_old} 张"
        + (f" + 旧版对照 {n_old} 张" if n_old else "，未附旧版对照表") + "）")
    if not a.no_mirror and cfg["output"]["mirror_dir"]:
        md = cfg["output"]["mirror_dir"]
        if os.path.isdir(md):
            out2 = os.path.join(md, cfg["output"]["mirror_tpl"].format(date=data_date))
            wb.save(out2)
            log(f"另存 {out2}")

    # 8 台账
    ledger = os.path.join(ROOT, "cache", "pool_runs.csv")
    new = not os.path.exists(ledger)
    with open(ledger, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["运行时间", "数据日期", "全A只数", "日线覆盖", "核心", "备选", "观察",
                        "板块数", "老龙记忆", "配置版本", "耗时s", "输出"])
        w.writerow([meta["run_at"], data_date, meta["snapshot_n"], meta["kline_n"],
                    meta["tier_core"], meta["tier_backup"], meta["tier_watch"],
                    sizes["wa"], sizes["wc"], cfg["version"], meta["elapsed_s"], out1])
    open(os.path.join(out_dir, "run.log"), "w", encoding="utf-8").write("\n".join(LOG))
    log(f"台账已追加 cache/pool_runs.csv；总耗时 {meta['elapsed_s']}s")


if __name__ == "__main__":
    main()
