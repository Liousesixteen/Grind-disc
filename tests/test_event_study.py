import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import event_study as ES


def panel(n=80, codes=('600001', '600002'), seed=7):
    dates = pd.date_range('2026-01-05', periods=n, freq='B').strftime('%Y-%m-%d').tolist()
    rng = np.random.default_rng(seed)
    close = pd.DataFrame(10 + np.cumsum(rng.normal(0, .05, (n, len(codes))), axis=0),
                         index=dates, columns=list(codes))
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([close, open_], axis=0).groupby(level=0).max().reindex(dates)
    low = pd.concat([close, open_], axis=0).groupby(level=0).min().reindex(dates)
    vol = pd.DataFrame(1e6, index=dates, columns=list(codes))
    px = {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol,
          'aopen': open_, 'ahigh': high, 'alow': low, 'aclose': close, 'avol': vol}
    up = close * 1.10
    down = close * 0.90
    return px, up, down, dates, list(codes)


class ForwardReturnTest(unittest.TestCase):
    def test_entry_next_open_exit_after_holding(self):
        px, up, down, dates, codes = panel(n=10)
        ret, valid, entry, exit_px, _ = ES.forward(px, up, down, 1)
        self.assertAlmostEqual(entry.iloc[3, 0], px['aopen'].iloc[4, 0])
        self.assertAlmostEqual(exit_px.iloc[3, 0], px['aclose'].iloc[5, 0])
        self.assertTrue(valid.iloc[3, 0])
        self.assertFalse(valid.iloc[8, 0], '末尾没有完整窗口')

    def test_limit_up_open_blocks_entry(self):
        px, up, down, dates, codes = panel(n=10)
        up2 = up.copy()
        up2.iloc[4, 0] = px['aopen'].iloc[4, 0]
        _, valid, _, _, _ = ES.forward(px, up2, down, 1)
        self.assertFalse(valid.iloc[3, 0])

    def test_limit_down_close_blocks_exit(self):
        px, up, down, dates, codes = panel(n=10)
        down2 = down.copy()
        down2.iloc[5, 0] = px['aclose'].iloc[5, 0]
        _, valid, _, _, _ = ES.forward(px, up, down2, 1)
        self.assertFalse(valid.iloc[3, 0])

    def test_missing_session_is_not_zero_return(self):
        px, up, down, dates, codes = panel(n=10)
        px['aclose'].iloc[5, 0] = np.nan
        _, valid, _, _, _ = ES.forward(px, up, down, 1)
        self.assertFalse(valid.iloc[3, 0])


class ClusterTest(unittest.TestCase):
    def test_cluster_mean_and_se(self):
        values = np.array([1., 2., 3.])
        dates = np.array(['d1', 'd1', 'd2'])
        out = ES.cluster_mean(values, dates)
        self.assertAlmostEqual(out['n'], 3)
        self.assertAlmostEqual(out['n_dates'], 2)
        self.assertAlmostEqual(out['mean'], 2.0)
        self.assertAlmostEqual(out['se'], np.sqrt(2 / 9))
        self.assertAlmostEqual(out['t'], 2.0 / np.sqrt(2 / 9))
        self.assertAlmostEqual(out['hit_rate'], 1.0)

    def test_bootstrap_brackets_mean(self):
        values = np.array([1., 2., 3., 4., 5., 6.])
        dates = np.array(['d1', 'd1', 'd2', 'd2', 'd3', 'd3'])
        lo, hi = ES.bootstrap_ci(values, dates, reps=500)
        self.assertLessEqual(lo, 3.5)
        self.assertGreaterEqual(hi, 3.5)


class FeatureTest(unittest.TestCase):
    def test_signal_a_requires_window(self):
        px, up, down, dates, codes = panel(n=80)
        f = ES.features(px)
        self.assertTrue(f['signal_a_classes'].iloc[:59].isna().all().all())
        self.assertFalse(f['signal_a_classes'].iloc[70:].isna().all().all())

    def test_no_lookahead(self):
        px, up, down, dates, codes = panel(n=80)
        f1 = ES.features(px)
        px2 = {k: v.copy() for k, v in px.items()}
        px2['aclose'].iloc[75:, 0] = px2['aclose'].iloc[75:, 0] * 3
        px2['aopen'].iloc[75:, 0] = px2['aopen'].iloc[75:, 0] * 3
        f2 = ES.features(px2)
        pd.testing.assert_series_equal(f1['shape_a'].iloc[:70, 0], f2['shape_a'].iloc[:70, 0])
        pd.testing.assert_series_equal(f1['signal_a_classes'].iloc[:70, 0], f2['signal_a_classes'].iloc[:70, 0])

    def test_breakout_uses_previous_high(self):
        px, up, down, dates, codes = panel(n=40)
        f = ES.features(px)
        row = 30
        expected = px['aclose'].iloc[row, 0] > px['ahigh'].iloc[row - 20:row, 0].max()
        self.assertEqual(bool(f['breakout20'].iloc[row, 0]), bool(expected))


if __name__ == '__main__':
    unittest.main()
