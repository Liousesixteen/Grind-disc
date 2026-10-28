"""Declarative daily hypotheses; no executable expressions or removable risk gates."""
import copy
import datetime as dt
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
from data_runtime import ROOT,TZ
from strategy_engine import load_catalog,compare,CATALOG

DAILY_FIELDS=frozenset(['signal_a_classes','ret5','ret10','ret20','ret60','position60','drawdown60','dry_volume_ratio','no_new_low10','range_contracting','completed_launch','b_nextday_confirmed','shape_a','momentum_turn'])
BOOL_FIELDS=frozenset(['no_new_low10','range_contracting','completed_launch','b_nextday_confirmed','shape_a','momentum_turn'])
OPS=frozenset(['gt','gte','lt','lte','eq','between'])


def validate_spec(spec):
    if not isinstance(spec,list) or not 1<=len(spec)<=6:raise ValueError('技术假设须有1—6项条件')
    for r in spec:
        if not isinstance(r,dict) or r.get('field') not in DAILY_FIELDS or r.get('op') not in OPS:raise ValueError('只允许已完成日线字段与白名单比较，不执行表达式')
        v=r.get('value')
        if r['field'] in BOOL_FIELDS:
            if r['op']!='eq' or not isinstance(v,bool):raise ValueError('布尔字段须使用eq true/false')
        else:
            values=v if r['op']=='between' else [v]
            if not isinstance(values,list) or (r['op']=='between' and len(values)!=2) or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in values):raise ValueError('比较阈值须为有限数字')
            if r['op']=='between' and values[0]>values[1]:raise ValueError('区间下限不得高于上限')
    return copy.deepcopy(spec)


def register_hypothesis(body,path=None):
    path=Path(path or CATALOG)
    name=str(body.get('name','')).strip();hypothesis=str(body.get('hypothesis','')).strip();invalid=str(body.get('invalidation','')).strip()
    if not 2<=len(name)<=60 or len(hypothesis)<10 or len(invalid)<5:raise ValueError('请写明策略名称、可证伪假设及失效条件')
    spec=validate_spec(body.get('conditions'));lock=path.with_suffix('.lock')
    with lock.open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX)
        catalog=load_catalog(path)
        if len(catalog['profiles'])>=16:raise ValueError('孵化策略已达16条上限，先复审/退役现有假设')
        if any(p['name']==name for p in catalog['profiles']):raise ValueError('策略名称已存在，不覆盖原版本')
        base=copy.deepcopy(next(p for p in catalog['profiles'] if p['id']=='wind_a'))
        ident='lab_'+uuid.uuid4().hex[:10];source='docs/自定义策略研究协议.md 一 研究假设登记'
        base.update({'id':ident,'name':name,'version':'0.1.0','style':'波段','lifecycle':'incubation','validated':False,'hypothesis':hypothesis,'created_at':dt.datetime.now(TZ).isoformat(),'invalidations':[invalid,'环境/风险核验未通过','条件变化或数据不可核验'],'backtest_spec':spec,'sources':base['sources']+[source],
          'capabilities':{'technical_backtest':True,'point_in_time_full_strategy':False,'auto_orders':False}})
        mandatory=[r for r in base['rules'] if r['kind']!='condition']
        base['rules']=mandatory+[{'id':'hypothesis_'+str(i),'field':r['field'],'label':r.get('label') or r['field'],'op':r['op'],'value':r['value'],'source':source,'kind':'condition','evidence_grade':'待回测假设，用户登记','reason':hypothesis+'；失效：'+invalid} for i,r in enumerate(spec,1)]
        catalog['profiles'].append(base);catalog.pop('hash',None);catalog.pop('source_drift',None)
        source_path=ROOT/'docs/自定义策略研究协议.md'
        if source_path.exists() and not any(r['path']=='docs/自定义策略研究协议.md' for r in catalog['source_files']):catalog['source_files'].append({'path':'docs/自定义策略研究协议.md','sha256':hashlib.sha256(source_path.read_bytes()).hexdigest()})
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n');temp.replace(path)
        return base
