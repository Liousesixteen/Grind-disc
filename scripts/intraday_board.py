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
from pathlib import Path

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
        # 实时成交额按链（2026-10-27）：代码→主业链，启动时读一次，刷新只做查表
        self.code2chain = {}
        try:
            import sqlite3 as _sq
            _c = _sq.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=5)
            for _code, _ch in _c.execute(
                    "SELECT src_id, dst_id FROM rel_edge WHERE rel_type='stock_chain' AND role='主业'"):
                self.code2chain[str(_code)] = _ch
            _c.close()
        except Exception:
            pass
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

    # ── 盘中主力资金流（东财 f62 口径，与 Tushare moneyflow 同源；失败保留旧值）──
    def fetch_flow_em(self):
        import requests as _rq
        out = {"ts": time.strftime("%H:%M:%S")}
        for tag, fs in (("ind", "m:90+t:2"), ("con", "m:90+t:3")):
            try:
                r = _rq.get("https://push2.eastmoney.com/api/qt/clist/get", params={
                    "pn": 1, "pz": 12, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f62",
                    "fs": fs, "fields": "f12,f14,f62,f184"}, timeout=5)
                arr = r.json().get("data", {}).get("diff", [])
                arr = list(arr.values()) if isinstance(arr, dict) else arr
                out[tag] = [{"name": x.get("f14"), "net": round((x.get("f62") or 0) / 1e8, 2),
                             "rate": x.get("f184")} for x in arr if x.get("f62") is not None]
            except Exception:
                return None      # 任一失败→整体放弃，用旧值
        return out

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
        if os.environ.get("TRADING_BOARD_LOG_EXPOSURES", "1") != "1":
            return
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
            # 题材→成分（供前端用实时涨停池重算「实时涨停数」）
            try:
                _km = {}
                for _t, _c in con.execute("SELECT theme_name, code FROM fact_kpl WHERE date=?", (d,)):
                    if _t:
                        _km.setdefault(_t, []).append(str(_c))
                out["kpl_map"] = _km
            except Exception:
                out["kpl_map"] = {}
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
            out["main_theme"] = R.get("main_theme", {})
            watched = {w.get("code") for w in self.watches_today()}
            out["watch_quotes"] = {
                r["code"]: {k: r.get(k) for k in ("price", "pct", "quote_time")}
                for r in snap if r.get("code") in watched
            }
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
            # 实时成交额（按链·主业口径）：amount 单位元
            try:
                _am = {}
                for _r in snap:
                    _ch = self.code2chain.get(str(_r.get("code")))
                    if not _ch:
                        continue
                    _v = _r.get("amount") or 0
                    if _v <= 0:
                        continue
                    _d = _am.setdefault(_ch, [0.0, 0])
                    _d[0] += _v
                    _d[1] += 1
                _lst = [{"chain": k, "yi": round(v[0] / 1e8, 1), "n": v[1]} for k, v in _am.items()]
                _lst.sort(key=lambda x: -x["yi"])
                out["amt_chain"] = _lst[:8]
                out["amt_chain_at"] = time.strftime("%H:%M:%S")
            except Exception:
                out["amt_chain"] = []
            out["zt_codes"] = _zt_codes if "_zt_codes" in dir() else []
            # 盘中主力资金流：成功才更新（口径=东财 f62，与 Tushare moneyflow 同源）
            _fe = self.fetch_flow_em()
            if _fe:
                self._flow_em = _fe
            out["flow_em"] = getattr(self, "_flow_em", None)
            out["lib"] = self.lib
            out["watch_n"] = (self.lib or {}).get("watch_n", 0)
            with self.lock:
                self.research_quotes = {r["code"]: r for r in snap if r.get("code")}
                self.state = out
            # ── 四时点正式落档：窗口内第一次刷新时写一份（复盘档案）──
            if os.environ.get('TRADING_BOARD_ARCHIVE', '1') == '1' and standard and slot not in self.logged_slots:
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


# Keep presentation separate from the market data service.
PAGE = Path(ROOT, "templates", "intraday_board_legacy.html").read_text(encoding="utf-8")


def make_handler(board, page_loader=None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] == "/api/watches":
                b = json.dumps({"records": board.watches_today()}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)
            elif self.path.startswith("/api/state"):
                b = json.dumps(board.get_state(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")   # 实时页禁缓存（2026-10-24）
                self.end_headers()
                self.wfile.write(b)
            elif self.path in ("/", "/index.html"):
                b = (page_loader() if page_loader else PAGE).encode()
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

    return H


def serve(board, port, page_loader=None):
    H = make_handler(board, page_loader)
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
