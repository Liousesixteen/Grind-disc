#!/usr/bin/env python3
"""ETF ingest with explicit target date, daily shares, atomic partitions and honest views."""
import argparse
import json
import sys
from data_runtime import (ROOT, DEFAULT_DB, ProClient, ProviderError, connect, date8,
                          iso_date, number, raw, run_partition, PartitionResult, target_date, trade_dates, job_lock)
from ts_daily import paged, require

DDL = '''
CREATE TABLE IF NOT EXISTS dim_etf(code TEXT PRIMARY KEY,name TEXT,extname TEXT,index_code TEXT,index_name TEXT,chain_id TEXT,seg_id TEXT,fund_type TEXT,mgr TEXT,list_date TEXT,m_fee REAL,c_fee REAL);
CREATE TABLE IF NOT EXISTS dim_etf_index(index_code TEXT PRIMARY KEY,index_name TEXT,pub_party TEXT,base_date TEXT);
CREATE TABLE IF NOT EXISTS rel_etf_cons(etf_code TEXT,con_code TEXT,con_name TEXT,qty REAL,sub_flag TEXT,PRIMARY KEY(etf_code,con_code));
CREATE TABLE IF NOT EXISTS fact_etf_daily(date TEXT,code TEXT,close REAL,pct REAL,amount REAL,nav REAL,net_asset REAL,premium REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS fact_etf_limit(date TEXT,code TEXT,up_limit REAL,down_limit REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS fact_etf_share(code TEXT,date TEXT,share REAL,PRIMARY KEY(code,date));
CREATE TABLE IF NOT EXISTS fact_etf_margin(date TEXT,code TEXT,rzye REAL,rqye REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS fact_margin(date TEXT,code TEXT,rzye REAL,rzmre REAL,rqye REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS fact_etf_pcf(date TEXT,etf_ts_code TEXT,con_ts_code TEXT,name TEXT,qty REAL,sub_flag TEXT,PRIMARY KEY(date,etf_ts_code,con_ts_code));
'''


