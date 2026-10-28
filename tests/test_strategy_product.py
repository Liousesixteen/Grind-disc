"""Risk gates, causal data, scoped evidence and immutable watch contracts."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from product_store import ProductStore
from strategy_engine import compare,evaluate,apply_reviews,load_catalog,build_features,TZ
from strategy_lab import replay,outcomes
import strategy_history as H

NOW=dt.datetime(2026,10,27,10,0,tzinfo=TZ)

def rule(field,value=True):return {'id':field,'field':field,'label':field,'op':'eq','value':value,'source':'test','reason':'gate'}
def profile():return {'id':'test','name':'test','version':'1','style':'短线','lifecycle':'experimental','invalidations':[],'rules':[rule('risk'),rule('momentum')]}

class StrategyContracts(unittest.TestCase):
 def test_failed_risk_cannot_be_offset(self):
  r=evaluate(profile(),{'risk':False,'momentum':True,'pct':20,'score':999})
  self.assertEqual(r['status'],'blocked');self.assertEqual(r['passed'],1);self.assertFalse(r['validated'])
 def test_unknown_is_not_false_or_pass(self):
  self.assertIsNone(compare(None,'eq',True));self.assertIsNone(compare(1,'eq',True))
  self.assertEqual(evaluate(profile(),{'momentum':True})['status'],'pending')
 def test_catalog_has_no_weighted_score_or_validated_claim(self):
  c=load_catalog();self.assertEqual(len(c['profiles']),8)
  for p in c['profiles']:
   self.assertFalse(p['validated']);self.assertNotIn('weights',p)
   self.assertTrue(all(r['source'] and r['reason'] for r in p['rules']))
 def test_scoped_manual_evidence_expires_and_cannot_override_quote(self):
  def rev(code,field,value=True,expiry='2026-10-28T00:00:00+08:00'):
   return {'code':code,'field':field,'value':json.dumps(value),'evidence':'dated evidence','reviewed_at':'2026-10-27T09:00:00+08:00','expires_at':expiry}
  f={'quote_valid':False,'_provenance':{}}
  apply_reviews(f,[rev('000001@wind_a','risk_review'),rev('*@wind_a','macro_approved'),rev('000001@wind_a','quote_valid'),rev('000001@wind_a','support_approved',expiry='2026-10-26T00:00:00+08:00')],'000001@wind_a',NOW)
  self.assertTrue(f['risk_review']);self.assertTrue(f['macro_approved']);self.assertFalse(f['quote_valid']);self.assertNotIn('support_approved',f)
  g={'_provenance':{}};apply_reviews(g,[rev('000001@wind_a','risk_review')],'000001@wind_b',NOW);self.assertNotIn('risk_review',g)
 def test_stale_pool_and_split_price_base_do_not_pass(self):
  f=build_features({'code':'000001','name':'test','池层':'核心','筛1_股性':True,'筛2_辨识度':True,'筛3_量价':True},{'price':11,'prev_close':5,'quote_time':'20261027095950'},{'data_date':'20261023','previous_session':'2026-10-26'},{'last_close':10,'prior_high':10.2,'ma20':9,'shape_a':True,'momentum_turn':True},[],NOW,True)
  self.assertFalse(f['prepool_eligible']);self.assertIsNone(f['above_previous_high']);self.assertIsNone(f['signal_a_classes'])
 def test_replay_uses_frozen_catalog_and_scoped_evidence(self):
  p=profile();payload={'evaluated_at':NOW.isoformat(),'catalog':{'profiles':[p]},'features':{'000001':{'risk':False,'momentum':True}},'reviews':[]}
  self.assertEqual(replay(payload)['test']['000001']['status'],'blocked')
  changed=load_catalog();changed['profiles']=[]
  self.assertEqual(replay(payload)['test']['000001']['status'],'blocked')

class WatchContracts(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=ProductStore(Path(self.tmp.name)/'r.sqlite')
 def tearDown(self):self.tmp.cleanup()
 def test_dedup_archive_restore_preserves_baseline(self):
  a={'code':'000001','name':'test'};q={'price':10,'quote_time':'20261027100000'}
  self.assertTrue(self.store.add_watch(a,q,tier='核心'));self.assertFalse(self.store.add_watch(a,{'price':99}))
  self.store.update_watch('000001','archive');self.assertEqual(self.store.list_watches(),[])
  self.store.update_watch('000001','restore')
  self.assertEqual(self.store.list_watches()[0]['last_observation']['entry_price'],10)
  self.assertEqual(len(self.store.observations('000001')),1)
 def test_snapshot_is_immutable_within_minute(self):
  a=self.store.snapshot('2026-10-27T10:00:01+08:00','hash',{'price':10})
  b=self.store.snapshot('2026-10-27T10:00:55+08:00','hash',{'price':99})
  self.assertEqual(a,b);self.assertEqual(self.store.snapshot_payload(a)['payload']['price'],10)
 def test_plan_rejects_invalid_boundaries_nan_and_past_deadline(self):
  base={'style':'短线','trigger_price':10,'invalid_price':9,'deadline':'2099-10-27T10:00:00+08:00'}
  self.store.save_plan('000001',base)
  for patch in ({'invalid_price':11},{'trigger_price':float('nan')},{'target_price':8},{'deadline':'2020-01-01T00:00:00+08:00'},{'trigger_price':True}):
   with self.assertRaises(ValueError):self.store.save_plan('000001',{**base,**patch})

class HistoryContracts(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.c=H.connect(Path(self.tmp.name)/'h.sqlite')
 def tearDown(self):self.c.close();self.tmp.cleanup()
 def seed(self):
  days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(65)]
  for i,d in enumerate(days):
   factor=1 if i<40 else 2;price=20/factor
   self.c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)',(d,'000001',price,price,price,price,100*factor,10000,factor))
   self.c.execute('INSERT INTO calendar VALUES(?,1)',(d,))
  self.c.commit();return days
 def test_causal_cutoff_and_adjustment(self):
  days=self.seed();f=H.features(self.c,'000001',days[59],days[:60]);self.assertEqual(f['contiguous_n'],60);self.assertEqual(f['ma60'],10);self.assertEqual(f['ret20'],0)
  self.c.execute('UPDATE bars SET close=999 WHERE date=?',(days[64],));self.assertEqual(H.features(self.c,'000001',days[59],days[:60])['ma60'],10)
 def test_missing_sessions_do_not_become_contiguous(self):
  days=self.seed();self.c.execute('DELETE FROM bars WHERE date=?',(days[-10],));f=H.features(self.c,'000001',days[-1],days)
  self.assertEqual(f['contiguous_n'],9);self.assertNotIn('ma20',f)
 def test_future_outcomes_remain_pending(self):
  p=profile();payload={'evaluated_at':NOW.isoformat(),'catalog':{'profiles':[p]},'features':{'000001':{'risk':True,'momentum':True}},'reviews':[]}
  r=outcomes(payload,'test',5,self.c);self.assertEqual(r['pending_n'],1);self.assertIsNone(r['equal_weight_mean_pct'])

if __name__=='__main__':unittest.main()

class OperationalContracts(unittest.TestCase):
 def test_failed_plan_stays_latched_and_deadline_works_without_quote(self):
  from product_service import ProductService
  with tempfile.TemporaryDirectory() as tmp:
   store=ProductStore(Path(tmp)/'r.sqlite');store.add_watch({'code':'000001','name':'test'})
   store.save_plan('000001',{'style':'短线','trigger_price':10,'invalid_price':9,'deadline':'2099-01-01T00:00:00+08:00'})
   svc=ProductService.__new__(ProductService);svc.store=store
   svc._plans({'000001':{'price':8,'amount':100,'quote_time':'20261027095959'}},NOW,True)
   svc._plans({'000001':{'price':12,'amount':100,'quote_time':'20261027095959'}},NOW,True)
   with store.connect() as c:self.assertEqual(c.execute('SELECT state FROM plan_state').fetchone()[0],'失效价触及')
   store.save_plan('000001',{'style':'波段','trigger_price':10,'invalid_price':9,'deadline':'2099-01-01T00:00:00+08:00'})
   svc._plans({},dt.datetime(2100,1,1,tzinfo=TZ),True)
   with store.connect() as c:self.assertEqual(c.execute('SELECT state FROM plan_state').fetchone()[0],'时限到期')
 def test_real_engine_replay_agrees_with_live_statuses(self):
  from strategy_engine import evaluate_universe
  c=load_catalog();rows=[{'code':'000001','name':'test','池层':'核心'}]
  rs,fs=evaluate_universe(c,rows,{'000001':{'price':10,'quote_time':'20261027095959'}},{'data_date':'20261026','previous_session':'2026-10-26'},{},[],NOW,True)
  frozen={'catalog':c,'features':fs,'reviews':[],'evaluated_at':NOW.isoformat()}
  rr=replay(frozen)
  for sid,items in rs.items():
   for r in items:
    for key in ('status','passed','missing','failed'):self.assertEqual(rr[sid][r['code']][key],r[key])
 def test_etf_market_prefix(self):
  from pool_pipeline import _tx_prefix
  self.assertEqual(_tx_prefix('510300'),'sh');self.assertEqual(_tx_prefix('159915'),'sz');self.assertEqual(_tx_prefix('920001'),'bj')

class RankingContracts(unittest.TestCase):
 def row(self,**kw):
  base={'status':'complete','setup_state':'confirmed','setup_failed':0,'missing':[],'tier':'核心'}
  base.update(kw);return base
 def test_risk_adjusted_key_prefers_higher_excess_per_vol(self):
  from strategy_engine import rank_value
  f={'relative20':4.0,'risk_adj':2.0}
  self.assertLess(rank_value(f,self.row(),'risk_adj'),rank_value({'relative20':4.0,'risk_adj':1.0},self.row(),'risk_adj'))
 def test_missing_risk_adjusted_sinks_without_faking_zero(self):
  from strategy_engine import rank_value
  self.assertEqual(rank_value({},self.row(),'risk_adj'),float('inf'))
 def test_hard_readiness_still_precedes_score(self):
  from strategy_engine import rank_value
  f={'risk_adj':99.0,'relative20':99.0}
  self.assertGreater(rank_value(f,self.row(status='blocked'),'readiness'),rank_value(f,self.row(status='complete'),'readiness'))
 def test_catalog_ranks_include_risk_adjusted(self):
  c=load_catalog()
  for p in c['profiles']:
   self.assertEqual(p['rank'][:3],['readiness','setup_state','risk_adj'])
   self.assertNotIn('missing_count',p['rank'][:3])
 def test_vol20_is_present_and_not_faked(self):
  with tempfile.TemporaryDirectory() as tmp:
   c=H.connect(Path(tmp)/'h.sqlite');days=[(dt.date(2026,1,1)+dt.timedelta(days=i)).isoformat() for i in range(30)]
   for i,d in enumerate(days):
    price=10*(1+0.01*i)
    c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)',(d,'000001',price,price,price,price,1000,10000,1))
   f=H.features(c,'000001',days[-1],days);self.assertIn('vol20',f);self.assertGreater(f['vol20'],0)
   f2=H.features(c,'000001',days[10],days[:11]);self.assertNotIn('vol20',f2);c.close()

class RankOrderTest(unittest.TestCase):
 def test_universe_sorts_by_risk_adjusted_excess(self):
  from strategy_engine import evaluate_universe
  c=load_catalog()
  rows=[{'code':code,'name':code,'池层':'核心','_scene':'机器人','筛1_股性':True,'筛2_辨识度':True,'筛3_量价':True} for code in ('000001','000002','000003')]
  quotes={code:{'price':10,'quote_time':'20261027100000','pct':1,'liangbi':1.2} for code in ('000001','000002','000003')}
  state={'data_date':'20261026','previous_session':'2026-10-26','research_pool_date':'20261026','quote_health':{}}
  base={'last_close':10,'previous_close':9.9,'ma20':9.5,'previous_ma20':9.4,'shape_a':True,'momentum_turn':True,'history_asof':'2026-10-26'}
  histories={'000001':{**base,'ret20':9,'vol20':10},'000002':{**base,'ret20':6,'vol20':20},'000003':{**base,'ret20':3,'vol20':40}}
  rs,fs=evaluate_universe(c,rows,quotes,state,histories,[],NOW,True)
  self.assertAlmostEqual(fs['000001']['risk_adj'],.3,places=6)
  self.assertAlmostEqual(fs['000002']['risk_adj'],0.0,places=6)
  self.assertEqual([r['code'] for r in rs['wind_a']][:3],['000001','000002','000003'])
