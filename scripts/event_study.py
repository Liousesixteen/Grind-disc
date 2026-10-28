#!/usr/bin/env python3
"""Full-market event study with date-clustered inference.

Answers one question at a time: when a completed-bar signal fires, what is the
forward return from the next session's open, after excluding entries that cannot
be filled (limit-up open / no volume) and exits that cannot be sold (limit-down)?

No portfolio, no position sizing, no order generation. Every number is conditional
on the stated entry/exit rule and the available history.

Usage
  .venv/bin/python scripts/event_study.py --target 2026-10-27 --days 121 \
      --signals signal_a,b_confirmed --horizons 1,5,10 \
      --out reports/2026-10-28/quant/event-study.json
"""
import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_history as H
from data_runtime import iso_date, now

HORIZON_LABEL = {1: '持有1日', 5: '持有5日', 10: '持有10日'}


# ---------------------------------------------------------------- data loading

def load(c, start, end):
    bars = pd.read_sql_query(
        'SELECT date,code,open,high,low,close,volume,adj FROM bars WHERE date BETWEEN ? AND ?',
        c, params=(start, end))
    if bars.empty:
        raise SystemExit('bars 为空，先回补历史')
    bars = bars.sort_values(['date', 'code'])
    for src, dst in (('open', 'aopen'), ('high', 'ahigh'), ('low', 'alow'), ('close', 'aclose')):
        bars[dst] = bars[src] * bars['adj']
    bars['avol'] = bars['volume'] / bars['adj']          # constant scaling per code
    days = sorted(bars['date'].unique())
    codes = sorted(bars['code'].unique())

    def pivot(col):
        return bars.pivot(index='date', columns='code', values=col).reindex(index=days, columns=codes)

    raw = {k: pivot(k) for k in ('open', 'high', 'low', 'close', 'volume')}
    adj = {k: pivot(k) for k in ('aopen', 'ahigh', 'alow', 'aclose', 'avol')}
    px = {**raw, **adj}
    lim = pd.read_sql_query(
        'SELECT date,code,up,down FROM market_limit WHERE date BETWEEN ? AND ?',
        c, params=(start, end))
    if lim.empty:
        raise SystemExit('market_limit 为空，先运行 research_data.py --sources limits')
    up = lim.pivot(index='date', columns='code', values='up').reindex(index=days, columns=codes)
    down = lim.pivot(index='date', columns='code', values='down').reindex(index=days, columns=codes)
    bas = pd.read_sql_query(
        'SELECT date,code,circ_mv_cny FROM basic WHERE date BETWEEN ? AND ?',
        c, params=(start, end))
    size = (bas.pivot(index='date', columns='code', values='circ_mv_cny').reindex(index=days, columns=codes)
            if not bas.empty else pd.DataFrame(np.nan, index=days, columns=codes))
    return px, up, down, size, days, codes


# ---------------------------------------------------------------- features

def ema(frame, span):
    return frame.ewm(span=span, adjust=False).mean()


def rsi14(close):
    """Wilder RSI seeded with the first 14 simple averages, matching strategy_history."""
    out = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    arr = close.to_numpy(dtype=float)
    for j in range(arr.shape[1]):
        col = arr[:, j]
        valid = np.where(~np.isnan(col))[0]
        if len(valid) < 60:
            continue
        series = col[valid]
        gains = np.maximum(np.diff(series), 0)
        losses = np.maximum(-np.diff(series), 0)
        ag, al = gains[:14].mean(), losses[:14].mean()
        vals = []
        for g, l in zip(gains[14:], losses[14:]):
            ag = (ag * 13 + g) / 14
            al = (al * 13 + l) / 14
            vals.append(100 - 100 / (1 + ag / al) if al > 0 else (100 if ag > 0 else 50))
        # gains[14:] ends at close[15..], matching strategy_history's rsis indexing.
        for k, v in enumerate(vals):
            out.iat[valid[15 + k], j] = v
    return out


def _masked(condition, available):
    """Keep True/False where inputs exist; NaN where the window is not available."""
    out = pd.DataFrame(np.nan, index=condition.index, columns=condition.columns)
    out[available] = condition[available].astype(float)
    return out


