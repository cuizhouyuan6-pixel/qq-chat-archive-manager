"""Message actions: local identity cards, read-only navigation and explicit Save As."""
import os,shutil,tempfile
from pathlib import Path
from PySide6.QtCore import Qt,QDateTime
from PySide6.QtGui import QImageReader
from PySide6.QtWidgets import (QApplication,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QListWidgetItem,
                              QMenu,QFileDialog,QAbstractItemView)
from .chrome import FramedDialog,AnimatedButton
from .date_widgets import DateTimeField,stamp
from .store import conversation_title
from .config import save_config
from .dialogs import MessageBox
from .modeless import FileDialog as QFileDialog
from PySide6.QtGui import QCursor
from shiboken6 import isValid

def avatar_menu(reader,message,point=None,*,owner=None,details=True):
    # Chat avatars always pass a conversation and keep both actions. Search sender
    # entries may have no conversation (owner names the account) or, for numbers
    # the user typed without a matching profile, no details at all.
    cid=message.get('conversation_id');conv=reader.arc.conversation(cid) if cid is not None else None
    uin=message['sender_uin'] or (conv['owner'] if conv and message.get('is_self') else '')
    menu=QMenu(reader);reader._avatar_menu=menu
    copy=menu.addAction('复制 QQ 号',lambda:QApplication.clipboard().setText(uin));copy.setEnabled(bool(uin))
    if details:menu.addAction('查看详细信息',lambda:show_person(reader,message,owner=owner))
    menu.aboutToHide.connect(menu.deleteLater)
    menu.popup(point or QCursor.pos());return menu

def copy_attachment(source,target,cancel,progress):
    source=Path(source);target=Path(target)
    if source.resolve()==target.resolve():raise ValueError('保存位置与原文件相同，请选择其他位置')
    before=source.stat();fd,tmp=tempfile.mkstemp(prefix='.saving-',dir=target.parent);done=0
    try:
        with os.fdopen(fd,'wb') as out,source.open('rb') as src:
            while data:=src.read(1024*1024):
                if cancel.is_set():raise InterruptedError('另存为已取消，原文件保留')
                out.write(data);done+=len(data);progress('保存附件',done,before.st_size)
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise OSError('原文件在保存期间变化，请重试')
        if cancel.is_set():raise InterruptedError('另存为已取消')
        os.replace(tmp,target)
    finally:Path(tmp).unlink(missing_ok=True)
    return str(target)

def show_person(reader,message,owner=None):
    """Identity card. Without a conversation (search sender entries) the caller passes
    the account; the card then hides "last message", which is per conversation."""
    host=reader.archive_host;cid=message.get('conversation_id')
    conv=dict(reader.arc.conversation(cid)) if cid is not None else None
    owner=conv['owner'] if conv else owner
    uin=message['sender_uin'] or (owner if message.get('is_self') else '');uid=message.get('sender_uid','')
    name=message.get('sender_name') or message.get('sender_card') or uin or '未知用户'
    def task(archive):return archive.user_history(owner,uin,conv['id'] if conv else None,uid)
    def done(info):
        if not isValid(reader):return
        d=FramedDialog(reader.window());d.setWindowTitle('用户资料');d.resize(450,620);l=QVBoxLayout(d);l.setContentsMargins(24,20,24,24);l.setSpacing(14)
        title=QLabel(info['profile'].get('name') or name);title.setObjectName('title');title.setWordWrap(True);l.addWidget(title)
        account=QLabel('QQ 号：'+(uin or '未识别'));account.setTextInteractionFlags(Qt.TextSelectableByMouse);l.addWidget(account)
        if conv:
            last=info['last'];line=QHBoxLayout();label=QLabel('在当前聊天最后发言\n'+(stamp(last['time']) if last else '没有对应记录'));label.setWordWrap(True);line.addWidget(label,1)
            locate=AnimatedButton('📍');locate.setToolTip('定位到该用户在当前聊天中的最后一次发言');locate.setEnabled(bool(last));line.addWidget(locate);l.addLayout(line)
            def jump_last():
                reader.open_message({**last,'conversation_id':conv['id']});d.accept()
            locate.clicked.connect(jump_last)
        else:
            hint=QLabel(f'未在具体聊天中打开，不显示最后发言 · 资料来自归档账号 {owner}');hint.setObjectName('muted');hint.setWordWrap(True);l.addWidget(hint)
        private=info['private']
        if private:
            chat=AnimatedButton('查看私聊记录');chat.setToolTip('在新的独立窗口打开与该用户的已归档私聊');chat.clicked.connect(lambda:host.chat_windows.open(private['id']));l.addWidget(chat)
        l.addWidget(QLabel(f"拥有过的共同群聊 · {len(info['groups'])}"));groups=QListWidget();groups.setWordWrap(True);l.addWidget(groups,1)
        for group in info['groups']:
            item=QListWidgetItem(conversation_title(group)+'\n最后发言 '+stamp(group['spoken_at']));item.setData(Qt.UserRole,group['id']);groups.addItem(item)
        groups.itemClicked.connect(lambda item:host.chat_windows.open(item.data(Qt.UserRole)))
        note=QLabel('依据本账号归档中该用户的群聊发言判断，不代表当前仍在群内。');note.setObjectName('muted');note.setWordWrap(True);l.addWidget(note)
        close=AnimatedButton('关闭');close.clicked.connect(d.accept);l.addWidget(close);d.exec()
    host.run_read_job('读取用户资料',task,done)

