"""Verified portable archive import, without trusting paths in a package."""
from __future__ import annotations
import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
from .models import NormalizedMessage
from .importer import Importer, ImportRequest
from .media import register_folder,link_media
from .exporters import digest

def safe_path(name):
    p=PurePosixPath(name)
    if not name or '\\' in name or ':' in name or p.is_absolute() or '..' in p.parts or any(ord(c)<32 for c in name):
        raise ValueError('归档内路径非法')
    if p.as_posix()!=name:raise ValueError('归档内路径不规范')
    return name

@contextmanager
def package_reader(path):
    path=Path(path)
    if path.is_dir():
        root=path.resolve()
        def read(name):
            p=(root/safe_path(name)).resolve()
            if not p.is_relative_to(root):raise ValueError('归档文件越过根目录')
            return p.open('rb')
        yield read
    else:
        with zipfile.ZipFile(path) as z:
            names=z.namelist()
            if len(names)!=len(set(names)):raise ValueError('归档存在重复文件名')
            for n in names:safe_path(n.rstrip('/'))
            yield z.open

def import_package(archive,path,*,progress=None,cancel=None,flt=None):
    with package_reader(path) as read:
        with read('manifest.json') as f:
            raw=f.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('清单过大')
        manifest=json.loads(raw)
        if manifest.get('format')!='qqarchive-v10':raise ValueError('不支持的归档格式')
        files=manifest.get('files',{})
        if not isinstance(files,dict) or 'messages.jsonl' not in files:raise ValueError('归档缺少消息清单')
        for name,expected in files.items():
            safe_path(name)
            if name not in ('messages.jsonl','profiles.json') and not name.startswith('assets/'):raise ValueError('归档包含未支持的文件')
            h=hashlib.sha256()
            with read(name) as f:
                for b in iter(lambda:f.read(1024*1024),b''):
                    if cancel is not None and cancel.is_set():raise InterruptedError('已取消')
                    h.update(b)
            if h.hexdigest()!=expected:raise ValueError('归档 SHA-256 校验失败')
        total=int(manifest.get('count',0))+int(manifest.get('variants',0))
        asset_links=[]
        identities=set();profiles=[]
        if 'profiles.json' in files:
            with read('profiles.json') as f:raw=f.read(32*1024*1024+1)
            if len(raw)>32*1024*1024:raise ValueError('资料清单过大')
            profiles=json.loads(raw)
            if not isinstance(profiles,list):raise ValueError('资料清单格式错误')
            for p in profiles:
                if not isinstance(p,dict) or not all(isinstance(p.get(k),str) for k in ('owner','kind','peer')):raise ValueError('资料身份格式错误')
                asset=p.get('avatar_asset','')
                if asset and (asset not in files or not asset.startswith('assets/')):raise ValueError('头像未包含在校验清单内')
        def messages():
            n=0
            with read('messages.jsonl') as stream:
                while True:
                    line=stream.readline(16*1024*1024+1)
                    if not line:break
                    if len(line)>16*1024*1024:raise ValueError('单条消息过大')
                    d=json.loads(line)
                    m=NormalizedMessage.from_dict(d)
                    n+=1
                    if flt is None or flt.accepts(m.kind,m.peer,m.time):
                        identities.add((m.owner,m.kind,m.peer));identities.add((m.owner,'private',m.sender_uin or m.sender_uid))
                        for idx,name in d.get('assets',{}).items():
                            if name not in files or not name.startswith('assets/'):
                                raise ValueError('消息附件未包含在校验清单内')
                            index=int(idx)
                            if not 0<=index<len(m.elements):raise ValueError('消息附件下标错误')
                            # Only selected messages' attachments are copied below.
                            asset_links.append((m.owner,m.kind,m.peer,m.key_hint,index,name,m.elements[index].get('name','')))
                        yield m
            if n!=total:raise ValueError('归档消息条数与清单不一致')
        req=ImportRequest('package',str(path),source_fingerprint=files['messages.jsonl'])
        result=Importer(archive).run(req,messages(),cancel=cancel,
            progress=(lambda s:progress(s,total)) if progress else None)
        # Media uses content-addressed paths; source files remain untouched.
        folder=archive.data_dir/'media'/'packages'
        folder.mkdir(parents=True,exist_ok=True)
        selected_assets={item[5] for item in asset_links}
        profiles=[p for p in profiles if (p['owner'],p['kind'],p['peer']) in identities]
        selected_assets.update(p['avatar_asset'] for p in profiles if p.get('avatar_asset'))
        for name,expected in files.items():
            if name not in selected_assets:continue
            suffix=PurePosixPath(name).suffix
            if not suffix[1:].isalnum() or len(suffix)>12:suffix='.bin'
            dst=folder/(expected+suffix)
            if dst.exists() and digest(dst)==expected:continue
            fd,tmp=tempfile.mkstemp(prefix='.asset-',dir=folder);os.close(fd)
            try:
                with read(name) as src,open(tmp,'wb') as out:shutil.copyfileobj(src,out,1024*1024)
                if digest(tmp)!=expected:raise ValueError('归档附件读取期间发生变化')
                os.replace(tmp,dst)
            finally:
                Path(tmp).unlink(missing_ok=True)
        register_folder(archive,folder,with_hash=True)
        # Package attachment mappings below are explicit; never scan all old messages.
        with archive.con:
            for owner,kind,peer,key,index,name,original_name in asset_links:
                m=archive.con.execute('SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.owner=? AND c.kind=? AND c.peer=? AND m.merge_key=? AND m.hidden=0',(owner,kind,peer,key)).fetchone()
                f=archive.con.execute("SELECT id FROM media_files WHERE sha256=? AND status='done' LIMIT 1",(files[name],)).fetchone()
                if m and f:
                    archive.con.execute('INSERT OR REPLACE INTO message_media VALUES(?,?,?,?)',(m[0],index,f[0],'linked'))
                    if original_name:archive.con.execute('UPDATE media_files SET name=? WHERE id=?',(original_name,f[0]))
        from .profiles import apply_profiles
        for p in profiles:
            asset=p.get('avatar_asset');p.pop('avatar_path',None);p['source']='package'
            if asset:
                suffix=PurePosixPath(asset).suffix
                if not suffix[1:].isalnum() or len(suffix)>12:suffix='.bin'
                p['avatar_path']=archive.relative_media_path(folder/(files[asset]+suffix))
        apply_profiles(archive,profiles)
        return result
