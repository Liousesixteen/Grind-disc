#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C 历史回看：簇的 20 日涨停序列 → 持续加速 / 一日游 / 平稳 / 消退"""
import collections, sqlite3, os
from data_runtime import DEFAULT_DB
c=sqlite3.connect(os.environ.get('TRADING_DB',str(DEFAULT_DB)), timeout=60)
c.executescript("""CREATE TABLE IF NOT EXISTS theme_cluster_trend(
 cluster_id TEXT PRIMARY KEY, name TEXT, seq20 TEXT, today INT, avg5 REAL, avg15 REAL,
 trend TEXT, updated TEXT);""")
ZT=("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)")
dates=[r[0] for r in c.execute("SELECT DISTINCT f.date FROM fact_snapshot f JOIN trade_calendar t ON t.cal_date=f.date AND t.exchange='SSE' AND t.is_open=1 ORDER BY f.date DESC LIMIT 20")]
dates=sorted(dates)
zt={d:{r[0] for r in c.execute("SELECT code FROM fact_snapshot WHERE date=? AND "+ZT,(d,))} for d in dates}
cl=collections.defaultdict(set)
for cid,code in c.execute("SELECT cluster_id,code FROM theme_cross_stock"): cl[cid].add(code)
nm={r[0]:r[1] for r in c.execute("SELECT cluster_id,members FROM theme_cross_cluster")}
c.execute("DELETE FROM theme_cluster_trend")
rows=[]
for cid,mem in cl.items():
    seq=[len(zt[d]&mem) for d in dates]
    today=seq[-1]; a5=round(sum(seq[-5:])/len(seq[-5:]),2); a15=round(sum(seq[:-5])/len(seq[:-5]),2) if len(seq)>=15 else round(sum(seq)/len(seq),2)
    if a15>0 and a5>=a15*1.5 and sum(seq[-3:])>=3: t='持续加速'
    elif today>=3 and today>=a5*1.3: t='今日发力'
    elif today>=3: t='高位维持'
    elif max(seq[-5:])>=3 and today<=1: t='一日游后回落'
    elif a5< a15*0.6: t='消退'
    else: t='平稳'
    rows.append((cid,nm.get(cid,''),','.join(map(str,seq)),today,a5,a15,t,''))
rows.sort(key=lambda x:(-x[3],-x[4]))
for r in rows: c.execute("INSERT OR REPLACE INTO theme_cluster_trend VALUES(?,?,?,?,?,?,?,datetime('now','localtime'))",r[:7])
c.commit()
import datetime as dt
print(f'回看 {len(dates)} 天（{dates[0]}→{dates[-1]}）\\n')
print(f"{'簇':<30}{'今日':>4}{'5日均':>7}{'15日均':>7}  趋势")
for r in rows[:14]:
    print(f"{r[1][:28]:<30}{r[3]:>4}{r[4]:>7}{r[5]:>7}  {r[6]}")
print('\\n趋势分布:', collections.Counter(r[6] for r in rows).most_common())