def message_menu(reader,message,point):
    host=reader.archive_host;menu=QMenu(reader);menu.addAction('复制消息内容',lambda:QApplication.clipboard().setText(message['text']))
    media=[p for p in reader.arc.media_for_message(message['id']).values() if p.get('kind') in ('image','video','file')]
    for item in media:
        path=item.get('abs_path');exists=bool(path and Path(path).is_file())
        container=menu.addMenu(item.get('name') or '附件') if len(media)>1 else menu
        save=container.addAction('另存为…',lambda checked=False,m=item:save_attachment(reader,m));save.setEnabled(exists)
        folder=container.addAction('打开所在文件夹',lambda checked=False,p=path:host.open_folder(Path(p).parent));folder.setEnabled(exists)
        if not exists:container.addAction('附件未找到').setEnabled(False)
    menu.exec(point)

def save_attachment(reader,media):
    host=reader.archive_host;source=Path(media['abs_path']);cfg=host.cfg
    folder=cfg.save_folder or (cfg.save_folder_history[0] if cfg.save_folder_history else str(Path.home()/'Downloads'))
    dialog=QFileDialog(reader.window(),'另存为',str(Path(folder)/source.name));dialog.setAcceptMode(QFileDialog.AcceptSave)
    dialog.setHistory(cfg.save_folder_history);dialog.setNameFilters([f'原始文件 (*{source.suffix})','所有文件 (*)'])
    if not dialog.exec():return
    path=Path(dialog.selectedFiles()[0])
    def done(result):
        chosen=str(path.parent);cfg.save_folder_history=[chosen]+[p for p in cfg.save_folder_history if p!=chosen][:7];save_config(cfg);host.statusBar().showMessage('已保存到 '+str(path),5000)
    host.run_job('另存为附件',lambda c,s:copy_attachment(source,path,c,s),done)

def locate_time(reader):
    if not reader.current:return
    d=FramedDialog(reader.window());d.setWindowTitle('按时间定位');d.resize(480,260);l=QVBoxLayout(d)
    l.addWidget(QLabel('选择有聊天记录的日期，或输入精确到秒的时间'))
    low,high=reader.arc.time_bounds(reader.current['id'])
    field=DateTimeField(QDateTime.fromSecsSinceEpoch(high) if high else None);field.bind_archive(reader.arc,lambda:reader.current['id']);l.addWidget(field)
    button=AnimatedButton('定位');button.setObjectName('primary');button.setEnabled(low is not None);button.setDefault(True);l.addWidget(button)   # Enter in the time field locates
    if low is None:l.addWidget(QLabel('当前会话没有带有效时间的消息'))
    def jump():
        if not field.normalize():return
        reader.set_messages(reader.arc.at_time(reader.current['id'],field._value.toSecsSinceEpoch()),False)
        rows=reader.model.rows;target=next((r for r in rows if r['time']>=field._value.toSecsSinceEpoch()),None)
        if target:reader.focus_message(target['id'])
        d.accept()
    button.clicked.connect(jump);d.exec()
