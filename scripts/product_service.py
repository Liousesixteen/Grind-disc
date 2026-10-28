"""Independent, versioned research service. Local-only; no brokerage or order API."""
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qs,urlsplit
from data_runtime import ROOT,DEFAULT_DB,TZ
from product_store import ProductStore,stamp,encode
from strategy_engine import load_catalog,evaluate_universe,MANUAL_FIELDS,quote_valid
from product_clock import clock_context,quote_contract,market_coverage,crosscheck_reference,suspend_market_conclusions
import strategy_history as H
import research_evidence as E


class ProductService:
    def __init__(self,core=None,upstream=None,store=None):
        self.core,self.upstream=core,upstream
        self.store=store or ProductStore();self.store.seed_legacy(ROOT/'watchlists')
        self.store.recover_experiments()
        self.lock=threading.RLock();self.wake=threading.Event();self.stop=threading.Event()
        self.state={'booting':True,'warn':['等待首份已验收数据'],'service_started_at':stamp()}
        self.catalog=load_catalog();self.quotes={};self.feature_map={};self.all_results={}
        self.history_key=None;self.histories={};self.old_statuses={};self.old_conditions={}
        self.attention=set();self.last_refresh=None;self.core_error=None
        files=['strategy_engine.py','strategy_history.py','product_clock.py','research_evidence.py','research_backtest.py','strategy_lab.py','strategy_registry.py','research_market.py','dual_gate.py','intraday_scan.py','pankou.py','pool_pipeline.py','product_service.py','data_runtime.py']
        self.source_bundle={name:(ROOT/'scripts'/name).read_text() for name in files}
        self.engine_sha=hashlib.sha256(self.source_bundle['strategy_engine.py'].encode()).hexdigest()
        self.members,self.asset_directory=E.load_membership();self._load_universe()

    def calendar(self,day):
        c=E.readonly()
        try:cal={r[0]:bool(r[1]) for r in c.execute("SELECT cal_date,is_open FROM trade_calendar WHERE exchange='SSE'")}
        finally:c.close()
        return cal

    def _load_universe(self):
        today=dt.datetime.now(TZ).strftime('%Y%m%d');valid=[]
        for p in (ROOT/'cache').glob('pool_*'):
            if not p.is_dir() or p.name.endswith('_intraday'):continue
            try:
                s=json.loads((p/'summary.json').read_text());d=str(s.get('data_date','')).replace('-','')
                if d and d<today and s.get('focus_index') and (p/'pool_final.json').is_file():valid.append((d,s.get('run_at',''),p))
            except (OSError,ValueError):continue
        if not valid:raise ValueError('没有已完成的盘前股池')
        p=sorted(valid)[-1][-1];self.pool_directory=str(p)
        raw=(p/'pool_final.json').read_bytes();self.pool_rows=json.loads(raw);self.pool_hash=hashlib.sha256(raw).hexdigest()[:16]
        self.pool_date=json.loads((p/'summary.json').read_text())['data_date']
        self.assets={code:{**r,'asset_type':'stock'} for code,r in self.asset_directory.items()}
        for r in self.pool_rows:self.assets[r['code']]={**self.assets.get(r['code'],{}),**r,'asset_type':'stock'}
        c=E.readonly()
        try:
            for code,name in c.execute('SELECT code,name FROM dim_etf'):self.assets.setdefault(code,{'code':code,'name':name,'asset_type':'etf'})
        finally:c.close()

    def _frame(self):
        if self.core:
            with self.core.lock:return copy.deepcopy(self.core.state),copy.deepcopy(getattr(self.core,'research_quotes',{}))
        if not self.upstream:raise ValueError('数据引擎未配置')
        from urllib.request import urlopen
        with urlopen(self.upstream+'/api/state',timeout=10) as r:return json.load(r),{}

    def _core_loop(self):
        from intraday_scan import latest_pool_dir,load_pre_focus,load_kprev
        import theme_attr as TA
        while not self.stop.is_set():
            try:
                path=latest_pool_dir(None)
                signature=(path,Path(path,'summary.json').stat().st_mtime_ns) if path else None
                if signature!=getattr(self,'core_pool_signature',None):
                    summary=json.loads(Path(path,'summary.json').read_text());rows=json.loads(Path(path,'pool_final.json').read_text())
                    con,ind,ind1=TA.load_tag_map(path,self.core.cfg,universe=self.core.uni)
                    self.core.rows,self.core.focus_idx,self.core.data_date,self.core.pool=rows,summary['focus_index'],summary['data_date'],path
                    self.core.con,self.core.ind,self.core.ind1=con,ind,ind1
                    self.core.pre=load_pre_focus(self.core.data_date);self.core.kprev=load_kprev(path)
                    self.core_pool_signature=signature
                self.core_attempt_at=stamp();ok=self.core.refresh();self.core_error=None if ok else '基础源本轮更新失败'
            except Exception as e:self.core_error=type(e).__name__
            self.wake.set()
            self.stop.wait(self.core.effective_interval() if not self.core_error else 30)

    def _maintenance(self):
        """Own only the isolated research store; shared primary jobs retain their lock."""
        while not self.stop.is_set():
            try:
                now=dt.datetime.now(TZ);context=clock_context(now,self.calendar(now.date().isoformat()));target=context['history_target']
                if target:
                    status=H.synchronize(target);self.store.record_task('history',status['state'],status)
                    etfs=[c for c in ('510300','510500','512480','588000') if c in self.assets]
                    key='etf-history:'+target
                    if E.task_due(self.store,key):
                        r=H.sync_etfs(etfs,target);self.store.record_task(key,'ok' if all(x['state']=='ok' for x in r) else 'partial',{'rows':r})
                    import research_market as RM
                    if E.task_due(self.store,'market-limits:'+target):
                        limits=RM.sync_market_limits(target,set(self.asset_directory));self.store.record_task('market-limits:'+target,'ok' if all(r['state']=='ok' for r in limits) else 'partial',{'rows':limits})
                    attention=sorted({w['code'] for w in self.store.list_watches()}|self.attention)
                    E.update_fundamentals(self.store,[c for c in attention if self.assets.get(c,{}).get('asset_type')=='stock'],target)
                self.wake.set()
            except Exception as e:self.store.record_task('history','error',{'reason':type(e).__name__})
            self.stop.wait(300)

    def refresh(self):
        state,quotes=self._frame()
        if state.get('booting'):
            with self.lock:self.state={**state,'service_started_at':self.state.get('service_started_at',stamp())}
            return
        now=dt.datetime.now(TZ);clock=clock_context(now,self.calendar(now.date().isoformat()))
        if getattr(self,'pool_day',None)!=clock['day']:
            self._load_universe();self.pool_day=clock['day']
        watches=self.store.list_watches();ids={r['code'] for r in self.pool_rows if r.get('池层') in ('核心','备选')}
        ids|={w['code'] for w in watches};ids|={c for c in ('510300','510500','512480','588000') if c in self.assets}
        missing=sorted(ids-quotes.keys())
        if missing:
            from pool_pipeline import src_snapshot_tencent
            for q in src_snapshot_tencent(missing,workers=2):quotes[q['code']]={**q,'src':'tencent_qt','fetched_at':stamp()}
        for q in quotes.values():q.setdefault('src',state.get('src') or 'unknown')
        health={c:quote_contract(q,clock,now) for c,q in quotes.items()}
        ref=H.connect()
        try:closes={r[0]:r[1] for r in ref.execute('SELECT code,close FROM bars WHERE date=?',(clock['history_target'],))}
        finally:ref.close()
        crosscheck=crosscheck_reference(quotes,health,closes,clock['history_target'])
        expected=self.asset_directory.keys();coverage=market_coverage([q for c,q in quotes.items() if health.get(c,{}).get('valid')],expected,state.get('src',''))
        coverage['crosscheck']=crosscheck
        if not coverage['complete']:
            # No partial-market denominator, even when individual watch quotes exist.
            state['mkt_med_pct']=None;state['warn']=list(state.get('warn',[]))+[coverage['label']]
        from research_market import apply_verified_dual
        apply_verified_dual(state,quotes,health,clock['reference_day'],getattr(self.core,'con',{}))
        suspend_market_conclusions(state,coverage)
        metrics,main,multi=E.theme_observations(self.members,quotes,state.get('main_theme') or {},state.get('ladder') or {},health)
        state.update({'clock':clock,'calendar_open':clock['is_open'],'previous_session':clock['previous_session'],
          'research_pool_date':self.pool_date,'history_target':clock['history_target'],'quote_health':health,'theme_metrics':metrics,'coverage':coverage})
        if not coverage['complete']:
            for m in metrics.values():m.update({'mean':None,'excess':None,'breadth':None,'leader_positive':None})
        rows=[{**copy.deepcopy(self.assets[c]),'_scene':main.get(c)} for c in sorted(ids) if c in self.assets]
        target=clock['history_target'];c=H.connect()
        try:
            partitions=[dict(r) for r in c.execute('SELECT * FROM partition ORDER BY date DESC')]
            fingerprint=hashlib.sha256(encode(partitions).encode()).hexdigest()
            sessions=[r[0] for r in c.execute('SELECT date FROM calendar WHERE is_open=1 AND date<=? ORDER BY date',(target,))] if target else []
            # WAL revision changes are detected through partition metadata, not file mtime.
            etf_update=c.execute("SELECT MAX(date) FROM bars WHERE code LIKE '5%'").fetchone()[0]
            key=(target,tuple(r['code'] for r in rows),fingerprint,etf_update,self.pool_hash)
            if key!=self.history_key:
                self.histories={r['code']:H.features(c,r['code'],target,sessions,r.get('last_zt')) for r in rows} if target else {}
                self.history_key=key
        finally:c.close()
        actual=max((p['date'] for p in partitions if p['status']=='ok'),default=None)
        clock['execution_available']=bool(clock['execution_available'] and not self.core_error and coverage['complete'] and actual==target and not state.get('cohort_health',{}).get('pool_conflicts'))
        catalog=load_catalog();reviews=self.store.reviews()
        results,features=evaluate_universe(catalog,rows,quotes,state,self.histories,reviews,now,clock['is_open'])
        actual=max((p['date'] for p in partitions if p['status']=='ok'),default=None)
        scene_map={}
        for r in rows:
            scene=r['_scene'];q=quotes.get(r['code'],{})
            if scene and r['code'] not in set(state.get('zt_codes',[])):
                scene_map.setdefault(scene,[]).append({'code':r['code'],'name':r['name'],'pct':q.get('pct'),'price':q.get('price'),'quote_time':q.get('quote_time'),'tier':r.get('池层') or '池外','relation':'共享主归属题材，不代表因果带动','status':health.get(r['code'],{}).get('reason','缺价')})
        for members in scene_map.values():members.sort(key=lambda r:({'核心':0,'备选':1}.get(r['tier'],2),r['code']))
        compact={sid:{r['code']:{k:r[k] for k in ('status','passed','missing','failed','setup_state','evidence_state','policy_state','execution_state')} for r in rs} for sid,rs in results.items()}
        source_files=['strategy_engine.py','strategy_history.py','product_clock.py','research_evidence.py','research_backtest.py']
        archive=self.store.archive_source(self.source_bundle)
        profile_caps=[]
        for p in catalog['profiles']:
            rs=results[p['id']];auto=[r for r in p['rules'] if r['kind']=='condition']
            coverage_by_field={r['field']:sum(features[code].get(r['field']) is not None for code in features) for r in auto}
            profile_caps.append({**{k:p[k] for k in ('id','name','version','style','lifecycle','sources','rank')},'coverage':coverage_by_field,'manual_n':sum(r['kind']=='manual' for r in p['rules']),'auto_n':len(auto),'capabilities':p.get('capabilities',{}),'backtest_spec':p.get('backtest_spec')})
        signature=hashlib.sha256(encode({'features':features,'reviews':reviews,'results':compact,'catalog':catalog['hash'],'history':fingerprint,'phase':clock['phase']}).encode()).hexdigest()
        if signature!=getattr(self,'snapshot_signature',None):
            self.snapshot_id=self.store.snapshot(stamp(),catalog['hash'],{'schema_version':2,'catalog':catalog,'features':features,'frozen_results':compact,'reviews':reviews,'evaluated_at':now.isoformat(),
             'engine_sha256':self.engine_sha,'source_archive':archive,'data_revision':fingerprint,'partitions':partitions,
             'quotes':{c:quotes[c] for c in ids if c in quotes},'clock':clock,'coverage':coverage,'pool_hash':self.pool_hash,'pool_date':self.pool_date,'membership':{c:multi.get(c,[]) for c in ids},'source_time':state.get('taken_at'),'universe':[{'code':r['code'],'name':r['name'],'tier':r.get('池层')} for r in rows],
             'protocol':'冻结输入与版本，非成交回测'})
            self.snapshot_signature=signature
        self._changes(results,watches)
        self._instance_events(results,quotes,clock,health,features)
        state.update({'research_groups':scene_map,'watch_quotes':{c:quotes[c] for c in ids if c in quotes},'watch_items':self._watch_items(watches,state,quotes),'watch_n':len(watches),'product_events':self.store.activity(),
          'research':{'profiles':profile_caps,'catalog_hash':catalog['hash'],'source_drift':catalog.get('source_drift',[]),'pool_hash':self.pool_hash,'pool_date':self.pool_date,
          'universe_n':len(rows),'quote_n':sum(c in quotes for c in ids),'valid_quote_n':sum(health.get(c,{}).get('valid',False) for c in ids),'live_quote_n':sum(health.get(c,{}).get('state')=='live' for c in ids),
          'mode':'独立研究服务','session':clock['label'],'phase':clock['phase'],'history_ok':sum(p['status']=='ok' for p in partitions),'history_failed':sum(p['status']!='ok' for p in partitions),
          'history_target':target,'history_asof':actual,'history_ready':actual==target,'history_60_n':sum(f.get('contiguous_n',0)>=60 for f in features.values()),'data_asof':state.get('taken_at'),'rendered_research_at':stamp(),
          'snapshot_id':self.snapshot_id,'source_archive':archive,'execution_available':clock['execution_available'],'coverage':coverage,'maintenance':self.store.task_status(),
          'disclaimer':'实验观察：数据、技术、证据、工具及执行状态独立；无账户与下单接口','rows':self.summaries(results['halfway_233'])[:40]}})
        for parent in state.get('pankou',[]):parent['候选']=[]
        state['slot']=clock['label'];state['clock_hint']=clock['label'];state['standard']=clock['live']
        state['service_health']={'state':'degraded' if self.core_error or actual!=target or not coverage['complete'] or not state.get('cohort_health',{}).get('ready') or state.get('cohort_health',{}).get('pool_conflicts') else 'ok','core_error':self.core_error,'heartbeat':stamp(),'source_quote_time':state.get('quote_time'),'reference_day':clock['reference_day']}
        with self.lock:
            self.catalog,self.all_results,self.feature_map,self.quotes,self.state=catalog,results,features,quotes,state
            self.research_rows,self.input_state,self.sessions=rows,copy.deepcopy(state),sessions
            self.last_refresh=stamp()

    @staticmethod
    def summaries(results):
        keys=('code','name','scene','tier','pct','status','label','passed','missing','failed','rank','style','strategy_id','version','history_n','quote_time','rank_basis','setup_state','setup_label','setup_passed','setup_total','evidence_state','data_state','policy_state','execution_state','next_confirmation','blockers')
        return [{k:r.get(k) for k in keys} for r in results]

    def _watch_items(self,watches,state,quotes):
        by={r['code']:r for r in self.pool_rows};instances=self.store.instances(archived=True)
        return [{**w,'current_tier':by.get(w['code'],{}).get('池层') or '池外','quote':quotes.get(w['code'],{}),'instances':[p for p in instances if p['code']==w['code'] and p['status']!='archived'],'plan':None} for w in watches]

    def _changes(self,results,watches=None):
        watched={w['code'] for w in watches or []}
        for sid,rs in results.items():
            for r in rs:
                key=(sid,r['code']);states={x['field']:x['state'] for x in r['rules']};old=self.old_conditions.get(key)
                if old and r['code'] in watched:
                    changes={k:{'from':old.get(k),'to':v} for k,v in states.items() if old.get(k)!=v}
                    if changes:self.store.event('research_conditions_changed',r['code'],{'strategy_id':sid,'changes':changes,'snapshot':self.snapshot_id,'quote_time':r['quote_time'],'notice':'观察证据变化，不是下单指令'})
                self.old_conditions[key]=states

    def _plans(self,quotes,current,opened):
        # Compatibility for pre-v2 single-plan journals; never used for new instances.
        active={w['code'] for w in self.store.list_watches()}
        with self.store.connect() as c:
            for code,p in self.store.plans().items():
                if code not in active:continue
                q=quotes.get(code,{});expired=current>=dt.datetime.fromisoformat(p['deadline'])
                if not expired and quote_valid(q,current.date().isoformat(),current,opened) is not True:continue
                stage='时限到期' if expired else '失效价触及' if q['price']<=p['invalid_price'] else '观察触发价满足' if q['price']>=p['trigger_price'] else '等待条件'
                old=c.execute('SELECT state FROM plan_state WHERE code=?',(code,)).fetchone()
                if old and old[0] in ('失效价触及','时限到期'):stage=old[0]
                if stage!='等待条件' and (not old or old[0]!=stage):self.store._event(c,'plan_observation',code,{'state':stage,'quote_time':q.get('quote_time'),'price':q.get('price')})
                c.execute('INSERT OR REPLACE INTO plan_state VALUES(?,?,?)',(code,stage,q.get('quote_time')))

    def _instance_events(self,results,quotes,clock,health,features=None):
        by={sid:{r['code']:r for r in rs} for sid,rs in results.items()};now=dt.datetime.now(TZ)
        for p in self.store.instances():
            q=quotes.get(p['code'],{});r=by.get(p['strategy_id'],{}).get(p['code']);old=p.get('state') or {}
            expired=now>=dt.datetime.fromisoformat(p['deadline'])
            previous=p.get('stage');terminal=previous in ('失效价触及','时限到期')
            if terminal:stage=previous
            elif expired:stage='时限到期'
            elif not clock['live']:stage='非交易时段·参考'
            elif not health.get(p['code'],{}).get('valid'):stage='数据暂停'
            elif (features or {}).get(p['code'],{}).get('price_basis_verified') is not True:stage='价格基准待核·暂停'
            elif q['price']<=p['invalid_price']:stage='失效价触及'
            elif p.get('target_price') and q['price']>=p['target_price']:stage='目标参考触及'
            elif q['price']>=p['trigger_price']:stage='价格满足·'+('证据齐备待验证' if r and r['status']=='complete' else '其余条件待核')
            else:stage='等待条件'
            body={'stage':stage,'price':q.get('price'),'quote_time':q.get('quote_time'),'strategy_id':p['strategy_id'],'plan_id':p['id'],'version':p['version'],'research_state':r['status'] if r else 'not_in_universe','snapshot':self.snapshot_id,'notice':'本机研究提醒，非交易指令'}
            with self.store.connect() as c:
                if stage!=previous:self.store._event(c,'plan_stage_changed',p['code'],{**body,'from':previous})
                c.execute('INSERT OR REPLACE INTO instance_state VALUES(?,?,?,?)',(p['id'],stage,encode(body),stamp()))

    def loop(self):
        if self.core:threading.Thread(target=self._core_loop,daemon=True,name='research-quotes').start()
        threading.Thread(target=self._maintenance,daemon=True,name='research-maintenance').start()
        while not self.stop.is_set():
            try:self.refresh()
            except Exception as e:
                with self.lock:self.state.update({'product_error':type(e).__name__,'service_health':{'state':'error','reason':type(e).__name__,'heartbeat':stamp()}})
                print('研究层更新失败：'+type(e).__name__,flush=True)
            self.wake.wait(15);self.wake.clear()

    def get_state(self):
        with self.lock:return copy.deepcopy(self.state)

    def watches_today(self):return self.store.list_watches()
    def record_watch(self,body):return self.watch_action({**body,'action':'add'})
    def watch_action(self,b):
        code=str(b.get('code',''))
        if code not in self.assets:raise ValueError('证券目录中未找到该代码')
        action=b.get('action','add');q=self.quotes.get(code,{})
        if action=='add':
            if not q:
                from pool_pipeline import src_snapshot_tencent
                fetched=src_snapshot_tencent([code],workers=1);q={**fetched[0],'src':'tencent_qt','fetched_at':stamp()} if fetched else {}
            added=self.store.add_watch(self.assets[code],q,str(b.get('scene',''))[:200],b.get('style','短线'),str(b.get('notes','')),self.assets[code].get('池层'),b.get('strategy_id','manual'))
        else:
            self.store.update_watch(code,action,scene=b.get('scene',''),style=b.get('style','短线'),notes=b.get('notes',''));added=False
            if action=='archive':
                for p in self.store.instances(code):self.store.instance_action(p['id'],'pause')
        with self.lock:
            if q:self.quotes[code]=q;self.state.setdefault('watch_quotes',{})[code]=q
            watches=self.store.list_watches();self.state['watch_items']=self._watch_items(watches,self.state,self.quotes);self.state['watch_n']=len(watches)
        self.wake.set();return {'ok':True,'added':added,'n':len(watches)}

    def search(self,q):
        q=q.strip()[:40]
        return [{k:r.get(k) for k in ('code','name','asset_type')} for r in self.assets.values() if q and (q in r['code'] or q in r['name'])][:30]

    def research(self,strategy,query='',status='all',offset=0,pinned=None):
        with self.lock:
            rs=self.all_results.get(strategy)
            if rs is None:raise ValueError('研究路径尚未就绪')
            chosen=[r for r in rs if (status=='all' or r['status']==status or r['setup_state']==status) and (not query or any(query in str(r.get(k,'')) for k in ('code','name','scene')))]
            visible=[next((r for r in rs if r['code']==code),None) for code in (pinned or [])] if pinned else chosen[offset:offset+40]
            return {'rows':self.summaries([r for r in visible if r]),'total':len(chosen),'counts':{s:sum(r['status']==s for r in rs) for s in ('complete','pending','blocked','restricted','unavailable')},'profile':next(p for p in self.catalog['profiles'] if p['id']==strategy),'snapshot':getattr(self,'snapshot_id',None),'asof':self.last_refresh,'rank_order':'就绪/证据 → 技术阶段 → 风险调整相对强度（20日相对收益÷20日波动）→ 池层 → 相对表现 → 未满足条件 → 缺证据数'}

    def detail(self,code,strategy,timeframe='day'):
        if code not in self.assets:raise ValueError('证券代码未知')
        self.attention.add(code)
        with self.lock:
            asset=copy.deepcopy(self.assets[code]);q=copy.deepcopy(self.quotes.get(code,{}));f=copy.deepcopy(self.feature_map.get(code,{}));state=copy.deepcopy(getattr(self,'input_state',{}));catalog=copy.deepcopy(self.catalog)
            result=next((r for r in self.all_results.get(strategy,[]) if r['code']==code),None)
        c=H.connect()
        try:
            bars=[dict(r) for r in c.execute('SELECT date,open,high,low,close,volume,amount,adj FROM bars WHERE code=? AND date<=? ORDER BY date DESC LIMIT 180',(code,state.get('history_target','')))][::-1]
            if result is None and state:
                extra={**asset,'_scene':state.get('main_theme',{}).get(code)}
                hist=H.features(c,code,state['history_target'],self.sessions,asset.get('last_zt'))
                rs,fs=evaluate_universe(catalog,[extra],{code:q},state,{code:hist},self.store.reviews(),dt.datetime.now(TZ),state.get('calendar_open'))
                result=rs.get(strategy,[None])[0];f=fs[code]
        finally:c.close()
        return {'asset':asset,'quote':q,'result':result,'features':f,'daily_bars':bars,'asof':stamp(),'snapshot':getattr(self,'snapshot_id',None),'clock':state.get('clock'),'observations':self.store.observations(code),'instances':self.store.instances(code,archived=True),'facts':E.context_facts(code,asset,f,state.get('theme_metrics',{}).get(f.get('scene')),self.store,state.get('history_target') or dt.date.today().isoformat())}


