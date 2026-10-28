#!/usr/bin/env python3
"""Preview the current template against an existing local board, without new data jobs.

Usage: .venv/bin/python scripts/preview_board.py --port 6063
The upstream board remains on 6060. Watch records use the same local journal.
"""
import argparse
import json
import datetime as dt
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from intraday_board import Board, serve
from pool_pipeline import src_snapshot_tencent


class Preview:
    def __init__(self, upstream):
        self.upstream = upstream
        self.journal = Board.__new__(Board)
        self.quote_lock = threading.Lock()
        self.quote_key = None
        self.quote_at = 0
        self.quote_cache = {}

    def watch_quotes(self):
        codes = tuple(sorted({str(w.get('code', '')) for w in self.watches_today()
                              if str(w.get('code', '')).isdigit() and len(str(w.get('code', ''))) == 6}))
        # Fetch the watchlist itself, not the changing related-candidate subset.
        # Empty codes must not be passed to the provider (it means all A shares).
        with self.quote_lock:
            if codes != self.quote_key or time.monotonic() - self.quote_at >= 15:
                rows = src_snapshot_tencent(list(codes), workers=1) if codes else []
                fetched = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.quote_cache = {r['code']: {
                    **{k: r.get(k) for k in ('price', 'pct', 'quote_time')},
                    'src': 'tencent_qt', 'fetched_at': fetched,
                } for r in rows if r.get('code') in codes}
                self.quote_key, self.quote_at = codes, time.monotonic()
            return dict(self.quote_cache)

    def loop(self):
        pass  # Reuse upstream data; no scheduler, pool rebuild or extra provider calls.

    def get_state(self):
        with urlopen(self.upstream + '/api/state', timeout=8) as response:
            state = json.load(response)
        state['watch_quotes'] = self.watch_quotes()
        return state

    def watches_today(self):
        return self.journal.watches_today()

    def record_watch(self, obj):
        self.journal.state = self.get_state()
        self.journal.record_watch(obj)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=6063)
    args = parser.parse_args()
    template = Path(__file__).resolve().parents[1] / 'templates' / 'intraday_board.html'
    serve(Preview('http://127.0.0.1:6060'), args.port,
          page_loader=lambda: template.read_text(encoding='utf-8'))
