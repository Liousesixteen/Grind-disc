"""Causal, constrained daily technical experiments; no live order/account connection."""
import datetime as dt
import hashlib
import json
import math
import statistics
import uuid
from data_runtime import TZ,ProClient,iso_date,number
from product_store import encode,stamp
from strategy_engine import enrich_daily_signals
import strategy_history as H


def execution_inputs(codes,dates,provider=None,db=H.HISTORY_DB):
    provider=provider or ProClient();c=H.connect(db)
    c.executescript('''CREATE TABLE IF NOT EXISTS execution_limit(date TEXT,code TEXT,up REAL,down REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS dividend_event(code TEXT,ex_date TEXT,body TEXT,PRIMARY KEY(code,ex_date));
CREATE TABLE IF NOT EXISTS execution_partition(kind TEXT,key TEXT,status TEXT,note TEXT,PRIMARY KEY(kind,key));''')
    failed=[]
    try:
        for code in codes:
            key=code+':'+dates[0]+':'+dates[-1]
            if c.execute("SELECT 1 FROM execution_partition WHERE kind='limits_code' AND key=? AND status='ok'",(key,)).fetchone():continue
            try:
                ts=code+('.SH' if code.startswith('6') else '.BJ' if code.startswith(('4','8','92')) else '.SZ')
                df=provider.stk_limit(ts_code=ts,start_date=dates[0].replace('-',''),end_date=dates[-1].replace('-',''),fields='ts_code,trade_date,up_limit,down_limit')
                rows=[]
                for r in df.to_dict('records'):
                    day=iso_date(r['trade_date'])
                    if r['ts_code']!=ts or not dates[0]<=day<=dates[-1]:raise ValueError('涨跌停证券/日期不符')
                    up,dn=number(r.get('up_limit')),number(r.get('down_limit'))
                    if up and dn and up>dn:rows.append((day,code,up,dn))
                if not rows or len(rows)>=6000:raise ValueError('涨跌停价格缺失/截断')
                with c:
                    c.executemany('INSERT OR REPLACE INTO execution_limit VALUES(?,?,?,?)',rows)
                    c.execute("INSERT OR REPLACE INTO execution_partition VALUES('limits_code',?,'ok','tushare stk_limit')",(key,))
            except Exception as e:
                failed.append({'kind':'limits','code':code,'reason':type(e).__name__})
                if len(failed)>=3:break
        for code in codes:
            key=code+':'+dates[-1]
            if c.execute("SELECT 1 FROM execution_partition WHERE kind='dividend' AND key=? AND status='ok'",(key,)).fetchone():continue
            try:
                ts=code+('.SH' if code.startswith('6') else '.BJ' if code.startswith(('4','8','92')) else '.SZ')
                df=provider.dividend(ts_code=ts,fields='ts_code,ann_date,end_date,div_proc,stk_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate')
                count=0
                with c:
                    for r in json.loads(df.to_json(orient='records')):
                        ex=str(r.get('ex_date') or '');ann=str(r.get('ann_date') or '')
                        if r.get('div_proc')!='实施' or len(ex)!=8 or len(ann)!=8 or ann>ex:continue
                        if dates[0].replace('-','')<=ex<=dates[-1].replace('-',''):
                            c.execute('INSERT OR REPLACE INTO dividend_event VALUES(?,?,?)',(code,iso_date(ex),encode(r)));count+=1
                    c.execute("INSERT OR REPLACE INTO execution_partition VALUES('dividend',?,'ok',?)",(key,'tushare dividend; '+str(count)))
            except Exception as e:failed.append({'kind':'dividend','code':code,'reason':type(e).__name__})
    finally:c.close()
    return failed


