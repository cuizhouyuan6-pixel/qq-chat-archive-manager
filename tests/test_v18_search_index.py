import os,threading,time
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
from qqarchive.store import Archive,SearchQuery,SEARCH_INDEX
from qqarchive.importer import Importer,ImportRequest
from qqarchive.models import NormalizedMessage
from qqarchive.config import Config

T=1_760_000_000

def fill(a,n=300):
    Importer(a).run(ImportRequest('t','index'),[NormalizedMessage('1','group','9',T+i,seq=i+1,sender_uin='42' if i%100==0 else str(1000+i%7),elements=[{'type':'text','text':f'第 {i} 条'}]) for i in range(n)])

def old_archive(path):
    """An archive as v17 left it: same schema version, no sender index."""
    a=Archive(path);fill(a,3000)
    with a.con:a.con.execute('DROP INDEX '+SEARCH_INDEX)
    a.close();return path

def test_new_archive_has_index_and_rare_sender_page_uses_it(tmp_path):
    a=Archive(tmp_path/'a.db')
    try:
        fill(a);assert a.has_search_index()
        cid=a.find_conversation('1','group','9')['id'];sqls=[];a.con.set_trace_callback(sqls.append)
        rows=a.search(SearchQuery(senders=('42',),conversation_id=cid,limit=100));a.con.set_trace_callback(None)
        assert [r['sender_uin'] for r in rows]==['42']*3
        plan=' / '.join(str(r[3]) for r in a.con.execute('EXPLAIN QUERY PLAN '+next(s for s in sqls if s.startswith('SELECT'))))
        assert SEARCH_INDEX in plan
    finally:a.close()

def test_existing_archive_is_not_indexed_at_open_but_by_explicit_build(tmp_path):
    path=old_archive(tmp_path/'a.db');a=Archive(path)
    try:
        assert not a.has_search_index()
        cancel=threading.Event();cancel.set()
        with pytest.raises(InterruptedError):a.build_search_index(cancel)
        assert not a.has_search_index() and a.count_messages()==3000
        assert a.build_search_index(threading.Event()) and a.has_search_index()
        assert a.build_search_index() is False
        assert a.con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    finally:a.close()

def test_main_window_builds_missing_index_in_background_task(tmp_path,monkeypatch):
    from qqarchive.desktop import MainWindow,STYLE
    import qqarchive.chrome as chrome
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    monkeypatch.setattr(chrome,'dialog_available_geometry',lambda d:QRect(0,0,1440,1000))
    monkeypatch.setattr(MainWindow,'SEARCH_INDEX_DELAY_MS',10)
    data=tmp_path/'data';data.mkdir();old_archive(data/'archive.db')
    w=MainWindow(Config(data_dir=str(data)))
    try:
        assert not w.arc.has_search_index()
        seen=[];until=time.monotonic()+15
        while time.monotonic()<until and not w.arc.has_search_index():
            app.processEvents()
            if w.worker is not None:seen.append((w.job_dialog.windowTitle(),w.job_dialog.isVisible(),not w.task_strip.isHidden(),w.job_dialog.isModal()))
            time.sleep(.01)
        until=time.monotonic()+5
        while w.worker is not None and time.monotonic()<until:app.processEvents();time.sleep(.01)
        assert w.arc.has_search_index() and w.worker is None
        assert seen and all(s==('优化搜索索引',False,True,False) for s in seen)
        assert w.pages.widget(1).isEnabled() and '搜索索引已就绪' in w.statusBar().currentMessage()
    finally:
        w.closing=True;w.phone_timer.stop();w.key_timer.stop();w.stop_summary();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()
