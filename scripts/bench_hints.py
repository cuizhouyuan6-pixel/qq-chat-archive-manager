"""Compare INDEXED BY / NOT INDEXED / no hint for the global grouping query.

Usage: .venv\\Scripts\\python.exe scripts\\bench_hints.py build\\bench-search\\idx [--repeat 5]
Runs Archive.search_conversations' SQL with each hint on a synthetic benchmark
archive (see bench_search.py) and prints median seconds and the query plan.
"""
import argparse,json,statistics,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qqarchive.store import Archive,SearchQuery

def grouping_sql(arc,q,hint):
    conds,args=arc._search_conditions(q)
    conds=[c.replace('c.owner = ?','m.conversation_id IN (SELECT id FROM conversations WHERE owner=?)') for c in conds]
    return ("SELECT c.*,hit.matches,hit.matched_at FROM (SELECT m.conversation_id,count(*) AS matches,max(m.time) AS matched_at FROM messages m "+hint+
            " WHERE "+' AND '.join(conds)+" GROUP BY m.conversation_id) hit JOIN conversations c ON c.id=hit.conversation_id ORDER BY hit.matched_at DESC,c.id LIMIT 1000"),args

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('directory');ap.add_argument('--repeat',type=int,default=5);a=ap.parse_args(argv)
    db=Path(a.directory)/'archive.db';out=[]
    probe=Archive(db,read_only=True)
    t0,t1=probe.con.execute('SELECT min(time),max(time) FROM messages').fetchone();probe.close();mid=(t0+t1)//2
    cases=[]
    for owner in (None,'10000'):   # SearchWindow sends no owner; owner-scoped callers exist too
        tag='' if owner is None else ' · 限账号'
        cases+=[('仅发送人'+tag,SearchQuery(owner=owner,senders=('20001',))),('罕见发送人'+tag,SearchQuery(owner=owner,senders=('20002',))),
                ('发送人+30天'+tag,SearchQuery(owner=owner,senders=('20001',),start=mid,end=mid+30*86400)),('2 字关键词'+tag,SearchQuery(owner=owner,keyword='颜色'))]
    hints=['INDEXED BY ix_msg_sender','NOT INDEXED','']
    for title,q in cases:
        for hint in hints:
            if hint.startswith('INDEXED BY') and not q.senders:continue
            secs=[];plan=None;rows=None
            for _ in range(a.repeat):
                arc=Archive(db,read_only=True)
                try:
                    sql,args=grouping_sql(arc,q,hint);t=time.perf_counter();rows=arc.con.execute(sql,args).fetchall();secs.append(time.perf_counter()-t)
                    if plan is None:plan=[r[3] for r in arc.con.execute('EXPLAIN QUERY PLAN '+sql,args)]
                finally:arc.close()
            out.append(dict(case=title,hint=hint or '(无提示)',median=round(statistics.median(secs),4),groups=len(rows),plan=plan))
            print(f"{title:12s} {hint or '(无提示)':26s} median {out[-1]['median']:.4f}s groups {len(rows)} | "+' / '.join(p for p in plan if p.startswith(('SCAN m','SEARCH m'))),flush=True)
    (Path(a.directory)/'hints.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
