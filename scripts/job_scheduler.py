#!/usr/bin/env python3
"""Standalone optional scheduler: persisted per-job success; bounded retries, no weekday guess."""
import argparse
import datetime as dt
import subprocess
import sys
import time
from data_runtime import ROOT,DEFAULT_DB,connect,now,refresh_calendar,ProClient
from job_runner import due_jobs


def tick(db=DEFAULT_DB):
    c=connect(db)
    try:
        day=now().date().isoformat()
        if not c.execute("SELECT 1 FROM trade_calendar WHERE exchange='SSE' AND cal_date=?",(day,)).fetchone():
            refresh_calendar(c,ProClient(),day)
        for mode in due_jobs(c,now()):
            exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_run'").fetchone()
            row=c.execute('SELECT status,attempts,updated_at FROM job_run WHERE mode=? AND date=?',(mode,day)).fetchone() if exists else None
            if row:
                elapsed=(now()-dt.datetime.fromisoformat(row[2])).total_seconds()
                if row[0]=='running' and elapsed<2700:continue
                if row[1]>=3 or elapsed<900:continue
            # Locks in job_runner prevent another scheduler or timer executing the same pipeline.
            subprocess.run([sys.executable,str(ROOT/'scripts/job_runner.py'),mode,'--date',day,'--db',str(db)],cwd=ROOT,timeout=2500)
    finally:c.close()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--once',action='store_true');a=ap.parse_args()
    while True:
        try:tick()
        except Exception as e:print(f'调度暂缓：{type(e).__name__}',flush=True)
        if a.once:break
        time.sleep(30)

if __name__=='__main__':main()
