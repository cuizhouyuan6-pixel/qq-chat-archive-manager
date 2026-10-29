"""Progressive desktop workflows; prerequisites remain enforced in handlers."""
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QListWidget,QListWidgetItem,QLineEdit,QComboBox,QCheckBox,QSpinBox,
    QTextEdit,QFileDialog)
from .dialogs import MessageBox as QMessageBox,InputDialog as QInputDialog
from .flow_widgets import Step,SelectionBar
from .keystore import check_key
from .key_lifecycle import validate,inspect_saved,close_qq
from .auto import acquire_keys,key_warning
from .config import save_config
from .sources import analyze

class Workflows:
    def build_import(self):
        from .desktop import label,row,button,Dates
        self.verified_sources={};self.flow_running=False
        layout=self.page('导入聊天记录','按顺序完成来源、密钥和导入范围；已完成步骤可随时展开修改。')
        from .history_window import open_history
        layout.addWidget(button('补全 QQ 中可查看、但本地缺失的历史',lambda:open_history(self)))
        self.flow_mode=QComboBox();self.flow_mode.addItem('引导模式 · 每步查看后继续','guided');self.flow_mode.addItem('全自动模式 · 确认范围后连续执行','auto')
        self.flow_mode.setCurrentIndex(1 if self.cfg.import_mode=='auto' else 0);self.flow_mode.currentIndexChanged.connect(self.mode_changed)
        self.flow_hint=label('引导模式会逐步展开所需选项。','muted')
        layout.addWidget(row(self.flow_mode,button('开始 / 继续流程',self.continue_flow,True)));layout.addWidget(self.flow_hint)
        self.source_step=Step(1,'选择来源','扫描电脑 QQ，或添加已有数据库 / 可迁移归档。只读取原文件。')
        box=self.source_step.box;self.source_list=QListWidget();self.source_list.setMinimumHeight(110);self.source_list.setMaximumHeight(180);self.source_list.hide();box.addWidget(self.source_list)
        box.addWidget(row(button('扫描电脑 QQ',self.scan_sources,True),button('添加数据库 / 归档',self.add_source),button('添加备份目录',self.add_source_dir)))
        box.addWidget(label('归档账号 QQ 号','fieldLabel'));self.owner=QLineEdit();self.owner.setPlaceholderText('自动识别，也可填写本人 QQ 号');box.addWidget(self.owner)
        self.source_step.expand(True,False);layout.addWidget(self.source_step)
        self.key_step=Step(2,'验证密钥','自动取钥需要电脑 QQ 已登录；也可以填写已取得的数据库密钥。')
        self.key_state=label('等待选择来源','muted');self.key_step.box.addWidget(self.key_state)
        self.key_step.box.addWidget(row(button('自动获取密钥',self.auto_key,True),button('手动填写密钥',self.manual_key),button('验证已有密钥',self.reuse_key)))
        self.key_step.unlock(False);layout.addWidget(self.key_step)
        self.scope_step=Step(3,'选择范围并导入','未勾选会话表示全部会话；重叠记录合并，冲突保留。附件引用原位置。')
        self.import_dates=Dates();self.scope_step.box.addWidget(self.import_dates)
        self.scope_step.box.addWidget(button('读取会话列表',self.source_conversations))
        self.import_convs=QListWidget();self.import_convs.setMinimumHeight(160);self.import_convs.setMaximumHeight(260);self.scope_step.box.addWidget(self.import_convs)
        self.import_selection=SelectionBar(self.import_convs);self.scope_step.box.addWidget(self.import_selection)
        self.import_submit=button('查看范围并开始导入',self.review_import,True);self.scope_step.box.addWidget(self.import_submit)
        self.scope_step.box.addWidget(button('核对并补回旧版误合并的记录',self.recover_import))
        self.scope_step.unlock(False);layout.addWidget(self.scope_step);layout.addStretch()
        self.source_list.currentRowChanged.connect(self.source_changed);self.owner.textEdited.connect(lambda _:self.source_changed(self.source_list.currentRow(),False))
        self.mode_changed()

    def mode_changed(self,*_):
        self.cfg.import_mode=self.flow_mode.currentData()
        if hasattr(self,'flow_hint'):
            self.flow_hint.setText('全自动：选定来源后自动取钥，经你确认关闭 QQ，再导入当前范围（默认全部时间与会话）。' if self.cfg.import_mode=='auto' else '引导：选择来源 → 验证密钥 → 读取会话并筛选 → 导入。')

    def source_changed(self,n,fill_owner=True):
        self.flow_running=False;self.import_convs.clear()
        valid=0<=n<len(self.sources);c=self.sources[n] if valid else None
        if c and fill_owner:self.owner.setText(c.owner)
        if not c:
            self.key_step.unlock(False);self.scope_step.unlock(False);return
        if c.kind=='official-backup':
            from .official_backup import ALTERNATIVE
            self.key_step.unlock(False);self.scope_step.unlock(False);self.source_step.note.setText('暂不支持导入：'+c.note+ALTERNATIVE)
            self.source_step.expand(True);return
        allowed=c.kind=='package' or self.owner.text().strip().isdigit()
        self.key_step.unlock(allowed and c.needs_key,'仅验证当前选中的数据库')
        verified=not c.needs_key or self.source_token(c) in self.verified_sources
        self.scope_step.unlock(allowed and verified)
        self.key_state.setText('已认证此来源' if verified else '等待密钥认证')
        self.source_step.complete('已选 1 个来源' if allowed else '请填写归档账号 QQ 号')
        if allowed:self.source_step.expand(False)

    def source_token(self,c):return (str(Path(c.path).resolve()).casefold(),self.owner.text().strip())

    def continue_flow(self):
        if self.worker:return
        if not self.sources:
            self.flow_running=True;self.scan_sources();return
        c=self.guard(self.candidate)
        if not c:return
        if c.needs_key and self.source_token(c) not in self.verified_sources:
            self.reuse_key(auto_acquire=True);return
        if self.cfg.import_mode=='auto':self.review_import()
        else:self.scope_step.expand(True);self.source_conversations()

    def reuse_key(self,auto_acquire=False):
        c=self.guard(self.candidate)
        if not c:return
        def task(cancel,status):
            for key,params in self.keys.candidates(c.owner,c.path):
                state,found=validate(c.path,key,params)
                if state=='valid':return key,found
            return None
        def done(found):
            if found:
                self.keys.put_source(c.owner,c.path,*found,remember=False);self.key_acquired(c)
            elif auto_acquire:self.auto_key()
            else:self.key_state.setText('当前没有可用密钥，请自动获取或手动填写。')
        self.run_job('验证当前来源密钥',task,done)

    def auto_key(self):
        c=self.guard(self.candidate)
        if not c:return
        if not c.needs_key:self.scope_step.unlock(True);return
        if QMessageBox.question(self,'自动获取密钥',key_warning(),QMessageBox.Yes|QMessageBox.Cancel)!=QMessageBox.Yes:return
        def done(rep):
            if rep.ok:self.key_acquired(c)
            else:self.key_state.setText('未取得可用密钥');QMessageBox.warning(self,'获取密钥未完成',rep.text())
        self.run_job('获取并校验密钥',lambda cancel,status:acquire_keys(self.cfg,[c],self.keys,status=status,cancel=cancel),done)

    def manual_key(self):
        c=self.guard(self.candidate)
        if not c:return
        text,ok=QInputDialog.getText(self,'填写密钥',"数据库口令；原始密钥使用 x'64位十六进制'",QLineEdit.Password)
        if not ok or not text:return
        def task(cancel,status):
            found=check_key(c.path,text)
            if not found:raise ValueError('密钥未通过数据库认证')
            self.keys.put_source(c.owner,c.path,*found,remember=self.cfg.remember_keys)
            return True
        self.run_job('校验密钥',task,lambda _:self.key_acquired(c))

    def key_acquired(self,c):
        self.verified_sources[self.source_token(c)]=True;self.key_state.setText('已通过当前数据库认证');self.key_step.complete('密钥有效')
        self.source_step.expand(False);self.key_step.expand(False);self.scope_step.unlock(True);self.scope_step.expand(True)
        self.invalid_notified.discard(self.keys.source_id(c.path))
        token=self.import_ui_token()
        choice=QMessageBox.question(self,'密钥已获取 · 下一步','是否关闭电脑 QQ，并自动导入当前范围的聊天记录？\n\n'+self.import_summary()+'\n\n选择“是”会结束电脑 QQ 的进程，请先保存未发送内容。\n选择“否”会使用 Windows 账户加密保存密钥，稍后可在下方选择会话和时间。',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if choice==QMessageBox.Yes:
            if self.import_ui_unchanged(token):self.close_then_import()
        else:
            item=self.keys.get(self.keys.source_id(c.path)) or self.keys.get(c.owner)
            if item:
                def save():self.keys.put_source(c.owner,c.path,*item,remember=True);return True
                if self.guard(save):
                    self.key_state.setText(f'已加密保存 · 每 {self.cfg.key_check_minutes} 分钟自动检查');self.statusBar().showMessage('密钥已加密保存，可继续选择导入范围',6000)

    def import_summary(self):
        from .desktop import stamp
        times=self.import_dates.values();count=sum(self.import_convs.item(i).checkState()==Qt.Checked for i in range(self.import_convs.count()))
        period=f'{stamp(times[0])} 至 {stamp(times[1])}' if times[0] is not None else '全部时间'
        return f'范围：{count} 个已选会话' + ('' if count else '（0 项表示全部会话）')+f'\n时间：{period}\n附件：引用原路径，不复制整库。'

    def review_import(self):
        c=self.guard(self.candidate);summary=self.guard(self.import_summary)
        if not c or summary is None:return
        if c.needs_key and self.source_token(c) not in self.verified_sources:
            self.reuse_key();return
        token=self.import_ui_token()
        answer=QMessageBox.question(self,'确认导入范围',summary+'\n\n继续时会先关闭电脑 QQ（如正在运行），再导入本地归档。',QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)
        if answer==QMessageBox.Yes and self.import_ui_unchanged(token):self.close_then_import()

    def import_ui_token(self):
        return (self.source_list.currentRow(),self.owner.text(),self.import_dates.enabled.isChecked(),
                self.import_dates.start.dateTime().toSecsSinceEpoch(),self.import_dates.end.dateTime().toSecsSinceEpoch(),
                tuple(self.import_convs.item(i).data(Qt.UserRole) for i in range(self.import_convs.count()) if self.import_convs.item(i).checkState()==Qt.Checked))
    def import_ui_unchanged(self,token):
        if self.closing or self.worker:return False
        if token!=self.import_ui_token():
            QMessageBox.information(self,'导入选项已变化','确认期间你修改了来源或范围，请重新查看范围后执行。');return False
        return True
    def recover_import(self):
        from dataclasses import replace
        from .recovery import recover_identities
        from .discovery import source_fingerprint
        from .store import Archive
        candidate=self.guard(self.candidate)
        if not candidate:return
        candidate=replace(candidate)
        def task(apply,cancel,status):
            archive=Archive(self.cfg.data_path/'archive.db',read_only=not apply)
            try:return recover_identities(archive,candidate,self.keys,apply=apply,cancel=cancel,
                progress=lambda stage,counts:status(stage,counts['source_rows'],0))
            finally:archive.close()
        def checked(result):
            count=result['audit']['recoverable']
            if not count:QMessageBox.information(self,'核对完成','当前已导入范围内，没有发现因消息序号复用而漏导的记录。');return
            if QMessageBox.question(self,'补回误合并记录',f'从原始数据库核对到 {count:,} 条可补回消息。\n是否追加到归档？已有消息与历史变体保留。',QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)!=QMessageBox.Yes:return
            if source_fingerprint(candidate.path)!=result['source_fingerprint']:
                QMessageBox.warning(self,'来源发生变化','请退出 QQ 后重新核对，尚未执行补回。');return
            self.run_job('补回聊天记录',lambda c,s:task(True,c,s),lambda r:QMessageBox.information(self,'补回完成',f"新增 {r['import']['inserted']:,} 条消息。"))
        self.run_job('核对旧版消息完整性',lambda c,s:task(False,c,s),checked)

    def close_then_import(self):
        from .keyscan import find_qq_processes
        if find_qq_processes():self.run_job('关闭 QQ 并准备导入',lambda c,s:close_qq(c,s),lambda _:self.start_import())
        else:self.start_import()

    def source_conversations(self):
        from .desktop import stamp
        c=self.guard(self.candidate)
        if not c:return
        if c.needs_key and self.source_token(c) not in self.verified_sources:self.reuse_key();return
        if c.kind=='package':self.scope_step.note.setText('可迁移归档可直接按时间导入，会话在导入后分类显示。');return
        def show(convs):
            self.import_convs.clear()
            for cv in convs:
                it=QListWidgetItem(f"{'群聊' if cv.kind=='group' else '私聊'} {cv.name or cv.peer} · {cv.count:,} 条\n{stamp(cv.first_time)} — {stamp(cv.last_time)}");it.setData(Qt.UserRole,(cv.kind,cv.peer));it.setCheckState(Qt.Unchecked);self.import_convs.addItem(it)
            self.scope_step.expand(True);self.scope_step.note.setText(f'读取成功 · {len(convs)} 个会话')
        self.run_job('读取会话列表',lambda cancel,status:analyze(c,self.keys,self.cfg.data_path/'tmp',cancel=cancel),show)

    def build_phone(self):
        from .desktop import row,button
        layout=self.page('手机附件','通过 USB 校验传输资源并关联聊天记录。消息正文需另行导入数据库。')
        self.phone_connect=Step(1,'连接并选择手机','开启 USB 调试，在手机上允许这台电脑。')
        self.devices=QComboBox();self.phone_connect.box.addWidget(row(self.devices,button('刷新设备',self.refresh_devices,True)));self.phone_connect.expand(True,False);layout.addWidget(self.phone_connect)
        self.phone_options=Step(2,'选择传输内容','选择需要的资源；中断后保留传输断点。')
        self.phone_kinds={};options=[]
        for k,n in [('image','图片'),('video','视频'),('voice','语音'),('file','文件')]:
            cb=QCheckBox(n);cb.setChecked(k in self.cfg.phone_kinds);self.phone_kinds[k]=cb;options.append(cb)
        box=self.phone_options.box;box.addWidget(row(*options));self.days=QSpinBox();self.days.setRange(0,36500);self.days.setSuffix(' 天内（0 = 全部）');self.days.setValue(self.cfg.phone_days);box.addWidget(self.days)
        self.phone_cache=QCheckBox('包含 QQ 缓存附件（建议限定最近天数）');self.phone_cache.setChecked(self.cfg.phone_include_cache);box.addWidget(self.phone_cache)
        self.phone_auto=QCheckBox('全自动：连接已选手机后自动传输');self.phone_auto.setChecked(self.cfg.phone_auto_on_connect);box.addWidget(self.phone_auto)
        self.phone_auto.toggled.connect(self.save_phone_options)
        box.addWidget(row(button('开始传输与关联',self.start_phone,True),button('打开附件文件夹',lambda:self.open_folder(self.cfg.data_path/'手机媒体'))));self.phone_options.unlock(False);layout.addWidget(self.phone_options)
        self.phone_result=Step(3,'传输结果');self.phone_report=QTextEdit();self.phone_report.setReadOnly(True);self.phone_report.setMinimumHeight(160);self.phone_result.box.addWidget(self.phone_report);self.phone_result.unlock(False);layout.addWidget(self.phone_result);layout.addStretch()
        self.devices.currentIndexChanged.connect(lambda _:self.phone_options.unlock(bool(self.devices.currentData())))

    def build_export(self):
        from .desktop import button,Dates
        layout=self.page('导出与迁移','选择会话后展开格式与范围，确认后导出。可迁移归档支持再次合并导入。')
        self.export_pick=Step(1,'选择会话');self.export_convs=QListWidget();self.export_convs.setMinimumHeight(200);self.export_convs.setMaximumHeight(300);self.export_pick.box.addWidget(self.export_convs)
        self.export_pick.expand(True,False);layout.addWidget(self.export_pick)
        self.export_scope=Step(2,'格式与范围');box=self.export_scope.box;self.export_dates=Dates();box.addWidget(self.export_dates)
        self.export_format=QComboBox()
        for title,value in [('可迁移归档（推荐）','package'),('HTML 分页网页','html'),('纯文本','txt'),('CSV 表格','csv'),('JSON 数据','json')]:self.export_format.addItem(title,value)
        self.export_kind=QComboBox()
        for title,value in [('全部消息',None),('文字','text'),('图片','image'),('视频','video'),('语音','voice'),('文件','file')]:self.export_kind.addItem(title,value)
        box.addWidget(self.export_format);box.addWidget(self.export_kind);self.include_media=QCheckBox('包含已关联附件（复制所选附件以便迁移）');box.addWidget(self.include_media)
        box.addWidget(button('确认范围并选择导出位置',self.start_export,True));self.export_scope.unlock(False);layout.addWidget(self.export_scope)
        self.export_selection=SelectionBar(self.export_convs,lambda n:self.export_scope.unlock(n>0,f'已选 {n} 个会话'));self.export_pick.box.addWidget(self.export_selection);layout.addStretch()

    def init_key_monitor(self):
        self.invalid_notified=set();self.key_probe=None;self.key_timer=QTimer(self)
        self.key_timer.timeout.connect(self.check_saved_keys);self.key_timer.start(max(1,min(1440,self.cfg.key_check_minutes))*60000)
        QTimer.singleShot(5000,self.check_saved_keys)

    def check_saved_keys(self):
        if self.closing or self.worker or self.key_probe:return
        from .desktop import Worker
        worker=Worker(lambda c,s:inspect_saved(self.keys,c));self.key_probe=worker;state=[]
        worker.result.connect(state.extend)
        def finish():
            self.key_probe=None;worker.deleteLater()
            if self.closing:return
            expired=[]
            for entry in state:
                if entry['state']=='valid':self.invalid_notified.discard(entry['id'])
                elif entry['state']=='invalid' and entry['id'] not in self.invalid_notified:
                    self.invalid_notified.add(entry['id']);expired.append(entry['account'])
            if expired:
                self.verified_sources.clear();self.source_changed(self.source_list.currentRow())
                QMessageBox.warning(self,'密钥已失效','以下账号保存的密钥无法认证当前数据库，请重新获取：\n'+'、'.join(sorted(set(expired))))
        worker.finished.connect(finish);worker.start()
