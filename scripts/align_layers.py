#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新数据层 → 坐标系 对齐 v1（2026-10-26）
同花顺1,694 / 开盘啦248 / 申万346 / 产品未映射 → 链·环节或8类目
方法：①名称词表 ②成分重合继承（≥0.5） ③8类目 ④未归则留清单
"""
import collections, json, re, sqlite3, sys
sys.path.insert(0, 'scripts')
import pool_pipeline as P
import concept_map as CM

cfg = json.load(open('scripts/pool_config.json', encoding='utf-8'))
c = sqlite3.connect('cache/kb/trading_kb.sqlite', timeout=300)
c.executescript("""
CREATE TABLE IF NOT EXISTS rel_ths_coord(ts_code TEXT PRIMARY KEY, name TEXT, category TEXT, chain_id TEXT, seg_id TEXT, rule TEXT, overlap REAL);
CREATE TABLE IF NOT EXISTS rel_kpl_coord(theme_name TEXT PRIMARY KEY, category TEXT, chain_id TEXT, seg_id TEXT, rule TEXT, overlap REAL);
CREATE TABLE IF NOT EXISTS rel_sw_coord(sw_l3 TEXT PRIMARY KEY, category TEXT, chain_id TEXT, seg_id TEXT, rule TEXT, overlap REAL);
CREATE TABLE IF NOT EXISTS align_audit(layer TEXT, total INT, by_name INT, by_member INT, by_category INT, unmapped INT, note TEXT);
""")
# 概念→坐标 & 倒排
cc = {r[0]: (r[1] or '', r[2] or '') for r in c.execute("SELECT concept, chain_id, seg_id FROM rel_concept_coord")}
code2con = collections.defaultdict(list)
for code, con in c.execute("SELECT code, concept FROM rel_stock_concept WHERE is_valid=1"):
    code2con[code].append(con)

def via_members(members):
    cnt = collections.Counter()
    for code in members:
        for x in code2con.get(code, ()):
            cnt[x] += 1
    if not cnt:
        return None, 0.0
    con, n = cnt.most_common(1)[0]
    return con, n / max(len(members), 1)

def norm(n):
    return re.sub(r'\((指数|板块)\)$', '', str(n or '')).strip()

def resolve(name, members):
    """→ (category, chain, seg, rule, overlap)"""
    nm = norm(name)
    ch, sg = P.chain_of(nm, cfg['chains'])
    if ch:
        return '产业题材', ch, (sg if sg and not str(sg).startswith('（') else ''), '名称', 1.0
    con, ratio = via_members(members)
    if con and ratio >= 0.5:
        ch2, sg2 = cc.get(con, ('', ''))
        if ch2:
            return '产业题材', ch2, sg2, f'成分继承:{con}', round(ratio, 2)
    m = CM.classify(nm)
    if m and m.get('类目') not in ('待归类', ''):
        return m['类目'], m.get('链', ''), m.get('环节', ''), '8类目', 0
    return '待归类', '', '', '未归', 0

def run(layer, items, table, cols):
    stat = collections.Counter(); unm = []
    for key, name, members in items:
        cat, ch, sg, rule, ov = resolve(name, members)
        c.execute(f"INSERT OR REPLACE INTO {table} VALUES({cols})",
                  (key, name, cat, ch, sg, rule, ov) if table == 'rel_ths_coord'
                  else (key, cat, ch, sg, rule, ov))
        stat[rule.split(':')[0] if rule.startswith('成分') else rule] += 1
        if cat == '待归类':
            unm.append(name)
    tot = len(items)
    by_name = sum(v for k, v in stat.items() if k == '名称')
    by_mem = sum(v for k, v in stat.items() if k.startswith('成分'))
    by_cat = stat.get('8类目', 0)
    un = len(unm)
    c.execute("INSERT INTO align_audit VALUES(?,?,?,?,?,?,?)", (layer, tot, by_name, by_mem, by_cat, un, ''))
    print(f'{layer}: 共{tot} | 名称{by_name} | 成分{by_mem} | 8类目{by_cat} | 未归{un}')
    return unm

# A 同花顺
ths_mem = collections.defaultdict(set)
for tc, code in c.execute("SELECT ths_code, code FROM rel_ths_member"):
    ths_mem[tc].add(code)
names = {r[0]: r[1] for r in c.execute("SELECT ts_code,name FROM dim_ths_board")}
un_a = run('同花顺', [(tc, names.get(tc, ''), mem) for tc, mem in ths_mem.items()], 'rel_ths_coord',
           '?,?,?,?,?,?,?')
# B 开盘啦
kpl = collections.defaultdict(set)
for th, code in c.execute("SELECT theme_name, code FROM fact_kpl WHERE date='2026-10-23'"):
    kpl[th].add(code)
un_b = run('开盘啦', [(th, th, mem) for th, mem in kpl.items()], 'rel_kpl_coord', '?,?,?,?,?,?')
# C 申万三级
sw_mem = collections.defaultdict(set)
for code, l3 in c.execute("SELECT code, sw_l3 FROM rel_stock_sw"):
    sw_mem[l3].add(code)
un_c = run('申万三级', [(l3, l3, mem) for l3, mem in sw_mem.items()], 'rel_sw_coord', '?,?,?,?,?,?')
# D 产品：未映射的用链词兜底
prods = [r[0] for r in c.execute("SELECT product FROM dim_product WHERE seg_id=''")]
fixed = 0
for p in prods:
    ch, sg = P.chain_of(p, cfg['chains'])
    if ch:
        c.execute("UPDATE dim_product SET chain_id=?, map_rule='链词兜底' WHERE product=?", (ch, p))
        fixed += 1
c.commit()
print(f'产品兜底：{len(prods)} 个未映射中，链词可定 {fixed} 个')
c.commit()
print('未归样例(同花顺前20):', un_a[:20])
print('未归样例(开盘啦):', un_b[:10])