def basic(c,pro,day):
    import pool_pipeline as P
    cfg=json.loads((ROOT/'scripts/pool_config.json').read_text())
    b=pro.etf_basic();idx=pro.etf_index()
    rows=require(b,['ts_code'],'etf_basic'); require(idx,['ts_code'],'etf_index')
    raw(c,'etf_basic',day,'basic',b);raw(c,'etf_basic',day,'indices',idx)
    for r in idx.to_dict('records'):
        c.execute('INSERT OR REPLACE INTO dim_etf_index VALUES(?,?,?,?)',(r['ts_code'],r.get('indx_csname') or r.get('indx_name'),r.get('pub_party_name'),r.get('base_date')))
    for r in rows:
        nm=str(r.get('extname') or r.get('cname') or '')
        ch,sg=P.chain_of(nm,cfg['chains'])
        c.execute('INSERT OR REPLACE INTO dim_etf VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (r['ts_code'].split('.')[0],nm,r.get('cname'),r.get('index_code'),r.get('index_name'),ch,sg,r.get('fund_type'),r.get('mgr_name'),r.get('list_date'),number(r.get('m_fee')),number(r.get('c_fee'))))
    return len(rows)


def daily(c,pro,day):
    q=paged(pro,'fund_daily',5000,trade_date=date8(day))
    nv=paged(pro,'fund_nav',2000,nav_date=date8(day))
    rows=require(q,['ts_code','close','pre_close','amount'],'fund_daily')
    require(nv,['ts_code','unit_nav'],'fund_nav',False)
    nav={r['ts_code']:(number(r.get('unit_nav')),number(r.get('net_asset'))) for r in nv.to_dict('records')}
    # A listed ETF's NAV may be reported using its registered .OF identifier.
    # Fallback only if all published observations for that bare code agree.
    nav_by_code={}
    for key,value in nav.items():nav_by_code.setdefault(key.split('.')[0],set()).add(value)
    nav_by_code={key:next(iter(values)) for key,values in nav_by_code.items() if len(values)==1}
    codes={r[0] for r in c.execute('SELECT code FROM dim_etf')}
    raw(c,'etf_daily',day,'prices',q);raw(c,'etf_daily',day,'nav',nv)
    d=iso_date(day);c.execute('DELETE FROM fact_etf_daily WHERE date=?',(d,));n=0
    for r in rows:
        code=r['ts_code'].split('.')[0]
        if code not in codes or not r['ts_code'].endswith(('.SH','.SZ')):continue
        u,na=nav.get(r['ts_code'],nav_by_code.get(code,(None,None)));cl=number(r['close']);pc=number(r['pre_close'])
        premium=(cl/u-1)*100 if cl is not None and u and u>0 else None
        pct=(cl/pc-1)*100 if cl is not None and pc and pc>0 else None
        amount=number(r['amount'])
        c.execute('INSERT INTO fact_etf_daily VALUES(?,?,?,?,?,?,?,?)',(d,code,cl,pct,amount*1000 if amount is not None else None,u,na,premium));n+=1
    missing=c.execute('SELECT COUNT(*) FROM fact_etf_daily WHERE date=? AND nav IS NULL',(d,)).fetchone()[0]
    return PartitionResult(n,'partial' if missing else 'ok',f'nav_missing={missing}/{n}')


def limit(c,pro,day):
    q=paged(pro,'etf_limit',5000,trade_date=date8(day));rows=require(q,['ts_code','up_limit','down_limit'],'etf_limit')
    raw(c,'etf_limit',day,'all',q);d=iso_date(day);c.execute('DELETE FROM fact_etf_limit WHERE date=?',(d,))
    for r in rows:c.execute('INSERT OR REPLACE INTO fact_etf_limit VALUES(?,?,?,?)',(d,r['ts_code'].split('.')[0],number(r['up_limit']),number(r['down_limit'])))
    return len(rows)


def share(c,pro,day):
    q=paged(pro,'fund_share',2000,trade_date=date8(day));rows=require(q,['ts_code','trade_date','fd_share'],'fund_share')
    codes={r[0] for r in c.execute('SELECT code FROM dim_etf')};d=iso_date(day)
    if any(iso_date(r['trade_date'])!=d for r in rows):raise ProviderError('fund_share: mismatched date')
    raw(c,'etf_share',day,'all',q);n=0
    for r in rows:
        code=r['ts_code'].split('.')[0];v=number(r['fd_share'])
        if code not in codes or v is None:continue
        c.execute('INSERT OR REPLACE INTO fact_etf_share VALUES(?,?,?)',(code,d,v));n+=1
    expected={r[0] for r in c.execute('SELECT code FROM fact_etf_daily WHERE date=?',(d,))}
    observed={r[0] for r in c.execute('SELECT code FROM fact_etf_share WHERE date=?',(d,))}
    if not expected or not expected<=observed:
        raise ProviderError('fund_share: daily active ETF coverage incomplete')
    return n


def margin(c,pro,day):
    q=paged(pro,'margin_detail',6000,trade_date=date8(day));rows=require(q,['ts_code','rzye','rzmre','rqye'],'margin_detail')
    d=iso_date(day);raw(c,'etf_margin',day,'all',q)
    c.execute('DELETE FROM fact_margin WHERE date=?',(d,));c.execute('DELETE FROM fact_etf_margin WHERE date=?',(d,))
    for r in rows:c.execute('INSERT OR REPLACE INTO fact_margin VALUES(?,?,?,?,?)',(d,r['ts_code'].split('.')[0],number(r['rzye']),number(r['rzmre']),number(r['rqye'])))
    c.execute('INSERT INTO fact_etf_margin SELECT date,code,rzye,rqye FROM fact_margin WHERE date=? AND code IN (SELECT code FROM dim_etf)',(d,))
    return len(rows)


def cons(c,pro,day):
    frames=[paged(pro,name,3000,trade_date=date8(day)) for name in ('etf_sh_cons','etf_sz_cons')]
    records=[]
    for name,q in zip(('sh','sz'),frames):
        records.extend(require(q,['ts_code','con_code','con_name','qty','sub_flag'],'etf_pcf_'+name))
    d=iso_date(day)
    for name,q in zip(('sh','sz'),frames):raw(c,'etf_cons',day,name,q)
    c.execute('DELETE FROM fact_etf_pcf WHERE date=?',(d,));c.execute('DELETE FROM rel_etf_cons')
    stock={r[0] for r in c.execute('SELECT code FROM dim_stock')}
    for r in records:
        c.execute('INSERT OR REPLACE INTO fact_etf_pcf VALUES(?,?,?,?,?,?)',(d,r['ts_code'],r['con_code'],r['con_name'],number(r['qty']),r['sub_flag']))
        code=r['con_code'].split('.')[0]
        if code in stock and r['con_code'].endswith(('.SH','.SZ','.BJ')):
            c.execute('INSERT OR REPLACE INTO rel_etf_cons VALUES(?,?,?,?,?)',(r['ts_code'].split('.')[0],code,r['con_name'],number(r['qty']),r['sub_flag']))
    return len(records)


VIEWS = '''
DROP VIEW IF EXISTS v_etf_flow;
CREATE VIEW v_etf_flow AS
WITH obs AS (
 SELECT code,date,share,LAG(date) OVER(PARTITION BY code ORDER BY date) prev_date,
 LAG(share) OVER(PARTITION BY code ORDER BY date) prev_share
 FROM fact_etf_share WHERE date<=date('now','+8 hours')
)
SELECT o.code,o.date,o.share,
 CASE WHEN o.prev_date=t.pretrade_date THEN o.share-o.prev_share END share_chg,
 o.prev_date,CASE WHEN o.prev_date=t.pretrade_date THEN 'adjacent' ELSE 'gap_or_first' END quality
FROM obs o LEFT JOIN trade_calendar t ON t.exchange='SSE' AND t.cal_date=o.date AND t.is_open=1;
DROP VIEW IF EXISTS v_etf_chain_flow;
CREATE VIEW v_etf_chain_flow AS
SELECT f.date,e.chain_id,SUM(f.share_chg*d.nav)/10000.0 AS 净申赎亿,
 COUNT(DISTINCT f.code) n_etf,'估算申赎对价；万份×元/份÷10000；非现金流实测' method
FROM v_etf_flow f JOIN dim_etf e ON e.code=f.code
JOIN fact_etf_daily d ON d.code=f.code AND d.date=f.date
WHERE e.chain_id<>'' AND f.share_chg IS NOT NULL AND d.nav>0
GROUP BY f.date,e.chain_id;
DROP VIEW IF EXISTS v_etf_premium;
CREATE VIEW v_etf_premium AS
SELECT d.date,d.code,e.name,d.close,d.nav,d.premium,e.chain_id
FROM fact_etf_daily d JOIN dim_etf e ON e.code=d.code
WHERE d.nav>0 AND ABS(d.premium)>=2 AND d.date<=date('now','+8 hours');
DROP VIEW IF EXISTS v_etf_momentum;
CREATE VIEW v_etf_momentum AS
WITH returns AS (
 SELECT d.*,ROW_NUMBER() OVER(PARTITION BY code ORDER BY date DESC) rn,
 COUNT(pct) OVER(PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) n5,
 COUNT(pct) OVER(PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) n20,
 SUM(LN(1+pct/100.0)) OVER(PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) r5,
 SUM(LN(1+pct/100.0)) OVER(PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) r20
 FROM fact_etf_daily d WHERE date<=date('now','+8 hours')
)
SELECT r.code,e.name,e.chain_id,r.date,
 CASE WHEN n5=5 THEN (EXP(r5)-1)*100 END avg5,
 CASE WHEN n20=20 THEN (EXP(r20)-1)*100 END avg20,
 '供应商除权涨幅复合；非含分红总回报' method
FROM returns r JOIN dim_etf e ON e.code=r.code WHERE rn=1;
DROP VIEW IF EXISTS v_etf_resonance;
CREATE VIEW v_etf_resonance AS
SELECT f.date,f.name stock,f.lb,e.name etf,e.chain_id
FROM fact_limitup f JOIN fact_etf_pcf p ON substr(p.con_ts_code,1,6)=f.code AND p.date=f.date
JOIN dim_etf e ON e.code=substr(p.etf_ts_code,1,6);
'''


def views(c):
    # DDL is transactional, including drops; an invalid replacement cannot remove working views.
    c.executescript('BEGIN;\n'+VIEWS+'\nCOMMIT;')


def main():
    ap=argparse.ArgumentParser()
    for k in ('basic','daily','limit','cons','share','margin','views','all'):ap.add_argument('--'+k,action='store_true')
    ap.add_argument('--date');ap.add_argument('--force',action='store_true');ap.add_argument('--days',type=int,default=1);ap.add_argument('--db',default=str(DEFAULT_DB));a=ap.parse_args()
    with job_lock('ts_etf'):
        c=connect(a.db);c.executescript(DDL);fail=[]
        try:
            requested=[k for k in ('basic','daily','limit','cons','share','margin') if getattr(a,k) or a.all]
            if requested:
                pro=ProClient();day=target_date(c,pro,a.date);days=trade_dates(c,a.days,day)
                for d in reversed(days):
                    for k in requested:
                        state=c.execute('SELECT status FROM ingest_partition WHERE source=? AND date=?',('etf_'+k,iso_date(d))).fetchone()
                        if state==('ok',) and not a.force:continue
                        if not run_partition(c,'etf_'+k,d,lambda k=k,d=d:globals()[k](c,pro,d)):fail.append((k,d))
            if a.views or a.all:views(c)
            return 1 if fail else 0
        finally:c.close()


if __name__=='__main__':sys.exit(main())
