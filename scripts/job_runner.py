#!/usr/bin/env python3
"""One-owner job execution and persisted retry state; UI is only an optional trigger."""
import argparse
import json
import subprocess
import sys
from data_runtime import ROOT,DEFAULT_DB,ProClient,connect,iso_date,now,is_open,refresh_calendar,job_lock

REQUIRED={'daily':('daily','moneyflow','kpl','ths','etf_daily','etf_limit','etf_share','etf_margin'),
          'lhb':('lhb',)}
TIMES={'daily':(16,30),'lhb':(20,30)}


def complete(c,mode,day):
    return all(c.execute('SELECT status FROM ingest_partition WHERE source=? AND date=?',(s,iso_date(day))).fetchone()==('ok',) for s in REQUIRED[mode])


def due_jobs(c,moment):
    day=moment.date().isoformat()
    if not is_open(c,day):return []
    return [mode for mode,(h,m) in TIMES.items() if (moment.hour,moment.minute)>=(h,m) and not complete(c,mode,day)]


def run_job(mode,day=None,db=DEFAULT_DB):
    with job_lock('pipeline_'+mode):
        c=connect(db)
        c.execute('CREATE TABLE IF NOT EXISTS job_run(mode TEXT,date TEXT,status TEXT,attempts INT,updated_at TEXT,PRIMARY KEY(mode,date))')
        try:
            day=iso_date(day or now().date())
            try:is_open(c,day)
            except Exception:refresh_calendar(c,ProClient(),day)
            if not is_open(c,day):print('休市，跳过');return 0
            if complete(c,mode,day):print('该作业全部分区已完成');return 0
            previous=c.execute('SELECT attempts FROM job_run WHERE mode=? AND date=?',(mode,day)).fetchone()
            attempts=(previous[0] if previous else 0)+1
            c.execute('INSERT OR REPLACE INTO job_run VALUES(?,?,?,?,?)',(mode,day,'running',attempts,now().isoformat()));c.commit()
            args=(["/bin/bash",str(ROOT/'scripts/daily_run.sh'),'--date',day,'--db',str(db)] if mode=='daily' else
                  [sys.executable,str(ROOT/'scripts/ts_retry_lhb.py'),day,'--db',str(db)])
            try:rc=subprocess.run(args,cwd=ROOT,timeout=2400).returncode
            except subprocess.TimeoutExpired:rc=124
            success=rc==0 and complete(c,mode,day)
            c.execute('UPDATE job_run SET status=?,updated_at=? WHERE mode=? AND date=?',('ok' if success else 'fail',now().isoformat(),mode,day));c.commit()
            return 0 if success else 1
        finally:c.close()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=REQUIRED);ap.add_argument('--date');ap.add_argument('--db',default=str(DEFAULT_DB));a=ap.parse_args()
    return run_job(a.mode,a.date,a.db)

if __name__=='__main__':raise SystemExit(main())
