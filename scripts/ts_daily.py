#!/usr/bin/env python3
"""Tushare日增量：分区原子提交、逐源水位、完整题材、规范单位。"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from data_runtime import (ROOT, DEFAULT_DB, ProClient, ProviderError, connect, date8,
                          iso_date, mark, now, number, raw, run_partition, target_date, job_lock)


def require(frame, fields, label, nonempty=True):
    if not set(fields) <= set(frame.columns) or (nonempty and frame.empty):
        raise ProviderError(f'{label}: empty or invalid schema')
    return frame.to_dict('records')


def daily(c, pro, day):
    d = iso_date(day)
    q = pro.daily(trade_date=date8(day))
    b = pro.daily_basic(trade_date=date8(day))
    a = pro.adj_factor(trade_date=date8(day))
    rows = require(q, ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'vol', 'amount'], 'daily')
    basics = {r['ts_code']: r for r in require(b, ['ts_code', 'turnover_rate', 'volume_ratio', 'circ_mv'], 'daily_basic')}
    factors = {r['ts_code']: r for r in require(a, ['ts_code', 'adj_factor'], 'adj_factor')}
    if len(rows)>=6000:raise ProviderError('daily: row cap reached; split by market required')
    codes = {r['ts_code'] for r in rows}
    if not codes <= basics.keys() or not codes <= factors.keys():
        raise ProviderError('daily: basic/adjustment coverage mismatch')
    if any(iso_date(r['trade_date']) != d for r in rows):
        raise ProviderError('daily: mismatched data date')
    names = dict(c.execute('SELECT code,name FROM dim_stock'))
    raw(c, 'daily', day, 'daily', q); raw(c, 'daily', day, 'basic', b); raw(c, 'daily', day, 'adj_factor', a)
    # Full daily source replaces only this date. Legacy compatibility columns use CNY.
    c.execute('DELETE FROM fact_snapshot WHERE date=?', (d,))
    c.execute('DELETE FROM fact_equity_daily WHERE date=?', (d,))
    for r in rows:
        ts_code = r['ts_code']; code = ts_code.split('.')[0]; basic = basics[ts_code]
        amount = number(r['amount']); amount = amount * 1000 if amount is not None else None
        volume = number(r['vol']); volume = volume * 100 if volume is not None else None
        mv = number(basic['circ_mv']); mv = mv * 10000 if mv is not None else None
        c.execute('INSERT INTO fact_equity_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (d, ts_code, r['open'], r['high'], r['low'], r['close'], r['pre_close'],
                   r['pct_chg'], volume, amount, number(basic['turnover_rate']),
                   number(basic['volume_ratio']), mv, number(factors[ts_code]['adj_factor']), 'tushare'))
        c.execute('INSERT INTO fact_snapshot VALUES(?,?,?,?,?,?,?,?,?)',
                  (d, code, names.get(code, ''), r['pct_chg'], r['close'], amount,
                   number(basic['turnover_rate']), number(basic['volume_ratio']), mv))
    return len(rows)


def moneyflow(c, pro, day):
    q = pro.moneyflow(trade_date=date8(day))
    rows = require(q, ['ts_code','buy_sm_amount','sell_sm_amount','buy_md_amount','sell_md_amount',
                       'buy_lg_amount','sell_lg_amount','buy_elg_amount','sell_elg_amount','net_mf_amount'], 'moneyflow')
    if len(rows) >= 6000:
        raise ProviderError('moneyflow: row cap reached; partition required')
    d = iso_date(day); raw(c, 'moneyflow', day, 'all', q)
    c.execute('DELETE FROM fact_moneyflow WHERE date=?', (d,))
    for r in rows:
        c.execute('INSERT INTO fact_moneyflow VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (d, r['ts_code'].split('.')[0], *(number(r[k]) for k in
                   ['buy_sm_amount','sell_sm_amount','buy_md_amount','sell_md_amount',
                    'buy_lg_amount','sell_lg_amount','buy_elg_amount','sell_elg_amount','net_mf_amount'])))
    return len(rows)


def lhb(c, pro, day):
    tl = pro.top_list(trade_date=date8(day)); ti = pro.top_inst(trade_date=date8(day))
    stocks = require(tl, ['ts_code','reason','l_buy','l_sell','net_amount'], 'top_list', False)
    seats = require(ti, ['ts_code','exalter','side','reason','buy','sell','net_buy'], 'top_inst', False)
    if len(stocks) >= 10000 or len(seats) >= 10000:
        raise ProviderError('lhb: row cap reached')
    if not stocks and not seats:
        return 0  # Retain old published partition and mark pending, never delete on empty response.
    if not stocks or not seats:
        raise ProviderError('lhb: stock/seat publication incomplete')
    d = iso_date(day); d8 = date8(day)
    raw(c, 'lhb', day, 'top_list', tl); raw(c, 'lhb', day, 'top_inst', ti)
    for table in ('fact_lhb_detail','fact_lhb_seat_detail','fact_lhb','fact_lhb_seat'):
        c.execute(f'DELETE FROM {table} WHERE date IN (?,?)', (d, d8))
    for r in stocks:
        c.execute('INSERT OR REPLACE INTO fact_lhb_detail VALUES(?,?,?,?,?,?,?)',
                  (d,r['ts_code'],r.get('name',''),r.get('reason') or '',number(r['l_buy']),number(r['l_sell']),number(r['net_amount'])))
    for r in seats:
        side = str(r['side']).split('.')[0]
        if side not in ('0','1'):
            raise ProviderError('top_inst: unexpected side')
        c.execute('INSERT OR REPLACE INTO fact_lhb_seat_detail VALUES(?,?,?,?,?,?,?,?)',
                  (d,r['ts_code'],r['exalter'],side,r.get('reason') or '',number(r['buy']),number(r['sell']),number(r['net_buy'])))
    # Compatibility: choose one disclosed reason rather than summing overlapping periods.
    c.execute('''INSERT INTO fact_lhb
+      SELECT date,substr(ts_code,1,6),name,buy,sell,net,reason FROM
+      (SELECT *,ROW_NUMBER() OVER(PARTITION BY date,ts_code ORDER BY ABS(net) DESC,reason) rn
+       FROM fact_lhb_detail WHERE date=?) WHERE rn=1'''.replace('+',''), (d,))
    c.execute('''INSERT INTO fact_lhb_seat
+      SELECT date,substr(ts_code,1,6),seat,CASE side WHEN '0' THEN '买入' ELSE '卖出' END,
+        MAX(CASE side WHEN '0' THEN buy ELSE sell END)
+      FROM fact_lhb_seat_detail WHERE date=? GROUP BY date,ts_code,seat,side'''.replace('+',''), (d,))
    return len(stocks) + len(seats)


def kpl(c, pro, day):
    directory = pro.kpl_concept(trade_date=date8(day))
    themes = require(directory, ['ts_code','name','z_t_num','up_num'], 'kpl_concept')
    batches = []
    for theme in themes:
        frame = pro.kpl_concept_cons(trade_date=date8(day), ts_code=theme['ts_code'])
        members = require(frame, ['ts_code','con_code','name','con_name','trade_date','hot_num'], 'kpl_concept_cons', False)
        if len(members) >= 3000:
            raise ProviderError('kpl: individual theme reached row cap')
        if any(r['ts_code'] != theme['ts_code'] or iso_date(r['trade_date']) != iso_date(day) for r in members):
            raise ProviderError('kpl: theme/date partition mismatch')
        batches.append((theme, frame, members))
    # Publication is atomic after ALL theme requests have succeeded, including legal empty themes.
    d = iso_date(day); raw(c,'kpl',day,'directory',directory)
    c.execute('DELETE FROM fact_kpl_directory WHERE date=?',(d,))
    c.execute('DELETE FROM fact_kpl WHERE date=?',(d,))
    total = 0
    for t,frame,members in batches:
        c.execute('INSERT INTO fact_kpl_directory VALUES(?,?,?,?,?)',
                  (d,t['ts_code'],t['name'],number(t['z_t_num']),number(t['up_num'])))
        raw(c,'kpl',day,t['ts_code'],frame)
        for r in members:
            c.execute('INSERT OR REPLACE INTO fact_kpl VALUES(?,?,?,?,?,?,?)',
                      (d,t['ts_code'],t['name'],r['con_code'].split('.')[0],r['con_name'],number(r['hot_num']),number(t['z_t_num'])))
            total += 1
    return total


def paged(pro, api, cap, **params):
    """Reject repeated pages; never silently stop at a guessed total row limit."""
    import pandas as pd
    frames=[]; fingerprints=set()
    for page in range(1000):
        q = getattr(pro,api)(limit=cap, offset=page*cap, **params)
        if q.empty:
            return pd.concat(frames, ignore_index=True) if frames else q
        fingerprint = q.to_json(orient='split',index=False)
        if fingerprint in fingerprints:
            raise ProviderError(f'{api}: pagination repeated (offset unsupported)')
        fingerprints.add(fingerprint); frames.append(q)
        if len(q) < cap:
            return pd.concat(frames,ignore_index=True)
    raise ProviderError(f'{api}: pagination safety budget exceeded')


def ths(c, pro, day):
    boards = pro.ths_index()
    members = paged(pro,'ths_member',6000)
    br = require(boards,['ts_code','name','type'],'ths_index')
    mr = require(members,['ts_code','con_code','con_name'],'ths_member')
    raw(c,'ths',day,'boards',boards);raw(c,'ths',day,'members',members)
    c.execute('CREATE TABLE IF NOT EXISTS rel_ths_member_full(asof TEXT,ths_code TEXT,ts_code TEXT,name TEXT,PRIMARY KEY(asof,ths_code,ts_code))')
    for r in br:
        c.execute('INSERT OR REPLACE INTO dim_ths_board VALUES(?,?,?)',(r['ts_code'],r['name'],r['type']))
    c.execute('DELETE FROM rel_ths_member_full WHERE asof=?',(iso_date(day),))
    c.execute('DELETE FROM rel_ths_member')
    universe={r[0] for r in c.execute('SELECT code FROM dim_stock')}
    for r in mr:
        c.execute('INSERT OR REPLACE INTO rel_ths_member_full VALUES(?,?,?,?)',
                  (iso_date(day),r['ts_code'],r['con_code'],r['con_name']))
        code=r['con_code'].split('.')[0]
        if code in universe and r['con_code'].endswith(('.SH','.SZ','.BJ')):
            c.execute('INSERT OR REPLACE INTO rel_ths_member VALUES(?,?,?)',(r['ts_code'],code,r['con_name']))
    return len(mr)


SOURCES = {'daily':daily,'moneyflow':moneyflow,'lhb':lhb,'kpl':kpl,'ths':ths}


def run(c, day, pro=None, sources=None, force=False):
    pro = pro or ProClient(); sources = sources or list(SOURCES)
    failed=[]
    for source in sources:
        state=c.execute('SELECT status FROM ingest_partition WHERE source=? AND date=?',(source,iso_date(day))).fetchone()
        if state==('ok',) and not force:
            print(f'{day} {source}: already complete',flush=True);continue
        if not run_partition(c,source,day,lambda source=source:SOURCES[source](c,pro,day)):
            failed.append(source)
    c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('last_daily_run',now().isoformat()))
    if not failed:
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('last_data_date',iso_date(day)))
    c.commit()
    return failed


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date');ap.add_argument('--db',default=str(DEFAULT_DB))
    ap.add_argument('--sources',default=','.join(SOURCES));ap.add_argument('--force',action='store_true');a=ap.parse_args()
    sources=a.sources.split(',')
    if not set(sources)<=SOURCES.keys():ap.error('unknown source')
    with job_lock('ts_daily'):
        c=connect(a.db)
        try:
            pro=ProClient();day=target_date(c,pro,a.date)
            return 1 if run(c,day,pro,sources,a.force) else 0
        finally:c.close()


if __name__=='__main__':
    sys.exit(main())
