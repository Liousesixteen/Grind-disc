#!/usr/bin/env python3
"""Frozen-input replay and prospective cohort outcomes, never simulated fills.

Usage: strategy_lab.py replay --snapshot ID
       strategy_lab.py outcomes --snapshot ID --strategy wind_a --horizon 5
Missing future sessions remain pending. No reconstruction of historical theme pools.
"""
import argparse
import copy
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path
from product_store import ProductStore, encode, stamp
from strategy_engine import evaluate, evaluate_dimensions, apply_reviews
import strategy_history as H


def replay(payload):
    expected = payload.get('engine_sha256')
    if expected and expected != hashlib.sha256(Path(__file__).with_name('strategy_engine.py').read_bytes()).hexdigest():
        raise ValueError('引擎源码版本不一致：请先切回该快照对应Git版本；禁止用新算法冒充原样复算')
    now = dt.datetime.fromisoformat(payload['evaluated_at'])
    output = {}
    for p in payload['catalog']['profiles']:
        output[p['id']] = {}
        for code, base in payload['features'].items():
            f = copy.deepcopy(base)
            apply_reviews(f, payload.get('reviews', []), code + '@' + p['id'], now)
            if base.get('risk_review') is False: f['risk_review'] = False
            profile = copy.deepcopy(p)
            if 'source_drift' in payload['catalog']:
                profile['rules'].append({'id':'source_version_match','field':'source_version_match','label':'规则映射的源文件版本一致','op':'eq','value':True,'source':'config/strategy_catalog.json source_files SHA256','kind':'data','evidence_grade':'版本事实','reason':'体系源已改变时须重新审核映射，不能沿用已通过结论'})
                f['source_version_match'] = not any(any(src.startswith(path) for src in p.get('sources',[])) or path=='DATA_SOURCE_POLICY.md' for path in payload['catalog']['source_drift'])
            if payload['catalog'].get('schema_version',1)>=2:
                profile['rules'].append({'id':'instrument_allowed','field':'instrument_allowed','label':'默认交易工具与例外授权','op':'eq','value':True,'source':'AGENTS.md 半导体默认ETF','kind':'policy','evidence_grade':'硬纪律','reason':'工具限制独立标记，不能解释为公司重大负面'})
                profile['rules'].sort(key=lambda r: {'instrument_allowed':1,'source_version_match':2}.get(r['id'],0))
                result=evaluate_dimensions(profile,f)
            else:result=evaluate(profile,f)
            output[p['id']][code] = {k: result[k] for k in (('status','passed','missing','failed','setup_state','evidence_state','policy_state','execution_state') if payload['catalog'].get('schema_version',1)>=2 else ('status','passed','missing','failed'))}
    return output


def outcomes(payload, strategy, horizon, connection, selection='complete'):
    """T+1 open to horizon close observation; no fill/position/strategy PnL claim."""
    results = replay(payload)
    if strategy not in results: raise ValueError('策略不存在于该冻结快照')
    if horizon not in (1, 5, 10, 20): raise ValueError('观察窗口仅支持1/5/10/20交易日')
    if selection not in ('complete','technical'):raise ValueError('观察口径无效')
    day = payload['evaluated_at'][:10]
    # Calendar must know the actual future sessions, not just available bars.
    sessions = [r[0] for r in connection.execute('SELECT date FROM calendar WHERE is_open=1 AND date>? ORDER BY date LIMIT ?', (day, horizon))]
    ready = len(sessions) == horizon
    def result_for(code):
        if not ready: return {'code': code, 'state': 'pending', 'reason': '未来交易日历/窗口尚不完整'}
        rows = [dict(r) for r in connection.execute('SELECT * FROM bars WHERE code=? AND date>=? AND date<=? ORDER BY date', (code,sessions[0],sessions[-1]))]
        if [r['date'] for r in rows] != sessions: return {'code':code,'state':'pending','reason':'未来窗口未发生或有停牌/缺失，不填零收益'}
        entry = rows[0]['open']*rows[0]['adj']; exit_price=rows[-1]['close']*rows[-1]['adj']
        return {'code':code,'state':'observed','return_pct':(exit_price/entry-1)*100,
                'mae_pct':(min(r['low']*r['adj'] for r in rows)/entry-1)*100,
                'mfe_pct':(max(r['high']*r['adj'] for r in rows)/entry-1)*100,
                'entry_date':sessions[0],'end_date':sessions[-1]}
    universe = [result_for(c) for c in payload['features']]
    selected_codes = {c for c,r in results[strategy].items() if (r.get('setup_state')=='confirmed' if selection=='technical' else r['status']=='complete')}
    selected = [r for r in universe if r['code'] in selected_codes]
    a=[r['return_pct'] for r in selected if r['state']=='observed']; b=[r['return_pct'] for r in universe if r['state']=='observed']
    return {'type':'前瞻队列标的表现，非成交回测', 'strategy':strategy,'horizon':horizon,'signal_date':day,'selection':selection,
        'eligible_n':len(selected_codes),'observed_n':len(a),'pending_n':len(selected)-len(a),
        'equal_weight_mean_pct':statistics.mean(a) if a else None,
        'frozen_universe_mean_pct':statistics.mean(b) if b else None,
        'excess_pct':statistics.mean(a)-statistics.mean(b) if a and b else None,
        'rows':selected,'limitations':['T+1开盘至窗口末收盘只是观测口径','未模拟涨跌停不可成交、滑点、费用或仓位','股票池和人工证据使用当时冻结快照','同池等权基准只统计完整窗口，须报告缺失样本','未经足够样本外与多重试验控制，不宣称有效']}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=('list','replay','outcomes'))
    ap.add_argument('--snapshot');ap.add_argument('--strategy',default='halfway_233');ap.add_argument('--horizon',type=int,default=5)
    ap.add_argument('--out',help='可选JSON报告路径')
    args=ap.parse_args();store=ProductStore()
    if args.command=='list': result=store.snapshots()
    else:
        snap=store.snapshot_payload(args.snapshot or '')
        if not snap: raise SystemExit('需要有效冻结快照ID；先运行6063研究服务')
        payload=snap['payload']
        if 'evaluated_at' not in payload: raise SystemExit('旧快照不含核验时刻，不能假装可重演；请选择新版快照')
        if args.command=='replay': result={'snapshot':snap['id'],'catalog_hash':snap['registry_hash'],'results':replay(payload),'kind':'冻结输入复算；不是回测收益'}
        else:
            c=H.connect()
            try:result=outcomes(payload,args.strategy,args.horizon,c)
            finally:c.close()
        trial=hashlib.sha256(encode({'snapshot':snap['id'],'command':args.command,'strategy':args.strategy,'horizon':args.horizon,'catalog':snap['registry_hash']}).encode()).hexdigest()[:24]
        with store.connect() as c:c.execute('INSERT OR IGNORE INTO experiment VALUES(?,?,?)',(trial,stamp(),encode({'snapshot':snap['id'],'command':args.command,'strategy':args.strategy,'horizon':args.horizon,'registry_hash':snap['registry_hash'],'engine_sha256':hashlib.sha256(Path(__file__).with_name('strategy_engine.py').read_bytes()).hexdigest(),'validated':False})))
    body=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.out:Path(args.out).write_text(body+'\n')
    else:print(body)


if __name__=='__main__':main()
