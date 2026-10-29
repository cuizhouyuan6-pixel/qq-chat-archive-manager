import json
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qqarchive.forward_window import open_forward
from qqarchive.forwards import cache_key, cache_path
from test_v13_interactions import ui, spin
from test_forwards import node


def open_empty(ui):
    app, w, _ = ui
    message = dict(w.model.rows[0], elements=[{'type':'forward', 'text':'测试转发'}])
    d = open_forward(w, message)
    QTest.qWait(220)
    return app, w, message, d


def close(app, w, d):
    d.reject(); QTest.qWait(220); app.processEvents()
    assert d.closed


def test_forward_reader_identity_copy_search_nested_and_persistent(ui, tmp_path):
    app, w, message, d = open_empty(ui)
    assert d.windowModality() == Qt.NonModal and open_forward(w, message) is d
    assert not d.nodes
    first = node()
    first['message'].append({'type':'forward','data':{'content':[node('654321','内部消息')]}})
    path = tmp_path / 'forward.json'; path.write_text(json.dumps({'messages':[first,node('', '号码未知')]}),encoding='utf-8')
    count_before = w.arc.conversation(w.current['id'])['msg_count']
    d.load_json(path); spin(app, lambda:d.future is None)
    assert len(d.nodes) == 2 and d.visible_nodes[0]['sender_uin'] == '123456'
    assert w.arc.conversation(w.current['id'])['msg_count'] == count_before
    menu = d.avatar(d.model.rows[0]); menu.actions()[0].trigger()
    assert QApplication.clipboard().text() == '123456'; menu.hide()
    d.query.setText('123456'); assert d.model.rowCount() == 1
    d.open_node(d.model.index(0)); assert d.nodes[0]['sender_uin'] == '654321'
    d.go_back(); assert d.query.text() == '123456' and len(d.nodes) == 2
    d.query.clear()
    menu = d.avatar(d.model.rows[1]); assert not menu.actions()[0].isEnabled(); menu.hide()
    assert cache_path(w.cfg.data_path,cache_key(w.current['owner'],message)).exists()
    close(app,w,d)
    d = open_forward(w,message); spin(app,lambda:bool(d.nodes))
    assert len(d.nodes) == 2
    close(app,w,d)


def test_failed_json_keeps_previous_rows_and_enter_does_not_close(ui, tmp_path):
    app,w,message,d = open_empty(ui)
    path=tmp_path/'forward.json'; path.write_text(json.dumps([node()]),encoding='utf-8')
    d.load_json(path); spin(app,lambda:d.future is None)
    path.write_text('{}',encoding='utf-8'); d.load_json(path); spin(app,lambda:d.future is None)
    assert len(d.nodes)==1 and '未找到' in d.status.text()
    d.query.setFocus();QTest.keyClick(d.query,Qt.Key_Return);QTest.qWait(200)
    assert d.isVisible()
    close(app,w,d)


def test_pagination_and_unknown_sender_filter(ui):
    app,w,message,d = open_empty(ui)
    from qqarchive.forwards import normalize
    d.show_nodes(normalize([node(str(123456+i%2),f'消息{i}') for i in range(250)]))
    assert d.model.rowCount()==100
    d.page(1); assert d.model.rowCount()==100 and d.offset==100
    d.page(1); assert d.model.rowCount()==50 and not d.next.isEnabled()
    d.people.setCurrentRow(1); assert d.offset==0 and d.model.rowCount()==100
    assert {n['sender_uin'] for n in d.visible_nodes}=={'123456'}
    close(app,w,d)


def test_cancel_keeps_ui_responsive_and_card_json_is_not_body(ui, tmp_path):
    app,w,message,d = open_empty(ui)
    d.start(lambda cancel: (cancel.wait(2), [])[1], '测试等待')
    assert d.future is not None and not d.fetch.isEnabled()
    d.query.setText('仍可输入'); assert d.query.text() == '仍可输入'
    d.cancel_read(); spin(app,lambda:d.future is None)
    assert '已取消' in d.status.text() and not d.nodes and d.fetch.isEnabled()
    from test_forwards import card
    path = tmp_path/'card.json';path.write_text(json.dumps(card()),encoding='utf-8')
    d.load_json(path);spin(app,lambda:d.future is None)
    assert d.ref.text() == 'forward-test-resource' and not d.nodes
    assert '尚未取得' in d.status.text() and d.options.isVisible()
    close(app,w,d)
