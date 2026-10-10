"""Synchronous result adapters without application/window modality."""
from PySide6.QtCore import Qt,QEventLoop
from PySide6.QtWidgets import QApplication,QFileDialog as QtFileDialog
from shiboken6 import isValid

def wait_result(dialog):
    # QDialog.exec() silently changes modality even after setModal(False).
    # Keep legacy result-returning callers while allowing other windows to work.
    if getattr(dialog,'_result_loop',None):return dialog.result()
    loop=QEventLoop();dialog._result_loop=loop;app=QApplication.instance()
    dialog.setWindowModality(Qt.NonModal);dialog.setModal(False)
    dialog.finished.connect(loop.quit);dialog.destroyed.connect(loop.quit);app.aboutToQuit.connect(loop.quit)
    try:
        dialog.show();dialog.raise_();loop.exec()
        return dialog.result() if isValid(dialog) else 0
    finally:
        app.aboutToQuit.disconnect(loop.quit)
        if isValid(dialog):
            dialog.finished.disconnect(loop.quit);dialog.destroyed.disconnect(loop.quit);dialog._result_loop=None

class FileDialog(QtFileDialog):
    AcceptSave=QtFileDialog.AcceptMode.AcceptSave
    ExistingFile=QtFileDialog.FileMode.ExistingFile
    Directory=QtFileDialog.FileMode.Directory
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.setOption(QtFileDialog.Option.DontUseNativeDialog,True);self.setWindowModality(Qt.NonModal)
    def exec(self):return wait_result(self)
    @classmethod
    def getOpenFileName(cls,parent=None,caption='',directory='',filter='',**kwargs):
        d=cls(parent,caption,directory,filter);d.setFileMode(cls.ExistingFile)
        ok=d.exec();result=(d.selectedFiles()[0],d.selectedNameFilter()) if ok and d.selectedFiles() else ('','')
        d.deleteLater();return result
    @classmethod
    def getExistingDirectory(cls,parent=None,caption='',directory='',**kwargs):
        d=cls(parent,caption,directory);d.setFileMode(cls.Directory);d.setOption(QtFileDialog.Option.ShowDirsOnly,True)
        ok=d.exec();result=d.selectedFiles()[0] if ok and d.selectedFiles() else '';d.deleteLater();return result

