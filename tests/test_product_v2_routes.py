"""Actual write routes, isolated SQLite, no sockets or fabricated market data."""
import io,json,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from product_service import handler
from product_store import ProductStore
from strategy_engine import load_catalog
import strategy_history as H
from research_market import verified_cohorts

class Routes(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=ProductStore(Path(self.tmp.name)/'store.sqlite')
  self.service=SimpleNamespace(store=self.store,catalog=load_catalog(),members={'fixture':{'000001'}},assets={'000001':{}},wake=threading.Event())
 def tearDown(self):self.tmp.cleanup()
 def post(self,path,body,**headers):
  cls=handler(self.service,lambda:'');h=cls.__new__(cls);raw=json.dumps(body).encode();h.path=path;h.rfile=io.BytesIO(raw)
  h.headers={'Content-Type':'application/json','Content-Length':str(len(raw)),'Host':'127.0.0.1:6064',**headers};result={}
  h.answer=lambda value,status=200:result.update(body=value,status=status)
  h.do_POST();return result
 def test_cross_site_rejected(self):
  r=self.post('/api/review',{},Origin='https://unrelated.example');self.assertEqual(r['status'],400)
 def test_global_stock_risk_cannot_be_approved(self):
  r=self.post('/api/review',{'strategy_id':'wind_a','field':'risk_review','code':'*','value':True});self.assertEqual(r['status'],400)
 def test_price_fields_cannot_be_manually_approved(self):
  r=self.post('/api/review',{'strategy_id':'wind_a','field':'price','code':'000001','value':True});self.assertEqual(r['status'],400)
 def test_global_macro_scope_valid(self):
  r=self.post('/api/review',{'strategy_id':'wind_a','field':'macro_approved','code':'*','value':False,'evidence':'fixture independent global evidence','expires_at':'2099-01-01T00:00:00+08:00'})
  self.assertEqual(r['status'],200);self.assertEqual(self.store.reviews()[0]['code'],'*@wind_a')
 def test_plan_rejects_style_mismatch(self):
  self.store.add_watch({'code':'000001','name':'fixture'},scene='fixture')
  r=self.post('/api/plan',{'code':'000001','strategy_id':'wind_a','style':'核心长线'});self.assertEqual(r['status'],400)
 def test_legacy_plan_migrates_paused_and_requires_revision(self):
  with self.store.connect() as c:c.execute('INSERT INTO watch_plan VALUES(?,?,?,?,?,?,?,?,?)',('000001','波段','wind_a',10,9,12,'2099-01-01T00:00:00+08:00','fixture old note','2026-10-27'))
  self.store.migrate_legacy_plans();self.store.migrate_legacy_plans();plans=self.store.instances(archived=True)
  self.assertEqual(len(plans),1);self.assertEqual(plans[0]['status'],'paused')
  with self.assertRaises(ValueError):self.store.instance_action(plans[0]['id'],'resume')
 def test_verified_cohort_uses_real_limits_not_cache_threshold(self):
  db=Path(self.tmp.name)/'history.sqlite';c=H.connect(db)
  c.executescript('CREATE TABLE market_limit_partition(date TEXT,status TEXT,n INT,note TEXT);CREATE TABLE execution_limit(date TEXT,code TEXT,up REAL,down REAL)')
  for day in ['2026-10-23','2026-10-26','2026-10-27']:
   c.execute('INSERT INTO calendar VALUES(?,1)',(day,))
   c.execute("INSERT INTO market_limit_partition VALUES(?,'ok',1,'fixture')",(day,))
   c.execute('INSERT INTO execution_limit VALUES(?,?,?,?)',(day,'000001',11,9))
   c.execute('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)',(day,'000001',10,11,10,11,10000,100000,1))
  c.commit();c.close();r=verified_cohorts('2026-10-27',{},db)
  self.assertTrue(r['ready']);self.assertEqual(r['previous_map']['000001'],2)

if __name__=='__main__':unittest.main()
