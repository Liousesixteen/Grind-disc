"""Durable watch universe, immutable observations, research snapshots and local events.

Separate database: no writes to the primary market-data database or legacy journals.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))


def stamp():
    return dt.datetime.now(TZ).isoformat(timespec='seconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class ProductStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('TRADING_PRODUCT_DB', ROOT / 'cache/product/research.sqlite'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS product_meta(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS watch_item(code TEXT PRIMARY KEY,name TEXT,scene TEXT,
 style TEXT NOT NULL DEFAULT '短线',status TEXT NOT NULL DEFAULT 'active',
 tier_at_add TEXT,added_at TEXT,updated_at TEXT,notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS observation(id TEXT PRIMARY KEY,code TEXT,ts TEXT,body TEXT);
CREATE INDEX IF NOT EXISTS observation_code ON observation(code,ts);
CREATE TABLE IF NOT EXISTS product_event(id TEXT PRIMARY KEY,ts TEXT,kind TEXT,code TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS research_review(code TEXT,field TEXT,value TEXT,evidence TEXT,
 reviewed_at TEXT,expires_at TEXT,PRIMARY KEY(code,field));
CREATE TABLE IF NOT EXISTS watch_plan(code TEXT PRIMARY KEY,style TEXT,strategy_id TEXT,
 trigger_price REAL,invalid_price REAL,target_price REAL,deadline TEXT,notes TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS plan_state(code TEXT PRIMARY KEY,state TEXT,quote_time TEXT);
CREATE TABLE IF NOT EXISTS research_snapshot(id TEXT PRIMARY KEY,asof TEXT,
 registry_hash TEXT,payload BLOB);
CREATE TABLE IF NOT EXISTS experiment(id TEXT PRIMARY KEY,created_at TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS plan_instance(id TEXT PRIMARY KEY,code TEXT,style TEXT,strategy_id TEXT,
 version INT,body TEXT,status TEXT,updated_at TEXT);
CREATE INDEX IF NOT EXISTS plan_code ON plan_instance(code,status);
CREATE TABLE IF NOT EXISTS plan_revision(instance_id TEXT,version INT,body TEXT,created_at TEXT,PRIMARY KEY(instance_id,version));
CREATE TABLE IF NOT EXISTS instance_state(instance_id TEXT PRIMARY KEY,stage TEXT,body TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS event_flag(event_id TEXT PRIMARY KEY,acknowledged INT DEFAULT 0,muted INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS source_archive(hash TEXT PRIMARY KEY,created_at TEXT,body BLOB);
CREATE TABLE IF NOT EXISTS research_fact(code TEXT,kind TEXT,published_at TEXT,asof TEXT,body TEXT,status TEXT,
 PRIMARY KEY(code,kind,published_at,asof));
CREATE TABLE IF NOT EXISTS maintenance_task(key TEXT PRIMARY KEY,status TEXT,updated_at TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS paper_entry(id TEXT PRIMARY KEY,plan_id TEXT,code TEXT,style TEXT,ts TEXT,body TEXT);

''')
        self.migrate_legacy_plans()

    def migrate_legacy_plans(self):
        # Legacy plans lacked complete risk/thesis fields: preserve them paused, never auto-enable.
        with self.connect() as c:
            for row in c.execute('SELECT * FROM watch_plan').fetchall():
                old=dict(row);ident='legacy-'+old['code']
                if c.execute('SELECT 1 FROM plan_instance WHERE id=?',(ident,)).fetchone():continue
                body={**old,'id':ident,'version':1,'status':'paused','thesis':old.get('notes') or '旧预案迁移：逻辑待补证','invalidation':'旧预案未记录完整失效逻辑，补齐后方可启用','max_model_position':.1,'first_fraction':1/3,'cash_floor':.2,'overnight_cap':.6,'migration_notice':'旧版缺少完整依据，已暂停；原字段保留'}
                c.execute('INSERT INTO plan_instance VALUES(?,?,?,?,?,?,?,?)',(ident,old['code'],old['style'],old['strategy_id'],1,encode(body),'paused',stamp()))
                c.execute('INSERT INTO plan_revision VALUES(?,?,?,?)',(ident,1,encode(body),stamp()))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        try:
            with c:
                yield c
        finally:
            c.close()

    def _event(self, c, kind, code, body, event_id=None):
        c.execute('INSERT OR IGNORE INTO product_event VALUES(?,?,?,?,?)',
                  (event_id or uuid.uuid4().hex, stamp(), kind, code, encode(body)))

    def seed_legacy(self, directory):
        """One-time idempotent import; legacy rows remain immutable observations."""
        with self.connect() as c:
            if c.execute("SELECT 1 FROM product_meta WHERE key='legacy_import_v1'").fetchone():
                return
            for file in sorted(Path(directory).glob('盘中候选_*.jsonl')):
                for line in file.read_text(encoding='utf-8').splitlines():
                    try:
                        r = json.loads(line)
                        code = str(r.get('code', ''))
                        if len(code) != 6 or not code.isdigit():
                            continue
                        ts = str(r.get('ts') or stamp())
                        c.execute('INSERT OR IGNORE INTO observation VALUES(?,?,?,?)',
                                  (hashlib.sha256(encode(r).encode()).hexdigest(), code, ts, encode(r)))
                        c.execute('''INSERT OR IGNORE INTO watch_item
                            (code,name,scene,style,status,tier_at_add,added_at,updated_at,notes)
                            VALUES(?,?,?,'短线','active',NULL,?,?,'从旧日志导入；原始记录保留')''',
                                  (code, r.get('name', code), r.get('scene', ''), ts, stamp()))
                    except (ValueError, TypeError):
                        continue
            c.execute("INSERT INTO product_meta VALUES('legacy_import_v1',?)", (stamp(),))

    def list_watches(self, include_archived=False):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM watch_item ' +
                             ('' if include_archived else "WHERE status='active' ") +
                             'ORDER BY added_at,code').fetchall()
            out = []
            for row in rows:
                item = dict(row)
                obs = c.execute('SELECT body FROM observation WHERE code=? ORDER BY ts DESC LIMIT 1',
                                (item['code'],)).fetchone()
                item['last_observation'] = json.loads(obs[0]) if obs else None
                item['observations'] = c.execute('SELECT COUNT(*) FROM observation WHERE code=?',
                                                (item['code'],)).fetchone()[0]
                out.append(item)
            return out

    def add_watch(self, asset, quote=None, scene='', style='短线', notes='', tier=None, strategy_id='manual'):
        if style not in ('短线', '波段', '核心长线'):
            raise ValueError('请选择短线、波段或核心长线')
        code, now = asset['code'], stamp()
        with self.connect() as c:
            old = c.execute('SELECT * FROM watch_item WHERE code=?', (code,)).fetchone()
            if old and old['status'] == 'active':
                return False
            if old:
                c.execute("UPDATE watch_item SET status='active',updated_at=? WHERE code=?", (now, code))
                self._event(c, 'watch_restored', code, {})
            else:
                c.execute('INSERT INTO watch_item VALUES(?,?,?,?,?,?,?,?,?)',
                          (code, asset['name'], scene, style, 'active', tier, now, now, notes[:4000]))
                self._event(c, 'watch_added', code, {'scene': scene, 'style': style, 'strategy_id': strategy_id})
            q = quote or {}
            qt = str(q.get('quote_time') or '')
            r = {'code': code, 'name': asset['name'], 'ts': now[:19].replace('T', ' '),
                 'data_date': f'{qt[:4]}-{qt[4:6]}-{qt[6:8]}' if len(qt) >= 8 else None,
                 'entry_price': q.get('price'), 'quote_time': qt, 'src': q.get('src'),
                 'scene': scene, 'style': style, 'strategy_id': strategy_id, 'tier_at_add': tier,
                 'kind': 'manual_observation', 'rule_version': 'product-watch-v1'}
            c.execute('INSERT INTO observation VALUES(?,?,?,?)', (uuid.uuid4().hex, code, now, encode(r)))
            return True

    def update_watch(self, code, action, **fields):
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM watch_item WHERE code=?', (code,)).fetchone():
                raise ValueError('自选不存在')
            if action in ('archive', 'restore'):
                c.execute('UPDATE watch_item SET status=?,updated_at=? WHERE code=?',
                          ('archived' if action == 'archive' else 'active', stamp(), code))
            elif action == 'edit':
                style = fields.get('style', '短线')
                if style not in ('短线', '波段', '核心长线'):
                    raise ValueError('交易风格无效')
                c.execute('UPDATE watch_item SET scene=?,style=?,notes=?,updated_at=? WHERE code=?',
                          (str(fields.get('scene', ''))[:200], style, str(fields.get('notes', ''))[:4000], stamp(), code))
            else:
                raise ValueError('操作无效')
            self._event(c, 'watch_' + action, code, fields)

    def observations(self, code):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT body FROM observation WHERE code=? ORDER BY ts DESC', (code,))]

    def save_review(self, code, field, value, evidence, expires_at):
        if not evidence.strip() or not expires_at:
            raise ValueError('人工核验必须写明证据及有效期')
        if dt.datetime.fromisoformat(expires_at).tzinfo is None or dt.datetime.fromisoformat(expires_at) <= dt.datetime.now(TZ):
            raise ValueError('有效期必须带时区')
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO research_review VALUES(?,?,?,?,?,?)',
                      (code, field, encode(value), evidence[:4000], stamp(), expires_at))
            self._event(c, 'review_saved', code, {'field': field, 'value': value, 'evidence': evidence, 'expires_at': expires_at})

    def reviews(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM research_review')]

    def save_plan(self, code, plan):
        if plan.get('style') not in ('短线', '波段', '核心长线'):
            raise ValueError('预案必须指定交易风格')
        trigger, invalid = plan.get('trigger_price'), plan.get('invalid_price')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (trigger, invalid)) or not 0 < invalid < trigger:
            raise ValueError('当前仅支持多头观察预案：0 < 失效价 < 触发价')
        target = plan.get('target_price')
        if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or target <= trigger):
            raise ValueError('目标价须高于触发价；未知则留空')
        deadline = plan.get('deadline', '')
        end = dt.datetime.fromisoformat(deadline)
        if end.tzinfo is None or end <= dt.datetime.now(TZ):
            raise ValueError('必须填写带时区、尚未到期的认错时限')
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO watch_plan VALUES(?,?,?,?,?,?,?,?,?)',
                      (code, plan['style'], plan.get('strategy_id', 'manual'), trigger, invalid, target,
                       deadline, str(plan.get('notes', ''))[:4000], stamp()))
            c.execute('DELETE FROM plan_state WHERE code=?', (code,))
            self._event(c, 'plan_saved', code, plan)

    def plans(self):
        with self.connect() as c:
            return {r['code']: dict(r) for r in c.execute('SELECT * FROM watch_plan')}

    def event(self, kind, code, body, event_id=None):
        with self.connect() as c:
            self._event(c, kind, code, body, event_id)

    def events(self, limit=80):
        with self.connect() as c:
            return [{**dict(r), 'body': json.loads(r['body'])} for r in
                    c.execute('SELECT * FROM product_event ORDER BY ts DESC,rowid DESC LIMIT ?', (limit,))]

    def snapshot(self, asof, registry_hash, payload):
        key = hashlib.sha256((asof[:16] + registry_hash + (hashlib.sha256(encode(payload).encode()).hexdigest() if payload.get('schema_version', 1) >= 2 else '')).encode()).hexdigest()[:24]
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO research_snapshot VALUES(?,?,?,?)',
                      (key, asof, registry_hash, zlib.compress(encode(payload).encode())))
        return key

    def snapshot_payload(self, key):
        with self.connect() as c:
            row = c.execute('SELECT * FROM research_snapshot WHERE id=?', (key,)).fetchone()
            return {**dict(row), 'payload': json.loads(zlib.decompress(row['payload']))} if row else None

    def snapshots(self, limit=100):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT id,asof,registry_hash FROM research_snapshot ORDER BY asof DESC LIMIT ?', (limit,))]

    def save_instance(self, code, body):
        """One security, many strategies/styles. Every edit creates immutable revision."""
        plan = dict(body)
        style=plan.get('style');sid=str(plan.get('strategy_id') or '')
        if style not in ('短线','波段','核心长线') or not sid:raise ValueError('必须选择风格与策略路径')
        for key in ('trigger_price','invalid_price'):
            v=plan.get(key)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValueError('触发和失效价必须是有限正数')
        if plan['invalid_price']>=plan['trigger_price']:raise ValueError('多头观察须满足失效价低于触发价')
        target=plan.get('target_price')
        if target is not None and (isinstance(target,bool) or not isinstance(target,(int,float)) or not math.isfinite(target) or target<=plan['trigger_price']):raise ValueError('目标参考必须高于触发价或留空')
        end=dt.datetime.fromisoformat(str(plan.get('deadline','')))
        if end.tzinfo is None or end<=dt.datetime.now(TZ):raise ValueError('时限必须带时区且尚未到期')
        if not str(plan.get('thesis','')).strip() or not str(plan.get('invalidation','')).strip():raise ValueError('请写明观察逻辑与失效条件')
        budget=plan.get('max_model_position',0)
        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not math.isfinite(budget) or not 0<budget<=.6:raise ValueError('模型仓位上限须大于0且不超过60%，不代表真实账户')
        first=plan.get('first_fraction',1/3)
        if not isinstance(first,(int,float)) or not math.isfinite(first) or not 0<first<=1/3:raise ValueError('首次不超过计划仓位1/3')
        tick=plan.get('tick_size',.001 if code.startswith(('5','15','16','18')) else .01)
        if not isinstance(tick,(int,float)) or not math.isfinite(tick) or tick<=0:raise ValueError('最小价格单位无效')
        for key in ('trigger_price','invalid_price','target_price'):
            if plan.get(key) is not None and abs(plan[key]/tick-round(plan[key]/tick))>1e-6:raise ValueError('价格必须满足所选最小价格单位')
        ident=plan.get('id') or uuid.uuid4().hex
        with self.connect() as c:
            old=c.execute('SELECT * FROM plan_instance WHERE id=?',(ident,)).fetchone()
            if old and (old['code']!=code or old['style']!=style or old['strategy_id']!=sid):raise ValueError('既有预案不能改换标的/风格/策略，请另建预案')
            if old and not str(plan.get('revision_reason','')).strip():raise ValueError('修改预案必须记录修订原因')
            version=old['version']+1 if old else 1
            plan.update({'id':ident,'code':code,'version':version,'style':style,'strategy_id':sid,'model_only':True,'tick_size':tick,'first_fraction':first,'cash_floor':.2,'overnight_ceiling':.6})
            text=encode(plan)
            c.execute('INSERT OR REPLACE INTO plan_instance VALUES(?,?,?,?,?,?,?,?)',(ident,code,style,sid,version,text,'active',stamp()))
            c.execute('INSERT INTO plan_revision VALUES(?,?,?,?)',(ident,version,text,stamp()))
            c.execute('DELETE FROM instance_state WHERE instance_id=?',(ident,))
            self._event(c,'plan_revision',code,{'plan_id':ident,'version':version,'reason':plan.get('revision_reason','首次建立'),'style':style})
        return plan

    def instances(self, code=None, archived=False):
        with self.connect() as c:
            rows=c.execute('SELECT p.*,s.stage,s.body AS state_body FROM plan_instance p LEFT JOIN instance_state s ON s.instance_id=p.id WHERE '+('p.code=? AND ' if code else '')+('1=1' if archived else "p.status='active'")+' ORDER BY p.updated_at DESC', (code,) if code else ()).fetchall()
            return [{**json.loads(r['body']),'status':r['status'],'stage':r['stage'] or '等待条件','state':json.loads(r['state_body']) if r['state_body'] else None} for r in rows]

    def instance_action(self, ident, action):
        if action not in ('pause','resume','archive'):raise ValueError('预案操作无效')
        with self.connect() as c:
            row=c.execute('SELECT code,body,version FROM plan_instance WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('预案不存在')
            if action=='resume':
                body=json.loads(row['body'])
                if body.get('migration_notice') and row['version']==1:raise ValueError('旧预案须先补齐依据并保存修订版本')
                if dt.datetime.fromisoformat(body['deadline'])<=dt.datetime.now(TZ):raise ValueError('预案已到期，须先修订')
                if not c.execute("SELECT 1 FROM watch_item WHERE code=? AND status='active'",(row['code'],)).fetchone():raise ValueError('请先恢复自选，再逐份启用预案')
            c.execute('UPDATE plan_instance SET status=?,updated_at=? WHERE id=?',({'pause':'paused','resume':'active','archive':'archived'}[action],stamp(),ident))
            self._event(c,'plan_'+action,row[0],{'plan_id':ident})

    def event_action(self, ident, action):
        if action not in ('ack','mute','unmute'):raise ValueError('提醒操作无效')
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM product_event WHERE id=?',(ident,)).fetchone():raise ValueError('提醒不存在')
            c.execute('INSERT OR IGNORE INTO event_flag VALUES(?,0,0)',(ident,))
            c.execute('UPDATE event_flag SET '+('acknowledged=1' if action=='ack' else 'muted='+('1' if action=='mute' else '0'))+' WHERE event_id=?',(ident,))

    def activity(self, limit=100, offset=0, code=''):
        with self.connect() as c:
            rows=c.execute('SELECT e.*,COALESCE(f.acknowledged,0) AS acknowledged,COALESCE(f.muted,0) AS muted FROM product_event e LEFT JOIN event_flag f ON f.event_id=e.id '+('WHERE e.code=? ' if code else '')+'ORDER BY e.ts DESC,e.rowid DESC LIMIT ? OFFSET ?',((code,) if code else ())+(limit,offset)).fetchall()
            return [{**dict(r),'body':json.loads(r['body'])} for r in rows]

    def archive_source(self, files):
        payload={str(k):v for k,v in files.items()};raw=encode(payload).encode();key=hashlib.sha256(raw).hexdigest()
        with self.connect() as c:c.execute('INSERT OR IGNORE INTO source_archive VALUES(?,?,?)',(key,stamp(),zlib.compress(raw)))
        return key

    def record_task(self,key,status,body):
        with self.connect() as c:
            previous=c.execute('SELECT body FROM maintenance_task WHERE key=?',(key,)).fetchone()
            attempts=json.loads(previous[0]).get('failure_attempts',0) if previous else 0
            body={**body,'failure_attempts':attempts+1 if status in ('unavailable','partial','error') else 0}
            c.execute('INSERT OR REPLACE INTO maintenance_task VALUES(?,?,?,?)',(key,status,stamp(),encode(body)))

    def task_status(self):
        with self.connect() as c:return [{**dict(r),'body':json.loads(r['body'])} for r in c.execute('SELECT * FROM maintenance_task')]

    def facts(self,code,asof):
        with self.connect() as c:
            rows=c.execute("SELECT * FROM research_fact WHERE code IN (?, '*') AND asof<=? AND (published_at='' OR published_at<=?) ORDER BY asof DESC,published_at DESC",(code,asof,asof)).fetchall()
            out={}
            for r in rows:out.setdefault(r['kind'],{**dict(r),'body':json.loads(r['body'])})
            return out

    def put_fact(self,code,kind,published_at,asof,body,status='ok'):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO research_fact VALUES(?,?,?,?,?,?)',(code,kind,published_at,asof,encode(body),status))

    def recover_experiments(self):
        with self.connect() as c:
            for row in c.execute('SELECT id,body FROM experiment').fetchall():
                body=json.loads(row['body'])
                if body.get('state')=='running':
                    body.update({'state':'interrupted','completed_at':stamp(),'result':{'reason':'进程恢复：上次实验未完成，未发布绩效；请另建新试验，原请求保留'}})
                    c.execute('UPDATE experiment SET body=? WHERE id=?',(encode(body),row['id']))

    def experiments(self):
        with self.connect() as c:return [{**dict(r),'body':json.loads(r['body'])} for r in c.execute('SELECT * FROM experiment ORDER BY created_at DESC LIMIT 100')]