def handler(service,page_loader):
    from intraday_board import make_handler
    import csv,gzip,io
    class ProductHandler(make_handler(service,page_loader)):
        def answer(self,value,status=200,content_type='application/json; charset=utf-8'):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            zipped='gzip' in self.headers.get('Accept-Encoding','') and len(raw)>16384
            if zipped:raw=gzip.compress(raw)
            self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(raw)))
            if zipped:self.send_header('Content-Encoding','gzip');self.send_header('Vary','Accept-Encoding')
            self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            u=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(u.query).items()}
            try:
                if u.path=='/health':return self.answer({'service':'research-6063','heartbeat':service.last_refresh,'booting':service.state.get('booting',False),'health':service.state.get('service_health'),'pid':os.getpid()})
                if u.path=='/api/state':return self.answer(service.get_state())
                if u.path in ('/product.js','/product-v2.js'):return self.answer((ROOT/'templates/product-v2.js').read_bytes(),content_type='text/javascript; charset=utf-8')
                if u.path in ('/product.css','/product-v2.css'):return self.answer((ROOT/'templates/product-v2.css').read_bytes(),content_type='text/css; charset=utf-8')
                if u.path=='/api/search':return self.answer({'assets':service.search(q.get('q',''))})
                if u.path=='/api/research':return self.answer(service.research(q.get('strategy','halfway_233'),q.get('q','')[:100],q.get('status','all'),max(0,int(q.get('offset',0))),q.get('pinned','').split(',')[:40] if q.get('pinned') else None))
                if u.path=='/api/detail':return self.answer(service.detail(q.get('code',''),q.get('strategy','halfway_233')))
                if u.path=='/api/events':return self.answer({'events':service.store.activity(min(200,int(q.get('limit',100))),max(0,int(q.get('offset',0))),q.get('code',''))})
                if u.path=='/api/events.csv':
                    out=io.StringIO();writer=csv.writer(out);writer.writerow(['时间','事件','代码','依据','已确认'])
                    for e in service.store.activity(10000):writer.writerow([e['ts'],e['kind'],e['code'],encode(e['body']),e['acknowledged']])
                    return self.answer(('\ufeff'+out.getvalue()).encode(),content_type='text/csv; charset=utf-8')
                if u.path=='/api/archives':return self.answer({'snapshots':service.store.snapshots()})
                if u.path=='/api/snapshot':return self.answer(service.store.snapshot_payload(q.get('id','')) or {'error':'快照不存在'})
                if u.path=='/api/watches':return self.answer({'records':service.store.list_watches(),'archived':service.store.list_watches(True)})
                if u.path=='/api/plan-revisions':
                    with service.store.connect() as c:rs=c.execute('SELECT * FROM plan_revision WHERE instance_id=? ORDER BY version DESC',(q.get('id',''),)).fetchall()
                    return self.answer({'revisions':[{**dict(r),'body':json.loads(r['body'])} for r in rs]})
                if u.path=='/api/plan-journal':
                    with service.store.connect() as c:rs=c.execute('SELECT * FROM paper_entry WHERE plan_id=? ORDER BY ts DESC',(q.get('id',''),)).fetchall()
                    return self.answer({'entries':[{**dict(r),'body':json.loads(r['body'])} for r in rs]})
                if u.path=='/api/plans':return self.answer({'plans':service.store.instances(q.get('code'),archived=True)})
                if u.path=='/api/experiments':return self.answer({'experiments':service.store.experiments()})
                if u.path=='/api/experiment':return self.answer(next((r for r in service.store.experiments() if r['id']==q.get('id')),{'error':'实验不存在'}))
                if u.path=='/api/source-archive':
                    with service.store.connect() as c:r=c.execute('SELECT hash,created_at,body FROM source_archive WHERE hash=?',(q.get('hash',''),)).fetchone()
                    import zlib
                    return self.answer({'hash':r['hash'],'created_at':r['created_at'],'files':json.loads(zlib.decompress(r['body']))} if r else {'error':'源码存档不存在'})
                return super().do_GET()
            except (ValueError,TypeError,KeyError) as e:return self.answer({'ok':False,'error':str(e)},400)
            except Exception as e:return self.answer({'ok':False,'error':'读取失败：'+type(e).__name__},500)
        def do_POST(self):
            try:
                n=int(self.headers.get('Content-Length') or 0)
                if not 0<n<=65536:raise ValueError('请求大小无效')
                if 'application/json' not in self.headers.get('Content-Type',''):raise ValueError('只接受JSON请求')
                origin=self.headers.get('Origin');host=self.headers.get('Host')
                if origin and origin not in ('http://'+str(host),'https://'+str(host)):raise ValueError('拒绝跨站写入本机研究库')
                b=json.loads(self.rfile.read(n));path=urlsplit(self.path).path
                if not isinstance(b,dict):raise ValueError('请求必须为对象')
                if path in ('/api/watch','/api/watch-action'):return self.answer(service.watch_action(b))
                if path=='/api/review':
                    field=b.get('field');sid=b.get('strategy_id');code=str(b.get('code',''));value=b.get('value')
                    if sid not in {p['id'] for p in service.catalog['profiles']} or field not in MANUAL_FIELDS:raise ValueError('核验路径/字段无效')
                    if code.startswith('theme:'):
                        if code[6:] not in service.members or field not in ('catalyst_confirmed','theme_freshness','expectation_gap','liquidity_confirmed'):raise ValueError('题材依据只能用于明确支持的题材字段')
                    elif code not in service.assets and code!='*':raise ValueError('核验范围无效')
                    if code=='*' and field not in ('macro_approved','environment_coefficient','behavior_approved'):raise ValueError('个股证据不得全局通过')
                    if field=='environment_coefficient':
                        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not 0<=value<=1:raise ValueError('环境系数须在0—1之间')
                    elif not isinstance(value,bool):raise ValueError('人工条件须明确通过/未通过')
                    service.store.save_review(code+'@'+sid,field,value,str(b.get('evidence','')),str(b.get('expires_at','')));service.wake.set();return self.answer({'ok':True})
                if path=='/api/plan':
                    code=b.get('code');sid=b.get('strategy_id')
                    if code not in {w['code'] for w in service.store.list_watches()}:raise ValueError('请先加入自选')
                    p=next((p for p in service.catalog['profiles'] if p['id']==sid),None)
                    if not p or p['style']!=b.get('style'):raise ValueError('预案风格必须与研究路径一致')
                    result=service.store.save_instance(code,b);service.wake.set();return self.answer({'ok':True,'plan':result})
                if path=='/api/plan-journal':
                    plan=next((p for p in service.store.instances(archived=True) if p['id']==b.get('id')),None)
                    if not plan or b.get('outcome') not in ('continue','invalid','abandon','completed') or not str(b.get('reason','')).strip():raise ValueError('复盘须指定预案、结论与理由')
                    import uuid
                    quote=service.quotes.get(plan['code'],{})
                    entry={'outcome':b['outcome'],'reason':str(b['reason'])[:4000],'reference_price':quote.get('price'),'quote_time':quote.get('quote_time'),'model_only':True,'notice':'参考行情不是成交记录，不计算账户收益'}
                    with service.store.connect() as c:
                        c.execute('INSERT INTO paper_entry VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,plan['id'],plan['code'],plan['style'],stamp(),encode(entry)))
                        service.store._event(c,'plan_review',plan['code'],{'plan_id':plan['id'],**entry})
                    return self.answer({'ok':True})
                if path=='/api/plan-action':service.store.instance_action(b.get('id'),b.get('action'));service.wake.set();return self.answer({'ok':True})
                if path=='/api/event-action':service.store.event_action(b.get('id'),b.get('action'));return self.answer({'ok':True})
                if path=='/api/strategy':
                    from strategy_registry import register_hypothesis
                    profile=register_hypothesis(b);service.store.event('hypothesis_registered','',{'id':profile['id'],'name':profile['name'],'hypothesis':profile['hypothesis'],'validated':False});service.wake.set()
                    return self.answer({'ok':True,'profile':profile})
                if path=='/api/lab':
                    mode=b.get('mode')
                    if mode=='technical':
                        if any(c not in service.assets for c in b.get('codes',[])):raise ValueError('实验代码未在证券目录')
                        from research_backtest import validate_settings,run_technical
                        validate_settings(b.get('settings',{}))
                        # Serialize expensive experiments; frontend receives job status immediately.
                        if getattr(service,'lab_running',False):raise ValueError('已有实验运行中，请先查看实验记录')
                        service.lab_running=True
                        def run():
                            try:run_technical(service.store,b)
                            except Exception as e:
                                import uuid
                                with service.store.connect() as c:c.execute('INSERT INTO experiment VALUES(?,?,?)',(uuid.uuid4().hex,stamp(),encode({'type':'technical_backtest','state':'failed','request':b,'result':{'reason':str(e) if isinstance(e,ValueError) else type(e).__name__},'validated':False})))
                            finally:service.lab_running=False
                        threading.Thread(target=run,daemon=True,name='technical-experiment').start();return self.answer({'ok':True,'state':'queued'})
                    snap=service.store.snapshot_payload(b.get('snapshot',''))
                    if not snap:raise ValueError('请选择有效快照')
                    from strategy_lab import replay,outcomes
                    if mode=='replay':result=replay(snap['payload'])
                    elif mode=='cohort':
                        c=H.connect()
                        try:result=outcomes(snap['payload'],b.get('strategy_id'),int(b.get('horizon',5)),c,selection=b.get('selection','complete'))
                        finally:c.close()
                    else:raise ValueError('实验类型无效')
                    import uuid
                    ident=uuid.uuid4().hex
                    body={'state':'complete','type':mode,'snapshot':snap['id'],'validated':False,'result':result,'request':b}
                    with service.store.connect() as c:c.execute('INSERT INTO experiment VALUES(?,?,?)',(ident,stamp(),encode(body)))
                    return self.answer({'ok':True,'id':ident,'result':result})
                return self.answer({'ok':False,'error':'接口不存在'},404)
            except (ValueError,TypeError,KeyError) as e:return self.answer({'ok':False,'error':str(e)},400)
            except Exception as e:return self.answer({'ok':False,'error':'写入失败：'+type(e).__name__},500)
    return ProductHandler


def main():
    import argparse,fcntl
    from http.server import ThreadingHTTPServer
    ap=argparse.ArgumentParser();ap.add_argument('--port',type=int,default=6063);ap.add_argument('--upstream');a=ap.parse_args()
    lock_path=ROOT/f'cache/product/service-{a.port}.lock';lock_path.parent.mkdir(parents=True,exist_ok=True)
    lock=lock_path.open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('同一端口研究服务已有实例，拒绝重复启动')
    lock.seek(0);lock.truncate();lock.write(str(os.getpid()));lock.flush()
    if os.environ.get('TRADING_MANAGED')=='1':
        import logging
        from logging.handlers import RotatingFileHandler
        log=logging.getLogger('grind-research');log.setLevel(logging.INFO)
        log.addHandler(RotatingFileHandler(ROOT/'cache/product/runtime.log',maxBytes=5_000_000,backupCount=3,encoding='utf-8'))
        class LocalLog:
            def write(self,message):
                if message.strip():log.info(message.rstrip())
                return len(message)
            def flush(self):
                for h in log.handlers:h.flush()
        import sys
        sys.stdout=LocalLog();sys.stderr=LocalLog()
    core=None
    if not a.upstream:
        for key in ('TRADING_BOARD_SCHEDULER','TRADING_BOARD_AUTO_POOL','TRADING_BOARD_LOG_EXPOSURES','TRADING_BOARD_ARCHIVE'):os.environ[key]='0'
        from intraday_board import Board
        core=Board(15)
    service=ProductService(core,a.upstream);threading.Thread(target=service.loop,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',a.port),handler(service,lambda:(ROOT/'templates/intraday_board_v2.html').read_text()))
    print(f'研究看板 http://127.0.0.1:{a.port} · 单实例 · 本机研究 · 无下单接口',flush=True)
    server.serve_forever()

if __name__=='__main__':main()
