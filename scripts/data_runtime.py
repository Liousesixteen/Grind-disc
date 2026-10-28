"""Shared provider, calendar and transactional ingestion primitives.

No credentials, database connections or network calls at import time.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / 'cache/kb/trading_kb.sqlite'
TZ = dt.timezone(dt.timedelta(hours=8))


def now():
    return dt.datetime.now(TZ)


def iso_date(value):
    value = str(value)[:10]
    if len(value) == 8:
        value = f'{value[:4]}-{value[4:6]}-{value[6:]}'
    return dt.date.fromisoformat(value).isoformat()


def date8(value):
    return iso_date(value).replace('-', '')


def number(value):
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class ProviderError(RuntimeError):
    pass


class ProClient:
    """HTTPS Tushare adapter; shared cross-process rate gate, bounded retries."""
    def __init__(self):
        self._token = os.environ.get('TUSHARE_TOKEN') or (ROOT / 'scripts/.tushare_token').read_text().strip()
        self.interval = max(float(os.environ.get('TUSHARE_REQUEST_INTERVAL', '0.65')), 0.32)

    def __getattr__(self, api):
        if api.startswith('_'):
            raise AttributeError(api)
        return lambda **params: self.request(api, **params)

    def request(self, api, **params):
        import pandas as pd
        import requests
        fields = params.pop('fields', '')
        gate = ROOT / 'cache/rate/tushare.lock'
        gate.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            with gate.open('a+') as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    previous = float(f.read() or 0)
                except ValueError:
                    previous = 0
                time.sleep(max(0, self.interval - (time.time() - previous)))
                f.seek(0); f.truncate(); f.write(str(time.time())); f.flush()
                fcntl.flock(f, fcntl.LOCK_UN)
            try:
                response = requests.post('https://api.tushare.pro', json={
                    'api_name': api, 'token': self._token, 'params': params, 'fields': fields,
                }, timeout=(10, 45))
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise ProviderError(f'{api}: transport/response failure') from None
                time.sleep(2 ** attempt)
                continue
            code = result.get('code')
            if code != 0:
                # Never include provider messages, request bodies or credentials in logs.
                if code == 40203:
                    raise ProviderError(f'{api}: permission denied ({code})')
                if attempt == 2:
                    raise ProviderError(f'{api}: provider error ({code})')
                time.sleep(3 * (attempt + 1))
                continue
            data = result.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('fields'), list) or not isinstance(data.get('items'), list):
                raise ProviderError(f'{api}: invalid data schema')
            return pd.DataFrame(data['items'], columns=data['fields'])


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_calendar(
 exchange TEXT,cal_date TEXT,is_open INT,pretrade_date TEXT,
 PRIMARY KEY(exchange,cal_date));
CREATE TABLE IF NOT EXISTS ingest_partition(
 source TEXT,date TEXT,status TEXT,rows INT,note TEXT,updated_at TEXT,
 PRIMARY KEY(source,date));
CREATE TABLE IF NOT EXISTS raw_response(
 source TEXT,date TEXT,partition_key TEXT,sha256 TEXT,payload TEXT,fetched_at TEXT,
 PRIMARY KEY(source,date,partition_key,sha256));
CREATE TABLE IF NOT EXISTS daily_log(date TEXT,source TEXT,rows INT,status TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS fact_equity_daily(
 date TEXT,ts_code TEXT,open REAL,high REAL,low REAL,close REAL,pre_close REAL,
 pct REAL,volume_shares REAL,amount_cny REAL,turnover REAL,volume_ratio REAL,
 float_mcap_cny REAL,adj_factor REAL,source TEXT,PRIMARY KEY(date,ts_code));
CREATE TABLE IF NOT EXISTS fact_kpl_directory(
 date TEXT,theme TEXT,name TEXT,z_t_num INT,up_num INT,PRIMARY KEY(date,theme));
CREATE TABLE IF NOT EXISTS fact_lhb_detail(
 date TEXT,ts_code TEXT,name TEXT,reason TEXT,buy REAL,sell REAL,net REAL,
 PRIMARY KEY(date,ts_code,reason));
CREATE TABLE IF NOT EXISTS fact_lhb_seat_detail(
 date TEXT,ts_code TEXT,seat TEXT,side TEXT,reason TEXT,buy REAL,sell REAL,net REAL,
 PRIMARY KEY(date,ts_code,seat,side,reason));
"""


