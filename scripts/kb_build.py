#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股全量投研知识库 · 建库脚本 v1（2026-10-25）
================================================================
把现有 JSON/CSV 无损导入 SQLite（cache/kb/trading_kb.sqlite），六组表：

  维度（8）：dim_stock / dim_chain / dim_segment / segment_word / chain_match /
            dim_concept / dim_region / dim_platform
  关系（4）：rel_stock_concept / rel_concept_coord / rel_edge / rel_event(空表待供需)
  事实（4）：fact_snapshot / fact_limitup / fact_concept_daily / fact_name_clan
  规则（3）：chain_binding / chain_cooccur / industry_chain
  元数据（2）：meta / audit_coverage
  视图（2）：v_stock_primary / v_segment_scope

「19 链 / 110 环节」怎么进库：
  1) 字典层：dim_chain(19) + dim_segment(110) + segment_word(856 词) + chain_match(350 词)
     + chain_binding(18 跨环节绑定) + chain_cooccur(7 共现门槛) + industry_chain(48 申万兜底)
  2) 归属层（两条独立的路，不混）：
     · 主业路：F10 主营项名 → 环节词匹配 → dim_stock.biz_chain/biz_seg
     · 题材路：概念 → 链词/环节词匹配 → dim_concept.chain_id/seg_id（已是 concept_map）
  3) 关系层：rel_edge 统一存 票→概念、票→链（主业/题材两种 role）、概念→链/环节
  4) 视图层：v_stock_primary 把"多对多"坍缩成"展示用的唯一"（主业优先，其次题材权重）

用法：
  python3 scripts/kb_build.py --rebuild            # 重建（旧库自动备份）
  python3 scripts/kb_build.py --verify             # 只校验
