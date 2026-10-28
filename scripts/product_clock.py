"""Exchange-calendar clock and per-source coverage contracts. No weekday inference."""
import datetime as dt
import math
from data_runtime import TZ, number


def clock_context(now, calendar):
    now = now.astimezone(TZ)
    day, hm = now.date().isoformat(), now.strftime('%H:%M')
    opened = calendar.get(day)
    prior = sorted(d for d, v in calendar.items() if v and d < day)
    if opened is None: phase, label = 'unknown', '交易日历缺失'
    elif not opened: phase, label = 'closed', '休市回看'
    elif hm < '09:15': phase, label = 'prepare', '盘前准备'
    elif hm < '09:30': phase, label = 'auction', '集合竞价'
    elif hm <= '11:30' or '13:00' <= hm < '15:00': phase, label = 'continuous', '连续交易'
    elif hm < '13:00': phase, label = 'break', '午间休市'
    else: phase, label = 'after_close', '盘后复盘'
    reference_day = day if opened and hm >= '09:15' else prior[-1] if prior else None
    completed = day if opened and hm >= '16:30' else prior[-1] if prior else None
    return {'wall_time': now.isoformat(timespec='seconds'), 'day': day, 'is_open': opened,
            'phase': phase, 'label': label, 'live': phase == 'continuous',
            'reference_day': reference_day, 'history_target': completed,
            'previous_session': prior[-1] if prior else None, 'prior_sessions': prior,
            'execution_available': phase == 'continuous'}


def quote_contract(q, context, now):
    try: qt = dt.datetime.strptime(str(q.get('quote_time')), '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    except (ValueError, TypeError): return {'state': 'unknown', 'valid': False, 'reason': '源时间未知'}
    if qt > now + dt.timedelta(seconds=60): return {'state':'invalid','valid':False,'reason':'源时钟在未来'}
    if not number(q.get('price')) or q['price'] <= 0: return {'state':'missing','valid':False,'reason':'无有效价格'}
    if context['reference_day'] is None: return {'state':'unknown','valid':False,'reason':'交易日历未核验'}
    if qt.date().isoformat() != context['reference_day']:
        return {'state':'stale','valid':False,'reason':'源报价所属日与分析基准不符'}
    if context['live'] and (now-qt).total_seconds() > 90:
        return {'state':'stale','valid':False,'reason':'交易时段源报价超过90秒'}
    return {'state':'live' if context['live'] else 'reference','valid':True,
            'reason':'连续交易源报价' if context['live'] else '最近交易日参考报价，不是当前新成交'}


def market_coverage(rows, expected, source='', minimum=.95):
    expected = set(expected)
    by = {str(r.get('code')): r for r in rows if r.get('code') in expected and number(r.get('price')) and r['price']>0 and number(r.get('pct')) is not None and len(str(r.get('quote_time','')))==14 and str(r.get('quote_time','')).isdigit()}
    missing = sorted(expected-by.keys())
    markets = {}
    def market(c): return 'BJ' if c.startswith(('4','8','92')) else 'SH' if c.startswith(('5','6')) else 'SZ'
    for name in ('SH','SZ','BJ'):
        codes = {c for c in expected if market(c)==name}
        markets[name] = {'expected':len(codes),'returned':len(codes & by.keys()),
                         'ratio':len(codes & by.keys())/len(codes) if codes else None}
    ratio = len(by)/len(expected) if expected else 0
    ids=[r.get('code') for r in rows if r.get('code') in expected]
    duplicate_n=len(ids)-len(set(ids))
    complete = duplicate_n==0 and bool(expected) and ratio >= minimum and all(v['ratio'] is None or v['ratio']>=.8 for v in markets.values())
    return {'source':source,'expected':len(expected),'returned':len(by),'ratio':round(ratio,4),
            'complete':complete,'duplicate_n':duplicate_n,'raw_returned':len(rows),'markets':markets,'missing_n':len(missing),'missing_sample':missing[:30],
            'label':'市场样本覆盖已验收' if complete else '覆盖不足，暂停全市场结论'}


def crosscheck_reference(quotes,health,closes,day,tolerance=.005):
    """Compare only completed-session quotes with same-session verified raw closes."""
    checked=[];conflicts=[]
    for code,q in quotes.items():
        h=health.get(code,{})
        if h.get('state')!='reference' or str(q.get('quote_time',''))[:8]!=str(day).replace('-',''):continue
        close=number(closes.get(code));price=number(q.get('price'))
        if not close or price is None:continue
        checked.append(code);diff=abs(price/close-1)
        h['crosscheck']={'source':'tushare.daily/fund_daily','date':day,'close':close,'relative_error':diff}
        if diff>tolerance:
            h.update(state='conflict',valid=False,reason='与同日已验收Tushare收盘价冲突，暂停条件判断');conflicts.append(code)
    return {'checked_n':len(checked),'conflict_n':len(conflicts),'conflict_codes':conflicts,'tolerance':tolerance,'scope':'仅同日已完成行情；盘中不以昨日收盘冒充交叉验证'}


def suspend_market_conclusions(state,coverage):
    if coverage.get('complete'):return False
    state.update({'sentiment':{},'dual':{'note':'全市场有效行情覆盖不足，暂停双闸与市场情绪结论'},'focus':[],
      'pankou':[],'ladder':{},'catchup':[],'alignment':{},'main_theme':{},'zt_codes':[],'mkt_med_pct':None})
    return True
