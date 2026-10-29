"""Chat browsing actions shared by main and detached views."""
import time
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QTextEdit,QLineEdit,QComboBox,QListWidget,QListWidgetItem,QAbstractItemView)
from .store import SearchQuery,conversation_title
from .chrome import AnimatedButton,FramedDialog as QDialog,fade_in

def button(text,fn,primary=False):
    w=AnimatedButton(text);w.clicked.connect(fn)
    if primary:w.setObjectName('primary')
    return w

def stamp(value):
    try:return time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(value))
    except (ValueError,OSError,TypeError):return '时间未知'

class ChatActions:
    @property
    def archive_host(self):
        # MainWindow.owner is the import-account input, not a window owner.
        return self if hasattr(self,'run_job') else self.owner
    def enrich_messages(self,rows):
        profiles={}
        for m in rows:
            identity=m['sender_uin'] or m.get('sender_uid','')
            if identity not in profiles:profiles[identity]=self.arc.profile(self.current['owner'],'private',identity,m.get('sender_uid',''))
            profile=profiles[identity]
            if profile.get('avatar_path'):m['avatar_path']=str(self.arc.resolve_media_path(profile['avatar_path']))
            if not m['sender_name'] and profile.get('name'):m['sender_name']=profile['name']
        return rows
    def set_messages(self,rows,bottom=True):
        self._exhausted=set()
        if hasattr(self.chat,'begin_transition'):self.chat.begin_transition()
        self.enrich_messages(rows)
        self.model.replace(rows,self.arc.media_for_messages([m['id'] for m in rows]))
        # A persistent opacity effect on a scroll area may reuse its cached
        # viewport image after a model reset. Animate the header; redraw rows.
        self.chat.doItemsLayout();self.chat.viewport().update()
        if bottom:self.chat.scrollToBottom()
        self.chat.viewport().update()
    def latest(self):
        if self.current:
            self._exhausted=set();self.set_messages(self.arc.latest(self.current['id'],100));self._exhausted.add(1)
    def page_messages(self,direction):
        if not self.current or not self.model.rows:return
        anchor=self.model.rows[0 if direction<0 else -1];fn=self.arc.before if direction<0 else self.arc.after
        rows=fn(self.current['id'],anchor['time'],anchor['id'],100)
        if rows:self.set_messages(rows,bottom=direction>0)
        else:self.statusBar().showMessage('当前方向没有更多已归档消息',4000)
    def message_detail(self,index):
        from .forwards import descriptor
        if descriptor(index.data(Qt.UserRole)) is not None:
            from .forward_window import open_forward
            return open_forward(self,index.data(Qt.UserRole))
        m=index.data(Qt.UserRole);d=QDialog(self);d.setWindowTitle('消息详情');d.resize(720,600);l=QVBoxLayout(d)
        text=QTextEdit();text.setReadOnly(True);lines=[m['text'],'',stamp(m['time']),'QQ：'+m['sender_uin'],'','来源：']
        lines += [f"批次 {s['batch_id']} · {s['source_kind']} · {s['source_label']}" for s in self.arc.message_sources(m['id'])]
        lines += ['\n冲突版本：'+v['text'] for v in self.arc.variants(m['id'])];text.setPlainText('\n'.join(lines));l.addWidget(text)
        for med in self.arc.media_for_message(m['id']).values():
            if med.get('abs_path'):l.addWidget(button('打开附件 · '+med.get('name','文件'),lambda _=False,p=med['abs_path']:self.open_file(p)))
        l.addWidget(button('关闭',d.accept));d.exec()
    def search_dialog(self,initial_types=()):
        from .search_window import SearchWindow
        host=self.archive_host;key=('chat',self.current['id'] if self.current else None)
        if not hasattr(host,'search_windows'):host.search_windows={}
        existing=host.search_windows.get(key)
        if existing and existing.isVisible():existing.raise_();existing.activateWindow();return existing
        dialog=SearchWindow(self,initial_types if isinstance(initial_types,tuple) else ());host.search_windows[key]=dialog
        dialog.finished.connect(lambda *_:host.search_windows.pop(key,None));dialog.show();return dialog
    def open_message(self,message):
        cid=message['conversation_id']
        if hasattr(self,'select_conversation'):
            from PySide6.QtWidgets import QTreeWidgetItem
            item=QTreeWidgetItem();item.setData(0,Qt.UserRole,cid);self.select_conversation(item)
        else:
            self.current=self.arc.conversation(cid);self.chat_title.setText(conversation_title(self.current))
            self.chat_subtitle.setText(f"{self.current['peer']} · 归档账号 {self.current['owner']}")
            if hasattr(self,'conversation_changed'):self.conversation_changed()
        self.set_messages(self.arc.around(message['id']),False);self.focus_message(message['id'])
    def focus_message(self,mid):
        for i,message in enumerate(self.model.rows):
            if message['id']==mid:
                index=self.model.index(i);self.chat.setCurrentIndex(index);self.chat.scrollTo(index,QAbstractItemView.PositionAtCenter);break
        self.chat.viewport().update()
    def connect_chat_actions(self):
        from .chat_tools import avatar_menu,message_menu
        self.chat.avatarClicked.connect(lambda message:avatar_menu(self,message))
        self.chat.avatarContext.connect(lambda message,point:avatar_menu(self,message,point))
        self.chat.contextMessage.connect(lambda message,point:message_menu(self,message,point))
        self.chat.boundary.connect(self.load_boundary);self._exhausted=set();self._paging=False
    def load_boundary(self,direction):
        if self._paging or not self.current or not self.model.rows or direction in self._exhausted:return
        self._paging=True
        try:
            anchor=self.model.rows[0 if direction<0 else -1]
            query=self.arc.before if direction<0 else self.arc.after
            rows=query(self.current['id'],anchor['time'],anchor['id'],100)
            if not rows:self._exhausted.add(direction);return
            self.enrich_messages(rows)
            old=list(self.model.rows);point=self.chat.indexAt(__import__('PySide6.QtCore',fromlist=['QPoint']).QPoint(8,8))
            visible=old[point.row()]['id'] if point.isValid() else anchor['id'];offset=self.chat.visualRect(point).top() if point.isValid() else 0
            combined=(rows+old)[:300] if direction<0 else (old+rows)[-300:]
            self.model.replace(combined,self.arc.media_for_messages([r['id'] for r in combined]));self.chat.doItemsLayout()
            for i,m in enumerate(combined):
                if m['id']==visible:
                    self.chat.scrollTo(self.model.index(i),QAbstractItemView.PositionAtTop)
                    bar=self.chat.verticalScrollBar();bar.setValue(bar.value()-offset);break
            self._exhausted.discard(-direction)
        finally:self._paging=False
    def chat_toolbar(self):
        from PySide6.QtWidgets import QWidget,QHBoxLayout
        from .icons import line_icon
        from .chat_tools import locate_time
        bar=QWidget();bar.setObjectName('chatTools');layout=QHBoxLayout(bar);layout.setContentsMargins(20,12,20,12);layout.setSpacing(8)
        actions=[('image','查看图片与视频',lambda:self.search_dialog(('image','video'))),('file','查看文件',lambda:self.search_dialog(('file',))),
                 ('search','搜索聊天记录',lambda:self.search_dialog()),('calendar','按时间定位',lambda:locate_time(self)),('latest','回到最新记录',self.latest)]
        for icon,title,action in actions:
            b=AnimatedButton('');b.setObjectName('chatTool');b.setIcon(line_icon(icon));b.setFixedSize(40,38);b.setToolTip(title);b.setAccessibleName(title);b.clicked.connect(action);layout.addWidget(b)
        layout.addStretch()
        if hasattr(self,'toggle_details'):
            details=AnimatedButton('');details.setObjectName('chatTool');details.setIcon(line_icon('info'));details.setFixedSize(40,38);details.setToolTip('会话信息');details.setAccessibleName('会话信息');details.clicked.connect(self.toggle_details);layout.addWidget(details)
        return bar
