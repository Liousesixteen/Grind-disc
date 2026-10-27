#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中实时看板（常驻本地服务 · 手动启动 / Ctrl-C 停止）
================================================================
定位
  intraday_scan.py 的「实时形态」：同一套计算（compute_readings，口径唯一定义处），
  从「手动跑一次出一次」变成「打开浏览器就自动刷新」。只产读数不产判断；下单仍以行情软件为准。

边界（必读）
  1. **不是系统定时任务**——盘中你手动启动、收盘 Ctrl-C 关闭；不设开机自启、不后台残留。
  2. 快照层缓存：页面刷新再频繁，出网拉取也只有每 interval 一次（默认 15s，2026-10-23 用户定；限流保护靠指数退避）；
     连续失败指数退避（60s→120s→…上限 10 分钟），期间页面显示上次成功读数 + 降级标注。
  3. 四时点正式快照**照常落档**（窗口内首次刷新时写 cache/intraday/<日期>/<HHMM>.json，
     与 intraday_scan 同格式）——高频是「流」，四时点是「档」，复盘对照不丢。
  4. 页面含内联 JS（fetch 轮询）——静态报告「无内联 JS」约定不适用于本页：
     实时页必须轮询，此为唯一例外，勿照搬到 reports/。

用法
  .venv/bin/python scripts/intraday_board.py                 # 默认 6060 端口 / 60s 刷新
  .venv/bin/python scripts/intraday_board.py --interval 30 --port 8080
  打开 http://127.0.0.1:6060 即看板；Ctrl-C 停止。
