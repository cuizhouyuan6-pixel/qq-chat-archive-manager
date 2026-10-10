"""Optional QQ identity metadata. Never infer a person's avatar from a hashed filename."""
from pathlib import Path
from .avatars import find_avatar
from .cipherdb import CipherConnection,PlainConnection
from .discovery import _candidate
from .source_guard import signature,SourceChangedError,CHANGED_MESSAGE

def related_sources(candidate):
    path=Path(candidate.path)
    if path.name.lower()!='nt_msg.db':return []
    return [c for name in ('group_info.db','profile_info.db')
            if (path.parent/name).is_file() and (c:=_candidate(path.parent/name,'pc',candidate.owner))]

def _text(value):
    if value is None:return ''
    if isinstance(value,bytes):return value.decode('utf-8','strict')
    return str(value)

def read_profiles(candidate,keys,cancel=None):
    """Short, authenticated reads; reject all results if DB/WAL changes mid-read."""
    before=signature(candidate.path);opened=None
    if candidate.needs_key:
        for key,params in keys.candidates(candidate.owner,candidate.path):
            try:opened=CipherConnection.open(candidate.path,key,params);break
            except Exception:continue
        if opened is None:raise ValueError(Path(candidate.path).name+' 需要独立密钥，请登录 QQ 后重新自动获取密钥')
    else:opened=PlainConnection(candidate.path)
    records={}
    try:
        conn=opened.conn
        if cancel:conn.set_progress_handler(cancel.is_set,1000)
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if Path(candidate.path).name=='group_info.db':
            adapters=[('group_detail_info_ver1','group','60001','60007',None,None),('group_list','group','60001','60007',None,None)]
        else:adapters=[('profile_info_v6','private','1002','20002','1000','20009')]
        recognized=False
        for table,kind,peer,name,uid,remark in adapters:
            if table not in tables:continue
            cols={r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if not {peer,name}.issubset(cols):continue
            recognized=True
            wanted=[peer,name]+[c for c in (uid,remark) if c and c in cols]
            for row in conn.execute('SELECT '+','.join('"'+c+'"' for c in wanted)+f' FROM "{table}"'):
                if cancel and cancel.is_set():raise InterruptedError('资料补齐已取消')
                d=dict(zip(wanted,row));p=_text(d[peer]);u=_text(d.get(uid))
                if p in ('','0'):p=u
                if not p:continue
                title=_text(d.get(remark)) or _text(d.get(name))
                records[kind,p]=dict(owner=candidate.owner,kind=kind,peer=p,uid=u,name=title,avatar_path=find_avatar(candidate.path,kind,p,u),source=Path(candidate.path).name)
        if not recognized:raise ValueError(Path(candidate.path).name+' 的资料表尚不支持')
    finally:opened.close()
    if signature(candidate.path)!=before:raise SourceChangedError(CHANGED_MESSAGE)
    return list(records.values())

def apply_profiles(archive,records,cancel=None):
    changed=0
    with archive.con:
        for p in records:
            if cancel and cancel.is_set():raise InterruptedError('资料补齐已取消')
            archive.con.execute('INSERT INTO profiles(owner,kind,peer,uid,name,avatar_path,source) VALUES(?,?,?,?,?,?,?) '
                'ON CONFLICT(owner,kind,peer) DO UPDATE SET uid=excluded.uid,name=CASE WHEN excluded.name!=\'\' THEN excluded.name ELSE profiles.name END, '
                "avatar_path=CASE WHEN profiles.source='user' AND excluded.source!='user' AND profiles.avatar_path!='' THEN profiles.avatar_path WHEN excluded.avatar_path!='' THEN excluded.avatar_path ELSE profiles.avatar_path END,source=CASE WHEN profiles.source='user' AND profiles.avatar_path!='' THEN profiles.source ELSE excluded.source END",
                (p['owner'],p['kind'],p['peer'],p.get('uid',''),p.get('name',''),p.get('avatar_path',''),p.get('source','')))
            if p.get('name'):
                cur=archive.con.execute("UPDATE conversations SET name=? WHERE owner=? AND kind=? AND custom_name=0 AND name!=? "
                    "AND (peer=? OR (?!='' AND (peer_uid=? OR peer=?)))",(p['name'],p['owner'],p['kind'],p['name'],p['peer'],p.get('uid',''),p.get('uid',''),p.get('uid','')))
                changed+=cur.rowcount
    return changed

def sync_profiles(archive,candidate,keys,*,cancel=None,progress=None):
    records=[];errors=[]
    for source in related_sources(candidate):
        if progress:progress('补齐群名和联系人资料 · '+Path(source.path).name,0,0)
        try:records.extend(read_profiles(source,keys,cancel))
        except InterruptedError:raise
        except Exception as exc:errors.append(str(exc))
    # A former group can be absent from today's membership table while its
    # exact group-code avatar is still cached. Keep that identity independent
    # of names, membership, and the successful decryption of today's profiles.
    known={(p['kind'],p['peer']) for p in records}
    for c in archive.conversations(candidate.owner):
        if cancel and cancel.is_set():raise InterruptedError('资料补齐已取消')
        if (c['kind'],c['peer']) in known:continue
        avatar=find_avatar(candidate.path,c['kind'],c['peer'],c.get('peer_uid',''))
        if avatar:records.append(dict(owner=c['owner'],kind=c['kind'],peer=c['peer'],uid=c.get('peer_uid',''),name='',avatar_path=avatar,source='qq-avatar-cache'))
    changed=apply_profiles(archive,records,cancel)
    return {'updated':changed,'profiles':len(records),'errors':errors,
            'avatars':sum(bool(p.get('avatar_path')) for p in records),
            'avatar_note':f"已关联 {sum(bool(p.get('avatar_path')) for p in records):,} 个本机头像；QQ 未缓存或资料库中没有身份映射的头像保留占位。"}
