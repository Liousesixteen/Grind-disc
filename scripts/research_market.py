"""Verified previous-session limit cohorts; never trust a directory date as source time."""
import statistics
import strategy_history as H
from data_runtime import ProClient,number,iso_date


def sync_market_limits(target,expected,provider=None,db=H.HISTORY_DB):
    provider=provider or ProClient();c=H.connect(db);report=[]
    c.executescript('''CREATE TABLE IF NOT EXISTS execution_limit(date TEXT,code TEXT,up REAL,down REAL,PRIMARY KEY(date,code));
CREATE TABLE IF NOT EXISTS market_limit_partition(date TEXT PRIMARY KEY,status TEXT,n INT,note TEXT);''')
    try:
        days=[r[0] for r in c.execute('SELECT date FROM calendar WHERE is_open=1 AND date<=? ORDER BY date DESC LIMIT 7',(target,))]
        for day in days:
            if c.execute("SELECT 1 FROM market_limit_partition WHERE date=? AND status='ok'",(day,)).fetchone():continue
            try:
                rows={}
                for page in range(4):
                    df=provider.stk_limit(trade_date=day.replace('-',''),limit=5800,offset=page*5800,fields='ts_code,trade_date,up_limit,down_limit')
                    batch=df.to_dict('records');added=0
                    for r in batch:
                        code=r['ts_code'].split('.')[0]
                        if iso_date(r['trade_date'])!=day:raise ValueError('涨跌停日期不符')
                        up,dn=number(r.get('up_limit')),number(r.get('down_limit'))
                        if code in expected and up and dn and up>dn:
                            if code not in rows:added+=1
                            rows[code]=(day,code,up,dn)
                    if len(rows)>=len(expected)*.99 or not batch or not added:break
                if len(rows)<len(expected)*.95:raise ValueError('实际股票涨跌停价覆盖不足95%，不发布历史晋级率')
                with c:
                    c.executemany('INSERT OR REPLACE INTO execution_limit VALUES(?,?,?,?)',rows.values())
                    c.execute('INSERT OR REPLACE INTO market_limit_partition VALUES(?,?,?,?)',(day,'ok',len(rows),'tushare stk_limit；按实际证券覆盖验收'))
                report.append({'date':day,'state':'ok','n':len(rows)})
            except Exception as e:
                report.append({'date':day,'state':'unavailable','reason':type(e).__name__})
                if sum(r['state']!='ok' for r in report)>=2:break
    finally:c.close()
    return report


def verified_cohorts(reference_day,quotes,db=H.HISTORY_DB):
    c=H.connect(db)
    try:
        days=[r[0] for r in c.execute('SELECT date FROM calendar WHERE is_open=1 AND date<? ORDER BY date DESC LIMIT 6',(reference_day,))]
        accepted={r[0] for r in c.execute("SELECT date FROM market_limit_partition WHERE status='ok'")}
        if not days or days[0] not in accepted:return {'ready':False,'note':'上一交易日真实涨跌停价格尚未验收，暂停历史晋级/溢价评分'}
        sets={}
        for day in days:
            if day not in accepted:break
            sets[day]={r[0] for r in c.execute('SELECT b.code FROM bars b JOIN execution_limit l ON b.date=l.date AND b.code=l.code WHERE b.date=? AND b.close>=l.up-0.00001',(day,))}
        prev=days[0];mapping={code:1 for code in sets[prev]}
        for code in mapping:
            for day in days[1:]:
                if code not in sets.get(day,set()):break
                mapping[code]+=1
        return {'ready':True,'previous_day':prev,'previous_map':mapping,'counts':{d.replace('-',''):len(sets[d]) for d in list(sets)[:5]},'note':'前日集合/连板由Tushare日线×同日真实涨跌停价重算；不读取按目录日期冒充已验收的旧快照'}
    except Exception as e:return {'ready':False,'note':'历史晋级样本待验收：'+type(e).__name__}
    finally:c.close()


def apply_verified_dual(state,quotes,health,reference_day,con):
    import dual_gate as DG
    cohort=verified_cohorts(reference_day,quotes)
    # Source pools must agree with individual valid quotes before contributing ladder evidence.
    known={code for code in state.get('zt_codes',[]) if health.get(code,{}).get('valid')}
    mismatched=[code for code in known if not number(quotes.get(code,{}).get('up_limit')) or quotes[code]['price']<quotes[code]['up_limit']-.011]
    if mismatched:
        cohort['pool_conflicts']=mismatched;cohort['note']+='；涨停池与报价冲突，池侧角色/梯队暂停'
        state['pankou']=[];state['ladder']={};state['zt_codes']=[];state['main_theme']={};state['catchup']=[]
        state['warn']=list(state.get('warn',[]))+[cohort['note']]
        state['sentiment']={};state['dual']={'A':{},'B':{},'灯':{'名':'数据待核','色':'#777','动作':'涨停池与有效报价不一致，暂停组合判断'},'note':cohort['note']}
    else:
        # Existing structural rows carry source-time validation above. Only prior cohorts are replaced.
        details={r['code']:r for r in state.get('pankou',[])}
        rows=[{'c':code,'n':quotes[code].get('name'),'lbc':cohort.get('previous_map',{}).get(code,0)+1,'fbt':int(str(details.get(code,{}).get('首封') or '0').replace(':','').replace('—','0')),'zbc':details.get(code,{}).get('炸板次数')} for code in known]
        state['dual']=DG.compute(state.get('sentiment'),state.get('ladder'),rows,list(quotes.values()),con,reference_day.replace('-',''),pk_rows=state.get('pankou'),verified_history=cohort)
    state['cohort_health']=cohort
    if not cohort['ready']:
        state['dual'].setdefault('灯',{}).update({'名':'历史待核','色':'#777','动作':'前日真实样本未齐，不输出历史晋级评分'})
    return cohort
