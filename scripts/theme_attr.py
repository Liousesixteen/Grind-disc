"""题材归因 · 动态层（2026-10-24 用户要求）

用户原话：「一个股票会有很多的概念的，每天最相关的不一样」。

旧口径的两个硬伤：
  ① **谁先出现谁当选**：代码里取 `题材[0]`，而标签串顺序来自东财 F10 的原始排列
     （启明星辰的 DeepSeek概念 排在 网络安全 前面）——这不是判断，是巧合。
  ② **大桶必胜**：pankou.roles_ladders 用「该题材涨停家数最多」选主归属。
     大桶成分股多，涨停家数天然多：『人工智能』『DeepSeek概念』这种 300+ 只的泛标签
     永远赢过『网络安全』这种 60 只的实环节，环节轮动被稀释成「AI 又涨了」。

本模块的口径（C 级草案，全部可观测、可复算）：
  score(题材 t | 个股 s) =
      0.42 × 强度   （该题材成分股等权涨 − 全市场中位，即当日超额）
    + 0.18 × 涨停率 （该题材内涨停家数 ÷ 成分数，**用率不用家数**——这才是大桶免疫的关键）
    + 0.12 × 广度   （该题材内上涨家数占比）
    + 0.10 × 焦点位 （是否在盘前 A0 焦点榜内，越靠前越高）
    + 0.18 × 特异度 （成分数 8—120 只 = 一个真环节；>250 只 = 大桶，折价）
  再叠加**主业锚定**（来自 stock_profile 的主营构成）：
    + 18 分：归因环节 == 主业环节（真受益）
    + 9  分：同链不同环节（链内外溢）
    − 12 分：泛标签（链级大标签，无环节）
    −  8 分：申万行业名（行业是分类学，不是资金炒的题材）

错位度（归因 vs 主业）是本模块最重要的产出：
  主业驱动 / 链内外溢 / 跨链蹭概念 / 行业内跟涨 —— 它回答「这票今天涨，是它自己的生意被买，还是被别人的故事带走」。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from kwrule import REGION_TOKENS, SW_L1   # noqa: E402  地域/一级行业锚点唯一来源

# 强度分档（超额，%）：0.5 以下算噪声，4 以上算主升
def _sig_exc(x):
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - 0.3) / 3.2))


def _sig_rate(x):
    # 涨停率 0—12% 线性；>12% 视作满格（小环节里 5 只涨停就很炸）
    if x is None:
        return 0.0
    return max(0.0, min(1.0, x / 0.12))


def _sig_breadth(x):
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - 0.35) / 0.45))


def spec_factor(n):
    """特异度：真环节 1.0；过窄 0.6（本质个股行情）；大桶 0.55（抹平环节）。"""
    if n is None or n <= 0:
        return 0.0
    if n < 8:
        return 0.6
    if n <= 120:
        return 1.0
    if n <= 250:
        return 0.8
    return 0.55


def build_theme_stats(snapshot, tag_map, mkt_med, zt_th=9.7, min_members=3):
    """{题材: {n, eq, exc, zt, ztr, up, ups, amt}}；tag_map = {code: [题材...]}（已剔除地域/风格/行业）。

    强度一律用**成分股等权**自算，不依赖外部榜单——取不到榜单也不影响归因。
    """
    px = {r["code"]: r for r in snapshot if r.get("code")}
    acc = {}
    for c, tags in tag_map.items():
        p = px.get(c, {}).get("pct")
        if p is None:
            continue
        amt = px.get(c, {}).get("amount") or 0
        for t in tags:
            a = acc.setdefault(t, {"ps": [], "n": 0, "zt": 0, "amt": 0.0})
            a["ps"].append(p)
            a["n"] += 1
            a["amt"] += amt
            if p >= zt_th:
                a["zt"] += 1
    out = {}
    for t, a in acc.items():
        n = a["n"]
        if n < min_members:
            continue
        ps = a["ps"]
        eq = sum(ps) / n
        out[t] = {"n": n, "eq": round(eq, 2),
                  "exc": round(eq - (mkt_med or 0), 2),
                  "zt": a["zt"], "ztr": a["zt"] / n,
                  "up": sum(1 for x in ps if x > 0), "ups": sum(1 for x in ps if x > 0) / n,
                  "amt": round(a["amt"] / 1e8, 1)}
    return out


def attribute(tags, stats, chain_of, focus_rank, main_chain="", main_seg="",
              ind_tags=(), big_labels=None, main_approx=False):
    """给单只票算归因。返回 {main, alts, layer, approx, note, scored}

    main_approx 主业是不是**推的**而不是算出来的（申万行业兜底/材料化工关键词兜底）。
                为 True 时错位度只作参考——行业兜底那条链本来就跟着申万行业走，
                归因链撞上它是常态，此时标「主业驱动」等于拿假设当证据。

    tags       该票的题材标签（已去地域/风格）
    focus_rank {题材: 名次}——盘前 A0 焦点榜名次，无则空 dict
    main_chain/main_seg  主业链/环节（stock_profile 给的，可空）
    """
    big_labels = big_labels or set()
    scored = []
    for t in dict.fromkeys(tags):
        st = stats.get(t)
        if not st:
            continue
        sp = spec_factor(st["n"])
        s = (0.42 * _sig_exc(st["exc"]) + 0.18 * _sig_rate(st["ztr"])
             + 0.12 * _sig_breadth(st["ups"])
             + 0.10 * (1.0 if t in focus_rank else 0.0)
             + 0.18 * sp)
        s *= (0.6 + 0.4 * sp)          # 特异度再乘一道：大桶不能靠体量赢
        ch, sg = chain_of(t)
        anchor = ""
        if main_chain and ch == main_chain:
            if main_seg and sg == main_seg:
                s += 0.35                       # 主业同环节：这票涨就是因为它的生意被买
                anchor = "主业同环节"
            else:
                s += 0.15                       # 同链：资金在链内换环节
                anchor = "同链"
        if t in big_labels or (ch and not sg):
            s -= 0.12
        if t in ind_tags:
            s -= 0.08
        scored.append({"theme": t, "score": round(s * 100, 1), "chain": ch, "seg": sg,
                       "n": st["n"], "exc": st["exc"], "zt": st["zt"], "ztr": round(st["ztr"] * 100, 1),
                       "ups": round(st["ups"] * 100, 0), "anchor": anchor})
    scored.sort(key=lambda x: -x["score"])
    if not scored:
        # ── 代位显示（2026-10-24 用户要求：不允许出现未归因的空格子）──────────
        # 概念标签全军覆没的两种实测成因：
        #   ① 北交所（920xxx）东财板块串为空 → 一个概念都没有（大鹏工业/众诚科技）；
        #   ② 概念全是行业名+噪声（四通股份：家居用品/其他家居用品/近期摘帽）。
        # 旧版返回「无题材读数」= 空格子。现在按【行业三级名 → 主业链】顺序代位：
        # 显示的是身份（它是什么公司），并明示「代位、无资金读数」，不冒充归因结论。
        for t in reversed(list(ind_tags or ())):
            ch, sg = chain_of(t)
            if ch:
                return {"main": {"theme": t, "score": None, "chain": ch, "seg": sg,
                                 "n": None, "exc": None, "zt": None, "ztr": None,
                                 "ups": None, "anchor": "行业代位"},
                        "alts": [], "layer": "行业代位", "approx": False,
                        "note": "无可用概念标签，按申万行业代位显示（无资金读数，不是归因结论）",
                        "scored": []}
        if main_chain:
            return {"main": {"theme": main_seg or main_chain, "score": None,
                             "chain": main_chain, "seg": main_seg or "",
                             "n": None, "exc": None, "zt": None, "ztr": None,
                             "ups": None, "anchor": "主业代位"},
                    "alts": [], "layer": "主业代位", "approx": False,
                    "note": "无可用概念标签，按主业链代位显示（无资金读数，不是归因结论）",
                    "scored": []}
        return {"main": None, "alts": [], "layer": "无题材读数", "approx": False,
                "note": "该票无可用概念标签且主业未归链", "scored": []}
    for x in scored:
        x["score"] = round(min(x["score"], 100.0), 1)

    # ── 两段式选择（2026-10-24）：先证「今天买的是不是它自己的生意」，再谈外溢 ──
    # 一段：主业环节内当日最强的题材，只要它自己动了（≥55 分）就是答案——
    #       这时不该让『MLOps概念』这种 10 只票的窄概念靠 3.89% 超额抢走绿盟的身份。
    # 二段：主业环节今天没资金（<55），才允许归因到链外热点，并显式标成外溢/蹭概念。
    same = [x for x in scored if x["anchor"] == "主业同环节"]
    main = None
    if same and same[0]["score"] >= 55:
        main = same[0]
        # 环节同名优先：归因写着『VPN』不如写『网络安全』——同环节内若存在与环节同名的
        # 题材且分数不弱于榜首 85%，取它作代表（信息量更高、更好读）
        if main["seg"]:
            twin = [x for x in same if x["theme"] == main["seg"]]
            if twin and twin[0]["score"] >= main["score"] * 0.85:
                main = twin[0]
    if main is None:
        top = scored[0]
        # 跨环节时再做一次「环节同名」校正：若榜首是同环节的窄概念，仍优先环节正名
        twin = [x for x in scored if x["seg"] and x["theme"] == x["seg"]
                and x["score"] >= top["score"] * 0.9]
        main = twin[0] if twin else top
    alts = [x for x in scored if x is not main][:4]
    ch, sg = main["chain"], main["seg"]
    # 错位度
    if not main_chain:
        layer = "主业未归链"
    elif ch == main_chain and main_seg and sg == main_seg:
        layer = "主业驱动"
    elif ch == main_chain:
        layer = "链内外溢"
    elif ch and sg:
        layer = "跨链蹭概念"
    else:
        layer = "题材未归链"
    note = ""
    if layer in ("跨链蹭概念", "链内外溢"):
        note = f"主业{main_chain}·{main_seg or '—'}，今日归因{ch}·{sg or '—'}"
    # 主业是推出来的 → 错位度只能作参考，前端在标签后加「≈」
    approx = bool(main_approx and layer in ("主业驱动", "链内外溢", "跨链蹭概念"))
    if approx:
        note = (note + "；" if note else "") + "主业为行业/关键词兜底推定，错位度仅供参考"
    return {"main": main, "alts": alts, "layer": layer, "approx": approx, "note": note,
            "biz_hot": bool(same and same[0]["score"] >= 55),
            "scored": scored[:8]}


def load_tag_map(pool_dir, cfg, classify_tags=None, universe=None):
    """从股池缓存的 themes.json 构建 {code: [概念标签]} + {code: [行业标签]} + {code: 申万一级}。

    结构判据（2026-10-24 实测 5576 只全部成立）：东财『所属板块』串的前 3 个词固定是
    申万一级 / 二级 / 三级（如 002439 = 计算机 · 软件开发 · 横向通用软件），紧随其后才是
    地域板块（北京板块）与概念、风格标签。所以**以第一个『…板块』为界**切分：
      之前 = 行业（不能当题材用，否则『横向通用软件』这种 34 只的三级行业会冒充题材夺冠），
      之后 = 概念候选（再按 exclude_words / exclude_regex 剔掉风格、资金、交易状态）。
    没有『…板块』的串退回旧的正则判据。

    ★ 用全市场 5576 只的标签，而不是只用池内 rows——否则「不在池内的涨停票」一个概念都没有，
      归因只能退回 hybk 行业名，粒度立刻变粗。

    ★ universe 覆盖（2026-10-25）：传入个股大表（cache/theme_universe/universe.json，
      {code: {有效概念: [...], 申万: [...]}} 或 {code: [概念...]}）时，**概念候选改走大表**——
      东财串口径概念不全（北交所全空）且缺东财自家没有的板块（实测密尔克卫『智能物流』
      只有同花顺有），大表是东财 F10 BOARDTYPE ＋ 同花顺概念的双源并集。
      取不到（该 code 不在大表 / 有效概念为空）**静默回退 themes.json 分层**，
      盘后 pool_pipeline 等既有调用不传 universe → 行为与改动前逐字一致。
      分层逻辑本身不变：行业仍归 ind/ind1，概念仍归 con；大表只是把「概念」这份换了个更全的源。
    """
    p = os.path.join(pool_dir, "themes.json")
    if not os.path.exists(p):
        return {}, {}, {}

    def _uni_con(c):
        if not universe:
            return None
        v = universe.get(c)
        if v is None:
            return None
        if isinstance(v, dict):
            v = v.get("有效概念") or []
        return [x for x in v if x] or None
    raw = json.load(open(p, encoding="utf-8"))
    G = cfg.get("granularity", {})
    ex, suf = set(G.get("exclude_words", [])), tuple(G.get("exclude_suffix", []))
    import re
    regs = [re.compile(x) for x in G.get("exclude_regex", [])]

    def keep(t):
        if not t or t in ex:
            return False
        if suf and t.endswith(suf):
            return False
        if t in REGION_TOKENS:      # 「黑龙江」「内蒙古」不带走『板块』，不能靠后缀滤掉
            return False
        return not any(r.match(t) for r in regs)

    con, ind, ind1, src_of = {}, {}, {}, {}
    for c, v in raw.items():
        toks = [x for x in (v or "").split() if x]
        k = next((i for i, x in enumerate(toks) if x.endswith("板块")), None)
        if k is None:   # 内蒙古 / 黑龙江 不写「板块」二字 → 换地域名当锚点
            k = next((i for i, x in enumerate(toks) if x in REGION_TOKENS), None)
        if k is None and len(toks) >= 4 and toks[0] in SW_L1:
            k = 3       # 无地域标签的科创板/次新/大市值（实测 9 只）→ 前 3 词仍是申万一二三级
        if k is not None and 1 <= k <= 4:
            inds, rest = toks[:k], toks[k:]
        elif classify_tags is not None:
            inds, rest = classify_tags(v, set(), G)
        else:
            inds, rest = [], toks
        uc = _uni_con(c)                       # 大表概念优先（取不到 → 回退串口径）
        if uc is not None:
            con[c], src_of[c] = uc, "universe"
        else:
            con[c], src_of[c] = [t for t in rest if keep(t)], "themes.json"
        # 行业层：串口径为空（北交所全空）时用大表的申万一二三级补，保证主归属能剔行业名
        if not inds and isinstance((universe or {}).get(c), dict):
            inds = [x for x in ((universe[c].get("申万") or [])) if x]
        ind[c] = inds
        ind1[c] = inds[0] if inds else ""
    load_tag_map.last_src = src_of             # 供自检/报告说明本轮概念来自哪张表
    return con, ind, ind1


def big_label_set(cfg):
    s = set()
    for k, v in (cfg.get("chains") or {}).items():
        if k.startswith("_"):
            continue
        s.update(v.get("链级大标签") or [])
    return s


if __name__ == "__main__":
    import sys
    import pool_pipeline as P
    pool = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "cache", "pool_20261023")
    cfg = json.load(open(os.path.join(ROOT, "scripts", "pool_config.json"), encoding="utf-8"))
    snap = json.load(open(os.path.join(pool, "all_a_snapshot.json"), encoding="utf-8"))
    med = sorted([r["pct"] for r in snap if r.get("pct") is not None])
    med = med[len(med) // 2]
    con, ind, _ = load_tag_map(pool, cfg, P.classify_tags)
    stats = build_theme_stats(snap, con, med)
    print(f"题材读数 {len(stats)} 个 · 全市场中位 {med}%")
    for t in ["网络安全", "DeepSeek概念", "人工智能", "AI应用", "数据安全", "信创"]:
        print(" ", t, stats.get(t))
    r = attribute(con.get("002439", []), stats, lambda t: P.chain_of(t, cfg["chains"]),
                  {}, "软件与数据链", "网络安全", ind.get("002439", []), big_label_set(cfg))
    print("\n启明星辰:", json.dumps({k: r[k] for k in ("layer", "note")}, ensure_ascii=False))
    for x in r["scored"][:6]:
        print("   ", x["theme"], x["score"], x["chain"], x["seg"], f'n={x["n"]} exc={x["exc"]}')