"""
import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import name_clan as NC
import pool_pipeline as P

DB_DIR = os.path.join(ROOT, "cache", "kb")
DB = os.path.join(DB_DIR, "trading_kb.sqlite")
UNI = os.path.join(ROOT, "cache", "theme_universe", "universe.json")
CMAP = os.path.join(ROOT, "cache", "theme_universe", "concept_map.json")
CSTAT = os.path.join(ROOT, "cache", "theme_universe", "concept_stats.json")
CFG = os.path.join(ROOT, "scripts", "pool_config.json")
STRENGTH = os.path.join(ROOT, "cache", "theme_universe", "strength_2026-10-23_concepts.json")
POOL = os.path.join(ROOT, "cache", "pool_20261023")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS dim_chain(
  chain_id TEXT PRIMARY KEY, name TEXT, n_segments INT, n_match_words INT);
CREATE TABLE IF NOT EXISTS dim_segment(
  seg_id TEXT PRIMARY KEY, chain_id TEXT, name TEXT, n_words INT, is_chain_level INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS segment_word(
  seg_id TEXT, word TEXT, PRIMARY KEY(seg_id, word));
CREATE TABLE IF NOT EXISTS chain_match(
  chain_id TEXT, word TEXT, PRIMARY KEY(chain_id, word));
CREATE TABLE IF NOT EXISTS chain_binding(word TEXT PRIMARY KEY, target TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS chain_cooccur(word TEXT PRIMARY KEY, rule_json TEXT);
CREATE TABLE IF NOT EXISTS industry_chain(sw_name TEXT PRIMARY KEY, chain_id TEXT);

CREATE TABLE IF NOT EXISTS dim_stock(
  code TEXT PRIMARY KEY, name TEXT, sw_l1 TEXT, sw_l2 TEXT, sw_l3 TEXT,
  region TEXT, board TEXT, n_concepts INT, n_valid INT,
  biz_chain TEXT, biz_seg TEXT, biz_note TEXT);
CREATE TABLE IF NOT EXISTS dim_concept(
  concept TEXT PRIMARY KEY, category TEXT, chain_id TEXT, seg_id TEXT,
  domain TEXT, subkind TEXT, rule TEXT,
  n_stocks INT, n_valid_stocks INT, zt_today INT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS dim_region(region TEXT PRIMARY KEY, kind TEXT);
CREATE TABLE IF NOT EXISTS dim_platform(platform TEXT PRIMARY KEY, concept TEXT);

-- L2 二级链 / 主题链（2026-10-25 自建候选：链内成规模的概念簇，待与外部产业链对齐）
CREATE TABLE IF NOT EXISTS dim_subchain(
  subchain TEXT PRIMARY KEY, chain_id TEXT, n_stocks INT, rule TEXT);
CREATE TABLE IF NOT EXISTS rel_concept_subchain(
  concept TEXT, subchain TEXT, PRIMARY KEY(concept, subchain));

-- L4 产品/器件层（F10 主营项名，自带营收占比）
CREATE TABLE IF NOT EXISTS dim_product(
  product TEXT PRIMARY KEY, n_stocks INT, ratio_sum REAL,
  seg_id TEXT, chain_id TEXT, map_rule TEXT);
CREATE TABLE IF NOT EXISTS dim_product_alias(
  product TEXT PRIMARY KEY, seg_id TEXT, src TEXT, n_products INT);
CREATE TABLE IF NOT EXISTS rel_stock_product(
  code TEXT, product TEXT, ratio REAL, asof TEXT, basis TEXT, PRIMARY KEY(code, product));

CREATE TABLE IF NOT EXISTS rel_stock_concept(
  code TEXT, concept TEXT, is_valid INT, PRIMARY KEY(code, concept));
CREATE TABLE IF NOT EXISTS rel_concept_coord(
  concept TEXT PRIMARY KEY, category TEXT, chain_id TEXT, seg_id TEXT, domain TEXT);
CREATE TABLE IF NOT EXISTS rel_edge(
  edge_id INTEGER PRIMARY KEY,
  src_type TEXT, src_id TEXT, rel_type TEXT, dst_type TEXT, dst_id TEXT,
  role TEXT, weight REAL, weight_unit TEXT,
  valid_from TEXT, valid_to TEXT,
  source TEXT, confidence TEXT, evidence TEXT, edge_group TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_edge_src ON rel_edge(src_type, src_id, rel_type, valid_to);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON rel_edge(dst_type, dst_id, rel_type, valid_to);
CREATE TABLE IF NOT EXISTS rel_event(
  event_id TEXT PRIMARY KEY, kind TEXT, date TEXT, amount REAL, amount_unit TEXT,
  product TEXT, is_related INT, source TEXT, confidence TEXT, evidence TEXT);

CREATE TABLE IF NOT EXISTS fact_snapshot(
  date TEXT, code TEXT, name TEXT, pct REAL, price REAL, amount REAL,
  turnover REAL, liangbi REAL, fmcap REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS fact_limitup(
  date TEXT, code TEXT, name TEXT, lb INT, first_seal TEXT, seal_amt REAL, seal_ratio REAL,
  zbc INT, score INT, roles TEXT, main_theme TEXT, attr_theme TEXT, attr_score REAL,
  layer TEXT, chain TEXT, seg TEXT, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS fact_concept_daily(
  date TEXT, concept TEXT, n INT, zt INT, med REAL, mean REAL, up_ratio REAL, excess REAL,
  PRIMARY KEY(date, concept));
CREATE TABLE IF NOT EXISTS fact_name_clan(
  date TEXT, code TEXT, name TEXT, clan TEXT, clan_size INT, tags TEXT, PRIMARY KEY(date, code));

CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit_coverage(
  date TEXT, entity TEXT, total INT, mapped INT, note TEXT);

CREATE VIEW IF NOT EXISTS v_stock_primary AS
SELECT c.code, c.chain_id,
  COALESCE(
    (SELECT e2.dst_id FROM rel_edge e2
      WHERE e2.src_type='stock' AND e2.src_id=c.code AND e2.rel_type='stock_segment'
        AND e2.role='主业' LIMIT 1),
    (SELECT e2.dst_id FROM rel_edge e2
      WHERE e2.src_type='stock' AND e2.src_id=c.code AND e2.rel_type='stock_segment'
        AND e2.dst_id LIKE c.chain_id || '·%'
      ORDER BY e2.weight DESC LIMIT 1)
  ) AS seg_id,
  c.role, c.weight
FROM (
  SELECT src_id AS code, dst_id AS chain_id, role, weight,
         ROW_NUMBER() OVER (PARTITION BY src_id
           ORDER BY CASE role WHEN '主业' THEN 0 ELSE 1 END, weight DESC) AS rn
  FROM rel_edge WHERE rel_type='stock_chain'
) c WHERE c.rn=1;

CREATE VIEW IF NOT EXISTS v_segment_scope AS
SELECT chain_id, COUNT(DISTINCT concept) AS n_concepts
FROM rel_concept_coord WHERE seg_id IS NOT NULL GROUP BY chain_id;
"""


