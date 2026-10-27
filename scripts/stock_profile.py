"""个股画像 · 静态层（2026-10-24 用户要求：分类要更细、要有核心业务）

用户原话：
  「分类的粒度不够细啊，你像启明星辰最相关的就是网络安全的啊，一个股票会有很多的概念的，
    每天最相关的不一样，还有每个公司最核心的业务是什么，这些都是需要你去考虑的」

结论：单靠「所属板块标签串的第 1 个词」定身份是错的——
  · 启明星辰 002439 的标签串里 DeepSeek概念 排在网络安全 前面，于是被标成「AI应用链·模型」，
    但它 99.3% 的收入来自「信息网络安全」（2026 中报），毛利 56.3%。它的身份就是网络安全。
  · 反之，标签里没有的，也不等于没有——要用主营构成反查。

本模块只做**静态层**（不随行情变）：
  1. 主营构成（按产品优先 → 按行业兜底）→ 核心业务、占比、毛利率、纯度
  2. 主营范围（经营范围原文）→ 关键词兜底
  3. 主业环节（把核心业务名映射到 chains 的 链·环节）

数据源：东财 F10 BusinessAnalysis（PageAjax），字段：
  zygcfx = 主营构成（MAINOP_TYPE: 1=按行业, 2=按产品, 3=按地区）
  zyfw   = 主营范围（BUSINESS_SCOPE）
缓存：cache/profile/{code}.json，增量，不重复请求；取不到就留空，不编造。
"""
import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache", "profile")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Referer": "https://emweb.securities.eastmoney.com/"}
F10 = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"


def secid(code):
    """代码 → 东财 secid。**92 开头的北交所新代码段必须先判 BJ**。

    踩过的坑：原写法 `startswith(("6","9")) → .SH` 把 920207/920091 拼成 `920207.SH`，
    F10 接口对这种代码直接返回 `zygcfx` 0 条，且**不报错**——缓存里就留下一条空画像，
    前端显示「核心业务 —」，肉眼完全看不出是拼错代码。
    """
    code = str(code)
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    if code.startswith(("6", "9")):
        return code + ".SH"
    return code + ".SZ"


def _path(code):
    return os.path.join(CACHE, f"{code}.json")


# 主营构成里的「非业务项」：既不是产品也不是行业，占比再大也不能拿来定身份
JUNK_ITEMS = ("补充", "分部间抵销", "分部间抵消", "抵销", "抵消", "合计", "小计",
              "其中:", "其中：", "内部交易", "其他业务")

# 地区口径识别：主营构成里以这些词为主的，说明这一栏是「按地区」而不是「按产品」
GEO_WORDS = ("境内", "境外", "国内", "国外", "出口", "内销", "外销", "华东", "华南", "华北",
             "华中", "西南", "西北", "东北", "总部", "其他地区", "港澳台", "亚洲", "欧洲",
             "北美", "海外", "中国大陆")


def _is_geo_split(items):
    if not items:
        return False
    hit = sum(1 for x in items if any(w in (x.get("ITEM_NAME") or "") for w in GEO_WORDS))
    return hit >= max(2, int(len(items) * 0.6))