def connect(path=DEFAULT_DB):
    c = sqlite3.connect(str(path), timeout=60)
    c.executescript(SCHEMA)
    return c


def raw(c, source, day, key, frame):
    payload = frame.to_json(orient='split', force_ascii=False)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO raw_response VALUES(?,?,?,?,?,?)',
              (source, iso_date(day), str(key), digest, payload, now().isoformat()))


def mark(c, source, day, status, rows=0, note=''):
    day = iso_date(day)
    c.execute('INSERT OR REPLACE INTO ingest_partition VALUES(?,?,?,?,?,?)',
              (source, day, status, rows, note, now().isoformat()))
    c.execute('INSERT INTO daily_log VALUES(?,?,?,?,?)', (day, source, rows, status, note))
    if status == 'ok':
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (source + ':last_data_date', day))
    c.commit()


@dataclass(frozen=True)
class PartitionResult:
    rows: int
    status: str = 'ok'
    note: str = ''


def run_partition(c, source, day, action):
    """The action is atomic. Failure cannot leak partial writes into the next source."""
    try:
        with c:
            result = action()
            rows = result.rows if isinstance(result, PartitionResult) else result
            if rows is None or rows < 0:
                raise ValueError('invalid row count')
            if isinstance(result,PartitionResult) and result.status not in ('ok','partial','empty'):
                raise ValueError('invalid partition status')
        status = result.status if isinstance(result, PartitionResult) else ('ok' if rows else 'empty')
        note = result.note if isinstance(result, PartitionResult) else ''
        if status not in ('ok','partial','empty'):
            raise ValueError('invalid partition status')
        mark(c, source, day, status, rows, note)
        print(f'{day} {source}: {rows} {status} {note}', flush=True)
        return status=='ok' and bool(rows)
    except Exception as error:
        c.rollback()
        note = str(error) if isinstance(error, ProviderError) else type(error).__name__
        mark(c, source, day, 'fail', 0, note)
        print(f'{day} {source}: fail ({note})', flush=True)
        return False


def refresh_calendar(c, pro, end=None, start=None):
    end = iso_date(end or now().date())
    start = iso_date(start) if start else (dt.date.fromisoformat(end) - dt.timedelta(days=400)).isoformat()
    frame = pro.trade_cal(exchange='SSE', start_date=date8(start), end_date=date8(end))
    if not {'cal_date', 'is_open', 'pretrade_date'} <= set(frame.columns):
        raise ProviderError('trade_cal: missing fields')
    dates = {iso_date(v) for v in frame['cal_date']}
    if end not in dates or start not in dates:
        raise ProviderError('trade_cal: incomplete requested range')
    with c:
        for r in frame.to_dict('records'):
            c.execute('INSERT OR REPLACE INTO trade_calendar VALUES(?,?,?,?)',
                      ('SSE', iso_date(r['cal_date']), int(r['is_open']), iso_date(r['pretrade_date'])))


def is_open(c, day):
    row = c.execute("SELECT is_open FROM trade_calendar WHERE exchange='SSE' AND cal_date=?", (iso_date(day),)).fetchone()
    if row is None:
        raise ProviderError('trade calendar unavailable; weekday approximation disabled')
    return row[0] == 1


def trade_dates(c, n=1, end=None):
    end = iso_date(end or now().date())
    dates = [r[0] for r in c.execute("SELECT cal_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 AND cal_date<=? ORDER BY cal_date DESC LIMIT ?", (end, n))]
    if len(dates) < n:
        raise ProviderError('insufficient verified trading calendar')
    return [date8(d) for d in dates]


def target_date(c, pro, requested=None):
    end = iso_date(requested or now().date())
    if end > now().date().isoformat():
        raise ValueError('future target date is not permitted')
    try:
        refresh_calendar(c, pro, end)
    except ProviderError:
        is_open(c, end)  # A complete, already verified calendar is a permitted fallback.
    if requested:
        if not is_open(c, end):
            raise ValueError('target is not an exchange trading day')
        return date8(end)
    if now().hour < 16:
        end = (dt.date.fromisoformat(end) - dt.timedelta(days=1)).isoformat()
    return trade_dates(c, end=end)[0]


@contextmanager
def job_lock(name):
    path = ROOT / 'cache/jobs' / (name + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('job already running') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
