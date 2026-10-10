"""Repeatable search benchmark on a synthetic archive (no real chat data).

Usage:
  .venv\\Scripts\\python.exe scripts\\bench_search.py --output build\\bench-search\\base
  .venv\\Scripts\\python.exe scripts\\bench_search.py --output build\\bench-search\\base --reuse

--output must not exist unless --reuse is given. The archive is generated with a
fixed seed, imported through the real Importer, then every scenario is run with a
fresh read-only Archive exactly as SearchWindow's ReadJob does. Results (timings,
match counts and EXPLAIN QUERY PLAN of every statement) go to results.json/.md.
Timings are wall-clock on this machine with a warm OS file cache; they are a
comparison baseline, not a speed promise for other computers.
"""
import argparse,json,random,statistics,sys,time
from dataclasses import replace
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qqarchive.store import Archive,SearchQuery
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage

OWNER='10000';TARGET='20001'   # TARGET speaks in many groups and has a private chat
RARE='20002'                   # only a few dozen messages, all inside the largest group
CHARS=('的一是不了人我在有他这中大来上国个到说们为子和你地出道也时年得就那要下以生会自着去之过家学对可里后小么心多天而能好都然没日于起'
       '还发成事只作当想看文无开手十用主行方又如前所本见经头面公同三已老从动两长知民样现分将外但身些与高意进把法此实回二理美点月明其种声全工己话'
       '儿者向情部正名定女问力机给等几很业最间新什打便位因重被走电四第门相次东政海口使教西再平真听世气信北少关并内加化由却代军产入先山五太水万市'
       '眼体别处总才场师书比住员九笑性通目华报立马命张活难神数件安表原车白应路期叫死常提感金何更反合放做系计或司利受光王果亲界及今京务制解各任至'
       '清物台象记边共风战干接它许八特觉望直服毛林题建南度统色字请交爱让认算论百吃义科怎元社术结六功指思非流每青管夫连远资队跟带花快条院变联言权')
WORDS=None

def words(rng):
    global WORDS
    if WORDS is None:WORDS=[CHARS[i]+CHARS[(i*7+3)%len(CHARS)] for i in range(len(CHARS))]
    return WORDS

