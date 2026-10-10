"""Can FTS5 trigram help 1-2 character keywords? Measured on a bench_search archive.

Usage: .venv\\Scripts\\python.exe scripts\\bench_short_terms.py build\\bench-search\\base
Compares the current LIKE scan with trigram MATCH, LIKE on the FTS table and a
prefix query, reporting matches, median seconds and EXPLAIN QUERY PLAN.
"""
import json,sqlite3,statistics,sys,time
from pathlib import Path

def run(con,sql,args,repeat=3):
    secs=[];value=None
    for _ in range(repeat):
        t=time.perf_counter();value=con.execute(sql,args).fetchone()[0];secs.append(time.perf_counter()-t)
    plan=[r[3] for r in con.execute('EXPLAIN QUERY PLAN '+sql,args)]
    return dict(sql=sql,args=args,count=value,median=round(statistics.median(secs),4),plan=plan)

def main(directory):
    db=Path(directory)/'archive.db';con=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True);out=[]
    try:
        for term in ('颜色','雨','下雨天'):
            out.append(dict(term=term,cases=[
                run(con,"SELECT count(*) FROM messages WHERE hidden=0 AND text LIKE ? ESCAPE '\\'",(f'%{term}%',)),
                run(con,'SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?',('"'+term+'"',)),
                run(con,'SELECT count(*) FROM messages_fts WHERE text LIKE ?',(f'%{term}%',)),
                run(con,'SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?',('"'+term+'"*',)),
            ]))
            for c in out[-1]['cases']:print(f"{term:4s} count {c['count']:>6} median {c['median']:.4f}s | {c['sql'][:70]} | {' / '.join(c['plan'])}",flush=True)
    finally:con.close()
    (Path(directory)/'short-terms.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main(sys.argv[1])
