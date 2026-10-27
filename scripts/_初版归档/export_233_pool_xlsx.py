# -*- coding: utf-8 -*-
"""把 233 盘前股池 JSON 导出为待搬运的中间 xlsx（4 张表）。
用法: python export_233_pool_xlsx.py <pool_dir> [out.xlsx]
口径定义见 熔炼体系.md 3.6。
"""
import json, os, sys
from openpyxl import Workbook

D = sys.argv[1] if len(sys.argv) > 1 else "cache/pool_20261017"
OUT = sys.argv[2] if len(sys.argv) > 2 else f"{D}/csv/pool_new_sheets.xlsx"
rows = json.load(open(f"{D}/pool_final.json"))
meta = json.load(open(f"{D}/summary.json"))
data_date = meta["data_date"]

# ---- 板块名归一化（涨停池截断名 → 申万全名） ----
industry_names = {r["industry"] for r in rows if r["industry"]}


def norm(name):
    if not name or name == "-":
        return ""
    if name in industry_names:
        return name
    c = [n for n in industry_names if n.startswith(name)]
    if len(c) == 1:
        return c[0]
    c2 = [n for n in industry_names if name in n or n in name]
    return c2[0] if len(c2) == 1 else name


for r in rows:
    r["板块"] = norm(r["industry"]) or norm(r["hybk"]) or "其他"

# ---- 窗口 A：板块取用 ----
by_sec = {}
for r in rows:
    if r["池层"] in ("核心", "备选", "观察"):
        by_sec.setdefault(r["板块"], []).append(r)
agg = {}
for r in rows:
    g = agg.setdefault(r["板块"], {"zt20": 0, "zt_today": 0, "pcts": [], "amt": 0.0,
                                   "核心": 0, "备选": 0, "观察": 0})
    g["zt20"] += r["zt20"]
    if r["pct"] is not None and r["pct"] >= 9.7:
        g["zt_today"] += 1
    if r["pct"] is not None:
        g["pcts"].append(r["pct"])
    g["amt"] += (r["amount"] or 0) / 1e8
    if r["池层"] in ("核心", "备选", "观察"):
        g[r["池层"]] += 1
wa = []
for k, g in agg.items():
    lst = sorted(by_sec.get(k, []), key=lambda r: (-(r["达标数"] or 0), -(r["zt60"] or 0)))
    core = [f"{r['code']} {r['name']}" for r in lst if r["池层"] == "核心"][:6]
    bak = [f"{r['code']} {r['name']}" for r in lst if r["池层"] == "备选"][:6]
    arr = [x for x in g["pcts"] if x is not None]
    wa.append([k, g["zt20"], g["zt_today"], round(sum(arr) / len(arr), 2) if arr else "",
               round(g["amt"], 1), sum(g[x] for x in ("核心", "备选", "观察")),
               g["核心"], g["备选"], g["观察"], " / ".join(core), " / ".join(bak)])
wa.sort(key=lambda x: (-x[1], -x[2]))
# 「其他（未分类）」不是真实板块，排到最末，避免压在所有板块之上
misc = [x for x in wa if x[0] in ("其他", "其他（未分类）")]
wa = [x for x in wa if x[0] not in ("其他", "其他（未分类）")] + [
    ["其他（未分类·盘前忽略）"] + x[1:] for x in misc]

# ---- 窗口 B：全 A 明细 ----
order = {"核心": 0, "备选": 1, "观察": 2}
rows_sorted = sorted(rows, key=lambda r: (order.get(r["池层"], 9), -(r["zt60"] or 0), -(r["max_lb"] or 0)))


def y(v):
    return "是" if v is True else ("否" if v is False else "")


wb_rows = []
for r in rows_sorted:
    wb_rows.append([r["code"], r["name"], r["板块"], r["池层"], r["达标数"],
                    y(r["筛1_股性"]), y(r["筛2_辨识度"]), y(r["筛3_量价"]),
                    r["price"], r["pct"], r["turnover"], r["liangbi"],
                    round(r["fmcap"] / 1e8, 1) if r["fmcap"] else "",
                    round(r["mcap"] / 1e8, 1) if r["mcap"] else "",
                    r["zt20"], r["zt60"], r["zt120"], r["max_lb"], r["last_zt"], r["days_since_zt"],
                    r["lhb60"], r["hongfei"], r["avg_up"], r["avg_dn"], r["ret20"], r["ret60"],
                    y(r["above_ma20"]), y(r["above_ma60"]), r["pct_ytd"], r["pct60"], r["pe"], r["pb"]])

# ---- 窗口 C：老龙记忆（涨停集中在 61—120 日区间，近期不活跃但市场有记忆） ----
old = [r for r in rows
       if (r["zt120"] or 0) >= 3
       and (r["zt60"] or 0) <= 1
       and not (r["name"] and ("ST" in r["name"].upper() or "退" in r["name"]))
       and 25 <= (r["days_since_zt"] or 999) <= 250
       and r.get("above_ma20") is not None]
old.sort(key=lambda r: (-(r["zt120"] or 0), -(r["max_lb"] or 0)))
wc = [[r["code"], r["name"], r["板块"], r["zt120"], r["zt60"], r["zt20"], r["max_lb"],
       r["last_zt"], r["days_since_zt"], r["lhb60"], r["hongfei"], r["ret20"], r["ret60"],
       r["price"], round(r["fmcap"] / 1e8, 1) if r["fmcap"] else "", r["池层"] or "未入池"]
      for r in old]

