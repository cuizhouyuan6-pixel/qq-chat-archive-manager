from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

def self_test(root):
    from .store import Archive,SearchQuery
    from .models import NormalizedMessage
    from .importer import Importer,ImportRequest
    from .exporters import export_archive
    from .package import import_package
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    a=Archive(root/'source'/'archive.db');b=Archive(root/'roundtrip'/'archive.db')
    try:
        msgs=[NormalizedMessage('10000','group','20000',1700000000+i,sender_uin='30000',sender_name='测试用户',seq=i+1 if i%2 else None,source_msg_id=str(i),elements=[{'type':'text','text':f'测试消息 {i} <script>alert(1)</script>'}]) for i in range(12)]
        first=Importer(a).run(ImportRequest('smoke','synthetic'),msgs)
        second=Importer(a).run(ImportRequest('smoke','synthetic-again'),msgs)
        assert first.inserted==12 and second.duplicates==12
        assert len(a.search(SearchQuery(keyword='测试')))==12
        for fmt in ('package','html','json','csv','txt'):export_archive(a,root/fmt,fmt)
        imported=import_package(b,root/'package');assert imported.inserted==12
        same=import_package(a,root/'package');assert same.inserted==0 and same.duplicates==12
        assert a.integrity_check()==['ok'] and b.integrity_check()==['ok']
        check=Archive(root/'identity-check'/'archive.db')
        try:
            collisions=[NormalizedMessage('10000','group','20000',1700000000,sender_uin='30000',seq=8,msg_random=i+1,elements=[{'type':'text','text':'同一序号下的不同消息'}]) for i in range(3)]
            assert Importer(check).run(ImportRequest('smoke','identities'),collisions).inserted==3
            assert Importer(check).run(ImportRequest('smoke','identities-again'),list(reversed(collisions))).duplicates==3
        finally:check.close()
        report={'passed':True,'messages':12,'duplicate_merge':True,'search':True,'formats':5,'package_roundtrip':True,'integrity':'ok','distinct_sequence_identities':True}
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return 0
    finally:a.close();b.close()

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--self-test');ap.add_argument('--phone-smoke');ap.add_argument('--key-smoke');ap.add_argument('--preview');ap.add_argument('--dialog-preview');ap.add_argument('--interaction-preview');ap.add_argument('--sender-preview');ap.add_argument('--forward-preview');ap.add_argument('--history-preview');ap.add_argument('--transfer-preview');ap.add_argument('--transfer-performance');ap.add_argument('--transfer-performance-mode',choices=('live','stalled'),default='live');ap.add_argument('--smoke',action='store_true');args=ap.parse_args(argv)
    if args.self_test:return self_test(args.self_test)
    if args.key_smoke:
        from .discovery import default_qq_roots,scan_qq_root
        from .keyscan import targets_from_candidates,extract_keys,hits_from_result
        from .ntqq import NtqqSource
        from .sqlcipher import Key
        root=Path(args.key_smoke);root.mkdir(parents=True,exist_ok=True)
        sources=scan_qq_root(default_qq_roots()[0])
        res=extract_keys(targets_from_candidates(sources),max_pids=3)
        hits=hits_from_result(targets_from_candidates(sources),res)
        opened=0
        for hit in hits:
            with NtqqSource(hit.db_path,hit.owner,Key(bytes.fromhex(hit.key_hex),raw=True),hit.params) as src:
                if any(src._columns.values()):opened+=1
        report={'sources':len(sources),'authenticated':len(hits),'schemas_opened':opened,
                'processes':len(res.pids_scanned),'seconds':round(res.seconds,2),
                'key_persisted':False,'source_copied':False,'passed':opened>0}
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return 0 if report['passed'] else 1
    if args.phone_smoke:
        from .adb import Adb,find_adb,list_files
        from .media_pull import pull_file
        root=Path(args.phone_smoke);root.mkdir(parents=True,exist_ok=True)
        path=find_adb()
        if not path:raise ValueError('未找到随程序提供的 ADB')
        adb=Adb(path);devices=adb.authorized_devices()
        if len(devices)!=1:raise ValueError('测试需要连接一台已授权手机')
        adb.serial=devices[0].serial
        files=list_files(adb,'/sdcard/Pictures/QQ')
        sample=min((f for f in files if 0<f.size<1024*1024),key=lambda f:f.size)
        result=pull_file(adb,sample,root/'sample.bin')
        report={'verified':result['verified'],'algorithm':result['algo'],'bytes':result['bytes'],'bundled_adb':True}
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return 0 if result['verified'] else 1
    from PySide6.QtWidgets import QApplication
    from .dialogs import MessageBox as QMessageBox
    from PySide6.QtCore import QTimer,QLockFile
    from PySide6.QtNetwork import QLocalServer,QLocalSocket
    from .config import config_dir,config_file,load_config,save_config
    from .desktop import MainWindow,STYLE
    app=QApplication(sys.argv);app.setApplicationName('QQ聊天记录管理器');app.setOrganizationName('QQArchive');app.setStyle('Fusion')
    from PySide6.QtGui import QFontDatabase,QFont
    font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/'msyh.ttc'
    if font.exists():QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont('Microsoft YaHei',10))
    from .chrome import app_icon
    app.setWindowIcon(app_icon());app.setStyleSheet(STYLE)
    if args.dialog_preview:
        from .dialog_checks import run
        return run(args.dialog_preview)
    if args.interaction_preview:
        from .interaction_checks import run
        return run(args.interaction_preview)
    if getattr(args,'history_preview',None):
        from .history_checks import run
        return run(args.history_preview)
    if args.forward_preview:
        from .forward_checks import run
        return run(args.forward_preview)
    if args.sender_preview:
        from .interaction_checks import sender_run
        return sender_run(args.sender_preview)
    if args.transfer_performance:
        from .transfer_performance import run
        return run(args.transfer_performance,args.transfer_performance_mode,40 if args.transfer_performance_mode=='live' else 12)
    if args.transfer_preview:
        from .transfer_checks import run
        return run(args.transfer_preview)
    if args.preview:
        # A visual acceptance run must never seed synthetic chats into real user data.
        os.environ['QQARCHIVE_CONFIG_DIR']=str(Path(args.preview).resolve()/'config')
    cfg=load_config()
    if not config_file().exists():
        base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent.parent
        seed=base/'data-directory.txt'
        if seed.exists() and not args.preview:
            selected=seed.read_text(encoding='utf-8-sig').strip()
            if Path(selected).is_absolute():cfg.data_dir=selected
        save_config(cfg)
    directory=config_dir();directory.mkdir(parents=True,exist_ok=True)
    name='QQArchive-'+hashlib.sha256(str(directory.resolve()).lower().encode()).hexdigest()[:24]
    lock=QLockFile(str(directory/'instance.lock'));lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        choice=QMessageBox.question(None,'QQ聊天记录管理器已在运行','是否关闭旧窗口，使用这次启动的版本？选择“否”返回原窗口。',QMessageBox.Yes|QMessageBox.No|QMessageBox.Cancel)
        if choice==QMessageBox.Cancel:return 0
        socket=QLocalSocket();socket.connectToServer(name)
        if not socket.waitForConnected(2000):
            QMessageBox.warning(None,'无法切换','旧程序尚未响应，请在旧窗口完成或取消任务后关闭，再启动。');return 1
        socket.write(b'exit' if choice==QMessageBox.Yes else b'show');socket.flush();socket.waitForBytesWritten(1000);socket.disconnectFromServer()
        if choice==QMessageBox.No:return 0
        until=time.monotonic()+30
        while not lock.tryLock(100):
            app.processEvents()
            if time.monotonic()>until:QMessageBox.information(None,'旧任务正在结束','旧程序仍在安全结束任务，请稍后启动。');return 1
    server=QLocalServer();server.setSocketOptions(QLocalServer.UserAccessOption);QLocalServer.removeServer(name)
    if not server.listen(name):lock.unlock();return 1
    win=MainWindow(cfg)
    def incoming():
        socket=server.nextPendingConnection()
        def command():
            text=bytes(socket.readAll())
            if text==b'exit':win.request_exit()
            else:win.restore()
            socket.disconnectFromServer();socket.deleteLater()
        socket.readyRead.connect(command)
        if socket.bytesAvailable():command()
    server.newConnection.connect(incoming);win.show()
    if args.preview:
        root=Path(args.preview);root.mkdir(parents=True,exist_ok=True)
        from .models import NormalizedMessage
        from .importer import Importer,ImportRequest
        preview_messages=[('陈默','早上好，周末的行程确认了吗？',False),('我','确认了。周六 9 点在地铁站碰面。',True),('林晓','我整理的路线图发在群里了，大家可以提前看看。',False),('陈默','收到，我们到时候见。',False),('我','好的，有变动就在这里同步。',True)]
        msgs=[NormalizedMessage('100001','group','900001',1760000000+i*180,sender_uin='100001' if me else str(200001+i),sender_name=n,is_self=me,seq=i+1,elements=[{'type':'text','text':t}]) for i,(n,t,me) in enumerate(preview_messages)]
        msgs += [NormalizedMessage('100001','private','200001',1760000800,sender_uin='200001',sender_name='陈默',peer_name='陈默',seq=1,elements=[{'type':'text','text':'下周见！'}])]
        Importer(win.arc).run(ImportRequest('preview','synthetic'),msgs)
        conv=win.arc.find_conversation('100001','group','900001');win.arc.rename_conversation(conv['id'],'周末出行讨论组');win.refresh()
        win.select_conversation(win.tree.topLevelItem(0).child(0).child(0));win.details.show()
        titles=('chat','import','phone','export','library','settings','monitor')
        def capture(index=0):
            if index<len(titles):
                win.navigate(index)
                def save_page():
                    win.grab().save(str(root/(titles[index]+'.png')));capture(index+1)
                QTimer.singleShot(250,save_page)
            else:
                one=win.detach_current();two=win.chat_windows.open(win.arc.find_conversation('100001','private','200001')['id'])
                win.chat_windows.merge_all(one)
                def save_group():
                    one.grab().save(str(root/'grouped.png'))
                    report={'version':__import__('qqarchive.version',fromlist=['VERSION']).VERSION,'pages':7,'grouped_tabs':one.tabs.count(),'frameless':bool(one.windowFlags() & __import__('PySide6.QtCore',fromlist=['Qt']).Qt.FramelessWindowHint),'icon_sizes':len(win.windowIcon().availableSizes()),'passed':one.tabs.count()==2}
                    (root/'ui-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8');win.request_exit()
                QTimer.singleShot(250,save_group)
        QTimer.singleShot(700,capture)
    elif args.smoke:QTimer.singleShot(1200,win.request_exit)
    result=app.exec();server.close();lock.unlock();return result
