"""Bounded identification of QQ backup containers, not a successful import.

The extension header does not authenticate the underlying data. Sealed key
material is deliberately excluded from the public inspection result.
"""
from pathlib import Path

LIMITATION=('已识别 QQ 备份加密容器，但尚未取得并认证这份备份的独立密钥，也未验证消息表结构。'
            '电脑 QQ 的消息库密钥不等同于备份密钥；当前版本不能直接导入这种备份。'
            '原文件保持不变，不会登记为导入成功。')
ALTERNATIVE='可改用 QQ 自带的“迁移聊天记录到电脑”，迁移完成后导入电脑 QQ 的消息库（nt_msg.db）。'

def inspect_header(path,head=None):
    path=Path(path)
    if head is None:
        with path.open('rb') as f:head=f.read(1024)
    if not head.startswith(b'SQLite header 3\0') or b'QQ_NT DB' in head[:1024]:return None
    size=path.stat().st_size;extension=int.from_bytes(head[16:18],'big')
    return {'format':'qq-backup-container','extension_size':extension,'size':size,
            'page_alignment_4096':bool(extension>=32 and size>extension and (size-extension)%4096==0),
            'authenticated':False,'messages_verified':False,'importable':False}
