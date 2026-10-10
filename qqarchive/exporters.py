"""Streaming exports. HTML is split into bounded pages; attachments are opt-in."""
from __future__ import annotations
import csv
import hashlib
import html
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from .models import NormalizedMessage


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024*1024), b''):
            h.update(b)
    return h.hexdigest()


def normalized(row, conv):
    d = {k: row[k] for k in NormalizedMessage.__dataclass_fields__ if k in row}
    d.update(owner=conv['owner'], kind=conv['kind'], peer=conv['peer'], peer_name=conv['name'],
             key_hint=row['merge_key'], exact_hint=bool(row['exact_key']))
    d['elements'] = [{k:v for k,v in e.items() if k not in ('src_path','local_path','url','path')}
                     for e in row['elements']]
    return d


def csv_safe(value):
    s=str(value if value is not None else '')
    return "'"+s if s.lstrip().startswith(('=','+','-','@')) or s.startswith(('\t','\r')) else s


def export_archive(archive, destination, fmt='package', *, conversation_ids=None, start=None,
                   end=None, kinds=None, attachments=False, cancel=None, progress=None):
    """Destination must be a new directory. Publish only on successful completion."""
    target=Path(destination).resolve()
    if target.exists():
        raise FileExistsError('请为每次导出选择一个新的文件夹名称')
    if start is not None and end is not None and start>end:
        raise ValueError('开始时间不能晚于结束时间')
    if fmt not in ('package','html','json','csv','txt'):
        raise ValueError('不支持的导出格式')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.export-',dir=target.parent))
    convs=[c for c in archive.conversations() if conversation_ids is None or c['id'] in conversation_ids]
    count=variants=0
    missing=0
    page_links=[]
    files={}
    assets={}
    exported_profiles={}
    output=None
    page=None
    def checked():
        if cancel is not None and cancel.is_set():
            raise InterruptedError('导出已取消')
    def profile(owner,kind,peer,uid=''):
        key=(owner,kind,peer)
        if key in exported_profiles:return exported_profiles[key]
        p=archive.profile(owner,kind,peer,uid)
        result=dict(owner=owner,kind=kind,peer=peer,uid=uid,name=p.get('name',''),avatar_asset='')
        if attachments and p.get('avatar_path'):
            source=archive.resolve_media_path(p['avatar_path'])
            if source.is_file():
                checked();sig=digest(source);suffix=source.suffix.lower()
                if suffix not in ('.png','.jpg','.jpeg','.webp','.bmp','.gif'):
                    from .avatars import image_suffix
                    suffix=image_suffix(source) or '.img'
                rel='assets/'+sig+suffix
                if rel not in assets:
                    dest=stage/rel;dest.parent.mkdir(exist_ok=True);shutil.copyfile(source,dest)
                    if digest(dest)!=sig:raise OSError('头像在导出期间发生变化')
                    assets[rel]=sig
                result['avatar_asset']=rel
        exported_profiles[key]=result
        return result
    def write_page_header(f, title, nav):
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; media-src \'self\'">'
                '<style>body{font:15px "Microsoft YaHei",sans-serif;background:#f8f9fb;color:#202126;max-width:980px;margin:32px auto;padding:0 20px}a{color:inherit}.msg{margin:16px 0;display:flex;flex-direction:column;align-items:flex-start}.self{align-items:flex-end}.bubble{background:white;border:1px solid #e9eaed;border-radius:12px;padding:13px 18px;max-width:75%;white-space:pre-wrap;overflow-wrap:anywhere}.self .bubble{background:#25262b;color:white}small{color:#777;margin:5px}img{max-width:260px;max-height:220px}nav{padding:20px;background:white}</style>')
        f.write('<h1>'+html.escape(title)+'</h1><nav>'+nav+'</nav>')
    try:
        if fmt in ('package','json','txt','csv'):
            name={'package':'messages.jsonl','json':'messages.json','txt':'messages.txt','csv':'messages.csv'}[fmt]
            output=(stage/name).open('w',encoding='utf-8-sig' if fmt=='csv' else 'utf-8',newline='')
            if fmt=='json': output.write('[\n')
            if fmt=='csv':
                writer=csv.writer(output)
                writer.writerow(['归档账号','会话类型','群号/对方QQ','会话名称','时间','发送者QQ','昵称','内容','冲突'])
        for conv in convs:
            checked()
            peer_profile=profile(conv['owner'],conv['kind'],conv['peer'],conv['peer_uid'])
            conv_count=0
            page_num=0
            for row in archive.range(conv['id'],start,end):
                checked()
                if kinds and row['msg_type'] not in kinds: continue
                record=normalized(row,conv)
                sender_profile=profile(conv['owner'],'private',row['sender_uin'] or row['sender_uid'],row['sender_uid'])
                record['peer_avatar']=peer_profile['avatar_asset'];record['sender_avatar']=sender_profile['avatar_asset']
                media=archive.media_for_message(row['id']) if attachments else {}
                asset_refs={}
                for idx, m in media.items():
                    p=Path(m.get('abs_path') or '')
                    if not p.is_file():
                        missing+=1;continue
                    sig=digest(p)
                    ext=p.suffix.lower()[:12]
                    if not ext[1:].isalnum(): ext='.bin'
                    rel=f'assets/{sig}{ext}'
                    if rel not in assets:
                        dst=stage/rel;dst.parent.mkdir(exist_ok=True)
                        shutil.copyfile(p,dst)
                        if digest(dst)!=sig: raise OSError('附件在复制期间发生变化')
                        assets[rel]=sig
                    asset_refs[str(idx)]=rel
                record['assets']=asset_refs
                if fmt=='package':
                    output.write(json.dumps(record,ensure_ascii=False)+'\n')
                    # Preserve conflict content and the established merge identity.
                    for v in archive.variants(row['id']):
                        vr=dict(record, sender_uin=v['sender_uin'],sender_name=v['sender_name'],
                                sender_card='',elements=v['elements'],assets={})
                        output.write(json.dumps(vr,ensure_ascii=False)+'\n');variants+=1
                elif fmt=='json':
                    if count: output.write(',\n')
                    output.write(json.dumps(record,ensure_ascii=False))
                elif fmt=='txt':
                    output.write(f"[{conv['owner']} · {conv['kind']} {conv['name'] or conv['peer']} ({conv['peer']})] {time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(row['time']))} {row['sender_card'] or row['sender_name'] or row['sender_uin']}\n{row['text']}\n\n")
                elif fmt=='csv':
                    writer.writerow([csv_safe(x) for x in (conv['owner'],conv['kind'],conv['peer'],conv['name'],
                        time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(row['time'])),row['sender_uin'],
                        row['sender_card'] or row['sender_name'],row['text'],row['conflict'])])
                else:
                    if conv_count%500==0:
                        if page: page.close()
                        page_num+=1
                        name=f'chat-{conv["id"]}-{page_num}.html'
                        title=f"{conv['name'] or conv['peer']} · {conv['owner']} · 第 {page_num} 页"
                        page_links.append((name,title))
                        page=(stage/name).open('w',encoding='utf-8')
                        write_page_header(page,title,'<a href="index.html">会话与分页目录</a>')
                        if peer_profile['avatar_asset']:page.write(f'<img width="48" height="48" style="border-radius:12px" alt="会话头像" src="{peer_profile["avatar_asset"]}">')
                    who=html.escape(row['sender_card'] or row['sender_name'] or row['sender_uin'])
                    stamp=time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(row['time']))
                    avatar=f'<img width="32" height="32" style="border-radius:8px" alt="发送者头像" src="{sender_profile["avatar_asset"]}">' if sender_profile['avatar_asset'] else ''
                    page.write(f'<article class="msg {"self" if row["is_self"] else ""}">{avatar}<small>{who} · {stamp}{" · 存在冲突版本" if row["conflict"] else ""}</small><div class="bubble">{html.escape(row["text"])}')
                    for idx,rel in asset_refs.items():
                        is_image=row['elements'][int(idx)].get('type')=='image'
                        page.write(f'<br><a href="{rel}">'+(f'<img loading="lazy" src="{rel}">' if is_image else '打开附件')+'</a>')
                    page.write('</div></article>')
                count+=1;conv_count+=1
                if progress and count%500==0:progress(count,0)
            if page:page.close();page=None
        if output:
            if fmt=='json':output.write('\n]\n')
            output.close();output=None
        if fmt=='html':
            with (stage/'index.html').open('w',encoding='utf-8') as f:
                write_page_header(f,'QQ聊天记录管理器 · 离线聊天记录','每页最多 500 条，可使用浏览器页面查找；跨页检索请导入 QQ聊天记录管理器。')
                for name,title in page_links:f.write(f'<p><a href="{name}">{html.escape(title)}</a></p>')
        if fmt=='package':
            (stage/'profiles.json').write_text(json.dumps(list(exported_profiles.values()),ensure_ascii=False),encoding='utf-8')
        for p in stage.rglob('*'):
            if p.is_file():files[p.relative_to(stage).as_posix()]=digest(p)
        (stage/'manifest.json').write_text(json.dumps({'format':'qqarchive-v10' if fmt=='package' else fmt,
            'count':count,'variants':variants,'missing_attachments':missing,'files':files},ensure_ascii=False,indent=2),encoding='utf-8')
        stage.rename(target)
        return {'messages':count,'variants':variants,'attachments':len(assets),'missing':missing,'path':str(target)}
    finally:
        if output:output.close()
        if page:page.close()
        if stage.exists():shutil.rmtree(stage)
