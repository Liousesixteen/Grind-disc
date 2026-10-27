#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出同花顺自选板块（把盘前股池装进行情软件里实时盯）
================================================================
用途
  看板看方向（板块级聚合），行情软件盯个股（秒级实时）——两者互补。
  本脚本把最新股池导出成同花顺可导入的 txt（每行一个 6 位代码）。

产出（落 watchlists/）
  同花顺导入_核心池_<数据日>.txt        核心+备选全部（盯池）
  同花顺导入_A0重点核心_<数据日>.txt    A0「重点」档题材的核心池（只盯重点）

同花顺导入步骤（PC 客户端）
  ① 自选股页 → 右键 →「批量导入自选股」（或：工具 → 自选股设置 → 导入）；
  ② 选 txt 文件；③ 选择「追加」或「覆盖」。手机端：自选 → 设置 → 从电脑/文件导入。
  ⚠ 不同版本菜单名略有差异；核心是 txt 每行一个 6 位代码。

用法
  .venv/bin/python scripts/export_watch.py                     # 取最新股池
  .venv/bin/python scripts/export_watch.py --pool-dir cache/pool_20261022
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from intraday_scan import latest_pool_dir          # 复用「取最新股池目录」逻辑

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="导出同花顺自选板块 txt（只产清单，不产判断）")
    ap.add_argument("--pool-dir", default=None)
    a = ap.parse_args()
    pool = latest_pool_dir(a.pool_dir)
    if not pool:
        raise SystemExit("找不到股池缓存（cache/pool_*）")
    summ = json.load(open(os.path.join(pool, "summary.json"), encoding="utf-8"))
    rows = json.load(open(os.path.join(pool, "pool_final.json"), encoding="utf-8"))
    data_date = summ.get("data_date", "")
    focus_idx = summ.get("focus_index") or []
    if not focus_idx:
        raise SystemExit("summary.json 无 focus_index（需 pool_pipeline v1.8+）")

    out_dir = os.path.join(ROOT, "watchlists")
    os.makedirs(out_dir, exist_ok=True)

    # ① 核心+备选全部
    core = [r for r in rows if r.get("池层") in ("核心", "备选")]
    f1 = os.path.join(out_dir, f"同花顺导入_核心池_{data_date}.txt")
    with open(f1, "w", encoding="gbk", errors="ignore") as w:   # 同花顺对 gbk txt 兼容最好；本文件只写代码行
        w.write("\n".join(r["code"] for r in core) + "\n")

    # ② A0「重点」档题材的核心池（只盯重点）
    zd_names = {e[1].split("·", 1)[-1] for e in focus_idx if e[0] == "重点"}
    zd_names |= {e[1].split("·", 1)[-1] for e in focus_idx if e[0] == "环节"
                 and "·" in e[1]}                       # 环节条目名 = 链·环节，取后半与题材匹配的交给下面
    core_zd = [r for r in core
               if zd_names & {str(t) for t in (r.get("题材") or [])}
               or any(str(t).split("·", 1)[-1] in zd_names for t in (r.get("题材") or []))]
    f2 = os.path.join(out_dir, f"同花顺导入_A0重点核心_{data_date}.txt")
    with open(f2, "w", encoding="gbk", errors="ignore") as w:
        w.write("\n".join(r["code"] for r in core_zd) + "\n")

    print(f"股池数据日 {data_date}")
    print(f"① 核心+备选全量：{len(core)} 只 → {f1}")
    print(f"② A0重点/环节档的核心池：{len(core_zd)} 只 → {f2}")
    print("导入：同花顺 PC 客户端 → 自选股右键 → 批量导入自选股 → 选 txt（选『追加』）")
    print("⚠ 只产清单不产判断：池层=候选资格，买入仍按熔炼系统层3/层4")


if __name__ == "__main__":
    main()
