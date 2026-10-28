"""Fault-driven contracts for clocks, coverage, isolation, states and simulated fills."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from data_runtime import TZ
from product_clock import clock_context,quote_contract,market_coverage
from product_store import ProductStore
from strategy_engine import evaluate_dimensions,build_features,apply_reviews
from research_evidence import theme_observations
from research_backtest import simulate,validate_settings
from strategy_history import synchronize,connect

NOW=dt.datetime(2026,10,28,10,0,tzinfo=TZ)
CAL={'2026-10-26':True,'2026-10-27':True,'2026-10-28':True,'2026-10-29':False}

def quote(code='000001',price=10,stamp='20261028095950'):
 return {'code':code,'name':'fixture','price':price,'pct':1.,'quote_time':stamp,'amount':10000,'volume':10000,'up_limit':11,'prev_close':9.9,'src':'fixture'}

def rule(field,kind='condition'):
 return {'id':field,'field':field,'label':field,'op':'eq','value':True,'kind':kind,'source':'fixture','evidence_grade':'test','reason':'contract'}

def profile():return {'id':'fixture','name':'fixture','version':'2','style':'短线','lifecycle':'experimental','rules':[rule('quote_valid','data'),rule('shape'),rule('risk_review','manual'),rule('instrument_allowed','policy'),rule('source_version_match','data')],'invalidations':[]}

class ClockCoverage(unittest.TestCase):
 def test_closed_holiday_uses_calendar(self):
  c=clock_context(dt.datetime(2026,10,29,10,tzinfo=TZ),CAL)
  self.assertEqual(c['phase'],'closed');self.assertEqual(c['reference_day'],'2026-10-28');self.assertFalse(c['execution_available'])
 def test_preopen_reference_and_actual_history(self):
  c=clock_context(NOW.replace(hour=0),CAL)
  self.assertEqual(c['phase'],'prepare');self.assertEqual(c['reference_day'],'2026-10-27');self.assertEqual(c['history_target'],'2026-10-27')
  self.assertEqual(quote_contract(quote(stamp='20261027150000'),c,NOW.replace(hour=0))['state'],'reference')
 def test_lunch_and_afterclose(self):
  self.assertEqual(clock_context(NOW.replace(hour=12),CAL)['phase'],'break')
  self.assertEqual(clock_context(NOW.replace(hour=16,minute=29),CAL)['history_target'],'2026-10-27')
  self.assertEqual(clock_context(NOW.replace(hour=16,minute=30),CAL)['history_target'],'2026-10-28')
 def test_live_rejects_stale_future_and_prior_day(self):
  context=clock_context(NOW,CAL)
  for stamp in ['20261028095000','20261028110000','20261027150000']:
   self.assertFalse(quote_contract(quote(stamp=stamp),context,NOW)['valid'])
  self.assertTrue(quote_contract(quote(),context,NOW)['valid'])
 def test_missing_calendar_not_weekday_guess(self):
  c=clock_context(NOW,{})
  self.assertEqual(c['phase'],'unknown');self.assertFalse(quote_contract(quote(),c,NOW)['valid'])
 def test_partial_market_and_duplicate_fail_closed(self):
  ids=['600001','600002','000001','000002','920001','920002'];rows=[quote(c) for c in ids]
  self.assertTrue(market_coverage(rows,ids)['complete'])
  self.assertFalse(market_coverage(rows[:4],ids)['complete'])
  self.assertFalse(market_coverage(rows+[rows[0]],ids)['complete'])
 def test_partial_coverage_suspends_global_conclusions(self):
  from product_clock import suspend_market_conclusions
  state={'sentiment':{'涨停':50},'dual':{'A':{'分':5}},'focus':[{'rank':1}]}
  suspend_market_conclusions(state,{'complete':False})
  self.assertFalse(state['sentiment']);self.assertNotIn('A',state['dual']);self.assertFalse(state['focus'])
 def test_price_without_source_time_not_coverage(self):
  q=quote();q.pop('quote_time');self.assertFalse(market_coverage([q],['000001'])['complete'])

class DimensionContracts(unittest.TestCase):
 def test_unavailable_data_is_not_failed_shape(self):
  r=evaluate_dimensions(profile(),{'quote_valid':False,'shape':True,'instrument_allowed':True,'source_version_match':True})
  self.assertEqual(r['status'],'unavailable');self.assertEqual(r['setup_state'],'confirmed')
 def test_tool_restriction_not_company_risk(self):
  f=build_features({'code':'688001','name':'fixture','biz_chain':'半导体链'},quote('688001'),{}, {'history_asof':'2026-10-27','last_close':9.9},[],NOW,True)
  self.assertFalse(f['instrument_allowed']);self.assertNotIn('risk_review',f)
 def test_version_drift_prevents_complete(self):
  r=evaluate_dimensions(profile(),{'quote_valid':True,'shape':True,'risk_review':True,'instrument_allowed':True,'source_version_match':False})
  self.assertEqual(r['status'],'pending');self.assertEqual(r['next_confirmation'],'source_version_match');self.assertEqual(r['evidence_state'],'expired')
 def test_context_closed_is_reference_not_ready(self):
  r=evaluate_dimensions(profile(),{'quote_valid':True,'shape':True,'risk_review':True,'instrument_allowed':True,'source_version_match':True,'live_execution_window':False})
  self.assertEqual(r['status'],'complete');self.assertEqual(r['execution_state'],'reference_only')
 def test_global_denial_cannot_be_overridden_by_stock(self):
  def r(code,value):return {'code':code,'field':'macro_approved','value':json.dumps(value),'reviewed_at':'2026-10-27T10:00:00+08:00','expires_at':'2099-01-01T00:00:00+08:00','evidence':'fixture evidence'}
  for reviews in [[r('*@x',False),r('000001@x',True)],[r('000001@x',True),r('*@x',False)]]:
   f={'_provenance':{}};apply_reviews(f,reviews,'000001@x',NOW);self.assertFalse(f['macro_approved'])
 def test_all_themes_independent_of_focus_list(self):
  qs={c:quote(c) for c in ['000001','000002','000003']}
  m,_,_=theme_observations({'not-in-focus':set(qs)},qs,{}, {},{c:{'valid':True} for c in qs})
  self.assertEqual(m['not-in-focus']['mean'],1);self.assertEqual(m['not-in-focus']['breadth'],1);self.assertIsNone(m['not-in-focus']['leader_positive'])
 def test_same_day_history_breakout_uses_previous_bar(self):
  q=quote(price=10.5);q['quote_time']='20261027150000';q['prev_close']=9.9
  f=build_features({'code':'000001','name':'fixture'},q,{}, {'history_asof':'2026-10-27','last_close':10.5,'previous_close':9.9,'prior_high':10.6,'previous_high':10},[],NOW,True)
  self.assertTrue(f['above_previous_high']);self.assertTrue(f['price_basis_verified'])

class StoreVersions(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=ProductStore(Path(self.tmp.name)/'r.sqlite')
 def tearDown(self):self.tmp.cleanup()
 def body(self,style='波段',sid='wind_a'):
  return {'style':style,'strategy_id':sid,'trigger_price':10,'invalid_price':9,'deadline':'2099-01-01T00:00:00+08:00','thesis':'fixture thesis','invalidation':'fixture invalidation','max_model_position':.1}
 def test_independent_styles_and_immutable_revisions(self):
  a=self.store.save_instance('000001',self.body());b=self.store.save_instance('000001',self.body('核心长线','fundamental_core'))
  self.assertNotEqual(a['id'],b['id']);self.assertEqual(len(self.store.instances('000001')),2)
  self.store.save_instance('000001',{**a,'trigger_price':10.1,'revision_reason':'new dated evidence'})
  with self.store.connect() as c:self.assertEqual(c.execute('select count(*) from plan_revision where instance_id=?',(a['id'],)).fetchone()[0],2)
  other=next(p for p in self.store.instances() if p['id']==b['id']);self.assertEqual(other['version'],1)
 def test_edit_cannot_change_style_or_erase_reason(self):
  a=self.store.save_instance('000001',self.body())
  for patch in [{'style':'短线','revision_reason':'change'},{'trigger_price':10.2}]:
   with self.assertRaises(ValueError):self.store.save_instance('000001',{**a,**patch})
 def test_hard_budgets_and_tick_units(self):
  for patch in [{'max_model_position':.9},{'first_fraction':.5},{'trigger_price':10.001},{'trigger_price':float('inf')}]:
   with self.assertRaises(ValueError):self.store.save_instance('000001',{**self.body(),**patch})
 def test_source_archive_and_ack(self):
  first=self.store.archive_source({'a.py':'x=1'});self.assertEqual(first,self.store.archive_source({'a.py':'x=1'}))
  self.store.event('fixture','000001',{'stage':'formed'});e=self.store.activity()[0];self.store.event_action(e['id'],'ack');self.assertTrue(self.store.activity()[0]['acknowledged'])
 def test_terminal_plan_not_revived_by_rebound(self):
  from product_service import ProductService
  p=self.store.save_instance('000001',self.body());service=ProductService.__new__(ProductService);service.store=self.store;service.snapshot_id='fixture'
  service._instance_events({}, {'000001':quote(price=8)}, {'live':True}, {'000001':{'valid':True}}, {'000001':{'price_basis_verified':True}})
  service._instance_events({}, {'000001':quote(price=12)}, {'live':True}, {'000001':{'valid':True}}, {'000001':{'price_basis_verified':True}})
  self.assertEqual(self.store.instances()[0]['stage'],'失效价触及')
 def test_reference_and_uncertain_basis_do_not_fire_stop(self):
  from product_service import ProductService
  p=self.store.save_instance('000001',self.body());service=ProductService.__new__(ProductService);service.store=self.store;service.snapshot_id='fixture'
  service._instance_events({}, {'000001':quote(price=8)}, {'live':False}, {'000001':{'valid':True}}, {'000001':{'price_basis_verified':True}})
  self.assertEqual(self.store.instances()[0]['stage'],'非交易时段·参考')
  service._instance_events({}, {'000001':quote(price=8)}, {'live':True}, {'000001':{'valid':True}}, {'000001':{'price_basis_verified':False}})
  self.assertEqual(self.store.instances()[0]['stage'],'价格基准待核·暂停')

class Synchronization(unittest.TestCase):
 def test_reuses_verified_primary_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as tmp:
   main=Path(tmp)/'m.sqlite';hist=Path(tmp)/'h.sqlite';c=sqlite3.connect(main)
   c.executescript('CREATE TABLE trade_calendar(exchange,cal_date,is_open);CREATE TABLE ingest_partition(source,date,status);CREATE TABLE dim_stock(code);CREATE TABLE fact_equity_daily(date,ts_code,open,high,low,close,volume_shares,amount_cny,adj_factor,source);')
   c.execute("INSERT INTO trade_calendar VALUES('SSE','2026-10-27',1)");c.execute("INSERT INTO ingest_partition VALUES('daily','2026-10-27','ok')");c.execute("INSERT INTO dim_stock VALUES('000001')");c.execute("INSERT INTO fact_equity_daily VALUES('2026-10-27','000001.SZ',10,11,9,10.5,10000,105000,1,'tushare')");c.commit();c.close()
   r=synchronize('2026-10-27',hist,main,days=1);self.assertEqual(r['state'],'ok');self.assertEqual(r['actual'],'2026-10-27')
   r=synchronize('2026-10-27',hist,main,days=1);self.assertEqual(r['repaired'],[])

class ExecutionContracts(unittest.TestCase):
 def fixtures(self):
  dates=['2026-10-'+str(i) for i in range(20,27)]
  bars={(d,'000001'):{'open':10,'high':11,'low':9,'close':10,'volume':10000,'adj':1} for d in dates}
  limits={(d,'000001'):{'up':11,'down':9} for d in dates}
  return dates,bars,limits
 def test_tplus1_stop_and_costs(self):
  dates,bars,limits=self.fixtures();r=simulate(['000001'],dates,bars,limits,{}, {dates[0]:['000001']},{'slippage_bps':0})
  self.assertEqual(r['trades'][0]['entry_date'],dates[1]);self.assertEqual(r['trades'][0]['exit_date'],dates[2]);self.assertEqual(r['trades'][0]['reason'],'stop');self.assertGreater(r['metrics']['costs_cny'],0)
  self.assertTrue(all(e['cash']>=e['nav']*.2-1e-6 for e in r['equity']))
 def test_limit_up_blocks_entry(self):
  dates,bars,limits=self.fixtures();bars[(dates[1],'000001')].update(open=11,high=11,low=11,close=11)
  r=simulate(['000001'],dates,bars,limits,{}, {dates[0]:['000001']},{})
  self.assertEqual(r['trades'],[]);self.assertTrue(any('开盘涨停' in x['reason'] for x in r['skips']))
 def test_limit_down_delays_exit(self):
  dates,bars,limits=self.fixtures();bars[(dates[2],'000001')].update(open=9,high=9,low=9,close=9)
  r=simulate(['000001'],dates,bars,limits,{}, {dates[0]:['000001']},{'slippage_bps':0})
  self.assertGreater(r['trades'][0]['exit_date'],dates[2]);self.assertTrue(any('封死跌停' in x['reason'] for x in r['skips']))
 def test_unreconciled_action_withholds_performance(self):
  dates,bars,limits=self.fixtures();bars[(dates[2],'000001')]['adj']=2
  r=simulate(['000001'],dates,bars,limits,{}, {dates[0]:['000001']},{})
  self.assertEqual(r['state'],'incomplete');self.assertIsNone(r['metrics']['total_return_pct'])
 def test_no_trades_has_no_winrate(self):
  dates,bars,limits=self.fixtures();r=simulate(['000001'],dates,bars,limits,{}, {},{})
  self.assertIsNone(r['metrics']['win_rate_pct']);self.assertEqual(r['metrics']['closed_trades'],0)
 def test_capacity_uses_prior_completed_volume(self):
  dates,bars,limits=self.fixtures();bars[(dates[0],'000001')]['volume']=100
  r=simulate(['000001'],dates,bars,limits,{}, {dates[0]:['000001']},{})
  self.assertEqual(r['trades'],[]);self.assertTrue(any('成交量容量约束' in x['reason'] for x in r['skips']))
 def test_no_invalid_parameters(self):
  for cfg in [{'max_exposure':.9},{'first_fraction':.5},{'commission_bps':-1},{'holding_days':1.5},{'capital':float('nan')}]:
   with self.assertRaises(ValueError):validate_settings(cfg)

class HypothesisContracts(unittest.TestCase):
 def test_reference_conflict_is_not_a_price_signal(self):
  from product_clock import crosscheck_reference
  q=quote(stamp='20261027150000');qs={'000001':q};hs={'000001':{'state':'reference','valid':True}}
  result=crosscheck_reference(qs,hs,{'000001':8},'2026-10-27')
  self.assertEqual(result['conflict_n'],1);self.assertFalse(hs['000001']['valid'])
 def test_reference_match_and_live_not_compared_to_old_close(self):
  from product_clock import crosscheck_reference
  qs={'000001':quote()};hs={'000001':{'state':'live','valid':True}}
  self.assertEqual(crosscheck_reference(qs,hs,{'000001':8},'2026-10-27')['checked_n'],0)
  qs['000001']['quote_time']='20261027150000';hs['000001']['state']='reference'
  self.assertEqual(crosscheck_reference(qs,hs,{'000001':10},'2026-10-27')['conflict_n'],0)
 def test_registry_keeps_risk_and_rejects_executable_spec(self):
  from strategy_registry import register_hypothesis,validate_spec
  from strategy_engine import CATALOG,load_catalog
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'catalog.json';path.write_bytes(CATALOG.read_bytes())
   body={'name':'验收假设','hypothesis':'可证伪的日线条件仅供实验验证','invalidation':'环境转弱立即失效','conditions':[{'field':'ret20','op':'gte','value':5}]}
   item=register_hypothesis(body,path)
   self.assertFalse(item['validated']);self.assertEqual(item['lifecycle'],'incubation')
   self.assertTrue(any(r['field']=='risk_review' for r in item['rules']))
   self.assertEqual(len(load_catalog(path)['profiles']),9)
   with self.assertRaises(ValueError):register_hypothesis(body,path)
   with self.assertRaises(ValueError):validate_spec([{'field':'__import__(os)','op':'eq','value':True}])
 def test_opening_buy_does_not_spend_later_intraday_exit(self):
  # Five positions use all exposure capacity. A later intraday exit must not fund
  # the sixth security at that same day's earlier opening auction.
  dates=['2026-10-20','2026-10-21','2026-10-22']
  codes=['00000'+str(i) for i in range(1,7)]
  bars={(d,c):{'open':10,'high':10.1,'low':9.9,'close':10,'volume':10000,'adj':1} for d in dates for c in codes}
  limits={(d,c):{'up':11,'down':9} for d in dates for c in codes}
  bars[(dates[2],codes[0])]['low']=9
  for b in bars.values():b['volume']=1e9
  cfg={'position_fraction':.6,'max_exposure':.6,'commission_bps':0,'commission_min':0,'slippage_bps':0,'sell_tax_bps':0,'risk_per_trade':.02}
  result=simulate(codes,dates,bars,limits,{}, {dates[0]:codes[:3],dates[1]:codes[3:]},cfg)
  self.assertTrue(any(x['reason']=='不足最小交易单位或风险预算' for x in result['skips']))
  self.assertFalse(any(c in result['open_positions'] for c in codes[3:]))

if __name__=='__main__':unittest.main()
