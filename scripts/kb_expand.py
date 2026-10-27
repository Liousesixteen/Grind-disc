#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库扩展 v1（2026-10-25）——把「剩余的下一步」一次补齐
================================================================
子命令（可单独跑，也可 --all）：
  --multi-day   6 个盘后池快照 → fact_snapshot（多日事实）
  --lhb         龙虎榜（当日全量 + 营业部明细）→ fact_lhb / dim_seat / rel_edge(seat→stock)
  --holders N   十大股东（成交额前 N 只）→ dim_holder / rel_holder_stock
  --seg-cand    L3 环节候选（从产品层聚类）→ dim_segment_candidate（待评审）
  --events      涨停池公告标题抽取（中标/合同/采购/供应）→ rel_event（B 级，待核）
  --concepts    同花顺概念页 → dim_concept_external（扩源候选）
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "cache", "kb", "trading_kb.sqlite")
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
     "Referer": "https://data.eastmoney.com/"}
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ANN = "https://np-anotice-stock.eastmoney.com/api/security/ann"

DDL = """
CREATE TABLE IF NOT EXISTS dim_seat(seat TEXT PRIMARY KEY, n_stocks INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS fact_lhb(
  date TEXT, code TEXT, name TEXT, buy_amt REAL, sell_amt REAL, net_amt REAL,
  explanation TEXT, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS fact_lhb_seat(
  date TEXT, code TEXT, seat TEXT, side TEXT, amount REAL);
CREATE TABLE IF NOT EXISTS dim_holder(holder TEXT PRIMARY KEY, kind TEXT);
CREATE TABLE IF NOT EXISTS rel_holder_stock(
  holder TEXT, code TEXT, ratio REAL, end_date TEXT, PRIMARY KEY(holder, code, end_date));
CREATE TABLE IF NOT EXISTS dim_segment_candidate(
  candidate TEXT, chain_id TEXT, seg_id TEXT, n_products INT, examples TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS dim_concept_external(
  concept TEXT, src TEXT, note TEXT, PRIMARY KEY(concept, src));
"""


def con():
    conn = sqlite3.connect(DB)
    conn.executescript(DDL)
    return conn


def get(url, params, retries=2, timeout=10):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, headers=H, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(0.6)
    return None


def multi_day(conn):
    """多日事实：cache/pool_YYYYMMDD/all_a_snapshot.json → fact_snapshot"""
    base = os.path.join(ROOT, "cache")
    n = 0
    for d in sorted(os.listdir(base)):
        if not re.fullmatch(r"pool_\d{8}", d):
            continue
        p = os.path.join(base, d, "all_a_snapshot.json")
        if not os.path.exists(p):
            continue
        date = f"{d[5:9]}-{d[9:11]}-{d[11:13]}"
        snap = json.load(open(p, encoding="utf-8"))
        for r in snap:
            conn.execute("INSERT OR REPLACE INTO fact_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
                         (date, r.get("code"), r.get("name"), r.get("pct"), r.get("price"),
                          r.get("amount"), r.get("turnover"), r.get("liangbi"), r.get("fmcap")))
        n += len(snap)
        print(f"  {date}: {len(snap)} 行")
    conn.commit()
    print(f"多日事实：累计写入 {n} 行；库内日期 {conn.execute('SELECT COUNT(DISTINCT date) FROM fact_snapshot').fetchone()[0]} 天")