def validate_settings(settings):
    defaults={'capital':100000.,'position_fraction':.10,'first_fraction':1/3,'max_exposure':.60,'stop_pct':.08,'target_pct':.16,'holding_days':5,
        'commission_bps':3.,'commission_min':5.,'sell_tax_bps':5.,'slippage_bps':5.,'dividend_tax':.20,'max_participation':.01,
        'risk_per_trade':.005,'group_corr':.70,'group_cap':.20,'max_new_per_day':3}
    if set(settings)-set(defaults):raise ValueError('未知实验参数')
    out={**defaults,**settings}
    for k,v in out.items():
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):raise ValueError('参数必须是有限数值')
    if not 10000<=out['capital']<=1e9 or not 0<out['position_fraction']<=.6 or not 0<out['first_fraction']<=1/3 or not 0<out['max_exposure']<=.6:raise ValueError('模型资金/仓位违反20%现金、隔夜60%或首次1/3约束')
    if not 0<out['stop_pct']<=.5 or not 0<out['target_pct']<=2 or not 1<=out['holding_days']<=60 or int(out['holding_days'])!=out['holding_days']:raise ValueError('止损止盈或持有期无效')
    if any(not 0<=out[k]<=100 for k in ('commission_bps','sell_tax_bps','slippage_bps')) or not 0<=out['commission_min']<=1000 or not 0<=out['dividend_tax']<=1:raise ValueError('费用假设无效')
    if not 0<out['max_participation']<=.05:raise ValueError('模型容量上限须为上一已完成交易日成交量的0—5%')
    if not 0<out['risk_per_trade']<=.02:raise ValueError('单笔风险预算须为0—2% NAV')
    if not 0<=out['group_corr']<1:raise ValueError('相关性合并阈值须在[0,1)')
    if not 0<out['group_cap']<=.6:raise ValueError('相关性组暴露上限须为0—60%')
    if not 1<=out['max_new_per_day']<=10 or int(out['max_new_per_day'])!=out['max_new_per_day']:raise ValueError('单日新开仓数须为1—10整数')
    return out


def _returns(bars, code, dates, end, lookback=60):
    out=[]
    for i in range(max(1,end-lookback),end):
        a=bars.get((dates[i-1],code));b=bars.get((dates[i],code))
        out.append(None if not a or not b else (b['close']*b['adj'])/(a['close']*a['adj'])-1)
    return out


def _corr(x,y):
    pairs=[(a,b) for a,b in zip(x,y) if a is not None and b is not None]
    if len(pairs)<30:return None
    xs=[p[0] for p in pairs];ys=[p[1] for p in pairs]
    mx=sum(xs)/len(xs);my=sum(ys)/len(ys)
    vx=sum((x-mx)**2 for x in xs);vy=sum((y-my)**2 for y in ys)
    if vx<=0 or vy<=0:return None
    return sum((x-mx)*(y-my) for x,y in pairs)/math.sqrt(vx*vy)


