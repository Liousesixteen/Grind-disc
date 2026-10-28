import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import research_backtest as R


def panel(corr=1.0, n=40):
    dates = [f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}' for i in range(n)]
    bars, limits = {}, {}
    for i, d in enumerate(dates):
        base = 10 * (1 + 0.01 * math.sin(i))
        for code, sign in (('600001', 1), ('600002', corr)):
            p = round(10 * (1 + 0.01 * sign * math.sin(i)), 4)
            bars[(d, code)] = {'open': p, 'high': p * 1.01, 'low': p * 0.99, 'close': p,
                               'volume': 1e7, 'adj': 1.0}
            limits[(d, code)] = {'up': round(p * 1.1, 2), 'down': round(p * 0.9, 2)}
    return dates, bars, limits


def settings(**kw):
    base = {'capital': 1e6, 'position_fraction': .6, 'first_fraction': 1 / 3, 'max_exposure': .6,
            'stop_pct': .08, 'target_pct': .16, 'holding_days': 60, 'commission_bps': 3.,
            'commission_min': 5., 'sell_tax_bps': 5., 'slippage_bps': 5., 'dividend_tax': .2,
            'max_participation': .01, 'risk_per_trade': .02, 'group_corr': .7,
            'group_cap': .05, 'max_new_per_day': 3}
    base.update(kw)
    return base


class PortfolioConstraintTest(unittest.TestCase):
    def test_risk_budget_scales_position_with_stop_distance(self):
        dates, bars, limits = panel()
        signals = {dates[35]: ['600001']}
        low = R.simulate(['600001'], dates, bars, limits, {}, signals, settings(risk_per_trade=.001))
        high = R.simulate(['600001'], dates, bars, limits, {}, signals, settings(risk_per_trade=.02))
        q_low = next(iter(low['open_positions'].values()))['qty']
        q_high = next(iter(high['open_positions'].values()))['qty']
        self.assertLess(q_low, q_high)
        self.assertLessEqual(q_low * 10 * .08, 1e6 * .001 + 1)

    def test_correlated_second_name_is_capped(self):
        dates, bars, limits = panel(corr=1.0)
        signals = {dates[35]: ['600001', '600002']}
        out = R.simulate(['600001', '600002'], dates, bars, limits, {}, signals, settings())
        self.assertEqual(len(out['open_positions']), 1, out['skips'])
        self.assertTrue(any('相关性' in s['reason'] for s in out['skips']))

    def test_uncorrelated_names_can_both_open(self):
        dates, bars, limits = panel(corr=-1.0)
        signals = {dates[35]: ['600001', '600002']}
        out = R.simulate(['600001', '600002'], dates, bars, limits, {}, signals, settings())
        self.assertEqual(len(out['open_positions']), 2)

    def test_daily_new_position_cap(self):
        dates, bars, limits = panel(corr=-1.0)
        signals = {dates[35]: ['600001', '600002']}
        out = R.simulate(['600001', '600002'], dates, bars, limits, {}, signals, settings(max_new_per_day=1))
        self.assertEqual(len(out['open_positions']), 1)
        self.assertTrue(any('单日新开仓' in s['reason'] for s in out['skips']))

    def test_invalid_constraints_are_rejected(self):
        for bad in ({'risk_per_trade': 0}, {'risk_per_trade': .5}, {'group_corr': 1.0},
                    {'group_cap': 0}, {'max_new_per_day': 0}, {'max_new_per_day': 2.5}):
            with self.assertRaises(ValueError):
                R.validate_settings(settings(**bad))


if __name__ == '__main__':
    unittest.main()
