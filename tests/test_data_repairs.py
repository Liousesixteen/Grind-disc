import datetime as dt
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pandas as pd
import data_runtime as D
import ts_daily as T
import ts_etf as E
import cand_review as C
from job_runner import due_jobs


class Repairs(unittest.TestCase):
    def setUp(self):
        self.c=D.connect(':memory:');self.c.executescript(E.DDL+'''
CREATE TABLE dim_stock(code TEXT PRIMARY KEY,name TEXT);
CREATE TABLE fact_snapshot(date TEXT,code TEXT,name TEXT,pct REAL,price REAL,amount REAL,turnover REAL,liangbi REAL,fmcap REAL,PRIMARY KEY(date,code));
CREATE TABLE fact_kpl(date TEXT,theme TEXT,theme_name TEXT,code TEXT,name TEXT,hot_num INT,z_t_num INT,PRIMARY KEY(date,theme,code));
CREATE TABLE fact_lhb(date TEXT,code TEXT,name TEXT,buy REAL,sell REAL,net REAL,reason TEXT,PRIMARY KEY(date,code));
CREATE TABLE fact_lhb_seat(date TEXT,code TEXT,seat TEXT,side TEXT,amount REAL);
CREATE TABLE fact_limitup(date TEXT,code TEXT,name TEXT,lb INT);
''')
    def tearDown(self):self.c.close()
    def cal(self,days):
        for i,d in enumerate(days):self.c.execute('INSERT INTO trade_calendar VALUES(?,?,?,?)',('SSE',d,1,days[i-1] if i else '2026-09-30'))
        self.c.commit()
    def snap(self,d,p):
        self.c.execute('INSERT INTO fact_snapshot VALUES(?,?,?,?,?,?,?,?,?)',(d,'000001','x',0,p,0,0,0,0));self.c.commit()
    def test_candidate_correct_future_and_compounding(self):
        self.cal(['2026-10-22','2026-10-23','2026-10-26','2026-10-27'])
        for d,p in [('2026-10-22',50),('2026-10-23',110),('2026-10-26',121)]:self.snap(d,p)
        r=C.evaluate(self.c,{'ts':'2026-10-23 10:00:00','code':'000001','entry_price':100})
        self.assertEqual(r['收盘'],110);self.assertEqual(r['次日日期'],'2026-10-26')
        self.assertEqual(r['当日剩余%'],10);self.assertEqual(r['次日%'],10);self.assertEqual(r['两日累计%'],21)
    def test_candidate_does_not_jump_suspension(self):
        self.cal(['2026-10-22','2026-10-23','2026-10-26']);self.snap('2026-10-22',10);self.snap('2026-10-26',20)
        r=C.evaluate(self.c,{'ts':'2026-10-22 10:00:00','code':'000001','entry_price':10})
        self.assertIsNone(r['次日%'])
    def test_missing_price_and_offline_click_not_fabricated(self):
        r=C.evaluate(self.c,{'ts':'2026-10-23 10:00:00','code':'000001'})
        self.assertIsNone(r['触发价']);self.assertIsNone(r['两日累计%'])
        r=C.evaluate(self.c,{'ts':'2026-10-24 00:01:00','data_date':'20261023','code':'000001','entry_price':10})
        self.assertEqual(r['样本类型'],'盘后回看');self.assertIsNone(r['次日%'])
    def test_partition_rollback_and_no_false_watermark(self):
        def bad():
            self.c.execute("INSERT INTO dim_stock VALUES('1','bad')")
            raise RuntimeError('fail')
        self.assertFalse(D.run_partition(self.c,'daily','20261023',bad))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM dim_stock').fetchone()[0],0)
        self.assertIsNone(self.c.execute("SELECT value FROM meta WHERE key='daily:last_data_date'").fetchone())
        self.assertTrue(D.run_partition(self.c,'other','20261023',lambda:2))
    def test_lhb_idempotent_sell_side_and_reasons(self):
        class Pro:
            def top_list(s,**k):return pd.DataFrame([
                dict(ts_code='000001.SZ',name='x',reason='day',l_buy=10,l_sell=2,net_amount=8),
                dict(ts_code='000001.SZ',name='x',reason='three-days',l_buy=100,l_sell=20,net_amount=80)])
            def top_inst(s,**k):return pd.DataFrame([dict(ts_code='000001.SZ',exalter='seat',side='1',reason='day',buy=10,sell=90,net_buy=-80)])
        for _ in range(2):
            with self.c:T.lhb(self.c,Pro(),'20261023')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM fact_lhb_detail').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*),MAX(amount) FROM fact_lhb_seat').fetchone(),(1,90))
    def test_kpl_fetches_all_directory_partitions(self):
        class Pro:
            calls=[]
            def kpl_concept(s,**k):return pd.DataFrame([dict(ts_code=t,name=t,z_t_num=1,up_num=1) for t in ['A','B']])
            def kpl_concept_cons(s,**k):
                s.calls.append(k['ts_code']);return pd.DataFrame([dict(ts_code=k['ts_code'],con_code='000001.SZ',name=k['ts_code'],con_name='x',trade_date='20261023',hot_num=5)])
        pro=Pro()
        with self.c:T.kpl(self.c,pro,'20261023')
        self.assertEqual(pro.calls,['A','B']);self.assertEqual(self.c.execute('SELECT COUNT(*) FROM fact_kpl').fetchone()[0],2)
    def test_partial_commit_does_not_advance_watermark(self):
        def partial():
            self.c.execute("INSERT INTO dim_stock VALUES('1','valid_partial')")
            return D.PartitionResult(1,'partial','NAV pending')
        self.assertFalse(D.run_partition(self.c,'etf_daily','20261023',partial))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM dim_stock').fetchone()[0],1)
        self.assertIsNone(self.c.execute("SELECT value FROM meta WHERE key='etf_daily:last_data_date'").fetchone())
    def test_etf_daily_distinguishes_listed_and_registered_identifiers(self):
        self.c.execute("INSERT INTO dim_etf(code,name) VALUES('158008','test')");self.c.commit()
        class Pro:
            def fund_daily(s,**k):return pd.DataFrame([
                dict(ts_code='158008.OF',close=1,pre_close=1,amount=100),
                dict(ts_code='158008.SZ',close=1,pre_close=1,amount=100)])
            def fund_nav(s,**k):return pd.DataFrame([dict(ts_code='158008.OF',unit_nav=1,net_asset=100)])
        with self.c:result=E.daily(self.c,Pro(),'20261023')
        self.assertEqual(result.rows,1)
        self.assertEqual(self.c.execute('SELECT amount,nav FROM fact_etf_daily').fetchone(),(100000,1))
    def test_repeated_page_rejected(self):
        class Pro:
            def ths_member(s,**k):return pd.DataFrame({'x':[1,2]})
        with self.assertRaises(D.ProviderError):T.paged(Pro(),'ths_member',2)
    def test_calendar_holiday_and_independent_jobs(self):
        self.cal(['2026-10-23','2026-10-26'])
        for src in ('daily','moneyflow','kpl','ths','etf_daily','etf_limit','etf_share','etf_margin'):D.mark(self.c,src,'20261026','ok',1)
        self.assertEqual(due_jobs(self.c,dt.datetime(2026,10,26,20,30,tzinfo=D.TZ)),['lhb'])
        self.c.execute("INSERT INTO trade_calendar VALUES('SSE','2026-10-01',0,'2026-09-30')");self.c.commit()
        self.assertEqual(due_jobs(self.c,dt.datetime(2026,10,1,20,30,tzinfo=D.TZ)),[])
    def test_flow_long_holiday_adjacent_missing_nav_gap(self):
        self.cal(['2026-09-30','2026-10-09','2026-10-12','2026-10-13'])
        self.c.execute("INSERT INTO dim_etf(code,name,chain_id) VALUES('510300','test','chain')")
        self.c.executemany('INSERT INTO fact_etf_share VALUES(?,?,?)',[('510300','2026-09-30',100),('510300','2026-10-09',120),('510300','2026-10-13',200)])
        self.c.execute("INSERT INTO fact_etf_daily VALUES('2026-10-09','510300',2,1,100,NULL,NULL,NULL)");self.c.commit();E.views(self.c)
        self.assertEqual(self.c.execute("SELECT share_chg FROM v_etf_flow WHERE date='2026-10-09'").fetchone()[0],20)
        self.assertIsNone(self.c.execute("SELECT share_chg FROM v_etf_flow WHERE date='2026-10-13'").fetchone()[0])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM v_etf_chain_flow').fetchone()[0],0)
        self.c.execute("UPDATE fact_etf_daily SET nav=2");self.c.commit()
        self.assertAlmostEqual(self.c.execute('SELECT 净申赎亿 FROM v_etf_chain_flow').fetchone()[0],.004)
    def test_momentum_exact_compound_window(self):
        self.c.execute("INSERT INTO dim_etf(code,name,chain_id) VALUES('510300','test','chain')")
        for i in range(22):
            self.c.execute('INSERT INTO fact_etf_daily VALUES(?,?,?,?,?,?,?,?)',((dt.date(2026,9,1)+dt.timedelta(days=i)).isoformat(),'510300',10,1,100,1,None,0))
        self.c.commit();E.views(self.c)
        r=self.c.execute('SELECT avg5,avg20 FROM v_etf_momentum').fetchone()
        self.assertAlmostEqual(r[0],(1.01**5-1)*100);self.assertAlmostEqual(r[1],(1.01**20-1)*100)

if __name__=='__main__':unittest.main()