"""
import argparse, datetime as dt, json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool_pipeline as P
import theme_attr as TA
import theme_universe as TU
import stock_profile as SP
from intraday_scan import (SLOTS, compute_readings, latest_pool_dir, load_kprev, med,
                           load_pre_focus, slot_of, take_indices, take_pools,
                           take_snapshot, take_boards)

ROOT = P.ROOT


class Board:
    """快照缓存 + 计算 + 时点落档。线程安全靠 GIL + 单写线程（只有 refresher 改 state）。"""

    def __init__(self, interval):
        self.interval = max(int(interval), 10)   # 2026-10-23 用户定 15s；下限放宽到 10s
        self.state = {"booting": True, "warn": ["启动中：正在拉取第一份快照…"]}
        self.lock = threading.Lock()
        self.logged_slots = set()          # 今日已落档的标准时点
        cfg = json.load(open(P.CFG_PATH, encoding="utf-8"))
        pool = latest_pool_dir(None)
        if not pool:
            raise SystemExit("找不到股池缓存（cache/pool_*），先跑 pool_pipeline.py")
        summ = json.load(open(os.path.join(pool, "summary.json"), encoding="utf-8"))
        rows = json.load(open(os.path.join(pool, "pool_final.json"), encoding="utf-8"))
        if not summ.get("focus_index"):
            raise SystemExit("summary.json 无 focus_index（需 pool_pipeline v1.8+）")
        self.cfg, self.rows = cfg, rows
        self.focus_idx = summ["focus_index"]
        self.data_date = summ.get("data_date", "")
        # ── 个股画像（2026-10-24，与 profile_report.py 同一套静态层/动态层）──
        # 启明星辰为什么必须显示「网络安全」而不是「AI应用链·人工智能」：
        # 身份来自主营构成，归因来自当日题材强度打分，二者分开显示，错位度标出情绪票。
        self.pool = pool
        # 个股大表（2026-10-25）：概念覆盖源。大表缺失 → load_tag_map 自动回退 themes.json
        self.uni = TU.load_universe()
        self.con, self.ind, self.ind1 = TA.load_tag_map(pool, cfg, universe=self.uni)
        self.big = TA.big_label_set(cfg)
        self.profiles = {}
        self._prof_threads = set()
        self._prof_fail = {}                 # code → 上次失败时刻（失败不能一棍子打死，要能重试）
        self.prof_note = "未加载"
        self._preload_profiles()
        # 盘前档案（昨夜 xlsx 静态读数）——并入每条焦点，看板与 reports 报告信息对齐
        self.pre = load_pre_focus(self.data_date)
        self.kprev = load_kprev(pool)
        # ── 股池自动更新状态（2026-10-23 用户需求）──
        # 四时点窗口内自动重出「盘中股池」；收盘后自动跑一次「最终股池」。
        # 已在盘前跑过正式股池的日子，final 视为已完成，不重复跑。
        self.pool_jobs = set()
        self.pool_log = []
        today = dt.date.today().strftime("%Y%m%d")
        self.final_done = os.path.exists(
            os.path.join(ROOT, "watchlists", f"233战法盘前股池_{today}.xlsx"))
        self.final_note = "已完成（开盘前已有正式股池）" if self.final_done else "待收盘后自动跑"
        print(f"股池: cache/{os.path.basename(pool)}（数据日 {self.data_date}，"
              f"焦点 {len(self.focus_idx)} 条，盘前档案 {len(self.pre)} 条）")

    def _spawn(self, cmds, label):
        """后台串行跑命令（不阻塞看板刷新）。cmds: [[脚本名, 参数...], ...]"""
        def w():
            t0 = time.time()
            ok = True
            for c in cmds:
                r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", c[0])] + c[1:],
                                   cwd=ROOT, capture_output=True, text=True)
                ok = ok and r.returncode == 0
                if r.returncode != 0:
                    print(f"[{dt.datetime.now():%H:%M:%S}] {label} 失败："
                          f"{(r.stderr or r.stdout or '')[-300:]}")
            rec = {"label": label, "at": dt.datetime.now().strftime("%H:%M"),
                   "ok": ok, "sec": round(time.time() - t0)}
            self.pool_log.append(rec)
            print(f"[{rec['at']}] {label} {'完成' if ok else '失败'}（{rec['sec']}s）")
        threading.Thread(target=w, daemon=True).start()

    def maybe_update_pool(self, now):
        """四时点窗口内：自动更新该时点的盘中股池；收盘后：跑一次最终股池。"""
        m = now.hour * 60 + now.minute
        if now.weekday() < 5:
            for slot, _, (a, b) in SLOTS:
                if a - 5 <= m <= b and slot not in self.pool_jobs:
                    self.pool_jobs.add(slot)
                    self._spawn([["pool_pipeline.py", "--intraday"]], f"盘中股池·{slot}")
                    break
            if m >= 15 * 60 + 5 and not self.final_done:
                self.final_done = True
                self.final_note = "运行中（全量→回补→重出，约 5 分钟）"
                self._spawn([["pool_pipeline.py"], ["backfill_daily.py"],
                             ["pool_pipeline.py", "--reuse"]], "最终股池·收盘后")
                self.final_note = "已触发（全量→回补→重出）"

    # ══════════ 个股画像：静态层（主营）+ 动态层（当日归因） ══════════
    def _preload_profiles(self):
        """启动时先把「今天会出现在表里的票」的画像读/抓进来。

        分两段：① 表里出现的票（涨停池/候选/补涨）**同步**载入——画像是身份不是行情，
        第一屏若空着，启明星辰又会退回按概念排序的『IPv6』，正是这次要修掉的毛病；
        ② 其余池内票丢后台慢慢补，不阻塞启动。
        """
        show = set()
        d = os.path.join(ROOT, "cache", "intraday", dt.date.today().strftime("%Y%m%d"))
        if os.path.isdir(d):
            fs = sorted(f for f in os.listdir(d) if f.endswith(".json"))
            if fs:
                try:
                    last = json.load(open(os.path.join(d, fs[-1]), encoding="utf-8"))
                    for b in ("pankou", "candidates", "catchup", "movers", "lurkers"):
                        show.update(x.get("code") for x in (last.get(b) or []) if x.get("code"))
                except Exception:
                    pass
        rest = {r.get("code") for r in (self.rows or [])
                if r.get("池层") in ("核心", "备选")} - show
        try:
            if show:
                got = SP.load(sorted(show), workers=8, verbose=False)
                self.profiles.update({k: v for k, v in got.items() if v})
                self.prof_note = f"{len(self.profiles)} 只"
                print(f"画像: 表内 {len(self.profiles)}/{len(show)} 只已载入")
            if rest:
                self._ensure_profiles(sorted(rest))       # 后台补，不阻塞
        except Exception as e:
            print(f"画像预载失败（不影响行情读数）：{e}")

    def _ensure_profiles(self, codes, block=False, retry_after=300):
        """补抓缺失的主营画像。默认放后台线程，不阻塞 15s 刷新；
        抓回来之前该票显示「—」，下一次刷新自动补上。

        ⚠ 2026-10-24 修：原实现把抓不到的票记成 `profiles[c]=None` 并**永不重试**，
        一次瞬时失败就整场会话钉死一只票（实测密尔克卫：缓存里 15:18 就有数据，
        看板 16:31 起来后一直显示「核心业务 — / 主业未归链」）。
        现在失败只记时间戳，冷却 `retry_after` 秒后再试；已有的 None 也会被重新纳入。
        """
        now = time.time()
        todo = []
        for c in codes:
            if not c or c in self._prof_threads:
                continue
            if c not in self.profiles:
                todo.append(c)
            elif self.profiles[c] is None:
                t = self._prof_fail.get(c)
                if t is None:
                    self._prof_fail[c] = now
                elif now - t >= retry_after:
                    todo.append(c)

        def w():
            got = SP.load(todo, workers=8, verbose=False)
            self.profiles.update({k: v for k, v in got.items() if v})
            for c in todo:
                self._prof_threads.discard(c)
                if c not in self.profiles or self.profiles.get(c) is None:
                    if c not in got:
                        self.profiles[c] = None      # 本轮仍无 → 记空，但保留重试机会
                        self._prof_fail[c] = time.time()
            self.prof_note = f"{len(self.profiles)} 只"

        if not todo:
            return
        if block:
            got = SP.load(todo, workers=8, verbose=False)
            self.profiles.update(got)
        else:
            self._prof_threads.update(todo)
            threading.Thread(target=w, daemon=True).start()

    def _attr_main(self, codes, stats):
        """{code: {theme, score, layer, chain}}——「主归属让位」用的归因摘要（只算涨停池）。
        与 _attach_profiles 同一套静态层（SP.main_seg）＋动态层（TA.attribute），
        但只取主归属规则需要的四个字段；单票失败跳过（不让一只票拖垮整轮刷新）。"""
        out = {}
        cof = lambda t: P.chain_of(t, self.cfg["chains"])
        for c in codes:
            if not c:
                continue
            try:
                p = self.profiles.get(c)
                if p is None:
                    pth = SP._path(c)
                    if os.path.exists(pth):
                        p = json.load(open(pth, encoding="utf-8"))
                        self.profiles[c] = p
                mc, ms, ev = SP.main_seg(p, self.cfg["chains"], self.ind.get(c, []),
                                         industry_to_chain=self.cfg.get("industry_to_chain"))
                approx = ev.startswith(("行业:", "关键词:"))
                a = TA.attribute(self.con.get(c, []), stats, cof, {}, mc, ms,
                                 self.ind.get(c, []), self.big, main_approx=approx)
                m = a.get("main")
                if m:
                    out[c] = {"theme": m.get("theme"), "score": m.get("score"),
                              "layer": a.get("layer"), "chain": m.get("chain") or ""}
            except Exception:
                continue
        return out

    def _attach_profiles(self, R, snap):
        """把画像 + 归因写进 pankou / candidates / catchup 的每一行，并单独给前端一份 prof 表。"""
        IC = self.cfg.get("intraday", {})
        buckets = ("pankou", "candidates", "catchup", "movers", "lurkers")
        codes = {x.get("code") for b in buckets for x in (R.get(b) or []) if x.get("code")}
        # 缺画像的，先直接读磁盘（缓存命中就立刻补上，不必等网络/后台线程）
        for c in codes:
            if not self.profiles.get(c):
                pth = SP._path(c)
                if os.path.exists(pth):
                    try:
                        self.profiles[c] = json.load(open(pth, encoding="utf-8"))
                        self._prof_fail.pop(c, None)
                    except Exception:
                        pass
        self._ensure_profiles(codes)
        med = R.get("mkt_med")
        stats = TA.build_theme_stats(snap, self.con, med, IC.get("zt_threshold", 9.7))
        prof_out = {}
        for b in buckets:
            for x in (R.get(b) or []):
                c = x.get("code")
                if not c:
                    continue
                p = self.profiles.get(c)
                mc, ms, ev = SP.main_seg(p, self.cfg["chains"], self.ind.get(c, []),
                                         industry_to_chain=self.cfg.get("industry_to_chain"))
                # 主业是「推的」还是「算的」：行业兜底 / 材料化工关键词兜底都算推的，不能当成确定身份
                # （经营范围兜底 2026-10-24 已删——工商登记文本写的是「可以做什么」，不是「靠什么赚钱」）
                approx = ev.startswith(("行业:", "关键词:"))
                a = TA.attribute(self.con.get(c, []), stats,
                                 lambda t: P.chain_of(t, self.cfg["chains"]),
                                 {}, mc, ms, self.ind.get(c, []), self.big,
                                 main_approx=approx)
                main = a.get("main")
                rec = {"core": SP.brief(p),
                       "core_asof": (p or {}).get("asof", ""),
                       "core_basis": (p or {}).get("basis", ""),
                       "core_note": (p or {}).get("note", ""),
                       # 「未归链」有两种：没查过 / 查过并确认 19 链覆盖不到（override 写空串）。
                       # 后者是结论，必须和前者分开显示，否则看起来像识别失败。
                       "biz": (f'{"≈" if approx else ""}{mc}'
                               + (f'·{ms}' if ms else "")) if mc
                              else ("未归链（已核实）" if ev.startswith("人工:") else "未归链"),
                       "biz_chain": mc, "biz_seg": ms, "biz_approx": approx,
                       "ind": self.ind1.get(c, ""),
                       "theme": main["theme"] if main else "",
                       "score": main["score"] if main else None,
                       "chain": (f'{main["chain"]}·{main["seg"] or "—"}' if main and main["chain"] else ""),
                       "chain_only": (main["chain"] if main else ""),
                       "layer": a.get("layer", ""),
                       "approx": a.get("approx", False),
                       "alts": "、".join(f'{y["theme"]}({y["score"]})' for y in a.get("alts") or []),
                       "ev": ev}
                x["画像"] = rec
                prof_out[c] = rec
        return prof_out

    def refresh(self):
        """拉快照 → 计算 → 存 state →（若在时点窗口内）落正式档。失败时保留旧 state。"""
        try:
            self.maybe_update_pool(dt.datetime.now())     # 四时点/收盘后的股池自动更新
            snap, src, warn, _sim = take_snapshot(False, "")
            if not snap:
                raise RuntimeError("快照源全部失败")
            boards, bwarn = take_boards(self.cfg, False)
            now = dt.datetime.now()
            pools = take_pools(now.strftime("%Y%m%d"), False)
            indices = take_indices(False)
            # 行业名全集（主归属候选剔行业名）：self.ind 是 load_tag_map 的三级名并集
            _ind = {x for v in (self.ind or {}).values() for x in (v or []) if x}
            # 归因先行（2026-10-25 主业让位规则）：主归属要在「归因确认主业驱动 ≥55」时跟归因走，
            # 所以归因必须算在主归属之前。只算涨停池里的票（主归属只对它们建桶）。
            med0 = med([r.get("pct") for r in snap if r.get("pct") is not None])
            _stats = TA.build_theme_stats(snap, self.con, med0,
                                          self.cfg.get("intraday", {}).get("zt_threshold", 9.7))
            _zt_codes = sorted({z.get("c") for z in (pools[0] if pools else []) if z.get("c")})
            attr_main = self._attr_main(_zt_codes, _stats)
            R = compute_readings(snap, boards, self.rows, self.focus_idx, self.cfg, src,
                                 pools=pools, kprev=self.kprev, ind_names=_ind,
                                 attr_main=attr_main, uni_con=TU.concept_map())
            hhmm = now.hour * 60 + now.minute
            slot, hint, standard = slot_of(hhmm)
            warn = warn + bwarn + R["warns"]
            # 盘前档案并入焦点条目（看板与 reports 报告信息对齐）；顺带列出今日已落档时点
            day = now.strftime("%Y%m%d")
            arch_dir = os.path.join(ROOT, "cache", "intraday", day)
            archived = sorted(f[:-5] for f in os.listdir(arch_dir)
                              if f.endswith(".json")) if os.path.isdir(arch_dir) else []
            for f in R["focus"]:
                p = self.pre.get(f["name"]) or self.pre.get(f["name"].split("·", 1)[-1]) or {}
                f["pre"] = {k: p.get(k) for k in ("pre_rank", "pre_zljlr", "pre_ret5",
                                                  "pre_run", "pre_cont", "pre_lc")}
            prof_out = self._attach_profiles(R, snap)     # 个股画像 + 当日归因（2026-10-24）
            out = {"slot": slot, "clock_hint": hint, "standard": standard,
                   "taken_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                   "scan_date": day, "data_date": self.data_date,
                   "src": src, "simulated": False, "n_snap": len(snap),
                   "mkt_med_pct": R["mkt_med"], "archived": archived,
                   "alignment": R["alignment"], "warn": warn,
                   "sentiment": R["sentiment"], "ladder": R["ladder"], "pankou": R["pankou"],
                   "indices": indices, "catchup": R.get("catchup", []),
                   "pool_log": self.pool_log[-6:], "final_note": self.final_note,
                   "focus": R["focus"], "movers": R["movers"], "lurkers": R["lurkers"],
                   "candidates": R.get("candidates", []),
                   "prof": prof_out, "prof_note": self.prof_note}
            with self.lock:
                self.state = out
            # ── 四时点正式落档：窗口内第一次刷新时写一份（复盘档案）──
            if standard and slot not in self.logged_slots:
                d = os.path.join(ROOT, "cache", "intraday", day)
                os.makedirs(d, exist_ok=True)
                jf = os.path.join(d, f'{now.strftime("%H%M")}.json')
                json.dump(out, open(jf, "w", encoding="utf-8"), ensure_ascii=False)
                self.logged_slots.add(slot)
                print(f"[{now:%H:%M:%S}] 落档 {jf}（{slot} 标准时点）")
            print(f"[{now:%H:%M:%S}] 刷新成功 · 源 {src} · {len(snap)} 只 · "
                  f"市场中位 {R['mkt_med']}%")
            return True
        except Exception as e:
            with self.lock:
                self.state.setdefault("warn", []).append(f"⚠ 本次刷新失败（保留上次读数）：{e}")
            print(f"[{dt.datetime.now():%H:%M:%S}] 刷新失败：{e}")
            return False

    def effective_interval(self, now=None):
        """分时段刷新（2026-10-23 用户定）：交易时段用设定频率（15s），
        收盘后/午休/周末自动降到 ≥10 分钟——收盘后不需要每 15 秒刷，别浪费拉取。"""
        now = now or dt.datetime.now()
        m = now.hour * 60 + now.minute
        trading = now.weekday() < 5 and (555 <= m < 690 or 780 <= m < 900)
        return self.interval if trading else max(self.interval, 600)

    def loop(self):
        backoff = self.interval
        while True:
            ok = self.refresh()
            base = self.effective_interval()
            backoff = base if ok else min(backoff * 2, 600)
            time.sleep(backoff)

    def get_state(self):
        with self.lock:
            return self.state


PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>盘中实时看板</title>
<style>
body{font-family:-apple-system,sans-serif;margin:18px;background:#1b1b1f;color:#e4e4e8}
h1{font-size:19px;margin:4px 0} h2{font-size:15px;margin:22px 0 8px;color:#c9c9d2}
.meta{color:#8a8a95;font-size:12.5px;margin:2px 0}
table{border-collapse:collapse;font-size:12.5px;width:100%}
td,th{border:1px solid #3a3a42;padding:3px 7px;text-align:left}
th{background:#26262c;color:#b8b8c2;font-weight:500}
.tag{display:inline-block;font-size:10.5px;padding:0 5px;border-radius:4px;color:#fff;margin-right:5px}
tr.t-zd td{background:#2d1e1e} tr.t-zd .tag{background:#b04a4a}
tr.t-gc td{background:#1c2733} tr.t-gc .tag{background:#3a7abf}
tr.t-hj td{background:#251f33} tr.t-hj .tag{background:#6b5fc0}
td.pre{color:#9a9aa5;font-size:11.5px}
.sent{font-size:13px;color:#c9c9d2;background:#242a33;border-left:3px solid #3a7abf;padding:6px 10px;margin:6px 0}
.sent b{color:#f0c46a}
.rl{display:inline-block;font-size:10.5px;padding:0 5px;border-radius:4px;color:#fff;
  margin-right:3px;border:1px solid transparent}
/* 角色标签配色（2026-10-24 用户报：『空间龙·总龙头·天量换手』显示成灰色认不出）
   旧写法把整串当 key 查表 → 复合标签查不到 → 一律落到 rl-f「跟风」的深灰底，
   等于把最强的票画成最不起眼的颜色。现在按 · 拆开逐段染色，并按重要性给了梯度：
   紫(空间龙/总龙头) > 红(板块龙头) > 橙(首板龙头) > 金(日内龙) > 青(早盘板) > 蓝(助攻)
   > 绿(补涨) > 紫灰(换手龙) > 棕(一字龙) > 暗红(天量换手) > 灰(独苗) > 深灰(跟风)。*/
.rl-x{background:#8B3FBF;border-color:#B074E0}
.rl-h{background:#C0392B;border-color:#E8756A}
.rl-s{background:#B4551F;border-color:#E08A4F}
.rl-1{background:#8A6D1B;border-color:#C9A227}
.rl-k{background:#1F6FA8;border-color:#4A9BD9}
.rl-e{background:#1F7A72;border-color:#4FB3A9}
.rl-a{background:#2E5F8A;border-color:#5A8FB8}
.rl-b{background:#3F6B28;border-color:#6FA34D}
.rl-t{background:#5B4A8A;border-color:#8D7AC0}
.rl-y{background:#7A4A2A;border-color:#B07A4A}
.rl-v{background:#7A3038;border-color:#B06068}
.rl-d{background:#4A4A55;border-color:#6E6E7A;color:#E4E4EA}
.rl-f{background:#33333C;border-color:#4A4A55;color:#B8B8C2}
.brk{font-size:10.5px;color:#7fae7f}
.lad-ok{color:#8fbf6a} .lad-bad{color:#d0a0a0}
.pos{font-size:10.5px;padding:0 4px;border-radius:4px;color:#fff;margin-left:3px}
.p-lo{background:#4e7a33}.p-md{background:#6b6b73}.p-hi{background:#a4742a}
.warn{color:#d0a0a0;font-size:12px}
.up{color:#e07a5f}.dn{color:#7fae7f}
/* 页头（2026-10-24 用户报：整块固定、向下滚动收不上去，占掉大半屏）
   #hd 不再 sticky，长图例折进 <details>；改一条 30px 的细状态条 #bar 吸顶，
   只在滚过页头后出现——滚动时仍然看得见时间/情绪/指数，但不再压住内容。*/
#hd{padding:4px 0 2px}
#hd h1{font-size:18px;margin:2px 0 4px}
#bar{position:sticky;top:0;z-index:6;display:none;align-items:center;gap:14px;
  background:#15151A;border-bottom:1px solid #3A3A44;padding:5px 10px;
  font-size:12px;color:#C9C9D2;white-space:nowrap;overflow:hidden}
#bar.on{display:flex}
#bar b{color:#F0C46A}
#bar #bidx{color:#9AA7B6;overflow:hidden;text-overflow:ellipsis}
#bar #bsent b{color:#F0C46A}
details#lg{margin:4px 0 0}
details#lg summary{cursor:pointer;color:#8FB4DD;font-size:12px;user-select:none}
details#lg summary:hover{color:#B8D4F0}
details#lg[open] summary{margin-bottom:4px}
.idx{font-size:12.5px;color:#C9C9D2;margin:3px 0}
.core{color:#a9c7e8;font-size:11.5px}
.sc{color:#f0c46a;font-style:normal;font-weight:600;font-size:11px}
.mt{color:#8a8a95;font-size:10.5px}
.lay{display:inline-block;padding:0 5px;border-radius:3px;font-size:10.5px;color:#fff;white-space:nowrap}
.l-main{background:#2f7d5c}.l-spill{background:#a4742a}.l-chase{background:#b04a4a}.l-none{background:#5a5a63}
.l-sub{background:none;border:1px dashed #5A6270;color:#AEB7C2}
.apx{color:#e8c26a;font-weight:700;margin-left:1px;cursor:help}
.nb{display:inline-block;padding:0 5px;border-radius:3px;font-size:10.5px;
  border:1px dashed #5A6270;color:#AEB7C2;white-space:nowrap}
.ctl{font-size:12px;color:#8a8a95;margin:6px 0 0}
.ctl button{background:#26262c;color:#c9c9d2;border:1px solid #3a3a42;border-radius:4px;
  padding:2px 9px;margin-right:5px;font-size:11.5px;cursor:pointer}
.ctl button.on{background:#3a5c8a;border-color:#5a86bf;color:#fff}
.ctl b.glab{color:#8fb4dd;font-weight:600}
.ctl b.olab{color:#e0b978;font-weight:600}
#ob button.on{background:#5a4a1f;border-color:#b08d3a;color:#ffe9c2}
/* ── 分组标题按「重要程度」染色（2026-10-24 用户要求）──────────────────────
   判据只有两个客观读数：**兵数**（该组今天有几只涨停）＋**最高成色**，不看涨跌幅、不看情绪。
   红=有兵有高度 · 橙=有兵或很高 · 蓝灰=中等 · 暗灰=单兵。
   颜色只解决「一眼扫出哪条链有兵」，不构成强弱判断，更不是买卖建议。 */
tr.gh td{border-top:2px solid var(--ghb,#4a6a92);border-left:5px solid var(--ghb,#4a6a92);
  background:var(--ghbg,#232a33);color:var(--ghfg,#cfe0f5);font-weight:600;font-size:11.5px;letter-spacing:.2px}
tr.gh .ghn{color:#8fb4dd;font-weight:400}
tr.gh .ghl{color:#e0b978;font-weight:400;margin-left:6px}
tr.gh.h4{--ghbg:#3E1418;--ghb:#E5484D;--ghfg:#FFDCDC}
tr.gh.h3{--ghbg:#33201A;--ghb:#E07A3A;--ghfg:#FFDCC4}
tr.gh.h2{--ghbg:#232A33;--ghb:#4A6A92;--ghfg:#CFE0F5}
tr.gh.h1{--ghbg:#1B2027;--ghb:#39424E;--ghfg:#9AA7B6}
/* 未归类桶（归因未归链/主业未归链/无行业）**不参与评级**：
   它的「兵数」是归因失败的残渣，不是资金聚集。给 9 只未归链打「主线级」红色＝把垃圾桶当主线。 */
tr.gh.hu{--ghbg:#1A1D21;--ghb:#5A6270;--ghfg:#AEB7C2}
tr.gh.hu td{border-top:2px dashed #5A6270;border-left:5px solid #5A6270;font-weight:500}
tr.gh.hu .hv{background:none;border:1px dashed #5A6270;color:#98A2AE}
tr.gh .hv{display:inline-block;margin-left:8px;padding:0 6px;border-radius:3px;
  font-size:10px;font-weight:700;letter-spacing:.5px}
tr.gh.h4 .hv{background:#E5484D;color:#fff}
tr.gh.h3 .hv{background:#E07A3A;color:#1b1b1f}
tr.gh.h2 .hv{background:#4A6A92;color:#fff}
tr.gh.h1 .hv{background:#39424E;color:#93a1b0}
tr.gs td{background:#1e232c;color:#9fb6cf;font-size:11px;font-weight:500;
  border-top:1px dashed #3a4a5e;border-left:5px solid var(--gsb,#3a4a5e);padding:2px 7px}
tr.gs.h4{--gsb:#E5484D;color:#E9B3B5}
tr.gs.h3{--gsb:#E07A3A;color:#E0BFA6}
tr.gs.h2{--gsb:#4A6A92}
tr.gs.h1{--gsb:#39424E;color:#8b98a7}
tr.gs .ghn{color:#6d7f93;font-weight:400}
</style></head><body>
<div id="bar"><span id="bst">启动中…</span><span id="bidx"></span><span id="bsent"></span></div>
<div id="hd"><h1>盘中实时看板 · <span id="dd" class="meta"></span></h1>
<p class="meta" id="st">启动中…</p>
<p class="idx" id="idx"></p>
<p class="sent" id="sent"></p>
<details id="lg"><summary>口径说明（点开看：个股画像 / 主归属 / 错位度 / 分组配色）</summary>
<p class="meta">个股画像：<b>核心业务</b>＝最新报告期主营构成占比最高项（东财 F10，标〔日期报〕＝该期只有地区口径、已回退到更早一期）｜<b>主业链·环节</b>（标 ≈ 为申万行业/材料化工兜底，属近似）｜<b>今日归因</b>＝当日最相关题材（0—100 分，五因子打分）｜<b>题材（主归属）·梯队</b>＝该票当日<b>唯一</b>归属题材（其标签中今日涨停家数最多者）及该题材的涨停家数/中军/判定，<b>梯队与龙头同源</b>；括号「/标N」＝挂该标签的涨停总数（含主归属在别处的票）｜二者是两个概念：<b>归因＝为什么涨，主归属＝算在谁的梯队里</b>，两者<b>跨链</b>不一致时场景格标 <span class="apx">⚠</span>（同链换环节不标）｜<b>错位度</b>：<span class="lay l-main">主业驱动</span> 涨的是自己的生意 · <span class="lay l-spill">链内外溢</span> 同链换环节 · <span class="lay l-chase">跨链蹭概念</span> 情绪票 · <span class="lay l-none">未归链</span> 匹配不上不硬编 · <span class="apx">≈</span>＝主业是推的，错位度仅供参考</p>
<p class="meta">盘口分组标题的<b>颜色＝重要程度</b>（分数＝兵数×10＋最高成色×0.5，<b>组间顺序与颜色同源</b>）：<span style="background:#3E1418;border-left:4px solid #E5484D;color:#FFDCDC;padding:1px 8px">主线级 ≥120</span> <span style="background:#33201A;border-left:4px solid #E07A3A;color:#FFDCC4;padding:1px 8px">有兵团 80—119</span> <span style="background:#232A33;border-left:4px solid #4A6A92;color:#CFE0F5;padding:1px 8px">零星 45—79</span> <span style="background:#1B2027;border-left:4px solid #39424E;color:#9AA7B6;padding:1px 8px">单兵 &lt;45</span> <span style="background:#1A1D21;border-left:4px dashed #5A6270;color:#AEB7C2;padding:1px 8px">未归类·不评级</span>　只回答「先看哪块」，不是强弱判断、更不是买卖建议。<b>「未归类」桶永远沉底且不评级</b>——它的兵数是归因没覆盖到的残渣，不是资金在那条线上聚集。</p>
</details>
<p class="warn" id="wn"></p></div>
<h2>焦点实时榜（按实时超额；盘前序→实时序）</h2><table id="tb1"></table>
<p class="meta" id="arch"></p>
<p class="meta" id="pool"></p>
<h2 id="h2m">机会候选（在动 = 价格已表态 · 潜伏 = 量能先行价格未跟 · 二者互斥）</h2><table id="tb2"></table>
<h2>补涨候选（前排已起 · 后排未涨 · 池内低位优先）</h2>
<p class="meta"><b>补涨＝龙头打出高度后，踏空资金在同属性内找低位票模仿龙头</b>（买不到一字板 / 不敢买高位）。判据（C级草案——肉肉原文未给量化判据，按行业口径补充）：题材前排已起（<b>主归属</b>涨停≥2家、桶内龙头≥3板、非独木难支）→ 池内(核心/备选)、未涨停、涨幅 −1~3%、5日跑输桶内中位的低位票；每场景取最低位 3 只，场景间按涨停家数降序。<b>场景＝该票当日唯一主归属题材</b>——梯队、龙头、成员全从这一个桶出，不再出现「场景一个题材、龙头另一个题材」；<b>场景格 ⚠ ＝该票的「今日归因链」与补涨场景不属于同一条产业链</b>（补涨逻辑与当日驱动跨链，需自行判断；同链内换环节不标）。<b>行业纪律：龙头封死涨停后当天/次日做低位首板；不做已加速到 3 板以上的补涨；龙头断板当天补涨必须走；补涨自身断板当天走；「跟风不跟涨」提前走。补涨高度 ≈ 龙头的 2/3，断板后很少反包——只作观察，不追。</b></p>
<table id="tb5"></table>
<h2 id="h2p">盘口质量</h2>
<div class="ctl" id="ctl"><b class="glab">分组</b>：
<span id="gb"></span>　<b class="olab">排序</b>：<span id="ob"></span>
<span class="mt" id="ginfo"></span></div>
<p class="meta">入选：池内涨停 ∪ 连板≥3 ∪ 焦点龙头（其余涨停不列）。<b>角色＝多标签可并存</b>：空间龙＝全市场连板最高；总龙头/板块龙头/首板龙头＝题材内连板最高（≥5板/2–4板/1板分级）；日内龙＝题材内最先涨停；早盘板＝首封≤10:30；助攻＝龙头封板后30分钟内跟进；补涨＝龙头≥5板时的题材内首板；换手龙＝连板≥2且换手15–30%；一字龙＝换手&lt;3%且封单厚；天量换手＝换手&gt;20%（出货嫌疑）；独苗＝题材仅一只涨停；跟风＝其余。<b>成色</b>＝身位32+封单24+炸板扣16+换手14+首封10+带动性4。<b>题材·梯队</b>＝该票当日唯一主归属题材 + 该题材<b>主归属</b>涨停家数/中军数（中军＝流通前3、红盘、不必涨停）→ 成立/缺中军/偏薄/独木难支；龙头＝桶内连板最高（并列取首封最早），与角色标签同一条规则，故一定对得上。卡位龙（老龙头断板日逆势抢涨停者）需前一交易日断板数据，暂不标。</p>
<table id="tb4"></table>
<p class="meta">角色：题材龙头＝该题材连板最高且封单占流通比最大；卡位龙＝其余里首封最早但连板低于龙头；独苗＝该题材仅一只涨停（不是竞争出来的龙头）。封单占流通％＝封板资金/流通市值。</p>
<h2>同向性检查</h2><p class="meta" id="al"></p>
<p class="meta" style="margin-top:26px">工具只产读数不产判断 · 下单以行情软件为准 ·
口径与 intraday_scan 一致（compute_readings 唯一定义）· 盘中 15s 自动刷新 · 收盘后自动降频(10分钟)</p>
<script>
function f(v,s){return (v===null||v===undefined||v==='')?'—':v+(s||'');}
function tag(t){const c={'重点':'t-zd','观察':'t-gc','环节':'t-hj'}[t]||'';return `<span class="tag ${c}">${t}</span>`;}
function arr(v,c){return (v>0?`<span class="up">↑</span>`:v<0?`<span class="dn">↓</span>`:'＝');}
function row(h){return `<tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr>`;}
/* 焦点实时榜的「梯队」＝**篮子口径**：挂该标签的全部涨停。
   它回答「这条主线今天有多少兵、结构全不全」；括号里的主归属家数＝其中多少只是「以它为主逻辑」。
   差额＝这些兵被更具体的题材认领了（一票一桶），主线的兵数不因此减少。
   个股视图（盘口/候选/补涨）才用主归属口径。 */
function lad(L){if(!L)return '—';
 /* 焦点实时榜的「梯队」＝**篮子口径**：挂该标签的全部涨停。
    它回答「这条主线今天有多少兵、结构全不全」；括号里的主归属家数＝其中多少只是「以它为主逻辑」。
    差额＝这些兵被更具体的题材认领了（一票一桶），主线的兵数不因此减少。
    个股视图（盘口/候选/补涨）才用主归属口径。
    兼容旧快照：字段不齐时**不混拼两套口径**——混拼会出「4家(主归属0)/0中军·无涨停」这种自相矛盾格。*/
 const raw=L['涨停标签'];
 if(raw==null){const m=L['涨停'];
   return m?`${m}家/${L['中军']}中军·${L['判定']||''}`:'—';}
 const own=L['涨停'];
 const tg=(own!=null&&own!==raw)
   ?`<span class="mt" title="篮子口径 ${raw} 家＝挂着这个标签的全部涨停；其中以它为主逻辑（主归属）${own} 家。差额被更具体的题材认领（一票一桶），不代表这条主线没兵。">(主归属${own})</span>`:'';
 if(L['判定标签']==null)return `<span class="lad-bad">${raw}家${tg}</span>`;
 const nm=L['中军标签'],j=L['判定标签'],ok=j==='成立';
 return `<span class="${ok?'lad-ok':'lad-bad'}">${raw}家${tg}/${nm}中军·${j}</span>`;}
/* 归因链 ≠ 场景链 才算错位（同一链内换个环节不算——先导基电「存储芯片→国产芯片」同属半导体链） */
function chainOff(x){const a=PF(x).chain_only||'',b=x['场景链']||'';return !!a&&a!==b;}
/* ══════ 场景格：梯队/龙头到底属于哪个题材，必须写在格子里 ══════
   2026-10-24 用户报错根因：旧版盘口/补涨表只显示「该题材梯队」「题材龙头」，
   **从不显示是哪个题材的**梯队；读者只能拿同一行的「今日归因」去对——
   于是「固态电池」那一行显示的是长江三角的梯队龙头（上海九百）。
   现在：场景名（＝该票当日唯一主归属题材）+ 家数 + 判定 + 龙头，全在一个桶里出。
   opt.head=false 时不输出龙头行（补涨表龙头单独一列）。*/
function sceneCell(z,opt){
 const o=opt||{};
 const th=z['题材']||z['场景']||'';
 if(!th)return '<td>—</td>';
 const n=z['梯队涨停'], nt=z['梯队涨停标签'];
 const tag=(nt!=null&&nt!==n)?`<span class="mt" title="标签口径家数（含主归属在别处的票）">/标${nt}</span>`:'';
 const hv=z['龙头']&&z['龙头']!==z['name'];
 const head=(o.head!==false&&hv)
   ?`<br><span class="mt">龙头 ${z['龙头']}${z['龙头板']?`（${z['龙头板']}板）`:''}</span>`:'';
 const off=o.off?' <span class="apx" title="该票的今日归因（五因子）与本次补涨场景不是同一个题材——补涨逻辑与当日驱动不一致，需自行判断">⚠</span>':'';
 /* 盘口行的两口径分歧（2026-10-24 用户问赛福天/南方路机）：主归属链 ≠ 归因链 时同样标 ⚠——
    赛福天主归属人形机器人（腱绳叙事、19家）、归因却是 TOPCon 主业驱动；两行并排读必然困惑，
    必须在题材格上把分歧显式标出来。 */
 const mm=(!o.off&&z['主链']&&PF(z).chain_only&&z['主链']!==PF(z).chain_only)
   ?` <span class="apx" title="主归属链（${z['主链']}）与今日归因链（${PF(z).chain_only}）不一致：梯队算在主归属桶，为什么涨看归因列——两个口径各自成立但叙事不同，需自行判断">⚠</span>`:'';
 return `<td><b>${th}</b>${off}${mm} <span class="mt">${n!=null?`${n}家${tag}·`:''}${z['梯队']||'—'}</span>${head}</td>`;
}
function brk(t){return (t&&t.length)?`<span class="brk">${t.join(' ')}</span>`:'—';}
/* 角色标签：按「·」拆开逐段染色。
   旧写法 `{'空间龙':'rl-x',...}[r]` 用**整串**查表，而服务端的 r 是复合标签
   （『空间龙·总龙头·天量换手』），查不到就落到 rl-f 跟风的深灰底——
   最强的票被画成最不起眼的颜色，用户报的「没有重点标出来」就是这个。*/
const RCLS={'空间龙':'rl-x','总龙头':'rl-h','板块龙头':'rl-s','首板龙头':'rl-1',
 '日内龙':'rl-k','早盘板':'rl-e','助攻':'rl-a','补涨':'rl-b','换手龙':'rl-t',
 '一字龙':'rl-y','天量换手':'rl-v','独苗':'rl-d','跟风':'rl-f'};
function roleTag(r){if(!r)return '—';
 return r.split('·').filter(Boolean)
   .map(x=>`<span class="rl ${RCLS[x]||'rl-f'}">${x}</span>`).join('');}
/* ── 个股画像三件套（服务端已把 z['画像'] 挂到每一行；未抓回来时显示「—」，下一轮自动补）── */
function PF(z){return z['画像']||{};}
function cCore(z){const p=PF(z);
 if(!p.core||p.core==='—')return '<td>—</td>';
 /* 主营构成取自更早报告期时，把报告期标出来——不标就等于让读者以为是最新的 */
 const old=(p.core_note&&p.core_note.indexOf('取')>=0)
   ?` <span class="mt" title="${p.core_note}">〔${p.core_asof}报〕</span>`:'';
 return `<td class="core">${p.core}${old}</td>`;}
function cBiz(z){const p=PF(z);
 const t=p.core_note?` title="${p.core_note}"`:'';
 const b=p.biz||'';
 if(b)return `<td class="mt"${t}>${b}</td>`;
 /* 未归链拆两栏（2026-10-24 用户拍板）：「已核实 19 链覆盖不到」与「规则还没补到」不是一回事。
    证据字段 ev 以「人工:」开头＝人工核实过（理由在 override 里）；其余＝待补（随词表修订继续收敛）。 */
 const verified=p.ev&&p.ev.indexOf('人工:')===0;
 const tip=verified?` title="${(p.ev||'').replace(/"/g,'')}"`
   :' title="待补：规则还没覆盖到（随词表修订继续收敛），不是已核实覆盖不到"';
 return `<td class="mt"${tip}><span class="nb">${verified?'非题材·已核实':'待补'}</span></td>`;}
function cAttr(z){const p=PF(z);
 if(!p.theme)return '<td>—</td>';
 /* 代位归因没有资金分（行业代位/主业代位），分数位显示「代位」二字，别让人当成 0 分证据 */
 const sc=(p.score==null)?'<span class="mt">代位</span>':`<i class="sc">${p.score}</i>`;
 const sub=p.anchor&&p.anchor.endsWith('代位')?` <span class="mt" title="该票无可用概念标签（北交所标签为空 / 概念全是行业与噪声），按${p.anchor==='行业代位'?'申万行业':'主业链'}代位显示——没有资金读数，不是归因结论">${p.anchor}</span>`:'';
 return `<td><b>${p.theme}</b> ${sc}${sub}<br><span class="mt">${p.chain||''}</span></td>`;}
function cLay(z){const p=PF(z);
 const c={'主业驱动':'l-main','链内外溢':'l-spill','跨链蹭概念':'l-chase',
          '行业代位':'l-sub','主业代位':'l-sub'}[p.layer]||'l-none';
 /* approx＝主业由行业/关键词兜底推定，错位度只作参考，加「≈」提示别当结论用 */
 const q=p.approx?'<span class="apx" title="主业为行业/关键词兜底推定，错位度仅供参考">≈</span>':'';
 return `<td><span class="lay ${c}">${p.layer||'—'}${q}</span></td>`;}
/* ══════ 盘口质量·分组视图（2026-10-24 用户要求：同一题材/归因/行业/主业链放一起，并排序）══════
   三种「内/外」的读法这里都给了：
     · 组间·强度  —— 哪条链今天最强先看（默认）
     · 组内·归因分 —— 同一题材里谁是真龙头（按归因分，不只看成色）
     · 由内到外   —— 主业驱动 → 链内外溢 → 跨链蹭概念，情绪票沉底
   分组键取「归因环节」而不是「题材」作默认：题材标签太碎（人形机器人/机器人概念/机器人执行器
   是同一个炒作单元拆成三行），先按环节聚，再按题材看，才不会把同一批资金拆散。*/
window.PK=[];
let GRP='chain', ORD='strength';
const UN='（归因未归链）';
const LAYER_RANK={'主业驱动':0,'链内外溢':1,'跨链蹭概念':2,'题材未归链':3,'主业未归链':3,'无题材读数':4,'行业代位':3,'主业代位':3};
const GMODES={
 chain:{t:'归因链→环节', k:z=>{const p=PF(z);return p.chain_only||UN;}, sub:true},
 main:{t:'主归属链（梯队）', k:z=>z['主链']||z['题材']||UN},
 seg:  {t:'归因环节', k:z=>{const p=PF(z);return p.chain||UN;}},
 theme:{t:'归因题材', k:z=>{const p=PF(z);return p.theme||'（无归因）';}},
 biz:  {t:'主业链→环节', k:z=>{const p=PF(z);return p.biz||'（主业未归链）';}},
 ind:  {t:'申万行业', k:z=>{const p=PF(z);return p.ind||'（无行业）';}}
};
const OMODES={
 strength:{t:'组内·成色'},
 score:   {t:'组内·归因分'},
 inner:   {t:'由内到外（驱动→蹭概念）'}
};
function pkRow(z){
 return `<tr><td>${roleTag(z['角色'])}</td><td><b>${z['成色']??'—'}</b></td><td>${z.name}${z['池内']?'':' '}</td>`+
  `${cCore(z)}${cAttr(z)}${cLay(z)}${cBiz(z)}${sceneCell(z)}<td>${z['连板']}</td><td>${z['首封']}</td>`+
  `<td>${z['封单亿']}</td><td>${z['封单占流通%']}</td><td>${z['炸板次数']}</td><td>${f(z['涨幅'],'%')}</td></tr>`;
}
function segOf(z){const p=PF(z);const c=p.chain||'';const i=c.indexOf('·');
 if(i>0)return c.slice(i+1);
 /* 归因是链级大题材（机器人概念/军工——环节为空）时，借主业环节落位：
    前提是主业链与归因链同链——「机器人概念」27 只里谁是做减速器的谁是做本体的，
    靠主业细分，比全部挤在「—」里可读得多。跨链不借（借了就是编）。 */
 if(p.chain_only&&p.biz_chain===p.chain_only&&p.biz_seg)return p.biz_seg+'·主业';
 return '—';}
/* ══════ 组的重要程度：**顺序和颜色用同一个分数**（2026-10-24）══════
   score = 兵数×10 + 最高成色×0.5 —— 兵数是主项（这条链今天有几只涨停），成色是修饰项。
   为什么不用纯成色：昨天出现「1 只涨停但成色 75」的组排在「9 只涨停、成色 74」的组前面，
   颜色和顺序各说各话。乘 10 保证「多一只兵」永远压过「成色高 20 分」。
   为什么不用纯兵数：3 只成色 87 的组，比 3 只成色 39 的组更值得先看。
   阈值 120/80/45 是粗筛，不是评分模型——它只回答「先看哪块」。*/
function gscore(n,best){return (n||0)*10+(best||0)*0.5;}
function heat(n,best){const s=gscore(n,best);
 return s>=120?4:s>=80?3:s>=45?2:1;}
const HTXT={4:'主线级',3:'有兵团',2:'零星',1:'单兵'};
/* 未归类桶：键是「装不下」的产物，不是题材。兵数再多也不评级。 */
function isUN(k){return k===UN||k==='（主业未归链）'||k==='（无行业）';}
function renderPk(){
 const pk=window.PK||[];
 if(!pk.length){document.getElementById('tb4').innerHTML=
   '<tr><td colspan="14">（涨停池未取到——盘中会实时拉取）</td></tr>';return;}
 const mode=GMODES[GRP], key=mode.k, m=new Map();
 /* 二级环节子标题只在「归因链→环节 + 组内·成色」时启用；
    其余排序若仍插子标题，同一环节会被打断成多段——宁可不插 */
 const useSub=(GRP==='chain' && ORD==='strength');
 pk.forEach(z=>{const k=key(z);if(!m.has(k))m.set(k,[]);m.get(k).push(z);});
 const gs=[...m.entries()].map(([k,arr])=>{
   const best=Math.max(...arr.map(x=>x['成色']??0));
   const sc=Math.max(...arr.map(x=>(PF(x).score??0)));
   const lc={};arr.forEach(x=>{const l=PF(x).layer||'—';lc[l]=(lc[l]||0)+1;});
   const dom=Object.entries(lc).sort((a,b)=>b[1]-a[1])[0][0];
   return {k,arr,best,sc,lc,dom};
 });
 /* 组间：未归链永远沉底；其余按「由内到外」或「组内最高成色」 */
 gs.sort((a,b)=>{
   const au=isUN(a.k), bu=isUN(b.k);
   if(au!==bu)return au?1:-1;
   if(ORD==='inner')return (LAYER_RANK[a.dom]??9)-(LAYER_RANK[b.dom]??9)
     ||gscore(b.arr.length,b.best)-gscore(a.arr.length,a.best);
   /* 其余模式：组间一律按重要程度（＝颜色同一个分数），组内才按用户选的模式 */
   return gscore(b.arr.length,b.best)-gscore(a.arr.length,a.best)
     ||b.arr.length-a.arr.length;
 });
 /* 组内排序：
   ① 组内·成色  → 先按「环节」聚成小块，小块之间按各自最高成色排（不是按拼音），块内按成色
   ② 组内·归因分 → 纯按归因分，不插子标题
   ③ 由内到外   → 先按错位度（驱动→外溢→蹭概念→未归链），同层再按成色 */
 gs.forEach(g=>{
   if(useSub){
     const sb={};g.arr.forEach(z=>{const s=segOf(z);sb[s]=Math.max(sb[s]??0,z['成色']??0);});
     const ord=Object.keys(sb).sort((a,b)=>sb[b]-sb[a]);
     const rk={};ord.forEach((s,i)=>rk[s]=i);
     g.segOrd=ord;
     g.arr.sort((a,b)=>(rk[segOf(a)]??99)-(rk[segOf(b)]??99)||(b['成色']??0)-(a['成色']??0));
   } else if(ORD==='score'){
     g.arr.sort((a,b)=>(PF(b).score??0)-(PF(a).score??0)||(b['成色']??0)-(a['成色']??0));
   } else {
     g.arr.sort((a,b)=>{const d=(LAYER_RANK[PF(a).layer]??9)-(LAYER_RANK[PF(b).layer]??9);
       return d||(b['成色']??0)-(a['成色']??0);});
   }
 });
 let html=row(['角色','成色','名称','核心业务（占比·毛利）','今日归因（分）','错位度','主业链·环节','题材（主归属）·梯队','连板','首封','封单(亿)','占流通%','炸板','涨幅']);
 gs.forEach(g=>{
   const ls=Object.entries(g.lc).map(([k,v])=>`${k}${v}`).join('/');
   const segc={};g.arr.forEach(x=>{const s=segOf(x);segc[s]=(segc[s]||0)+1;});
   const segTxt=Object.entries(segc).sort((a,b)=>b[1]-a[1])
      .map(([s,n])=>`${s}${n}`).join('/');
   const segLine=(useSub?'':`　<span class="ghl">环节 ${segTxt}</span>`);
   const hg=isUN(g.k)?0:heat(g.arr.length,g.best);
   const cls=hg?`gh h${hg}`:'gh hu';
   const hv=hg?`<span class="hv">${HTXT[hg]}</span>`
     :'<span class="hv" title="这个桶装的是「一条链都没归上」的票，兵数来自归因覆盖不全，不是资金在这条线上聚集。给个高低级会把垃圾桶当主线——所以不评级。">未归类·不评级</span>';
   html+=`<tr class="${cls}"><td colspan="14">◤ ${g.k}　<span class="ghn">${g.arr.length} 只 · 最高成色 ${g.best} · 最强归因 ${g.sc||'—'}</span>${segLine}<span class="ghl">${ls}</span>${hv}</td></tr>`;
   let lastSeg=null;
   g.arr.forEach(z=>{
     if(useSub){
       const s=segOf(z);
       if(s!==lastSeg && s!=='—'){lastSeg=s;
         const sq=g.arr.filter(x=>segOf(x)===s), sn=sq.length,
               sb=Math.max(...sq.map(x=>x['成色']??0)), hs=heat(sn,sb);
         html+=`<tr class="gs h${hs}"><td colspan="14">· ${s}　<span class="ghn">${sn} 只 · 最高成色 ${sb}</span></td></tr>`;}
     }
     html+=pkRow(z);
   });
 });
 document.getElementById('tb4').innerHTML=html;
 document.getElementById('ginfo').textContent=`共 ${gs.length} 组 / ${pk.length} 只`;
}
function ctlBtn(txt,on,fn){const b=document.createElement('button');b.textContent=txt;
 if(on)b.className='on';b.onclick=fn;return b;}
function mountCtl(){
 const gb=document.getElementById('gb');gb.innerHTML='';
 Object.entries(GMODES).forEach(([k,v])=>gb.appendChild(ctlBtn(v.t,k===GRP,()=>{GRP=k;mountCtl();renderPk();})));
 const ob=document.getElementById('ob');ob.innerHTML='';
 Object.entries(OMODES).forEach(([k,v])=>ob.appendChild(ctlBtn(v.t,k===ORD,()=>{ORD=k;mountCtl();renderPk();})));
}
mountCtl();
/* 页头滚过后显示细条（问题 3：整块固定收不上去 → 改 30px 细条吸顶） */
const _bar=document.getElementById('bar');
window.addEventListener('scroll',()=>{_bar.classList.toggle('on',window.scrollY>170);},{passive:true});
async function tick(){
 const _age=(d)=>(Date.now()-new Date(d.taken_at.replace(' ','T')).getTime())/1000;
 try{
  const d=await (await fetch('/api/state?ts='+Date.now(), {cache:'no-store'})).json();
  if(d.booting){document.getElementById('st').textContent='启动中…';return;}
  document.getElementById('dd').textContent=`股池数据日 ${d.data_date}`;
  document.getElementById('st').textContent=
   `${d.taken_at} · 源 ${d.src} · ${d.n_snap} 只 · 市场中位 ${f(d.mkt_med_pct,'%')} · `+
   `${d.slot}${d.standard?'':'（非标）'} · ${d.clock_hint.split('——')[0]}`+
   (_age>60?` ⚠⚠ 数据已陈旧 ${Math.round(_age)} 秒——服务可能已停止`:'');
  document.getElementById('wn').innerHTML=(d.warn||[]).join(' ｜ ');
  /* 吸顶细条同步（2026-10-24）：滚动时只剩这一行，时间/指数/情绪不能跟着页头滚走 */
  const se0=d.sentiment||{};
  document.getElementById('bst').textContent=
   `${d.taken_at} · ${d.slot}${d.standard?'':'（非标）'} · ${d.n_snap} 只`+
   (_age>60?` ⚠⚠ 陈旧 ${Math.round(_age)} 秒`:'');
  document.getElementById('bidx').textContent=(d.indices||[])
   .map(x=>`${x.name} ${x.pct>0?'+':''}${x.pct}%`).join('  ');
  document.getElementById('bsent').innerHTML= se0['涨停']!==undefined?
   `涨停 <b>${se0['涨停']}</b> · 炸板 <b>${se0['炸板']}</b> · 跌停 <b>${se0['跌停']}</b> · 最高 <b>${Math.trunc(se0['最高连板']||0)}板</b>`:'';
  const r=d.focus.filter(x=>x.exc!==null).sort((a,b)=>a.rank-b.rank).slice(0,14);
  const pre=(x)=>{const p=x.pre||{};return [f(p.pre_rank),f(p.pre_zljlr,'亿'),f(p.pre_ret5,'%'),f(p.pre_run),`${f(p.pre_cont)}<br>${f(p.pre_lc)}`];};
  document.getElementById('tb1').innerHTML=
   row(['档位','题材','涨','超额','涨停','梯队','涨股比','量比','资金','盘前→现在',
        '盘前·排名','盘前·主力','盘前·自算5日','盘前·连胜','盘前·连续性/生命周期'])+
   r.map(x=>{const p=pre(x);return `<tr class="${{'重点':'t-zd','观察':'t-gc','环节':'t-hj'}[x.tier]}"><td>${tag(x.tier)}</td>`+
   `<td>${x.name}</td><td>${f(x.eq_pct,'%')}</td><td>${f(x.exc,'%')}</td><td>${x.zt}</td>`+
   `<td>${lad(x.ladder)}</td>`+
   `<td>${f(x.ups)}</td><td>${f(x.lb)}</td><td>${f(x.zljlr_yi,'亿')}</td>`+
   `<td>#${x.pre_rank}→#${x.rank} ${arr(x.delta)}</td>`+
   p.map(v=>`<td class="pre">${v}</td>`).join('')+'</tr>';}).join('');
  const ix=d.indices||[];
  document.getElementById('idx').innerHTML= ix.length?
   ix.map(x=>{const c=x.pct>0?'up':x.pct<0?'dn':'';return `${x.name} <span class="${c}">${x.price}（${x.pct>0?'+':''}${x.pct}%）</span>`;}).join(' · ') : '';
  const se=d.sentiment||{};
  document.getElementById('sent').innerHTML= se['涨停']!==undefined ?
   `情绪条 · 涨停 <b>${se['涨停']}</b> · 炸板 <b>${se['炸板']}</b>（率 <b>${se['炸板率']}%</b> = 炸板池 ${se['炸板池']??'—'} + 回封 ${se['回封']??'—'}）· 跌停 <b>${se['跌停']}</b>（封死 ${se['封死跌停']??'—'}）· 最高 <b>${Math.trunc(se['最高连板']||0)}板</b> · 连板家数 <b>${se['连板家数']}</b>` : '';
  const pl=(d.pool_log||[]).map(x=>`${x.label} ${x.ok?'✓':'✗'} ${x.at}(${x.sec}s)`).join(' · ');
  document.getElementById('pool').innerHTML=
   `<b>股池自动更新</b>（四时点盘中版 + 收盘最终版）｜${pl||'今日暂无盘中更新'}｜最终股池：${d.final_note||'—'}`;
  document.getElementById('arch').innerHTML=
   `今日已落档时点：${(d.archived||[]).join('、')||'（暂无——标准时点窗口内自动落档）'}`;
  const cand=d.candidates||[];
  document.getElementById('h2m').textContent=
   `机会候选（🔥已动 ${d.movers.length} + 💤潜伏 ${d.lurkers.length} = ${cand.length} 只 · 状态互斥）`;
  document.getElementById('tb2').innerHTML=
   row(['状态','池层','名称','核心业务（占比·毛利）','今日归因（分）','错位度','主业链·环节','场景（主归属）·梯队','涨幅','量能','5日','突破','涨速','额(亿)','位置'])+
   cand.map(x=>{const st=x['状态']==='已动'
      ?'<span class="rl" style="background:#b04a4a">🔥已动</span>'
      :'<span class="rl" style="background:#3a7abf">💤潜伏</span>';
    const vl=x['量比注']||f(x['量比']);
    const r5=x.ret5!==undefined?f(x.ret5,'%'):'—';
    const pos=x['位置']?`<span class="pos ${x['位置']==='低位'?'p-lo':x['位置']==='中位'?'p-md':'p-hi'}">${x['位置']}</span>`:'—';
    const off=chainOff(x);
    return `<tr class="${x['池层']==='核心'?'t-zd':'t-gc'}"><td>${st}</td><td>${tag(x['池层'])}</td>`+
     `<td>${x.name}</td>${cCore(x)}${cAttr(x)}${cLay(x)}${cBiz(x)}${sceneCell(x,{head:false,off:off})}<td>${f(x.pct,'%')}</td>`+
     `<td>${vl}</td><td>${r5}</td><td>${brk(x['突破'])}</td><td>${f(x['涨速'])}</td>`+
     `<td>${f(x['额亿'],'亿')}</td><td>${pos}</td></tr>`;}).join('');
  const a=d.alignment;
  document.getElementById('al').innerHTML=
   `盘前 top10 重合 <b>${a.overlap}</b> · 在盘前全部焦点内 <b>${a.overlap40}</b>/10 → ${a.verdict}`;
  const cu=d.catchup||[];
  document.getElementById('tb5').innerHTML=row(['池层','名称','核心业务（占比·毛利）','今日归因（分）','错位度','主业链·环节','补涨场景（主归属·家数·判定）','场景龙头（属哪个桶）','涨幅','5日','换手'])+
   (cu.length?cu.map(x=>{const off=chainOff(x);
    return `<tr class="${x['池层']==='核心'?'t-zd':'t-gc'}"><td>${tag(x['池层'])}</td>`+
    `<td>${x.name}</td>${cCore(x)}${cAttr(x)}${cLay(x)}${cBiz(x)}${sceneCell(x,{head:false,off:off})}`+
    /* 龙头格自带桶名（2026-10-24 用户把金百泽行的龙头「大业股份」误读成 PCB 链的人）：
       没有桶名，读者会拿左边的主业链去解释这个名字——加上「·机器人概念桶」就从根上断掉误读 */
    `<td>${x['龙头']||'—'}${x['龙头板']?` <span class="mt">${x['龙头板']}板</span>`:''}`+
    `${x['场景']?` <span class="mt">·${x['场景']}桶</span>`:''}`+
    `${x['龙头']&&x['龙头']===x.name?' <span class="mt">（本票）</span>':''}</td>`+
    `<td>${f(x.pct,'%')}</td><td>${f(x.ret5,'%')}</td><td>${f(x['换手'])}</td></tr>`;}).join('')
    :'<tr><td colspan="11">（暂无——需题材前排已起且池内有低位未涨票）</td></tr>');
  const pk=d.pankou||[];
  window.PK=pk;
  document.getElementById('h2p').textContent=
   `盘口质量（显示 ${pk.length} 只 / 全市场涨停 ${(d.sentiment||{})['涨停']??'—'} 只 · 肉肉 4.1/4.2/5.2）`;
  renderPk();
 }catch(e){document.getElementById('st').textContent='连接失败（服务已停止？）：'+e;}
}
tick(); setInterval(tick, 15000);
</script></body></html>"""


def serve(board, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/state"):
                b = json.dumps(board.get_state(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")   # 实时页禁缓存（2026-10-24）
                self.end_headers()
                self.wfile.write(b)
            elif self.path in ("/", "/index.html"):
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, *a):
            pass                                  # 页面 30s 轮询，别刷屏

    threading.Thread(target=board.loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), H)
    print(f"看板已启动 → http://127.0.0.1:{port}（Ctrl-C 停止；收盘记得关）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。今日四时点快照已留在 cache/intraday/。")


def main():
    ap = argparse.ArgumentParser(description="盘中实时看板（只产读数；Ctrl-C 停）")
    ap.add_argument("--port", type=int, default=6060)
    ap.add_argument("--interval", type=int, default=15, help="快照刷新间隔秒（默认 15，用户 2026-10-23 定）")
    a = ap.parse_args()
    serve(Board(a.interval), a.port)


if __name__ == "__main__":
    main()
