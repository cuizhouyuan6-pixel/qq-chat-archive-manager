"""Modeless forwarded-record viewer with bounded asynchronous acquisition."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                              QListWidget, QListWidgetItem, QMenu, QApplication,
                              QAbstractItemView, QWidget)
from .chrome import FramedDialog, AnimatedButton, fade_in
from .message_view import MessageView
from .smooth_scroll import SmoothScroll
from .modeless import FileDialog
from .config import save_config
from .forwards import (ForwardError, LocalQQ, normalize, descriptor, cache_key,
                       cache_path, save_cache, payload_from_file, uid_resolver)
from .store import Archive
from .forwards import recover_descriptor, resource_key, card_descriptor


def open_forward(reader, message):
    host = reader.archive_host
    if not hasattr(host, 'forward_windows'):
        host.forward_windows = {}
    key = message['id']
    previous = host.forward_windows.get(key)
    if previous:
        previous.show(); previous.raise_(); previous.activateWindow()
        return previous
    window = ForwardWindow(host, message)
    host.forward_windows[key] = window
    window.finished.connect(lambda *_: host.forward_windows.pop(key, None))
    window.show()
    return window


class ForwardWindow(FramedDialog):
    def __init__(self, host, message):
        super().__init__(None)
        self.fixed_body = True
        self.host = host
        self.message = dict(message)
        self.arc = host.arc
        self.archive_host = host
        conv = self.arc.conversation(message['conversation_id'])
        self.conv = dict(conv)
        self.owner = conv['owner']
        self.key = cache_key(self.owner, message)
        self.info = descriptor(message) or {}
        self.frames = []
        self.nodes = []
        self.visible_nodes = []
        self.offset = 0
        self.closed = False
        self.future = None
        self.cancel = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='forward-read')
        self.setWindowFlag(Qt.Window, True)
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle('转发聊天记录')
        self.resize(960, 760); self.setMinimumSize(700, 560)
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 16, 24, 24); layout.setSpacing(12)
        title = QLabel('转发聊天记录'); title.setObjectName('title'); layout.addWidget(title)
        note = QLabel('显示转发内部标注的发送者；不会计入当前群的发言或共同群聊。')
        note.setObjectName('muted'); note.setWordWrap(True); layout.addWidget(note)
        top = QHBoxLayout()
        self.back = AnimatedButton('返回上层'); self.back.setEnabled(False); self.back.clicked.connect(self.go_back); top.addWidget(self.back)
        self.query = QLineEdit(); self.query.setPlaceholderText('搜索本层消息、昵称或 QQ 号'); self.query.textChanged.connect(self.filter_changed); top.addWidget(self.query, 1)
        self.more_options = AnimatedButton('读取正文'); self.more_options.clicked.connect(self.toggle_options); top.addWidget(self.more_options)
        layout.addLayout(top)
        self.options = QWidget(); options = QVBoxLayout(self.options); options.setContentsMargins(0, 0, 0, 0); options.setSpacing(8)
        explain = QLabel('本地已有正文会直接显示。也可打开转发 JSON，或通过已登录同一账号的 NapCat 本机 HTTP 服务读取。令牌仅在本次窗口中使用。')
        explain.setWordWrap(True); explain.setObjectName('muted'); options.addWidget(explain)
        self.ref = QLineEdit(str(self.info.get('id') or message.get('source_msg_id') or ''))
        self.ref.setPlaceholderText('转发资源标识 / NapCat 可识别的消息 ID（不是归档行号）')
        self.ref.setAccessibleName('转发资源标识'); options.addWidget(self.ref)
        connect = QHBoxLayout()
        self.endpoint = QLineEdit(getattr(host.cfg, 'forward_endpoint', '') or 'http://127.0.0.1:3000')
        self.endpoint.setAccessibleName('本机接口地址'); connect.addWidget(self.endpoint, 2)
        self.token = QLineEdit(); self.token.setEchoMode(QLineEdit.Password); self.token.setPlaceholderText('访问令牌（如有）')
        self.token.setAccessibleName('本机接口访问令牌'); connect.addWidget(self.token, 1); options.addLayout(connect)
        buttons = QHBoxLayout()
        self.fetch = AnimatedButton('从 QQ 读取'); self.fetch.setObjectName('primary'); self.fetch.clicked.connect(self.fetch_online); buttons.addWidget(self.fetch)
        self.from_file = AnimatedButton('打开转发 JSON'); self.from_file.clicked.connect(self.choose_file); buttons.addWidget(self.from_file); buttons.addStretch()
        self.from_db = AnimatedButton('从原库补读卡片'); self.from_db.clicked.connect(self.choose_database); buttons.insertWidget(2, self.from_db)
        options.addLayout(buttons); layout.addWidget(self.options)
        body = QHBoxLayout()
        self.people = QListWidget(); self.people.setFixedWidth(210); self.people.setWordWrap(True)
        self.people.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.people.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.people_scroll = SmoothScroll(self.people)
        self.people.currentRowChanged.connect(self.filter_changed); body.addWidget(self.people)
        from .desktop import ChatModel, BubbleDelegate
        self.chat = MessageView(); self.chat.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.chat.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chat.message_font_size = host.cfg.chat_font_size
        self.model = ChatModel(); self.chat.setModel(self.model); self.chat.setItemDelegate(BubbleDelegate(self.model, self.chat))
        self.chat.avatarClicked.connect(self.avatar)
        self.chat.avatarContext.connect(self.avatar)
        self.chat.contextMessage.connect(self.message_menu)
        self.chat.doubleClicked.connect(self.open_node)
        body.addWidget(self.chat, 1); layout.addLayout(body, 1)
        self.status = QLabel('正在检查已保存的转发正文…'); self.status.setWordWrap(True); self.status.setObjectName('muted'); layout.addWidget(self.status)
        bottom = QHBoxLayout()
        self.prev = AnimatedButton('上一页'); self.prev.clicked.connect(lambda: self.page(-1)); bottom.addWidget(self.prev)
        self.next = AnimatedButton('下一页'); self.next.clicked.connect(lambda: self.page(1)); bottom.addWidget(self.next)
        bottom.addStretch()
        self.stop_button = AnimatedButton('取消读取'); self.stop_button.clicked.connect(self.cancel_read); self.stop_button.hide(); bottom.addWidget(self.stop_button)
        close = AnimatedButton('关闭'); close.clicked.connect(self.reject); bottom.addWidget(close); layout.addLayout(bottom)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(60)
        self.finished.connect(self.stop)
        QTimer.singleShot(0, self.load_local)

    def toggle_options(self):
        self.options.setVisible(not self.options.isVisible())
        if self.options.isVisible():
            fade_in(self.options)

    def load_local(self):
        path = cache_path(self.host.cfg.data_path, self.key)
        inline = self.info.get('content', self.info.get('messages'))
        if path.is_file():
            self.start(lambda cancel: self.parse(payload_from_file(path), cancel), '读取本地转发正文')
        elif isinstance(inline, list):
            self.start(lambda cancel: self.parse(inline, cancel), '解析转发正文')
        else:
            self.status.setText('尚未取得内部正文。旧版摘要不会包含内部发送者 QQ 号；可从 QQ 读取或打开转发 JSON。')
            self.show_nodes([])

    def parse(self, payload, cancel, persist=None):
        archive = Archive(self.host.cfg.data_path / 'archive.db', read_only=True)
        try:
            rows = normalize(payload, uid_resolver(archive, self.owner), cancel)
        finally:
            archive.close()
        if cancel.is_set():
            raise InterruptedError('已取消读取')
        if persist:
            key, source = persist
            save_cache(self.host.cfg.data_path, key, payload, source)
        return rows

    def start(self, fn, title, nested=False):
        if self.closed or self.future is not None:
            return
        self.cancel = threading.Event(); self.started = time.monotonic(); self.task_title = title; self.pending_nested = nested
        self.fetch.setEnabled(False); self.from_file.setEnabled(False); self.back.setEnabled(False)
        self.from_db.setEnabled(False)
        self.stop_button.show(); self.stop_button.setEnabled(True)
        self.status.setText(title + ' · 已执行 0 秒')
        self.future = self.pool.submit(fn, self.cancel)

    def fetch_online(self):
        ref, endpoint, token = self.ref.text().strip(), self.endpoint.text().strip(), self.token.text()
        try:
            LocalQQ(endpoint, token)
        except ForwardError as exc:
            self.status.setText(str(exc)); return
        self.host.cfg.forward_endpoint = endpoint; save_config(self.host.cfg)
        def work(cancel):
            payload = LocalQQ(endpoint, token, cancel).fetch(ref, self.owner)
            key = resource_key(self.owner, ref) if self.frames else self.key
            return self.parse(payload, cancel, persist=(key, 'QQ 本机接口'))
        self.start(work, '读取转发正文')

    def choose_file(self):
        path, _ = FileDialog.getOpenFileName(self, '打开转发正文 JSON', '', 'JSON 文件 (*.json)')
        if path:
            self.load_json(path)

    def load_json(self, path):
        # Retain the original file; cache only the selected parsed response.
        key = resource_key(self.owner, self.ref.text().strip()) if self.frames else self.key
        def work(cancel):
            payload = payload_from_file(path)
            card = card_descriptor(payload)
            if card is not None and card.get('id'):
                return {'card': card}
            return self.parse(payload, cancel, persist=(key, '本地 JSON'))
        self.start(work, '解析转发 JSON')

    def choose_database(self):
        if self.frames:
            self.status.setText('请先返回最外层；内部转发不对应接收群中的数据库行'); return
        path, _ = FileDialog.getOpenFileName(self, '选择原始 QQ 消息数据库', '', 'QQ 消息数据库 (*.db)')
        if path:
            self.recover_database(path)

    def recover_database(self, path):
        keys = self.host.keys.candidates(self.owner, path)
        sources = self.arc.message_sources(self.message['id'])
        source_id = next((s['source_msg_id'] for s in sources if s['source_kind'] == 'ntqq' and s['source_msg_id']), self.message.get('source_msg_id'))
        def work(cancel):
            return {'card': recover_descriptor(path, self.owner, self.conv['kind'], self.conv['peer'],
                                              source_id, self.message['time'], keys, cancel)}
        self.start(work, '只读定位原始转发卡片')

    def cancel_read(self):
        self.cancel.set(); self.stop_button.setEnabled(False); self.status.setText('正在取消读取…')

    def poll(self):
        if self.closed or self.future is None:
            return
        if not self.future.done():
            if not self.cancel.is_set():
                self.status.setText(f'{self.task_title} · 已执行 {int(time.monotonic() - self.started)} 秒')
            return
        future, self.future = self.future, None
        self.fetch.setEnabled(True); self.from_file.setEnabled(True); self.stop_button.hide()
        self.from_db.setEnabled(not self.frames)
        self.back.setEnabled(bool(self.frames))
        if self.cancel.is_set():
            self.status.setText('已取消；现有转发记录保留'); return
        try:
            rows = future.result()
        except (ValueError, OSError, InterruptedError) as exc:
            self.status.setText(str(exc)); return
        except Exception:
            self.status.setText('读取未完成，请检查正文格式或重试'); return
        if isinstance(rows, dict) and 'card' in rows:
            self.info = rows['card']; self.ref.setText(str(self.info['id']))
            self.options.show()
            self.status.setText('已取得转发资源标识。可点击“从 QQ 读取”获取内部正文；尚未取得发送者列表。')
            return
        if self.pending_nested:
            self.frames.append((self.nodes, self.query.text(), self.people.currentRow(), self.offset, self.ref.text(), self.chat.verticalScrollBar().value()))
        self.show_nodes(rows)
        self.options.hide()

    def show_nodes(self, nodes):
        self.nodes = nodes; self.offset = 0
        self.query.blockSignals(True); self.query.clear(); self.query.blockSignals(False)
        self.people.blockSignals(True); self.people.clear()
        all_item = QListWidgetItem(f'全部发送者 · {len(nodes):,} 条'); all_item.setData(Qt.UserRole, None); self.people.addItem(all_item)
        groups = {}
        for node in nodes:
            key = node['sender_uin'] or node['sender_uid'] or ('unknown', node['sender_name'])
            name = node['sender_card'] or node['sender_name'] or node['sender_uin'] or '未知发送者'
            if key not in groups:
                groups[key] = [name, node['sender_uin'], 0]
            groups[key][2] += 1
        for key, (name, uin, count) in groups.items():
            item = QListWidgetItem(f'{name}\n{uin or "未获取 QQ 号"} · {count:,} 条'); item.setToolTip(item.text()); item.setData(Qt.UserRole, key); self.people.addItem(item)
        self.people.setCurrentRow(0); self.people.blockSignals(False)
        self.back.setEnabled(bool(self.frames)); self.render()
        self.from_db.setEnabled(not self.frames and self.future is None)

    def filter_changed(self, *args):
        self.offset = 0; self.render()

    def render(self):
        item = self.people.currentItem(); selected = item.data(Qt.UserRole) if item else None
        term = self.query.text().strip().casefold()
        matches = []
        for node in self.nodes:
            identity = node['sender_uin'] or node['sender_uid'] or ('unknown', node['sender_name'])
            if selected is not None and selected != identity:
                continue
            if term and term not in '\n'.join(str(node.get(k, '')) for k in ('text', 'sender_uin', 'sender_name', 'sender_card')).casefold():
                continue
            matches.append(node)
        self.visible_nodes = matches[self.offset:self.offset + 100]
        rows = []
        for i, node in enumerate(self.visible_nodes):
            row = dict(node, id=i, is_self=False, conflict=False)
            if node['children'] or node['refs']:
                row['text'] += '\n[双击展开内部转发]'
            row['sender_name'] = (node['sender_card'] or node['sender_name'] or '未知发送者') + ' · ' + (node['sender_uin'] or 'QQ 号未知')
            row['sender_card'] = ''
            rows.append(row)
        self.chat.begin_transition(); self.model.replace(rows, {}); self.chat.doItemsLayout(); self.chat.scrollToTop()
        self.prev.setEnabled(self.offset > 0); self.next.setEnabled(self.offset + 100 < len(matches))
        known = sum(bool(n['sender_uin']) for n in self.nodes)
        if self.nodes:
            self.status.setText(f'本层 {len(self.nodes):,} 条 · {known:,} 条带 QQ 号 · 筛选 {len(matches):,} 条 · 第 {self.offset // 100 + 1} 页')

    def page(self, direction):
        self.offset = max(0, self.offset + direction * 100); self.render()

    def open_node(self, index):
        if not index.isValid() or self.future:
            return
        node = self.visible_nodes[index.row()]
        if node['children']:
            self.frames.append((self.nodes, self.query.text(), self.people.currentRow(), self.offset, self.ref.text(), self.chat.verticalScrollBar().value()))
            self.show_nodes(node['children'])
        elif node['refs']:
            if len(node['refs']) == 1:
                self.open_ref(node['refs'][0])
            else:
                menu = QMenu(self); self._menu = menu
                for i, ref in enumerate(node['refs'], 1):
                    menu.addAction(f'打开第 {i} 条内部转发', lambda checked=False, ref=ref: self.open_ref(ref))
                menu.aboutToHide.connect(menu.deleteLater); menu.popup(QCursor.pos())
        else:
            d = FramedDialog(self); d.setWindowTitle('转发消息详情'); d.resize(620, 480)
            from PySide6.QtWidgets import QTextEdit
            from .date_widgets import stamp
            layout = QVBoxLayout(d); text = QTextEdit(); text.setReadOnly(True)
            text.setPlainText(f'昵称：{node["sender_name"]}\nQQ：{node["sender_uin"] or "未获取"}\n身份来源：{node["identity_source"]}\n时间：{stamp(node["time"])}\n\n{node["text"]}')
            layout.addWidget(text); close = AnimatedButton('关闭'); close.clicked.connect(d.accept); layout.addWidget(close); d.show()
            self._detail = d

    def open_ref(self, ref):
        self.frames.append((self.nodes, self.query.text(), self.people.currentRow(), self.offset, self.ref.text(), self.chat.verticalScrollBar().value()))
        self.show_nodes([]); self.ref.setText(ref)
        path = cache_path(self.host.cfg.data_path, resource_key(self.owner, ref))
        if path.is_file():
            self.start(lambda cancel: self.parse(payload_from_file(path), cancel), '读取本地内部转发')
        else:
            self.options.show()
            self.status.setText('内部转发尚未保存正文，点击“从 QQ 读取”继续。')

    def go_back(self):
        if not self.frames or self.future:
            return
        nodes, term, person, offset, ref, scroll = self.frames.pop()
        self.show_nodes(nodes); self.query.setText(term); self.people.setCurrentRow(person); self.offset = offset; self.ref.setText(ref); self.render()
        self.chat.verticalScrollBar().setValue(scroll)

    def avatar(self, message, point=None):
        node = self.visible_nodes[message['id']]
        from .chat_tools import avatar_menu
        # No conversation_id: forwarded statements must not be mistaken for a
        # statement in the receiving group or used as its last message.
        return avatar_menu(self, dict(node, is_self=False), point, owner=self.owner)

    def message_menu(self, message, point):
        menu = QMenu(self); self._menu = menu
        menu.addAction('复制消息内容', lambda: QApplication.clipboard().setText(message['text']))
        node = self.visible_nodes[message['id']]
        copy = menu.addAction('复制 QQ 号', lambda: QApplication.clipboard().setText(node['sender_uin']))
        copy.setEnabled(bool(node['sender_uin']))
        menu.aboutToHide.connect(menu.deleteLater); menu.popup(point or QCursor.pos())

    def stop(self, *args):
        if self.closed:
            return
        self.closed = True; self.cancel.set(); self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True); self.token.clear()
