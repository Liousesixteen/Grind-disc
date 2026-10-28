"""Inspectable three-state rule engine. No weighted total, probabilities or orders."""
import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from data_runtime import ROOT, TZ, number

CATALOG = Path(os.environ.get('TRADING_STRATEGY_CATALOG', ROOT / 'config/strategy_catalog.json'))
MANUAL_FIELDS = frozenset(('macro_approved', 'risk_review', 'environment_coefficient',
    'leadership_verified', 'expectation_gap', 'support_approved', 'catalyst_confirmed',
    'theme_freshness', 'boge_l1', 'boge_l2', 'counter_scenario', 'industry_verified',
    'fundamental_verified', 'valuation_verified', 'behavior_approved', 'liquidity_confirmed', 'execution_plan_verified', 'first_board_quality', 'right_side_confirmed'))


def load_catalog(path=CATALOG):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    ids = set()
    for p in data['profiles']:
        if p['id'] in ids or p.get('validated') is not False:
            raise ValueError('重复策略或未经独立验收的验证状态')
        ids.add(p['id'])
        if p.get('rank') and (len(set(p['rank']))!=len(p['rank']) or set(p['rank'])-{'readiness','setup_state','failed_conditions','pool_tier','relative20','risk_adj','missing_count'}):raise ValueError('排序字段不支持')
        for rule in p['rules']:
            if rule['op'] not in ('eq', 'gt', 'gte', 'lt', 'lte', 'between', 'in'):
                raise ValueError('规则运算符不支持；禁止执行表达式')
            if not rule.get('source') or not rule.get('reason'):
                raise ValueError('规则缺来源或失效解释')
    data['source_drift'] = [r['path'] for r in data.get('source_files', [])
                            if not (ROOT / r['path']).is_file() or hashlib.sha256((ROOT / r['path']).read_bytes()).hexdigest() != r['sha256']]
    data['hash'] = hashlib.sha256(raw).hexdigest()[:16]
    return data


def compare(value, op, target):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    if op == 'eq':
        return value == target if not isinstance(target, bool) or isinstance(value, bool) else None
    if op == 'in':
        return value in target
    if number(value) is None:
        return None
    if op == 'gt': return value > target
    if op == 'gte': return value >= target
    if op == 'lt': return value < target
    if op == 'lte': return value <= target
    if op == 'between': return target[0] <= value <= target[1]
    raise ValueError('unsupported comparator')


def evaluate(profile, features):
    rules = []
    for spec in profile['rules']:
        value = features.get(spec['field'])
        passed = compare(value, spec['op'], spec['value'])
        rules.append({**spec, 'observed': value, 'state': 'unknown' if passed is None else 'pass' if passed else 'fail',
                      'provenance': features.get('_provenance', {}).get(spec['field'], '缺有效证据' if value is None else '已取得观测值')})
    failed = [r['label'] for r in rules if r['state'] == 'fail']
    missing = [r['label'] for r in rules if r['state'] == 'unknown']
    status = 'blocked' if failed else 'pending' if missing else 'complete'
    return {'strategy_id': profile['id'], 'strategy_name': profile['name'], 'version': profile['version'],
            'style': profile['style'], 'lifecycle': profile['lifecycle'], 'validated': False,
            'status': status, 'label': {'blocked': '条件不符·观察', 'pending': '证据待补', 'complete': '条件齐备·仍待验证'}[status],
            'passed': sum(r['state'] == 'pass' for r in rules), 'missing': missing, 'failed': failed,
            'rules': rules, 'invalidations': profile['invalidations'], 'execution': '不产生下单/仓位指令'}


