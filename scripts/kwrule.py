#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关键词规则：归属绑定 + 共现门槛（身份识别「三层架构」第 1 层的两条守门规则）

为什么单独抽成一个模块：
  这两条规则要被三处用到——`stock_profile.deep_match`（主营项名 → 环节）、
  `pool_pipeline.chain_of`（题材名 → 链·环节）、`audit_config.py`（词表自检）。
  前两个跑在 .venv 里（带 requests），自检脚本要能用系统 python3 直接跑，
  如果规则写在 stock_profile 里，audit 就只能复制一份——**两份规则一定会漂移**，
  而漂移的规则比没有规则更危险（自检说通过，执行却是另一套）。
  所以这里只依赖标准库，谁都能 import。

两条规则解决「最长词优先」自己解决不了的两个死角：
  ① 同一个词登记在两个环节（服装/养殖/硅料/北斗/系统集成…共 18 个）→ 归属由 JSON
     书写顺序决定，同一份数据两次运行可能给出两个答案。绑定表把这类词钉死到一处。
  ② 有些词字少但语义不收敛（工程/租赁/电力/商业/食品/饲料）→ 它没有更长的词压着时就赢。
     门槛表要求它必须与限定词共现才算数，否则让位给下一个候选。

注意：绑定不是"人工指定答案"，是"消除不可复现"——它不改变业务判断，
      只保证同一个词永远指向同一个环节。
