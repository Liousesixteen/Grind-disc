#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tushare Pro 接入（2026-10-26，2000 积分档）
--concepts  概念列表(3000)+成分 → rel_stock_concept / ts_concept
--lhb       龙虎榜 60 交易日 → fact_lhb / fact_lhb_seat
--holders   十大股东全量 → rel_holder_stock / dim_holder
--daily     近 250 交易日日线 → fact_snapshot
限速：0.32s/次（约 190 次/分，低于 200/分上限）
"""
import argparse, json, os, sys, time
import sqlite3
from data_runtime import ROOT as ROOT_PATH, ProClient, refresh_calendar, trade_dates as verified_dates, connect as runtime_connect, run_partition, now, iso_date
ROOT=str(ROOT_PATH)
DB=str(ROOT_PATH/'cache/kb/trading_kb.sqlite')
pro=ProClient()
SLEEP=0.32

def con():
    c=sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS ts_concept(ts_code TEXT PRIMARY KEY, name TEXT, src TEXT);
    CREATE TABLE IF NOT EXISTS dim_concept_external(concept TEXT, src TEXT, note TEXT, PRIMARY KEY(concept,src));
    CREATE TABLE IF NOT EXISTS dim_seat(seat TEXT PRIMARY KEY, n_stocks INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS fact_lhb(date TEXT, code TEXT, name TEXT, buy_amt REAL, sell_amt REAL, net_amt REAL, explanation TEXT, PRIMARY KEY(date,code));
    CREATE TABLE IF NOT EXISTS fact_lhb_seat(date TEXT, code TEXT, seat TEXT, side TEXT, amount REAL);
    CREATE TABLE IF NOT EXISTS dim_holder(holder TEXT PRIMARY KEY, kind TEXT);
    CREATE TABLE IF NOT EXISTS rel_holder_stock(holder TEXT, code TEXT, ratio REAL, end_date TEXT, PRIMARY KEY(holder,code,end_date));
    CREATE TABLE IF NOT EXISTS fact_snapshot(date TEXT, code TEXT, name TEXT, pct REAL, price REAL, amount REAL, turnover REAL, liangbi REAL, fmcap REAL, PRIMARY KEY(date,code));
    """)
    return c

def trade_dates(n=250):
    c=runtime_connect(DB)
    try:
        refresh_calendar(c,pro)
        end=now().date().isoformat()
        if now().hour<16:
            import datetime as dt
            end=(now().date()-dt.timedelta(days=1)).isoformat()
        return verified_dates(c,n,end)
    finally:c.close()

def concepts(c):
    df=pro.concept()
    print('概念列表', len(df), flush=True)
    for _,r in df.iterrows():
        c.execute("INSERT OR REPLACE INTO ts_concept VALUES(?,?,?)",(r['code'], r['name'], 'tushare'))
        c.execute("INSERT OR REPLACE INTO dim_concept_external VALUES(?,?,?)",(r['name'],'tushare','Tushare概念'))
    c.commit()
    ok=0
    for i,(_,r) in enumerate(df.iterrows(),1):
        try:
            d=pro.concept_detail(id=r['code'])
            for _,x in d.iterrows():
                code=str(x['ts_code']).split('.')[0]
                c.execute("INSERT OR IGNORE INTO rel_stock_concept VALUES(?,?,1)",(code, r['name']))
            ok+=1
        except Exception as e:
            if i<3: print('detail fail', str(e)[:60], flush=True)
        time.sleep(SLEEP)
        if i%200==0:
            c.commit(); print(f'  概念成分 {i}/{len(df)}', flush=True)
    c.commit(); print('概念成分完成', ok, flush=True)

def lhb(c, days=60):
    from ts_daily import lhb as ingest
    c.executescript(__import__('data_runtime').SCHEMA)
    failed=[]
    for d in trade_dates(days):
        if not run_partition(c,'lhb',d,lambda d=d:ingest(c,pro,d)):failed.append(d)
    if failed:raise RuntimeError(f'lhb: {len(failed)} partitions incomplete')

def holders(c):
    codes=[r[0] for r in c.execute("SELECT code FROM dim_stock")]
    for i,code in enumerate(codes,1):
        tscode=code+('.SH' if code[0] in '6' else ('.BJ' if code.startswith(('4','8','92')) else '.SZ'))
        try:
            d=pro.top10_holders(ts_code=tscode)
            for _,r in d.iterrows():
                nm=r['holder_name']
                c.execute("INSERT OR IGNORE INTO dim_holder VALUES(?,?)",(nm,'未核验'))
                c.execute("INSERT OR REPLACE INTO rel_holder_stock VALUES(?,?,?,?)",(nm, code, r.get('hold_ratio'), iso_date(r['end_date'])))
        except Exception as e:
            pass
        time.sleep(SLEEP)
        if i%200==0: c.commit(); print(f'  股东 {i}/{len(codes)}', flush=True)
    c.commit(); print('股东完成', flush=True)

def daily(c, days=250):
    from ts_daily import daily as ingest
    c.executescript(__import__('data_runtime').SCHEMA)
    failed=[]
    for d in trade_dates(days):
        if not run_partition(c,'daily',d,lambda d=d:ingest(c,pro,d)):failed.append(d)
    if failed:raise RuntimeError(f'daily: {len(failed)} partitions incomplete')

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    for k in ('--concepts','--lhb','--holders','--daily','--all'): ap.add_argument(k,action='store_true')
    a=ap.parse_args()
    c=con()
    if a.concepts or a.all: print('== 概念 ==', flush=True); concepts(c)
    if a.lhb or a.all: print('== 龙虎榜 ==', flush=True); lhb(c)
    if a.holders or a.all: print('== 股东 ==', flush=True); holders(c)
    if a.daily or a.all: print('== 日线 ==', flush=True); daily(c)
    c.close()
