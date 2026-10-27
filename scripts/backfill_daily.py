# -*- coding: utf-8 -*-
"""日线回补：给 kline_metrics.json 补上「最近 N 个交易日的日涨跌幅」序列
================================================================
为什么需要
  只存聚合指标（涨停次数 / 红肥绿瘦 / 20日涨幅）时，**算不出任何「多日」的东西**：
  - 题材 5 日涨幅（外部榜单匹配不上的 317 个题材因此完全没有 5 日数据）
  - 连续跑赢天数（判断「连续性」的真正依据，而不是拿当日 vs 5日 的绝对正负凑）
  - 历史台账回补（连续在榜天数）

做法
  对 kline_metrics.json 里的每只票，拉最近 (N+1) 根日线，算最近 N 个交易日的日涨跌幅，
  写进新字段 p5（列表，**最新在末尾**）与 ret5（N 日累计涨幅%）。
  原始字段一个不动，只增不改；写回前自动备份。

用法
  python scripts/backfill_daily.py                      # 默认 N=5，12 并发
  python scripts/backfill_daily.py --days 5 --workers 12 --cache-dir cache/pool_20261017
  python scripts/backfill_daily.py --dry-run            # 只测速与成功率，不写文件

原则
  取不到就留空，绝不编造（DATA_SOURCE_POLICY）；回退链 腾讯 → 新浪 → 东财。
"""
import argparse, json, os, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_pipeline as P          # 复用数据源与缓存路径

ROOT = P.ROOT


def pct_series(bars, n):
    """bars: [[day, open, close, high, low, vol], ...] 升序。返回最近 n 个交易日的日涨跌幅%。"""
    cl = []
    for b in bars:
        try:
            cl.append(float(b[2]))
        except (TypeError, ValueError, IndexError):
            cl.append(None)
    cl = [c for c in cl if c]
    if len(cl) < n + 1:
        return None
    out = []
    for i in range(len(cl) - n, len(cl)):
        p = cl[i - 1]
        out.append(round((cl[i] / p - 1) * 100, 2) if p else None)
    return out


def prev_info(bars):
    """bars 升序 [day,open,close,high,low,vol]；取倒数第二根＝上一交易日的 high/low/vol。
    用途（2026-10-23 肉肉 4.1/4.2 熔入）：intraday 的「破昨高」判据与「量能进度」。"""
    if not bars or len(bars) < 2:
        return None
    b = bars[-2]
    try:
        return {"prev_high": float(b[3]), "prev_low": float(b[4]), "prev_vol": float(b[5])}
    except (TypeError, ValueError, IndexError):
        return None


def one(code, n):
    for src in ("tencent", "sina", "eastmoney_push2his"):
        bars = P.src_kline(src, code, n + 6, min_bars=2)   # 回补只需 n+6 根，不能被 25 根校验挡死
        s = pct_series(bars, n) if bars else None
        if s and all(x is not None for x in s):
            return s, prev_info(bars)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    d = a.cache_dir or os.path.join(ROOT, "cache", sorted(
        [x for x in os.listdir(os.path.join(ROOT, "cache"))
         if x.startswith("pool_") and os.path.isdir(os.path.join(ROOT, "cache", x))],
        reverse=True)[0])
    # ⚠ 必须过滤目录：cache/ 下还有 pool_runs.csv（台账文件）同以 pool_ 开头，
    #   不过滤会选中它然后 json.load 崩溃（2026-10-18 发现）
    if not os.path.isabs(d):
        d = os.path.join(ROOT, d)
    p = os.path.join(d, "kline_metrics.json")
    if not os.path.exists(p):
        raise SystemExit(f"找不到 {p}")
    km = json.load(open(p, encoding="utf-8"))
    codes = list(km)
    print(f"日线回补：{len(codes)} 只，N={a.days}，并发 {a.workers}（源 {p}）")

    t0 = time.time()
    got, done = {}, 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for c, (s, pv) in zip(codes, ex.map(lambda c: one(c, a.days), codes)):
            done += 1
            if s:
                got[c] = (s, pv)
            if done % 500 == 0:
                print(f"  {done}/{len(codes)}  成功 {len(got)}  {time.time()-t0:.0f}s")
    print(f"完成：成功 {len(got)}/{len(codes)}（{len(got)/max(len(codes),1)*100:.1f}%）用时 {time.time()-t0:.0f}s")

    if a.dry_run:
        print("--dry-run：不写文件")
        return

    bak = p.replace(".json", f".bak{time.strftime('%Y%m%d_%H%M')}.json")
    shutil.copy(p, bak)
    print(f"已备份 {os.path.basename(bak)}")

    for c, (s, pv) in got.items():
        km[c]["p5" if a.days == 5 else f"p{a.days}"] = s
        km[c]["ret5" if a.days == 5 else f"ret{a.days}"] = round(
            (1 + s[0] / 100) * (1 + s[1] / 100) * (1 + s[2] / 100) *
            (1 + s[3] / 100) * (1 + s[4] / 100) * 100 - 100, 2) if a.days == 5 else None
        if pv:
            km[c].update(pv)          # prev_high / prev_low / prev_vol（昨日）
    json.dump(km, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    miss = len(codes) - len(got)
    print(f"已写回 {p}（补上 {len(got)} 只，仍缺 {miss} 只——缺失者字段留空，不编造）")


if __name__ == "__main__":
    main()
