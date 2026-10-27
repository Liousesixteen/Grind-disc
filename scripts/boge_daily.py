#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
波哥体系 v3.0 · 每日八仪表读数器

做什么
  读日线数据 → 算《波哥体系-可执行版-v3.0》第三章的八个仪表 → 输出 L1/L2 读数与
  「按决策矩阵映射出的仓位上限」。只产读数与规则映射，不产买卖建议。

不做什么
  · 不算临界点（波哥算法不公开）——用「代理关键位」替代，并明确标注。
  · 不做微浪型人工划分——用 ZigZag 自动分段。
  · 不做浪型命名（扩D/扁担/渐缩）——只给「级别/方向/失效位」。

数据源
  腾讯 fqkline → 新浪 KLineData → 本地缓存（cache/index_kline/<code>.json）
  任一指数取数失败：该仪表显示「缺失」，不做插值、不做推测（3.9 铁律①）。

用法
  python3 scripts/boge_daily.py                # 抓最新并出报告
  python3 scripts/boge_daily.py --date 20261016  # 用缓存里该日的数据出报告
  python3 scripts/boge_daily.py --no-fetch      # 只读缓存
  python3 scripts/boge_daily.py --json          # 额外打印 JSON
"""

import io
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, 'scripts', 'boge_config.json')
OUT_DIR = os.path.join(ROOT, 'cache', 'boge_daily')


# ---------------------------------------------------------------- 基础工具
def load_cfg():
    return json.load(io.open(CFG_PATH, encoding='utf-8'))


def num(x, default=None):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def fetch_tencent(code, n, timeout):
    url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq'
           % (code, n))
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    raw = json.load(urllib.request.urlopen(req, timeout=timeout))
    node = raw.get('data', {}).get(code, {})
    arr = node.get('day') or node.get('qfqday')
    if not arr:
        return None
    bars = []
    for r in arr:
        v = num(r[5], 0.0)
        bars.append({'date': r[0].replace('-', ''), 'open': num(r[1]), 'close': num(r[2]),
                     'high': num(r[3]), 'low': num(r[4]), 'volume': v})
    return bars


def fetch_sina(code, n, timeout):
    url = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d' % (code, n))
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    arr = json.load(urllib.request.urlopen(req, timeout=timeout))
    if not arr:
        return None
    return [{'date': r['day'].replace('-', ''), 'open': num(r['open']), 'close': num(r['close']),
             'high': num(r['high']), 'low': num(r['low']), 'volume': num(r['volume'], 0.0)}
            for r in arr]


def cache_path(cfg, code):
    d = os.path.join(ROOT, cfg['fetch']['cache_dir'])
    return os.path.join(d, code + '.json')


def load_cache(cfg, code):
    p = cache_path(cfg, code)
    if not os.path.isfile(p):
        return None
    try:
        return json.load(io.open(p, encoding='utf-8')).get('bars')
    except (OSError, ValueError):
        return None


def save_cache(cfg, code, bars):
    p = cache_path(cfg, code)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({'code': code, 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'bars': bars},
              io.open(p, 'w', encoding='utf-8'), ensure_ascii=False)


def get_bars(cfg, code, allow_fetch=True):
    """多源回退：腾讯 → 新浪 → 缓存。返回 (bars, source)。"""
    if allow_fetch:
        for src in cfg['fetch']['sources']:
            fn = fetch_tencent if src == 'tencent' else fetch_sina
            try:
                bars = fn(code, cfg['fetch']['datalen'], cfg['fetch']['timeout'])
                if bars and len(bars) > 60:
                    save_cache(cfg, code, bars)
                    return bars, src
            except Exception:
                continue
    bars = load_cache(cfg, code)
    return (bars, 'cache') if bars else (None, '缺失')


# ---------------------------------------------------------------- 指标
def ma(bars, n, idx=-1):
    """收盘价简单均线；idx 为负表示从末尾数（-1＝最后一根）。"""
    end = len(bars) + idx + 1 if idx < 0 else idx + 1
    start = end - n
    if start < 0 or end > len(bars):
        return None
    seg = bars[start:end]
    vals = [b['close'] for b in seg if b['close'] is not None]
    return sum(vals) / n if len(vals) == n else None


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]['high'], bars[i]['low'], bars[i - 1]['close']
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def pct_change(bars, days):
    if len(bars) <= days:
        return None
    a, b = bars[-days - 1]['close'], bars[-1]['close']
    return None if (a in (None, 0) or b is None) else (b / a - 1) * 100


def td_sequence(bars, skip=4, extend=11):
    """TD 序列：与「往前隔 skip-1 根」的收盘价比较。返回 (类型, 计数)。"""
    if len(bars) <= skip:
        return ('无', 0)
    up = 0
    down = 0
    closes = [b['close'] for b in bars]
    for i in range(len(closes) - 1, skip - 1, -1):
        c, ref = closes[i], closes[i - skip]
        if None in (c, ref):
            break
        if c > ref:
            if down:
                break
            up += 1
        elif c < ref:
            if up:
                break
            down += 1
        else:
            break
    if up >= 4:
        return ('高', up)
    if down >= 4:
        return ('低', down)
    return ('无', 0)


def zigzag_points(bars, mult=1.0, lookback=250):
    """用 ATR 倍数为阈值的简易 ZigZag，返回最后若干转折点索引。"""
    a = atr(bars)
    if not a:
        return []
    thr = a * mult
    sub = bars[-lookback:]
    off = len(bars) - len(sub)
    pts = []
    if len(sub) < 3:
        return []
    direction = 0
    ext_i, ext_p = 0, sub[0]['close']
    for i in range(1, len(sub)):
        c = sub[i]['close']
        if c is None:
            continue
        if direction >= 0 and c > ext_p:
            ext_i, ext_p = i, c
        elif direction <= 0 and c < ext_p:
            ext_i, ext_p = i, c
        if direction == 0:
            if c >= sub[0]['close'] + thr:
                pts.append((off + 0, sub[0]['close'], 'L'))
                direction, ext_i, ext_p = 1, i, c
            elif c <= sub[0]['close'] - thr:
                pts.append((off + 0, sub[0]['close'], 'H'))
                direction, ext_i, ext_p = -1, i, c
        elif direction == 1 and c <= ext_p - thr:
            pts.append((off + ext_i, ext_p, 'H'))
            direction, ext_i, ext_p = -1, i, c
        elif direction == -1 and c >= ext_p + thr:
            pts.append((off + ext_i, ext_p, 'L'))
            direction, ext_i, ext_p = 1, i, c
    pts.append((off + ext_i, ext_p, 'H' if direction == 1 else 'L'))
    return pts


def find_gaps(bars, min_pct, max_age, today):
    out = []
    n = len(bars)
    start = max(1, n - max_age)
    for i in range(start, n):
        h0, l0 = bars[i - 1]['high'], bars[i - 1]['low']
        h1, l1 = bars[i]['high'], bars[i]['low']
        if None in (h0, l0, h1, l1):
            continue
        if l1 > h0 and (l1 - h0) / h0 >= min_pct:
            filled = any((bars[j]['low'] or 1e9) <= h0 for j in range(i + 1, n))
            out.append({'date': bars[i]['date'], 'dir': '向上', 'lo': h0, 'hi': l1,
                        'filled': filled, 'age': n - 1 - i})
        elif h1 < l0 and (l0 - h1) / h1 >= min_pct:
            filled = any((bars[j]['high'] or -1) >= l0 for j in range(i + 1, n))
            out.append({'date': bars[i]['date'], 'dir': '向下', 'lo': h1, 'hi': l0,
                        'filled': filled, 'age': n - 1 - i})
    return [g for g in out if not g['filled']]


# ---------------------------------------------------------------- 八仪表
def gauge_rank(cfg, data):
    rows = []
    for name, meta in cfg['indices'].items():
        bars = data.get(meta['code'])
        if not bars:
            rows.append((name, None, None, '缺失'))
            continue
        r1 = pct_change(bars, 1)
        r5 = pct_change(bars, 5)
        rows.append((name, r1, r5, 'ok'))
    known = [r for r in rows if r[1] is not None]
    known.sort(key=lambda x: -(x[1] or -999))
    return rows, known


def gauge_spread(cfg, data):
    sp = cfg['spread']
    fb = data.get(cfg['indices'][sp['fast']]['code']) if sp['fast'] in cfg['indices'] else None
    sb = data.get(cfg['indices'][sp['slow']]['code']) if sp['slow'] in cfg['indices'] else None
    if not fb or not sb:
        return None
    fast = fb[-1]['close']
    slow = sb[-1]['close']
    cur = fast - slow
    d = sp['converge_days']
    if len(fb) > d and len(sb) > d:
        prev = fb[-1 - d]['close'] - sb[-1 - d]['close']
        change = cur - prev
    else:
        prev, change = None, None
    return {'fast': sp['fast'], 'slow': sp['slow'], 'cur': cur,
            'change': change, 'prev': prev,
            'inverted': cur < sp['invert_warn'],
            'extreme': abs(cur) > sp['extreme_abs']}


def gauge_adhesion(cfg, data):
    """均线粘连度：用「8/7 根均线最大间距 ÷ ATR14」，并给出该比值在过去 250 日的分位。
    分位才是可用读数——绝对比值随指数波动尺度变化，不可跨期比较。"""
    bars = data.get(cfg['benchmark'])
    if not bars or len(bars) < 300:
        return None
    a = atr(bars, cfg['adhesion']['atr_window'])
    if not a:
        return None
    res = {}
    for key, periods in cfg['ma_sets'].items():
        def ratio_at(end_idx):
            sub = bars[:end_idx]
            if len(sub) < max(periods) + 1:
                return None
            A = atr(sub, cfg['adhesion']['atr_window'])
            if not A:
                return None
            vals = [ma(sub, p) for p in periods]
            vals = [v for v in vals if v]
            if len(vals) < len(periods) - 2:
                return None
            return (max(vals) - min(vals)) / A
        cur = ratio_at(len(bars))
        if cur is None:
            res[key] = None
            continue
        hist = []
        for k in range(len(bars) - 250, len(bars)):
            r = ratio_at(k)
            if r is not None:
                hist.append(r)
        pct = (sum(1 for h in hist if h <= cur) / len(hist) * 100) if hist else None
        res[key] = {'ratio': cur, 'pct': pct, 'n_hist': len(hist)}
    return res


def gauge_td(cfg, data):
    out = []
    for name, meta in cfg['indices'].items():
        bars = data.get(meta['code'])
        if not bars:
            continue
        kind, cnt = td_sequence(bars, cfg['td']['skip'], cfg['td']['extend'])
        out.append((name, kind, cnt))
    return out


def gauge_gap(cfg, data):
    bars = data.get(cfg['benchmark'])
    if not bars:
        return None
    return find_gaps(bars, cfg['gap']['min_pct'], cfg['gap']['max_age_days'], bars[-1]['date'])


def gauge_time(cfg, data):
    bars = data.get(cfg['benchmark'])
    if not bars:
        return None
    pts = zigzag_points(bars, cfg['zigzag_atr_mult'])
    if not pts:
        return None
    idx, price, kind = pts[-1]
    days = len(bars) - 1 - idx
    tol = cfg['time_code_tolerance']
    hit = [c for c in cfg['time_codes'] if abs(days - c) <= tol]
    return {'days': days, 'kind': kind, 'price': price, 'date': bars[idx]['date'],
            'codes_hit': hit, 'next_codes': [c for c in cfg['time_codes'] if c > days][:3]}


def gauge_volume(cfg, data):
    bars = data.get(cfg['benchmark'])
    if not bars:
        return None
    w = cfg['volume']['avg_window']
    if len(bars) <= w:
        return None
    vols = [b['volume'] for b in bars[-w - 1:-1] if b['volume']]
    if not vols:
        return None
    avg = sum(vols) / len(vols)
    cur = bars[-1]['volume'] or 0
    ratio = cur / avg if avg else None
    chg = (bars[-1]['close'] / bars[-2]['close'] - 1) * 100 if len(bars) > 1 else None
    return {'cur': cur, 'avg': avg, 'ratio': ratio, 'chg': chg,
            'surge': ratio is not None and ratio >= cfg['volume']['ratio_trigger'],
            'stall': ratio is not None and ratio >= cfg['volume']['ratio_trigger']
                     and chg is not None and abs(chg) < 0.3}


def gauge_sector_mv():
    """板块市值排名：从最近一次股池出表（窗口B）聚合。取不到即返回 None（不猜）。"""
    import csv
    import glob
    cands = sorted(glob.glob(os.path.join(ROOT, 'cache', 'pool_*', 'csv', '窗口B-*.csv')))
    if not cands:
        return None
    path = cands[-1]
    try:
        rows = list(csv.DictReader(io.open(path, encoding='utf-8-sig')))
    except (OSError, UnicodeDecodeError):
        return None
    agg = {}
    for r in rows:
        b = r.get('板块') or '-'
        mv = num(r.get('总市值(亿)'), 0.0) or 0.0
        agg[b] = agg.get(b, 0.0) + mv
    top = sorted(agg.items(), key=lambda x: -x[1])[:5]
    return {'source': os.path.relpath(path, ROOT), 'top': top}


# ---------------------------------------------------------------- 判断
def classify_l1(cfg, known):
    """L1 定位：权重市 / 小盘市 / 普涨 / 混沌。
    用 **5 日涨幅** 判风格（当日涨跌噪声太大，且节假日效应明显）。"""
    if not known:
        return '缺失', '指数数据缺失，无法定位'
    big = [r for r in known if cfg['indices'][r[0]]['role'] in ('big',)]
    small = [r for r in known if cfg['indices'][r[0]]['role'] in ('small', 'tail')]
    if not big or not small:
        return '部分缺失', '大小盘各自缺一个，无法比较'
    b = sum(x[2] for x in big if x[2] is not None) / max(1, len([1 for x in big if x[2] is not None]))
    s_ = sum(x[2] for x in small if x[2] is not None) / max(1, len([1 for x in small if x[2] is not None]))
    m = cfg['l1_margin']
    diff = b - s_
    if abs(diff) < m:
        return ('普涨', '5日：权重 %+.2f%% / 小盘 %+.2f%%，同向（差 %.2f）' % (b, s_, diff)) \
            if (b > 0 and s_ > 0) else \
            ('混沌', '5日：权重 %+.2f%% / 小盘 %+.2f%%，同向偏弱（差 %.2f）' % (b, s_, diff))
    if diff >= m:
        return '权重强、小盘弱', '5日：权重 %+.2f%% vs 小盘 %+.2f%%（差 %.2f 个百分点）' % (b, s_, diff)
    return '小盘强、权重弱', '5日：小盘 %+.2f%% vs 权重 %+.2f%%（差 %.2f 个百分点）' % (s_, b, -diff)


def classify_l2(cfg, adh, td, gaps, vol):
    """L2 定性：等变盘 / 趋势启动 / 衰竭 / 派发 / 无信号。"""
    tags = []
    if adh and adh.get('eight') and adh['eight']['pct'] is not None and adh['eight']['pct'] <= cfg['adhesion']['pct_trigger']:
        tags.append('均线粘连')
    if adh and adh.get('seven') and adh['seven']['pct'] is not None and adh['seven']['pct'] <= cfg['adhesion']['pct_trigger']:
        tags.append('7 线粘连')
    hi = sum(1 for _, k, c in td if k == '高' and c >= 9)
    lo = sum(1 for _, k, c in td if k == '低' and c >= 9)
    if hi >= 3:
        tags.append('高9共振×%d' % hi)
    if lo >= 3:
        tags.append('低9共振×%d' % lo)
    if vol and vol.get('stall'):
        tags.append('放量滞涨')
    if vol and vol.get('surge') and not (vol.get('stall')):
        tags.append('放量（%.2f倍）' % vol['ratio'])

    if '放量滞涨' in tags:
        state = '派发'
    elif hi >= 3:
        state = '衰竭（顶部）'
    elif lo >= 3:
        state = '衰竭（底部）'
    elif '均线粘连' in tags or '7 线粘连' in tags:
        state = '等变盘'
    elif any(t.startswith('放量（') for t in tags):
        state = '趋势启动'
    else:
        state = '无明确信号'
    return state, tags, hi, lo


def cap_of(cfg, l1, l2):
    """仓位上限来自 config.cap_matrix（键："{L1}|{L2}"），不在代码里写死。"""
    return cfg.get('cap_matrix', {}).get('%s|%s' % (l1, l2))


# ---------------------------------------------------------------- 报告
def render(cfg, data_date, data, sources, gauges, l1, l1note, l2, tags, cap, extra):
    L = []
    L.append('=' * 74)
    L.append('波哥体系 v3.0 · 每日八仪表读数　数据日期 %s' % data_date)
    L.append('=' * 74)

    L.append('\n【1】七指数梯队（当日涨幅降序）')
    rows, known = gauges['rank']
    for name, r1, r5, st in rows:
        if st == '缺失':
            L.append('   %-8s 缺失（取数失败，未插值）' % name)
        else:
            note = '※代理(中证2000不可得)' if name == '国证2000' else ''
            L.append('   %-8s 当日 %s   5日 %s %s' % (name,
                     ('%+6.2f%%' % r1) if r1 is not None else '  缺失 ',
                     ('%+6.2f%%' % r5) if r5 is not None else '  缺失 ', note))

    sp = gauges['spread']
    L.append('\n【2】%s − %s 价差' % (cfg['spread']['fast'], cfg['spread']['slow']))
    if sp:
        chg = '%+.1f' % sp['change'] if sp['change'] is not None else '缺失'
        L.append('   当前 %+.1f 点；%d 日变化 %s' % (sp['cur'], cfg['spread']['converge_days'], chg))
        L.append('   倒挂：%s　极值：%s' % ('是 ⚠' if sp['inverted'] else '否',
                                          '是 ⚠' if sp['extreme'] else '否'))
    else:
        L.append('   缺失（缺 1000/500 数据）')

    L.append('\n【3】板块总市值前五')
    mv = gauges['sector_mv']
    if mv:
        for i, (b, v) in enumerate(mv['top'], 1):
            L.append('   %d. %-10s %8.0f 亿' % (i, b, v))
        L.append('   （来源 %s；易位需连续 3 个回合才算真换位）' % mv['source'])
    else:
        L.append('   缺失（无股池出表；不猜）')

    adh = gauges['adhesion']
    L.append('\n【4】均线粘连度（最大间距 ÷ ATR14）')
    if adh:
        for k, v in adh.items():
            lab = {'seven': '7 根', 'eight': '8 根'}.get(k, k)
            if not v:
                L.append('   %s：缺失' % lab)
                continue
            pct = v.get('pct')
            flag = ''
            if pct is not None and pct <= cfg['adhesion']['pct_trigger']:
                flag = '← 处于近一年最低 %d%%（粘连）' % cfg['adhesion']['pct_trigger']
            L.append('   %s：比值 %.3f　历史分位 %s %s'
                     % (lab, v['ratio'], ('%.0f%%' % pct) if pct is not None else '缺失', flag))
        L.append('   ⚠ 代理口径：原文未给均线周期组合（BG-H18/H24 缺口）')
    else:
        L.append('   缺失')

    L.append('\n【5】九转序列（隔 3 根口径，≥9 才列）')
    td = gauges['td']
    any9 = [t for t in td if t[2] >= 9]
    L.append('   共振数：高9 %d 个 / 低9 %d 个' % (sum(1 for t in td if t[1] == '高' and t[2] >= 9),
                                                  sum(1 for t in td if t[1] == '低' and t[2] >= 9)))
    for n, k, c in td:
        if c >= 9:
            L.append('   %-8s %s%d' % (n, k, c))
    if not any9:
        L.append('   无（最近序列计数均 <9）')

    L.append('\n【6】未补缺口（基准 %s，近 %d 日）' % (cfg['benchmark'], cfg['gap']['max_age_days']))
    gaps = gauges['gap']
    if gaps is None:
        L.append('   缺失')
    elif not gaps:
        L.append('   无')
    else:
        for g in gaps[:8]:
            L.append('   %s %s 缺口 %.2f–%.2f（%d 日前）' % (g['date'], g['dir'], g['lo'], g['hi'], g['age']))

    tm = gauges['time']
    L.append('\n【7】时间窗（ZigZag 自动分段，不作人工数浪）')
    if tm:
        L.append('   距上一转折（%s %s %.2f）已 %d 个交易日'
                 % (tm['date'], tm['kind'], tm['price'], tm['days']))
        if tm['codes_hit']:
            L.append('   ⚠ 落在时间密码 %s（±%d）' % (tm['codes_hit'], cfg['time_code_tolerance']))
        else:
            L.append('   未落在密码；下一档候选 %s' % tm['next_codes'])
    else:
        L.append('   缺失')

    vol = gauges['volume']
    L.append('\n【8】量能（基准指数成交量口径）')
    if vol:
        L.append('   当日 %.3e（原始单位） / 20 日均 %.3e → %.2f 倍'
                 % (vol['cur'], vol['avg'], vol['ratio']))
        if vol['surge']:
            L.append('   ⚠ 放量（≥%.2f 倍）%s' % (cfg['volume']['ratio_trigger'],
                                                 '，且滞涨 → 派发警报' if vol['stall'] else ''))
    else:
        L.append('   缺失')

    L.append('\n' + '-' * 74)
    L.append('【L1 定位】%s' % l1)
    L.append('           %s' % l1note)
    L.append('【L2 定性】%s　标签：%s' % (l2, '、'.join(tags) if tags else '无'))
    L.append('【决策矩阵】按 v3.0 第五章映射 → 当日仓位上限 %s 成' % ('0' if cap is None else cap))
    L.append('           （规则映射，非买卖建议；硬约束：单方向 / 空间不足不出手 / 左侧只做 ETF）')

    if extra['missing']:
        L.append('\n⚠ 缺失项（留空不补）：%s' % '、'.join(extra['missing']))
    L.append('【数据源】%s' % '，'.join('%s=%s' % (k, v) for k, v in sources.items()))
    L.append('【声明】所有阈值均为 C 级草案，待回测标定；本输出只产读数，不构成投资建议。')
    return '\n'.join(L)


def main():
    cfg = load_cfg()
    argv = sys.argv[1:]
    want_date = None
    allow_fetch = '--no-fetch' not in argv
    if '--date' in argv:
        want_date = argv[argv.index('--date') + 1]
        allow_fetch = False

    codes = [m['code'] for m in cfg['indices'].values()] + [cfg['benchmark']]
    data, sources, missing = {}, {}, []
    for c in dict.fromkeys(codes):
        bars, src = get_bars(cfg, c, allow_fetch)
        if not bars:
            missing.append(c)
            sources[c] = '缺失'
            continue
        if want_date:
            bars = [b for b in bars if b['date'] <= want_date]
            if not bars:
                missing.append(c)
                sources[c] = '无该日数据'
                continue
        for b in bars:
            b['pct'] = None
        data[c] = bars
        sources[c] = src

    if not data:
        print('❌ 全部取数失败，无法出报告。请检查网络或先用 --no-fetch 配合本地缓存。')
        return 1

    # 统一数据日期 = 基准指数最后一根
    base = data.get(cfg['benchmark']) or list(data.values())[0]
    data_date = base[-1]['date']

    gauges = {
        'rank': gauge_rank(cfg, data),
        'spread': gauge_spread(cfg, data),
        'sector_mv': gauge_sector_mv(),
        'adhesion': gauge_adhesion(cfg, data),
        'td': gauge_td(cfg, data),
        'gap': gauge_gap(cfg, data),
        'time': gauge_time(cfg, data),
        'volume': gauge_volume(cfg, data),
    }
    l1, l1note = classify_l1(cfg, gauges['rank'][1])
    l2, tags, hi, lo = classify_l2(cfg, gauges['adhesion'], gauges['td'],
                                   gauges['gap'], gauges['volume'])
    cap = cap_of(cfg, l1, l2)

    print(render(cfg, data_date, data, sources, gauges, l1, l1note, l2, tags, cap,
                 {'missing': missing}))

    os.makedirs(OUT_DIR, exist_ok=True)
    out = {'data_date': data_date, 'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
           'cfg_version': cfg['version'], 'sources': sources, 'missing': missing,
           'l1': l1, 'l1_note': l1note, 'l2': l2, 'tags': tags, 'cap': cap,
           'gauges': {
               'rank': gauges['rank'][0],
               'spread': gauges['spread'],
               'sector_mv': gauges['sector_mv'],
               'adhesion': gauges['adhesion'],
               'td': gauges['td'],
               'gap': gauges['gap'],
               'time': gauges['time'],
               'volume': gauges['volume']}}
    p = os.path.join(OUT_DIR, data_date + '.json')
    json.dump(out, io.open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n已存 %s' % os.path.relpath(p, ROOT))
    if '--json' in argv:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
