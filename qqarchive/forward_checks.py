"""Isolated visual/interaction check using fictitious forwarded messages only."""
import json
import os
import time
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def run(root):
    root = Path(root).resolve(); root.mkdir(parents=True, exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR'] = str(root / 'config')
    os.environ['QQARCHIVE_DISABLE_TRANSFER_DETECTION'] = '1'
    from .config import Config
    from .desktop import MainWindow
    from .models import NormalizedMessage
    from .importer import Importer, ImportRequest
    from .forward_window import open_forward
    app = QApplication.instance()
    host = MainWindow(Config(data_dir=str(root / 'data'), transfer_detection=False))
    report = {'passed': False}
    try:
        inner = {'sender': {'user_id': '200002', 'nickname': '林晓'}, 'time': 1760000042,
                 'message': [{'type': 'text', 'data': {'text': '这一条来自转发内部，号码由原始字段提供。'}}]}
        messages = [
            {'sender': {'user_id': '200001', 'nickname': '陈默'}, 'time': 1760000000,
             'message': [{'type': 'text', 'data': {'text': '周末的行程已经整理好了，大家可以看一下。'}}]},
            inner,
            {'sender': {'nickname': '仅保留昵称的消息'}, 'message': [{'type': 'text', 'data': {'text': '这条没有发送者号码，界面应显示未知。'}}]},
            {'sender': {'user_id': '200003', 'nickname': '周舟'}, 'time': 1760000090,
             'message': [{'type': 'forward', 'data': {'content': [inner]}}]},
        ]
        msg = NormalizedMessage('100001', 'group', '900001', 1760000100, sender_uin='200004',
                                seq=1, elements=[{'type': 'forward', 'text': '周末出行讨论',
                                                  'forward': {'id': '', 'content': messages}}])
        Importer(host.arc).run(ImportRequest('preview', 'synthetic-forward'), [msg])
        host.refresh()
        conv = host.arc.find_conversation('100001', 'group', '900001')
        message = host.arc.latest(conv['id'], 1)[0]
        d = open_forward(host, message)
        deadline = time.monotonic() + 10
        while not d.nodes and time.monotonic() < deadline:
            app.processEvents(); time.sleep(.01)
        assert len(d.nodes) == 4
        until = time.monotonic() + .4
        while time.monotonic() < until:
            app.processEvents(); time.sleep(.01)
        d.grab().save(str(root / 'forward.png'))
        assert d.chat.width() > 350 and d.windowModality() == Qt.NonModal
        menu = d.avatar(d.model.rows[0]); menu.actions()[0].trigger(); menu.hide()
        assert app.clipboard().text() == '200001'
        d.query.setText('200002'); assert d.model.rowCount() == 1
        d.query.clear(); d.open_node(d.model.index(3)); assert d.nodes[0]['sender_uin'] == '200002'
        d.go_back(); assert len(d.nodes) == 4
        d.toggle_options()
        until = time.monotonic() + .25
        while time.monotonic() < until:
            app.processEvents(); time.sleep(.01)
        d.grab().save(str(root / 'forward-options.png'))
        report.update(passed=True, nodes=4, modeless=True, copy=True, search=True, nested=True,
                      unknown_identity=True, width=d.width(), height=d.height(), dpr=d.devicePixelRatioF())
        d.stop(); d.hide()
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        (root / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        host.request_exit()
    return 0 if report['passed'] else 1
