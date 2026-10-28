#!/usr/bin/env python3
"""Full-market research inputs for causal studies: real limit prices, daily basic
metrics and index daily bars. Additive tables in the isolated research history DB.

Design rules
  * One partition per (source, trade_date); a day is published only after its rows
    pass coverage/consistency checks. Failure never overwrites an accepted day.
  * Units are standardised on write: CNY for money, shares for share counts.
  * Idempotent: rerunning a published partition is a no-op unless --force.
  * No credentials, no network at import time.

Usage
  .venv/bin/python scripts/research_data.py --target 2026-10-27 --days 121
  .venv/bin/python scripts/research_data.py --target 2026-10-27 --days 121 --verify
  .venv/bin/python scripts/research_data.py --target 2026-10-27 --days 5 --db /tmp/isolated.sqlite
"""
import argparse
import datetime as dt
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_history as H
from data_runtime import DEFAULT_DB, ProClient, ProviderError, iso_date, number, now

INDEX_CODES = ('000001.SH', '000016.SH', '000300.SH', '000905.SH', '000852.SH',
               '399006.SZ', '399106.SZ', '399303.SZ')
SOURCE_LABEL = 'tushare'

DDL = '''
CREATE TABLE IF NOT EXISTS market_limit(
  date TEXT, code TEXT, up REAL, down REAL, source TEXT,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS market_limit_date ON market_limit(date);
CREATE TABLE IF NOT EXISTS basic(
  date TEXT, code TEXT, close REAL, turnover_rate REAL, volume_ratio REAL,
  pe_ttm REAL, pb REAL, total_mv_cny REAL, circ_mv_cny REAL,
  float_share_shares REAL, total_share_shares REAL, source TEXT,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS basic_date ON basic(date);
CREATE TABLE IF NOT EXISTS index_daily(
  date TEXT, code TEXT, open REAL, high REAL, low REAL, close REAL,
  vol_shares REAL, amount_cny REAL, source TEXT,
  PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS research_partition(
  source TEXT, date TEXT, status TEXT, rows INT, note TEXT, updated_at TEXT,
  PRIMARY KEY(source, date));
'''


def connect(path=H.HISTORY_DB):
    c = H.connect(path)
    c.executescript(DDL)
    return c


def published(c, source, day):
    row = c.execute("SELECT status FROM research_partition WHERE source=? AND date=?",
                    (source, iso_date(day))).fetchone()
    return bool(row and row[0] == 'ok')


def mark(c, source, day, status, rows, note):
    c.execute('INSERT OR REPLACE INTO research_partition VALUES(?,?,?,?,?,?)',
              (source, iso_date(day), status, rows, note, now().isoformat()))


def sessions(c, target, days):
    rows = c.execute("SELECT date FROM calendar WHERE is_open=1 AND date<=? ORDER BY date DESC LIMIT ?",
                     (iso_date(target), days)).fetchall()
    return [r[0] for r in reversed(rows)]


def _code(ts_code):
    ts = str(ts_code)
    if not ts.endswith(('.SH', '.SZ', '.BJ')):
        return None
    return ts.split('.')[0]


def sync_limits(c, pro, day):
    d8 = day.replace('-', '')
    frame = pro.stk_limit(trade_date=d8)
    fields = {'ts_code', 'trade_date', 'up_limit', 'down_limit'}
    if not fields <= set(frame.columns) or frame.empty:
        raise ValueError('stk_limit schema/empty')
    rows = {}
    for r in frame.to_dict('records'):
        code = _code(r['ts_code'])
        up, down = number(r['up_limit']), number(r['down_limit'])
        if not code or up is None or down is None or not up > down > 0:
            continue
        if iso_date(r['trade_date']) != day:
            raise ValueError('stk_limit date mismatch')
        rows[code] = (day, code, up, down, SOURCE_LABEL)
    if len(rows) < 3000:
        raise ValueError(f'stk_limit truncated ({len(rows)} rows)')
    with c:
        c.execute('DELETE FROM market_limit WHERE date=?', (day,))
        c.executemany('INSERT OR REPLACE INTO market_limit VALUES(?,?,?,?,?)', list(rows.values()))
        mark(c, 'limits', day, 'ok', len(rows), 'tushare stk_limit; real up/down prices; CNY')
    return len(rows)


