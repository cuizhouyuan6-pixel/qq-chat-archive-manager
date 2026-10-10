import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from qqarchive.store import Archive,SearchQuery
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.search_window import SearchWindow
from test_v13_interactions import ui,spin

T=1_760_000_000

def two_groups():
    out=[]
    for i in range(30):out.append(NormalizedMessage('1','group','100',T+i,seq=i+1,sender_uin=['11','12','777'][i%3],sender_name=['甲','乙','丙'][i%3],elements=[{'type':'text','text':f'群A 苹果 {i}' if i%2 else f'群A 香蕉 {i}'}]))
    for i in range(20):out.append(NormalizedMessage('1','group','200',T+100+i,seq=i+1,sender_uin=['13','777'][i%2],sender_name=['丁','丙'][i%2],elements=[{'type':'text','text':f'群B 苹果 {i}'}]))
    return out

@pytest.fixture
def arc(tmp_path):
    a=Archive(tmp_path/'archive.db');Importer(a).run(ImportRequest('test','v18'),two_groups())
    yield a
    a.close()

def test_sender_only_without_keyword_returns_messages(arc):
    rows=arc.search(SearchQuery(senders=('777',),limit=500))
    assert len(rows)==20 and {r['sender_uin'] for r in rows}=={'777'}

def test_uin_only_without_keyword_returns_messages(arc):
    rows=arc.search(SearchQuery(uin='13',limit=500))
    assert len(rows)==10 and {r['sender_uin'] for r in rows}=={'13'}

def test_sender_and_keyword_are_combined_with_and(arc):
    both=arc.search(SearchQuery(keyword='香蕉',senders=('777',),limit=500))
    assert both and all(r['sender_uin']=='777' and '香蕉' in r['text'] for r in both)
    assert len(both)==len([r for r in arc.search(SearchQuery(keyword='香蕉',limit=500)) if r['sender_uin']=='777'])
    assert arc.search(SearchQuery(keyword='香蕉',senders=('13',),limit=500))==[]

def test_same_sender_found_in_two_conversations(arc):
    peers={r['peer'] for r in arc.search(SearchQuery(senders=('777',),limit=500))}
    assert peers=={'100','200'}
    groups={g['peer']:g['matches'] for g in arc.search_conversations(SearchQuery(senders=('777',)))}
    assert groups=={'100':10,'200':10}

def test_unknown_sender_is_empty_without_error(arc):
    assert arc.search(SearchQuery(senders=('999999',)))==[]
    assert arc.search(SearchQuery(uin='999999'))==[]
    assert arc.search_conversations(SearchQuery(senders=('999999',)))==[]

def test_sender_grouping_and_page_details_both_keep_sender_condition(arc):
    extra=[NormalizedMessage('1','group','100',T+1000+i,seq=1000+i,sender_uin='777' if i%4==0 else '11',elements=[{'type':'text','text':f'补充 {i}'}]) for i in range(1000)]
    Importer(arc).run(ImportRequest('test','v18-more'),extra)
    groups=arc.search_conversations(SearchQuery(senders=('777',)))
    a=next(g for g in groups if g['peer']=='100');assert a['matches']==260
    pages=[arc.search(SearchQuery(senders=('777',),conversation_id=a['id'],offset=o,limit=100)) for o in (0,100,200)]
    assert [len(p) for p in pages]==[100,100,60]
    assert all(r['sender_uin']=='777' and r['conversation_id']==a['id'] for p in pages for r in p)
    assert len({r['id'] for p in pages for r in p})==260

def add_second_group(w):
    Importer(w.arc).run(ImportRequest('test','second'),[NormalizedMessage('1','group','50',T+i,seq=i+1,sender_uin='3' if i%2 else '2',sender_name='用户丙' if i%2 else '用户乙',elements=[{'type':'text','text':f'第二群 {i}'}]) for i in range(10)]);w.refresh()

def idle(app,d):spin(app,lambda:not d.debounce.isActive() and d.read.future is None and d.read.pending is None and d.people.future is None and d.people.pending is None)

def test_enter_in_search_fields_searches_and_keeps_window_open(ui):
    app,w,t=ui;d=SearchWindow(w);d.show();idle(app,d);closed=[];d.finished.connect(closed.append)
    d.sender_input.setFocus();d.sender_input.setText('2');QTest.keyClick(d.sender_input,Qt.Key_Return);QTest.qWait(250);idle(app,d)
    assert not closed and d.isVisible() and d.chosen_senders=={'2':'2'}
    assert d.rows and {r['sender_uin'] for r in d.rows}=={'2'}
    assert d.mode_label.isVisible() and '本会话' in d.mode_label.text()
    d.keyword.setFocus();d.keyword.setText('beta');QTest.keyClick(d.keyword,Qt.Key_Return);QTest.qWait(250);idle(app,d)
    assert not closed and d.isVisible() and all('beta' in r['text'] and r['sender_uin']=='2' for r in d.rows)
    assert not d.mode_label.isVisible()
    d.reject();QTest.qWait(230)

