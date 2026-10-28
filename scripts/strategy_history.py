#!/usr/bin/env python3
"""Isolated Tushare OHLCV+adjustment history for causal research features.

No legacy-unit guesses and no current-day unfinished candle in premarket features.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
from data_runtime import ROOT, DEFAULT_DB, ProClient, number, iso_date, now

HISTORY_DB = ROOT / 'cache/product/history.sqlite'


def connect(path=HISTORY_DB):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''CREATE TABLE IF NOT EXISTS bars(date TEXT,code TEXT,
 open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,adj REAL,
 PRIMARY KEY(date,code));
CREATE INDEX IF NOT EXISTS bars_code ON bars(code,date);
CREATE TABLE IF NOT EXISTS partition(date TEXT PRIMARY KEY,status TEXT,rows INT,fetched_at TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS calendar(date TEXT PRIMARY KEY,is_open INT);
CREATE TABLE IF NOT EXISTS history_revision(date TEXT,hash TEXT,path TEXT,created_at TEXT,PRIMARY KEY(date,hash));
''')
    return c


def archive_revision(c,day,records):
    """Content-addressed immutable OHLCV revision, including adjustment factors."""
    body=json.dumps(sorted([list(r) for r in records],key=lambda r:(r[0],r[1])),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    digest=hashlib.sha256(body).hexdigest();path=Path(c.execute('PRAGMA database_list').fetchone()[2]).parent/'history-revisions'/day/(digest+'.json.gz')
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as handle:handle.write(gzip.compress(body,mtime=0))
    with c:c.execute('INSERT OR IGNORE INTO history_revision VALUES(?,?,?,?)',(day,digest,str(path),now().isoformat()))
    return digest


def ingest_day(c, pro, day):
    d = iso_date(day)
    daily = pro.daily(trade_date=d.replace('-', ''))
    factor = pro.adj_factor(trade_date=d.replace('-', ''))
    fields = {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'amount'}
    if not fields <= set(daily.columns) or daily.empty or len(daily) >= 6000:
        raise ValueError('daily schema/empty/row-cap requires review')
    if not {'ts_code', 'adj_factor'} <= set(factor.columns) or factor.empty:
        raise ValueError('adjustment partition missing')
    adj = dict(zip(factor.ts_code, factor.adj_factor))
    records = []
    for r in daily.to_dict('records'):
        a = number(adj.get(r['ts_code']))
        vals = [number(r[k]) for k in ('open', 'high', 'low', 'close', 'vol', 'amount')]
        if iso_date(r['trade_date']) != d or any(v is None for v in vals) or not a or a <= 0:
            raise ValueError('date/price/adjustment coverage mismatch')
        if min(vals[:4]) <= 0 or vals[1] < max(vals[0], vals[2], vals[3]) or min(vals[4:]) < 0:
            raise ValueError('invalid OHLCV')
        records.append((d, r['ts_code'].split('.')[0], *vals[:4], vals[4] * 100, vals[5] * 1000, a))
    if len({r[1] for r in records}) != len(records):
        raise ValueError('duplicate equity identifiers')
    revision=archive_revision(c,d,records)
    raw = ROOT / 'cache/product/raw' / (d + '.json.gz')
    raw.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw, 'wt', encoding='utf-8') as f:
        json.dump({'date': d, 'fetched_at': now().isoformat(), 'source': 'tushare',
                   'daily': json.loads(daily.to_json(orient='records')),
                   'adj_factor': json.loads(factor.to_json(orient='records'))}, f, ensure_ascii=False)
    with c:
        c.execute('DELETE FROM bars WHERE date=?', (d,))
        c.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)', records)
        c.execute('INSERT OR REPLACE INTO partition VALUES(?,?,?,?,?)',
                  (d, 'ok', len(records), now().isoformat(), 'OHLCV CNY/shares; daily+adj coverage verified; revision='+revision))
    return len(records)


def features(c, code, cutoff, sessions, launch_date=None):
    """cutoff inclusive; caller supplies prior exchange sessions, never future bars."""
    rows = [dict(r) for r in c.execute('SELECT * FROM bars WHERE code=? AND date<=? ORDER BY date DESC LIMIT 130',
                                      (code, iso_date(cutoff)))][::-1]
    out = {'history_asof': rows[-1]['date'] if rows else None, 'history_n': len(rows),
           'history_source': 'tushare normalized daily+adj'}
    if not rows or rows[-1]['date'] != iso_date(cutoff):
        return out
    if any(not r['adj'] or r['adj'] <= 0 for r in rows):
        return out
    # Keep indicators unavailable across suspension/missing sessions, not a fake contiguous window.
    seq = sessions[-len(rows):]
    if seq and [r['date'] for r in rows] != seq:
        n = 0
        for r, day in zip(reversed(rows), reversed(sessions)):
            if r['date'] != day:
                break
            n += 1
        rows = rows[-n:] if n else []
    out['contiguous_n'] = len(rows)
    if not rows:
        return out
    anchor = rows[-1]['adj']
    close = [r['close'] * r['adj'] / anchor for r in rows]
    high = [r['high'] * r['adj'] / anchor for r in rows]
    low = [r['low'] * r['adj'] / anchor for r in rows]
    volume = [r['volume'] * anchor / r['adj'] for r in rows]
    for period in (5, 10, 20, 60):
        if len(rows) >= period:
            out['ma' + str(period)] = statistics.mean(close[-period:])
            out['high' + str(period)] = max(high[-period:])
            out['low' + str(period)] = min(low[-period:])
        if len(rows) >= period + 1:
            out['ret' + str(period)] = (close[-1] / close[-period - 1] - 1) * 100
    if len(rows) >= 60:
        out['drawdown60'] = (1 - close[-1] / max(high[-60:])) * 100
        lo, hi = min(low[-60:]), max(high[-60:])
        out['position60'] = (close[-1] - lo) / (hi - lo) if hi > lo else None
    if len(rows) >= 21:
        out['previous_ma20'] = statistics.mean(close[-21:-1])
        rets = [close[i] / close[i - 1] - 1 for i in range(1, len(close))]
        out['vol20'] = statistics.stdev(rets[-20:]) * 100
    if len(rows) >= 23:
        out['ret20_last3'] = [(close[-i] / close[-i-20] - 1) * 100 for i in (3,2,1)]
    if launch_date:
        launch = iso_date(launch_date)
        idx = next((i for i, r in enumerate(rows) if r['date'] == launch), None)
        if idx is not None and idx >= 20 and idx < len(rows)-1:
            after = rows[idx+1:]
            out['days_since_zt'] = len(after)
            base_vol = volume[idx]
            out['pullback_volume_ratio'] = statistics.mean(volume[idx+1:]) / base_vol if base_vol > 0 else None
            out['pullback_above_prebase'] = statistics.mean(volume[idx+1:]) > statistics.mean(volume[idx-20:idx])
            launch_open = rows[idx]['open']*rows[idx]['adj']/anchor
            out['holds_launch_body'] = min(low[idx+1:]) >= launch_open
    if len(rows) >= 30:
        oldvol, newvol = statistics.mean(volume[-30:-10]), statistics.mean(volume[-10:])
        out['dry_volume_ratio'] = newvol / oldvol if oldvol > 0 else None
        prior_floor = min(low[-30:-10])
        out['no_new_low10'] = min(low[-10:]) >= prior_floor
        oldrange = statistics.mean((h - l) / p for h, l, p in zip(high[-30:-10], low[-30:-10], close[-30:-10]))
        newrange = statistics.mean((h - l) / p for h, l, p in zip(high[-10:], low[-10:], close[-10:]))
        out['range_contracting'] = newrange < oldrange
    if len(rows) >= 2:
        out['last_close'], out['last_open'] = close[-1], rows[-1]['open'] * rows[-1]['adj'] / anchor
        out['previous_close'] = close[-2]
        out['previous_high'] = high[-2]
        out['prior_high'] = high[-1]
    if len(rows) >= 22:
        base_volume = statistics.mean(volume[-21:-1])
        launch_last = base_volume > 0 and volume[-1] / base_volume > 1.5 and close[-1] > rows[-1]['open'] * rows[-1]['adj'] / anchor
        out['completed_launch'] = launch_last or (volume[-2] > statistics.mean(volume[-22:-2]) * 1.5 and close[-2] > rows[-2]['open'] * rows[-2]['adj'] / anchor)
        out['shape_a'] = launch_last or close[-1] > max(high[-21:-1])
    if len(rows) >= 60:
        def ema(values, period):
            a = 2 / (period + 1)
            result = [values[0]]
            for v in values[1:]: result.append(a * v + (1 - a) * result[-1])
            return result
        e12, e26 = ema(close, 12), ema(close, 26)
        dif = [a - b for a, b in zip(e12, e26)]
        dea = ema(dif, 9)
        gains = [max(b - a, 0) for a, b in zip(close, close[1:])]
        losses = [max(a - b, 0) for a, b in zip(close, close[1:])]
        ag, al = statistics.mean(gains[:14]), statistics.mean(losses[:14])
        rsis = []
        for g, l in zip(gains[14:], losses[14:]):
            ag, al = (ag * 13 + g) / 14, (al * 13 + l) / 14
            rsis.append(100 - 100 / (1 + ag / al) if al > 0 else 100 if ag > 0 else 50)
        out['momentum_turn'] = (dif[-1] > dea[-1] and dif[-2] <= dea[-2]) or (dif[-1] < 0 and dif[-1] > dif[-2]) or (rsis[-2] < 30 and 30 <= rsis[-1] <= 55)
    if len(rows) >= 62:
        def setup(i):
            prefix = rows[:i]
            cv, lo, hi, pr = volume[:i], low[:i], high[:i], close[:i]
            dry = statistics.mean(cv[-10:]) / statistics.mean(cv[-30:-10]) if statistics.mean(cv[-30:-10]) > 0 else None
            stopped = min(lo[-10:]) >= min(lo[-30:-10])
            contracting = statistics.mean((h-l)/p for h,l,p in zip(hi[-10:],lo[-10:],pr[-10:])) < statistics.mean((h-l)/p for h,l,p in zip(hi[-30:-10],lo[-30:-10],pr[-30:-10]))
            dd = (1 - pr[-1]/max(hi[-60:]))*100
            launched = close[i] > rows[i]['open'] * rows[i]['adj'] / anchor and volume[i] > statistics.mean(cv[-20:])*1.5
            return bool(dry is not None and dry < .75 and stopped and contracting and dd > 15 and launched), (dry,stopped,contracting,dd)
        latest, earlier = len(rows)-1, len(rows)-2
        current_setup, current_inputs = setup(latest)
        earlier_setup, earlier_inputs = setup(earlier)
        chosen = current_inputs if current_setup else earlier_inputs if earlier_setup else None
        if chosen:
            for k,v in zip(('dry_volume_ratio','no_new_low10','range_contracting','drawdown60'),chosen):out[k]=v
        if current_setup:
            out['b_nextday_confirmed'] = None
            out['b_stage'] = '启动日完成，下一交易日尚未完成确认'
        elif earlier_setup:
            launch_open = rows[earlier]['open'] * rows[earlier]['adj'] / anchor
            advance = close[earlier] - launch_open
            out['b_nextday_confirmed'] = close[-1] >= launch_open and (advance <= 0 or (close[earlier]-close[-1])/advance < .8)
            out['b_stage'] = '次一已完成交易日确认' if out['b_nextday_confirmed'] else '次日结构失败'
        else:
            out['b_nextday_confirmed'] = False
            out['b_stage'] = '最近两日无完整B启动结构'
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--end', required=True)
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--db', default=str(HISTORY_DB))
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    end = iso_date(a.end)
    if end >= now().date().isoformat():
        raise SystemExit('预盘历史仅回补今天以前的已结束交易日')
    source = sqlite3.connect(DEFAULT_DB.resolve().as_uri() + '?mode=ro', uri=True)
    cal = source.execute('SELECT cal_date,is_open FROM trade_calendar WHERE exchange=\'SSE\' AND cal_date<=? ORDER BY cal_date', (end,)).fetchall()
    source.close()
    days = [d for d, opened in cal if opened][-a.days:]
    if len(days) < a.days:
        raise SystemExit('权威交易日历不足，先补日历，不按工作日猜测')
    c = connect(a.db)
    with c:
        c.executemany('INSERT OR REPLACE INTO calendar VALUES(?,?)', cal)
    pro = ProClient()
    failures = 0
    # Most recent partitions first: useful indicators become available before older coverage.
    for d in reversed(days):
        if not a.force and c.execute("SELECT 1 FROM partition WHERE date=? AND status='ok'", (d,)).fetchone():
            continue
        try:
            count = ingest_day(c, pro, d)
            print(f'{d} ok {count}', flush=True)
        except Exception as e:
            failures += 1
            with c:
                c.execute('INSERT OR REPLACE INTO partition VALUES(?,?,?,?,?)', (d, 'failed', 0, now().isoformat(), type(e).__name__))
            print(f'{d} failed {type(e).__name__}', flush=True)
            if failures >= 3:
                break
    c.close()
    return 1 if failures else 0


def synchronize(target, db=HISTORY_DB, main_db=DEFAULT_DB, provider=None, days=120):
    """Idempotent, single-writer repair of completed partitions; shared primary first."""
    import fcntl
    import hashlib
    lock = Path(db).with_suffix('.sync.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'state':'running','target':target,'note':'其他进程正在同步'}
        source = sqlite3.connect(Path(main_db).resolve().as_uri()+'?mode=ro', uri=True)
        source.row_factory=sqlite3.Row
        c=connect(db)
        try:
            cal=[tuple(r) for r in source.execute("SELECT cal_date,is_open FROM trade_calendar WHERE exchange='SSE' ORDER BY cal_date")]
            dates=[d for d,o in cal if o and d<=target][-days:]
            with c:c.executemany('INSERT OR REPLACE INTO calendar VALUES(?,?)',cal)
            done=[];failed=[]
            for day in dates:
                existing=c.execute("SELECT * FROM partition WHERE date=? AND status='ok'",(day,)).fetchone()
                if existing and day!=target:continue
                partition=source.execute("SELECT status FROM ingest_partition WHERE source='daily' AND date=?",(day,)).fetchone()
                rows=[dict(r) for r in source.execute('SELECT * FROM fact_equity_daily WHERE date=?',(day,))] if partition and partition[0]=='ok' else []
                if rows and all(r['source']=='tushare' and r['adj_factor'] and r['adj_factor']>0 and all(r[k] is not None and r[k]>0 for k in ('open','high','low','close')) for r in rows):
                    expected=source.execute('SELECT COUNT(*) FROM dim_stock').fetchone()[0]
                    if len(rows) < expected*.95:rows=[]
                else:rows=[]
                try:
                    if rows:
                        records=[(day,r['ts_code'].split('.')[0],r['open'],r['high'],r['low'],r['close'],r['volume_shares'],r['amount_cny'],r['adj_factor']) for r in rows]
                        fingerprint=archive_revision(c,day,records)
                        if existing:
                            stored=[tuple(r) for r in c.execute('SELECT * FROM bars WHERE date=? ORDER BY code',(day,))]
                            if stored==sorted(records,key=lambda r:r[1]):continue
                        with c:
                            c.executemany('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?)',records)
                            c.execute('INSERT OR REPLACE INTO partition VALUES(?,?,?,?,?)',(day,'ok',len(rows),now().isoformat(),'已验收Tushare主库；sha256='+fingerprint))
                    else:
                        if existing:continue
                        if provider is None:provider=ProClient()
                        ingest_day(c,provider,day)
                    done.append(day)
                except Exception as e:
                    failed.append({'date':day,'reason':type(e).__name__})
                    # Do not overwrite a previously accepted partition on transport failure.
                    if len(failed)>=3:break
            latest=c.execute("SELECT MAX(date) FROM partition WHERE status='ok'").fetchone()[0]
            return {'state':'ok' if latest==target and not failed else 'partial','target':target,'actual':latest,'repaired':done,'failures':failed,'source':'tushare'}
        finally:source.close();c.close()


def sync_etfs(codes, target, provider=None, db=HISTORY_DB):
    """ETF OHLCV needs its own fund adjustment factors; no fabricated factor=1."""
    provider=provider or ProClient();c=connect(db);report=[]
    c.execute('CREATE TABLE IF NOT EXISTS etf_partition(code TEXT,target TEXT,status TEXT,n INT,updated_at TEXT,note TEXT,PRIMARY KEY(code,target))')
    start=(dt.date.fromisoformat(target)-dt.timedelta(days=240)).isoformat().replace('-','')
    try:
        for code in codes:
            if c.execute("SELECT 1 FROM etf_partition WHERE code=? AND target=? AND status='ok'",(code,target)).fetchone():continue
            try:
                ts=code+('.SH' if code.startswith('5') else '.SZ')
                bars=provider.fund_daily(ts_code=ts,start_date=start,end_date=target.replace('-',''))
                factors=provider.fund_adj(ts_code=ts,start_date=start,end_date=target.replace('-',''))
                adj={str(r['trade_date']):number(r['adj_factor']) for r in factors.to_dict('records')}
                records=[]
                for r in bars.to_dict('records'):
                    a=adj.get(str(r['trade_date']));v=[number(r[k]) for k in ('open','high','low','close','vol','amount')]
                    if not a or any(x is None for x in v) or min(v[:4])<=0:raise ValueError('ETF复权/行情缺失')
                    records.append((iso_date(r['trade_date']),code,*v[:4],v[4]*100,v[5]*1000,a))
                if not records or max(r[0] for r in records)!=target:raise ValueError('ETF窗口未覆盖目标日')
                with c:
                    c.executemany('INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?)',records)
                    c.execute('INSERT OR REPLACE INTO etf_partition VALUES(?,?,?,?,?,?)',(code,target,'ok',len(records),now().isoformat(),'tushare fund_daily+fund_adj'))
                report.append({'code':code,'state':'ok','n':len(records)})
            except Exception as e:
                with c:c.execute('INSERT OR REPLACE INTO etf_partition VALUES(?,?,?,?,?,?)',(code,target,'unavailable',0,now().isoformat(),type(e).__name__))
                report.append({'code':code,'state':'unavailable','reason':type(e).__name__})
    finally:c.close()
    return report


if __name__ == "__main__":
    raise SystemExit(main())