def features(px):
    o, h, l, c, v = px['aopen'], px['ahigh'], px['alow'], px['aclose'], px['avol']
    f = {}
    f['prev_close'] = c.shift(1)
    f['ma20'] = c.rolling(20, min_periods=20).mean()
    f['prev_ma20'] = f['ma20'].shift(1)
    ma_ok = f['ma20'].notna() & f['prev_ma20'].notna() & f['prev_close'].notna()
    f['ma20_cross'] = _masked((c > f['ma20']) & (f['prev_close'] <= f['prev_ma20']), ma_ok)
    f['high20_prev'] = h.rolling(20, min_periods=20).max().shift(1)
    base_vol = v.rolling(20, min_periods=20).mean().shift(1)
    f['vol_ratio20'] = v / base_vol
    shape_ok = f['high20_prev'].notna() & f['vol_ratio20'].notna() & c.notna() & o.notna()
    f['launch_last'] = _masked((f['vol_ratio20'] > 1.5) & (c > o), shape_ok)
    f['shape_a'] = _masked(((f['vol_ratio20'] > 1.5) & (c > o)) | (c > f['high20_prev']), shape_ok)

    e12, e26 = ema(c, 12), ema(c, 26)
    dif = e12 - e26
    dea = ema(dif, 9)
    macd_cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    dif_turn = (dif < 0) & (dif > dif.shift(1))
    r = rsi14(c)
    rsi_turn = (r.shift(1) < 30) & (r >= 30) & (r <= 55)
    mom_ok = dif.notna() & dea.notna() & r.notna() & r.shift(1).notna() & c.rolling(60, min_periods=60).count().eq(60)
    f['momentum_turn'] = _masked(macd_cross | dif_turn | rsi_turn, mom_ok)
    f['rsi14'] = r
    f['dif'] = dif

    known = f['ma20_cross'].notna() & f['shape_a'].notna() & f['momentum_turn'].notna()
    count = f['ma20_cross'].astype(float).fillna(0) + f['shape_a'].astype(float).fillna(0) + f['momentum_turn'].astype(float).fillna(0)
    sac = np.where(known, count, np.where(count >= 2, count, np.nan))
    f['signal_a_classes'] = pd.DataFrame(sac, index=c.index, columns=c.columns)

    # B setup: prefix statistics exclude the launch bar itself, as in strategy_history.
    v10 = v.shift(1).rolling(10, min_periods=10).mean()
    v20prev = v.shift(11).rolling(20, min_periods=20).mean()
    dry = v10 / v20prev
    low10 = l.shift(1).rolling(10, min_periods=10).min()
    low20prev = l.shift(11).rolling(20, min_periods=20).min()
    rng = (h - l) / c
    r10 = rng.shift(1).rolling(10, min_periods=10).mean()
    r20prev = rng.shift(11).rolling(20, min_periods=20).mean()
    dd60 = (1 - c.shift(1) / h.shift(1).rolling(60, min_periods=60).max()) * 100
    launched = (c > o) & (v > v.shift(1).rolling(20, min_periods=20).mean() * 1.5)
    b_ok = (dry.notna() & low10.notna() & low20prev.notna() & r10.notna()
            & r20prev.notna() & dd60.notna() & launched.notna())
    setup = _masked((dry < .75) & (low10 >= low20prev) & (r10 < r20prev) & (dd60 > 15) & launched, b_ok)
    setup_bool = setup.eq(1)
    launch_open = o.shift(1)
    advance = c.shift(1) - launch_open
    confirmed = (c >= launch_open) & ((advance <= 0) | ((c.shift(1) - c) / advance < .8))
    confirmed_ok = confirmed.notna() & c.notna() & launch_open.notna() & advance.notna() & c.shift(1).notna()
    b = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    b[setup_bool] = np.nan
    prev = setup_bool.shift(1).fillna(False).astype(bool)
    b[prev] = confirmed.astype(float)
    b[prev & ~confirmed_ok] = np.nan
    b[~c.rolling(62, min_periods=62).count().eq(62)] = np.nan
    f['b_confirmed'] = b

    # Simple reference factors (descriptive, not tuned).
    f['breakout20'] = _masked(c > f['high20_prev'], f['high20_prev'].notna() & c.notna())
    f['above_ma20'] = _masked(c > f['ma20'], f['ma20'].notna() & c.notna())
    f['ret5'] = (c / c.shift(5) - 1) * 100
    f['ret20'] = (c / c.shift(20) - 1) * 100
    f['dd60'] = (1 - c / h.rolling(60, min_periods=60).max()) * 100
    return f


# ---------------------------------------------------------------- forward returns

