"""Read-only identity audit and append-only recovery from a stable QQ source.

No variants are fabricated as messages: accurate identities/times always come
from the original database. Source index queries avoid reimporting 13M rows.
"""
from dataclasses import dataclass,asdict
from .ntqq import TABLES,WANTED,row_to_message
from .merge import merge_key,collision_key,identity_conflicts
from .discovery import source_fingerprint
from .sources import open_ntqq
from .source_guard import StableSource
from .importer import Importer,ImportRequest,ImportCancelled

@dataclass
class RecoveryAudit:
    sequence_groups:int=0
    source_rows:int=0
    existing:int=0
    recoverable:int=0
    excluded:int=0

def recover_identities(archive,candidate,keystore,*,apply=False,cancel=None,progress=None):
    """Only reconcile conversations/time ranges previously imported from path."""
    if candidate.kind=='package':raise ValueError('请选原始 QQ 消息数据库进行完整性核对')
    fingerprint=source_fingerprint(candidate.path);audit=RecoveryAudit();con=archive.con
    batches=[dict(r) for r in con.execute("SELECT id,params,range_start,range_end FROM batches WHERE source_label=? AND status='done' AND source_kind!='identity-recovery'",(candidate.path,))]
    if not batches:raise ValueError('该来源没有成功导入历史，请先执行正常导入')
    import json
    ranges=[]
    for b in batches:
        params=json.loads(b['params']);ranges.append((params.get('conversations','all'),b['range_start'],b['range_end']))
    convs={(r['kind'],r['peer']):r['id'] for r in con.execute('SELECT id,kind,peer FROM conversations WHERE owner=?',(candidate.owner,))}
    def checked():
        if cancel is not None and cancel.is_set():raise ImportCancelled('已取消完整性核对')
    def notify(stage):
        if progress:progress(stage,asdict(audit))
    with StableSource(candidate.path) as stable,open_ntqq(candidate,keystore) as src:
        src.set_cancel(cancel);pairs={}
        for kind,table in TABLES.items():
            if not src.has_table(kind):continue
            checked();notify('读取消息身份索引')
            index=table+'_idx40027_40003'
            actual=next(src.conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'index\' AND name=?',(index,)),None)
            if not actual:raise ValueError('来源缺少可核对的消息身份索引；需要全表核对，未修改归档')
            pairs[kind]=list(src.conn.execute(f'SELECT "40027","40003",count(*) FROM {table} INDEXED BY {index} GROUP BY "40027","40003" HAVING count(*)>1'))
            audit.sequence_groups+=len(pairs[kind])
        def missing():
            for kind,pair_list in pairs.items():
                table=TABLES[kind];select=[c for c in WANTED if c in src._columns[kind]]
                columns=','.join('"'+c+'"' for c in select)
                query=f'SELECT {columns} FROM {table} WHERE "40027"=? AND "40003"=?'
                for internal,seq,_ in pair_list:
                    checked()
                    if src.table_reader:
                        ids=list(src.conn.execute(f'SELECT rowid FROM {table} INDEXED BY {table}_idx40027_40003 WHERE "40027"=? AND "40003"=?',(internal,seq)))
                        records=(src.table_reader.row(table,r[0],set(select)) for r in ids)
                    else:records=(dict(zip(select,r)) for r in src.conn.execute(query,(internal,seq)))
                    for raw in records:
                        if raw is None:raise ValueError('消息索引指向不存在的正文记录；未修改归档')
                        audit.source_rows+=1;m=row_to_message(kind,raw,candidate.owner)
                        cid=convs.get((kind,m.peer));eligible=cid and any((chosen=='all' or f'{kind}:{m.peer}' in chosen) and (start is None or m.time>=start) and (end is None or m.time<=end) for chosen,start,end in ranges)
                        if not eligible:audit.excluded+=1;continue
                        key,_=merge_key(m,'ntqq-pc')
                        row=con.execute('SELECT id,sender_uin,sender_uid,time,msg_random FROM messages WHERE conversation_id=? AND merge_key=? AND hidden=0',(cid,key)).fetchone()
                        if row is not None and identity_conflicts(row,m):
                            key=collision_key(key,m)
                            row=con.execute('SELECT id FROM messages WHERE conversation_id=? AND merge_key=? AND hidden=0',(cid,key)).fetchone()
                        if row is None:
                            audit.recoverable+=1;yield m
                        else:audit.existing+=1
                    if audit.source_rows%1000<30:notify('核对原始消息与归档')
            stable.check()
            if source_fingerprint(candidate.path)!=fingerprint:raise OSError('来源发生变化，核对无效')
        if apply:
            request=ImportRequest('identity-recovery',candidate.path,source_fingerprint=fingerprint,owner=candidate.owner,
                params={'recovery':'sequence-identity-v1','source_fingerprint':fingerprint,'conversations':'previously_imported'})
            stats=Importer(archive).run(request,missing(),cancel=cancel)
            result={'audit':asdict(audit),'import':stats.to_dict(),'applied':True}
        else:
            for _ in missing():pass
            result={'audit':asdict(audit),'applied':False}
        result['source_fingerprint']=fingerprint;notify('核对完成');return result