def board_of(code):
    c = str(code)
    if c.startswith(("60", "68")):
        return "沪"
    if c.startswith(("00", "30")):
        return "深"
    if c.startswith(("4", "8", "92")):
        return "北"
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--date", default="2026-10-23")
    a = ap.parse_args()
    os.makedirs(DB_DIR, exist_ok=True)

    if a.verify:
        return verify()

    if a.rebuild and os.path.exists(DB):
        bak = DB + ".bak" + dt.datetime.now().strftime("%Y%m%d_%H%M")
        source=sqlite3.connect('file:'+DB+'?mode=ro',uri=True,timeout=60)
        backup=sqlite3.connect(bak)
        try:source.backup(backup)
        finally:backup.close();source.close()
        print("旧库已备份 →", os.path.basename(bak))
    if a.rebuild and os.path.exists(DB):
        os.remove(DB)
        for suf in ("-wal", "-shm"):
            if os.path.exists(DB + suf):
                os.remove(DB + suf)

    cfg = json.load(open(CFG, encoding="utf-8"))
    uni = json.load(open(UNI, encoding="utf-8"))
    cmap = json.load(open(CMAP, encoding="utf-8"))
    cstat = json.load(open(CSTAT, encoding="utf-8")) if os.path.exists(CSTAT) else {}
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = {}

    # ── 1. 链 / 环节 / 词表 ────────────────────────────────
    chains = {k: v for k, v in cfg["chains"].items() if not k.startswith("_")}
    seg_rows, word_rows, match_rows = [], [], []
    for cname, cv in chains.items():
        match = cv.get("match") or []
        segs = cv.get("环节") or {}
        n["dim_chain"] = n.get("dim_chain", 0) + 1
        conn.execute("INSERT OR REPLACE INTO dim_chain VALUES(?,?,?,?)",
                     (cname, cname, len(segs), len(match)))
        for w in match:
            conn.execute("INSERT OR IGNORE INTO chain_match VALUES(?,?)", (cname, w))
        for sname, words in segs.items():
            sid = f"{cname}·{sname}"
            conn.execute("INSERT OR REPLACE INTO dim_segment VALUES(?,?,?,?,?)",
                         (sid, cname, sname, len(words), 0))
            for w in words:
                conn.execute("INSERT OR IGNORE INTO segment_word VALUES(?,?)", (sid, w))
    for w, tgt in (cfg["chains"].get("_归属绑定") or {}).items():
        conn.execute("INSERT OR REPLACE INTO chain_binding VALUES(?,?,?)", (w, tgt, ""))
    for w, rule in (cfg["chains"].get("_共现门槛") or {}).items():
        conn.execute("INSERT OR REPLACE INTO chain_cooccur VALUES(?,?)",
                     (w, json.dumps(rule, ensure_ascii=False)))
    for sw, ch in (cfg.get("industry_to_chain") or {}).items():
        conn.execute("INSERT OR REPLACE INTO industry_chain VALUES(?,?)", (sw, ch))

    # ── 2. 概念维度（含 8 类目归位）────────────────────────
    cnt, vcnt = {}, {}
    for code, v in uni.items():
        if code.startswith("_"):
            continue
        for t in (v.get("全部概念") or []):
            cnt[t] = cnt.get(t, 0) + 1
        for t in (v.get("有效概念") or []):
            vcnt[t] = vcnt.get(t, 0) + 1
    for concept, m in cmap.items():
        st = cstat.get(concept, {})
        conn.execute("INSERT OR REPLACE INTO dim_concept VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (concept, m.get("类目", ""), m.get("链", ""), m.get("环节", ""),
                      m.get("主题域", ""), m.get("子类", ""), m.get("规则", ""),
                      cnt.get(concept, 0), vcnt.get(concept, 0), st.get("zt_today", 0),
                      st.get("first_seen", ""), st.get("last_seen", "")))
        conn.execute("INSERT OR REPLACE INTO rel_concept_coord VALUES(?,?,?,?,?)",
                     (concept, m.get("类目", ""), m.get("链", ""), m.get("环节", ""), m.get("主题域", "")))
        if m.get("类目") == "产业题材" and m.get("链"):
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,"
                         "source,confidence,created_at) VALUES('concept',?,'concept_chain','chain',?,?,?,?)",
                         (concept, m["链"], "concept_map", "B", now))
            if m.get("环节"):
                conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,"
                             "source,confidence,created_at) VALUES('concept',?,'concept_segment','segment',?,?,?,?)",
                             (concept, f'{m["链"]}·{m["环节"]}', "concept_map", "B", now))
        if m.get("类目") == "平台生态" and m.get("主题域"):
            conn.execute("INSERT OR IGNORE INTO dim_platform VALUES(?,?)", (m["主题域"], concept))

    # ── 2.5 二级链 L2（自建候选）：链内「成规模的概念簇」───────
    # 口径：产业题材、n_stocks ≥5、名称不等于该链的环节名；每链最多取 8 个
    sub_n = 0
    for cname, cv in chains.items():
        segnames = set((cv.get("环节") or {}).keys())
        cand = []
        for concept, m in cmap.items():
            if m.get("类目") != "产业题材" or m.get("链") != cname:
                continue
            if concept in segnames or cnt.get(concept, 0) < 5:
                continue
            cand.append((cnt.get(concept, 0), concept))
        for n2, concept in sorted(cand, reverse=True)[:8]:
            conn.execute("INSERT OR REPLACE INTO dim_subchain VALUES(?,?,?,?)",
                         (concept, cname, n2, "概念聚簇（自建候选）"))
            conn.execute("INSERT OR IGNORE INTO rel_concept_subchain VALUES(?,?)",
                         (concept, concept))
            sub_n += 1
    print(f"  二级链：{sub_n} 个（自建候选，待对齐外部产业链）")

    # ── 3. 个股维度 + 票-概念挂靠 + 票-链关系 ───────────────
    chain_hits = {}
    for code, v in uni.items():
        if code.startswith("_"):
            continue
        biz = v.get("主业链·环节") or ""
        bch, bseg = (biz.split("·", 1) + [""])[:2] if "·" in biz else (biz, "")
        regions = v.get("地域") or []
        conn.execute("INSERT OR REPLACE INTO dim_stock VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, v.get("name", ""), v.get("申万一级", ""), v.get("申万二级", ""),
                      v.get("申万三级", ""), "、".join(regions), board_of(code),
                      len(v.get("全部概念") or []), len(v.get("有效概念") or []),
                      bch, bseg, ""))
        for r in regions:
            conn.execute("INSERT OR IGNORE INTO dim_region VALUES(?,?)", (r, "地域"))
        valid = set(v.get("有效概念") or [])
        for t in (v.get("全部概念") or []):
            conn.execute("INSERT OR IGNORE INTO rel_stock_concept VALUES(?,?,?)",
                         (code, t, 1 if t in valid else 0))
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,"
                         "source,confidence,created_at) VALUES('stock',?,'stock_concept','concept',?,?,?,?,?)",
                         (code, t, "有效概念" if t in valid else "噪声标签", "universe",
                          "A" if t in valid else "C", now))
        # 票→链：主业（权重 1.0）+ 题材（按命中概念数加权）
        if bch:
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,"
                         "weight,weight_unit,source,confidence,created_at) "
                         "VALUES('stock',?,'stock_chain','chain',?,'主业',1.0,'1','F10','A',?)",
                         (code, bch, now))
            chain_hits.setdefault(bch, set()).add(code)
        cc = {}
        for t in (v.get("有效概念") or []):
            m = cmap.get(t) or {}
            if m.get("类目") == "产业题材" and m.get("链"):
                cc[m["链"]] = cc.get(m["链"], 0) + 1
        for ch, k in cc.items():
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,"
                         "weight,weight_unit,source,confidence,created_at) "
                         "VALUES('stock',?,'stock_chain','chain',?,'题材',?,'概念命中数','concept_map','B',?)",
                         (code, ch, float(k), now))
            chain_hits.setdefault(ch, set()).add(code)
        # 票→环节（主业 + 题材两种 role，供视图取环节）
        seg_hits = {}
        if bseg and bch:
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,"
                         "weight,weight_unit,source,confidence,created_at) "
                         "VALUES('stock',?,'stock_segment','segment',?,'主业',1.0,'1','F10','A',?)",
                         (code, f"{bch}·{bseg}", now))
        for t in (v.get("有效概念") or []):
            m = cmap.get(t) or {}
            if m.get("类目") == "产业题材" and m.get("链") and m.get("环节"):
                sid = f'{m["链"]}·{m["环节"]}'
                seg_hits[sid] = seg_hits.get(sid, 0) + 1
        for sid, k in seg_hits.items():
            conn.execute("INSERT INTO rel_edge(src_type,src_id,rel_type,dst_type,dst_id,role,"
                         "weight,weight_unit,source,confidence,created_at) "
                         "VALUES('stock',?,'stock_segment','segment',?,'题材',?,'概念命中数','concept_map','B',?)",
                         (code, sid, float(k), now))

    # ── 3.5 产品层 L4：F10 主营项名 → dim_product + rel_stock_product + 产品→环节 ──
    PROD = os.path.join(ROOT, "cache", "theme_universe", "products.json")
    if os.path.exists(PROD):
        prod = json.load(open(PROD, encoding="utf-8"))
        # 环节词表（长词优先）用于产品→环节
        seg_words = []
        for cv_name, cv in chains.items():
            for sname, words in (cv.get("环节") or {}).items():
                for w in words:
                    seg_words.append((w, f"{cv_name}·{sname}"))
        seg_words.sort(key=lambda x: -len(x[0]))
        # 产品→环节的例外表（长词优先仍会误伤：投资银行→银行、资产管理→银行）
        PROD_EXC = {"投资银行": "金融链·券商", "资产管理": "金融链·多元金融",
                    "证券经纪": "金融链·券商", "保险业务": "金融链·保险"}
        for pname, d in prod.items():
            sid = chid = rule = ""
            _al = conn.execute("SELECT seg_id FROM dim_product_alias WHERE product=?", (pname,)).fetchone()
            if _al and _al[0]:
                sid = _al[0]; chid = sid.split("·")[0]; rule = "产品别名"
            for k, v2 in ({} if sid else PROD_EXC).items():
                if k in pname:
                    sid = v2
                    chid, rule = v2.split("·")[0], f"例外表:{k}"
                    break
            if sid:
                pass
            else:
              for w, s2 in seg_words:
                if w and w in pname:
                    sid, chid, rule = s2, s2.split("·")[0], f"环节词:{w}"
                    break
            if not chid:
                c2, _ = P.chain_of(pname, cfg["chains"])
                if c2:
                    chid, rule = c2, "链词"
            conn.execute("INSERT OR REPLACE INTO dim_product VALUES(?,?,?,?,?,?)",
                         (pname, d.get("n_stocks", 0), d.get("ratio_sum", 0),
                          sid, chid, rule))
        # 票→产品（营收占比为权重）
        prof_dir = os.path.join(ROOT, "cache", "profile")
        for fn in os.listdir(prof_dir):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                p = json.load(open(os.path.join(prof_dir, fn), encoding="utf-8"))
            except Exception:
                continue
            code = p.get("code") or fn[:-5]
            for it in (p.get("items") or []):
                nm = (it.get("name") or "").strip()
                if not nm:
                    continue
                conn.execute("INSERT OR REPLACE INTO rel_stock_product VALUES(?,?,?,?,?)",
                             (code, nm, it.get("ratio"), p.get("asof", ""), p.get("basis", "")))
        mapped = conn.execute("SELECT COUNT(*) FROM dim_product WHERE seg_id<>''").fetchone()[0]
        print(f"  产品层：{len(prod)} 个产品节点，映射到环节 {mapped} 个")

    # ── 4. 事实层：快照 / 涨停 / 概念读数 / 名字族 ─────────
    snap = json.load(open(os.path.join(POOL, "all_a_snapshot.json"), encoding="utf-8"))
    for r in snap:
        conn.execute("INSERT OR REPLACE INTO fact_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
                     (a.date, r.get("code"), r.get("name"), r.get("pct"), r.get("price"),
                      r.get("amount"), r.get("turnover"), r.get("liangbi"), r.get("fmcap")))
    # 涨停：优先用实时 state（含画像/归因）；没有则留空
    st = None
    for p in ("/tmp/f21.json", "/tmp/f20.json", "/tmp/state_v2.json"):
        if os.path.exists(p):
            st = json.load(open(p, encoding="utf-8"))
            break
    if st and not st.get("booting"):
        json.dump(st, open(os.path.join(DB_DIR, "source_state.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        for r in (st.get("pankou") or []):
            pr = r.get("画像") or {}
            conn.execute("INSERT OR REPLACE INTO fact_limitup VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (a.date, r.get("code"), r.get("name"), r.get("连板"), r.get("首封"),
                          r.get("封单亿"), r.get("封单占流通%"), r.get("炸板次数"), r.get("成色"),
                          r.get("角色"), r.get("题材"), pr.get("theme"), pr.get("score"),
                          pr.get("layer"), pr.get("chain_only"), pr.get("chain")))
        zt = [(r.get("code"), r.get("name")) for r in (st.get("pankou") or [])]
        clans = NC.compute(zt)
        for code, v in clans.items():
            conn.execute("INSERT OR REPLACE INTO fact_name_clan VALUES(?,?,?,?,?,?)",
                         (a.date, code, next((n for c, n in zt if c == code), ""),
                          v.get("族", ""), v.get("族大小", 0), "、".join(v.get("标签") or [])))
    if os.path.exists(STRENGTH):
        sc = json.load(open(STRENGTH, encoding="utf-8"))
        for x in (sc.get("concepts") or []):
            conn.execute("INSERT OR REPLACE INTO fact_concept_daily VALUES(?,?,?,?,?,?,?,?)",
                         (a.date, x.get("concept"), x.get("n"), x.get("zt"), x.get("med"),
                          x.get("mean"), x.get("up_ratio"), x.get("excess")))

    # ── 5. 元数据与审计 ────────────────────────────────
    meta = {
        "schema_ver": "1.0", "built_at": now, "data_date": a.date,
        "chains": str(len(chains)), "segments": str(sum(len(v.get("环节") or {}) for v in chains.values())),
        "concepts": str(len(cmap)), "stocks": str(sum(1 for k in uni if not k.startswith("_"))),
    }
    for k, v in meta.items():
        conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    conn.execute("INSERT INTO audit_coverage VALUES(?,?,?,?,?)",
                 (a.date, "concept", len(cmap),
                  sum(1 for v in cmap.values() if v.get("类目") != "待归类"),
                  "8 类目归位（待归类=0）"))
    conn.execute("INSERT INTO audit_coverage VALUES(?,?,?,?,?)",
                 (a.date, "stock_chain", len(uni) - 1,
                  sum(len(s) for s in chain_hits.values()),
                  "票→链边数（含主业+题材，多对多）"))
    conn.commit()

    print("建库完成 →", DB)
    for t in ("dim_chain", "dim_segment", "segment_word", "chain_match", "dim_stock",
              "dim_concept", "rel_stock_concept", "rel_edge", "fact_snapshot",
              "fact_limitup", "fact_concept_daily", "fact_name_clan"):
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {c:>8d}")
    conn.close()


def verify():
    if not os.path.exists(DB):
        raise SystemExit("库不存在，先跑 --rebuild")
    conn = sqlite3.connect(DB)
    print("=== 结构 ===")
    for t in ("dim_chain", "dim_segment", "segment_word", "chain_match", "chain_binding",
              "chain_cooccur", "industry_chain", "dim_subchain", "dim_product", "rel_stock_product",
              "dim_stock", "dim_concept", "rel_stock_concept",
              "rel_edge", "fact_snapshot", "fact_limitup", "fact_concept_daily", "fact_name_clan"):
        c = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:20s} {c:>8d}")
    print("\n=== 抽查：南方路机(603280) 全部身份 ===")
    for row in conn.execute("SELECT code,name,sw_l1,sw_l3,biz_chain,biz_seg,n_concepts,n_valid "
                            "FROM dim_stock WHERE code='603280'"):
        print("  ", row)
    print("  概念数：", conn.execute("SELECT COUNT(*) FROM rel_stock_concept WHERE code='603280'").fetchone()[0])
    print("  票→链：", conn.execute("SELECT role,dst_id,weight FROM rel_edge "
                                    "WHERE src_type='stock' AND src_id='603280' AND rel_type='stock_chain' "
                                    "ORDER BY role,weight DESC").fetchall())
    print("\n=== 链覆盖（票数，含多对多）===")
    for row in conn.execute("SELECT dst_id, COUNT(DISTINCT src_id) c FROM rel_edge "
                            "WHERE rel_type='stock_chain' GROUP BY dst_id ORDER BY c DESC LIMIT 8"):
        print("  ", row)
    print("\n=== 涨停池（含归因）前 5 ===")
    for row in conn.execute("SELECT name,lb,first_seal,score,main_theme,attr_theme,attr_score,layer "
                            "FROM fact_limitup ORDER BY score DESC LIMIT 5"):
        print("  ", row)
    conn.close()


if __name__ == "__main__":
    main()
