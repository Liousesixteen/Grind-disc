#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把实时看板的某一刻 state 存成**自包含静态 HTML**（可发出去看的那一张）
================================================================
为什么单独一个脚本
  看板本体（intraday_board.py）是常驻服务：页面靠 fetch('/api/state') 轮询。
  想给别人/给自己事后看某一刻的样子，那条 fetch 一定会失败（服务已关），
  页面只剩「连接失败」。所以要把那一刻的 state 直接焊进页面。

做法（只动三处，渲染代码一个字不改 —— 保证「快照＝当时真实渲染」）
  1) fetch('/api/state')  →  window.__STATE
  2) tick(); setInterval(tick,15000)  →  tick();   （静态页不轮询）
  3) 顶部加一条「静态快照」横幅，写明 taken_at / 数据日 / 只读

用法
  .venv/bin/python scripts/board_snapshot.py \
      --state cache/_replay/state6082.json \
      --out "reports/2026-10-24/看板快照-20261024-2029分组配色.html"
  # 不给 --out 时按 taken_at 自动命名到 reports/<日期>/ 下
"""
import argparse, json, os, re, sys, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import intraday_board as B


BANNER = """<div id="snapbar">静态快照 · {ts} · 数据日 {dd} · 只读（不轮询、不可交互刷新）</div>
<style>
#snapbar{{background:#3a2c12;border:1px solid #b08d3a;color:#ffe9c2;
  font-size:12.5px;padding:6px 10px;border-radius:5px;margin:0 0 12px}}
body{{padding-top:0}}
</style>
"""


def main():
    ap = argparse.ArgumentParser(description="实时看板 → 自包含静态快照")
    ap.add_argument("--state", required=True, help="state JSON（/api/state 的落盘）")
    ap.add_argument("--out", default="", help="输出 HTML 路径")
    ap.add_argument("--board", default="intraday_board",
                    help="页面模板来源模块（默认 intraday_board；测新版可传 intraday_board_v2）")
    a = ap.parse_args()

    st = json.load(open(a.state, encoding="utf-8"))
    if st.get("booting"):
        raise SystemExit("这份 state 还是 booting，没有内容可快照")
    ts = st.get("taken_at") or "?"
    dd = st.get("data_date") or "?"

    import importlib
    Bm = importlib.import_module(a.board) if a.board != "intraday_board" else B
    page = Bm.PAGE

    # ── 1) 断掉轮询 ──────────────────────────────────────────────
    old_fetch = "const d=await (await fetch('/api/state?ts='+Date.now(), {cache:'no-store'})).json();"
    if old_fetch not in page:
        # 兜底：正则宽松匹配（以后改了参数名也不会静默失败）
        page, n = re.subn(r"const d=await \(await fetch\('/api/state[^)]*\)\)\.json\(\);",
                          "const d=window.__STATE;", page)
        if n != 1:
            raise SystemExit(f"没找到 fetch('/api/state') 那一行（命中 {n} 次）——模板改过了，先看 intraday_board.py")
    else:
        page = page.replace(old_fetch, "const d=window.__STATE;")

    old_loop = "tick(); setInterval(tick, 15000);"
    if old_loop not in page:
        raise SystemExit("没找到 setInterval 那一行——模板改过了")
    page = page.replace(old_loop, "tick();   /* 静态快照：不轮询 */")

    # ── 2) 把 state 焊进去（</script> 必须先转义，否则提前闭合）────────
    blob = json.dumps(st, ensure_ascii=False).replace("</", "<\\/")
    inject = f"<script>window.__STATE={blob};</script>\n"

    # 注在**主 script 之前**：主 script 末尾会立刻 tick()，state 必须已就位
    anchor = "<script>"
    i = page.index(anchor)
    page = page[:i] + inject + page[i:]

    # ── 3) 横幅（插在 <body> 之后、第一段内容之前）──────────────────
    m = re.search(r"<body[^>]*>", page)
    if m:
        page = page[:m.end()] + "\n" + BANNER.format(ts=ts, dd=dd) + page[m.end():]
    else:
        page = BANNER.format(ts=ts, dd=dd) + page

    # ── 4) 标题也带上时间，避免两个标签页分不清 ────────────────────
    page = page.replace("<title>盘中实时看板</title>",
                        f"<title>盘中看板快照 {ts}</title>", 1)

    out = a.out
    if not out:
        d8 = (st.get("scan_date") or dt.date.today().isoformat()).replace("-", "")
        hhmm = ts.split(" ")[-1].replace(":", "")[:4] if " " in ts else "0000"
        out = os.path.join(ROOT, "reports", f"{st.get('scan_date') or dt.date.today().isoformat()}",
                           f"看板快照-{d8}-{hhmm}.html")
    out = out if os.path.isabs(out) else os.path.join(ROOT, out)

    # 已存在就拒绝覆盖（项目铁律：动已交付文件先备份）
    if os.path.exists(out):
        raise SystemExit(f"目标已存在，不覆盖：{out}\n  换个 --out，或先手动备份")

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    print(f"已写 {out}")
    print(f"  {len(page):,} 字节 · taken_at {ts} · pankou {len(st.get('pankou') or [])} 只 "
          f"· catchup {len(st.get('catchup') or [])} 只")
    print(f"  自检：__STATE {'✓' if 'window.__STATE=' in page else '✗'} · "
          f"无 fetch {'✓' if "fetch('/api/state" not in page else '✗'} · "
          f"无 setInterval(tick {'✓' if 'setInterval(tick' not in page else '✗'}")


if __name__ == "__main__":
    main()