def fetch_one(code, retries=2, timeout=12):
    """单只：主营构成 + 主营范围。失败返回 None（调用方留空，不编造）。"""
    for _ in range(retries):
        try:
            r = requests.get(F10, params={"code": secid(code)}, headers=UA, timeout=timeout)
            if r.status_code != 200 or not r.text.strip():
                time.sleep(0.6)
                continue
            d = r.json()
            zy = d.get("zygcfx") or []
            if not zy:
                return {"code": code, "asof": "", "basis": "", "items": [], "scope": "",
                        "src": "eastmoney_f10", "note": "无主营构成数据",
                        "fetched": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
            # ── 报告期 × 口径的选择（2026-10-24 重写）──
            # 半年报经常只披露「按地区」，年报才有「按产品/按行业」。原实现只取最新一期，
            # 于是大智慧(601519)这类公司「核心业务」永远是「—」，只能靠行业兜底，
            # 连带着错位度也被算成『跨链蹭概念』——一个数据缺失被包装成了一个结论。
            # 改为跨报告期回退：
            #   ① 优先「有按产品」的最新一期（比最新报告期旧 500 天以上则让位，避免用太陈旧的业务口径）
            #   ② 否则用「有按行业」的最新一期
            #   ③ 再不然用最新一期剔除地区词后的剩余项
            #   ④ 全无 → 留空（不编造）
            dates = sorted({(x.get("REPORT_DATE") or "") for x in zy if str(x.get("REPORT_DATE") or "")[:10]<=datetime.date.today().isoformat()}, reverse=True)
            latest = dates[0] if dates else ""

            def _grp(dt, tp):
                g = [x for x in zy if (x.get("REPORT_DATE") or "") == dt
                     and str(x.get("MAINOP_TYPE")) == tp and (x.get("MBI_RATIO") or 0) > 0]
                return g if (g and not _is_geo_split(g)) else []

            def _newest(tp):
                for dt in dates:
                    g = _grp(dt, tp)
                    if g:
                        return dt, g
                return "", []

            def _age(d):
                try:
                    return (datetime.date.fromisoformat(latest[:10])
                            - datetime.date.fromisoformat(d[:10])).days
                except Exception:
                    return 9999

            d2, g2 = _newest("2")          # 按产品
            d1, g1 = _newest("1")          # 按行业
            basis, pick, asof = "", [], latest
            if g2 and (not g1 or _age(d2) <= 500):
                basis, pick, asof = "按产品", g2, d2
            elif g1:
                basis, pick, asof = "按行业", g1, d1
            elif g2:
                basis, pick, asof = "按产品", g2, d2
            else:
                same = [x for x in zy if (x.get("REPORT_DATE") or "") == latest]
                pick = [x for x in same if (x.get("MBI_RATIO") or 0) > 0
                        and not any(w in (x.get("ITEM_NAME") or "") for w in GEO_WORDS)]
                basis = "未标注口径" if pick else ""
            note = ""
            if pick and asof[:10] != latest[:10]:
                note = f"最新报告期 {latest[:10]} 只有按地区口径，取 {asof[:10]} 的{basis}数据"
            items = []
            for x in sorted(pick, key=lambda y: -(y.get("MBI_RATIO") or 0)):
                nm = (x.get("ITEM_NAME") or "").strip()
                if not nm or any(j in nm for j in JUNK_ITEMS):
                    continue
                items.append({"name": nm,
                              "ratio": round((x.get("MBI_RATIO") or 0) * 100, 1),
                              "gross": (round(x.get("GROSS_RPOFIT_RATIO") * 100, 1)
                                        if isinstance(x.get("GROSS_RPOFIT_RATIO"), (int, float)) else None)})
            scope = ""
            for s in (d.get("zyfw") or []):
                if s.get("BUSINESS_SCOPE"):
                    scope = s["BUSINESS_SCOPE"]
                    break
            return {"code": code, "asof": asof[:10], "basis": basis, "items": items[:10],
                    "scope": scope[:600], "src": "eastmoney_f10", "note": note,
                    "fetched": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
        except Exception as e:
            last = str(e)[:60]
            time.sleep(0.6)
    return None


def load(codes, workers=8, refresh=False, verbose=True):
    """批量读画像（先读缓存，缺的才抓）。返回 {code: profile}；抓失败的键值为 None。"""
    os.makedirs(CACHE, exist_ok=True)
    out, todo = {}, []
    for c in dict.fromkeys(str(x) for x in codes if x):
        p = _path(c)
        if os.path.exists(p) and not refresh:
            try:
                value = json.load(open(p, encoding="utf-8"))
                if value.get("asof", "")>datetime.date.today().isoformat():
                    raise ValueError("future profile asof")
                out[c] = value
                continue
            except Exception:
                pass
        todo.append(c)
    if todo:
        if verbose:
            print(f"  画像抓取：缓存命中 {len(out)}，需抓 {len(todo)}")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, (c, p) in enumerate(zip(todo, ex.map(fetch_one, todo)), 1):
                if p:
                    out[c] = p
                    json.dump(p, open(_path(c), "w", encoding="utf-8"), ensure_ascii=False)
                if verbose and i % 50 == 0:
                    print(f"    进度 {i}/{len(todo)}")
    return out


# ══════════════ 核心业务 ══════════════
def core(p):
    """→ {main, ratio, gross, purity, items}；无数据返回 None 字段（不编造）。"""
    if p and p.get("asof", "")>datetime.date.today().isoformat():
        p={"items":[],"note":"未来报告期已隔离，等待核验"}
    if not p or not p.get("items"):
        return {"main": "", "ratio": None, "gross": None, "purity": "", "items": [],
                "asof": (p or {}).get("asof", ""), "basis": (p or {}).get("basis", "")}
    it = p["items"]
    top = it[0]
    purity = ""
    if top.get("ratio") is not None:
        r = top["ratio"]
        purity = "单一主业" if r >= 90 else ("主业突出" if r >= 70 else ("多元" if r >= 40 else "分散"))
    return {"main": top["name"], "ratio": top.get("ratio"), "gross": top.get("gross"),
            "purity": purity, "items": it, "asof": p.get("asof", ""), "basis": p.get("basis", "")}


def brief(p, max_items=2):
    """一句话核心业务：主项 99.3%(毛利56.3%) + 次项"""
    c = core(p)
    if not c["main"]:
        return "—"
    s = c["main"]
    if c["ratio"] is not None:
        s += f" {c['ratio']:g}%"
        if c["gross"] is not None:
            s += f"(毛利{c['gross']:g}%)"
    rest = [x for x in c["items"][1:] if (x.get("ratio") or 0) >= 8]
    if rest:
        s += " + " + "、".join(f'{x["name"]}{x["ratio"]:g}%' for x in rest[:max_items])
    return s


# ══════════════ 行业 → 链（兜底） ══════════════
IND2CHAIN = {
    "计算机": "软件与数据链", "软件开发": "软件与数据链",
    "计算机设备": "AI算力链", "通信": "AI算力链", "通信服务": "AI算力链",
    "电子": "半导体链", "半导体": "半导体链", "元件": "电子元器件链",
    "光学光电子": "电子元器件链", "消费电子": "电子元器件链",
    "电力设备": "光储风链", "电池": "新能源车链", "汽车": "新能源车链",
    "汽车零部件": "新能源车链", "自动化设备": "机器人链",
    "工程机械": "地产基建链",
    "国防军工": "军工链", "医药生物": "医药链", "医疗服务": "医药链",
    "食品饮料": "消费链", "家用电器": "消费链", "美容护理": "消费链",
    "纺织服饰": "轻工纺服链", "轻工制造": "轻工纺服链", "造纸": "轻工纺服链",
    "包装印刷": "轻工纺服链", "文娱用品": "轻工纺服链",
    "商贸零售": "消费链", "社会服务": "消费链", "传媒": "传媒内容链",
    "银行": "金融链", "非银金融": "金融链",
    "房地产": "地产基建链", "建筑装饰": "地产基建链", "建筑材料": "地产基建链",
    "钢铁": "资源链", "有色金属": "资源链", "煤炭": "资源链", "石油石化": "资源链",
    "基础化工": "资源链", "化学制品": "资源链", "化学原料": "资源链",
    "公用事业": "公用事业链", "环保": "公用事业链", "电力": "公用事业链",
    "交通运输": "交运链", "农林牧渔": "农业链", "综合": "",
}


def ind_chain(ind, ind2chain=None):
    """申万行业名 → 链。先精确、再前缀、再包含。取不到返回空串。"""
    m = ind2chain or IND2CHAIN
    if not ind:
        return ""
    if ind in m:
        return m[ind]
    for k, v in m.items():
        if ind.startswith(k) or k.startswith(ind):
            return v
    for k, v in m.items():
        if k in ind or ind in k:
            return v
    return ""


# 环节名天然是泛词，直接拿来匹配主营项名会抢错（实测：华依科技『专用设备制造行业』
# 被半导体链的『设备』抢走，实际做汽车测试设备）。这类词只允许通过『链 match』命中。
# 注意『软件』『芯片』不在此列：『电力造价软件』必须归 软件与数据链·行业IT，
# 而不是被公用事业链的『电力』抢到火电水电。
GENERIC_KW = {"设备", "材料", "应用", "服务", "设计", "制造", "测试", "存储", "功率",
              "控制", "感知", "本体", "零部件", "化工", "煤炭"}

# 材料/化工类词：命中则视为化工新材料，避免『广告喷墨打印材料』被传媒链的『广告』抢走。
# 裸词『材料』已剔除（2026-10-24）：它把『电子元器件及电子材料』『铜基合金材料』一起吞进
# 资源链·化工——材料类公司多数能靠环节词表自己命中（云母复合材料/超硬材料/氟材料都在），
# 只对极少数『XX材料』兜底有效，代价却是把电子材料误判成化工。改用『喷墨』这类具体词兜底。
MATERIAL_KW = ("化工", "树脂", "涂料", "油墨", "纤维", "助剂", "添加剂",
               "薄膜", "基膜", "橡胶", "塑料", "喷墨")


# ── 归属绑定 / 共现门槛（2026-10-24 Step1）────────────────────────────────
# 规则本体在 scripts/kwrule.py（无第三方依赖，自检脚本也要用同一份，避免两份规则漂移）。
# 两个机制的数据挂在 pool_config.json 的 chains["_归属绑定"] / chains["_共现门槛"] 里，
# 用下划线键的约定让 8 处调用点都不必改签名（所有遍历 chains 的地方本就跳过 "_" 开头）。
from kwrule import GATE_STRIP_SUFFIX, bind_ok, gate_ok  # noqa: E402  (规则唯一定义处)

# 申万三级行业名带罗马数字后缀（「IT服务Ⅲ」「中药Ⅲ」「航空装备Ⅲ」），
# `_行业定环节` 表按不带后缀的写法维护，查表前统一剥掉。
IND_SUFFIX = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")


def deep_match(name, chains):
    """名称 → (链, 环节, 命中词)。**先找环节、再找链、再判材料**。

    为什么顺序要反过来：chain_of 要求先命中链的 match 词表，而 match 词表是给
    『题材标签』设计的（网络安全/信创/白酒…），主营项名往往写法不同——
    『安全产品』『安全运营与服务』『茅台酒』『商业』都命不中 match，却都在环节词表里。
    先扫 109 个环节，命中「环节」比命中「链」信息量大得多，也顺手解决了
    『横向通用软件』这类申万三级冒充题材的问题（它不在任何环节词表里）。
    """
    if not name:
        return "", "", ""
    bind = chains.get("_归属绑定") or {}
    gates = chains.get("_共现门槛") or {}
    generic = set(chains.get("_泛词不参与环节") or GENERIC_KW)
    best = ("", "", "", 0)
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        if any(k in name for k in cv.get("exclude", [])):
            continue
        for sn, kws in (cv.get("环节") or {}).items():
            for kw in kws:
                if not kw or kw in generic or kw not in name:
                    continue
                if not bind_ok(kw, cn, sn, bind):
                    continue
                if not gate_ok(name, kw, gates):
                    continue
                if len(kw) > best[3]:
                    best = (cn, sn, kw, len(kw))
    if best[0]:
        return best[0], best[1], best[2]
    # 材料类兜底（放在链 match 之前）：『广告喷墨打印材料』不该是传媒·广告营销
    if any(w in name for w in MATERIAL_KW):
        return "资源链", "化工", next(w for w in MATERIAL_KW if w in name)
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        if any(k in name for k in cv.get("exclude", [])):
            continue
        for kw in cv.get("match", []):
            if not kw or kw not in name:
                continue
            if not bind_ok(kw, cn, "", bind):
                continue
            if len(kw) > best[3]:
                best = (cn, "", kw, len(kw))
    return (best[0], best[1], best[2]) if best[0] else ("", "", "")


_OVERRIDE = None


def load_override(path=None):
    """人工核实过的逐票身份（`scripts/identity_override.json`）。

    为什么需要这一层：19 链 / 108 环节是一套**有限**的分类体系，而 5000 多家公司的
    业务是无限的。总有一批票的生意落在这套体系之外（工业制冷设备、煤炭机械、检测服务…），
    规则无论怎么改都答不对——这时候正确答案是「体系覆盖不到」，不是硬塞一个环节。

    结构：
      { "600202": {"name":"哈空调", "chain":"", "seg":"",
                   "reason":"石化/电站空冷器，19 链无对应", "by":"人工",
                   "conf":0.95, "asof":"2026-10-24"} }
    `chain`/`seg` 同为空串 = **已核实确认不归链**，与「还没查过」是两件事，
    前端据此显示「未归链（已核实）」而不是空白。

    只认 `code`。改这个词表以外的东西都不会影响它——它是最后一道人工闸门。
    """
    global _OVERRIDE
    default = path is None
    if default:
        if _OVERRIDE is not None:
            return _OVERRIDE
        path = os.path.join(ROOT, "scripts", "identity_override.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = {k: v for k, v in d.items() if not k.startswith("_") and isinstance(v, dict)}
    if default:
        _OVERRIDE = d
    return d


def main_seg(p, chains, inds=(), ind2chain=None, industry_to_chain=None):
    """主业 → (链, 环节, 证据)。

    优先级：
      0. 人工核实过的 override（最高优先，见 load_override 的说明）；
      1. 主营项名（占比大的先）逐条 deep_match —— 这是「公司靠什么赚钱」的直接答案；
      2. 申万三级 → 二级 → 一级 的行业兜底（只做精确匹配，不猜）；
      3. 经营范围原文：逐条链的关键词做子串搜索（**不做滑窗切词**——滑窗会把
         『安全运营与服务系统开发』切成 12 字块，偶然切出『包装』『服装』，
         实测把启明星辰判成了轻工纺服链·纺织制造）。
    返回 ("","","") 表示没归上，调用方老实写「主业未归链」，不硬编。
    """
    if not p:
        return "", "", ""
    ov = load_override().get(str(p.get("code") or ""))
    if ov is not None:
        return ov.get("chain") or "", ov.get("seg") or "", f"人工:{ov.get('reason') or '已核实'}"
    # 只用**真正撑起收入**的主营项：占比 ≥20% 且 ≥最大项的 85%（并列主业才都算）。
    # 两个反例都是实测出来的：中基健康被 3.2% 的『种子』归进农业链·种业；
    # 海辰药业被 25% 的电解液添加剂归进新能源车链·电池，而它 67% 的收入来自心脑血管药。
    its = [x for x in (p.get("items") or []) if (x.get("ratio") or 0) > 0]
    top = max([x.get("ratio") or 0 for x in its], default=0)
    chain_only = None            # 主营项名只定到链、定不了环节时先记下
    for it in its:
        r = it.get("ratio") or 0
        if r < max(20.0, top * 0.85):
            continue
        ch, sg, kw = deep_match(it.get("name") or "", chains)
        if ch and sg:
            return ch, sg, f"主营:{it.get('name')}"
        if ch and not chain_only:
            chain_only = (ch, f"主营:{it.get('name')}")
    # 主营只定到链（『芯片销售』『智慧空间整体解决方案』这类）→ 借行业表补一次环节，
    # 但**链必须一致**才采信。2026-10-24 实测：33 只汽车零部件、16 只软件开发、
    # 10 只半导体都卡在这一步——主营项名叫『芯片销售』只够定链，行业三级名却能定环节。
    if chain_only:
        _seg = chains.get("_行业定环节") or {}
        for _ind in reversed(list(inds or [])):
            _hit = _seg.get(_ind) or _seg.get(IND_SUFFIX.sub("", _ind).strip()) if _ind else None
            if _hit and "·" in _hit:
                _cn, _sn = _hit.split("·", 1)
                if _cn == chain_only[0] and _sn in (chains[_cn].get("环节") or {}):
                    return _cn, _sn, f"{chain_only[1]}+行业:{_ind}"
        return chain_only[0], "", chain_only[1]
    # 行业兜底：**三级最准**，逐级放宽（inds 是 [一级, 二级, 三级]，所以倒着走）；
    # 只认精确键，不猜。歌尔股份靠这一条从『电子→半导体链』修正到『消费电子→电子元器件链』。
    # 2026-10-24 补：行业名本身也能定**环节**——申万三级里的『机器人』『消费电子』『白酒』
    # 本来就是环节词。原来只取链、环节一律留空，等于把最准的那条信息扔了（全池 49.7% 靠兜底）。
    m = industry_to_chain or ind2chain or IND2CHAIN
    # 「行业 → 环节」显式表（2026-10-24 Step2）：申万三级名有 105 种，其中 270 只票
    # 卡在"链对了但下不到环节"——deep_match 认不出「IT服务Ⅲ」「印制电路板」「医疗耗材」
    # 这类申万专用名。逐个判断 105 个名字，比逐票判断 270 只便宜得多，而且一次判断永久有效。
    # 查表前剥掉罗马数字后缀：映射表按「IT服务」写，实际标签是「IT服务Ⅲ」。
    segmap = chains.get("_行业定环节") or {}
    if segmap:
        for ind in reversed(list(inds or [])):
            if not ind:
                continue
            hit = segmap.get(ind) or segmap.get(IND_SUFFIX.sub("", ind).strip())
            if hit and "·" in hit:
                cn, sn = hit.split("·", 1)
                if cn in chains and sn in (chains[cn].get("环节") or {}):
                    return cn, sn, f"行业:{ind}"
    # 只有**最细那一级**（申万三级）才允许用它去猜环节；二级/一级名太粗，猜出来的环节多半是错的：
    # 『食品饮料』含『饮料』→ 全部归啤酒饮料（乳制品/奶酪/火腿/速冻食品全中招，实测 15 只）、
    # 『纺织服饰』含『纺织』→ 全部归纺织制造（男鞋/名表/钻石饰品全中招，实测 13 只）、
    # 『传媒』→ 数字媒体（演艺演出/物资销售也中招，实测 11 只）。
    # 这两个反例都是 2026-10-24 抽查 103 条行业表时翻出来的，属于预存在错误。
    # 粗一级的行业名只用来**定链**，环节宁可留空——留空是诚实地不知道，猜错是假装知道。
    for depth, ind in enumerate(reversed(list(inds or []))):
        if ind in m and m[ind]:
            ch = m[ind]
            # 链名必须是 chains 里真实存在的链。IND2CHAIN 曾把「商贸零售/社会服务」
            # 映射到不存在的「商贸社服链」——那条链没有环节、不在题材榜，票会被
            # 静默写进一条空链，错位度算不出来也不报错。宁可退回空，也不给幽灵链。
            if ch not in chains:
                continue
            if depth == 0:
                ch2, sg2, _ = deep_match(ind, chains)
                if ch2 == ch and sg2:
                    return ch, sg2, f"行业:{ind}"
            return ch, "", f"行业:{ind}"
    # 材料/化工类词尾兜底：主营产品本身是材料（『超硬材料』『高分子改性材料』）时给资源链·化工。
    # 2026-10-24 收紧——旧版把**全部**主营项名拼成一个串做子串搜索，命中数一直是 0（都被行业兜底
    # 抢先返回），所以从没被真正检验过；删掉『机械设备→机器人链』后它第一次生效，立刻判错三只：
    #   利欧股份（0.2% 的『金属材料贸易』→ 传媒广告 80.7%）、铁科轨道（9.4% 的『工程材料』→ 轨道扣件 67.9%）、
    #   同力天启（『电梯部件及材料』90.4%，主体是电梯部件，不是材料）。
    # 现在两条约束：① 只认真正撑收入的主营项（≥max(20%, 最大项×85%)，同 main_seg 口径）；
    #              ② 材料/化工词必须**收尾**，且名称里不能带『部件/设备/整机/装置/系统』——
    #                 『电梯部件及材料』是部件生意，『超硬材料』才是材料生意。
    MAT_TAIL = ("材料", "化工", "树脂", "涂料", "油墨", "纤维", "助剂",
                "添加剂", "薄膜", "基膜", "橡胶", "塑料")
    PART_WORDS = ("部件", "设备", "整机", "装置", "系统", "机械", "装备", "组件", "器件", "零件")
    for it in its:
        r = it.get("ratio") or 0
        nm = it.get("name") or ""
        if r < max(20.0, top * 0.85) or not nm.endswith(MAT_TAIL):
            continue
        if any(w in nm for w in PART_WORDS):
            continue
        return "资源链", "化工", f"关键词:材料/化工({nm})"
    # ══════ 经营范围兜底：已删除（2026-10-24）══════
    # 旧版最后一道兜底是「拿每条链的 match 词去搜经营范围原文」。它在本数据集里**从未命中过一次**
    # （2026-10-23 全池 1037 只有画像的票：人工 27 / 主营 502 / 行业 508，经营范围 0）——
    # 因为『机械设备→机器人链』那条一级兜底总是先返回。删掉那条之后它第一次真正生效，
    # 59 只里几乎全错：软件与数据链 12 只（杰克科技=缝纫机、卓郎智能=纺机、博众精工=3C设备，
    # 经营范围里都有『软件』）、电子元器件链 7 只（山东威达=电动工具、大元泵业=水泵，都有『元件』）、
    # 地产基建链 3 只（铁科轨道=高铁扣件，有『建筑』）、传媒内容链 1 只（快意电梯，有『体育』）、
    # 消费链 4 只（都有『零售』）。
    # 根因：经营范围是工商登记的法律文本，写的是「**可以**做什么」，不是「靠什么赚钱」。
    # 用它对全市场做子串搜索，等于拿一份营业执照猜主业。宁可写「主业未归链」，也不许这样硬编。
    # 需要更细的身份，走 identity_override.json 逐票人工核实（那份表出错的代价只有一只票）。
    return "", "", ""


if __name__ == "__main__":
    codes = sys.argv[1:] or ["002439", "300369", "603278"]
    prof = load(codes)
    for c in codes:
        p = prof.get(c)
        print(c, "|", brief(p), "|", core(p)["purity"], "|", (p or {}).get("basis"))
