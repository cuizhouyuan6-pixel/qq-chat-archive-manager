"""Nonmodal history backfill workflow, sharing the app's serialized task runner."""
from pathlib import Path
import threading
from PySide6.QtCore import Qt, QDateTime
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QWidget, QScrollArea,
                              QLineEdit, QComboBox, QCheckBox, QSpinBox, QFileDialog,
                              QListWidget, QListWidgetItem, QAbstractItemView)
from .chrome import FramedDialog
from .flow_widgets import Step
from .smooth_scroll import SmoothScroll
from .date_widgets import DateTimeField
from .config import save_config
from .history import Scope, HistoryClient, run, summary
from .dialogs import MessageBox


def open_history(host):
    previous = getattr(host, 'history_window', None)
    if previous:
        previous.show(); previous.raise_(); previous.activateWindow()
        return previous
    window = HistoryWindow(host)
    host.history_window = window
    window.show()
    return window


class HistoryWindow(FramedDialog):
    def __init__(self, host):
        super().__init__(None)
        self.fixed_body = True
        from .desktop import label, button, row
        self.host = host; self.verified = None; self.busy = False
        self.connect_cancel = threading.Event()
        self.setWindowTitle('补全 QQ 历史记录')
        self.resize(820, 760); self.setMinimumSize(580, 460)
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 18, 24, 20); layout.setSpacing(14)
        layout.addWidget(label('补全缺失的历史记录', 'title'))
        layout.addWidget(label('从已登录 QQ 的本机接口读取，并合并到现有聊天。不会复制原数据库。', 'muted'))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget(); content = QVBoxLayout(body); content.setContentsMargins(0, 0, 4, 0); content.setSpacing(16)
        scroll.setWidget(body); layout.addWidget(scroll, 1); self.scroll_motion = SmoothScroll(scroll)
        self.connection = Step(1, '连接已登录的 QQ',
            '需要另外运行 NapCat 并登录本人账号，启用 HTTP 服务，消息格式选择 array。'
            '普通 QQ 登录不会自动提供此接口。服务地址仅允许本机；访问令牌仅在本次窗口内使用。')
        self.owner = QLineEdit(); self.owner.setPlaceholderText('归档账号 QQ 号')
        self.endpoint = QLineEdit(getattr(host.cfg, 'forward_endpoint', 'http://127.0.0.1:3000'))
        self.endpoint.setAccessibleName('NapCat 本机 HTTP 地址')
        self.token = QLineEdit(); self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText('NapCat HTTP 访问令牌（如有）')
        self.connect_button = button('验证连接与账号', self.connect_qq, True)
        box = self.connection.box
        box.addWidget(label('归档账号', 'fieldLabel')); box.addWidget(self.owner)
        box.addWidget(label('本机接口地址与访问令牌', 'fieldLabel')); box.addWidget(row(self.endpoint, self.token))
        box.addWidget(self.connect_button); self.connection.expand(True, False); content.addWidget(self.connection)
        self.range_step = Step(2, '选择会话和时间', '同一范围可以断点继续，也可重新检查。每页成功提交后保存断点。')
        self.known = QComboBox(); self.known.addItem('手动填写群号 / 私聊 QQ 号', None)
        for c in getattr(host, '_session_records', []):
            self.known.addItem(c.get('name') or c['peer'], (c['owner'], c['kind'], c['peer']))
        self.kind = QComboBox(); self.kind.addItem('群聊', 'group'); self.kind.addItem('私聊', 'private')
        self.peer = QLineEdit(); self.peer.setPlaceholderText('群号 / 私聊 QQ 号')
        self.start = DateTimeField(QDateTime.currentDateTime().addDays(-1))
        self.end = DateTimeField()
        self.pages = QSpinBox(); self.pages.setRange(1, 10000); self.pages.setValue(2000); self.pages.setSuffix(' 页 / 次')
        self.batch = QSpinBox(); self.batch.setRange(10, 100); self.batch.setSingleStep(10)
        self.batch.setValue(20); self.batch.setSuffix(' 条 / 页')
        box = self.range_step.box; box.addWidget(self.known); box.addWidget(row(self.kind, self.peer))
        # Stacked date fields remain readable at 200% display scaling.
        box.addWidget(label('开始时间（包含）', 'fieldLabel')); box.addWidget(self.start)
        box.addWidget(label('结束时间（包含）', 'fieldLabel')); box.addWidget(self.end)
        box.addWidget(row(label('本次读取上限'), self.pages))
        box.addWidget(row(label('每页请求数量'), self.batch))
        content.addWidget(self.range_step); self.range_step.unlock(False)
        self.attachments = Step(3, '附件选项',
            '已有本机文件直接引用。勾选后下载 QQ 返回的附件链接；单文件最多 2 GB。'
            '失效或不受支持的链接保留为待补齐，不影响已导入正文。')
        self.media = {}
        choices = QWidget(); cl = QHBoxLayout(choices); cl.setContentsMargins(0, 0, 0, 0)
        for kind, title in [('image','图片'),('video','视频'),('voice','语音'),('file','文件')]:
            cb = QCheckBox(title); self.media[kind] = cb; cl.addWidget(cb)
        self.folder = QLineEdit(str(host.cfg.data_path / '历史补全附件'))
        self.attachments.box.addWidget(choices)
        self.attachments.box.addWidget(row(self.folder, button('保存目录', self.choose_folder)))
        self.attachments.box.addWidget(button('仅重试该范围的待补齐附件', lambda: self.start_job(media_only=True)))
        content.addWidget(self.attachments); self.attachments.unlock(False)
        self.past = QListWidget(); self.past.setMaximumHeight(150)
        self.past.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.past_scroll = SmoothScroll(self.past); self.past.itemDoubleClicked.connect(self.select_job)
        content.addWidget(label('最近补全任务 · 双击恢复范围', 'fieldLabel')); content.addWidget(self.past)
        content.addStretch()
        self.note = label('请先验证连接。接口返回的记录不代表全部云端历史。', 'muted')
        self.note.setWordWrap(True); layout.addWidget(self.note)
        self.begin = button('开始 / 继续补全', self.start_job, True)
        self.restart = button('重新检查这一范围', lambda: self.start_job(True))
        self.reopen = button('查看当前任务', self.host.restore_task)
        layout.addWidget(row(self.reopen, self.restart, self.begin))
        for edit in (self.owner, self.endpoint, self.token):
            edit.textChanged.connect(self.invalidate)
        self.known.currentIndexChanged.connect(self.select_known)
        self.reload_jobs(); self.invalidate()
        owners = {c['owner'] for c in getattr(host, '_session_records', [])}
        if len(owners) == 1:
            self.owner.setText(next(iter(owners)))

    def identity(self):
        return (self.owner.text().strip(), self.endpoint.text().strip(), self.token.text())

    def invalidate(self, *_):
        valid = self.verified == self.identity()
        self.range_step.unlock(valid); self.attachments.unlock(valid)
        self.begin.setEnabled(valid and not self.busy); self.restart.setEnabled(valid and not self.busy)
        if not valid:
            self.connection.note.setText('请验证当前连接与归档账号')

    def connect_qq(self):
        if self.busy:
            return
        owner, endpoint, token = identity = self.identity()
        try:
            # Validate locally before starting background I/O.
            Scope(owner, 'group', '1', 1, 2); HistoryClient(endpoint, token)
        except ValueError as exc:
            self.note.setText(str(exc)); return
        self.busy = True; self.connect_button.setEnabled(False)
        self.note.setText('正在验证接口与账号…')
        self.connect_cancel = cancel = threading.Event()
        def task(archive):
            try:
                data = HistoryClient(endpoint, token, cancel).verify(owner)
                return {'ok': True, 'nickname': str(data.get('nickname') or '')}
            except Exception as exc:
                return {'ok': False, 'error': str(exc)}
        def done(result):
            self.busy = False; self.connect_button.setEnabled(True)
            if identity != self.identity():
                self.invalidate(); return
            if not result['ok']:
                self.note.setText(result['error']); self.invalidate(); return
            self.verified = identity
            self.host.cfg.forward_endpoint = endpoint; save_config(self.host.cfg)
            self.invalidate(); self.connection.complete('登录账号已核对')
            self.connection.expand(False); self.range_step.expand(True); self.attachments.expand(False)
            self.note.setText('连接已就绪。选择范围后开始；读取过程中请保持接口登录同一账号。')
        self.host.run_read_job('验证 QQ 历史接口', task, done)

    def select_known(self):
        data = self.known.currentData()
        if data:
            owner, kind, peer = data
            self.owner.setText(owner); self.kind.setCurrentIndex(0 if kind == 'group' else 1); self.peer.setText(peer)

    def choose_folder(self):
        value = QFileDialog.getExistingDirectory(self, '选择新下载附件的保存位置', self.folder.text())
        if value:
            self.folder.setText(value)

    def start_job(self, restart=False, media_only=False):
        if self.host.worker is not None:
            self.note.setText('已有任务正在执行，可以先隐藏窗口，完成后再开始。'); return
        if self.verified != self.identity():
            self.invalidate(); return
        try:
            if not self.start.normalize() or not self.end.normalize():
                raise ValueError('请检查起止日期，需精确到秒')
            scope = Scope(self.owner.text().strip(), self.kind.currentData(), self.peer.text().strip(),
                          self.start.dateTime().toSecsSinceEpoch(), self.end.dateTime().toSecsSinceEpoch())
            folder = Path(self.folder.text()).resolve()
        except ValueError as exc:
            self.note.setText(str(exc)); return
        _, endpoint, token = self.identity()
        kinds = tuple(k for k, cb in self.media.items() if cb.isChecked()); pages = self.pages.value(); page_size = self.batch.value()
        self.busy = True; self.invalidate()
        def task(cancel, status):
            return self.host.with_archive(lambda archive: run(
                archive, HistoryClient(endpoint, token, cancel), scope, cancel=cancel, status=status,
                restart=bool(restart), max_pages=pages, page_size=page_size, media_kinds=kinds, media_root=folder, media_only=media_only))
        def done(result):
            self.busy = False; self.invalidate(); self.reload_jobs()
            text = summary(result)
            if 'media' in result:
                m = result['media']
                text += f"\n附件：引用 {m['linked']} · 下载 {m['downloaded']} · 待补齐 {m['missing']} · 失败 {m['failed']}"
            self.note.setText(text)
        # Main task dialog owns cancel/pause + elapsed time + hide-to-bottom.
        self.host.run_job('导入 · QQ 历史补全', task, done)
        if self.host.worker is not None:
            self.host.worker.finished.connect(self.finished_job)

    def finished_job(self):
        self.busy = False; self.invalidate()

    def reload_jobs(self):
        self.past.clear()
        exists = self.host.arc.con.execute("SELECT 1 FROM sqlite_master WHERE name='history_jobs'").fetchone()
        if not exists:
            return
        import json
        for r in self.host.arc.con.execute("SELECT * FROM history_jobs ORDER BY updated DESC LIMIT 50"):
            scope = json.loads(r['scope'])
            start = QDateTime.fromSecsSinceEpoch(scope['start']).toString('MM-dd HH:mm')
            end = QDateTime.fromSecsSinceEpoch(scope['end']).toString('MM-dd HH:mm')
            item = QListWidgetItem(f"{scope['peer']} · {start} — {end} · 新增 {r['inserted']:,}")
            item.setToolTip(r['note']); item.setData(Qt.UserRole, scope); self.past.addItem(item)

    def select_job(self, item):
        if self.busy:
            return
        s = item.data(Qt.UserRole); self.owner.setText(s['owner'])
        self.kind.setCurrentIndex(0 if s['kind'] == 'group' else 1); self.peer.setText(s['peer'])
        self.start.setDateTime(QDateTime.fromSecsSinceEpoch(s['start']))
        self.end.setDateTime(QDateTime.fromSecsSinceEpoch(s['end']))
        self.note.setText(item.toolTip())

    def reject(self):
        # Hiding must not cancel an active serialized job or retain UI secrets.
        self.token.clear()
        self.connect_cancel.set()
        super().reject()
