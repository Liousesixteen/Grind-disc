#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D 反向验证：跨链簇内涨停票的次日溢价 vs 市场
口径：T 日簇内涨停（收盘买入，理论值，实际涨停难成交）→ T+1 收盘卖出
基准：①全市场当日涨停票 ②全市场所有票
输出：分档统计 + 写入 bt_theme_result
"""
import collections, sqlite3, statistics as st, argparse, os
from data_runtime import DEFAULT_DB
ap=argparse.ArgumentParser(description='仅事后描述；不是可交易策略回测')
ap.add_argument('--descriptive-only',action='store_true');a=ap.parse_args()
if not a.descriptive_only:
    ap.error('旧结果已失效。尚无历史成分时态；仅允许 --descriptive-only，不能据此验证策略。')
c=sqlite3.connect(os.environ.get('TRADING_DB',str(DEFAULT_DB)), timeout=60)
c.executescript("""CREATE TABLE IF NOT EXISTS bt_theme_result(
 bucket TEXT, samples INT, avg_next REAL, win_rate REAL, avg_excess REAL, note TEXT);""")
ZT=("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)")
# 日线：按 code 取有序 (date, close, pct)
rows=c.execute("SELECT code,date,price,pct FROM fact_snapshot ORDER BY code,date").fetchall()
px=collections.defaultdict(list)
for code,d,cl,p in rows:
    if cl: px[code].append((d,cl,p or 0))
dates=sorted({r[1] for r in rows})
print('日线天数', len(dates), dates[0], '→', dates[-1])
# 次日收益表
nxt={}   # (code,date) -> T+1 收益%
for code,seq in px.items():
    for i in range(len(seq)-1):
        d0,c0,_=seq[i]; d1,c1,_=seq[i+1]
        nxt[(code,d0)]=(c1/c0-1)*100
# 每日涨停集合
zt={}
for code,seq in px.items():
    for d,cl,p in seq:
        thr=19.5 if (code[:2] in ('30','68')) else (29 if code[0] in '48' or code[:2] in ('92',) else 9.5)
        if p>=thr: zt.setdefault(d,set()).add(code)
# 簇成员
clusters=collections.defaultdict(set)
for cid,code in c.execute("SELECT cluster_id,code FROM theme_cross_stock"):
    clusters[cid].add(code)
names={r[0]:r[1] for r in c.execute("SELECT cluster_id,members FROM theme_cross_cluster")}
print('簇', len(clusters))

def stat(pairs):
    if not pairs: return (0,0,0,0)
    v=[x for x in pairs]
    return (len(v), round(sum(v)/len(v),2), round(sum(1 for x in v if x>0)/len(v)*100,1), 0)

# 基准：全市场涨停次日、全市场所有票次日
base_zt=[]; base_all=[]
for d in dates[:-1]:
    for code in zt.get(d,()):
        if (code,d) in nxt: base_zt.append(nxt[(code,d)])
for (code,d),v in nxt.items(): base_all.append(v)
b1=stat(base_zt); b2=stat(base_all)
print(f'\n基准①全市场涨停次日: 样本{b1[0]:>6} 均值{b1[1]:>6}% 胜率{b1[2]}%')
print(f'基准②全市场所有票次日: 样本{b2[0]:>6} 均值{b2[1]:>6}% 胜率{b2[2]}%')

# 簇内涨停：分档（当日簇内涨停数 1/2/3+/5+）
res=[]
for lo,label in ((1,'簇内涨停≥1'),(2,'簇内涨停≥2'),(3,'簇内涨停≥3'),(5,'簇内涨停≥5')):
    pairs=[]
    for d in dates[:-1]:
        z=zt.get(d,set())
        for cid,mem in clusters.items():
            hit=z & mem
            if len(hit)>=lo:
                for code in hit:
                    if (code,d) in nxt: pairs.append((code,d))
    pairs=[nxt[key] for key in set(pairs)]
    s=stat(pairs)
    exc=round(s[1]-b1[1],2) if s[0] else 0
    res.append((label,s[0],s[1],s[2],exc))
    print(f'{label}: 样本{s[0]:>6} 均值{s[1]:>6}% 胜率{s[2]:>5}% 超额(vs涨停){exc:>6}%')
c.execute("DELETE FROM bt_theme_result")
c.execute("INSERT INTO bt_theme_result VALUES(?,?,?,?,?,?)",('基准·全市场涨停',b1[0],b1[1],b1[2],0,'基准'))
c.execute("INSERT INTO bt_theme_result VALUES(?,?,?,?,?,?)",('基准·全市场所有票',b2[0],b2[1],b2[2],0,'基准'))
for r in res:
    c.execute("INSERT INTO bt_theme_result VALUES(?,?,?,?,?,?)",(r[0],r[1],r[2],r[3],r[4],'仅事后描述；当前成分回填；非策略验证'))
c.commit()
print('\n已写入 bt_theme_result')
# 分簇看：哪些簇的历史样本超额最高（≥2 次涨停的样本）
print('\n分簇表现（样本≥15）:')
per=[]
for cid,mem in clusters.items():
    pairs=[]
    for d in dates[:-1]:
        z=zt.get(d,set())
        if len(z&mem)>=2:
            for code in (z&mem):
                if (code,d) in nxt: pairs.append(nxt[(code,d)])
    if len(pairs)>=15:
        m=sum(pairs)/len(pairs); per.append((round(m-b1[1],2),round(m,2),len(pairs),names.get(cid,'')[:34]))
per.sort(reverse=True)
for x in per[:8]: print(f'  +{x[0]:>5}% (均值{x[1]}% n={x[2]}) {x[3]}')
