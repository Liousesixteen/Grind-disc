#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名字族（炒名字 / 字辈 / 谐音 / 生肖 / 方位 / 数字）—— 2026-10-25，C 级
================================================================
只服务于看板排序：输入当日涨停池 [(code, name)]，输出
  {code: {族, 字, 族大小, 类型, 标签[]}}
口径来源（公开复盘案例，非本系统发明）：
  · 同字族：简称共享同一汉字——华字辈（2026-09 新华传媒重组点燃，全市场 25 只带华涨停）、
    龙字辈（2023 末，14 连板）、蛇字辈（2024 末）、牛字辈（2026-08）、东方（2024-11）；
  · 同词系：两字组合重复——「新华系」6 只（同款逻辑：买不到龙头找同款）；
  · 谐音族：同音字发散——羊年炒 羊/洋/阳/杨（天洋新材九天六板只因带「洋」）、
    蛇年炒 斯莱克（snake）、猴年炒 宏辉果蔬（香蕉＝猴子上游，属发散联想，本模块不做）；
  · 生肖/方位/数字：简称含生肖字、东南西北中、一至十。
只把「池子里 ≥2 只」的字/词当族——单只不构成族（一只票没有跟风盘，谈不上炒字辈）。
"""
import re

# 常见非炒作字（出现在大量简称里的行业/结构词，进族会把「科技/集团」当成字辈）
STOP = set(
    "股份科技集团电子生物医药智能新材能源电气环保机械建设发展传媒文化信息通信"
    "网络软件装备制造工业实业控股投资资本基金证券银行保险地产置业钢铁有色化工"
    "农业食品饮料酒纺织服饰家电汽车航空船舶铁路交通物流港口城市政园林旅游酒店"
    "商业贸易超市数据云计算机器光电半导体芯片通讯移动联通电信广播影视出版教育"
    "体育娱乐游戏动漫广告营销电商零售批发供应链仓储快递国际神州环球联合精密高"
    "中字号股份公司事业产业工程系统设备材料制品加工服务咨询管理运营开发科研院"
)

# 同音字族（谐音炒作的实际用字；再往下发散就是联想级，不做）
HOMO = {
    "羊": "羊", "洋": "羊", "阳": "羊", "杨": "羊", "扬": "羊", "佯": "羊", "养": "羊", "佯": "羊",
    "龙": "龙", "隆": "龙", "珑": "龙", "陇": "龙",
    "马": "马", "玛": "马", "码": "马", "蚂": "马",
    "蛇": "蛇", "舌": "蛇", "佘": "蛇",
    "牛": "牛", "纽": "牛", "钮": "牛", "扭": "牛",
    "虎": "虎", "琥": "虎",
    "兔": "兔", "图": "兔", "途": "兔",
    "猴": "猴", "侯": "猴", "候": "猴",
    "鸡": "鸡", "基": "鸡", "吉": "鸡",
    "狗": "狗", "苟": "狗",
    "猪": "猪", "珠": "猪", "朱": "猪", "诸": "猪",
    "鼠": "鼠", "数": "鼠", "属": "鼠",
    "东": "东", "冬": "东",
}
SX = set("鼠牛虎兔龙蛇马羊猴鸡狗猪")     # 生肖字
FW = set("东西南北中")                   # 方位字（日出东方/渤海化学的老梗）
SZ = set("一二三四五六七八九十")          # 数字玄学（三柏硕/三维化学/六国化工）
# 优先当「族」的炒作字（其余非停用字按频次取最高）
PRIORITY = "华龙蛇羊牛马虎兔猴鸡狗猪东西南北中一二三四五六七八九十"
_CJK = re.compile(r"[\u4e00-\u9fff]")


def compute(items):
    """items: [(code, name)] → {code: {...}}。样本＝当日涨停池（炒名字只在涨停群里成立）。"""
    names = {str(c): (n or "").strip() for c, n in (items or []) if c and n}
    if not names:
        return {}
    char_f, big_f = {}, {}
    for n in names.values():
        for ch in set(n):
            if ch in STOP or not _CJK.match(ch):
                continue
            char_f[ch] = char_f.get(ch, 0) + 1
        for i in range(len(n) - 1):
            bg = n[i:i + 2]
            if len(bg) < 2 or any(ch in STOP for ch in bg) or not all(_CJK.match(ch) for ch in bg):
                continue
            big_f[bg] = big_f.get(bg, 0) + 1
    clans = {ch: k for ch, k in char_f.items() if k >= 2}
    bigs = {bg: k for bg, k in big_f.items() if k >= 2}

    out = {}
    for c, n in names.items():
        tags = []
        for ch in n:
            fam = HOMO.get(ch)
            if fam and fam in SX and f"{fam}谐音" not in tags:
                tags.append(f"{fam}谐音")
            if ch in SX and f"生肖{ch}" not in tags:
                tags.append(f"生肖{ch}")
            if ch in FW and f"方位{ch}" not in tags:
                tags.append(f"方位{ch}")
            if ch in SZ and f"数字{ch}" not in tags:
                tags.append(f"数字{ch}")
        cand = [ch for ch in n if ch in clans]
        prim = ""
        for ch in PRIORITY:                      # 炒名字先看「炒作字」，再看普通重复字
            if ch in cand:
                prim = ch
                break
        if not prim and cand:
            prim = max(cand, key=lambda ch: clans[ch])
        bgs = [n[i:i + 2] for i in range(len(n) - 1) if n[i:i + 2] in bigs]
        if bgs:
            bg = max(bgs, key=lambda b: bigs[b])
            if f"{bg}系" not in tags:
                tags.append(f"{bg}系")
        if prim:
            out[c] = {"族": f"{prim}字辈", "字": prim, "族大小": clans[prim], "类型": "同字", "标签": tags}
        elif bgs:
            out[c] = {"族": f"{bg}系", "字": bg, "族大小": bigs[bg], "类型": "同词", "标签": tags}
        else:
            out[c] = {"族": "", "字": "", "族大小": 0, "类型": "", "标签": tags}
    return out


if __name__ == "__main__":
    import json, sys
    sys.path.insert(0, "scripts")
    rows = json.load(open("cache/pool_20261023/all_a_snapshot.json", encoding="utf-8"))
    import name_clan as _self
    from pankou import _limit_th
    zt = [(r["code"], r.get("name")) for r in rows
          if r.get("pct") is not None and r["pct"] <= 30.5
          and r["pct"] >= _limit_th(str(r["code"]), r.get("name"))]
    res = _self.compute(zt)
    fam = {}
    for c, v in res.items():
        if v["族"]:
            fam.setdefault(v["族"], []).append(next((n for k, n in zt if k == c), c))
    for k, v in sorted(fam.items(), key=lambda kv: -len(kv[1])):
        print(k, len(v), "：", "/".join(v))