def test_ticking_and_removing_sender_search_immediately(ui):
    app,w,t=ui;d=SearchWindow(w);d.show();spin(app,lambda:bool(d.rows) and d.senders.count()==2)
    item=next(d.senders.item(i) for i in range(d.senders.count()) if d.senders.item(i).data(Qt.UserRole)[0]=='1')
    item.setCheckState(Qt.Checked);idle(app,d)
    assert d.rows and {r['sender_uin'] for r in d.rows}=={'1'}
    d.remove_sender(d.sender_selected.item(0));idle(app,d)
    assert {r['sender_uin'] for r in d.rows}=={'1','2'} and not d.mode_label.isVisible()
    d.reject();QTest.qWait(230)

def test_global_typed_qq_outside_roster_lists_all_conversations(ui):
    app,w,t=ui;add_second_group(w);d=w.global_search();idle(app,d)
    if d.filters_hidden:d.toggle_filters()
    d.sender_input.setText('2');d.select_typed_sender();idle(app,d)
    assert d.groups.count()==3 and {r['sender_uin'] for r in d.rows}=={'2'}
    assert sum(c['matches'] for c in d.group_rows)==170+1+5
    assert '全部发言，不限会话' in d.mode_label.text() and '3 个聊天' in d.mode_label.text()
    d.groups.setCurrentRow(2);idle(app,d)
    assert all(r['sender_uin']=='2' and r['conversation_id']==d.cid for r in d.rows)
    d.sender_input.setText('3');d.select_typed_sender();d.remove_sender(d.sender_selected.item(0));idle(app,d)
    assert d.groups.count()==1 and d.rows and {r['sender_uin'] for r in d.rows}=={'3'}
    d.reject();QTest.qWait(230)

def test_global_clear_filters_after_results_does_not_crash(ui):
    app,w,t=ui;add_second_group(w);d=w.global_search();idle(app,d)
    d.keyword.setText('第二群');d.search();idle(app,d);assert d.groups.count()==1 and d.rows
    d.clear_filters();idle(app,d)
    assert d.groups.count()==0 and not d.rows and d.isVisible()
    d.reject();QTest.qWait(230)

def open_global(app,w):
    d=w.global_search();idle(app,d)
    if d.filters_hidden:d.toggle_filters()
    return d

def press_enter(app,d,text):
    d.sender_input.setFocus();d.sender_input.setText(text);QTest.keyClick(d.sender_input,Qt.Key_Return);idle(app,d)

def chosen_item(d,uin):
    return next(d.sender_selected.item(i) for i in range(d.sender_selected.count()) if d.sender_selected.item(i).data(Qt.UserRole)==uin)

def add_profile(arc,owner,peer,name,avatar='',uid=''):
    with arc.con:arc.con.execute("INSERT INTO profiles(owner,kind,peer,uid,name,avatar_path,source) VALUES(?,'private',?,?,?,?,'user')",(owner,peer,uid,name,avatar))

def context_actions(view,item):
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication
    pos=view.visualItemRect(item).center();window=view.window()
    QApplication.sendEvent(view.viewport(),QContextMenuEvent(QContextMenuEvent.Mouse,pos,view.viewport().mapToGlobal(pos)));QTest.qWait(80)
    menu=window.context_menu;actions=[a.text() for a in menu.actions()];return menu,actions

def test_typed_qq_becomes_entry_and_field_clears_in_default_global_state(ui):
    app,w,t=ui;add_second_group(w);d=open_global(app,w)
    assert d.cid is None and d.groups.count()==0
    press_enter(app,d,'3')
    assert d.isVisible() and not d.sender_input.text() and d.sender_selected.count()==1 and chosen_item(d,'3').text()=='3  ×'
    assert d.rows and {r['sender_uin'] for r in d.rows}=={'3'}
    d.reject();QTest.qWait(230)

def test_two_typed_numbers_both_apply(ui):
    app,w,t=ui;add_second_group(w);d=open_global(app,w)
    press_enter(app,d,'3');press_enter(app,d,'1')
    assert set(d.chosen_senders)=={'3','1'} and d.sender_selected.count()==2 and not d.sender_input.text()
    assert sum(c['matches'] for c in d.group_rows)==5+170
    seen=set()
    for row in range(d.groups.count()):
        d.groups.setCurrentRow(row);idle(app,d);seen|={r['sender_uin'] for r in d.rows}
    assert seen=={'1','3'}
    d.reject();QTest.qWait(230)

