import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import research_data as RD


class FakePro:
    def __init__(self, n=3000):
        self.n = n

    def stk_limit(self, trade_date=''):
        rows = [{'ts_code': f'60000{i}.SH', 'trade_date': trade_date, 'up_limit': 11.0 + i, 'down_limit': 9.0 + i}
                for i in range(self.n)]
        return pd.DataFrame(rows)

    def daily_basic(self, trade_date='', fields=''):
        rows = [{'ts_code': f'60000{i}.SH', 'trade_date': trade_date, 'close': 10.0,
                 'turnover_rate': 1.5, 'volume_ratio': 1.1, 'pe_ttm': 12.0, 'pb': 1.2,
                 'total_mv': 1000.0, 'circ_mv': 800.0, 'total_share': 100.0, 'float_share': 80.0}
                for i in range(self.n)]
        return pd.DataFrame(rows)

    def index_daily(self, ts_code='', start_date='', end_date=''):
        rows = [{'ts_code': ts_code, 'trade_date': '20261027', 'open': 1.0, 'high': 1.1,
                 'low': 0.9, 'close': 1.0, 'vol': 10.0, 'amount': 20.0}]
        return pd.DataFrame(rows)


class ResearchDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'h.sqlite')
        self.c = RD.connect(self.db)
        with self.c:
            self.c.execute("INSERT INTO calendar VALUES('2026-10-26',1)")
            self.c.execute("INSERT INTO calendar VALUES('2026-10-27',1)")

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_units_converted(self):
        RD.sync_basic(self.c, FakePro(3000), '2026-10-27')
        row = self.c.execute('SELECT total_mv_cny,circ_mv_cny,float_share_shares FROM basic LIMIT 1').fetchone()
        self.assertAlmostEqual(row[0], 1000.0 * 1e4)
        self.assertAlmostEqual(row[1], 800.0 * 1e4)
        self.assertAlmostEqual(row[2], 80.0 * 1e4)

    def test_truncated_partition_refused(self):
        with self.assertRaises(ValueError):
            RD.sync_limits(self.c, FakePro(2), '2026-10-27')
        self.assertFalse(RD.published(self.c, 'limits', '2026-10-27'))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM market_limit').fetchone()[0], 0)

    def test_date_mismatch_refused(self):
        class WrongDate(FakePro):
            def stk_limit(self, trade_date=''):
                frame = super().stk_limit(trade_date)
                frame['trade_date'] = '20261026'
                return frame
        with self.assertRaises(ValueError):
            RD.sync_limits(self.c, WrongDate(3000), '2026-10-27')

    def test_sessions_reverse_order(self):
        days = RD.sessions(self.c, '2026-10-27', 2)
        self.assertEqual(days, ['2026-10-26', '2026-10-27'])

    def test_verify_reports_missing(self):
        RD.sync_limits(self.c, FakePro(3000), '2026-10-27')
        out = RD.verify(self.c, ['2026-10-26', '2026-10-27'])
        self.assertEqual(out['sources']['limits']['ok_days'], 1)
        self.assertIn('2026-10-26', out['sources']['limits']['missing_days'])

    def test_index_partial_without_all_codes(self):
        class Thin(FakePro):
            def index_daily(self, ts_code='', start_date='', end_date=''):
                raise ValueError('no data')
        failed = RD.sync_index(self.c, Thin(), ['2026-10-27'])
        self.assertTrue(failed)
        status = self.c.execute("SELECT status FROM research_partition WHERE source='index' AND date='2026-10-27'").fetchone()[0]
        self.assertEqual(status, 'partial')


if __name__ == '__main__':
    unittest.main()