def forward(px, up, down, holding):
    """Entry at t+1 open, exit at close of t+1+holding. T+1 forbids same-day exit.

    Returns are computed on adjusted prices; limit and volume checks use raw prices.
    """
    c, o, v = px['aclose'], px['aopen'], px['volume']
    raw_open, raw_close = px['open'], px['close']
    entry = o.shift(-1)
    exit_px = c.shift(-(1 + holding))
    valid = entry.notna() & exit_px.notna() & (entry > 0)
    for k in range(1, 2 + holding):
        valid &= c.shift(-k).notna()
    ret = exit_px / entry - 1
    up_next = up.shift(-1)
    down_exit = down.shift(-(1 + holding))
    entry_raw = raw_open.shift(-1)
    exit_raw = raw_close.shift(-(1 + holding))
    tradable = up_next.notna() & (entry_raw < up_next - 1e-6) & v.shift(-1).fillna(0).gt(0)
    sellable = down_exit.notna() & (exit_raw > down_exit + 1e-6)
    return ret, valid & tradable & sellable, entry, exit_px, up_next


# ---------------------------------------------------------------- statistics

def cluster_mean(values, dates):
    """values/dates are aligned arrays; cluster-robust SE of the mean by date."""
    frame = pd.DataFrame({'v': values, 'd': dates})
    g = frame.groupby('d')['v'].agg(['sum', 'count'])
    n = int(g['count'].sum())
    if n < 2:
        return None
    mean = float(g['sum'].sum() / n)
    var = float(((g['sum'] - g['count'] * mean) ** 2).sum() / (n ** 2))
    se = math.sqrt(var)
    hit = float((values > 0).mean())
    return {'n': n, 'n_dates': int(len(g)), 'mean': mean, 'se': se,
            't': mean / se if se > 0 else None, 'hit_rate': hit,
            'median': float(np.median(values))}


def bootstrap_ci(values, dates, reps=2000, seed=42):
    frame = pd.DataFrame({'v': values, 'd': dates})
    g = frame.groupby('d')['v'].agg(['sum', 'count'])
    sums, counts = g['sum'].to_numpy(), g['count'].to_numpy()
    keep = counts > 0
    sums, counts = sums[keep], counts[keep]
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(sums), size=(reps, len(sums)))
    means = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)
    return float(np.quantile(means, .025)), float(np.quantile(means, .975))


def monthly(values, dates):
    frame = pd.DataFrame({'v': values, 'd': dates})
    frame['m'] = frame['d'].str[:7]
    out = []
    for m, sub in frame.groupby('m'):
        if len(sub) < 20:
            continue
        out.append({'month': m, 'n': int(len(sub)), 'mean_pct': float(sub['v'].mean() * 100),
                    'hit_rate': float((sub['v'] > 0).mean())})
    return out


# ---------------------------------------------------------------- study

def residualize(ret, size, excluded):
    """Cross-sectional OLS on log float market cap, one regression per date."""
    out = pd.DataFrame(np.nan, index=ret.index, columns=ret.columns)
    logsize = np.log(size.where(size > 0))
    for day in ret.index:
        y, s = ret.loc[day], logsize.loc[day]
        ok = y.notna() & s.notna() & ~excluded.loc[day]
        if ok.sum() < 50:
            continue
        x = np.column_stack([np.ones(int(ok.sum())), s[ok].to_numpy(dtype=float)])
        beta, *_ = np.linalg.lstsq(x, y[ok].to_numpy(dtype=float), rcond=None)
        out.loc[day, ok] = y[ok].to_numpy(dtype=float) - x @ beta
    return out