def simulate(codes,dates,bars,limits,dividends,signals,settings):
    """Next-session open; T+1; limit locks; stop-first OHLC ambiguity; no leverage."""
    cfg=validate_settings(settings);capital=cash=cfg['capital'];positions={};pending=set();trades=[];skips=[];equity=[];issues=[];receivables=[]
    slip=cfg['slippage_bps']/10000
    def fee(v,sell=False):return max(cfg['commission_min'],v*cfg['commission_bps']/10000)+(v*cfg['sell_tax_bps']/10000 if sell else 0)
    def rounded(v,buy):return (math.ceil(v*100-1e-8) if buy else math.floor(v*100+1e-8))/100
    last_marks={};start_marks={};last_adj={}
    for index,day in enumerate(dates):
        buys_today=0
        for due in list(receivables):
            if due['pay_date'] and day>=due['pay_date']:cash+=due['amount'];receivables.remove(due)
        # Corporate actions are reconciled before opens, never using future dividends to choose trades.
        for code,p in list(positions.items()):
            b=bars.get((day,code))
            if not b:continue
            previous=last_adj.get(code,b['adj'])
            if abs(b['adj']/previous-1)>1e-6:
                event=dividends.get((day,code))
                if not event:
                    issues.append({'code':code,'date':day,'reason':'持仓复权因子改变但无可核验公司行为，整体绩效不发布'})
                else:
                    cash_per=number(event.get('cash_div_tax')) or 0;stock=number(event.get('stk_div')) or 0
                    prev=last_marks.get(code)
                    theoretical=(prev-cash_per)/(1+stock) if prev else None
                    expected_ratio=prev/theoretical if theoretical and theoretical>0 else None
                    if expected_ratio is None or abs(b['adj']/previous/expected_ratio-1)>.005:
                        issues.append({'code':code,'date':day,'reason':'公司行为与因子变动未对账，整体绩效不发布'})
                    else:
                        amount=p['qty']*cash_per*(1-cfg['dividend_tax'])
                        pay=event.get('pay_date');pay=iso_date(pay) if pay and len(str(pay))==8 else None
                        receivables.append({'code':code,'pay_date':pay,'amount':amount})
                        if not pay:issues.append({'code':code,'date':day,'reason':'现金分红到账日缺失，不能假定即时可用'})
                        shares=p['qty']*(1+stock)
                        if abs(shares-round(shares))>1e-6:issues.append({'code':code,'date':day,'reason':'送转零碎股结算未知'})
                        p['qty']=shares;p['dividend_income']=p.get('dividend_income',0)+amount
                        if stock:
                            unlock=event.get('div_listdate')
                            if not unlock or len(str(unlock))!=8:issues.append({'code':code,'date':day,'reason':'送转股份上市日期缺失，整体绩效不发布'})
                            else:p['unlock_date']=iso_date(unlock)
                        p['entry']/=expected_ratio;p['stop']/=expected_ratio;p['target']/=expected_ratio
        # Signals from the previous close only. A signal is not an executable fill.
        for code in sorted(pending):
            if code in positions:continue
            if buys_today>=int(cfg['max_new_per_day']):
                skips.append({'code':code,'date':day,'reason':'单日新开仓数已达上限'});continue
            b=bars.get((day,code));limit=limits.get((day,code))
            if not b or not limit:
                skips.append({'code':code,'date':day,'reason':'次日开盘行情或真实涨跌停价缺失，不成交'});continue
            if not b['volume'] or b['open']>=limit['up']-.00001:
                skips.append({'code':code,'date':day,'reason':'开盘涨停/无量，不假定排队买入成功'});continue
            fill=min(b['high'],limit['up'],rounded(b['open']*(1+slip),True))
            nav=cash+sum(p['qty']*(bars.get((day,c)) or {}).get('open',last_marks.get(c,p['entry'])) for c,p in positions.items())+sum(r['amount'] for r in receivables)
            exposure=sum(p['qty']*(bars.get((day,c)) or {}).get('open',last_marks.get(c,p['entry'])) for c,p in positions.items())
            budget=min(nav*cfg['position_fraction']*cfg['first_fraction'],nav*cfg['max_exposure']-exposure,cash-nav*.2)
            lot=200 if code.startswith('688') else 100
            prior_bar=bars.get((dates[index-1],code)) if index else None
            capacity=(prior_bar['volume']*cfg['max_participation']) if prior_bar else 0
            qty=max(0,int(min(budget/fill,capacity)/lot)*lot)
            while qty and qty*fill+fee(qty*fill)>min(budget,cash-nav*.2):qty-=lot
            blocked=None
            stop_dist=fill*cfg['stop_pct']
            risk_qty=int((nav*cfg['risk_per_trade'])/stop_dist/lot)*lot if stop_dist>0 else 0
            if risk_qty<qty:qty=risk_qty;blocked='单笔风险预算（失效距离）'
            group_value=0.0
            for c,p in positions.items():
                corr=_corr(_returns(bars,code,dates,index),_returns(bars,c,dates,index))
                if corr is not None and corr>=cfg['group_corr']:
                    group_value+=p['qty']*(bars.get((day,c)) or {}).get('open',last_marks.get(c,p['entry']))
            if group_value>0:
                room=max(0.0,nav*cfg['group_cap']-group_value)
                group_qty=int(room/fill/lot)*lot
                if group_qty<qty:qty=group_qty;blocked='相关性合并后组暴露上限'
            if not qty:
                skips.append({'code':code,'date':day,'reason':blocked or ('不足最小交易单位或风险预算' if budget/fill<lot else '上一交易日成交量容量约束，不假定可无限成交')});continue
            debit=qty*fill+fee(qty*fill);cash-=debit
            positions[code]={'qty':qty,'entry':fill,'entry_raw':fill,'debit':debit,'fees':fee(qty*fill),'day':day,'bought_index':index,'stop':fill*(1-cfg['stop_pct']),'target':fill*(1+cfg['target_pct'])}
            last_marks[code]=b['open'];last_adj[code]=b['adj'];buys_today+=1
        # Sell only after at least one exchange session; missing bars suspend exits, not delete samples.
        for code,p in list(positions.items()):
            b=bars.get((day,code));limit=limits.get((day,code))
            if not b:
                skips.append({'code':code,'date':day,'reason':'持仓无行情/停牌，不能假设成交'});continue
            last_marks[code]=b['close'];last_adj[code]=b['adj']
            if index<=p['bought_index'] or day<p.get('unlock_date',day):continue
            if not limit:issues.append({'code':code,'date':day,'reason':'持仓退出缺真实涨跌停价'});continue
            if b['high']<=limit['down']+.00001:
                skips.append({'code':code,'date':day,'reason':'封死跌停，卖单延后'});continue
            reason=None;price=None
            if b['low']<=p['stop']:reason='stop';price=min(b['open'],p['stop'])
            elif b['high']>=p['target']:reason='target';price=max(b['open'],p['target'])
            elif index-p['bought_index']>=cfg['holding_days']:reason='time';price=b['close']
            if reason:
                fill=max(b['low'],limit['down'],rounded(price*(1-slip),False));proceeds=p['qty']*fill;cost=fee(proceeds,True);cash+=proceeds-cost
                trades.append({'code':code,'entry_date':p['day'],'exit_date':day,'entry_price':p['entry_raw'],'exit_price':fill,'qty':p['qty'],'reason':reason,'cost_cny':p['fees']+cost,'pnl_cny':proceeds-cost-p['debit']+p.get('dividend_income',0),'bars_held':index-p['bought_index']})
                del positions[code]
        pending=set(signals.get(day,[]))
        for code in codes:
            b=bars.get((day,code))
            if b:
                last_marks[code]=b['close'];last_adj[code]=b['adj'];start_marks.setdefault(code,b['close']*b['adj'])
        nav=cash+sum(p['qty']*last_marks.get(c,p['entry']) for c,p in positions.items())+sum(r['amount'] for r in receivables)
        equity.append({'date':day,'nav':nav,'cash':cash,'positions_n':len(positions),'exposure_pct':sum(p['qty']*last_marks.get(c,p['entry']) for c,p in positions.items())/nav*100})
    peak=capital;dd=0
    for row in equity:peak=max(peak,row['nav']);dd=max(dd,1-row['nav']/peak)
    benchmark=[]
    for day in dates:
        returns=[bars[(day,code)]['close']*bars[(day,code)]['adj']/start_marks[code]-1 for code in codes if (day,code) in bars and code in start_marks]
        benchmark.append({'date':day,'nav':capital*(1+statistics.mean(returns)) if len(returns)==len(codes) else None,'valid_n':len(returns)})
    complete=not issues
    return {'state':'complete' if complete else 'incomplete','benchmark':benchmark,'benchmark_notice':'固定名单等权总回报价差参考；不含交易成本/仓位约束，不代表可得收益','equity':equity,'trades':trades,'skips':skips,'issues':issues,'open_positions':positions,'receivables':receivables,
        'metrics':{'total_return_pct':(equity[-1]['nav']/capital-1)*100 if complete and equity else None,'max_drawdown_pct':dd*100 if complete else None,'closed_trades':len(trades),'win_rate_pct':sum(t['pnl_cny']>0 for t in trades)/len(trades)*100 if complete and trades else None,'costs_cny':sum(t['cost_cny'] for t in trades)},
        'settings':cfg,'capacity_notice':'容量仅按上日成交量比例限额；未模拟开盘深度、排队与市场冲击，不等同真实可成交性','disclaimer':'固定名单的技术假设实验；费用为明确模型假设；非完整熔炼执行路径、非真实账户收益'}