def generate(total,seed):
    rng=random.Random(seed);vocab=words(rng)
    # 60 groups + 20 private chats with a skewed size distribution (largest ~25%).
    convs=[('group',str(900000+i)) for i in range(60)]+[('private',str(30000+i)) for i in range(19)]+[('private',TARGET)]
    weights=[1/(i+1)**0.9 for i in range(len(convs))];scale=total/sum(weights)
    sizes=[max(50,int(w*scale)) for w in weights];sizes[0]+=total-sum(sizes)
    target_groups={convs[i][1] for i in range(0,60,5)}   # 12 groups
    start=1_660_000_000;span=3*365*86400
    for (kind,peer),n in zip(convs,sizes):
        pool=[str(100000+rng.randrange(50000)) for _ in range(min(2000,max(5,n//400)))] if kind=='group' else [peer,OWNER]
        step=span/max(1,n);t=start+rng.randrange(86400)
        for i in range(n):
            t+=max(1,int(rng.expovariate(1/step)));sender=rng.choice(pool)
            if kind=='group' and peer in target_groups and rng.random()<0.01:sender=TARGET
            if kind=='group' and peer==convs[0][1] and rng.random()<0.0002:sender=RARE
            r=rng.random()
            if r<0.06:elements=[{'type':'image','name':f'{rng.getrandbits(64):016x}.png','md5':f'{rng.getrandbits(128):032x}'}]
            elif r<0.10:elements=[{'type':'face','text':'/微笑'}]
            else:
                text=''.join(rng.choice(vocab) for _ in range(rng.randint(1,12)))
                x=rng.random()
                if x<0.003:text+='下雨天'
                elif x<0.011:text+='颜色'
                if rng.random()<0.01:text+=' https://example.invalid/'+str(i)
                elements=[{'type':'text','text':text}]
            yield NormalizedMessage(OWNER,kind,peer,min(t,start+span),sender_uin=sender,sender_name='用户'+sender[-4:],
                                    is_self=sender==OWNER,seq=i+1,msg_random=rng.getrandbits(31),elements=elements,peer_name=f'{kind}-{peer}')

def build(db,total,seed,pre_sql=()):
    arc=Archive(db)
    try:
        for sql in pre_sql:arc.con.execute(sql)   # e.g. a candidate index present during import
        t=time.perf_counter();stats=Importer(arc).run(ImportRequest('bench','synthetic'),generate(total,seed))
        return dict(import_seconds=round(time.perf_counter()-t,2),inserted=stats.inserted,read=stats.read,pre_sql=list(pre_sql))
    finally:arc.close()

def apply_post_sql(db,statements):
    """Time statements (e.g. CREATE INDEX on an existing archive) on a copied database."""
    import sqlite3
    con=sqlite3.connect(db);out=[]
    try:
        for sql in statements:
            t=time.perf_counter();con.execute(sql);con.commit();out.append(dict(sql=sql,seconds=round(time.perf_counter()-t,2)))
    finally:con.close()
    return out

def plan(con,sql):
    rows=con.execute('EXPLAIN QUERY PLAN '+sql).fetchall();depth={0:-1};out=[]
    for node,parent,_,detail in rows:
        depth[node]=depth.get(parent,-1)+1;out.append('  '*depth[node]+detail)
    return out

def scenarios(arc):
    big=arc.con.execute("SELECT id FROM conversations ORDER BY msg_count DESC LIMIT 1").fetchone()[0]
    t0,t1=arc.con.execute('SELECT min(time),max(time) FROM messages').fetchone();mid=(t0+t1)//2
    # Same fields SearchWindow sends: no owner, 100 rows per page.
    g=lambda **k:SearchQuery(limit=100,**k)
    return [
        ('a','3+ 字关键词「下雨天」 · 全局',g(keyword='下雨天'),'global'),
        ('b1','2 字关键词「颜色」 · 全局',g(keyword='颜色'),'global'),
        ('b2','1 字关键词「雨」 · 全局',g(keyword='雨'),'global'),
        ('b4','1 字常见字「天」 · 全局',g(keyword='天'),'global'),
        ('b3','2 字关键词「颜色」 · 最大会话内',g(keyword='颜色',conversation_id=big),'conversation'),
        ('c','仅发送人 · 全局',g(senders=(TARGET,)),'global'),
        ('c2','仅发送人 · 全局 · 第 11 页',g(senders=(TARGET,),offset=1000),'page'),
        ('d','发送人 + 30 天时间范围 · 全局',g(senders=(TARGET,),start=mid,end=mid+30*86400),'global'),
        ('c3','罕见发送人 · 全局（只在最大会话发言）',g(senders=(RARE,)),'global'),
        ('c4','罕见发送人 · 最大会话内',g(senders=(RARE,),conversation_id=big),'conversation'),
        ('ab','长短关键词组合「下雨天 天」 · 全局',g(keyword='下雨天 天'),'global'),
        ('e','无关键词 · 仅最大会话',g(conversation_id=big),'conversation'),
        ('f','全部聊天发送人名单（无缓存）',None,'roster'),
    ]

def run_one(db,q,mode):
    """Mirror SearchWindow.search: global mode groups first, then the first group's page."""
    arc=Archive(db,read_only=True);sqls=[];arc.con.set_trace_callback(sqls.append)
    try:
        t=time.perf_counter();groups=None;tg=None
        if mode=='global':
            t1=time.perf_counter();groups=arc.search_conversations(replace(q,conversation_id=None,offset=0));tg=time.perf_counter()-t1
            target=groups[0]['id'] if groups else None
            rows=arc.search(replace(q,conversation_id=target)) if target is not None else []
        elif mode=='roster':
            import qqarchive.store as store
            store._sender_cache.clear();rows=arc.senders(None,'',100)
        elif mode=='page':
            target=arc.search_conversations(replace(q,conversation_id=None,offset=0))[0]['id']
            sqls.clear();t=time.perf_counter();rows=arc.search(replace(q,conversation_id=target))
        else:rows=arc.search(q)
        total=time.perf_counter()-t
    finally:arc.con.set_trace_callback(None)
    try:
        plans=[dict(sql=s,plan=plan(arc.con,s)) for s in sqls if s.lstrip().upper().startswith('SELECT')]
    finally:arc.close()
    return dict(seconds=total,group_seconds=tg,rows=len(rows),groups=len(groups) if groups is not None else None,
                matches=sum(x['matches'] for x in groups) if groups else None,plans=plans)

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--messages',type=int,default=1_000_000)
    ap.add_argument('--seed',type=int,default=20261010);ap.add_argument('--repeat',type=int,default=3);ap.add_argument('--reuse',action='store_true')
    ap.add_argument('--label',default='');ap.add_argument('--pre-sql',action='append',default=[],help='run before importing (new archive only)')
    ap.add_argument('--from-dir',help='copy archive.db/build.json from this benchmark directory instead of importing')
    ap.add_argument('--post-sql',action='append',default=[],help='timed on the copied archive, e.g. CREATE INDEX');ap.add_argument('--no-queries',action='store_true')
    a=ap.parse_args(argv)
    out=Path(a.output);db=out/'archive.db'
    if a.reuse:
        if not db.is_file():raise SystemExit('--reuse requires an existing '+str(db))
        meta=json.loads((out/'build.json').read_text(encoding='utf-8'))
    else:
        if out.exists():raise SystemExit('output already exists; pass a new directory or --reuse')
        out.mkdir(parents=True)
        if a.from_dir:
            import shutil
            source=Path(a.from_dir);shutil.copy2(source/'archive.db',db);meta=json.loads((source/'build.json').read_text(encoding='utf-8'))
            meta.update(copied_from=str(source),post_sql=apply_post_sql(db,a.post_sql))
        else:meta=dict(messages=a.messages,seed=a.seed,**build(db,a.messages,a.seed,a.pre_sql))
        (out/'build.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    if a.no_queries:print(json.dumps(meta,ensure_ascii=False));return 0
    import sqlite3
    arc=Archive(db,read_only=True)
    try:specs=scenarios(arc);indexes=[r[0] for r in arc.con.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages' ORDER BY name")]
    finally:arc.close()
    results=[]
    for key,title,q,mode in specs:
        runs=[run_one(db,q,mode) for _ in range(a.repeat)];secs=[r['seconds'] for r in runs]
        results.append(dict(key=key,title=title,mode=mode,first=round(secs[0],4),median=round(statistics.median(secs),4),
                            group_median=round(statistics.median(r['group_seconds'] for r in runs),4) if runs[0]['group_seconds'] is not None else None,
                            rows=runs[0]['rows'],groups=runs[0]['groups'],matches=runs[0]['matches'],plans=runs[0]['plans']))
        print(f"{key:3s} {title:28s} median {results[-1]['median']:8.3f}s  rows {runs[0]['rows']:4d}  groups {runs[0]['groups']}  matches {runs[0]['matches']}",flush=True)
    report=dict(label=a.label,sqlite=sqlite3.sqlite_version,build=meta,indexes=indexes,repeat=a.repeat,results=results,
                measured_at=time.strftime('%Y-%m-%d %H:%M:%S'))
    name='results'+('-'+a.label if a.label else '')
    (out/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=[f"# 搜索基准 {a.label}",'',f"SQLite {sqlite3.sqlite_version} · {meta['messages']:,} 条合成消息 · 导入 {meta['import_seconds']} 秒 · 每场景 {a.repeat} 次取中位数",'',
           '| 场景 | 中位耗时 (s) | 其中分组 (s) | 首次 (s) | 本页行数 | 命中会话 | 命中总数 |','|---|---|---|---|---|---|---|']
    for r in results:lines.append(f"| {r['key']} {r['title']} | {r['median']} | {r['group_median'] if r['group_median'] is not None else '—'} | {r['first']} | {r['rows']} | {r['groups'] if r['groups'] is not None else '—'} | {r['matches'] if r['matches'] is not None else '—'} |")
    lines+=['','## 查询计划','']
    for r in results:
        lines.append(f"### {r['key']} {r['title']}")
        for p in r['plans']:lines+=['```',p['sql'][:600],'--',*p['plan'],'```']
    (out/(name+'.md')).write_text('\n'.join(lines),encoding='utf-8')
    return 0

if __name__=='__main__':raise SystemExit(main())