def run(px, up, down, days, codes, signals, horizons, size=None, neutralize='size',
        cost_bps=18.0, min_events=500, min_dates=20):
    rets = {h: forward(px, up, down, h) for h in horizons}
    st_like = (up / px['close'].shift(1) - 1) < .075     # raw prices; 5% band ⇒ ST-like
    coverage = {
        'days': len(days), 'codes': len(codes),
        'limit_days': int(up.notna().any(axis=1).sum()),
        'avg_limits_per_day': float(up.notna().sum(axis=1).mean()),
    }
    results, notes = [], []
    for name, sig in signals.items():
        signal = sig.fillna(0).gt(0)
        for h in horizons:
            ret, valid, entry, exit_px, up_next = rets[h]
            mask = signal & valid & ~st_like
            # Market mean uses every stock with a valid forward return that day.
            market = ret.where(valid & ~st_like).mean(axis=1)
            if neutralize == 'size' and size is not None and not size.empty:
                excess = residualize(ret.where(valid & ~st_like), size, st_like | ~valid)
            else:
                excess = ret.sub(market, axis=0)
            flat = excess.where(mask).stack().dropna()
            vals = flat.to_numpy(dtype=float)
            dates = flat.index.get_level_values(0).to_numpy()
            raw = ret.where(mask).stack()
            raw = raw[raw.notna()].to_numpy(dtype=float)
            if len(vals) < 2:
                results.append({'signal': name, 'horizon': h, 'n': int(len(vals)), 'state': 'insufficient'})
                continue
            stats = cluster_mean(vals, dates)
            lo, hi = bootstrap_ci(vals, dates)
            # Overlapping holdings across adjacent dates inflate N; report a spaced subsample.
            day_index = {d: i for i, d in enumerate(days)}
            spaced = np.array([day_index[d] % h == 0 for d in dates])
            nonoverlap = cluster_mean(vals[spaced], dates[spaced]) if spaced.sum() >= 100 else None
            mid = days[len(days) // 2]
            halves = {}
            for label, part in (('前半', dates <= mid), ('后半', dates > mid)):
                sub = cluster_mean(vals[part], dates[part]) if part.sum() >= 100 else None
                if sub:
                    halves[label] = {'n': sub['n'], 'n_dates': sub['n_dates'],
                                     'mean_pct': sub['mean'] * 100, 't': sub['t']}
            stats.update({'signal': name, 'horizon': h, 'label': HORIZON_LABEL.get(h, f'持有{h}日'),
                          'mean_pct': stats['mean'] * 100, 'median_pct': stats['median'] * 100,
                          'se_pct': stats['se'] * 100, 'ci_low_pct': lo * 100, 'ci_high_pct': hi * 100,
                          'raw_mean_pct': float(raw.mean() * 100),
                          'net_mean_pct': stats['mean'] * 100 - cost_bps / 100,
                          'market_mean_pct': float(market.mean() * 100),
                          'state': 'ok' if stats['n'] >= min_events and stats['n_dates'] >= min_dates else 'low_sample',
                          'halves': halves,
                          'nonoverlap': ({'n': nonoverlap['n'], 'n_dates': nonoverlap['n_dates'],
                                          'mean_pct': nonoverlap['mean'] * 100,
                                          't': nonoverlap['t']} if nonoverlap else None),
                          'monthly': monthly(vals, dates)})
            results.append(stats)
            if stats['n'] < min_events or stats['n_dates'] < min_dates:
                notes.append(f'{name}/持有{h}日 事件 {stats["n"]} 个/独立交易日 {stats["n_dates"]} 个，'
                             f'低于阈值 {min_events}/{min_dates}，只作描述不推断')
    # Coverage counts of blocked entries/exits, for transparency.
    entry_raw_all = px['open'].shift(-1)
    blocked = {}
    for name, sig in signals.items():
        signal = sig.fillna(0).gt(0)
        for h in horizons:
            ret, valid, entry, exit_px, up_next = rets[h]
            raw_signal = signal & entry.notna()
            blocked[f'{name}|{h}'] = {
                'signal_days': int(raw_signal.to_numpy().sum()),
                'limit_up_open': int((raw_signal & up_next.notna() & (entry_raw_all >= up_next - 1e-6)).to_numpy().sum()),
                'missing_window': int((raw_signal & ~valid).to_numpy().sum()),
            }
    return results, {'coverage': coverage, 'blocked': blocked, 'notes': notes,
                     'neutralize': neutralize, 'cost_bps': cost_bps,
                     'entry_rule': 'T+1 开盘买入；T+1+持有日收盘卖出；开盘涨停/无量不成交；收盘跌停不卖出',
                     'return_rule': '复权价；对数流通市值截面回归残差；按交易日聚类标准误；日期自助法置信区间',
                     'sample_notice': '未按市值/行业中性化；未扣交易成本；结论仅为条件性统计，不是交易策略'}


def render(results, meta):
    lines = ['# 全市场事件研究', '']
    cov = meta['coverage']
    lines += [f"- 样本：{cov['days']} 个交易日 × {cov['codes']} 只证券",
              f"- 涨跌停覆盖：{cov['limit_days']} 天，平均每日 {cov['avg_limits_per_day']:.0f} 只",
              f"- 入场规则：{meta['entry_rule']}",
              f"- 统计口径：{meta['return_rule']}",
              f"- 中性化：{'对数流通市值截面回归残差' if meta.get('neutralize') == 'size' else '仅同日全市场去均值'}", '',
              '| 信号 | 持有 | 事件数 | 交易日 | 超额均值 | 净超额 | 95%CI | t | 非重叠t | 胜率 | 状态 |',
              '|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|']
    for r in results:
        if r.get('state') == 'insufficient':
            lines.append(f"| {r['signal']} | {r['horizon']} | {r['n']} | - | - | - | - | - | - | - | 样本不足 |")
            continue
        ci = f"[{r['ci_low_pct']:.2f}%, {r['ci_high_pct']:.2f}%]"
        t = '-' if r['t'] is None else f"{r['t']:.2f}"
        n = r.get('nonoverlap') or {}
        nt = f"{n['t']:.2f}" if n.get('t') is not None and n.get('n_dates', 0) >= 20 else '-'
        lines.append(f"| {r['signal']} | {r['label']} | {r['n']} | {r['n_dates']} | {r['mean_pct']:.2f}% | {r['net_mean_pct']:.2f}% | {ci} | {t} | {nt} | {r['hit_rate']*100:.0f}% | {'可推断' if r['state']=='ok' else '样本偏低'} |")
    mode = '对数流通市值截面回归残差' if meta.get('neutralize') == 'size' else '同日全市场等权去均值'
    cost = meta.get('cost_bps', 0) / 100
    lines += ['', f'> 超额 = {mode}；净超额 = 超额 − {cost:.2f}% 双边成本假设。t 值按交易日聚类；非重叠t 仅在子样本交易日 ≥20 时给出；CI 为按日自助法分位；胜率指超额>0 的比例。',
              '> 未模拟冲击成本、未做行业中性化；这是条件统计，不是已验证策略。']
    if meta['notes']:
        lines += ['', '## 提示'] + [f'- {n}' for n in meta['notes']]
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--target', required=True)
    ap.add_argument('--days', type=int, default=121)
    ap.add_argument('--db', default=str(H.HISTORY_DB))
    ap.add_argument('--signals', default='signal_a,b_confirmed')
    ap.add_argument('--horizons', default='1,5,10')
    ap.add_argument('--min-events', type=int, default=500)
    ap.add_argument('--min-dates', type=int, default=20)
    ap.add_argument('--neutralize', choices=('size', 'none'), default='size')
    ap.add_argument('--cost-bps', type=float, default=18.0)
    ap.add_argument('--out')
    args = ap.parse_args()

    target = iso_date(args.target)
    c = H.connect(args.db)
    try:
        days = [r[0] for r in c.execute(
            'SELECT date FROM calendar WHERE is_open=1 AND date<=? ORDER BY date DESC LIMIT ?',
            (target, args.days))]
        days = list(reversed(days))
        px, up, down, size, loaded_days, codes = load(c, days[0], days[-1])
    finally:
        c.close()
    f = features(px)
    catalog = {
        'signal_a': f['signal_a_classes'].ge(2),
        'signal_a_ma20': f['ma20_cross'],
        'signal_a_shape': f['shape_a'],
        'signal_a_momentum': f['momentum_turn'],
        'b_confirmed': f['b_confirmed'].eq(1),
        'breakout20': f['breakout20'].eq(1),
        'above_ma20': f['above_ma20'].eq(1),
    }
    requested = [s.strip() for s in args.signals.split(',') if s.strip()]
    unknown = set(requested) - set(catalog)
    if unknown:
        raise SystemExit('未知信号: ' + ','.join(sorted(unknown)))
    horizons = [int(h) for h in args.horizons.split(',') if h.strip()]
    results, meta = run(px, up, down, loaded_days, codes, {k: catalog[k] for k in requested}, horizons,
                        size=size, neutralize=args.neutralize, cost_bps=args.cost_bps,
                        min_events=args.min_events, min_dates=args.min_dates)
    meta.update({'target': target, 'days': [loaded_days[0], loaded_days[-1]],
                 'generated_at': now().isoformat(), 'signals': requested, 'horizons': horizons})
    body = {'meta': meta, 'results': results}
    text = json.dumps(body, ensure_ascii=False, indent=2, default=str)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n')
        path.with_suffix('.md').write_text(render(results, meta))
        print(render(results, meta))
        print(f'已写入 {path} 与 {path.with_suffix(".md")}')
    else:
        print(render(results, meta))


if __name__ == '__main__':
    main()
