#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨链新簇 v2（A）：强绑定跨链共现 → 主题簇 + 加速判据
口径：概念 8~250 只；跨链共现≥6 只且 overlap coefficient≥0.2；簇≥3 概念；今日涨停≥3

TODO[B-01] 待优化：参数未定标/簇ID不稳定/未回测/未接daily_run。详见 docs/待优化清单.md
"""
import collections, re, sqlite3, time, os, hashlib
from data_runtime import DEFAULT_DB, now
c=sqlite3.connect(os.environ.get('TRADING_DB', str(DEFAULT_DB)), timeout=60)
c.executescript("""CREATE TABLE IF NOT EXISTS theme_cross_stock(
 cluster_id TEXT, code TEXT, PRIMARY KEY(cluster_id, code));
CREATE TABLE IF NOT EXISTS theme_cross_cluster(
 cluster_id TEXT PRIMARY KEY, chains TEXT, n_chains INT, n_concepts INT, n_stocks INT,
 zt_today INT, zt_avg20 REAL, ratio REAL, cohesion REAL, members TEXT, updated TEXT);""")
BAD=re.compile(r'(指数|\(A股\)|Ⅱ|Ⅲ|地方国企|制造业|服务业|美国|香港|蓝筹|城市群|经济带|自贸|一带一路|同花顺|全A|高价股|创指|微盘|大盘股|中盘|小盘)')
c2ch={}
for con,ch in c.execute("SELECT concept,chain_id FROM rel_concept_coord WHERE chain_id<>''"): c2ch[con]=ch
for nm,ch in c.execute("SELECT name,chain_id FROM rel_ths_coord WHERE category='产业题材' AND chain_id<>''"): c2ch.setdefault(nm,ch)
c2ch={k:v for k,v in c2ch.items() if not BAD.search(k)}
c2s=collections.defaultdict(set)
for con,code in c.execute("SELECT concept,code FROM rel_stock_concept WHERE is_valid=1"):
    if con in c2ch: c2s[con].add(code)
for nm,code in c.execute("SELECT b.name,m.code FROM rel_ths_member m JOIN dim_ths_board b ON b.ts_code=m.ths_code"):
    if nm in c2ch: c2s[nm].add(code)
universe={r[0] for r in c.execute('SELECT code FROM dim_stock')}
c2s={k:(v & universe) for k,v in c2s.items() if 8<=len(v & universe)<=250}
codes=collections.defaultdict(list)
for con,ss in c2s.items():
    for x in ss: codes[x].append(con)
edge=collections.Counter()
for code,cons in codes.items():
    if len(cons)<2 or len(cons)>12: continue
    for i in range(len(cons)):
        for j in range(i+1,len(cons)):
            a,b=cons[i],cons[j]
            if c2ch[a]==c2ch[b]: continue
            edge[(a,b) if a<b else (b,a)]+=1
adj=collections.defaultdict(dict)
for (a,b),w in edge.items():
    if w<6: continue
    j=w/min(len(c2s[a]),len(c2s[b]))
    if j<0.2: continue
    adj[a][b]=j; adj[b][a]=j
nodes=sorted(adj)
deg={n:sum(adj[n].values()) for n in nodes}
order=sorted(nodes,key=lambda n:(-deg[n],n)); lab={n:n for n in nodes}
for _ in range(25):
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
dates=[r[0] for r in c.execute("SELECT DISTINCT f.date FROM fact_snapshot f JOIN trade_calendar t ON t.cal_date=f.date AND t.exchange='SSE' AND t.is_open=1 WHERE f.date<=date('now','+8 hours') ORDER BY f.date DESC LIMIT 21")]
ZT=("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)")
zt={d:{r[0] for r in c.execute("SELECT code FROM fact_snapshot WHERE date=? AND "+ZT,(d,))} for d in dates}
today=dates[0]; hist=dates[1:]
c.execute("DELETE FROM theme_cross_cluster"); c.execute("DELETE FROM theme_cross_stock")
c.execute("CREATE TABLE IF NOT EXISTS theme_cross_concept(cluster_id TEXT,concept TEXT,PRIMARY KEY(cluster_id,concept))")
c.execute("DELETE FROM theme_cross_concept")
rows=[]; row_ms={}; row_concepts={}
for cid,ms in comm.items():
    if len(ms)<3: continue
    chs=collections.Counter(c2ch[m] for m in ms)
    if len(chs)<2: continue
    st=set()
    for m in ms: st|=c2s[m]
    z=len(st&zt[today])
    hz=[len(st&zt[d]) for d in hist if d in zt]
    avg=round(sum(hz)/len(hz),1) if hz else 0
    ratio=round(z/avg,2) if avg else None
    tot=cnt=0; s=set(ms)
    for m in ms:
        for nb,w in adj[m].items():
            if nb in s: tot+=w; cnt+=1
    stable_id='X'+hashlib.sha256('\n'.join(sorted(ms)).encode()).hexdigest()[:16]
    row_ms[stable_id]=set().union(*(c2s[m] for m in ms))
    row_concepts[stable_id]=sorted(ms)
    rows.append((stable_id,'/'.join(k for k,_ in chs.most_common(3)),len(chs),len(ms),len(st),z,avg,ratio,
                 round(tot/cnt,2) if cnt else 0,'、'.join(sorted(ms,key=lambda m:-len(c2s[m]))[:8])))
rows.sort(key=lambda x:(-x[3],-x[8]))
# Metadata and membership share the SAME stable key; never zip independently sorted lists.
c.execute("CREATE TABLE IF NOT EXISTS theme_cross_history(asof TEXT,cluster_id TEXT,metadata TEXT,codes TEXT,PRIMARY KEY(asof,cluster_id))")
import json
for r in rows:
    members=row_ms[r[0]]
    c.executemany("INSERT INTO theme_cross_concept VALUES(?,?)",[(r[0],name) for name in row_concepts[r[0]]])
    assert len(members)==r[4], 'cluster membership mismatch'
    c.execute("INSERT OR REPLACE INTO theme_cross_cluster VALUES(?,?,?,?,?,?,?,?,?,?,?)",r+(now().isoformat(),))
    c.executemany("INSERT INTO theme_cross_stock VALUES(?,?)",[(r[0],code) for code in sorted(members)])
    c.execute("INSERT OR REPLACE INTO theme_cross_history VALUES(?,?,?,?)",(today,r[0],json.dumps(r,ensure_ascii=False),json.dumps(sorted(members))))
c.commit()
print(f'跨链结构簇 {len(rows)} 个（不按涨停过滤，展示时再筛）｜数据日 {today}')
for r in rows[:12]:
    print(f'  跨{r[2]}链 {r[1][:26]:<26} 概念{r[3]:>2} 股{r[4]:>4} 今日涨停{r[5]:>2}/均{r[6]:>4}')
