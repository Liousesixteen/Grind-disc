"""Source-backed observations; human thesis judgments are never auto-approved."""
import datetime as dt
import json
import sqlite3
import statistics
from data_runtime import ROOT, DEFAULT_DB, ProClient, TZ, number, iso_date


def readonly(path=DEFAULT_DB):
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c


def load_membership(db=DEFAULT_DB):
    c=readonly(db)
    try:
        members={}
        for code,theme in c.execute("SELECT code,concept FROM rel_stock_concept WHERE is_valid=1"):
            members.setdefault(theme,set()).add(code)
        assets={r['code']:dict(r) for r in c.execute('SELECT * FROM dim_stock')}
        return members,assets
    finally:c.close()


def theme_observations(members, quotes, canonical, ladder, quote_health):
    """All configured concepts, not the top-N display focus. Never fuzzy-join names."""
    valid={c:q for c,q in quotes.items() if quote_health.get(c,{}).get('valid')}
    market=[q.get('pct') for q in valid.values() if number(q.get('pct')) is not None]
    median=statistics.median(market) if market else None
    metrics={};code2themes={}
    for theme,codes in members.items():
        rows=[valid[c] for c in codes if c in valid and number(valid[c].get('pct')) is not None]
        coverage=len(rows)/len(codes) if codes else 0
        mean=statistics.mean(r['pct'] for r in rows) if rows and coverage>=.8 else None
        zt=[c for c in codes if c in valid and number(valid[c].get('up_limit')) and valid[c]['price']>=valid[c]['up_limit']-.00001]
        entry=ladder.get(theme,{})
        name=entry.get('龙头')
        leader=next((c for c in codes if quotes.get(c,{}).get('name')==name),None) if name else None
        metrics[theme]={'mean':mean,'excess':mean-median if mean is not None and median is not None else None,
            'breadth':sum(r['pct']>0 for r in rows)/len(rows) if rows and coverage>=.8 else None,
            'member_n':len(codes),'quote_n':len(rows),'coverage':round(coverage,4),'zt_n':len(zt),
            'leader_code':leader,'leader_name':name,'leader_positive':valid[leader]['pct']>0 if leader in valid else None,
            'source':'现行有效概念成员×逐股有效行情','membership_asof':dt.datetime.now(TZ).date().isoformat(),
            'pit_membership':False,'method':'精确同名；覆盖≥80%才计算均涨与宽度；成员时点未历史验证'}
        for c in codes:code2themes.setdefault(c,[]).append(theme)
    main=dict(canonical)
    # When ladder provider is unavailable, do not invent a "leader" or primary theme.
    # Static membership remains available independently, clearly named multi-membership.
    return metrics,main,code2themes


def context_facts(code,asset,features,theme,store,asof):
    facts=store.facts(code,asof)
    observations=[{'kind':'history','label':'量价与位置','source':features.get('history_source'),
        'asof':features.get('history_asof'),'status':'ok' if features.get('contiguous_n',0)>=60 else 'missing',
        'values':{k:features.get(k) for k in ('contiguous_n','ret5','ret20','position60','ma5','ma20','ma60','dry_volume_ratio','signal_a_detail','b_stage')}}]
    if theme:observations.append({'kind':'theme','label':'方向、宽度与同桶核心','source':theme.get('source'),'asof':asof,'status':'ok' if theme.get('mean') is not None else 'missing','values':theme})
    observations.append({'kind':'industry','label':'生意与产业链身份（分类，不是盈利结论）','source':'本地dim_stock/有效概念图谱','asof':asof,'status':'reference','values':{k:asset.get(k) for k in ('sw_l1','sw_l2','sw_l3','biz_chain','biz_seg','biz_note')}})
    for kind in ('valuation','financial','income','cashflow','balance'):
        fact=facts.get(kind)
        observations.append({'kind':kind,'label':{'valuation':'估值事实','financial':'财务指标','income':'利润表','cashflow':'现金流量表','balance':'资产负债表'}[kind],
            'source':'Tushare '+kind,'asof':fact['asof'] if fact else None,'published_at':fact['published_at'] if fact else None,
            'status':fact['status'] if fact else 'missing','values':fact['body'] if fact else {},
            'notice':'仅展示已披露事实；不会自动确认预期差、估值合理或无风险'})
    return observations