spec = [
    ["数据日期", f"{data_date}（最近交易日收盘）；用于下一交易日盘前取用"],
    ["生成时间", meta["run_at"]],
    ["覆盖范围", f"全A {meta['snapshot_n']} 只；日线指标 {meta['kline_n']} 只（{round(meta['kline_n']/meta['snapshot_n']*100,1)}%）；龙虎榜 {meta['lhb_codes']} 只（近60交易日）"],
    ["池层结果", f"核心 {meta['tier_core']} / 备选 {meta['tier_backup']} / 观察 {meta['tier_watch']}；板块 {len(wa)} 个；老龙记忆 {len(wc)} 只"],
    ["窗口A用途", "当天资金进攻某板块 → 按板块直接取用池内个股（先核心池，再备选池）"],
    ["窗口B用途", "全A明细与三筛选字段；可按板块 / 池层 / 达标数筛选"],
    ["窗口C用途", "老龙记忆池：涨停集中在 61—120 日区间、近 60 日基本不活跃，但市场仍有记忆的票 = 233「过去资金喜欢攻击谁，下次同类行情仍值得先看」；对应旧版 120 日老龙窗口"],
    ["①股性（涨停基因）", "字段：近60日涨停次数、120日内最长连板。判定：近60日涨停≥2 次 或 最长连板≥3。等级：C级草案（阈值为本体系新增，233原文无任何数字）"],
    ["②辨识度（市场共识）", "字段：近60日龙虎榜上榜次数（人气代理）、最长连板、近60日涨停次数。判定：龙虎榜≥2 次 或 最长连板≥5 或 近60日涨停≥4 次。等级：C级草案；「龙虎榜=人气」本身是C级假设"],
    ["③量价（红肥绿瘦）", "字段：红肥绿瘦比 = 近60日阳线平均涨幅 / |阴线平均跌幅|。判定：比值≥1.2 且 近20日涨跌幅 > −8%（未破位）。等级：C级草案"],
    ["池层判定", "核心 = 三条全达标；备选 = 达标两条；观察 = 达标一条；ST / 退市整理 → 剔除。组织方式 B 级，阈值 C 级"],
    ["数据来源", "东财 push2 全A快照 / 东财 datacenter 龙虎榜 / 东财 push2ex 涨停池（仅板块归属与交叉校验）/ 新浪日线（涨停次数与红肥绿瘦由日线直算；腾讯 fqkline 与东财 push2his 本次被 WAF / 代理阻断，按 DATA_SOURCE_POLICY 走回退源）"],
    ["口径限制", "①日线为新浪不复权，除权日附近涨跌幅与红肥绿瘦比可能失真；②日线覆盖 5550/5923（93.7%），未覆盖者涨停与量价字段为空；③概念 / 题材维度未覆盖（仅行业口径）；④龙虎榜为人气代理，混有机构席位与公告冲击；⑤涨停按收盘涨停判定，未含盘中触板未封；⑥板块名按申万口径归一化，仍有少量残留"],
    ["证据等级", "全部阈值为本体系新增量化草案（233原文无任何数字），C 级待回测，不得单独触发交易"],
    ["使用纪律", "池层只决定候选资格、不决定仓位；仓位仍由熔炼体系第五章方程决定；环境系数 <0.4 时 233 路径不动手；板块进攻但池内无标的 → 默认不追（3.6 出口规则二）"],
    ["免责声明", "以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。"],
]

wbook = Workbook()
wbook.remove(wbook.active)


def add(title, header, data):
    ws = wbook.create_sheet(title)
    ws.append(header)
    for r in data:
        ws.append(r)
    return ws


add("窗口A·板块取用", ["板块", "近20日涨停家数", "当日涨停家数", "当日平均涨跌幅%", "当日成交额(亿)",
                       "池内个股数", "核心池", "备选池", "观察池", "核心池个股(前6)", "备选池个股(前6)"], wa)
add("窗口B·全A股池明细", ["代码", "名称", "板块", "池层", "达标数", "筛1_股性", "筛2_辨识度", "筛3_量价",
                          "现价", "当日涨跌幅%", "换手率%", "量比", "流通市值(亿)", "总市值(亿)",
                          "近20日涨停", "近60日涨停", "近120日涨停", "最长连板", "最近涨停日", "距最近涨停(日)",
                          "近60日龙虎榜", "红肥绿瘦比", "阳线均值%", "阴线均值%", "20日涨幅%", "60日涨幅%",
                          "站上20日线", "站上60日线", "年初至今%", "东财60日涨幅%", "PE(动)", "PB"], wb_rows)
add("窗口C·老龙记忆", ["代码", "名称", "板块", "近120日涨停", "近60日涨停", "近20日涨停", "最长连板",
                       "最近涨停日", "距最近涨停(日)", "近60日龙虎榜", "红肥绿瘦比", "20日涨幅%", "60日涨幅%",
                       "现价", "流通市值(亿)", "池层"], wc)
add("口径与来源说明", ["项", "内容"], spec)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
wbook.save(OUT)
print(f"窗口A 板块 {len(wa)} 行 / 窗口B {len(wb_rows)} 行 / 窗口C 老龙记忆 {len(wc)} 行 / 口径 {len(spec)} 行")
print("输出:", OUT)
