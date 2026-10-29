"""Frozen/offscreen native workflow and genuine loopback HTTP contract check."""
import json
import os
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PySide6.QtCore import Qt, QDateTime
from PySide6.QtWidgets import QApplication


def run(root):
    root = Path(root).resolve(); root.mkdir(parents=True, exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR'] = str(root / 'config')
    os.environ['QQARCHIVE_DISABLE_TRANSFER_DETECTION'] = '1'
    from .desktop import MainWindow
    from .config import Config
    from .history_window import open_history
    app = QApplication.instance()
    owner, peer = 100001, 900001
    rows = [dict(self_id=owner,group_id=peer,message_type='group',message_id=n,real_seq=str(n),
            time=1760000000+n,sender={'user_id':200001,'nickname':'测试发送人'},
            message=[{'type':'text','data':{'text':f'用于补全验证的合成消息 {n}'}}]) for n in (5,4,3,2,1)]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            data={'user_id':owner} if self.path=='/get_login_info' else {'messages':rows}
            body=json.dumps({'status':'ok','retcode':0,'data':data}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    host=MainWindow(Config(data_dir=str(root/'data'),transfer_detection=False))
    report={'passed':False}
    def wait(condition, seconds=15):
        deadline=time.monotonic()+seconds
        while not condition() and time.monotonic()<deadline:
            app.processEvents();time.sleep(.01)
        assert condition(), 'workflow timed out'
    def settle():
        deadline=time.monotonic()+.35
        while time.monotonic()<deadline:app.processEvents();time.sleep(.01)
    try:
        window=open_history(host)
        window.owner.setText(str(owner));window.endpoint.setText(f'http://127.0.0.1:{server.server_port}')
        settle();window.grab().save(str(root/'history-connect.png'))
        assert not window.begin.isEnabled()
        window.connect_qq();wait(lambda:not window.busy)
        assert window.begin.isEnabled()
        window.peer.setText(str(peer));window.start.setDateTime(QDateTime.fromSecsSinceEpoch(1760000002))
        window.end.setDateTime(QDateTime.fromSecsSinceEpoch(1760000005))
        settle();window.grab().save(str(root/'history-scope.png'))
        assert window.begin.isVisible() and window.begin.visibleRegion().boundingRect().height()>20
        window.start_job();wait(lambda:host.worker is None and '已读取' in window.note.text())
        assert host.arc.find_conversation(str(owner),'group',str(peer))['msg_count']==4
        assert window.past.count()==1 and window.windowModality()==Qt.NonModal
        settle();window.grab().save(str(root/'history-result.png'))
        window.start_job(True);wait(lambda:host.worker is None and '重复 4' in window.note.text())
        assert host.arc.find_conversation(str(owner),'group',str(peer))['msg_count']==4
        report.update(passed=True,http=True,account_guard=True,imported=4,reimport_duplicates=4,
                      modeless=True,task_strip=True,dpr=window.devicePixelRatioF(),
                      width=window.width(),height=window.height())
    except Exception as exc:
        report['error']=str(exc)
    finally:
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        host.request_exit();server.shutdown();server.server_close();thread.join()
    return 0 if report['passed'] else 1
