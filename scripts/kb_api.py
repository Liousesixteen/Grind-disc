#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库工具层（P1）——统一查询接口，供 CLI / HTTP / 未来 Agent 调用
CLI:  python scripts/kb_api.py stock 603280
      python scripts/kb_api.py chain 机器人链
      python scripts/kb_api.py theme
      python scripts/kb_api.py flow
HTTP: python scripts/kb_api.py serve --port 6061
      GET /stock/<code> /chain/<name> /theme /flow /search?q=
"""
import argparse, json, os, sqlite3, sys
from urllib.parse import unquote, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB=os.path.join(ROOT,'cache','kb','trading_kb.sqlite')

def con():
    c=sqlite3.connect("file:%s?mode=ro"%DB, uri=True, timeout=15)
    c.row_factory=sqlite3.Row
    return c

def _d(rows): return [dict(r) for r in rows]

def stock(code):
    """个股全息：身份 / 链环节 / 概念 / 产品 / 资金 / ETF / 股东"""
    c=con(); code=str(code).zfill(6)
    out={"code":code,"status":"research_only","data_notes":"篮子关联不代表持仓权重或龙头；资金流非机构身份；旧回测未验证"}
    r=c.execute("SELECT * FROM dim_stock WHERE code=?",(code,)).fetchone()
    if not r: return {"error":"未找到该代码"}
    out["identity"]=dict(r)
    out["primary"]=dict(c.execute("SELECT * FROM v_stock_primary WHERE code=?",(code,)).fetchone() or {})
    out["sw_l3"]=[x[0] for x in c.execute("SELECT sw_l3 FROM rel_stock_sw WHERE code=?",(code,))]
    out["chains"]=_d(c.execute(
        "SELECT dst_id chain, role, weight FROM rel_edge WHERE src_type='stock' AND src_id=? "
        "AND rel_type='stock_chain' ORDER BY CASE role WHEN '主业' THEN 0 ELSE 1 END, weight DESC",(code,)))
    out["concepts"]=[x[0] for x in c.execute(
        "SELECT concept FROM rel_stock_concept WHERE code=? AND is_valid=1 LIMIT 30",(code,))]
    out["products"]=_d(c.execute(
        "SELECT p.product, r.ratio, p.seg_id FROM rel_stock_product r "
        "LEFT JOIN dim_product p ON p.product=r.product WHERE r.code=? AND r.asof<=date('now','+8 hours') ORDER BY r.ratio DESC LIMIT 10",(code,)))
    out["moneyflow"]=_d(c.execute(
        "SELECT date, net_mf FROM fact_moneyflow WHERE code=? ORDER BY date DESC LIMIT 5",(code,)))
    out["etf_basket_association"]=_d(c.execute(
        "SELECT e.name etf, e.chain_id chain FROM rel_etf_cons rc JOIN dim_etf e ON e.code=rc.etf_code "
        "WHERE rc.con_code=? LIMIT 10",(code,)))
    out["holders"]=_d(c.execute(
        "SELECT holder, ratio, end_date FROM rel_holder_stock WHERE code=? ORDER BY ratio DESC LIMIT 8",(code,)))
    out["lhb"]=_d(c.execute(
        "SELECT date, seat, side, amount FROM fact_lhb_seat WHERE code=? ORDER BY date DESC LIMIT 10",(code,)))
    c.close(); return out

def chain(name):
    """链全息：环节 / 成分股 / 资金 / ETF / 跨链簇"""
    c=con(); out={"chain":name}
    out["segments"]=_d(c.execute(
        "SELECT name seg, n_words FROM dim_segment WHERE chain_id=? ORDER BY seg_id",(name,)))
    out["top_stocks"]=_d(c.execute(
        "SELECT f.code, s.name, f.pct, f.amount FROM fact_snapshot f "
        "LEFT JOIN dim_stock s ON s.code=f.code WHERE f.date=(SELECT MAX(date) FROM fact_snapshot) "
        "AND f.code IN (SELECT src_id FROM rel_edge WHERE rel_type='stock_chain' AND dst_id=? AND role='主业') "
        "ORDER BY f.amount DESC LIMIT 15",(name,)))
    out["moneyflow"]=_d(c.execute(
        "SELECT f.date, ROUND(SUM(f.net_mf)/10000.0,1) 净流入亿 FROM fact_moneyflow f "
        "JOIN rel_edge e ON e.rel_type='stock_chain' AND e.src_id=f.code AND e.role='主业' AND e.dst_id=? "
        "GROUP BY f.date ORDER BY f.date DESC LIMIT 5",(name,)))
    out["etf_flow"]=_d(c.execute(
        "SELECT date, ROUND(净申赎亿,1) 净申赎亿, n_etf FROM v_etf_chain_flow WHERE chain_id=? "
        "ORDER BY date DESC LIMIT 5",(name,)))
    out["clusters"]=[x[0] for x in c.execute(
        "SELECT cluster_id FROM theme_cross_cluster WHERE chains LIKE ?",('%'+name+'%',))]
    c.close(); return out

def theme():
    """今日题材全景：开盘啦题材 / 跨链簇 / 涨停家数"""
    c=con(); d=c.execute("SELECT MAX(date) FROM fact_kpl").fetchone()[0]
    out={"date":d,"method":"Tushare net_mf供应商净流向；ETF为有效覆盖内估算对价；非完整市场现金流"}
    out["kpl"]=_d(c.execute(
        "SELECT theme_name 题材, MAX(z_t_num) 涨停, MAX(hot_num) 人气 FROM fact_kpl WHERE date=? "
        "GROUP BY theme_name ORDER BY 涨停 DESC, 人气 DESC LIMIT 15",(d,)))
    out["cross"]=_d(c.execute(
        "SELECT members 成员, chains 跨链, n_chains 链数, zt_today 涨停, zt_avg20 均20, ratio 倍数 "
        "FROM theme_cross_cluster ORDER BY n_concepts DESC LIMIT 15"))
    out["limitup_count"]=c.execute(
        "SELECT COUNT(*) FROM fact_snapshot WHERE date=? AND (CASE WHEN code LIKE '30%' OR code LIKE '68%' "
        "THEN pct>=19.5 WHEN code LIKE '8%' OR code LIKE '4%' OR code LIKE '92%' THEN pct>=29 ELSE pct>=9.5 END)",(d,)).fetchone()[0]
    c.close(); return out

def flow():
    """今日资金全景：按链主力净流入/流出 + ETF 净申赎"""
    c=con(); d=c.execute("SELECT MAX(date) FROM fact_moneyflow").fetchone()[0]
    out={"date":d,"method":"Tushare net_mf供应商净流向；ETF为有效覆盖内估算对价；非完整市场现金流"}
    out["by_chain"]=_d(c.execute(
        "SELECT e.dst_id 链, ROUND(SUM(f.net_mf)/10000.0,1) 净流入亿, COUNT(*) 只数 "
        "FROM fact_moneyflow f JOIN rel_edge e ON e.rel_type='stock_chain' AND e.src_id=f.code AND e.role='主业' "
        "WHERE f.date=? GROUP BY e.dst_id ORDER BY 净流入亿 DESC",(d,)))
    out["etf"]=_d(c.execute(
        "SELECT chain_id 链, ROUND(净申赎亿,1) 净申赎亿 FROM v_etf_chain_flow WHERE date=? "
        "ORDER BY 净申赎亿 DESC",(d,)))
    c.close(); return out

def search(q):
    c=con()
    like=f'%{q}%'
    out={"stocks":_d(c.execute("SELECT code,name,sw_l3 FROM dim_stock WHERE name LIKE ? OR code LIKE ? LIMIT 20",(like,like))),
         "concepts":[x[0] for x in c.execute("SELECT concept FROM dim_concept WHERE concept LIKE ? LIMIT 20",(like,))],
         "chains":[x[0] for x in c.execute("SELECT name FROM dim_chain WHERE name LIKE ? LIMIT 10",(like,))]}
    c.close(); return out

ROUTES={'stock':stock,'chain':chain,'theme':theme,'flow':flow,'search':search}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u=urlparse(self.path); p=u.path.strip('/').split('/')
        try:
            if not p or p[0] not in ROUTES:
                body={"routes":list(ROUTES), "usage":"/stock/<code> /chain/<name> /theme /flow /search?q="}
            elif p[0]=='search':
                q=unquote(parse_qs(u.query).get('q',[''])[0])
                body=search(q)
            elif len(p)>1:
                body=ROUTES[p[0]](unquote(p[1]))
            else:
                body=ROUTES[p[0]]()
            raw=json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(200)
        except Exception as e:
            raw=json.dumps({"error":str(e)[:200]}, ensure_ascii=False).encode(); self.send_response(500)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(raw)
    def log_message(self,*a): pass

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('cmd'); ap.add_argument('arg',nargs='?')
    ap.add_argument('--port',type=int,default=6061); a=ap.parse_args()
    if a.cmd=='serve':
        print(f'kb_api 监听 127.0.0.1:{a.port}')
        ThreadingHTTPServer(('127.0.0.1',a.port), H).serve_forever()
    elif a.cmd in ROUTES:
        fn=ROUTES[a.cmd]
        r=fn(a.arg) if a.arg is not None else fn()
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    else:
        print('用法: stock <code> | chain <name> | theme | flow | search <q> | serve --port 6061')
