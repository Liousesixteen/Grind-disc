#!/usr/bin/env python3
"""Independent, idempotent LHB publication retry (same implementation as daily ingest)."""
import argparse
from data_runtime import DEFAULT_DB, ProClient, connect, target_date, run_partition, job_lock
from ts_daily import lhb

def main():
    ap=argparse.ArgumentParser();ap.add_argument('date',nargs='?');ap.add_argument('--db',default=str(DEFAULT_DB));a=ap.parse_args()
    with job_lock('ts_lhb'):
        c=connect(a.db)
        try:
            pro=ProClient();day=target_date(c,pro,a.date)
            return 0 if run_partition(c,'lhb',day,lambda:lhb(c,pro,day)) else 1
        finally:c.close()

if __name__=='__main__':raise SystemExit(main())