"""

# 分部报表口径的后缀：『电力板块』去掉后缀就是业务名本身，不该被门槛当成泛词误伤。
GATE_STRIP_SUFFIX = ("板块", "业务")

# 地域标签：东财『所属板块』串的写法是「申万一级 二级 三级 地域 …概念」，
# 地域绝大多数写成「北京板块」「贵州板块」，但**内蒙古与黑龙江两个例外**——
# 它们不带「板块」二字，直接写成「内蒙古」「黑龙江」。
# 后果不是"少一个标签"：锚点找不到 → 整只票的行业标签与概念标签全部切错
# （实测 64 只：『汽车』『医药生物』『黑龙江』被当成题材去打分，行业兜底整条失效）。
# 所以锚点判据必须是「以板块结尾 **或** 是地域名」，两处调用共用这一份表。
REGION_TOKENS = frozenset((
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西",
    "甘肃", "青海", "宁夏", "新疆", "台湾", "香港", "澳门",
))

# 申万一级行业（31 个）。用途：少数票（科创板/次新/大市值，实测 9 只）东财既不给
# 『…板块』也不给地域，标签串开头直接就是行业——此时前 3 词仍是申万一级/二级/三级，
# 但**必须先用一级行业名验证过才敢当锚点**，否则碰到以概念开头的串会切错。
SW_L1 = frozenset((
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器", "食品饮料",
    "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输", "房地产",
    "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰", "电力设备",
    "机械设备", "国防军工", "汽车", "计算机", "传媒", "通信", "银行",
    "非银金融", "美容护理", "石油石化", "环保", "煤炭",
))


def bind_ok(kw, cn, sn, bind):
    """该词在当前 (链, 环节) 处是否合法。无绑定、或正好是绑定的目标 → 合法。

    链级判断（sn 为空串）时只要求**链一致**，不比对环节。否则
    bind_ok('云计算', 'AI算力链', '', ...) 会拿『AI算力链·』去比『AI算力链·算力服务』，
    永远不相等 → 该词的链级 match 被整体过滤掉 → 整条链被 skip → 环节也扫不到，
    chain_of('云计算') 直接返回空。这个坑踩过一次，别再用 f"{cn}·{sn}" 一把梭。
    """
    t = (bind or {}).get(kw)
    if not t or "·" not in t:
        return True
    if not sn:
        return t.split("·", 1)[0] == cn
    return t == f"{cn}·{sn}"


def gate_ok(name, kw, gates):
    """泛词共现门槛：项名含该词但一个限定词都没有 → 不生效。

    两种写法可以单用也可以合用：
      `必须含` —— 白名单，一个都不含就不认（工程/电力/商业/食品/饲料）
      `除非含` —— 黑名单，含任一就不认（租赁：租赁公司普遍叫『XX租赁』，
                  用白名单会把『德润租赁』这种正主一起挡掉，只能反着排）
    项名恰好等于该词、或只是『该词 + 分部口径后缀』时放行——东财分部报表会写成
    『电力板块』『电力业务』，去掉后缀就是业务名本身（601991 大唐发电就栽在这里）。
    """
    g = (gates or {}).get(kw)
    if not g:
        return True
    n = (name or "").strip()
    if n == kw:
        return True
    core = n
    for s in GATE_STRIP_SUFFIX:
        if core.endswith(s) and len(core) > len(s):
            core = core[: -len(s)]
            break
    if core == kw:
        return True
    if any(w in n for w in (g.get("除非含") or [])):
        return False
    need = g.get("必须含") or []
    if not need:
        return True
    return any(w in n for w in need)


def chain_of(name, chains):
    """题材名 → (产业链, 环节)。**最长词优先**，同长按 config 书写顺序。

    为什么不再用「配置顺序首个命中」：短词会吃掉长词——
    `AI应用链·应用` 词表里有裸词 `AI`，它排在 `终端` 之前，于是
    『AI眼镜』『AI手机』『AIPC』全被归成「应用」，`终端` 环节永远吃不到东西。
    `deep_match` 早就是最长优先，这里补齐，静态层与归因层口径才一致。

    - 命中 exclude 的关键词则**跳过该链**（例：『HJT电池』含『电池』会被新能源车链抢走，
      但它是光伏技术，必须让给光储风链）；
    - 命中『链级大标签』（军工 / 半导体概念 / 人工智能 这类覆盖整条链的泛标签）时环节返回空串，
      由调用方标成『链级·需下钻』——它不能当作环节用，否则环节榜会被 400+ 只的大桶霸占。

    `_题材直连`（2026-10-24 Step3）：题材名 → 链·环节 显式表，**第一优先**。
    为什么：chain_of 的流程是「先命中链级 match、再查环节」，而很多真题材只够得上环节词、
    够不上 match 词——『PLC概念』里 PLC 在机器人链·控制的环节词表里，match 里却没有，
    于是整条链被跳过，落进「归因未归链」。往 match 里塞这些词会误伤静态层（主营项名匹配），
    所以单独建表、只作用于题材名这一层。
    """
    if not name:
        return "", ""
    direct = chains.get("_题材直连") or {}
    if name in direct and "·" in (direct.get(name) or ""):
        cn, sn = direct[name].split("·", 1)
        return cn, sn
    bind = chains.get("_归属绑定") or {}
    gates = chains.get("_共现门槛") or {}
    best = ("", "", 0, 0)          # (链, 环节, 链命中词长, 环节命中词长)
    for cn, cv in chains.items():
        if cn.startswith("_"):
            continue
        if any(k in name for k in cv.get("exclude", [])):
            continue
        hit = [k for k in cv.get("match", [])
               if k and k in name and bind_ok(k, cn, "", bind)]
        if not hit:
            continue
        cl = max(len(k) for k in hit)
        sn_best, sl = "", 0
        for sn, kws in (cv.get("环节") or {}).items():
            for kw in kws:
                if not kw or kw not in name:
                    continue
                if not bind_ok(kw, cn, sn, bind):
                    continue
                if not gate_ok(name, kw, gates):
                    continue
                if len(kw) > sl:
                    sn_best, sl = sn, len(kw)
        if (cl, sl) > (best[2], best[3]):      # 严格大于 → 同分保留先写的链
            best = (cn, sn_best, cl, sl)
    if not best[0]:
        return "", ""
    cn, sn = best[0], best[1]
    if sn:
        return cn, sn
    if any(k in name for k in chains[cn].get("链级大标签", [])):
        return cn, "（链级·需下钻）"
    return cn, ""
