"""Opt-in synthetic UI acceptance for the frozen Windows build; no QQ access."""
import json,os
from pathlib import Path
from PySide6.QtCore import QEventLoop,QTimer,QPoint,QRect
from PySide6.QtWidgets import QApplication
from .dialogs import MessageBox
from .chrome import dialog_available_geometry

def wait(ms):
    loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()

def run(destination):
    root=Path(destination).resolve();root.mkdir(parents=True,exist_ok=True)
    os.environ['QQARCHIVE_CONFIG_DIR']=str(root/'config')
    from .desktop import MainWindow,JobDialog,Worker
    from .config import Config
    from .auto import key_warning
    app=QApplication.instance();app.setQuitOnLastWindowClosed(False)
    host=MainWindow(Config(data_dir=str(root/'synthetic-data')));host.resize(1100,760);host.show();host.navigate(1);wait(250)
    cases=[('warning','获取密钥未完成','自动获取密钥没有完成。\n没有发现正在运行的电脑 QQ，请先登录 QQ 后重试。\n也可手动填写密钥。',MessageBox.Warning,MessageBox.Ok),
           ('question','自动获取密钥',key_warning(),MessageBox.Question,MessageBox.Yes|MessageBox.Cancel),
           ('next-step','密钥已获取 · 下一步','是否关闭电脑 QQ，并自动导入当前范围的聊天记录？\n\n范围：0 个已选会话\n时间：全部时间\n附件：引用原路径，不复制文件\n\n选择“是”会结束电脑 QQ 的进程，请先保存未发送内容。\n选择“否”会使用 Windows 账户加密保存密钥，稍后可在下方选择会话和时间。',MessageBox.Question,MessageBox.Yes|MessageBox.No),
           ('long-error','读取失败','\n'.join(f'第 {i} 行：D:\\聊天记录\\'+('目录_'*15)+'\\nt_msg.db' for i in range(100)),MessageBox.Warning,MessageBox.Ok),
           ('exit','退出 QQ聊天记录管理器','关闭程序会结束后台任务。也可以隐藏至任务栏，稍后继续。',MessageBox.Question,None)]
    report={'scale':host.devicePixelRatioF(),'cases':[],'passed':False}
    try:
        for name,title,text,icon,buttons in cases:
            d=MessageBox(host);d.setWindowTitle(title);d.setText(text);d.setIcon(icon)
            if buttons:d.setStandardButtons(buttons)
            else:
                for label,role in [('关闭程序',MessageBox.AcceptRole),('隐藏至任务栏',MessageBox.ActionRole),('取消',MessageBox.RejectRole)]:d.addButton(label,role)
            d.show();wait(240)
            assert not d.isModal() and not getattr(d,'_shade',None)
            assert d.content.toPlainText()==text
            assert dialog_available_geometry(d).contains(d.frameGeometry())
            assert d.content.horizontalScrollBar().maximum()==0
            for b in d.buttons():assert d.rect().contains(QRect(b.mapTo(d,QPoint()),b.size()))
            if name!='long-error':assert d.content.verticalScrollBar().maximum()==0
            else:assert d.content.verticalScrollBar().maximum()>0
            expected=host.frameGeometry().center();actual=d.geometry().center()
            # Screen edges may constrain centering on a partly offscreen host.
            bounds=dialog_available_geometry(d).adjusted(16,16,-16,-16)
            x=max(bounds.left(),min(expected.x()-d.width()//2,bounds.right()-d.width()+1))
            y=max(bounds.top(),min(expected.y()-d.height()//2,bounds.bottom()-d.height()+1))
            assert (d.pos()-QPoint(x,y)).manhattanLength()<=2
            assert d.grab().save(str(root/(name+'.png')))
            report['cases'].append({'name':name,'width':d.width(),'height':d.height(),'full_text':True,'buttons_visible':True,'centered':True,'scrolling':bool(d.content.verticalScrollBar().maximum())})
            d.reject();d.deleteLater();wait(200)
        worker=Worker(lambda c,s:None);job=JobDialog('验证数据库',worker,host);job.show();wait(240)
        assert dialog_available_geometry(job).contains(job.frameGeometry())
        job.close();assert not worker.cancel.is_set() and not job.isVisible()
        job.show();wait(240);job.grab().save(str(root/'progress.png'));job.cancel();assert worker.cancel.is_set()
        job.timer.stop();job.accept();job.deleteLater()
        report['job_hide_preserves_task']=True;report['explicit_cancel']=True;report['ordinary_dialogs_modeless']=True;report['passed']=True
    finally:
        (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        host.key_timer.stop();host.phone_timer.stop();host.request_exit();wait(200)
    return 0