def task_due(store,key):
    task=next((t for t in store.task_status() if t['key']==key),None)
    if not task:return True
    if task['status']=='ok':return False
    if task['body'].get('failure_attempts',0)>=3:return False
    try:return (dt.datetime.now(TZ)-dt.datetime.fromisoformat(task['updated_at'])).total_seconds()>=1800
    except (ValueError,TypeError):return False


def update_fundamentals(store,codes,target,provider=None):
    """Small bounded watch/attention universe; cache all-market daily_basic once."""
    provider=provider or ProClient();report=[]
    if task_due(store,'valuation:'+target):
        try:
            df=provider.daily_basic(trade_date=target.replace('-',''),fields='ts_code,trade_date,pe_ttm,pb,ps_ttm,dv_ttm,total_mv,circ_mv,turnover_rate,volume_ratio')
            _,assets=load_membership()
            codes_seen=df['ts_code'].str.split('.').str[0]
            if codes_seen.duplicated().any() or len(set(codes_seen)&set(assets))/max(1,len(assets))<.95:raise ValueError('daily_basic证券覆盖不足95%或重复')
            for r in json.loads(df.to_json(orient='records')):
                if iso_date(r['trade_date'])!=target:raise ValueError('估值日期不符')
                store.put_fact(r['ts_code'].split('.')[0],'valuation',target,target,{**r,'units':'PE/PB倍；市值万元；换手率%','url':'https://tushare.pro/document/2?doc_id=32'})
            store.record_task('valuation:'+target,'ok',{'n':len(df),'source':'tushare.daily_basic'})
        except Exception as e:store.record_task('valuation:'+target,'unavailable',{'reason':type(e).__name__})
    fields={
      'financial':('fina_indicator','ts_code,ann_date,end_date,roe,roe_dt,netprofit_yoy,dt_netprofit_yoy,or_yoy,grossprofit_margin,debt_to_assets,ocf_to_profit'),
      'income':('income','ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,revenue,operate_profit,n_income_attr_p'),
      'cashflow':('cashflow','ts_code,ann_date,f_ann_date,end_date,report_type,n_cashflow_act,free_cashflow'),
      'balance':('balancesheet','ts_code,ann_date,f_ann_date,end_date,report_type,total_assets,total_liab,total_hldr_eqy_exc_min_int')}
    for code in codes[:20]:
        ts=code+('.SH' if code.startswith('6') else '.BJ' if code.startswith(('4','8','92')) else '.SZ')
        for kind,(api,cols) in fields.items():
            key=f'{kind}:{code}:{target}'
            if not task_due(store,key):continue
            try:
                df=getattr(provider,api)(ts_code=ts,limit=12,fields=cols)
                records=json.loads(df.to_json(orient='records'))
                valid=[]
                for r in records:
                    announced=str(r.get('f_ann_date') or r.get('ann_date') or '')
                    end=str(r.get('end_date') or '')
                    if len(announced)==8 and len(end)==8 and end<=announced<=target.replace('-',''):
                        if r.get('report_type') not in (None,'1',1):continue
                        valid.append(r)
                valid.sort(key=lambda r:(str(r.get('end_date')),str(r.get('f_ann_date') or r.get('ann_date'))),reverse=True)
                if not valid:raise ValueError('无截止日前已披露的规范财务记录')
                published=iso_date(valid[0].get('f_ann_date') or valid[0]['ann_date'])
                store.put_fact(code,kind,published,target,{'records':valid[:8],'api':api,'currency':'CNY','notice':'流量表为累计期间口径，不直接跨季度相加；披露日不精确到时分秒，盘中不用于历史触发'})
                store.record_task(key,'ok',{'n':len(valid),'source':'tushare.'+api});report.append({'code':code,'kind':kind,'state':'ok'})
            except Exception as e:
                store.record_task(key,'unavailable',{'reason':type(e).__name__});report.append({'code':code,'kind':kind,'state':'unavailable'})
    return report