def quote_valid(quote, day, now, calendar_open):
    try:
        q = dt.datetime.strptime(str(quote.get('quote_time')), '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None
    if number(quote.get('price')) is None or quote['price'] <= 0:
        return False
    if number(quote.get('amount')) is not None and quote['amount'] <= 0:
        return False
    if calendar_open is None:
        return None
    if q.date().isoformat() != day or q > now + dt.timedelta(seconds=60):
        return False
    clock = now.strftime('%H:%M')
    active = calendar_open and ('09:30' <= clock <= '11:30' or '13:00' <= clock <= '15:00')
    return (now - q).total_seconds() <= 90 if active else calendar_open


def apply_reviews(features, reviews, code, now):
    """Global → theme → security, with explicit global environmental veto."""
    ident,sid=code.rsplit('@',1)
    scope=['*@'+sid]
    if features.get('scene'): scope.append('theme:'+features['scene']+'@'+sid)
    scope.append(code)
    valid=[]
    for r in reviews:
        if r['code'] not in scope or r['field'] not in MANUAL_FIELDS:continue
        try:
            expires=dt.datetime.fromisoformat(r['expires_at']);reviewed=dt.datetime.fromisoformat(r['reviewed_at'])
            if expires<=now or reviewed>now or not r['evidence'].strip():continue
            value=json.loads(r['value'])
        except (ValueError,TypeError):continue
        valid.append((scope.index(r['code']),r,value))
    for _,r,value in sorted(valid,key=lambda x:(x[0],x[1]['reviewed_at'])):
        features[r['field']]=value
        features['_provenance'][r['field']]='人工核验 '+r['reviewed_at']+'；范围 '+r['code']+'；'+r['evidence']
    for _,r,value in valid:
        if r['code']=='*@'+sid and r['field'] in ('macro_approved','behavior_approved') and value is False:
            features[r['field']]=False
            features['_provenance'][r['field']]='全局环境/门禁否决：'+r['evidence']
        if r['code']=='*@'+sid and r['field']=='environment_coefficient' and isinstance(value,(float,int)):
            features['environment_coefficient']=min(value,features.get('environment_coefficient',value))


def enrich_daily_signals(history):
    """Completed-bar signal A; the three categories cannot double-count each other."""
    h = dict(history)
    c, previous = h.get('last_close'), h.get('previous_close')
    ma = None if c is None or previous is None or h.get('ma20') is None or h.get('previous_ma20') is None else c > h['ma20'] and previous <= h['previous_ma20']
    shape = h.get('shape_a')
    indicator = h.get('momentum_turn')
    h['signal_a_detail'] = {'ma20_cross_up': ma, 'price_volume_shape': shape, 'macd_rsi_turn': indicator}
    vals = [ma, shape, indicator]
    # Known two classes can confirm >=2; otherwise missing category stays unknown.
    h['signal_a_classes'] = sum(v is True for v in vals) if all(v is not None for v in vals) or sum(v is True for v in vals) >= 2 else None
    return h


def build_features(row, quote, state, history, reviews, now, calendar_open):
    code, day = row['code'], now.date().isoformat()
    h = enrich_daily_signals(history)
    f = {**h, '_provenance': {k: 'Tushare已完成日线；截至' + str(h.get('history_asof')) for k in h}}
    f['_provenance']['signal_a_classes'] = '已完成日线三类代理，不重复计数：' + json.dumps(h.get('signal_a_detail'), ensure_ascii=False)
    f.update({'quote_valid': (state.get('quote_health',{}).get(code,{}).get('valid') if 'quote_health' in state else quote_valid(quote, day, now, calendar_open)),
              'not_st': not ('ST' in str(row.get('name', '')).upper() or '退' in str(row.get('name', ''))),
              'is_etf': row.get('asset_type') == 'etf',
              'prepool_eligible': row.get('池层') in ('核心', '备选') and str(state.get('research_pool_date', state.get('data_date', ''))).replace('-', '') == str(state.get('previous_session', '')).replace('-', '') and bool(state.get('previous_session')),
              'pool_three_passed': None if any(row.get(k) is None for k in ('筛1_股性', '筛2_辨识度', '筛3_量价')) else all(bool(row[k]) for k in ('筛1_股性', '筛2_辨识度', '筛3_量价')),
              'pool_tier': row.get('池层') or '池外', 'volume_ratio': number(quote.get('liangbi')),
              'days_since_zt': h.get('days_since_zt'),
              'price': number(quote.get('price')), 'pct': number(quote.get('pct'))})
    for key in ('quote_valid', 'volume_ratio', 'price', 'pct'):
        f['_provenance'][key] = str(quote.get('src') or state.get('src') or '源未知')+' 行情 '+str(quote.get('quote_time') or '未知')
    f['_provenance']['prepool_eligible'] = '盘前股池 ' + str(state.get('research_pool_date',state.get('data_date'))) + '；仅截至上一收盘'
    if not f['prepool_eligible']:
        for key in ('pool_three_passed', 'days_since_zt'):
            f[key] = None
    price = f['price']
    reference_high = h.get('previous_high') if str(quote.get('quote_time',''))[:8] == str(h.get('history_asof','')).replace('-','') else h.get('prior_high')
    f['above_previous_high'] = price > reference_high if price is not None and reference_high is not None else None
    up = number(quote.get('up_limit'))
    f['not_limit_up'] = price < up and (number(quote.get('amount')) or 0) > 0 if price is not None and up else None
    # Core/follow confirmation uses the same named bucket; co-membership is not causality.
    related = next((x for x in state.get('candidates', []) if x.get('code') == code), None)
    scene = row.get('_scene') or (related or {}).get('场景')
    focus = next((x for x in state.get('focus', []) if x.get('name') == scene), None)
    metric=(state.get('theme_metrics') or {}).get(scene)
    if metric:
        f.update({'theme_positive':metric.get('mean'),'theme_excess':metric.get('excess'),'theme_breadth':metric.get('breadth'),'leader_positive':metric.get('leader_positive'),'ladder_zt':metric.get('zt_n')})
        for key in ('theme_positive','theme_excess','theme_breadth','leader_positive','ladder_zt'):
            f['_provenance'][key]=metric.get('source','')+'；成员'+str(metric.get('member_n'))+'，有效'+str(metric.get('quote_n'))+'；'+metric.get('method','')
    elif focus:
        f['theme_positive'], f['theme_excess'] = number(focus.get('eq_pct')), number(focus.get('exc'))
        ups = str(focus.get('ups') or '').split('/')
        try: f['theme_breadth'] = int(ups[0]) / int(ups[1])
        except (ValueError, IndexError, ZeroDivisionError): f['theme_breadth'] = None
    leader = (related or {}).get('龙头')
    parent = next((x for x in state.get('pankou', []) if x.get('题材') == scene), None)
    if parent:
        f['ladder_zt'] = number(parent.get('梯队涨停'))
        leader = leader or parent.get('龙头')
    leader_q = next((x for x in state.get('pankou', []) if x.get('name') == leader), None)
    if not metric:
        f['leader_positive'] = leader_q.get('涨幅') > 0 if leader_q and number(leader_q.get('涨幅')) is not None else None
    f['_provenance']['leader_positive'] = '同一主归属桶的已识别龙头快照；不是带动性因果证据'
    if scene:
        f['scene'] = scene
    # A current ex-rights reference may differ from yesterday's raw close. Until the
    # current adjustment is verified, do not compare two incompatible price bases.
    pc = number(quote.get('prev_close'))
    expected_close=h.get('previous_close') if str(quote.get('quote_time',''))[:8]==str(h.get('history_asof','')).replace('-','') else h.get('last_close')
    f['price_basis_verified']=bool(pc and expected_close and abs(pc/expected_close-1)<=.005)
    if pc and expected_close and abs(pc / expected_close - 1) > .005:
        for key in ('above_previous_high', 'signal_a_classes', 'b_nextday_confirmed', 'support_approved'):
            f[key] = None
            f['_provenance'][key] = '当日昨收与历史收盘不一致，复权基准待核验'
    # These are policy inputs, never inferred from a red/green short-term lamp.
    if '半导体' in ''.join(str(row.get(k,'')) for k in ('板块','行业标签','biz_chain','sw_l2','sw_l3')) and row.get('asset_type') != 'etf':
        f['instrument_allowed'] = False
        f['_provenance']['instrument_allowed'] = '半导体默认ETF；个股例外未获单独授权，仍可研究；不等于公司有重大负面'
    f.setdefault('instrument_allowed', True)
    f['data_state']=state.get('quote_health',{}).get(code,{}).get('state','unknown')
    f['live_execution_window']=state.get('clock',{}).get('execution_available',False)
    f['history_target']=state.get('history_target')
    return f


def evaluate_universe(catalog, rows, quotes, state, histories, reviews, now, calendar_open):
    feature_map = {r['code']: build_features(r, quotes.get(r['code'], {}), state,
                                           histories.get(r['code'], {}), reviews, now, calendar_open) for r in rows}
    groups = {}
    for code, f in feature_map.items():
        if f.get('ret20') is not None and f.get('scene'):
            groups.setdefault(f['scene'], []).append(f['ret20'])
    for code, f in feature_map.items():
        a = groups.get(f.get('scene'), [])
        f['relative20'] = f['ret20'] - statistics.median(a) if len(a) >= 3 and f.get('ret20') is not None else None
        f['_provenance']['relative20'] = '同题材本次研究池、至少3只有效历史样本的中位差；非全市场行业指数'
    for code, f in feature_map.items():
        vol = number(f.get('vol20'))
        f['risk_adj'] = f['relative20'] / vol if f.get('relative20') is not None and vol and vol > 0 else None
        f['_provenance']['risk_adj'] = '20日相对收益 ÷ 20日已实现波动；分母缺失或<=0 留空，不填0'
    for code, f in feature_map.items():
        series = f.get('ret20_last3')
        peers = [x.get('ret20_last3') for x in feature_map.values() if x.get('scene') == f.get('scene') and x.get('ret20_last3')]
        if f.get('scene') and series and len(peers) >= 3:
            flags = [series[i] > statistics.median(p[i] for p in peers) for i in range(3)]
            f['rs_persistence'] = next((i for i, v in enumerate(reversed(flags)) if not v), 3)
            f['_provenance']['rs_persistence'] = '本次题材成员回看最近3个已完成交易日20日相对收益；非历史成员池、非因果回测'
    output = {}
    names = {r['code']: r['name'] for r in rows}
    for p in catalog['profiles']:
        scoped = {}
        for code, base in feature_map.items():
            f = copy.deepcopy(base)
            policy_risk = f.get('risk_review')
            apply_reviews(f, reviews, code + '@' + p['id'], now)
            if policy_risk is False:
                f['risk_review'] = False
                f['_provenance']['risk_review'] = base['_provenance']['risk_review']
            scoped[code] = f
        profile = copy.deepcopy(p)
        profile['rules'].append({'id':'instrument_allowed','field':'instrument_allowed','label':'默认交易工具与例外授权','op':'eq','value':True,'source':'AGENTS.md 半导体默认ETF','kind':'policy','evidence_grade':'硬纪律','reason':'工具限制独立标记，不能解释为公司重大负面'})
        profile['rules'].append({'id':'source_version_match','field':'source_version_match','label':'规则映射的源文件版本一致','op':'eq','value':True,'source':'config/strategy_catalog.json source_files SHA256','kind':'data','evidence_grade':'版本事实','reason':'体系源已改变时须重新审核映射，不能沿用已通过结论'})
        for f in scoped.values():
            f['source_version_match'] = not any(any(src.startswith(path) for src in p.get('sources',[])) or path=='DATA_SOURCE_POLICY.md' for path in catalog.get('source_drift',[]))
            f['_provenance']['source_version_match'] = '源文件哈希核验；变化：' + ','.join(catalog.get('source_drift', []))
        results = [{**evaluate_dimensions(profile, f), 'code': code, 'name': names[code],
                    'scene': f.get('scene', ''), 'tier': f['pool_tier'], 'pct': f.get('pct'),
                    'history_n': f.get('contiguous_n', 0), 'quote_time': quotes.get(code, {}).get('quote_time')}
                   for code, f in scoped.items()]
        def rank_key(r):
            f=feature_map[r['code']]
            order=p.get('rank') or ['readiness','setup_state','failed_conditions','pool_tier','relative20','missing_count']
            r['rank_basis']={k:(rank_value(f,r,k) if math.isfinite(rank_value(f,r,k)) else None) for k in order}
            return tuple(rank_value(f,r,k) for k in order)+(r['code'],)
        results.sort(key=rank_key)
        for i, r in enumerate(results, 1): r['rank'] = i
        output[p['id']] = results
    return output, feature_map


def rank_value(f, r, key):
    """Sort keys are explicit: hard gates first, then expected excess per unit risk."""
    return {'readiness': {'complete': 0, 'pending': 1, 'blocked': 2, 'restricted': 3, 'unavailable': 4}.get(r['status'], 5),
            'setup_state': {'confirmed': 0, 'forming': 1, 'unknown': 2, 'failed': 3}.get(r['setup_state'], 4),
            'failed_conditions': r['setup_failed'],
            'missing_count': len(r['missing']),
            'pool_tier': {'核心': 0, '备选': 1}.get(r['tier'], 2),
            'relative20': -f['relative20'] if number(f.get('relative20')) is not None else float('inf'),
            'risk_adj': -f['risk_adj'] if number(f.get('risk_adj')) is not None else float('inf')}[key]


def evaluate_dimensions(profile,features):
    result=evaluate(profile,features)
    automatic=[r for r in result['rules'] if r['kind']=='condition']
    unknown=[r for r in automatic if r['state']=='unknown']; failed=[r for r in automatic if r['state']=='fail']
    setup='failed' if failed else 'unknown' if unknown else 'confirmed'
    if unknown and any(r['state']=='pass' for r in automatic) and not failed:setup='forming'
    evidence=[r for r in result['rules'] if r['kind']=='manual']
    manual_failed=any(r['state']=='fail' for r in evidence)
    source_changed=features.get('source_version_match') is False
    data_failed=features.get('quote_valid') is not True
    policy=features.get('instrument_allowed') is False or features.get('not_st') is False
    status='unavailable' if data_failed else 'restricted' if policy else 'blocked' if failed or manual_failed else 'pending' if result['missing'] or source_changed else 'complete'
    result.update({'status':status,'label':{'unavailable':'数据待修复','restricted':'工具限制·可研究','blocked':'形态/研究条件未满足','pending':'形态观察·待补证','complete':'条件齐备·实验观察'}[status],
      'setup_state':setup,'setup_label':{'confirmed':'技术条件已形成','forming':'技术条件形成中','failed':'技术条件未形成','unknown':'技术输入不足'}[setup],
      'setup_passed':sum(r['state']=='pass' for r in automatic),'setup_total':len(automatic),'setup_failed':len(failed),
      'price_basis_verified':features.get('price_basis_verified'),'evidence_state':'expired' if source_changed else 'rejected' if manual_failed else 'pending' if any(r['state']=='unknown' for r in evidence) else 'reviewed',
      'data_state':features.get('data_state','unknown'),'policy_state':'restricted' if policy else 'allowed',
      'execution_state':'reference_only' if not features.get('live_execution_window') else 'experimental_ready' if status=='complete' else 'not_ready',
      'next_confirmation':next((r['label'] for r in result['rules'] if r['state']!='pass' and r['kind'] in ('data','policy')),None) or ((failed+unknown+[r for r in evidence if r['state']!='pass'])[0]['label'] if failed or unknown or any(r['state']!='pass' for r in evidence) else '继续验证，非下单指令'),
      'blockers':[r['label'] for r in result['rules'] if r['state']!='pass']})
    return result