def lhb(conn, date="2026-10-23"):
    """龙虎榜：股票级 + 营业部明细（RPT_BILLBOARD_DAILYDETAILSBUY）"""
    got = 0
    page = 1
    while True:
        d = get(DC, {"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW", "columns": "ALL",
                     "pageNumber": page, "pageSize": 500,
                     "sortColumns": "TRADE_DATE", "sortTypes": -1,
                     "filter": f"(TRADE_DATE='{date}')"})
        rows = ((d or {}).get("result") or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            conn.execute("INSERT OR REPLACE INTO fact_lhb VALUES(?,?,?,?,?,?,?)",
                         (date, r.get("SECURITY_CODE"), r.get("SECURITY_NAME_ABBR"),
                          r.get("BILLBOARD_BUY_AMT"), r.get("BILLBOARD_SELL_AMT"),
                          r.get("BILLBOARD_NET_AMT"), r.get("EXPLANATION") or r.get("EXPLAIN")))
        got += len(rows)
        if page >= ((d.get("result") or {}).get("pages") or 1):
            break
        page += 1
    print(f"龙虎榜股票级：{got} 行")
    # 营业部明细（席位）
    seat_n, page = 0, 1
    while True:
        d = get(DC, {"reportName": "RPT_BILLBOARD_DAILYDETAILSBUY", "columns": "ALL",
                     "pageNumber": page, "pageSize": 500,
                     "sortColumns": "TRADE_DATE", "sortTypes": -1,
                     "filter": f"(TRADE_DATE='{date}')"})
        rows = ((d or {}).get("result") or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            seat = r.get("OPERATEDEPT_NAME") or ""
            code = r.get("SECURITY_CODE")
            if not seat or not code:
                continue
            amt = r.get("BUY_AMOUNT") or r.get("BUY") or 0
            conn.execute("INSERT INTO fact_lhb_seat VALUES(?,?,?,?,?)", (date, code, seat, "买入", amt))
            conn.execute("INSERT OR IGNORE INTO dim_seat VALUES(?,0)", (seat,))
            seat_n += 1
        if page >= ((d.get("result") or {}).get("pages") or 1):
            break
        page += 1
    # 席位→股票边 + 席位覆盖数
    conn.execute("""UPDATE dim_seat SET n_stocks=(
        SELECT COUNT(DISTINCT code) FROM fact_lhb_seat s WHERE s.seat=dim_seat.seat)""")
    conn.execute("DELETE FROM rel_edge WHERE rel_type='seat_stock'")
    conn.execute("""INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,weight,weight_unit,
                    source,confidence,created_at)
        SELECT 'seat',seat,'seat_stock','stock',code,'买入',SUM(amount),'元','龙虎榜','A',
               datetime('now','localtime') FROM fact_lhb_seat GROUP BY seat,code""")
    conn.commit()
    print(f"营业部明细：{seat_n} 行｜席位 {conn.execute('SELECT COUNT(*) FROM dim_seat').fetchone()[0]} 个"
          f"｜同席边 {conn.execute(chr(83)+'ELECT COUNT(*) FROM rel_edge WHERE rel_type='+chr(39)+'seat_stock'+chr(39)).fetchone()[0]}")


def holders(conn, limit=300):
    """十大股东：成交额前 N 只（最新报告期）"""
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM fact_snapshot WHERE date=(SELECT MAX(date) FROM fact_snapshot) "
        "ORDER BY amount DESC LIMIT ?", (limit,))]
    done = 0

    def one(code):
        d = get(DC, {"reportName": "RPT_F10_EH_FREEHOLDERS", "columns": "ALL",
                     "pageNumber": 1, "pageSize": 10,
                     "filter": f'(SECURITY_CODE="{code}")'})
        return code, (((d or {}).get("result") or {}).get("data") or [])

    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(one, c) for c in codes]):
            code, rows = f.result()
            for r in rows:
                nm = r.get("HOLDER_NAME") or ""
                if not nm:
                    continue
                conn.execute("INSERT OR IGNORE INTO dim_holder VALUES(?,?)",
                             (nm, "机构" if r.get("IS_HOLDORG") else "个人"))
                conn.execute("INSERT OR REPLACE INTO rel_holder_stock VALUES(?,?,?,?)",
                             (nm, code, r.get("HOLD_RATIO"), (r.get("END_DATE") or "")[:10]))
            if rows:
                done += 1
    conn.commit()
    print(f"十大股东：覆盖 {done}/{len(codes)} 只｜股东 {conn.execute('SELECT COUNT(*) FROM dim_holder').fetchone()[0]}"
          f"｜持股边 {conn.execute('SELECT COUNT(*) FROM rel_holder_stock').fetchone()[0]}")


