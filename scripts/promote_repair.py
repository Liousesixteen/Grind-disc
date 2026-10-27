#!/usr/bin/env python3
"""Promote only validated repaired partitions; preserve unrelated concurrent data and raw evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from data_runtime import ROOT,DEFAULT_DB,SCHEMA,iso_date,now
from ts_etf import DDL,VIEWS

DAY='2026-10-26'
FULL=['trade_calendar','rel_ths_member','rel_ths_member_full','theme_cross_cluster',
      'theme_cross_stock','theme_cross_concept','theme_cross_history','theme_cluster_trend']
DATED=['fact_snapshot','fact_equity_daily','fact_moneyflow','fact_kpl','fact_kpl_directory',
       'fact_etf_daily','fact_etf_limit','fact_etf_margin','fact_margin']
LHB=['fact_lhb','fact_lhb_seat','fact_lhb_detail','fact_lhb_seat_detail']


def promote(staging,database,apply=False):
    c=sqlite3.connect(str(database),timeout=60)
    c.execute('ATTACH DATABASE ? AS repaired',(str(staging),))
    def count(t,schema='main'):
        if not c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?",(t,)).fetchone():return 0
        return c.execute(f'SELECT COUNT(*) FROM {schema}.{t}').fetchone()[0]
    required=['daily','moneyflow','lhb','kpl','ths','etf_limit','etf_share','etf_margin']
    for source in required:
        status=c.execute('SELECT status FROM repaired.ingest_partition WHERE source=? AND date=?',(source,DAY)).fetchone()
        if status!=('ok',):raise RuntimeError(f'Cannot publish incomplete {source}')
    if c.execute('SELECT COUNT(*) FROM repaired.fact_kpl_directory WHERE date=?',(DAY,)).fetchone()[0]!=250:
        raise RuntimeError('unexpected directory coverage')
    if c.execute('''SELECT COUNT(*) FROM (SELECT t.cluster_id FROM repaired.theme_cross_cluster t
+      LEFT JOIN repaired.theme_cross_stock s USING(cluster_id) GROUP BY t.cluster_id HAVING COUNT(s.code)<>t.n_stocks)'''.replace('+','')).fetchone()[0]:
        raise RuntimeError('cluster binding mismatch')
    manifest={'asof':now().isoformat(),'target_day':DAY,'staging':str(staging),'applied':apply,
              'before':{t:count(t) for t in DATED+LHB},'copied':{}}
    if not apply:c.close();return manifest
    # Existing pre-repair online backup is mandatory; no unbacked publication.
    backup=json.loads((ROOT/'reports/2026-10-27/修复备份位置.json').read_text())['backup']
    if not (Path(backup)/'trading_kb.sqlite').is_file():raise RuntimeError('repair backup missing')
    c.executescript(SCHEMA+DDL)
    for table in FULL:
        sql=c.execute("SELECT sql FROM repaired.sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
        if sql and not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():c.execute(sql[0])
    c.execute('CREATE TABLE IF NOT EXISTS repair_quarantine(id TEXT PRIMARY KEY,table_name TEXT,reason TEXT,payload TEXT,quarantined_at TEXT)')
    c.execute('BEGIN IMMEDIATE')
    try:
        for t in FULL:
            c.execute(f'DELETE FROM {t}')
            c.execute(f'INSERT INTO {t} SELECT * FROM repaired.{t}')
            manifest['copied'][t]=count(t)
        c.execute('INSERT OR REPLACE INTO dim_ths_board SELECT * FROM repaired.dim_ths_board')
        for t in DATED:
            c.execute(f'DELETE FROM {t} WHERE date=?',(DAY,))
            c.execute(f'INSERT INTO {t} SELECT * FROM repaired.{t} WHERE date=?',(DAY,))
        for t in LHB:
            # All 60 original trading days have been independently re-fetched.
            c.execute(f'DELETE FROM {t} WHERE replace(date,\'-\',\'\') BETWEEN \'20260727\' AND \'20261026\'')
            c.execute(f'INSERT INTO {t} SELECT * FROM repaired.{t} WHERE date BETWEEN \'2026-07-27\' AND \'2026-10-26\'')
        c.execute('INSERT OR REPLACE INTO fact_etf_share SELECT * FROM repaired.fact_etf_share WHERE date=?',(DAY,))
        c.execute('INSERT OR IGNORE INTO raw_response SELECT * FROM repaired.raw_response')
        c.execute('INSERT OR REPLACE INTO ingest_partition SELECT * FROM repaired.ingest_partition')
        # Partial NAV coverage is explicit; no fake 1-CNY substitute.
        n,missing=c.execute('SELECT COUNT(*),SUM(nav IS NULL) FROM fact_etf_daily WHERE date=?',(DAY,)).fetchone()
        c.execute('UPDATE ingest_partition SET status=?,note=? WHERE source=? AND date=?',
                  ('partial' if missing else 'ok',f'nav_missing={missing}/{n}','etf_daily',DAY))
        for t,col in [('fact_etf_share','date'),('rel_stock_product','asof')]:
            columns=[r[1] for r in c.execute(f'PRAGMA table_info({t})')]
            rows=c.execute(f'SELECT * FROM {t} WHERE {col}>?',(now().date().isoformat(),)).fetchall()
            for row in rows:
                payload=json.dumps(dict(zip(columns,row)),ensure_ascii=False)
                key=hashlib.sha256((t+payload).encode()).hexdigest()
                c.execute('INSERT OR IGNORE INTO repair_quarantine VALUES(?,?,?,?,?)',
                          (key,t,'future observation/report period; requires source verification',payload,now().isoformat()))
            c.execute(f'DELETE FROM {t} WHERE {col}>?',(now().date().isoformat(),))
            manifest['copied']['quarantined_'+t]=len(rows)
        c.execute("UPDATE dim_holder SET kind='未核验' WHERE kind IN ('个人','机构')")
        c.execute("UPDATE bt_theme_result SET note='已失效：旧成员错配/时态与成交假设不合格，禁止作为策略验证'")
        for statement in VIEWS.split(';'):
            if statement.strip():c.execute(statement)
        c.execute('SELECT * FROM v_etf_momentum LIMIT 1').fetchall()
        c.execute('SELECT * FROM v_etf_chain_flow LIMIT 1').fetchall()
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('repair_version','2026-10-27-p0-v1'))
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('legacy_history_status','pre-2026-10-26 snapshot amounts lack source attribution; do not assume normalized units'))
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('repair_completed_at',now().isoformat()))
        c.commit()
    except BaseException:
        c.rollback();raise
    manifest['after']={t:count(t) for t in DATED+LHB}
    manifest['quick_check']=c.execute('PRAGMA quick_check').fetchall()
    c.close();return manifest


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--staging',required=True);ap.add_argument('--db',default=str(DEFAULT_DB));ap.add_argument('--apply',action='store_true');a=ap.parse_args()
    result=promote(Path(a.staging),Path(a.db),a.apply)
    path=ROOT/'reports/2026-10-27'/('修复入库结果.json' if a.apply else '修复入库预检.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(path)