def test_profile_hit_replaces_entry_in_place(ui,tmp_path):
    from PySide6.QtGui import QImage,QColor
    app,w,t=ui;add_second_group(w);picture=tmp_path/'p.png';image=QImage(16,16,QImage.Format_RGB32);image.fill(QColor('#336699'));image.save(str(picture))
    add_profile(w.arc,'1','3','资料丙',str(picture))
    d=open_global(app,w);press_enter(app,d,'3')
    item=chosen_item(d,'3');assert item.text()=='资料丙 · 3  ×' and not item.icon().isNull()
    assert d.sender_meta['3']['owner']=='1' and d.sender_meta['3']['avatar']==str(picture)
    assert '资料丙（3）' in d.mode_label.text()
    d.reject();QTest.qWait(230)

def test_profile_miss_keeps_number_without_error(ui):
    app,w,t=ui;d=open_global(app,w);press_enter(app,d,'424242')
    assert chosen_item(d,'424242').text()=='424242  ×' and not d.sender_meta['424242'].get('owner')
    assert not d.rows and '查询失败' not in d.state.text()
    d.reject();QTest.qWait(230)

def test_profile_lookup_follows_account_priority_and_uid_fallback(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        Importer(a).run(ImportRequest('t','acc'),[NormalizedMessage('A','group','1',T,seq=1,sender_uin='9',sender_uid='u_9',elements=[{'type':'text','text':'x'}]),
                                                  NormalizedMessage('B','group','2',T,seq=1,sender_uin='9',sender_uid='u_9',elements=[{'type':'text','text':'y'}])])
        add_profile(a,'B','9','B 的备注')
        assert a.sender_profile('9')['owner']=='B'
        add_profile(a,'A','u_9','A 中仅有 UID 的资料',uid='u_9')
        assert a.sender_profile('9')['name']=='A 中仅有 UID 的资料'
        assert a.sender_profile('9',['B'])['name']=='B 的备注'
        assert a.sender_profile('404')=={} and a.sender_profile('')=={}
    finally:a.close()

def test_roster_api_uid_field_keeps_existing_callers(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        Importer(a).run(ImportRequest('t','uid'),[NormalizedMessage('1','group','5',T+i,seq=i+1,sender_uin='7',sender_uid='u_7' if i else '',sender_name='七',elements=[{'type':'text','text':'z'}]) for i in range(3)])
        cid=a.find_conversation('1','group','5')['id'];rows=a.senders(cid)
        assert rows==[{'sender_uin':'7','nickname':'七','card':'','uid':'u_7'}]
        assert a.senders(cid,'七')==rows and a.senders(None,'7')==rows
    finally:a.close()

def test_typed_and_ticked_sender_results_show_same_user_info(ui,tmp_path):
    from qqarchive.search_delegate import PREVIEW
    from PySide6.QtGui import QImage,QColor
    app,w,t=ui;picture=tmp_path/'p.png';image=QImage(16,16,QImage.Format_RGB32);image.fill(QColor('#336699'));image.save(str(picture))
    add_profile(w.arc,'1','2','乙的资料',str(picture))
    d=SearchWindow(w);d.show();spin(app,lambda:d.senders.count()==2);idle(app,d)
    press_enter(app,d,'2');typed=[d.results.item(i).data(PREVIEW) for i in range(5)]
    d.remove_sender(chosen_item(d,'2'));idle(app,d)
    item=next(d.senders.item(i) for i in range(d.senders.count()) if d.senders.item(i).data(Qt.UserRole)[0]=='2');item.setCheckState(Qt.Checked);idle(app,d)
    ticked=[d.results.item(i).data(PREVIEW) for i in range(5)]
    strip=lambda rows:[(r['id'],r['name'],r['avatar']) for r in rows]
    assert strip(typed)==strip(ticked) and all(r['avatar']==str(picture) and r['name']=='用户乙' for r in typed)
    d.reject();QTest.qWait(230)

def test_roster_rows_have_avatar_and_menu_in_existing_order(ui):
    from qqarchive.session_delegate import SENDER
    app,w,t=ui;d=SearchWindow(w);d.show();spin(app,lambda:d.senders.count()==2);idle(app,d)
    data=[d.senders.item(i).data(SENDER) for i in range(2)]
    assert [x['uin'] for x in data]==['1','2'] and [x['name'] for x in data]==['用户甲','用户乙']
    assert not d.senders.itemDelegate().problems(d.senders)
    menu,actions=context_actions(d.senders,d.senders.item(1));menu.hide()
    assert actions==['复制 QQ 号','查看详细信息']
    d.reject();QTest.qWait(230)

def test_manual_entry_offers_copy_only_until_profile_found(ui):
    app,w,t=ui;d=open_global(app,w);press_enter(app,d,'424242')
    menu,actions=context_actions(d.sender_selected,chosen_item(d,'424242'));menu.hide()
    assert actions==['复制 QQ 号']
    add_profile(w.arc,'1','424242','后来找到的资料');d.search();idle(app,d)
    assert chosen_item(d,'424242').text()=='后来找到的资料 · 424242  ×'
    menu,actions=context_actions(d.sender_selected,chosen_item(d,'424242'));menu.hide()
    assert actions==['复制 QQ 号','查看详细信息']
    d.reject();QTest.qWait(230)

def test_right_click_does_not_remove_chosen_entry_and_ticked_entry_keeps_conversation(ui):
    app,w,t=ui;d=SearchWindow(w);d.show();spin(app,lambda:d.senders.count()==2);idle(app,d)
    d.senders.item(0).setCheckState(Qt.Checked);idle(app,d)
    menu,actions=context_actions(d.sender_selected,chosen_item(d,'1'));menu.hide()
    assert actions==['复制 QQ 号','查看详细信息'] and d.sender_meta['1']['conversation_id']==w.current['id'] and '1' in d.chosen_senders
    d.reject();QTest.qWait(230)

def capture_person_dialog(w,monkeypatch):
    from qqarchive.chrome import FramedDialog
    from PySide6.QtWidgets import QLabel,QPushButton
    shown=[]
    def job(title,task,done,**kw):done(task(w.arc))
    def execute(dialog):
        shown.append(dict(labels=[label.text() for label in dialog.findChildren(QLabel)],buttons=[b.toolTip() for b in dialog.findChildren(QPushButton)]));return 0
    monkeypatch.setattr(w,'run_read_job',job);monkeypatch.setattr(FramedDialog,'exec',execute);return shown

def test_details_without_conversation_hide_last_message(ui,monkeypatch):
    app,w,t=ui;add_second_group(w);add_profile(w.arc,'1','3','资料丙')
    d=open_global(app,w);press_enter(app,d,'3');shown=capture_person_dialog(w,monkeypatch)
    menu,actions=context_actions(d.sender_selected,chosen_item(d,'3'));menu.actions()[1].trigger();menu.hide()
    labels=' | '.join(shown[0]['labels'])
    assert '资料丙' in labels and 'QQ 号：3' in labels and '不显示最后发言' in labels and '在当前聊天最后发言' not in labels
    assert '拥有过的共同群聊 · 1' in labels
    d.reject();QTest.qWait(230)

def test_chat_avatar_menu_and_card_unchanged(ui,monkeypatch):
    from qqarchive.chat_tools import avatar_menu
    app,w,t=ui;shown=capture_person_dialog(w,monkeypatch)
    message=next(m for m in reversed(w.model.rows) if m['sender_uin']=='2')
    menu=avatar_menu(w,message);assert [a.text() for a in menu.actions()]==['复制 QQ 号','查看详细信息']
    menu.actions()[1].trigger();menu.hide()
    labels=' | '.join(shown[0]['labels'])
    assert '在当前聊天最后发言' in labels and '不显示最后发言' not in labels and '定位到该用户在当前聊天中的最后一次发言' in shown[0]['buttons']

@pytest.mark.parametrize('scale',['1','1.5','2'])
def test_sender_roster_offscreen_acceptance_at_three_scales(tmp_path,scale):
    """Real check/remove/context-menu events and row layout at 100/150/200 %."""
    import json,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    # Qt rejects non-ASCII absolute configfile paths, so pass it relative to cwd.
    (tmp_path/'screen.json').write_text(json.dumps({'screens':[{'name':'QA','x':0,'y':0,'width':2560,'height':1440,'logicalDpi':96,'logicalBaseDpi':96,'dpr':1}]}),encoding='ascii')
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen:configfile=screen.json',QT_SCALE_FACTOR=scale,QQARCHIVE_CONFIG_DIR=str(tmp_path/'config'))
    subprocess.run([sys.executable,str(root/'main.py'),'--sender-preview',str(tmp_path/'out')],cwd=tmp_path,env=env,check=True,timeout=120)
    report=json.loads((tmp_path/'out'/'result.json').read_text(encoding='utf-8'))
    assert report['passed'] and report['scale']==float(scale) and report['layout_problems']==[]

def test_conversation_window_opens_cross_conversation_history(ui):
    app,w,t=ui;add_second_group(w);d=SearchWindow(w);d.show();idle(app,d)
    assert not d.everywhere.isVisible()
    d.sender_input.setText('3');d.select_typed_sender();idle(app,d)
    assert not d.rows and '在全部聊天中查看此人发言' in d.state.text() and d.everywhere.isVisible()
    QTest.mouseClick(d.everywhere,Qt.LeftButton);g=w.search_windows[('global',None)];idle(app,g)
    assert g is not d and g.global_mode and g.chosen_senders=={'3':'3'} and not g.keyword.text()
    assert g.rows and {r['sender_uin'] for r in g.rows}=={'3'} and g.groups.count()==1
    d.reject();g.reject();QTest.qWait(230)
