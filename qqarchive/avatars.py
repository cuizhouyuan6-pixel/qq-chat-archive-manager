"""QQ NT disk-avatar addressing, verified against 9.9.33.52230.

The native path builder hashes the identity three times, appending the original
identity between rounds. Private avatars use UID (never guess it from UIN).
Only existing local image files are returned; no network or copied cache.
"""
import hashlib
from pathlib import Path

def cache_digest(identity):
    identity=str(identity)
    first=hashlib.md5(identity.encode('utf-8')).hexdigest()
    second=hashlib.md5((first+identity).encode('utf-8')).hexdigest()
    return hashlib.md5((second+identity).encode('utf-8')).hexdigest()

def image_suffix(path):
    try:
        with Path(path).open('rb') as f:head=f.read(16)
    except OSError:return ''
    if head.startswith(b'\xff\xd8\xff'):return '.jpg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):return '.png'
    if head[:6] in (b'GIF87a',b'GIF89a'):return '.gif'
    if head.startswith(b'RIFF') and head[8:12]==b'WEBP':return '.webp'
    if head.startswith(b'BM'):return '.bmp'
    return ''

def find_avatar(database,kind,peer,uid=''):
    db=Path(database)
    # Adapters for other backup layouts must supply a verified mapping first.
    if db.parent.name!='nt_db' or kind not in ('group','private'):return ''
    identity=str(peer) if kind=='group' else uid
    if not identity or identity=='0':return ''
    digest=cache_digest(identity);root=db.parent.parent/'nt_data'/'avatar'/('group' if kind=='group' else 'user')
    for size in ('b_','s_'):
        file=root/digest[:2]/(size+digest)
        if file.is_file() and image_suffix(file):return str(file)
    return ''
