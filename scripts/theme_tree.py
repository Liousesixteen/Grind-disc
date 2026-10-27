#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主题树：概念共现 → 社区发现（纯 Python 标签传播，零依赖）
输出：theme_cluster / theme_cluster_member
口径：只用「产业题材」概念；共现权重=共同持股数；归一化=共享数/min(两概念规模)
"""
import collections, json, sqlite3, sys, time

DB = 'cache/kb/trading_kb.sqlite'
c = sqlite3.connect(DB, timeout=600)
c.executescript("""
CREATE TABLE IF NOT EXISTS theme_cluster(
  cluster_id TEXT PRIMARY KEY, name TEXT, n_concepts INT, n_stocks INT,
  dominant_chain TEXT, purity REAL, zt_today INT, cohesion REAL, updated TEXT);
CREATE TABLE IF NOT EXISTS theme_cluster_member(
  cluster_id TEXT, concept TEXT, weight REAL, PRIMARY KEY(cluster_id, concept));
""")

# 1) 取产业题材概念（东财 699 里链非空 + 同花顺产业题材）
concept_chain = {}
for con, ch in c.execute("SELECT concept, chain_id FROM rel_concept_coord WHERE chain_id IS NOT NULL AND chain_id<>''"):
    concept_chain[con] = ch
for name, ch in c.execute("SELECT name, chain_id FROM rel_ths_coord WHERE category='产业题材' AND chain_id<>''"):
    concept_chain.setdefault(name, ch)
print('产业概念池:', len(concept_chain))

# 2) 概念→股票
c2s = collections.defaultdict(set)
for con, code in c.execute("SELECT concept, code FROM rel_stock_concept WHERE is_valid=1"):
    if con in concept_chain:
        c2s[con].add(code)
# 同花顺成分（只取产业题材概念）
for code, ths in c.execute("SELECT code, ths_code FROM rel_ths_member"):
    pass
for ths_code, code in c.execute("SELECT ths_code, code FROM rel_ths_member"):
    pass
for ths_code, name, con_code in c.execute(
        "SELECT m.ths_code, b.name, m.code FROM rel_ths_member m JOIN dim_ths_board b ON b.ts_code=m.ths_code"):
    if name in concept_chain:
        c2s[name].add(con_code)

# 只留有规模的
c2s = {k: v for k, v in c2s.items() if len(v) >= 5}
print('有效概念(≥5只):', len(c2s))

# 3) 共现边
edge = collections.Counter()
codes = collections.defaultdict(list)
for con, ss in c2s.items():
    if len(ss) > 800:      # 过大的泛概念不入图
        continue
    for code in ss:
        codes[code].append(con)
for code, cons in codes.items():
    if len(cons) < 2 or len(cons) > 25:
        continue
    cons = sorted(cons)
    for i in range(len(cons)):
        for j in range(i + 1, len(cons)):
            edge[(cons[i], cons[j])] += 1
print('共现边:', len(edge))

# 4) 归一化权重 + 邻接
adj = collections.defaultdict(dict)
for (a, b), w in edge.items():
    if w < 3:              # 至少 3 只共同持股
        continue
    jac = w / min(len(c2s[a]), len(c2s[b]))
    if jac < 0.06:
        continue
    adj[a][b] = jac
    adj[b][a] = jac
nodes = sorted(adj)
print('入图概念:', len(nodes), '| 平均度:', round(sum(len(adj[n]) for n in nodes) / max(len(nodes), 1), 1))

# 5) 标签传播（确定性：按加权度排序，平局按名字）
label = {n: n for n in nodes}
deg = {n: sum(adj[n].values()) for n in nodes}
order = sorted(nodes, key=lambda n: (-deg[n], n))
for it in range(25):
    changed = 0
    for n in order:
        score = collections.Counter()
        for m, w in adj[n].items():
            score[label[m]] += w
        if not score:
            continue
        best = max(score.items(), key=lambda kv: (kv[1], -order.index(kv[0]) if False else kv[1], kv[0]))[0]
        if best != label[n]:
            label[n] = best
            changed += 1
    if changed == 0:
        break
print('标签传播迭代结束, 变动', changed)

# 6) 社区聚合
comm = collections.defaultdict(list)
for n in nodes:
    comm[label[n]].append(n)

# 今日涨停（分板块阈值）
today = c.execute("SELECT MAX(date) FROM fact_snapshot").fetchone()[0]
ZT = ("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 "
      "WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)")
zt_codes = {r[0] for r in c.execute(
    "SELECT code FROM fact_snapshot WHERE date=? AND " + ZT, (today,))}
print('数据日', today, '| 涨停近似', len(zt_codes))

c.execute("DELETE FROM theme_cluster")
c.execute("DELETE FROM theme_cluster_member")
rows = []
for cid, members in comm.items():
    if len(members) < 4:
        continue
    chains = collections.Counter(concept_chain[m] for m in members if m in concept_chain)
    if not chains:
        continue
    dom, dn = chains.most_common(1)[0]
    purity = dn / len(members)
    stocks = set()
    for m in members:
        stocks |= c2s.get(m, set())
    zt = len(stocks & zt_codes)
    # 内聚度：社区内平均边权
    tot = cnt = 0
    ms = set(members)
    for m in members:
        for nb, w in adj[m].items():
            if nb in ms:
                tot += w; cnt += 1
    coh = round(tot / cnt, 3) if cnt else 0
    name = f'{dom}·{members[0]}等'
    rows.append((cid, name, len(members), len(stocks), dom, round(purity, 2), zt, coh,
                 time.strftime('%Y-%m-%d %H:%M')))
rows.sort(key=lambda x: (-x[6], -x[2]))
for r in rows:
    c.execute("INSERT OR REPLACE INTO theme_cluster VALUES(?,?,?,?,?,?,?,?,?)", r)
    for m in comm[r[0]]:
        c.execute("INSERT OR REPLACE INTO theme_cluster_member VALUES(?,?,?)",
                  (r[0], m, round(deg.get(m, 0), 2)))
c.commit()
print(f'\n社区 {len(rows)} 个（成员≥4）：')
for r in rows[:8]:
    print(f'  {r[1]:28s} 概念{r[2]:>3} 股票{r[3]:>4} 主导链={r[4]:<8} 纯度={r[5]} 今日涨停={r[6]} 内聚={r[7]}')
