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
import dual_gate as DG              # 双闸对照（v2 新增，2026-10-25 方案）
import name_clan as NC              # 名字族（字辈/谐音/生肖/方位/数字，2026-10-25）
from intraday_scan import (SLOTS, compute_readings, latest_pool_dir, load_kprev, med,
                           load_pre_focus, slot_of, take_indices, take_pools,
                           take_snapshot, take_boards)

ROOT = P.ROOT
DB_PATH=os.environ.get("TRADING_DB",os.path.join(ROOT,"cache","kb","trading_kb.sqlite"))
# 概念全量归位表（8 类目·零盲区，2026-10-25）：只用于展示「未归链」票的真实归属，不改判定
_CM_PATH = os.path.join(ROOT, "cache", "theme_universe", "concept_map.json")
CMAP = json.load(open(_CM_PATH, encoding="utf-8")) if os.path.exists(_CM_PATH) else {}


class Board:
    """快照缓存 + 计算 + 时点落档。线程安全靠 GIL + 单写线程（只有 refresher 改 state）。"""

    def __init__(self, interval):
        self.interval = max(int(interval), 10)   # 2026-10-23 用户定 15s；下限放宽到 10s
        self.state = {"booting": True, "warn": ["启动中：正在拉取第一份快照…"], "lib": {"ready": False}}
        self.lock = threading.Lock()
        self._candidate_seen = set()
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
        # ── 库数据旁路（2026-10-26）：只读 SQLite，5 分钟一次，绝不进 15s 循环 ──
        self.lib = {"ready": False}
        self._ths_key, self._ths_map = None, {}
        # ── 内置调度（2026-10-26）：15:10 增量 / 20:30 龙虎榜，替代被沙箱挡住的 launchd ──
        self._sched_done = set()
        if os.environ.get("TRADING_BOARD_SCHEDULER", "1") == "1":
            threading.Thread(target=self._sched_loop, daemon=True).start()
        threading.Thread(target=self._lib_loop, daemon=True).start()
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

    def _load_ths_map(self, codes):
        """涨停池代码 → 同花顺产业概念（仅概念板块 kind='N'）。带缓存，不进 15s 常规流程。"""
        import sqlite3 as _sq
        db = DB_PATH
        if not os.path.exists(db) or not codes:
            return {}
        con = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        try:
            q = ",".join("?" * len(codes))
            rows = con.execute(
                "SELECT m.code, b.name FROM rel_ths_member m "
                "JOIN dim_ths_board b ON b.ts_code=m.ths_code AND b.kind='N' "
                "JOIN rel_ths_coord c ON c.ts_code=m.ths_code AND c.category='产业题材' "
                "WHERE m.code IN (" + q + ")", tuple(codes)).fetchall()
            out = {}
            for code, name in rows:
                out.setdefault(code, []).append(name)
            return out
        finally:
            con.close()

    def _run_job(self, mode):
        """后台跑 daily_cron.sh（不阻塞刷新）"""
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            subprocess.Popen(["/bin/bash", os.path.join(root, "scripts", "daily_cron.sh"), mode],
                             cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def _db_state(self):
        """返回 (today_str, 今日日线是否已入库, 今日龙虎榜是否已入库)"""
        import sqlite3 as _sq
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db = DB_PATH
        d = time.strftime("%Y-%m-%d")
        try:
            con = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
            n_snap = con.execute("SELECT COUNT(*) FROM fact_snapshot WHERE date=?", (d,)).fetchone()[0]
            n_lhb = con.execute("SELECT COUNT(*) FROM fact_lhb WHERE date=?", (d,)).fetchone()[0]
            con.close()
            return d, n_snap > 0, n_lhb > 0
        except Exception:
            return d, False, False

    def _sched_loop(self):
        """Optional compatibility trigger; job runner owns locks and durable status."""
        import job_scheduler
        while True:
            try:
                job_scheduler.tick()
            except Exception as error:
                print(f"调度暂缓：{type(error).__name__}", flush=True)
            time.sleep(30)

    def _backfill_lhb(self):
        """开机补跑：最近 10 个交易日若缺龙虎榜则补齐（覆盖看板 20:30 未开的情况）"""
        import sqlite3 as _sq, subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db = DB_PATH
        try:
            con = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
            have = {r[0] for r in con.execute("SELECT DISTINCT date FROM fact_lhb")}
            days = [r[0] for r in con.execute(
                "SELECT DISTINCT date FROM fact_snapshot ORDER BY date DESC LIMIT 10")]
            con.close()
            for d in days:
                if d in have:
                    continue
                subprocess.run([os.path.join(root, ".venv", "bin", "python"),
                                os.path.join(root, "scripts", "ts_retry_lhb.py"), d.replace("-", "")],
                               cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                time.sleep(2)
        except Exception:
            pass

    # ── 下一棒候选的关注记录（2026-10-27）：点击即落盘，盘后可回测 ──
    def _watch_file(self):
        d = time.strftime("%Y%m%d")
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "watchlists", f"盘中候选_{d}.jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def record_watch(self, obj):
        if not self.state.get("quote_date"):
            raise ValueError("源行情日期未知，暂不记录可回测候选")
        if len(str(obj.get("code") or ""))!=6 or not str(obj.get("code")).isdigit():
            raise ValueError("invalid stock code")
        candidate=next((x for row in self.state.get("pankou",[]) for x in row.get("候选",[])
                        if x.get("code")==obj.get("code") and row.get("题材")==obj.get("scene")),None)
        if candidate is None:
            raise ValueError("候选已变化，请刷新后重试")
        quote=str(candidate.get("quote_time") or self.state.get("quote_time") or "")
        rec = {"ts":time.strftime("%Y-%m-%d %H:%M:%S"),
               "data_date":quote[:4]+"-"+quote[4:6]+"-"+quote[6:8] if len(quote)>=8 else self.state.get("quote_date"),
               "code":candidate["code"],"name":candidate.get("name"),"scene":obj.get("scene"),
               "entry_pct":candidate.get("pct"),"entry_price":candidate.get("price"),
               "quote_time":quote,"rule_version":"candidate-observation-v2","src":"pankou_next"}
        with open(self._watch_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def record_exposures(self, state):
        """Log all first candidate exposures, independently of user clicks, outside SQLite."""
        if not state.get("quote_date"):
            return
        path=os.path.join(ROOT,"watchlists",f"候选曝光_{state['quote_date'].replace('-','')}.jsonl")
        for row in state.get("pankou",[]):
            for rank,x in enumerate(row.get("候选",[]),1):
                key=(state["quote_date"],row.get("题材"),x.get("code"))
                if key in self._candidate_seen:continue
                rec={"ts":state["taken_at"],"data_date":state["quote_date"],"scene":row.get("题材"),
                     "anchor":row.get("code"),"rank":rank,"rule_version":"candidate-observation-v2",**x}
                with open(path,"a",encoding="utf-8") as f:f.write(json.dumps(rec,ensure_ascii=False)+"\n")
                self._candidate_seen.add(key)

    def watches_today(self):
        p = self._watch_file()
        out = []
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        return out

    def _lib_loop(self):
        while True:
            try:
                self.lib = self._lib_load()
                with self.lock:
                    if isinstance(self.state, dict):
                        self.state["lib"] = self.lib
            except Exception as e:
                self.lib = {"ready": False, "err": str(e)[:80]}
            time.sleep(300)

    def _lib_load(self):
        """从全量库读当日结论：题材榜 / 资金榜 / ETF 估算申赎对价。只读连接。"""
        import sqlite3 as _sq
        db = DB_PATH
        if not os.path.exists(db):
            return {"ready": False, "err": "库不存在"}
        con = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        con.row_factory = _sq.Row
        try:
            d = con.execute("SELECT MAX(date) FROM fact_kpl").fetchone()[0]
            out = {"ready": True, "date": d, "asof": time.strftime("%H:%M:%S")}
            out["source_dates"] = {t: con.execute("SELECT MAX(date) FROM " + t + " WHERE date<=date('now','+8 hours')").fetchone()[0]
                for t in ("fact_kpl", "fact_moneyflow", "fact_etf_daily", "fact_etf_share")}
            out["quality_note"] = "各卡按独立数据日发布；ETF估计值不含缺净值或缺相邻交易日记录"
            out["partitions"] = [dict(r) for r in con.execute(
                "SELECT source,date,status,rows,note FROM ingest_partition WHERE date=?",(d,))]
            out["themes"] = [dict(r) for r in con.execute(
                "SELECT theme_name, MAX(z_t_num) zt, MAX(hot_num) hot, COUNT(*) n "
                "FROM fact_kpl WHERE date=? GROUP BY theme_name "
                "ORDER BY IFNULL(zt,0) DESC, IFNULL(hot,0) DESC LIMIT 10", (d,))]
            out["flow_in"] = [dict(r) for r in con.execute(
                "SELECT e.dst_id chain, ROUND(SUM(f.net_mf)/10000.0,1) yi, COUNT(*) n "
                "FROM fact_moneyflow f JOIN rel_edge e ON e.rel_type='stock_chain' "
                "AND e.src_id=f.code AND e.role='主业' WHERE f.date=? "
                "GROUP BY e.dst_id ORDER BY yi DESC LIMIT 6", (d,))]
            out["flow_out"] = [dict(r) for r in con.execute(
                "SELECT e.dst_id chain, ROUND(SUM(f.net_mf)/10000.0,1) yi, COUNT(*) n "
                "FROM fact_moneyflow f JOIN rel_edge e ON e.rel_type='stock_chain' "
                "AND e.src_id=f.code AND e.role='主业' WHERE f.date=? "
                "GROUP BY e.dst_id ORDER BY yi ASC LIMIT 4", (d,))]
            # ETF 估算申赎对价 5 日趋势（各链）
            try:
                _ds = [r[0] for r in con.execute(
                    "SELECT DISTINCT date FROM v_etf_chain_flow ORDER BY date DESC LIMIT 5")]
                _ds = sorted(_ds)
                out["etf5"] = {"dates": _ds, "rows": [dict(r) for r in con.execute(
                    "SELECT chain_id chain, date, ROUND(净申赎亿,1) yi FROM v_etf_chain_flow "
                    "WHERE date IN (%s) ORDER BY chain_id, date" % ",".join("?" * len(_ds)), tuple(_ds))]}
            except Exception:
                out["etf5"] = {"dates": [], "rows": []}
            out["etf"] = [dict(r) for r in con.execute(
                "SELECT chain_id chain, ROUND(净申赎亿,1) yi, n_etf FROM v_etf_chain_flow "
                "WHERE date=? ORDER BY 净申赎亿 DESC LIMIT 8", (d,))]
            # ── 涨停近似（分板块阈值）：主板 9.5 / 创业科创 19.5 / 北交所 29 ──
            ZT = ("(CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN pct>=19.5 "
                  "WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 "
                  "ELSE pct>=9.5 END)")
            # ② 涨停历史：近 25 个交易日
            hist = [dict(r) for r in con.execute(
                "SELECT date, SUM(CASE WHEN " + ZT + " THEN 1 ELSE 0 END) n "
                "FROM fact_snapshot WHERE date>=date(?, '-90 day') GROUP BY date ORDER BY date", (d,))]
            out["history"] = hist
            if hist:
                ns = [x["n"] for x in hist]
                out["zt_today"] = ns[-1]
                out["zt_avg5"] = round(sum(ns[-5:]) / max(len(ns[-5:]), 1), 1)
                out["zt_avg20"] = round(sum(ns[-20:]) / max(len(ns[-20:]), 1), 1)
            # ③ ETF篮子关联：今日涨停票 ∩ ETF 成分（按 ETF 当日净申赎排序）
            out["resonance"] = [dict(r) for r in con.execute(
                "SELECT x.code, x.name, x.pct, x.etf, x.etf_pct, x.chain, x.flow FROM ("
                "  SELECT s.code, IFNULL(st.name, s.code) name, ROUND(s.pct,2) pct, "
                "         e.name etf, ROUND(IFNULL(ed.pct,0),2) etf_pct, e.chain_id chain, "
                "         ROUND(IFNULL(f.净申赎亿,0),1) flow, "
                "         ROW_NUMBER() OVER (PARTITION BY s.code ORDER BY IFNULL(ed.pct,0) DESC) rn "
                "  FROM fact_snapshot s "
                "  LEFT JOIN dim_stock st ON st.code=s.code "
                "  JOIN rel_etf_cons rc ON rc.con_code=s.code "
                "  JOIN dim_etf e ON e.code=rc.etf_code "
                "  LEFT JOIN fact_etf_daily ed ON ed.code=e.code AND ed.date=s.date "
                "  LEFT JOIN v_etf_chain_flow f ON f.date=s.date AND f.chain_id=e.chain_id "
                "  WHERE s.date=? AND " + ZT.replace("code", "s.code").replace("pct", "s.pct") +
                ") x WHERE x.rn=1 ORDER BY x.etf_pct DESC, x.pct DESC LIMIT 10", (d,))]
            # ④ 同花顺概念榜：今日涨停票按概念聚合
            out["ths"] = [dict(r) for r in con.execute(
                "SELECT b.name concept, COUNT(DISTINCT s.code) zt "
                "FROM fact_snapshot s "
                "JOIN rel_ths_member m ON m.code=s.code "
                "JOIN dim_ths_board b ON b.ts_code=m.ths_code "
                "JOIN rel_ths_coord c ON c.ts_code=m.ths_code AND c.category='产业题材' "
                "JOIN dim_ths_board bb ON bb.ts_code=m.ths_code AND bb.kind='N' "
                "WHERE s.date=? AND " + ZT.replace("code", "s.code").replace("pct", "s.pct") + " "
                "GROUP BY b.name HAVING zt>=2 ORDER BY zt DESC LIMIT 10", (d,))]
            # ⑤ 跨链新簇（加速榜）：来自 theme_cross.py 的离线结果
            try:
                out["cross"] = [dict(r) for r in con.execute(
                    "SELECT cluster_id, chains, n_chains, n_concepts, n_stocks, zt_today, zt_avg20, ratio, members "
                    "FROM theme_cross_cluster ORDER BY n_concepts DESC LIMIT 40")]
                _cc = {}
                for cid, code in con.execute("SELECT cluster_id, code FROM theme_cross_stock"):
                    _cc.setdefault(cid, []).append(code)
                out["cross_codes"] = _cc
                out["cross_at"] = con.execute(
                    "SELECT MAX(updated) FROM theme_cross_cluster").fetchone()[0]
            except Exception as _ce:
                out["cross"] = []; out["cross_err"] = str(_ce)[:120]
            try:
                out["watch_n"] = len(self.watches_today())
            except Exception:
                out["watch_n"] = 0
            return out
        finally:
            con.close()

    def refresh(self):
        """拉快照 → 计算 → 存 state →（若在时点窗口内）落正式档。失败时保留旧 state。"""
        try:
            if os.environ.get("TRADING_BOARD_AUTO_POOL","1")=="1":
                self.maybe_update_pool(dt.datetime.now())
            snap, src, warn, _sim = take_snapshot(False, "")
            if not snap:
                raise RuntimeError("快照源全部失败")
            quote_times=[str(r.get("quote_time")) for r in snap if r.get("quote_time")]
            quote_dates=[v[:4]+"-"+v[4:6]+"-"+v[6:8] for v in quote_times if len(v)>=8 and v[:8].isdigit()]
            quote_date=max(set(quote_dates),key=quote_dates.count) if quote_dates else None
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
                   "scan_date": day, "data_date": self.data_date, "quote_date": quote_date,
                   "quote_time": max(quote_times) if quote_times else None,
                   "src": src, "simulated": False, "n_snap": len(snap),
                   "mkt_med_pct": R["mkt_med"], "archived": archived,
                   "alignment": R["alignment"], "warn": warn,
                   "sentiment": R["sentiment"], "ladder": R["ladder"], "pankou": R["pankou"],
                   "indices": indices, "catchup": R.get("catchup", []),
                   "pool_log": self.pool_log[-6:], "final_note": self.final_note,
                   "focus": R["focus"], "movers": R["movers"], "lurkers": R["lurkers"],
                   "candidates": R.get("candidates", []),
                   "prof": prof_out, "prof_note": self.prof_note}
            # ── 双闸对照（v2 新增，2026-10-25 方案；C 级草案）──
            # quote_day＝行情所属交易日：交易日=真实今天；周末/节假日复盘=股池 data_date。
            # 否则溢价会退化成「昨日涨停票当日涨幅」（恒≈+10%，无意义）。
            # 题材→链·环节映射（焦点榜按大类分组用；48 条，一次算好随 state 下发）
            out["theme_chain"] = {f.get("name"): list(P.chain_of(f.get("name"), self.cfg["chains"]))
                                  for f in R.get("focus", []) if f.get("name")}
            # 概念归位（8 类目）：让「归因未归产业」的票显示它到底属于哪一类（平台生态/地域/政策…），
            # 不再以「未归链」一句话带过——全量透明的展示层落点
            _themes = {(r.get("画像") or {}).get("theme") for r in (R.get("pankou") or [])}
            out["concept_cls"] = {t: CMAP[t] for t in _themes if t and t in CMAP}
            out["cm_cov"] = {"total": len(CMAP),
                             "todo": sum(1 for v in CMAP.values() if v.get("类目") == "待归类")}
            # 名字族（炒名字/字辈/谐音/生肖）：只对当日涨停池算，供结构表「按字辈」排序
            try:
                out["name_clan"] = NC.compute([(z.get("c"), z.get("n"))
                                               for z in (pools[0] if pools else [])])
            except Exception:
                out["name_clan"] = {}
            try:
                _wd = now.weekday()
                _quote_day = quote_date.replace("-", "") if quote_date else str(self.data_date)
                out["dual"] = DG.compute(R.get("sentiment"), R.get("ladder"),
                                         (pools[0] if pools else []), snap, self.con,
                                         _quote_day, pk_rows=R.get("pankou"))
                out["dual"]["asof"] = out["taken_at"]
            except Exception as _de:
                out["dual"] = {"等级": "C级草案", "note": f"双闸计算失败：{_de}"}
            try:
                _codes = tuple(sorted({z.get("code") for z in (R.get("pankou") or []) if z.get("code")}))
                if _codes != self._ths_key:
                    self._ths_map = self._load_ths_map(_codes)
                    self._ths_key = _codes
                out["ths_map"] = self._ths_map
            except Exception as _te:
                out["ths_map"] = {}
            try:
                _live = {z.get("c") for z in (pools[0] if pools else []) if z.get("c")}
                _cc = (self.lib or {}).get("cross_codes") or {}
                if _cc and _live:
                    _rows = []
                    for r in ((self.lib or {}).get("cross") or []):
                        _z = sum(1 for x in (_cc.get(r.get("cluster_id")) or []) if x in _live)
                        _avg = r.get("zt_avg20") or 0
                        _rows.append({**r, "zt_today": _z,
                                      "ratio": (round(_z / _avg, 2) if _avg else None)})
                    _rows = [r for r in _rows if r["zt_today"] >= 1 and (r.get("zt_avg20") or 0) > 0]
                    _rows.sort(key=lambda r: (-(r.get("zt_today") or 0), -(r.get("ratio") or 0)))
                    out["cross_live"] = _rows[:10]
                    out["cross_note"] = "历史按近似涨停阈值，实时按完整涨停池；仅描述，不作交易触发"
                else:
                    out["cross_live"] = []
            except Exception as _xe:
                out["cross_live"] = []
            self.record_exposures(out)
            out["lib"] = self.lib
            out["watch_n"] = (self.lib or {}).get("watch_n", 0)
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
                # 首刷就失败（state 还停在 booting）时把原因写进状态，别只显示「启动中…」——
                # 2026-10-25 实测：行情源全挂时页面看着像卡死，实际是在自动重试
                if self.state.get("booting"):
                    self.state["warn"] = [f"⚠ 行情源暂不可用（自动重试中，无需操作）：{e}"]
                else:
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
/* ── K 线形态标签（2026-10-27 用户要求）────────────────────────────────
   实心＝角色（它在梯队里是什么身份），空心＝形态（这根 K 线长什么样）。
   两者可同时成立（一字板 + 空间龙），所以必须视觉上分开，不能混进同一串。
   一字板给金边：开盘即封死＝隔夜资金一致预期最强，是形态里信号最硬的一种。*/
.fm{display:inline-block;font-size:10px;line-height:1.55;padding:0 4px;border-radius:3px;
  margin:0 3px 1px 0;background:transparent;border:1px solid #5a6270;color:#aab4c2;white-space:nowrap}
.fm-yz{border-color:#c9a227;background:#2b2416;color:#e0c46a}
.fm-tz{border-color:#a4742a;color:#d8a86a}
.fm-dt{border-color:#8B3FBF;color:#c08fe0}
.fm-td{border-color:#a32d2d;color:#d08a8a}
.fm-hf{border-color:#3a7abf;color:#8fb4dd}
.fm-jj{border-color:#4e7a33;color:#8fbf6a}
.fm-wp{border-color:#5a6270;color:#9aa5b4}
.brk{font-size:10.5px;color:#7fae7f}
.lad-ok{color:#8fbf6a} .lad-bad{color:#d0a0a0}
.pos{font-size:10.5px;padding:0 4px;border-radius:4px;color:#fff;margin-left:3px}
.p-lo{background:#4e7a33}.p-md{background:#6b6b73}.p-hi{background:#a4742a}
.warn{color:#d0a0a0;font-size:12px}
/* ── 双闸对照（v2，2026-10-25 方案；C 级草案）── 两卡平级并排 + 组合条 ── */
.dualrow{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 6px;align-items:flex-start}
.dualcard{flex:1;min-width:400px;border:1px solid #3a3a42;border-radius:10px;background:#20202a;padding:10px 12px}
.dualcard h3{font-size:13px;margin:0 0 7px;color:#c9c9d2;font-weight:500}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{font-size:11.5px;background:#2a2a33;border:1px solid #3a3a44;border-radius:5px;padding:2px 7px;color:#c9c9d2}
.chip b{color:#f0c46a;font-weight:500}
.chip.good b{color:#8fbf6a}.chip.bad b{color:#d08080}
.chip.hi{border-color:#6b5a2a;background:#2b2416}
.chip.on{border-color:#5a86bf;background:#26303c;color:#cfe0f5}
/* ── 闸A 重排（2026-10-27）：一屏三块 —— 大数 → 三条量表 → 四个计数 ──────────
   旧版是 20 个等权重 chip 平铺，扫过去只看到一片框，看不出「今天最异常的是哪一项」。
   现在把「规模（涨停家数）」放大，「质量（炸板率/一进二/首板溢价）」做成可比较的量表，
   其余读数降级成一行小字事实——三档视觉权重，才有一眼定位。*/
.gA{display:flex;flex-wrap:wrap;gap:12px 16px;align-items:center;margin:4px 0 8px}
.gA-big{text-align:center;flex:0 0 auto;min-width:88px}
.gA-mtr{display:flex;flex-direction:column;gap:6px;flex:1 1 190px;min-width:168px}
.gA-kpi{display:grid;grid-template-columns:repeat(2,auto);gap:5px 14px;flex:0 0 auto}
.gA-big b{display:block;font-size:34px;line-height:1.05;color:#e8e8ef;font-variant-numeric:tabular-nums}
.gA-big i{display:block;font-size:11px;color:#8a8a95;font-style:normal;margin-top:1px}
.gA-big u{display:block;font-size:10.5px;color:#6f7885;text-decoration:none;margin-top:3px;white-space:nowrap}
.mtr{display:flex;align-items:center;gap:8px;font-size:11.5px}
.mtr .ml{width:56px;color:#8a8a95;flex:none}
.mtr .mtrack{flex:1;height:9px;border-radius:5px;background:#2a2a33;overflow:hidden}
.mtr .mtrack i{display:block;height:100%;border-radius:5px;background:#5c5c68;transition:width .3s}
.mtr em{width:64px;text-align:right;font-style:normal;color:#c9c9d2;font-variant-numeric:tabular-nums;flex:none}
.mtr.good .mtrack i{background:#5b8f5b}.mtr.good em{color:#8fbf6a}
.mtr.bad .mtrack i{background:#a04a4a}.mtr.bad em{color:#d08080}
.gA-kpi div{font-size:10.5px;color:#8a8a95;white-space:nowrap}
.gA-kpi b{display:block;font-size:15px;color:#dcdce4;font-variant-numeric:tabular-nums;line-height:1.25;font-weight:600}
.gA-facts{display:flex;flex-wrap:wrap;gap:3px 15px;font-size:11.5px;color:#7f8794;
  border-top:1px dashed #33333c;padding-top:7px;margin-top:2px}
.gA-facts .fct b{color:#cfd4dd;font-weight:600;margin-left:4px}
.gA-facts .fct.bad b{color:#d08080}.gA-facts .fct.good b{color:#8fbf6a}
@media (max-width:820px){.gA{grid-template-columns:1fr;gap:10px}.gA-big{text-align:left}
 .gA-big b{display:inline;font-size:26px;margin-right:6px}.gA-big i,.gA-big u{display:inline}}
.dmore{color:#8fb4dd;cursor:pointer;margin-left:8px;text-decoration:none}
.dmore:hover{color:#cfe0f5;text-decoration:underline}
.toast{position:fixed;right:22px;bottom:60px;background:#242a33;border:1px solid #3a7abf;color:#cfe0f5;
 padding:8px 14px;border-radius:8px;font-size:12.5px;opacity:0;transition:.3s;pointer-events:none;z-index:99}
.candit{cursor:pointer;border-radius:4px;padding:1px 4px}
.candit:hover{background:#232a33;outline:1px solid #3a7abf}
.cx{color:#5a6270;font-size:10px;margin-right:3px;display:inline-block;transition:.15s}
tr.cxopen .cx{transform:rotate(90deg);color:#c9c9d2}
tr.candline td{background:#171a1f;border-top:1px dashed #2e3540;padding:6px 8px}
.candit{display:inline-block;margin:0 14px 0 0;font-size:12px;color:#c9c9d2}
.candit b{color:#e8b96a;font-weight:600}
.candit .cp{color:#e07575;margin-left:4px}
.candit .cm{color:#6f7885;margin-left:4px;font-size:11px}
.candhd{color:#8a8a95;font-size:11.5px;margin-right:10px}
.structone{border:1px solid #2e2e36;border-radius:8px;background:#1d1d24;padding:8px 10px;margin:6px 0 2px}
.so-hd{font-size:12px;color:#b8b8c2;margin:0 0 6px;font-weight:500}
.so-hd span{color:#6f7885;font-weight:400;margin-left:8px;font-size:11px}
.bpct{color:#6f7885;font-size:10px;font-style:normal;margin-left:3px}
.librow{display:flex;gap:12px;flex-wrap:wrap;align-items:stretch;margin:0 0 12px}
.librow .dualcard{flex:1 1 320px;min-width:0;padding:9px 11px;overflow:hidden}
.librow .dualcard table{width:100%}
.librow .dualcard th,.librow .dualcard td{white-space:nowrap;padding:3px 6px}
.librow .dualcard td:first-child{max-width:150px;overflow:hidden;text-overflow:ellipsis}
.librow .dualcard h3{font-size:12.5px;margin:0 0 6px;color:#c9c9d2;font-weight:500;letter-spacing:.2px}
.librow .drilltab{font-size:12px}
.dualcombo{border:1px solid #3a3a42;border-radius:10px;background:#20202a;padding:9px 12px;margin:0 0 4px}
.duallamp{display:inline-block;font-size:13px;font-weight:500;color:#fff;padding:3px 12px;border-radius:6px}
.dualact{font-size:12px;color:#e4e4e8;background:#26262c;border-left:3px solid #f0c46a;padding:5px 9px;margin-top:7px}
.dualflag{font-size:12px;color:#f0c46a;background:#33261a;border-left:3px solid #b4551f;padding:4px 9px;margin-top:5px}
.dualnote{font-size:11px;color:#8a8a95;margin-top:6px}
.drillitem{display:inline-block;font-size:11.5px;color:#c9c9d2;text-decoration:none;
  background:#26262c;border:1px solid #3a3a44;border-radius:5px;padding:2px 8px;margin:0 4px 4px 0;white-space:nowrap}
.drillitem:hover{background:#2e3644;border-color:#5a86bf;color:#fff}
/* 钻取面板（2026-10-25 重设计：内联长串→紧凑小表，行间信息对齐、可滚动、点名称跳盘口） */
/* 涨停结构（2026-10-25 三改：条形左右并排＋明细可折叠默认展开） */
.structinner{margin-top:9px;border-top:1px dashed #3a3a42;padding-top:8px}
.structgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px 22px}
@media (max-width:980px){.structgrid{grid-template-columns:1fr}}
.bars{display:flex;flex-direction:column;gap:3px}
.bar{display:flex;align-items:center;gap:9px;cursor:pointer;padding:2px 5px;border-radius:5px}
.bar:hover{background:#26262c}
.bar.on{background:#26303c;outline:1px solid #5a86bf}
.bar .bl{width:78px;font-size:11px;color:#aeb7c2;text-align:right;white-space:nowrap;flex:none}
.bar .btrack{flex:1;height:13px;background:#22222a;border-radius:3px;overflow:hidden}
.bar .bfill{display:block;height:100%;background:#4a7fb5;border-radius:3px;transition:width .25s}
.bar.on .bfill{background:#6fa3d8}
.bar .bn{width:42px;font-size:11px;color:#e4e4e8;text-align:left;flex:none}
.drillwrap{max-height:236px;overflow-y:auto;margin-top:5px;border:1px solid #3a3a42;border-radius:7px}
.drilltab{width:100%;border-collapse:collapse;font-size:11px}
.drilltab th{background:#26262c;color:#b8b8c2;font-weight:500;padding:4px 7px;text-align:left;
  position:sticky;top:0;z-index:1;border-bottom:1px solid #3a3a44}
.drilltab td{border-top:1px solid #2a2a32;padding:3px 7px;color:#c9c9d2;white-space:nowrap}
.drilltab tbody tr:nth-child(even) td{background:#1f1f26}
.drilltab tbody tr:hover td{background:#242e3a}
.drilltab th.sortth:hover{background:#30303a;color:#e4e4e8;text-decoration:underline}
.dname{color:#8fb4dd;text-decoration:none;font-weight:500;cursor:pointer}
.dname:hover{color:#cfe0f5;text-decoration:underline}
tr.hlrow td{background:#3a2c12 !important;outline:1px solid #b08d3a}
tr.gsep td{background:#26262c;color:#c9c9d2;font-size:11.5px;font-weight:600;letter-spacing:.3px;
  border-left:4px solid #6b5a2a}
tr.gsep.fire td{border-left-color:#b04a4a}
tr.gsep td .mt{font-weight:400;margin-left:8px}
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
/* ── 左侧固定侧边栏（v2 三轮改版：顶部目录点击后会消失 → 侧栏永久可见＋滚动高亮）──
   速读块＝组合灯/涨停/炸板率/最强链，滚动时始终在视野里——「短时间全面了解」的锚点 */
html{scroll-behavior:smooth}
body{padding-left:146px}
#side{position:fixed;left:0;top:0;bottom:0;width:132px;z-index:8;overflow-y:auto;
  background:#15151A;border-right:1px solid #3a3a42;padding:12px 10px;
  display:flex;flex-direction:column;gap:10px}
.sbrand{font-size:13px;font-weight:500;color:#e4e4e8;line-height:1.5}
#squick{border:1px solid #3a3a42;border-radius:8px;background:#1b1b22;padding:8px;font-size:11.5px;line-height:1.75;color:#c9c9d2}
#squick .sl{color:#8a8a95;font-size:10.5px;display:block;margin-top:5px}
#squick .sl:first-child{margin-top:0}
#squick b{color:#f0c46a;font-weight:500}
#squick .sch{display:inline-block;font-size:10.5px;color:#fff;padding:1px 7px;border-radius:4px}
#snav{display:flex;flex-direction:column;gap:2px}
#snav a{color:#aeb7c2;text-decoration:none;font-size:12.5px;padding:6px 8px;border-radius:6px;border-left:3px solid transparent}
#snav a:hover{background:#22222a;color:#e4e4e8}
#snav a.cur{background:#26303c;color:#cfe0f5;border-left-color:#4a9bd9}
.sfoot{margin-top:auto;font-size:10.5px;color:#6b6b73;word-break:break-all}
@media (max-width:1100px){#side{display:none}body{padding-left:0}}
h2[id],#dualrow{scroll-margin-top:14px}
#blamp{font-size:11px;color:#fff;padding:1px 7px;border-radius:4px;white-space:nowrap}
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
<div id="side">
<div class="sbrand">盘中看板<br><span class="meta" style="font-size:10.5px">v2 · 双闸对照</span></div>
<div id="squick"><span class="sl">大盘速读（随刷新更新）</span>启动中…</div>
<nav id="snav">
<a id="sn_dualrow" href="#dualrow" data-t="dualrow">① 双闸对照</a>
<a id="sn_h2lib" href="#h2lib" data-t="h2lib">② 库数据</a>
<a id="sn_h2p" href="#h2p" data-t="h2p">③ 盘口质量</a>
<a id="sn_h2f" href="#h2f" data-t="h2f">④ 焦点实时榜</a>
<a id="sn_h2m" href="#h2m" data-t="h2m">⑤ 机会候选</a>
<a id="sn_h2c" href="#h2c" data-t="h2c">⑥ 补涨候选</a>
<a id="sn_h2a" href="#h2a" data-t="h2a">⑦ 同向性检查</a>
</nav>
<div class="sfoot" id="stime"></div>
</div>
<div id="bar"><span id="bst">启动中…</span><span id="blamp"></span><span id="bidx"></span><span id="bsent"></span></div>
<div id="hd"><h1>盘中实时看板 · <span id="dd" class="meta"></span>　<span style="font-size:12px;color:#f0c46a;border:1px solid #6b5a2a;border-radius:5px;padding:1px 7px">v2 · 双闸对照（2026-10-25 方案，C 级草案）</span></h1>
<p class="meta" id="st">启动中…</p>
<p class="idx" id="idx"></p>
<p class="sent" id="sent"></p>
<details id="lg"><summary>口径说明（点开看：个股画像 / 主归属 / 错位度 / 分组配色）</summary>
<p class="meta">个股画像：<b>核心业务</b>＝最新报告期主营构成占比最高项（东财 F10，标〔日期报〕＝该期只有地区口径、已回退到更早一期）｜<b>主业链·环节</b>（标 ≈ 为申万行业/材料化工兜底，属近似）｜<b>今日归因</b>＝当日最相关题材（0—100 分，五因子打分）｜<b>题材（主归属）·梯队</b>＝该票当日<b>唯一</b>归属题材（其标签中今日涨停家数最多者）及该题材的涨停家数/中军/判定，<b>梯队与龙头同源</b>；括号「/标N」＝挂该标签的涨停总数（含主归属在别处的票）｜二者是两个概念：<b>归因＝为什么涨，主归属＝算在谁的梯队里</b>，两者<b>跨链</b>不一致时场景格标 <span class="apx">⚠</span>（同链换环节不标）｜<b>错位度</b>：<span class="lay l-main">主业驱动</span> 涨的是自己的生意 · <span class="lay l-spill">链内外溢</span> 同链换环节 · <span class="lay l-chase">跨链蹭概念</span> 情绪票 · <span class="lay l-none">未归链</span> 匹配不上不硬编 · <span class="apx">≈</span>＝主业是推的，错位度仅供参考</p>
<p class="meta">盘口分组标题的<b>颜色＝重要程度</b>（分数＝兵数×10＋最高成色×0.5，<b>组间顺序与颜色同源</b>）：<span style="background:#3E1418;border-left:4px solid #E5484D;color:#FFDCDC;padding:1px 8px">主线级 ≥120</span> <span style="background:#33201A;border-left:4px solid #E07A3A;color:#FFDCC4;padding:1px 8px">有兵团 80—119</span> <span style="background:#232A33;border-left:4px solid #4A6A92;color:#CFE0F5;padding:1px 8px">零星 45—79</span> <span style="background:#1B2027;border-left:4px solid #39424E;color:#9AA7B6;padding:1px 8px">单兵 &lt;45</span> <span style="background:#1A1D21;border-left:4px dashed #5A6270;color:#AEB7C2;padding:1px 8px">未归类·不评级</span>　只回答「先看哪块」，不是强弱判断、更不是买卖建议。<b>「未归类」桶永远沉底且不评级</b>——它的兵数是归因没覆盖到的残渣，不是资金在那条线上聚集。</p>
</details>
<p class="warn" id="wn"></p></div>
<div id="dualrow"><div id="dualA" class="dualcard"></div><div id="dualB" class="dualcard"></div></div>
<div id="dualcombo" class="dualcombo"></div>
<h2 id="h2lib">库数据 · 题材 / 资金 / ETF / 共振</h2>
<p class="meta" id="libmeta">加载中…</p>
<div id="lib"></div>
<h2 id="h2p">盘口质量</h2>
<div class="ctl" id="ctl"><b class="glab">分组</b>：
<span id="gb"></span>　<b class="olab">排序</b>：<span id="ob"></span>
<span class="mt" id="ginfo"></span></div>
<p class="meta"><span id="watchn"></span><b>点任一列展开「相关观察候选」</b>——同链·环节·题材里未涨停但在动的票（口径：233 半路＋共振四条件 / 肉肉 板块容量，非买卖建议）。入选：池内涨停 ∪ 连板≥3 ∪ 焦点龙头（其余涨停不列）。<b>角色＝多标签可并存</b>：空间龙＝全市场连板最高；总龙头/板块龙头/首板龙头＝题材内连板最高（≥5板/2–4板/1板分级）；日内龙＝题材内最先涨停；早盘板＝首封≤10:30；助攻＝龙头封板后30分钟内跟进；补涨＝龙头≥5板时的题材内首板；换手龙＝连板≥2且换手15–30%；一字龙＝换手&lt;3%且封单厚；天量换手＝换手&gt;20%（出货嫌疑）；独苗＝题材仅一只涨停；跟风＝其余。<b>成色</b>＝身位32+封单24+炸板扣16+换手14+首封10+带动性4。<b>题材·梯队</b>＝该票当日唯一主归属题材 + 该题材<b>主归属</b>涨停家数/中军数（中军＝流通前3、红盘、不必涨停）→ 成立/缺中军/偏薄/独木难支；龙头＝桶内连板最高（并列取首封最早），与角色标签同一条规则，故一定对得上。卡位龙（老龙头断板日逆势抢涨停者）需前一交易日断板数据，暂不标。</p>
<table id="tb4"></table>
<h2 id="h2f">焦点实时榜（按实时超额；盘前序→实时序）</h2><table id="tb1"></table>
<p class="meta" id="arch"></p>
<p class="meta" id="pool"></p>
<h2 id="h2m">机会候选（在动 = 价格已表态 · 潜伏 = 量能先行价格未跟 · 二者互斥）</h2><table id="tb2"></table>
<h2 id="h2c">补涨候选（前排已起 · 后排未涨 · 池内低位优先）</h2>
<p class="meta"><b>补涨＝龙头打出高度后，踏空资金在同属性内找低位票模仿龙头</b>（买不到一字板 / 不敢买高位）。判据（C级草案——肉肉原文未给量化判据，按行业口径补充）：题材前排已起（<b>主归属</b>涨停≥2家、桶内龙头≥3板、非独木难支）→ 池内(核心/备选)、未涨停、涨幅 −1~3%、5日跑输桶内中位的低位票；每场景取最低位 3 只，场景间按涨停家数降序。<b>场景＝该票当日唯一主归属题材</b>——梯队、龙头、成员全从这一个桶出，不再出现「场景一个题材、龙头另一个题材」；<b>场景格 ⚠ ＝该票的「今日归因链」与补涨场景不属于同一条产业链</b>（补涨逻辑与当日驱动跨链，需自行判断；同链内换环节不标）。<b>行业纪律：龙头封死涨停后当天/次日做低位首板；不做已加速到 3 板以上的补涨；龙头断板当天补涨必须走；补涨自身断板当天走；「跟风不跟涨」提前走。补涨高度 ≈ 龙头的 2/3，断板后很少反包——只作观察，不追。</b></p>
<table id="tb5"></table>
<p class="meta">角色：题材龙头＝该题材连板最高且封单占流通比最大；卡位龙＝其余里首封最早但连板低于龙头；独苗＝该题材仅一只涨停（不是竞争出来的龙头）。封单占流通％＝封板资金/流通市值。</p>
<h2 id="h2a">同向性检查</h2><p class="meta" id="al"></p>
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
/* 形态标签（2026-10-27）：空心小标，与角色分开渲染。
   为什么不一并塞进「角色」串：角色参与 RANK 排序与龙头判定，形态不参与；
   混在一起会让「一字板」被当成角色去比优先级，排序就乱了。 */
const FCLS={'一字板':'fm-yz','T字板':'fm-tz','地天板':'fm-dt','天地板':'fm-td',
 '回封':'fm-hf','竞价板':'fm-jj','尾盘板':'fm-wp'};
const FTIP={'一字板':'全天只有一个价：开盘即封死，没有波动——隔夜资金一致预期最强',
 'T字板':'开盘涨停→盘中打开→收盘回封，说明有分歧但被买回',
 '地天板':'最低摸过跌停、收盘涨停，日内反转最强',
 '天地板':'最高摸过涨停、收盘跌停，最伤人气',
 '回封':'今日曾炸板后重新封上',
 '竞价板':'09:25 集合竞价就封死',
 '尾盘板':'14:30 后才封板，接力意愿弱'};
function formTag(r){if(!r)return '';
 return r.split('·').filter(Boolean)
   .map(x=>`<span class="fm ${FCLS[x]||''}" title="${FTIP[x]||'K线形态'}">${x}</span>`).join('');}
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
 /* 归因未归产业时不再留白：回退显示归位类目（平台生态·华为／地域区域／政策主题…），
    全量透明（2026-10-25 用户要求：任何细节不许隐形） */
 const _cl=(window._CCLS||{})[p.theme];
 const _cls=(!_nc(p.chain)&&_cl&&_cl['类目']&&_cl['类目']!=='产业题材')
   ?`<br><span class="mt">${_cl['类目']}${_cl['主题域']?'·'+_cl['主题域']:''}</span>`:'';
 /* 代位归因没有资金分（行业代位/主业代位），分数位显示「代位」二字，别让人当成 0 分证据 */
 const sc=(p.score==null)?'<span class="mt">代位</span>':`<i class="sc">${p.score}</i>`;
 const sub=p.anchor&&p.anchor.endsWith('代位')?` <span class="mt" title="该票无可用概念标签（北交所标签为空 / 概念全是行业与噪声），按${p.anchor==='行业代位'?'申万行业':'主业链'}代位显示——没有资金读数，不是归因结论">${p.anchor}</span>`:'';
 return `<td><b>${p.theme}</b> ${sc}${sub}<br><span class="mt">${p.chain||''}</span>${_cls}</td>`;}
function _nc(v){return !v||v==='—';}
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
 ind:  {t:'申万行业', k:z=>{const p=PF(z);return p.ind||'（无行业）';}},
 ths:  {t:'同花顺概念', k:z=>((window.THSMAP||{})[z.code]||['（未归类）'])[0]}
};
const OMODES={
 strength:{t:'组内·成色'},
 score:   {t:'组内·归因分'},
 inner:   {t:'由内到外（驱动→蹭概念）'}
};
function pkRow(z){const _id=z.code?' id="pk_'+z.code+'"':'';
 return `<tr${_id}><td>${roleTag(z['角色'])}${formTag(z['形态'])}</td><td><b>${z['成色']??'—'}</b></td><td>${z.name}${z['池内']?'':' '}</td>`+
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
/* 场景（主归属题材）的环节：优先查 题材→链·环节 映射（服务端主题表），
   映射没有但场景链与归因链同链时借归因环节，跨链一律「—」不硬编 */
function segOfScene(z){const p=PF(z);
 const cs=(window._TC||{})[z['场景']||z['题材']]||[];
 const sg=cs[1]&&String(cs[1]).indexOf('（')<0?cs[1]:'';
 if(sg)return sg;
 if(p.chain_only&&p.chain_only===(z['场景链']||p.chain_only))return segOf(z);
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
/* 页头滚过后显示细条 + 侧栏滚动高亮（scrollspy：视口顶最近的上一个区块）。
   兼容性：不用 querySelectorAll/getBoundingClientRect 裸调（静态快照回归的 mock DOM 没有它们）*/
const _bar=document.getElementById('bar');
const _secIds=['dualrow','h2p','h2f','h2m','h2c','h2a'];
const _navs={};_secIds.forEach(id=>{_navs[id]=document.getElementById('sn_'+id);});
window.addEventListener('scroll',()=>{
 _bar.classList.toggle('on',window.scrollY>170);
 let cur=_secIds[0];
 for(const id of _secIds){const el=document.getElementById(id);
   if(el&&el.getBoundingClientRect&&el.getBoundingClientRect().top<=100)cur=id;}
 _secIds.forEach(id=>{const a=_navs[id];if(a&&a.classList)a.classList.toggle('cur',id===cur);});
},{passive:true});
function renderLib(L){
 const box=document.getElementById('lib'),mt=document.getElementById('libmeta');
 if(!box)return;
 if(!L||!L.ready){if(mt)mt.textContent='库数据未就绪'+(L&&L.err?'（'+L.err+'）':'');return;}
 if(mt)mt.textContent=`题材 ${L.source_dates?.fact_kpl||L.date||'—'} · 资金 ${L.source_dates?.fact_moneyflow||'—'} · ETF行情 ${L.source_dates?.fact_etf_daily||'—'} · 份额 ${L.source_dates?.fact_etf_share||'—'} · 预加载 ${L.asof||'—'}（非实时资金流）`+
  (L.partitions||[]).filter(p=>p.status!=='ok').map(p=>` · ${p.source}:${p.status} ${p.note||''}`).join('');
 const P=v=>(v>0?'#e07575':(v<0?'#6fae8f':'#9a9aa5'));
 const num=(v,unit)=>`<span style="color:${P(v)};font-variant-numeric:tabular-nums">${v==null?'—':(v>0?'+':'')+v}${unit||''}</span>`;
 const card=(title,inner,extra)=>`<div class="dualcard"><h3>${title}${extra?`<span style="float:right;color:#8a8a95;font-weight:400;font-size:11px">${extra}</span>`:''}</h3>${inner}</div>`;
 const T=(head,rows)=>`<table class="drilltab"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;

 // ① 题材榜
 const th=(L.themes||[]).map(t=>`<tr><td>${t.theme_name}</td><td style="color:#e07575">${t.zt??'—'}</td><td style="color:#8a8a95">${t.hot??'—'}</td></tr>`);
 // ② 订单净流向（流入+流出）
 const fl=(L.flow_in||[]).map(t=>`<tr><td>${t.chain}</td><td>${num(t.yi,'亿')}</td><td style="color:#8a8a95">${t.n}</td></tr>`)
   .concat((L.flow_out||[]).map(t=>`<tr><td style="color:#9a9aa5">${t.chain}</td><td>${num(t.yi,'亿')}</td><td style="color:#8a8a95">${t.n}</td></tr>`));
 // ③ ETF 估算申赎对价
 const et=(L.etf||[]).map(t=>`<tr><td>${t.chain}</td><td>${num(t.yi,'亿')}</td><td style="color:#8a8a95">${t.n_etf}</td></tr>`);
 // ④ ETF篮子关联
 const rs=(L.resonance||[]).map(t=>`<tr><td>${t.name}</td><td style="color:#e07575">+${t.pct}%</td><td style="color:#9fb6cf;font-size:11.5px">${t.etf||''}</td><td>${num(t.etf_pct,'%')}</td></tr>`);
 // ⑤ 同花顺概念榜
 const hs=(L.ths||[]).map(t=>`<tr><td>${t.concept}</td><td style="color:#e07575">${t.zt}</td></tr>`);
 // ⑥ 跨链新簇
 const CX=(window.CROSS&&window.CROSS.length?window.CROSS:(L.cross||[]));
 const cr=CX.map(t=>{
   const nm=(t.members||'').split('、').slice(0,2).join('·');
   const r=(t.ratio==null?null:Number(t.ratio));
   const col=(r==null?'#9a9aa5':(r>=1.5?'#e07575':(r>=1.0?'#d8c07a':'#8a8a95')));
   return `<tr><td title="${t.members||''}">${nm}</td><td style="color:#8a8a95;font-size:11.5px">${t.chains||''}</td><td style="color:#c9c9d2">${t.zt_today??'—'}<span style="color:#5a6270">/${t.zt_avg20??'—'}</span></td><td style="color:${col};font-weight:600">${r==null?'—':'×'+r.toFixed(2)}</td></tr>`;
 });

 let h='<div class="librow">';
 h+=card('题材榜 <span style="color:#5a6270;font-size:11px">开盘啦·按涨停</span>', T(['题材','涨停','人气'],th));
 h+=card('订单净流向 <span style="color:#5a6270;font-size:11px">亿元·按链</span>', T(['链','净额','只数'],fl));
 h+=card('ETF 估算申赎对价 <span style="color:#5a6270;font-size:11px">亿元</span>', T(['链','净申赎','ETF'],et));
 h+='</div><div class="librow">';
 h+=card('ETF篮子关联 <span style="color:#5a6270;font-size:11px">涨停票 ∈ ETF 成分</span>', T(['个股','涨幅','持有它的 ETF','ETF 今日'],rs));
 h+=card('跨链新簇 <span style="color:#5a6270;font-size:11px">加速榜 · 实时</span>', T(['主题簇','跨链','今日/均','倍数'],cr));
 h+='</div>';

 // ⑦ 涨停 20 日柱 + ETF 5 日趋势
 const H=(L.history||[]).slice(-20);
 let hh='';
 if(H.length){
   const mx=Math.max(...H.map(x=>x.n),1);
   let bars='';
   H.forEach((x,i)=>{
     const bh=Math.max(2,Math.round(x.n/mx*46));
     const col=(i===H.length-1)?'#c96a6a':'#4a6a8a';
     bars+=`<rect x="${i*14}" y="${48-bh}" width="9" height="${bh}" fill="${col}" rx="1"><title>${x.date} ${x.n}家</title></rect>`;
   });
   hh+=`<div style="display:flex;align-items:flex-end;gap:10px"><svg width="${H.length*14}" height="52">${bars}</svg>
        <span class="meta">区间 ${Math.min(...H.map(x=>x.n))}–${mx} 家 · 今日 ${H[H.length-1].n}</span></div>`;
 } else hh+='<span class="meta">暂无历史</span>';
 const E5=L.etf5||{};
 if((E5.dates||[]).length>1){
   const by={}; (E5.rows||[]).forEach(r=>{(by[r.chain]=by[r.chain]||{})[r.date]=r.yi;});
   const chs=Object.keys(by).slice(0,6);
   const rows=chs.map(c=>`<tr><td>${c}</td>${E5.dates.map(d=>`<td>${num(by[c][d])}</td>`).join('')}</tr>`);
   hh+=`<div style="margin-top:8px">${T(['链'].concat(E5.dates.map(d=>d.slice(5))),rows)}</div>`;
 }
 const cLine=(L.ths||[]).length
   ? `<div class="meta" style="margin-top:8px">同花顺概念榜（涨停家数）：${(L.ths||[]).map(t=>`<span style="color:#c9c9d2">${t.concept}</span> <span style="color:#e07575">${t.zt}</span>`).join('　')}</div>`
   : '<div class="meta" style="margin-top:8px">同花顺概念榜：今日涨停未命中产业概念</div>';
 h+='<div class="librow">';
 h+=card('涨停家数 <span style="color:#5a6270;font-size:11px">近 20 个交易日</span>', hh+cLine);
 h+='</div>';

 box.innerHTML=h;
 if(mt)mt.textContent=`题材 ${L.source_dates?.fact_kpl||L.date||'—'} · 资金 ${L.source_dates?.fact_moneyflow||'—'} · ETF行情 ${L.source_dates?.fact_etf_daily||'—'} · 份额 ${L.source_dates?.fact_etf_share||'—'} · 预加载 ${L.asof||'—'}（非实时资金流）`+
  (L.partitions||[]).filter(p=>p.status!=='ok').map(p=>` · ${p.source}:${p.status} ${p.note||''}`).join('');
}
function candRow(z){
 const cs=z['候选']||[];
 if(!cs.length) return `<tr class="candline"><td colspan="14"><span class="candhd">同桶未涨停观察候选</span><span class="cm" style="color:#6f7885">暂无：该桶其余票没有「在动」的（盘前池内、非ST、量比≥1.2且额≥3000万；涨幅上限按真实涨停价折算）</span></td></tr>`;
 const items=cs.map(c=>`<span class="candit" onclick="watchCand('${c.code}','${(c.name||'').replace(/'/g,'')}','${(z['题材']||'').replace(/'/g,'')}',${c.pct},${c.price||0})" title="点击记录到今日候选（盘后可回测）"><b>${c.name}</b><span class="cp">+${c.pct}%</span><span class="cm">${c.why||''}</span></span>`).join('');
 return `<tr class="candline"><td colspan="14"><span class="candhd">下一棒候选（同桶·未涨停·在动）</span>${items}`+
        `<span class="cm" style="color:#4f5866;margin-left:10px">点该行收起</span></td></tr>`;
}
function watchCand(code,name,scene,pct,price){
 fetch('/api/watch',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({code,name,scene,pct,price})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){ toast(`已记录 ${name}（今日共 ${d.n} 只）`); }
    else { toast('记录失败：'+(d.err||'')); }
  }).catch(()=>toast('记录失败（网络）'));
}
function toast(msg){
 let el=document.getElementById('_toast');
 if(!el){el=document.createElement('div');el.id='_toast';el.className='toast';document.body.appendChild(el);}
 el.textContent=msg; el.style.opacity=1;
 clearTimeout(el._t); el._t=setTimeout(()=>{el.style.opacity=0},1800);
}
function bindCand(){
 const tb=document.getElementById('tb4');
 if(!tb||tb._candBound) return;
 tb._candBound=1;
 tb.addEventListener('click',e=>{
  const tr=e.target.closest('tr[id^="pk_"]');
  if(!tr) return;
  const nx=tr.nextElementSibling;
  if(nx&&nx.classList.contains('candline')){nx.remove();tr.classList.remove('cxopen');return;}
  const z=(window.PK||[]).find(x=>('pk_'+x.code)===tr.id);
  if(!z) return;
  tr.insertAdjacentHTML('afterend',candRow(z));
  tr.classList.add('cxopen');
 });
}
async function tick(){
 const _age=(d)=>(Date.now()-new Date(d.taken_at.replace(' ','T')).getTime())/1000;
 try{
  const d=await (await fetch('/api/state?ts='+Date.now(), {cache:'no-store'})).json();
  if(d.booting){document.getElementById('st').textContent='启动中… '+((d.warn||[]).join(' ｜ ')||'正在拉取第一份快照');
   const _w=document.getElementById('wn');if(_w)_w.innerHTML=(d.warn||[]).join(' ｜ ');return;}
  document.getElementById('dd').textContent=`股池数据日 ${d.data_date}`;
  document.getElementById('st').textContent=
   `${d.taken_at} · 源 ${d.src} · ${d.n_snap} 只 · 市场中位 ${f(d.mkt_med_pct,'%')} · `+
   `${d.slot}${d.standard?'':'（非标）'} · ${d.clock_hint.split('——')[0]}`+
   (_age(d)>60?` ⚠⚠ 数据已陈旧 ${Math.round(_age(d))} 秒——服务可能已停止`:'');
  document.getElementById('wn').innerHTML=(d.warn||[]).join(' ｜ ');
  if(d.ths_map){window.THSMAP=d.ths_map;}
  bindCand();
  const _wn=document.getElementById('watchn');
  if(_wn&&d.watch_n!==undefined)_wn.innerHTML=d.watch_n?`<b style="color:#e8b96a">今日已记录 ${d.watch_n} 只候选</b> ｜ `:'';
  if(d.cross_live){window.CROSS=d.cross_live;}
  try{renderLib(d.lib||{})}catch(e){}
  /* 侧栏速读 + 时刻（滚动时始终可见——「短时间全面了解」的锚点） */
  const _dd=d.dual||{},_L=_dd['灯']||{},_A=_dd.A||{},_B=_dd.B||{};
  const _sq=document.getElementById('squick');
  if(_sq)_sq.innerHTML=
   `<span class="sl">组合灯</span><span class="sch" style="background:${_L['色']||'#5a6270'}">${_L['名']||'—'}</span>`+
   `<span class="sl">涨停 / 炸板率</span><b>${_A['涨停']??'—'}</b> 家 / <b>${_A['炸板率']??'—'}%</b>`+
   `<span class="sl">一进二 / 首板溢价</span><b>${_A['一进二']!=null?_A['一进二']+'%':'—'}</b> / <b>${_A['首板溢价']!=null?(_A['首板溢价']>0?'+':'')+_A['首板溢价']+'%':'—'}</b>`+
   `<span class="sl">最强链</span>${_B['top链']||'—'}`+
   (d.cm_cov?`<span class="sl">概念归位</span>${d.cm_cov.total-d.cm_cov.todo}/${d.cm_cov.total}（待归类 ${d.cm_cov.todo}）`:'');
  const _st2=document.getElementById('stime');if(_st2)_st2.textContent=d.taken_at||'';
  const _bl=document.getElementById('blamp');
  if(_bl)_bl.innerHTML=_L['名']?`<span class="sch" style="background:${_L['色']||'#5a6270'};display:inline-block">${_L['名']}</span>`:'';
  /* 吸顶细条同步（2026-10-24）：滚动时只剩这一行，时间/指数/情绪不能跟着页头滚走 */
  const se0=d.sentiment||{};
  document.getElementById('bst').textContent=
   `${d.taken_at} · ${d.slot}${d.standard?'':'（非标）'} · ${d.n_snap} 只`+
   (_age>60?` ⚠⚠ 陈旧 ${Math.round(_age)} 秒`:'');
  document.getElementById('bidx').innerHTML=(d.indices||[])
   .map(x=>{const c=x.pct>0?'up':x.pct<0?'dn':'';return `${x.name} <span class="${c}">${x.pct>0?'+':''}${x.pct}%</span>`;}).join('  ');
  document.getElementById('bsent').innerHTML= se0['涨停']!==undefined?
   `涨停 <b>${se0['涨停']}</b> · 炸板 <b>${se0['炸板']}</b> · 跌停 <b>${se0['跌停']}</b> · 最高 <b>${Math.trunc(se0['最高连板']||0)}板</b>`:'';
  const r=d.focus.filter(x=>x.exc!==null).sort((a,b)=>a.rank-b.rank).slice(0,14);
  const pre=(x)=>{const p=x.pre||{};return [f(p.pre_rank),f(p.pre_zljlr,'亿'),f(p.pre_ret5,'%'),f(p.pre_run),`${f(p.pre_cont)}<br>${f(p.pre_lc)}`];};
  const tb1row=(x)=>{const p=pre(x);return `<tr class="${{'重点':'t-zd','观察':'t-gc','环节':'t-hj'}[x.tier]}"><td>${tag(x.tier)}</td>`+
   `<td>${x.name}</td><td>${f(x.eq_pct,'%')}</td><td>${f(x.exc,'%')}</td><td>${x.zt}</td>`+
   `<td>${lad(x.ladder)}</td>`+
   `<td>${f(x.ups)}</td><td>${f(x.lb)}</td><td>${f(x.zljlr_yi,'亿')}</td>`+
   `<td>#${x.pre_rank}→#${x.rank} ${arr(x.delta)}</td>`+
   p.map(v=>`<td class="pre">${v}</td>`).join('')+'</tr>';};
  /* 2026-10-25 用户要求：题材很多同属一个大类 → 像盘口质量那样先按链分组、链内再按环节细分 */
  let _h1=row(['档位','题材','涨','超额','涨停','梯队','涨股比','量比','资金','盘前→现在',
        '盘前·排名','盘前·主力','盘前·自算5日','盘前·连胜','盘前·连续性/生命周期']);
  const _tc=d.theme_chain||{};
  if(!Object.keys(_tc).length){_h1+=r.map(tb1row).join('');}
  else{
    const gm=new Map();
    r.forEach(x=>{const cs=_tc[x.name]||[];const ch=cs[0]||'（未归链）';
      const sg=(cs[1]&&String(cs[1]).indexOf('（')<0)?cs[1]:'—';
      if(!gm.has(ch))gm.set(ch,new Map());
      const m=gm.get(ch);if(!m.has(sg))m.set(sg,[]);m.get(sg).push(x);});
    const gs=[];
    gm.forEach((m,ch)=>{const a2=[];m.forEach(v=>a2.push(...v));
      gs.push({ch,m,arr:a2,zt:a2.reduce((s,x)=>s+(x.zt||0),0),
               exc:Math.max(...a2.map(x=>x.exc||0)),n:a2.length});});
    gs.sort((a,b)=>b.zt-a.zt||b.exc-a.exc);
    gs.forEach(g=>{
      _h1+=`<tr class="gh h2"><td colspan="15">◤ ${g.ch}<span class="ghn"> ${g.n} 条 · 涨停合计 ${g.zt} · 最高超额 ${f(g.exc,'%')}</span></td></tr>`;
      const ss=[];g.m.forEach((xs,sg)=>ss.push({sg,xs,exc:Math.max(...xs.map(x=>x.exc||0))}));
      ss.sort((a,b)=>b.exc-a.exc);
      ss.forEach(o=>{
        if(o.sg!=='—')_h1+=`<tr class="gs"><td colspan="15">· ${o.sg}<span class="ghn"> ${o.xs.length} 条 · 最高超额 ${f(o.exc,'%')}</span></td></tr>`;
        _h1+=o.xs.map(tb1row).join('');
      });
    });
  }
  document.getElementById('tb1').innerHTML=_h1;
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
  /* 2026-10-25 二改（用户要求）：候选表也按「场景链（大类）→ 环节（细分）」分组，
     组头给 只数/已动潜伏/梯队成立/最高成色；组内先已动（梯队→成色→涨幅）再潜伏（低位→量比） */
  const posRk=x=>x['位置']==='低位'?0:x['位置']==='中位'?1:2;
  const candRow=x=>{const st=x['状态']==='已动'
      ?'<span class="rl" style="background:#b04a4a">🔥已动</span>'
      :'<span class="rl" style="background:#3a7abf">💤潜伏</span>';
    const vl=x['量比注']||f(x['量比']);
    const r5=x.ret5!==undefined?f(x.ret5,'%'):'—';
    const pos=x['位置']?`<span class="pos ${x['位置']==='低位'?'p-lo':x['位置']==='中位'?'p-md':'p-hi'}">${x['位置']}</span>`:'—';
    const off=chainOff(x);
    const cs=x['成色']!=null?` <span class="pos p-md">成色${x['成色']}</span>`:'';
    return `<tr class="${x['池层']==='核心'?'t-zd':'t-gc'}"><td>${st}${cs}</td><td>${tag(x['池层'])}</td>`+
     `<td>${x.name}</td>${cCore(x)}${cAttr(x)}${cLay(x)}${cBiz(x)}${sceneCell(x,{head:false,off:off})}<td>${f(x.pct,'%')}</td>`+
     `<td>${vl}</td><td>${r5}</td><td>${brk(x['突破'])}</td><td>${f(x['涨速'])}</td>`+
     `<td>${f(x['额亿'],'亿')}</td><td>${pos}</td></tr>`;};
  const _gm2=new Map();
  cand.forEach(x=>{const p=PF(x);const ch=x['场景链']||p.chain_only||'（未归链）';
    const sg=segOfScene(x);
    if(!_gm2.has(ch))_gm2.set(ch,new Map());
    const m=_gm2.get(ch);if(!m.has(sg))m.set(sg,[]);m.get(sg).push(x);});
  const _gs2=[];_gm2.forEach((m,ch)=>{const a2=[];m.forEach(v=>a2.push(...v));
    _gs2.push({ch,m,arr:a2,n:a2.length,fired:a2.filter(x=>x['状态']==='已动').length,
      est:a2.filter(x=>x['梯队']==='成立').length,best:Math.max(...a2.map(x=>x['成色']||0))});});
  _gs2.sort((a,b)=>b.n-a.n||b.best-a.best);
  let _h2=row(['状态','池层','名称','核心业务（占比·毛利）','今日归因（分）','错位度','主业链·环节','场景（主归属）·梯队','涨幅','量能','5日','突破','涨速','额(亿)','位置']);
  _gs2.forEach(g=>{
    _h2+=`<tr class="gh h2"><td colspan="15">◤ ${g.ch}<span class="ghn"> ${g.n} 只 · 已动${g.fired}/潜伏${g.n-g.fired} · 梯队成立${g.est} · 最高成色 ${g.best||'—'}</span></td></tr>`;
    const ss=[];g.m.forEach((xs,sg)=>{
      xs.sort((a,b)=>(a['状态']==='已动'?0:1)-(b['状态']==='已动'?0:1)
        ||((b['成色']||0)-(a['成色']||0))||(posRk(a)-posRk(b))||((b.pct||0)-(a.pct||0)));
      ss.push({sg,xs,best:Math.max(...xs.map(x=>x['成色']||0))});});
    ss.sort((a,b)=>b.xs.length-a.xs.length||b.best-a.best);
    ss.forEach(o=>{
      if(o.sg!=='—')_h2+=`<tr class="gs"><td colspan="15">· ${o.sg}<span class="ghn"> ${o.xs.length} 只 · 最高成色 ${o.best||'—'}</span></td></tr>`;
      _h2+=o.xs.map(candRow).join('');
    });
  });
  document.getElementById('tb2').innerHTML=_h2;
  const a=d.alignment;
  document.getElementById('al').innerHTML=
   `盘前 top10 重合 <b>${a.overlap}</b> · 在盘前全部焦点内 <b>${a.overlap40}</b>/10 → ${a.verdict}`;
  const cu=d.catchup||[];
  /* 2026-10-25 二改（用户要求）：补涨表同 tb2——按「场景链（大类）→ 环节」分组，组头给只数/场景数/最高成色 */
  const cuRow=(x)=>{const off=chainOff(x);
    return `<tr class="${x['池层']==='核心'?'t-zd':'t-gc'}"><td>${tag(x['池层'])}</td>`+
    `<td>${x.name}</td>${cCore(x)}${cAttr(x)}${cLay(x)}${cBiz(x)}${sceneCell(x,{head:false,off:off})}`+
    /* 龙头格自带桶名（2026-10-24 用户把金百泽行的龙头「大业股份」误读成 PCB 链的人）：
       没有桶名，读者会拿左边的主业链去解释这个名字——加上「·机器人概念桶」就从根上断掉误读。
       2026-10-25 优化：龙头炸板次数 ⚠（炸板不回封＝补涨逻辑失效的先行信号）＋桶内涨停家数 */
    `<td>${x['龙头']||'—'}${x['龙头板']?` <span class="mt">${x['龙头板']}板</span>`:''}`+
    `${x['龙头炸板']?` <span class="apx" title="场景龙头今日炸板 ${x['龙头炸板']} 次——龙头炸板不回封则补涨逻辑失效（纪律：龙头断板当天补涨必须走）">⚠炸${x['龙头炸板']}</span>`:''}`+
    `${x['场景涨停']?` <span class="mt">·桶内涨停${x['场景涨停']}家</span>`:''}`+
    `${x['场景']?` <span class="mt">·${x['场景']}桶</span>`:''}`+
    `${x['龙头']&&x['龙头']===x.name?' <span class="mt">（本票）</span>':''}</td>`+
    `<td>${f(x.pct,'%')}</td><td>${f(x.ret5,'%')}</td><td>${f(x['换手'])}</td></tr>`;};
  let _h5=row(['池层','名称','核心业务（占比·毛利）','今日归因（分）','错位度','主业链·环节','补涨场景（主归属·家数·判定）','场景龙头（属哪个桶）','涨幅','5日','换手']);
  if(!cu.length){
    _h5+='<tr><td colspan="11">（暂无——需题材前排已起且池内有低位未涨票）</td></tr>';
  }else{
    const _gm5=new Map();
    cu.forEach(x=>{const p=PF(x);const ch=x['场景链']||p.chain_only||'（未归链）';
      const sg=segOfScene(x);
      if(!_gm5.has(ch))_gm5.set(ch,new Map());
      const m=_gm5.get(ch);if(!m.has(sg))m.set(sg,[]);m.get(sg).push(x);});
    const _gs5=[];_gm5.forEach((m,ch)=>{const a5=[];m.forEach(v=>a5.push(...v));
      const scenes=new Set(a5.map(x=>x['场景']||''));
      _gs5.push({ch,m,n:a5.length,scenes:scenes.size,
        best:Math.max(...a5.map(x=>x['成色']||0)),
        ztmax:Math.max(...a5.map(x=>x['场景涨停']||0))});});
    _gs5.sort((a,b)=>b.ztmax-a.ztmax||b.n-a.n);
    _gs5.forEach(g=>{
      _h5+=`<tr class="gh h2"><td colspan="11">◤ ${g.ch}<span class="ghn"> ${g.n} 只 · ${g.scenes} 个场景 · 场景最多涨停 ${g.ztmax||'—'} 家 · 最高成色 ${g.best||'—'}</span></td></tr>`;
      const ss=[];g.m.forEach((xs,sg)=>ss.push({sg,xs,
        best:Math.max(...xs.map(x=>x['成色']||0))}));
      ss.sort((a,b)=>b.xs.length-a.xs.length||b.best-a.best);
      ss.forEach(o=>{
        if(o.sg!=='—')_h5+=`<tr class="gs"><td colspan="11">· ${o.sg}<span class="ghn"> ${o.xs.length} 只 · 最高成色 ${o.best||'—'}</span></td></tr>`;
        _h5+=o.xs.map(cuRow).join('');
      });
    });
  }
  document.getElementById('tb5').innerHTML=_h5;
  const pk=d.pankou||[];
  window.PK=pk;
  window._TC=d.theme_chain||{};
  window._CLAN=d.name_clan||{};
  window._CCLS=d.concept_cls||{};
  renderDual(d.dual);
  document.getElementById('h2p').textContent=
   `盘口质量（显示 ${pk.length} 只 / 全市场涨停 ${(d.sentiment||{})['涨停']??'—'} 只 · 肉肉 4.1/4.2/5.2）`;
  renderPk();
 }catch(e){document.getElementById('st').textContent='连接失败（服务已停止？）：'+e;}
}
/* ── 双闸对照（v2，2026-10-25 方案；C 级草案）── 闸A/闸B 两卡平级并排，组合判定条在下 ──
   闸A＝市场级盘口质量（答"能不能做/给多少仓"）；闸B＝板块级方向强度（答"做哪里"，
   板块链/环节/题材三层各列最强者）。两闸平级互验（熔炼 3.5）；A 闸极端弱时一票否决。
   只出读数与组合命名，不是买卖指令。*/
function chip(k,v,cls){return `<span class="chip ${cls||''}">${k}<b>${v==null||v===''?'—':v}</b></span>`;}
/* 闸B 榜单 chip：可点击——按维度切到盘口表对应分组并跳转高亮（2026-10-25 用户要求） */
function rankChips(arr,mode){const RK='①②③④⑤';return (arr||[]).map((x,i)=>
 `<span class="chip ${i===0?'hi':''}" style="cursor:pointer" onclick="jumpGrp('${mode}','${x[0]}')">${RK[i]||''} ${x[0]}<b>${x[1]}</b></span>`
).join('')||'—';}
/* 闸B 榜 → 盘口表：按维度切到对应分组模式（链/环节/题材），滚动到该组并高亮 */
function jumpGrp(mode,name){
 GRP=mode;mountCtl();renderPk();
 setTimeout(()=>{
  const tb=document.getElementById('tb4');
  let hit=null;
  if(tb&&tb.querySelectorAll){
   const hs=tb.querySelectorAll('tr.gh');
   for(let i=0;i<hs.length;i++){const t=hs[i].textContent||'';
     if(t.indexOf(name)>=0){hit=hs[i];break;}}
  }
  if(hit){
   if(hit.scrollIntoView)hit.scrollIntoView({behavior:'smooth',block:'center'});
   hit.classList.add('hlrow');setTimeout(()=>hit.classList.remove('hlrow'),2600);
  }else{
   const el=document.getElementById('h2p');
   if(el&&el.scrollIntoView)el.scrollIntoView({behavior:'smooth',block:'start'});
  }
 },80);
}
function distChips(arr,unit){return (arr||[]).filter(x=>x[1]>0).map(x=>chip(x[0],x[1]+(unit||'家'),'')).join('')||'—';}
/* ══════ 涨停结构面板（2026-10-25 调研后重构）══════
   依据：信息分层（KPI 摘要 → 分布图 → 明细表）、渐进式披露、分布用条形看形状、
   点击条形下钻＋一键重置。原先把 82 行明细塞进 KPI 卡里＝把第三层塞进第一层，
   结果两卡失衡、信息过载——现在：双闸卡只留读数，结构面板单独承载分布＋明细。*/
let DRILL={dim:'',lab:''};
let TABLE_OPEN=true;
/* 明细表排序（2026-10-25 用户要求：每一列都能排；名称支持「按字辈族」——
   炒名字/字辈/谐音/生肖是 A 股真实存在的短线路径，族谱见 scripts/name_clan.py）*/
let SORT={col:'score',mode:'desc'};
const SMODES={name:['asc','desc','clan'],theme:['group','asc','desc'],
              seg:['group','asc','desc'],ladder:['desc','asc'],fbt:['asc','desc'],score:['desc','asc']};
function sortBy(col){
 const ms=SMODES[col]||['desc','asc'];
 const i=(SORT.col===col)?ms.indexOf(SORT.mode):-1;
 SORT={col:col,mode:ms[(i+1)%ms.length]};
 renderStruct();
}
function sortMark(col){
 if(SORT.col!==col)return '';
 const m=SORT.mode;
 return m==='clan'?' <span class="mt">⭮族</span>':m==='group'?' <span class="mt">▤组</span>':m==='asc'?' ▲':' ▼';
}
function clanOf(c){const g=(window._CLAN||{})[c]||{};return g['族']||'';}
function clanTags(c){const g=(window._CLAN||{})[c]||{};const t=(g['标签']||[]).slice(0,3);
 return t.length?` <span class="mt">${t.join('·')}</span>`:'';}          // 明细表默认展开（用户 2026-10-25 要求；点「折叠」收起）
function toggleTable(){TABLE_OPEN=!TABLE_OPEN;renderStruct();}
function drill(dim,lab){
 DRILL=(DRILL.dim===dim&&DRILL.lab===lab)?{dim:'',lab:''}:{dim:dim,lab:lab};
 renderStruct();
}
function jumpPk(c){
 const el=document.getElementById('pk_'+c);
 if(!el)return;
 if(el.scrollIntoView)el.scrollIntoView({behavior:'smooth',block:'center'});
 if(el.classList){el.classList.add('hlrow');setTimeout(()=>el.classList.remove('hlrow'),2400);}
}
/* 横向条形（分布形状一眼可见；0 值也画，形状不断裂） */
function bars(arr,dim,unit,total){
 const mx=Math.max(1,...(arr||[]).map(x=>x[1]||0));
 return `<div class="bars">`+(arr||[]).map(x=>{
  const on=(DRILL.dim===dim&&DRILL.lab===x[0]);
  const w=Math.max(2,Math.round((x[1]||0)/mx*100));
  return `<div class="bar${on?' on':''}" onclick="drill('${dim}','${x[0]}')">`+
   `<span class="bl">${x[0]}</span><span class="btrack"><span class="bfill" style="width:${w}%"></span></span>`+
   `<span class="bn">${x[1]||0}${unit||''}${total?`<em class="bpct">${Math.round((x[1]||0)/total*100)}%</em>`:''}</span></div>`;}).join('')+`</div>`;
}
function renderStruct(){
 const box=document.getElementById('struct');if(!box)return;
 const d=window._DUAL||{},A=d.A||{};
 const pk=window.PK||[];
 const segOfLoc=z=>{const p=String(z['定位']||'').split('·');return p.length>1?p[1]:'';};
 const lbTxt=v=>{const n=parseInt(v);return isNaN(n)?'—':(n>1?n+'板':'首板');};
 const mk=z=>({c:z.code,n:z.name||'—',t:z['题材']||'',sg:segOfLoc(z),
   lb:lbTxt(z['连板']),fs:z['首封']||'',p:z['涨幅'],cs:z['成色']});
 let rows,head;
 if(!DRILL.dim){
   rows=pk.map(mk);
   head=`<b>全部涨停 ${rows.length} 只</b> · 按成色排序 · 点上方条形筛选`;
 }else{
   const det=DRILL.dim==='ladder'?(A['梯队明细']||{}):DRILL.dim==='fbt'?(A['首封明细']||{}):(A['首封未知明细']||[]);
   const arr=Array.isArray(det)?det:(det[DRILL.lab]||[]);
   const pm={};pk.forEach(z=>{pm[z.code]=z;});
   rows=arr.map(pair=>{const z=pm[pair[0]];return z?mk(z):{c:pair[0],n:pair[1]||pair[0],t:'',sg:'',lb:'',fs:'',p:null,cs:null};});
   const nm=DRILL.dim==='ladder'?'连板梯队':DRILL.dim==='fbt'?'首封时段':'首封未知';
   head=`${nm} · <b>${DRILL.lab||''}</b> ${rows.length} 只 · 再点该条形或点「重置」回全量`;
 }
 rows.sort((a,b)=>((b.cs??0)-(a.cs??0))||((b.p??0)-(a.p??0)));
 const TOT=(A['涨停']||0)||1;
 const chart=(title,arr,dim)=>`<div>`+
   `<div class="dualnote" style="margin:0 0 5px">${title}</div>${bars(arr,dim,'',TOT)}</div>`;
 box.innerHTML=
  `<div class="structone"><div class="so-hd">涨停结构 · 高度 × 时段`+
   `<span>括号为占全部 ${A['涨停']??'—'} 家的比例 · 点条形筛选</span></div>`+
   `<div class="structgrid">`+
    chart('连板梯队（家数）',A['梯队长']||[],'ladder')+
    chart('首封时段（家数 · 越早越强）',A['首封分布']||[],'fbt')+
  `</div></div>`+
  `<div class="dualnote" style="margin:10px 0 4px">${head}`+
   `<a class="dmore" href="javascript:void(0)" onclick="drillAll()">重置（全部）</a>`+
   `<a class="dmore" href="javascript:void(0)" onclick="toggleTable()">${TABLE_OPEN?'折叠明细':'展开明细'}</a></div>`+
  (TABLE_OPEN?`<div class="drillwrap" style="max-height:250px"><table class="drilltab"><thead><tr>`+
   [`name:名称`,`theme:题材（主归属）`,`seg:环节`,`ladder:梯队`,`fbt:首封`,`score:成色`]
     .map(x=>{const i=x.indexOf(':');return `<th class="sortth" onclick="sortBy('${x.slice(0,i)}')" `+
       `title="点列头切换排序（名称含「按字辈族」模式）">${x.slice(i+1)}${sortMark(x.slice(0,i))}</th>`;}).join('')+
   `</tr></thead><tbody>`+ _tbBody(rows) +`</tbody></table></div>`:'');
}
function _tbBody(rows){
 if(!rows.length)return '<tr><td colspan="6">（无）</td></tr>';
 const rowH=r=>`<tr><td><a class="dname" href="#pk_${r.c}" onclick="jumpPk('${r.c}');return false;">${r.n}</a>${clanTags(r.c)}</td>`+
   `<td>${r.t||'—'}</td><td class="mt">${r.sg||'—'}</td><td class="mt">${r.lb||'—'}</td><td class="mt">${r.fs||'—'}</td>`+
   `<td>${r.cs??'—'}</td></tr>`;
 const num=v=>{const m=String(v).match(/([0-9]+)/);return m?parseInt(m[1]):1;};
 const gsep=(k,n,best)=>`<tr class="gs"><td colspan="6">◤ ${k}（${n} 只）<span class="ghn"> 最高成色 ${best||'—'}</span></td></tr>`;
 const grp=(keyOf,unlab)=>{
   const m=new Map();
   rows.forEach(r=>{const k=keyOf(r)||unlab;if(!m.has(k))m.set(k,[]);m.get(k).push(r);});
   const gs=[...m.entries()].map(([k,arr])=>({k,arr,best:Math.max(...arr.map(x=>x.cs??0))}));
   gs.sort((a,b)=>((a.k===unlab?1:0)-(b.k===unlab?1:0))||b.arr.length-a.arr.length||b.best-a.best);
   return gs.map(g=>gsep(g.k,g.arr.length,g.best)+g.arr.sort((x,y)=>(y.cs??0)-(x.cs??0)||((y.p??0)-(x.p??0))).map(rowH).join('')).join('');
 };
 if(SORT.col==='name'&&SORT.mode==='clan') return grp(r=>clanOf(r.c),'（未成族）');
 if(SORT.col==='theme'&&SORT.mode==='group') return grp(r=>r.t,'（无题材）');
 if(SORT.col==='seg'&&SORT.mode==='group') return grp(r=>r.sg,'—');
 const cmp={
  name:(a,b)=>String(a.n).localeCompare(String(b.n),'zh-Hans-CN'),
  theme:(a,b)=>String(a.t).localeCompare(String(b.t),'zh-Hans-CN'),
  seg:(a,b)=>String(a.sg).localeCompare(String(b.sg),'zh-Hans-CN'),
  ladder:(a,b)=>num(a.lb)-num(b.lb),
  fbt:(a,b)=>String(a.fs).localeCompare(String(b.fs)),
  score:(a,b)=>(a.cs??0)-(b.cs??0)
 }[SORT.col]||((a,b)=>(a.cs??0)-(b.cs??0));
 const dir=SORT.mode==='asc'?1:-1;
 rows.sort((a,b)=>cmp(a,b)*dir);
 return rows.map(rowH).join('');
}
function drillAll(){DRILL={dim:'',lab:''};renderStruct();}
function pctS(v){return v==null?null:(v>0?'+':'')+v+'%';}
function renderDual(d){
 const eA=document.getElementById('dualA'),eB=document.getElementById('dualB'),eC=document.getElementById('dualcombo');
 if(!eA)return;
 if(!d||!d.A){eA.innerHTML=eB.innerHTML='';if(eC)eC.innerHTML='';return;}
 window._DUAL=d;
 const A=d.A,B=d.B,L=d['灯']||{};
 const ajc={'强':'#4e7a33','中':'#6b6b73','弱':'#a32d2d'}[A['判定']]||'#6b6b73';
 const bjc={'集中':'#4e7a33','中':'#6b6b73','分散':'#a4742a'}[B['判定']]||'#6b6b73';
 /* 闸A 重排（2026-10-27 用户：20 个同权重 chip 一扫过去看不出重点）
    → 大数（涨停家数）+ 三条相对量表（炸板率/一进二/首板溢价）+ 四个关键计数
    量表颜色只在突破阈值时才染色（好=绿、坏=红），中性保持灰——不制造无意义的红绿。*/
 const _mtr=(lab,v,o)=>{
  o=o||{};
  const has=(v!=null&&!isNaN(v));
  const mx=o.max||100;
  const w=has?Math.max(2,Math.min(100,Math.abs(v)/mx*100)):0;
  const cls=!has?'':(o.inv?(v>o.bad?'bad':(v<=o.good?'good':'')):(v>=o.good?'good':(v<o.bad?'bad':'')));
  return `<div class="mtr ${cls}"${o.tip?` title="${o.tip}"`:''}><span class="ml">${lab}</span>`+
   `<span class="mtrack"><i style="width:${w}%"></i></span>`+
   `<em>${has?((o.sign&&v>0?'+':'')+v+(o.unit||'')):'—'}</em></div>`;
 };
 const _f=(k,v,cls)=>`<span class="fct ${cls||''}">${k}<b>${v==null?'—':v}</b></span>`;
 eA.innerHTML=`
  <h3>闸A 盘口质量（市场级 → 能不能做）　<span style="color:${ajc}">${A['判定']}</span><span style="color:#6f7885;font-weight:400">　分 ${A['分']??'—'}</span></h3>
  <div class="gA">
   <div class="gA-big">
    <b>${A['涨停']??'—'}</b><i>家涨停</i>
    <u>近5日均 ${A['近5日均涨停']??'—'}${A['今日倍数']!=null?' · ×'+A['今日倍数']:''}</u>
   </div>
   <div class="gA-mtr">
    ${_mtr('炸板率',A['炸板率'],{max:100,good:20,bad:40,inv:true,unit:'%',tip:'炸板(含回封)/尝试封板'})}
    ${_mtr('一进二',A['一进二'],{max:100,good:50,bad:30,unit:'%',tip:'样本 n='+(A['一进二样本']??'—')})}
    ${_mtr('首板溢价',A['首板溢价'],{max:6,good:3,bad:0,unit:'%',sign:true,tip:'昨日首板今日等权涨幅'})}
   </div>
   <div class="gA-kpi">
    <div>最高连板<b>${A['最高连板']??'—'} 板</b></div>
    <div>连板家数<b>${A['连板家数']??'—'}</b></div>
    <div>大面 ≤−8%<b>${A['大面']??'—'}</b></div>
    <div>跌停/封死<b>${A['跌停']??'—'}/${A['封死跌停']??'—'}</b></div>
   </div>
  </div>
  <div class="gA-facts">
   ${_f('昨日涨停（'+(A['昨日日期']||'—')+'）',(A['昨日涨停']??'—')+' 家')}
   ${_f('二进三以上',(A['二进三以上']??'—')+(A['连板晋级样本']?'%（n='+A['连板晋级样本']+'）':''))}
   ${_f('连板溢价',pctS(A['连板溢价']),(A['连板溢价']!=null&&A['连板溢价']>=0)?'good':'bad')}
   ${_f('回封率',A['回封率']!=null?A['回封率']+'%':'—')}
   ${_f('梯队形状',A['梯队形状']||'—',(A['梯队形状']&&A['梯队形状'].indexOf('菱形')>=0)?'bad':'')}
   ${_f('首封 上午/下午',(A['上午封板']??'—')+' / '+(A['下午封板']??'—'))}
   ${_f('首封未知',A['首封未知']??'—')}
  </div>
  <div class="structinner" id="struct"></div>
  <div class="dualnote">A 闸评分构成：${A['构成']||'—'}　·　三条量表＝相对量纲（越靠右越极端），点下方条形可下钻到个股</div>`;
 renderStruct();
 eB.innerHTML=`
  <h3>闸B 方向强度（板块级 → 做哪里）　<span style="color:${bjc}">${B['判定']}</span></h3>
  <div class="dualnote" style="margin:0 0 4px">链条强度</div>
  <div class="chips">${rankChips(B['链榜'],'chain')}</div>
  <div class="dualnote" style="margin:7px 0 4px">环节强度</div>
  <div class="chips">${rankChips(B['环节榜'],'seg')}</div>
  <div class="dualnote" style="margin:7px 0 4px">题材强度</div>
  <div class="chips">${rankChips(B['题材榜'],'theme')}</div>
  <div class="chips" style="margin-top:9px">
   ${chip('TOP1 占全市场涨停',B['集中度']!=null?B['集中度']+'%':null)}
   ${chip('龙头',(B['龙头']?B['龙头']+(B['龙头板']?'·'+B['龙头板']+'板':''):null))}
   ${chip('梯队',B['梯队判定']||null)}
   ${chip('扩散（最强题材成员涨超5%）',B['扩散']||null)}
  </div>
  <div class="dualnote">三个维度各出 TOP5——第 1 名之外看第 2—5 名，才分得清「一家独大」还是「多点开花」；<b>口径＝归因计数·每票只算一次</b>，与下方盘口表「归因链→环节」分组逐一对得上（点榜单直接跳过去）；龙头＝该链内连板最高，梯队＝该链涨停家数（≥3 成立/2 偏薄/1 独木）</div>`;
 if(eC)eC.innerHTML=`
  <span class="duallamp" style="background:${L['色']||'#5a6270'}">组合：${L['名']||'—'}</span>
  <span class="meta" style="margin-left:10px">闸A × 闸B 平级互验（熔炼 3.5）· 判定全部 C 级草案</span>
  <div class="dualact">动作倾向：${L['动作']||'—'}</div>
  ${(d['背离']||[]).map(f=>`<div class="dualflag">背离：${f}</div>`).join('')}
  <div class="dualnote">${d['note']?('数据说明：'+d['note']+'。'):''}阈值全为经验统计（C 级），只作纵向对比不作绝对判据；本条只给读数与组合命名，不构成交易指令。</div>`;
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
        def do_POST(self):
            if not self.path.startswith("/api/watch"):
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                board.record_watch(body)
                b = json.dumps({"ok": True, "n": len(board.watches_today())}, ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as e:
                b = json.dumps({"ok": False, "err": str(e)[:120]}, ensure_ascii=False).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers(); self.wfile.write(b)
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