def sync_basic(c, pro, day):
    d8 = day.replace('-', '')
    frame = pro.daily_basic(trade_date=d8, fields='ts_code,trade_date,close,turnover_rate,volume_ratio,'
                                                   'pe_ttm,pb,total_mv,circ_mv,total_share,float_share')
    required = {'ts_code', 'trade_date', 'close', 'turnover_rate', 'volume_ratio',
                'pe_ttm', 'pb', 'total_mv', 'circ_mv', 'total_share', 'float_share'}
    if not required <= set(frame.columns) or frame.empty:
        raise ValueError('daily_basic schema/empty')
    rows = []
    for r in frame.to_dict('records'):
        code = _code(r['ts_code'])
        close = number(r['close'])
        if not code or close is None or close <= 0:
            continue
        if iso_date(r['trade_date']) != day:
            raise ValueError('daily_basic date mismatch')
        # Provider units: money in 10k CNY, shares in 10k shares. Store CNY/shares.
        def scaled(key, factor):
            v = number(r[key])
            return None if v is None else v * factor
        rows.append((day, code, close, number(r['turnover_rate']), number(r['volume_ratio']),
                     number(r['pe_ttm']), number(r['pb']),
                     scaled('total_mv', 1e4), scaled('circ_mv', 1e4),
                     scaled('float_share', 1e4), scaled('total_share', 1e4), SOURCE_LABEL))
    if len(rows) < 3000:
        raise ValueError(f'daily_basic truncated ({len(rows)} rows)')
    with c:
        c.execute('DELETE FROM basic WHERE date=?', (day,))
        c.executemany('INSERT OR REPLACE INTO basic VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        mark(c, 'basic', day, 'ok', len(rows), 'tushare daily_basic; CNY/shares; nullable ratios')
    return len(rows)


def sync_index(c, pro, days, force=False):
    """One ranged request per index; publish each day only when all indices exist."""
    if not days:
        return []
    start, end = days[0], days[-1]
    done, failed = [], []
    for ts in INDEX_CODES:
        try:
            frame = pro.index_daily(ts_code=ts, start_date=start.replace('-', ''), end_date=end.replace('-', ''))
            if frame.empty or not {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'amount'} <= set(frame.columns):
                raise ValueError('index_daily schema/empty')
            rows = []
            for r in frame.to_dict('records'):
                day = iso_date(r['trade_date'])
                if not start <= day <= end:
                    continue
                vals = [number(r[k]) for k in ('open', 'high', 'low', 'close')]
                if any(v is None or v <= 0 for v in vals):
                    raise ValueError('index_daily invalid OHLC')
                rows.append((day, str(r['ts_code']), *vals,
                             (number(r['vol']) or 0) * 100, (number(r['amount']) or 0) * 1000, SOURCE_LABEL))
            with c:
                c.executemany('INSERT OR REPLACE INTO index_daily VALUES(?,?,?,?,?,?,?,?,?)', rows)
            done.append(ts)
        except Exception as error:
            failed.append({'code': ts, 'reason': str(error) if isinstance(error, ProviderError) else type(error).__name__})
    for day in days:
        if not force and published(c, 'index', day):
            continue
        got = {r[0] for r in c.execute('SELECT code FROM index_daily WHERE date=?', (day,))}
        status = 'ok' if set(INDEX_CODES) <= got else 'partial'
        with c:
            mark(c, 'index', day, status, len(got), f'indices {len(got)}/{len(INDEX_CODES)}; source tushare')
    return failed


def verify(c, days):
    out = {'days': len(days), 'sources': {}}
    for source, table in (('limits', 'market_limit'), ('basic', 'basic'), ('index', 'index_daily')):
        okdays = [r[0] for r in c.execute(
            "SELECT date FROM research_partition WHERE source=? AND status='ok' AND date BETWEEN ? AND ? ORDER BY date",
            (source, days[0], days[-1]))]
        counts = dict(c.execute(
            f'SELECT date, COUNT(*) FROM {table} WHERE date BETWEEN ? AND ? GROUP BY date', (days[0], days[-1])).fetchall())
        out['sources'][source] = {
            'ok_days': len(okdays), 'expected_days': len(days),
            'missing_days': [d for d in days if d not in okdays][:10],
            'min_rows': min(counts.values()) if counts else 0,
            'max_rows': max(counts.values()) if counts else 0,
        }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target', required=True, help='last completed trade date (YYYY-MM-DD or YYYYMMDD)')
    ap.add_argument('--days', type=int, default=121)
    ap.add_argument('--db', default=str(H.HISTORY_DB))
    ap.add_argument('--sources', default='limits,basic,index')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--max-consecutive-failures', type=int, default=3)
    ap.add_argument('--allow-today', action='store_true',
                    help='允许目标日=今天，但要求主库 daily 分区已 ok 且已过 15:05')
    args = ap.parse_args()

    target = iso_date(args.target)
    today = now().date().isoformat()
    if target > today:
        raise SystemExit('目标日不能晚于今天')
    if target == today:
        if not args.allow_today:
            raise SystemExit('目标日必须是已完成交易日（早于今天）；确需今天请加 --allow-today')
        if now().strftime('%H:%M') < '15:05':
            raise SystemExit('今天尚未收盘，拒绝用未完成日线')
        main = sqlite3.connect(str(Path(DEFAULT_DB).resolve().as_uri()) + '?mode=ro', uri=True)
        try:
            row = main.execute("SELECT status FROM ingest_partition WHERE source='daily' AND date=?", (target,)).fetchone()
        finally:
            main.close()
        if not row or row[0] != 'ok':
            raise SystemExit('主库 daily 分区未验收，拒绝回补今天')
    sources = [s.strip() for s in args.sources.split(',') if s.strip()]
    unknown = set(sources) - {'limits', 'basic', 'index'}
    if unknown:
        raise SystemExit('未知来源: ' + ','.join(sorted(unknown)))

    c = connect(args.db)
    try:
        days = sessions(c, target, args.days)
        if len(days) < min(args.days, 2):
            raise SystemExit('交易日历不足，先刷新日历，不按工作日猜测')
        print(f'窗口 {days[0]}..{days[-1]} · {len(days)} 个交易日 · db={args.db}', flush=True)
        pro = None
        failures = 0
        for day in reversed(days):
            for source in sources:
                if source == 'index':
                    continue
                if not args.force and published(c, source, day):
                    continue
                try:
                    if pro is None:
                        pro = ProClient()
                    fn = sync_limits if source == 'limits' else sync_basic
                    rows = fn(c, pro, day)
                    print(f'{day} {source} {rows} ok', flush=True)
                    failures = 0
                except Exception as error:
                    failures += 1
                    note = str(error) if isinstance(error, ProviderError) else type(error).__name__
                    with c:
                        mark(c, source, day, 'fail', 0, note)
                    print(f'{day} {source} fail {note}', flush=True)
                    if failures >= args.max_consecutive_failures:
                        print('连续失败达到上限，停止', flush=True)
                        break
            if failures >= args.max_consecutive_failures:
                break
        if 'index' in sources:
            if pro is None:
                pro = ProClient()
            failed = sync_index(c, pro, days, force=args.force)
            if failed:
                print('指数失败:', failed, flush=True)
        if args.verify:
            import json
            print(json.dumps(verify(c, days), ensure_ascii=False, indent=2), flush=True)
    finally:
        c.close()


if __name__ == '__main__':
    main()
