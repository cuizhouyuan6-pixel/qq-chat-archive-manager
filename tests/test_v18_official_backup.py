import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from qqarchive.discovery import discover
from qqarchive.sources import open_ntqq,KeyRequired,UnsupportedSource
from qqarchive.config import Config
from qqarchive import auto

def container(folder):
    folder.mkdir(parents=True,exist_ok=True);f=folder/('A'*32)
    f.write_bytes(b'SQLite header 3\0'+(1024).to_bytes(2,'big')+bytes(1006)+bytes(8192));return f

@pytest.fixture
def window(tmp_path,monkeypatch):
    from qqarchive.desktop import MainWindow,STYLE
    import qqarchive.chrome as chrome
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    monkeypatch.setenv('QQARCHIVE_CONFIG_DIR',str(tmp_path/'config'))
    monkeypatch.setattr(chrome,'dialog_available_geometry',lambda d:QRect(0,0,1440,1000))
    w=MainWindow(Config(data_dir=str(tmp_path/'data')))
    yield w
    w.closing=True;w.phone_timer.stop();w.key_timer.stop();w.stop_summary();w.tray.hide();w.arc.close();w.close();w.deleteLater();app.processEvents()

def test_unsupported_is_distinct_from_missing_key_but_compatible(tmp_path):
    container(tmp_path/'b');c=discover([],[tmp_path/'b'])[0]
    with pytest.raises(UnsupportedSource,match='不能直接导入') as caught:open_ntqq(c,None)
    assert isinstance(caught.value,KeyRequired)

def test_import_page_says_unsupported_for_every_entry(window,tmp_path,monkeypatch):
    import qqarchive.desktop as desktop
    f=container(tmp_path/'backup');before=f.read_bytes();popups=[]
    for name in ('warning','information','question','critical'):
        monkeypatch.setattr(desktop.QMessageBox,name,staticmethod(lambda parent,title,text,*a,_n=name,**k:(popups.append((_n,title,text)),0)[1]))
    w=window;w.show_sources(discover([],[tmp_path/'backup']));w.owner.setText('12345');w.source_list.setCurrentRow(0);QTest.qWait(30)
    text=w.source_list.item(0).text()
    assert '暂不支持导入' in text and '需密钥' not in text and '未导入' not in text
    assert not w.key_step.available and not w.scope_step.available and w.source_step.note.text().startswith('暂不支持导入：')
    for action in ('continue_flow','review_import','source_conversations','recover_import','reuse_key','auto_key','manual_key','start_import'):
        popups.clear();getattr(w,action)();QTest.qWait(20)
        assert len(popups)==1 and popups[0][:2]==('information','该来源暂不支持导入'),(action,popups)
        assert '不能直接导入' in popups[0][2] and '迁移聊天记录到电脑' in popups[0][2] and w.worker is None
    assert f.read_bytes()==before

def test_auto_check_and_run_all_never_call_it_a_key_problem(tmp_path,monkeypatch):
    from qqarchive.store import Archive
    container(tmp_path/'backup');cfg=Config(data_dir=str(tmp_path/'data'));cfg.backup_dirs=[str(tmp_path/'backup')];cfg.qq_roots=[str(tmp_path/'none')]
    arc=Archive(cfg.data_path/'archive.db')
    try:
        class Keys:
            def has(self,owner):return 'session'
            def candidates(self,*a):return []
        result=auto.check(cfg,arc,Keys())
        assert result.need_key==[] and [c.kind for c in result.unsupported]==['official-backup']
        assert '暂不支持导入' in result.message() and '密钥' not in result.message()
        called=[];monkeypatch.setattr(auto,'acquire_keys',lambda *a,**k:called.append(a))
        text=auto.run_all(cfg,arc,Keys(),result).text()
        assert not called and '暂不支持导入' in text and '密钥无法打开' not in text and '导入失败' not in text
    finally:arc.close()
