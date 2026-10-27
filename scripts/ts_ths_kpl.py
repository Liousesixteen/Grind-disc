#!/usr/bin/env python3
"""Compatibility entry: complete THS/KP partitions via the maintained daily pipeline.

Use --date YYYYMMDD for historical partitions. The old 3000-row whole-day
shortcut and fixed 20261026 history cutoff have been retired.
"""
import sys
from ts_daily import main
if __name__=='__main__':
    sys.argv.extend(['--sources','ths,kpl'])
    raise SystemExit(main())
