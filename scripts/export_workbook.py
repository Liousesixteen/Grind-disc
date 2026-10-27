#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股全量库 → 多 sheet Excel（2026-10-25）
================================================================
把三个 CSV（个股全量 / 概念归位 / 链环节字典）＋ 每日强度 ＋ 名字族 ＋ 旧核对表
合成一个工作簿，供人工查阅。后续接知识库时直接读这个口径。

用法：
  python3 scripts/export_workbook.py --date 2026-10-25 --pool 20261023
"""
import argparse
import json
import os
import sys
from collections import Counter

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import concept_map as CM
import name_clan as NC

HDR_FILL = PatternFill("solid", fgColor="2F3B4C")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=13)


def sheet(wb, title, header, rows, widths=None, freeze="A2", filt=True):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in range(1, len(header) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for i, w in enumerate(widths or [], 1):
        if w:
            ws.column_dimensions[get_column_letter(i)].width = w
    if filt and rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{len(rows)+1}"
    if freeze:
        ws.freeze_panes = freeze
    ws.row_dimensions[1].height = 28
    return ws


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-25")
    ap.add_argument("--pool", default="20261023")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    uni = json.load(open(CM.UNI, encoding="utf-8"))
    cmap = json.load(open(CM.OUT_MAP, encoding="utf-8"))
    stats = {}
    p = os.path.join(ROOT, "cache", "theme_universe", "concept_stats.json")
    if os.path.exists(p):
        stats = json.load(open(p, encoding="utf-8"))
    _pd = f"{a.pool[:4]}-{a.pool[4:6]}-{a.pool[6:]}"      # 20261023 → 2026-10-23
    _dates = [a.date, _pd] if _pd != a.date else [a.date]
    strength, sconc = {}, {}
    for _d in _dates:
        p = os.path.join(ROOT, "cache", "theme_universe", f"strength_{_d}.json")
        if os.path.exists(p) and not strength:
            strength = json.load(open(p, encoding="utf-8"))
            a.strength_date = _d
        p = os.path.join(ROOT, "cache", "theme_universe", f"strength_{_d}_concepts.json")
        if os.path.exists(p) and not sconc:
            sconc = json.load(open(p, encoding="utf-8"))

    # 名字族（当日涨停池）
    snap = json.load(open(os.path.join(ROOT, "cache", f"pool_{a.pool}", "all_a_snapshot.json"),
                          encoding="utf-8"))
    from pankou import _limit_th
    zt = [(r["code"], r.get("name")) for r in snap
          if r.get("pct") is not None and r["pct"] <= 30.5
          and r["pct"] >= _limit_th(str(r["code"]), r.get("name"))]
    clans = NC.compute(zt)

    _sd = getattr(a, "strength_date", _pd)   # 强度列的真实数据日（池日期）
    wb = Workbook()
    ws = wb.active
    ws.title = "说明"
    ws["A1"] = "A股全量库（多 sheet）"
    ws["A1"].font = TITLE_FONT
    lines = [
        ("生成时间", f"{a.date}（个股/概念数据截至 {a.pool}；强度列来自 {_sd}）"),
        ("数据源", "东财 F10 BOARDTYPE（行业/地域/概念）＋ 同花顺概念；全市场 5576 只"),
        ("Sheet 1 个股全量", "5576 只：概念计数按 8 类目拆分、主业链·环节、当日强度、名字族"),
        ("Sheet 2 概念归位", "699 个概念：8 类目（产业/平台生态/地域/政策/资金/交易状态/事件/泛主题），待归类 0"),
        ("Sheet 3 链环节字典", "19 链 110 环节及词表规模（判定用，改词表须跑 audit_config --strict）"),
        ("Sheet 4 每日强度·概念", f"{_sd} 概念层读数（中位涨幅/相对超额/涨停数，家数门槛见表头）"),
        ("Sheet 5 名字族", f"当日涨停池 {len(zt)} 只的炒名字归族（同字/同词/谐音/生肖/方位/数字）；个股全量里的族列仅涨停池有值"),
        ("Sheet 6 旧核对表口径", "2026-10-25 上午版（只有 19 链一个出口，未映射列已作废，仅供对照）"),
        ("口径 · 行业", "申万（有官方标准，2021 版三级）"),
        ("口径 · 概念", "东财＋同花顺双源并集，按排除词/正则/地域/行业名过滤（非标准，但固定可复现）"),
        ("口径 · 题材", "当日有资金读数的概念（涨停家数/涨停率/相对超额）；主归属一票一桶"),
        ("口径 · 归位", "产业题材→链·环节；其余归平台生态/地域/政策/资金/交易状态/事件/泛主题，零盲区"),
        ("免责", "仅为数据整理，不构成投资建议；买卖判断以行情软件与 A 级纪律为准"),
    ]
    for i, (k, v) in enumerate(lines, 3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True, size=10)
        ws.cell(row=i, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 120

    # ── Sheet 1 个股全量 ──
    rows = []
    for code, v in uni.items():
        if code.startswith("_"):
            continue
        cats = Counter(cmap.get(t, {}).get("类目", "待归类") for t in (v.get("全部概念") or []))
        s = strength.get(code) or {}
        cl = clans.get(code) or {}
        rows.append([
            code, v.get("name", ""), v.get("申万一级", ""), v.get("申万二级", ""), v.get("申万三级", ""),
            "、".join(v.get("地域") or []), len(v.get("全部概念") or []), len(v.get("有效概念") or []),
            cats.get("产业题材", 0),
            cats.get("平台生态", 0) + cats.get("地域区域", 0) + cats.get("政策主题", 0),
            cats.get("资金持仓", 0) + cats.get("交易状态", 0) + cats.get("事件财务", 0) + cats.get("泛主题", 0),
            (v.get("主业链·环节") or ""), cl.get("族", ""), "、".join(cl.get("标签") or []),
            s.get("best_concept", ""), s.get("excess", ""), s.get("concept_zt", ""),
            s.get("rank_in_concept", ""), s.get("pct", ""),
            "丨".join(v.get("全部概念") or []), "丨".join(v.get("有效概念") or []),
        ])
    rows.sort(key=lambda r: (r[2] == "", r[2], r[0]))
    sheet(wb, "1 个股全量", [
        "代码", "名称", "申万一级", "申万二级", "申万三级", "地域", "概念数", "有效概念数",
        "产业题材数", "平台/地域/政策数", "资金/交易/事件数", "主业链·环节", "名字族", "族标签",
        f"{_sd}最相关概念", "概念相对超额%", "概念涨停数", "自身在概念内百分位", f"自身{_sd}涨幅%",
        "全部概念", "有效概念"], rows,
        widths=[9, 10, 10, 12, 14, 10, 7, 8, 8, 10, 10, 20, 10, 16, 16, 10, 8, 10, 10, 90, 50])

    # ── Sheet 2 概念归位 ──
    cnt = Counter()
    for code, v in uni.items():
        if code.startswith("_"):
            continue
        for t in (v.get("全部概念") or []):
            cnt[t] += 1
    rows = []
    for t, n in cnt.most_common():
        m = cmap.get(t, {})
        st = stats.get(t, {})
        rows.append([t, m.get("类目", ""), m.get("链", ""), m.get("环节", ""), m.get("主题域", ""),
                     m.get("子类", ""), m.get("规则", ""), n,
                     st.get("zt_today", ""), st.get("first_seen", ""), st.get("last_seen", ""),
                     st.get("zt_days", "")])
    sheet(wb, "2 概念归位", ["概念", "类目", "链", "环节", "主题域", "子类", "规则",
                            "覆盖票数", "当日涨停数", "首见日", "最近活跃日", "累计涨停日数"], rows,
          widths=[22, 10, 12, 12, 10, 8, 12, 9, 9, 11, 11, 11])
    ws2 = wb["2 概念归位"]
    for r in range(2, len(rows) + 2):
        if ws2.cell(row=r, column=2).value != "产业题材":
            for c in range(1, 13):
                ws2.cell(row=r, column=c).fill = PatternFill("solid", fgColor="2A2A30")

    # ── Sheet 3 链环节字典 ──
    rows = []
    for cname, cv in CM.CFG["chains"].items():
        if cname.startswith("_"):
            continue
        rows.append([cname, "", "", "", "丨".join((cv.get("match") or [])[:12])])
        for sname, words in (cv.get("环节") or {}).items():
            rows.append([cname, sname, len(words), "、".join(words), ""])
    sheet(wb, "3 链环节字典", ["链", "环节", "环节词数", "环节词", "链级 match 词（前 12）"], rows,
          widths=[12, 14, 8, 70, 60])

    # ── Sheet 4 每日强度·概念 ──
    rows = []
    cs = (sconc or {}).get("concepts") or []
    if isinstance(cs, dict):
        cs = [dict(v, concept=k) for k, v in cs.items()]
    for v in sorted(cs, key=lambda x: -(x.get("excess") or -99)):
        rows.append([v.get("concept"), v.get("n"), v.get("zt"), v.get("med"), v.get("mean"),
                     v.get("up_ratio"), v.get("excess")])
    sheet(wb, "4 每日强度·概念", ["概念", "成员数", "涨停数", "中位涨幅%", "均值%", "上涨占比", "相对超额%"],
          rows, widths=[22, 9, 9, 11, 9, 10, 11])
    ws4 = wb["4 每日强度·概念"]
    ws4.cell(row=1, column=7, value=f"口径：中位涨幅−全市场中位 {sconc.get('med_all')}%；"
                                    f"成员数门槛 {sconc.get('min_members')}").font = Font(italic=True, size=9)

    # ── Sheet 5 名字族 ──
    rows = []
    for code, v in clans.items():
        rows.append([v.get("族", ""), v.get("族大小", 0), next((n for c, n in zt if c == code), ""),
                     code, "、".join(v.get("标签") or [])])
    rows.sort(key=lambda r: (-r[1], r[0]))
    sheet(wb, "5 名字族", ["族", "族大小", "名称", "代码", "标签（谐音/生肖/方位/数字/同词系）"], rows,
          widths=[12, 8, 12, 9, 40])

    # ── Sheet 6 旧核对表口径（对照用）──
    import csv as _csv
    old = ""
    for _n in (f"全市场个股概念核对表_{a.pool}.csv",
               "全市场个股概念核对表_" + a.date.replace("-", "") + ".csv"):
        _p = os.path.join(ROOT, "reports", a.date, _n)
        if os.path.exists(_p):
            old = _p
            break
    if old and os.path.exists(old):
        with open(old, encoding="utf-8-sig") as f:
            rd = list(_csv.reader(f))
        sheet(wb, "6 旧核对表口径", rd[0], rd[1:], widths=[9] + [10] * (len(rd[0]) - 1))
        ws6 = wb["6 旧核对表口径"]
        ws6.cell(row=1, column=len(rd[0]) + 2,
                 value="⚠ 旧口径：只有 19 链一个出口，「未映射概念」列已作废；请以 Sheet 2 八类目为准"
                 ).font = Font(italic=True, color="C05050", size=9)

    out = a.out or os.path.join(ROOT, "reports", a.date, "全量库", f"A股全量库_{a.pool}.xlsx")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)
    print("已生成", out)
    print("sheets:", wb.sheetnames)
    return out


if __name__ == "__main__":
    main()