def run_technical(store,body,db=H.HISTORY_DB,fetch=True):
    strategy=body.get('strategy_id');codes=sorted(set(body.get('codes',[])))
    from strategy_engine import load_catalog,compare
    from strategy_registry import validate_spec
    catalog=load_catalog();profile=next((p for p in catalog['profiles'] if p['id']==strategy),None)
    if not profile or not profile.get('backtest_spec'):raise ValueError('此路径缺少可因果计算的日线假设，拒绝伪造完整回测')
    spec=validate_spec(profile['backtest_spec'])
    if not codes or len(codes)>20 or any(not isinstance(c,str) or len(c)!=6 or not c.isdigit() or c.startswith(('5','15','16','18')) for c in codes):raise ValueError('本期技术成交实验支持1—20只显式股票代码；ETF需另行核验交易规则')
    if body.get('scope')!='fixed_exploratory' or body.get('accept_assumptions') is not True:raise ValueError('请明确接受固定名单选择偏差与费用模型假设')
    cfg=validate_settings(body.get('settings',{}));start=iso_date(body['start']);end=iso_date(body['end'])
    c=H.connect(db)
    try:
        dates=[r[0] for r in c.execute('SELECT date FROM calendar WHERE is_open=1 AND date>=? AND date<=? ORDER BY date',(start,end))]
        if not dates or len(dates)<5 or len(dates)>120:raise ValueError('实验窗口须为5—120个已完成交易日')
        last=c.execute("SELECT MAX(date) FROM partition WHERE status='ok'").fetchone()[0]
        if not last or dates[-1]>last:raise ValueError('窗口超出已验收日线，不用未来数据补齐')
    finally:c.close()
    ident=uuid.uuid4().hex
    request={'type':'technical_backtest','request':body,'created_at':stamp(),'validated':False,'state':'running','frozen_profile':profile,'catalog_hash':catalog['hash']}
    with store.connect() as c:c.execute('INSERT INTO experiment VALUES(?,?,?)',(ident,stamp(),encode(request)))
    try:
        failures=execution_inputs(codes,dates,db=db) if fetch else []
        c=H.connect(db)
        try:
            rows=[dict(r) for code in codes for r in c.execute('SELECT * FROM bars WHERE code=? AND date>=? AND date<=?',(code,start,end))]
            bars={(r['date'],r['code']):r for r in rows};limits={(r['date'],r['code']):dict(r) for r in c.execute('SELECT date,code,up,down FROM execution_limit WHERE date>=? AND date<=?',(start,end))}
            div={(r['ex_date'],r['code']):json.loads(r['body']) for r in c.execute('SELECT * FROM dividend_event WHERE ex_date>=? AND ex_date<=?',(start,end))}
            frozen_bars={code:[dict(r) for r in c.execute('SELECT * FROM bars WHERE code=? AND date<=? ORDER BY date',(code,end))] for code in codes}
            request['input_archive']=store.archive_source({'bars':encode(frozen_bars),'limits':encode(list(limits.values())),'dividends':encode([{'date':d,'code':code,'body':v} for (d,code),v in div.items()]),'profile':encode(profile),'settings':encode(cfg),'research_backtest.py':__import__('pathlib').Path(__file__).read_text()})
            missing=[{'code':code,'date':d} for code in codes for d in dates if (d,code) in bars and (d,code) not in limits]
            if missing:result={'state':'blocked','reason':'真实涨跌停价尚未完整取得，不发布成交绩效','missing_limits':missing[:30],'provider_failures':failures}
            else:
                sessions=[r[0] for r in c.execute('SELECT date FROM calendar WHERE is_open=1 ORDER BY date')];signals={};warmup_missing=0
                for day in dates:
                    prior=[d for d in sessions if d<=day]
                    for code in codes:
                        f=enrich_daily_signals(H.features(c,code,day,prior))
                        verdicts=[compare(f.get(r['field']),r['op'],r['value']) for r in spec]
                        if any(v is None for v in verdicts):warmup_missing+=1;continue
                        if all(verdicts):signals.setdefault(day,[]).append(code)
                result=simulate(codes,dates,bars,limits,div,signals,cfg)
                if warmup_missing:
                    result['state']='incomplete';result['issues'].append({'reason':'部分代码/信号日缺少技术输入，不把不可判断记作无信号或零收益','n':warmup_missing})
                    for k in ('total_return_pct','max_drawdown_pct','win_rate_pct'):result['metrics'][k]=None
                result.update({'warmup_missing_n':warmup_missing,'signal_count':sum(map(len,signals.values())),'sample_sessions':len(dates),'selection_bias':'固定显式名单，无历史选股有效性结论','validation_status':'unvalidated','signals':signals})
                # Chronological holdout is reported separately; no parameter selection on holdout.
                split=max(1,int(len(dates)*.7));validation_dates=dates[split:]
                holdout=simulate(codes,validation_dates,bars,limits,div,{d:signals.get(d,[]) for d in validation_dates},cfg)
                if warmup_missing:
                    holdout['state']='incomplete'
                    for k in ('total_return_pct','max_drawdown_pct','win_rate_pct'):holdout['metrics'][k]=None
                result['holdout']={'dates':validation_dates,'metrics':holdout['metrics'],'state':holdout['state'],'notice':'参数预先固定，后30%为时间留出；单次留出不足以证明策略有效'}
                result['walk_forward']=[]
                if len(dates)>=15:
                    for fold in range(3):
                        window=dates[fold*len(dates)//3:(fold+1)*len(dates)//3]
                        probe=simulate(codes,window,bars,limits,div,{d:signals.get(d,[]) for d in window},cfg)
                        if warmup_missing:
                            probe['state']='incomplete'
                            for k in ('total_return_pct','max_drawdown_pct','win_rate_pct'):probe['metrics'][k]=None
                        result['walk_forward'].append({'fold':fold+1,'start':window[0],'end':window[-1],'state':probe['state'],'metrics':probe['metrics'],'notice':'固定参数分段检验；每段独立起始，不继承跨段持仓，不冒称已调参验证'})
                result['sensitivity']=[] if warmup_missing else [{'slippage_bps':bps,'metrics':simulate(codes,dates,bars,limits,div,signals,{**cfg,'slippage_bps':bps})['metrics']} for bps in sorted(set([cfg['slippage_bps'],cfg['slippage_bps']*2,10.]))]
        finally:c.close()
        request.update({'state':result['state'],'result':result,'completed_at':stamp(),'data_dates':{'start':dates[0],'end':dates[-1]},'code_hash':hashlib.sha256(__import__('pathlib').Path(__file__).read_bytes()).hexdigest()})
    except Exception as e:
        request.update({'state':'failed','error':type(e).__name__,'completed_at':stamp()})
    with store.connect() as c:c.execute('UPDATE experiment SET body=? WHERE id=?',(encode(request),ident))
    return {'id':ident,**request}
