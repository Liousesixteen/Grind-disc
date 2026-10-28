import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import strategy_history as H
import event_study as ES


class FeatureEquivalenceTest(unittest.TestCase):
    """The batch engine must agree with the production single-stock features."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'h.sqlite')
        self.c = H.connect(self.db)
        rng = np.random.default_rng(11)
        n, code = 90, '600001'
        dates = pd.date_range('2026-05-01', periods=n, freq='B').strftime('%Y-%m-%d').tolist()
        close = 10 * np.exp(np.cumsum(rng.normal(0, .02, n)))
        open_ = close * (1 + rng.normal(0, .005, n))
        high = np.maximum(open_, close) * 1.01
        low = np.minimum(open_, close) * .99
        volume = 1e6 * np.exp(rng.normal(0, .2, n))
        adj = 1 + np.arange(n) * .001          # varying adjustment factor
        rows = [(dates[i], code, open_[i], high[i], low[i], close[i], volume[i], volume[i] * close[i], adj[i])
                for i in range(n)]
        with self.c:
            self.c.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)', rows)
            self.c.executemany("INSERT INTO calendar VALUES(?,1)", [(d,) for d in dates])
        self.dates = dates

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_agrees_on_several_cutoffs(self):
        code = '600001'
        rows = [dict(r) for r in self.c.execute('SELECT * FROM bars WHERE code=? ORDER BY date', (code,))]
        df = pd.DataFrame(rows)
        df['aopen'] = df.open * df.adj
        df['ahigh'] = df.high * df.adj
        df['alow'] = df.low * df.adj
        df['aclose'] = df.close * df.adj
        df['avol'] = df.volume / df.adj
        px = {k: df.set_index('date')[[k]].rename(columns={k: code}) for k in
              ('open', 'high', 'low', 'close', 'volume', 'aopen', 'ahigh', 'alow', 'aclose', 'avol')}
        batch = ES.features(px)
        from strategy_engine import enrich_daily_signals
        checked = 0
        for offset in (1, 5, 10, 20):
            day = self.dates[-offset]
            prod = enrich_daily_signals(dict(H.features(self.c, code, day, [d for d in self.dates if d <= day])))
            pos = self.dates.index(day)
            last = px['aclose'].iloc[pos, 0]
            self.assertAlmostEqual(prod['ma20'] / prod['last_close'], batch['ma20'].iloc[pos, 0] / last, places=8)
            self.assertEqual(bool(prod['shape_a']), bool(batch['shape_a'].iloc[pos, 0]))
            self.assertEqual(bool(prod['momentum_turn']), bool(batch['momentum_turn'].iloc[pos, 0]))
            got = batch['signal_a_classes'].iloc[pos, 0]
            self.assertEqual(pd.isna(prod['signal_a_classes']), pd.isna(got))
            if not pd.isna(got):
                self.assertEqual(prod['signal_a_classes'], got)
            got_b = batch['b_confirmed'].iloc[pos, 0]
            self.assertEqual(pd.isna(prod['b_nextday_confirmed']), pd.isna(got_b))
            if not pd.isna(got_b):
                self.assertEqual(bool(prod['b_nextday_confirmed']), bool(got_b))
            checked += 1
        self.assertEqual(checked, 4)

if __name__ == '__main__':
    unittest.main()
