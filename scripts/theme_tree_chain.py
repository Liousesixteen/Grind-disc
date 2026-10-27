#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题树 v2：链内社区发现（每链独立聚簇）——产出可用的二级题材簇
TODO[B-01] 待优化：簇名不可读/噪声未清净。详见 docs/待优化清单.md
"""
import collections, re, sqlite3, time
c=sqlite3.connect('cache/kb/trading_kb.sqlite', timeout=600)
c.executescript("""
CREATE TABLE IF NOT EXISTS theme_cluster(
  cluster_id TEXT PRIMARY KEY, name TEXT, n_concepts INT, n_stocks INT,
  dominant_chain TEXT, purity REAL, zt_today INT, cohesion REAL, updated TEXT);
""")
# 概念→链、概念→股票
c2ch={}
for con,ch in c.execute("SELECT concept,chain_id FROM rel_concept_coord WHERE chain_id<>''"): c2ch[con]=ch
for nm,ch in c.execute("SELECT name,chain_id FROM rel_ths_coord WHERE category='产业题材' AND chain_id<>''"): c2ch.setdefault(nm,ch)
c2s=collections.defaultdict(set)
for con,code in c.execute("SELECT concept,code FROM rel_stock_concept WHERE is_valid=1"):
    if con in c2ch: c2s[con].add(code)
for nm,code in c.execute("SELECT b.name,m.code FROM rel_ths_member m JOIN dim_ths_board b ON b.ts_code=m.ths_code"):
    if nm in c2ch: c2s[nm].add(code)
BAD=re.compile(r'(指数|\(A股\)|Ⅱ|Ⅲ|地方国企|行业|制造业|服务业|美国软件股|大型软件公司|软件股票)')
c2s={k:v for k,v in c2s.items() if 5<=len(v)<=800 and not BAD.search(k)}
today=c.execute("SELECT MAX(date) FROM fact_snapshot").fetchone()[0]
ZT=("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)")
zt={r[0] for r in c.execute("SELECT code FROM fact_snapshot WHERE date=? AND "+ZT,(today,))}
c.execute("DELETE FROM theme_cluster")
total=0; report={}
for chain in sorted(set(c2ch.values())):
    pool=[k for k,v in c2ch.items() if v==chain and k in c2s]
    if len(pool)<8: continue
    codes=collections.defaultdict(list)
    for con in pool:
        for code in c2s[con]: codes[code].append(con)
    edge=collections.Counter()
    for code,cons in codes.items():
        if len(cons)<2 or len(cons)>20: continue
        cons=sorted(cons)
        for i in range(len(cons)):
            for j in range(i+1,len(cons)): edge[(cons[i],cons[j])]+=1
    adj=collections.defaultdict(dict)
    for (a,b),w in edge.items():
        if w<3: continue
        jac=w/min(len(c2s[a]),len(c2s[b]))
        if jac<0.18: continue      # 链内更严：只连"本质同一题材"的概念
        adj[a][b]=jac; adj[b][a]=jac
    nodes=sorted(adj)
    if len(nodes)<8: continue
    deg={n:sum(adj[n].values()) for n in nodes}
    order=sorted(nodes,key=lambda n:(-deg[n],n))
    lab={n:n for n in nodes}
    for _ in range(20):
        ch=0
        for n in order:
            sc=collections.Counter()
            for m,w in adj[n].items(): sc[lab[m]]+=w
            if not sc: continue
            b=max(sc.items(),key=lambda kv:(kv[1],kv[0]))[0]
            if b!=lab[n]: lab[n]=b; ch+=1
        if ch==0: break
    comm=collections.defaultdict(list)
    for n in nodes: comm[lab[n]].append(n)
    cs=[]
    for cid,ms in comm.items():
        if len(ms)<3: continue
        stocks=set(); [stocks.update(c2s[m]) for m in ms]
        tot=cnt=0; s=set(ms)
        for m in ms:
            for nb,w in adj[m].items():
                if nb in s: tot+=w; cnt+=1
        coh=round(tot/cnt,2) if cnt else 0
        z=len(stocks & zt)
        # 簇名：取该簇内覆盖最广的概念
        top=sorted(ms,key=lambda m:-len(c2s[m]))[0]
        cs.append((f'{chain}#{len(cs)+1}',f'{chain}·{top}',len(ms),len(stocks),chain,1.0,z,coh,ms))
    cs.sort(key=lambda x:(-x[6],-x[3]))
    for r in cs:
        c.execute("INSERT OR REPLACE INTO theme_cluster VALUES(?,?,?,?,?,?,?,?,?)",r[:8]+(time.strftime('%Y-%m-%d %H:%M'),))
        total+=1
    report[chain]=cs[:3]
c.commit()
print(f'链内社区共 {total} 个\n')
for chain,cs in report.items():
    if not cs: continue
    print(f'【{chain}】')
    for r in cs:
        print(f'   {r[1]:30s} 概念{r[2]:>2} 股票{r[3]:>4} 涨停{r[6]:>2} 内聚{r[7]} | 成员: {"、".join(r[8][:6])}')
