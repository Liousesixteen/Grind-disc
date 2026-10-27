#!/usr/bin/env python3
"""Read-only evidence collection for the 2026-10-27 project review.

Never imports production modules, calls providers, or modifies the database.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def collect(asof, database=None):
    db = Path(database).resolve() if database else ROOT / 'cache/kb/trading_kb.sqlite'
    c = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=30)
    c.execute('PRAGMA query_only=ON')
    c.execute('BEGIN')
    out = {'audit_asof': asof, 'mode': 'read-only snapshot', 'tables': {}}

    def q(sql, args=()):
        return [list(r) for r in c.execute(sql, args)]

    tables = q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    out['table_count'] = len(tables)
    out['view_count'] = q("SELECT COUNT(*) FROM sqlite_master WHERE type='view'")[0][0]
    out['quick_check'] = q('PRAGMA quick_check')
    for (name,) in tables:
        cols = [r[1] for r in c.execute(f'PRAGMA table_info("{name}")')]
        rec = {'rows': q(f'SELECT COUNT(*) FROM "{name}"')[0][0], 'columns': cols}
        if 'date' in cols:
            rec['date_range'] = q(f'SELECT MIN(date),MAX(date),COUNT(DISTINCT date) FROM "{name}"')[0]
            normalized = "CASE WHEN length(date)=8 THEN substr(date,1,4)||'-'||substr(date,5,2)||'-'||substr(date,7,2) ELSE date END"
            rec['date_format_counts'] = q(f'SELECT length(date),COUNT(*) FROM "{name}" GROUP BY length(date)')
            rec['normalized_date_range'] = q(f'SELECT MIN({normalized}),MAX({normalized}),COUNT(DISTINCT {normalized}) FROM "{name}"')[0]
            rec['future_rows'] = q(f'SELECT COUNT(*) FROM "{name}" WHERE ({normalized})>?', (asof,))[0][0]
        out['tables'][name] = rec
    out['meta'] = q('SELECT * FROM meta')
    out['recent_runs'] = q('SELECT * FROM daily_log ORDER BY rowid DESC LIMIT 30')
    queries = {
        'lhb_date_formats': "SELECT length(date),COUNT(*),MIN(date),MAX(date) FROM fact_lhb GROUP BY length(date)",
        'lhb_seat_duplicate_groups': "SELECT COUNT(*),COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM fact_lhb_seat GROUP BY date,code,seat,side,amount HAVING COUNT(*)>1)",
        'lhb_seat_sides': 'SELECT side,COUNT(*) FROM fact_lhb_seat GROUP BY side',
        'ths_by_type': 'SELECT kind,COUNT(*) FROM dim_ths_board GROUP BY kind',
        'ths_member_universe': 'SELECT COUNT(DISTINCT code),COUNT(DISTINCT ths_code) FROM rel_ths_member',
        'ths_member_not_stock': 'SELECT COUNT(*),COUNT(DISTINCT m.code) FROM rel_ths_member m LEFT JOIN dim_stock s ON s.code=m.code WHERE s.code IS NULL',
        'ths_coord_categories': 'SELECT category,COUNT(*) FROM rel_ths_coord GROUP BY category',
        'kpl_daily_counts': 'SELECT date,COUNT(*),COUNT(DISTINCT theme) FROM fact_kpl GROUP BY date ORDER BY date DESC',
        'kpl_mapping_sample': "SELECT * FROM rel_kpl_coord WHERE theme_name IN ('生育概念','超聚变','MR概念')",
        'snapshot_latest_coverage': 'SELECT date,COUNT(*),SUM(turnover IS NOT NULL),SUM(liangbi IS NOT NULL),SUM(fmcap IS NOT NULL) FROM fact_snapshot GROUP BY date ORDER BY date DESC LIMIT 5',
        'etf_daily_latest_coverage': 'SELECT date,COUNT(*),SUM(nav IS NOT NULL),SUM(net_asset IS NOT NULL) FROM fact_etf_daily GROUP BY date ORDER BY date DESC LIMIT 5',
        'etf_share_latest_distribution': 'SELECT latest,COUNT(*) FROM (SELECT code,MAX(date) latest FROM fact_etf_share GROUP BY code) GROUP BY latest ORDER BY latest DESC',
        'etf_share_future_sample': 'SELECT code,MIN(date),MAX(date),COUNT(*) FROM fact_etf_share WHERE date>? GROUP BY code ORDER BY code LIMIT 15',
        'etf_share_on_asof': 'SELECT date,COUNT(DISTINCT code) FROM fact_etf_share WHERE date<=? GROUP BY date ORDER BY date DESC LIMIT 5',
        'etf_cons_coverage': 'SELECT COUNT(DISTINCT etf_code),COUNT(*),SUM(s.code IS NULL) FROM rel_etf_cons rc LEFT JOIN dim_stock s ON s.code=rc.con_code',
        'etf_momentum_filter_repro': "SELECT MAX(date),MAX(date)-5,MAX(date)-20 FROM fact_etf_daily",
        'etf_nav_missing_flow_rows': 'SELECT f.date,COUNT(*),SUM(ABS(f.share_chg)) FROM v_etf_flow f JOIN dim_etf e ON e.code=f.code LEFT JOIN fact_etf_daily d ON d.code=f.code AND d.date=f.date WHERE f.date IN (SELECT DISTINCT date FROM fact_etf_daily ORDER BY date DESC LIMIT 5) AND e.chain_id<>\'\' AND f.share_chg IS NOT NULL AND f.share_chg<>0 AND d.nav IS NULL GROUP BY f.date',
        'cluster_member_count_mismatch': 'SELECT t.cluster_id,t.n_stocks,COUNT(s.code) actual FROM theme_cross_cluster t LEFT JOIN theme_cross_stock s ON s.cluster_id=t.cluster_id GROUP BY t.cluster_id HAVING t.n_stocks<>COUNT(s.code)',
        'edge_temporal_provenance': "SELECT COUNT(*),SUM(valid_from IS NULL OR valid_from=''),SUM(source IS NULL OR source=''),SUM(evidence IS NULL OR evidence=''),SUM(valid_to IS NOT NULL AND valid_to<>'') FROM rel_edge",
        'holder_periods': 'SELECT end_date,COUNT(*),COUNT(DISTINCT code) FROM rel_holder_stock GROUP BY end_date ORDER BY end_date DESC LIMIT 10',
        'holder_classification': 'SELECT kind,COUNT(*) FROM dim_holder GROUP BY kind',
        'product_mapping': "SELECT COUNT(*),SUM(seg_id IS NOT NULL AND seg_id<>'') FROM dim_product",
        'product_asof': 'SELECT asof,COUNT(*) FROM rel_stock_product GROUP BY asof ORDER BY COUNT(*) DESC LIMIT 8',
        'product_future_asof': 'SELECT asof,COUNT(*),COUNT(DISTINCT code) FROM rel_stock_product WHERE asof>? GROUP BY asof',
        'existing_backtest': 'SELECT * FROM bt_theme_result',
    }
    out['checks'] = {}
    for key, sql in queries.items():
        try:
            out['checks'][key] = q(sql, (asof,) if '?' in sql else ())
        except sqlite3.Error as e:
            out['checks'][key] = {'error': str(e)}
    c.rollback()
    c.close()
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--asof', default='2026-10-27')
    ap.add_argument('--out', required=True)
    ap.add_argument('--db')
    args = ap.parse_args()
    dt.date.fromisoformat(args.asof)
    result = collect(args.asof,args.db)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'out': str(dest.resolve()), 'tables': result['table_count'],
                      'views': result['view_count'], 'rows': sum(t['rows'] for t in result['tables'].values()),
                      'quick_check': result['quick_check']}, ensure_ascii=False))