def seg_candidates(conn):
    """L3 环节候选：每个环节下的产品聚合（n_stocks≥2），作为待评审的子环节候选"""
    conn.execute("DELETE FROM dim_segment_candidate")
    rows = conn.execute("""SELECT seg_id, chain_id, product, n_stocks FROM dim_product
                           WHERE seg_id<>'' AND n_stocks>=2 ORDER BY seg_id, n_stocks DESC""").fetchall()
    by = {}
    for seg, ch, prod, n in rows:
        by.setdefault((seg, ch), []).append((prod, n))
    cnt = 0
    for (seg, ch), arr in by.items():
        for prod, n in arr[:3]:                    # 每个环节取覆盖最广的 3 个产品作候选
            conn.execute("INSERT OR REPLACE INTO dim_segment_candidate VALUES(?,?,?,?,?,?)",
                         (prod, ch, seg, n, prod, "待评审"))
            cnt += 1
    conn.commit()
    print(f"L3 环节候选：{cnt} 个（覆盖 {len(by)} 个环节，待人工评审后并入 dim_segment）")


def events(conn, date="2026-10-23"):
    """供需事件：涨停池公告标题抽取（B 级，待核）"""
    codes = [r[0] for r in conn.execute("SELECT code FROM fact_limitup WHERE date=?", (date,))]
    pat = re.compile(r"(中标|合同|订单|采购|供应|客户|签署|战略合作|供货)")
    ORG = r"([\u4e00-\u9fa5]{2,12}(?:公司|集团|股份|研究院|大学|政府|局|厂|银行|电网))"
    CP_PATS = [re.compile(r"与" + ORG), re.compile(r"向" + ORG), re.compile(ORG + r"中标")]
    n = 0
    for code in codes:
        for page in (1, 2):
            d = get(ANN, {"sr": -1, "page_size": 50, "page_index": page, "ann_type": "A",
                          "client_source": "web", "stock_list": code})
            lst = ((d or {}).get("data") or {}).get("list") or []
            if not lst:
                break
            for a in lst:
                title = a.get("title") or ""
                nd = (a.get("notice_date") or "")[:10]
                if nd < "2026-07-01" or not pat.search(title):
                    continue
                m = None
                for _p in CP_PATS:
                    m = _p.search(title)
                    if m:
                        break
                conn.execute("INSERT OR REPLACE INTO rel_event VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (a.get("art_code"), "contract", nd, None, None,
                              m.group(1) if m else "", None, "公告标题", "B", title[:120]))
                n += 1
            time.sleep(0.05)
    conn.commit()
    print(f"供需事件（公告标题抽取）：{n} 条（B 级、待核；对手方从标题正则提取，未命中留空）")


def concepts(conn):
    """概念扩源：同花顺概念列表页（首页）→ 候选"""
    try:
        r = requests.get("http://q.10jqka.com.cn/gn/", headers=H, timeout=10)
        html = r.content.decode("gbk", errors="ignore")
    except Exception as e:
        print("同花顺概念页失败：", e)
        return
    names = set(re.findall(r'/gn/detail/code/\d+/"[^>]*>([^<]{2,12})<', html))
    names |= set(re.findall(r'<a[^>]+/gn/detail/code/\d+/[^>]*>([^<]{2,12})</a>', html))
    exist = {r[0] for r in conn.execute("SELECT concept FROM dim_concept")}
    new = sorted(n for n in names if n and n not in exist)
    for n in names:
        conn.execute("INSERT OR IGNORE INTO dim_concept_external VALUES(?,?,?)",
                     (n, "同花顺概念页", "扩源候选"))
    conn.commit()
    print(f"同花顺概念页：解析 {len(names)} 个概念｜库内已有 {len(names)-len(new)}｜新增候选 {len(new)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("--multi-day", "--lhb", "--holders", "--seg-cand", "--events", "--concepts", "--all"):
        ap.add_argument(k, action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    a = ap.parse_args()
    conn = con()
    if a.multi_day or a.all:
        print("== 多日事实 =="); multi_day(conn)
    if a.lhb or a.all:
        print("== 龙虎榜 =="); lhb(conn)
    if a.holders or a.all:
        print("== 十大股东 =="); holders(conn, a.limit)
    if a.seg_cand or a.all:
        print("== L3 环节候选 =="); seg_candidates(conn)
    if a.events or a.all:
        print("== 供需事件 =="); events(conn)
    if a.concepts or a.all:
        print("== 概念扩源 =="); concepts(conn)
    conn.close()
