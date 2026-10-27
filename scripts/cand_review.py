#!/usr/bin/env python3
"""Candidate outcomes: exact signal day, exchange-calendar T+1/T+2, no invented prices.

Outputs are mark-to-market observations, not executable strategy P&L. Offline clicks
and incomplete/missing-adjustment observations are explicitly separated.
"""
import argparse
import datetime as dt
import csv
import glob
import json
from pathlib import Path
import sqlite3
from data_runtime import ROOT,DEFAULT_DB,iso_date,number,now


def evaluate(c,r):
    day=iso_date(r.get('data_date') or r['ts'][:10]);code=str(r['code'])
    entry=number(r.get('entry_price'))
    ts=r.get('ts','');clock=ts[11:16]
    live_click=ts[:10]==day and ('09:25'<=clock<='11:30' or '13:00'<=clock<='15:00')
    out={'日期':day,'代码':code,'名称':r.get('name'),'场景':r.get('scene'),
         '样本类型':'盘中观察' if live_click else '盘后回看',
         '状态':'待到期','触发价':entry,'收盘':None,'次日日期':None,'两日日期':None,
         '当日剩余%':None,'次日%':None,'两日累计%':None,'T+2累计%':None,
         '收益口径':'价格盯市，非已成交收益；不含费用；未复权'}
    quote=str(r.get('quote_time') or '')
    if quote and len(quote)>=14:
        try:
            quote_dt=dt.datetime.strptime(quote[:14],'%Y%m%d%H%M%S')
            click_dt=dt.datetime.strptime(ts[:19],'%Y-%m-%d %H:%M:%S')
            if abs((click_dt-quote_dt).total_seconds())>60:
                out['状态']='行情陈旧，不纳入信号收益';return out
        except ValueError:
            out['状态']='行情时间格式未验证';return out
    if entry is None or entry<=0:
        out['状态']='缺有效触发价';return out
    if not live_click:
        out['状态']='盘后记录不纳入信号收益';return out
    cal=c.execute("SELECT is_open FROM trade_calendar WHERE exchange='SSE' AND cal_date=?",(day,)).fetchone()
    if not cal or cal[0]!=1:
        out['状态']='缺有效交易日历';return out
    q=c.execute('SELECT price FROM fact_snapshot WHERE date=? AND code=?',(day,code)).fetchone()
    if not q or not number(q[0]):
        out['状态']='待当日收盘';return out
    close0=float(q[0]);out['收盘']=close0;out['当日剩余%']=round((close0/entry-1)*100,4)
    future=[x[0] for x in c.execute("SELECT cal_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date>? ORDER BY cal_date LIMIT 2",(day,))]
    out['次日日期']=future[0] if future else None;out['两日日期']=future[1] if len(future)>1 else None
    prices=[]
    for d in future:
        if d>now().date().isoformat():prices.append(None);continue
        p=c.execute('SELECT price FROM fact_snapshot WHERE date=? AND code=?',(d,code)).fetchone()
        prices.append(number(p[0]) if p else None)
    # Never jump past an absent T+1 row (suspension or missing ingestion).
    if not prices or prices[0] is None:
        out['状态']='待次交易日行情（含停牌/缺数）';return out
    factors=[]
    for d in [day,*future]:
        f=c.execute('SELECT adj_factor FROM fact_equity_daily WHERE date=? AND substr(ts_code,1,6)=?',(d,code)).fetchone()
        factors.append(number(f[0]) if f else None)
    adjusted=bool(factors[0] and len(factors)>1 and factors[1])
    p1=prices[0]*(factors[1]/factors[0] if adjusted else 1)
    out['次日%']=round((p1/close0-1)*100,4)
    out['两日累计%']=round((p1/entry-1)*100,4)
    out['状态']='T+1已到期'
    if adjusted:out['收益口径']='复权因子调整的价格盯市，非已成交收益；不含费用'
    if len(prices)>1 and prices[1] is not None:
        if adjusted and factors[2]:
            p2=prices[1]*factors[2]/factors[0]
            out['T+2累计%']=round((p2/entry-1)*100,4)
        elif not adjusted:
            out['T+2累计%']=round((prices[1]/entry-1)*100,4)
        out['状态']='T+2已到期'
    return out


def review(c,records):
    # Earliest actual timestamp per signal trading day, independent of file order/natural date.
    records=sorted(records,key=lambda r:r.get('ts',''))
    seen=set();out=[]
    for r in records:
        key=(iso_date(r.get('data_date') or r['ts'][:10]),str(r['code']))
        if key in seen:continue
        seen.add(key);out.append(evaluate(c,r))
    return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=str(DEFAULT_DB));ap.add_argument('--out');a=ap.parse_args()
    records=[]
    for f in sorted((ROOT/'watchlists').glob('盘中候选_*.jsonl')):
        for line in f.read_text().splitlines():
            if line.strip():records.append(json.loads(line))
    if not records:print('暂无有效候选记录');return 0
    c=sqlite3.connect(Path(a.db).resolve().as_uri()+'?mode=ro',uri=True)
    try:rows=review(c,records)
    finally:c.close()
    dest=Path(a.out) if a.out else ROOT/'reports'/f'候选复盘_修复版_{now():%Y%m%d}.csv'
    dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    print(f'{len(rows)}条 → {dest}；仅为观察样本，未验证可成交性/统计优势。')
    return 0


if __name__=='__main__':raise SystemExit(main())
