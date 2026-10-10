"""Native Qt desktop: QQ-style conversations, bounded message models, background jobs."""
from __future__ import annotations
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from .summary import read_summary
from pathlib import Path
from PySide6.QtCore import Qt,QAbstractListModel,QModelIndex,QSize,QRectF,QThread,Signal,QTimer,QUrl,QDateTime
from PySide6.QtGui import QColor,QPainter,QFont,QFontMetrics,QDesktopServices,QPixmap,QImageReader,QIcon
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QHBoxLayout,QVBoxLayout,QLabel,QPushButton,
 QLineEdit,QTreeWidget,QTreeWidgetItem,QListView,QStyledItemDelegate,QSplitter,QStackedWidget,QFrame,
 QDialog,QProgressBar,QFileDialog,QCheckBox,QComboBox,QTextEdit,QFormLayout,QDateTimeEdit,
 QSpinBox,QListWidget,QListWidgetItem,QAbstractItemView,QSystemTrayIcon,QMenu,QScrollArea,QLayout,QTabBar)
from .dialogs import MessageBox as QMessageBox
from .config import load_config,save_config,ensure_layout,migrate_data_dir
from .store import Archive,SearchQuery,conversation_title
from .keystore import KeyStore,check_key
from .discovery import discover,default_qq_roots,scan_backup_dir
from .sources import analyze,import_candidate
from .ntqq import Filter
from .auto import acquire_keys,key_warning,pull_phone_media
from .media import register_folder,link_media,media_summary,human_size
from .adb import Adb,find_adb
from .exporters import export_archive
from .version import VERSION
from .chrome import ChromeWindow,AnimatedButton,TitleLabel,app_icon,glyph,fade_in,FramedDialog
from .chat_actions import ChatActions
from .chat_windows import ChatWindows
from .session_tree import SessionTree
from .session_delegate import SessionDelegate,ROW,session_data
from .panels import SlidePanel
from .theme import STYLE
from PySide6.QtCore import QPropertyAnimation,QEasingCurve




def stamp(value):
    try:return time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(value))
    except (ValueError,OSError,TypeError):return '时间未知'

def label(text,kind=None):
    w=QLabel(text);w.setWordWrap(True)
    if kind:w.setObjectName(kind)
    return w

def button(text,fn,primary=False):
    w=AnimatedButton(text);w.clicked.connect(fn)
    if primary:w.setObjectName('primary')
    return w

def row(*widgets):
    w=QWidget();l=QHBoxLayout(w);l.setContentsMargins(0,0,0,0)
    for x in widgets:l.addWidget(x)
    return w

def card(title,description=''):
    w=QFrame();w.setObjectName('card');l=QVBoxLayout(w);l.setContentsMargins(24,22,24,22);l.setSpacing(14)
    l.addWidget(label(title,'title'))
    if description:l.addWidget(label(description,'muted'))
    return w,l

class Dates(QWidget):
    def __init__(self):
        super().__init__();l=QVBoxLayout(self);l.setContentsMargins(0,0,0,0)
        from .date_widgets import DateTimeField
        self.enabled=QCheckBox('限定时间');self.start=DateTimeField(QDateTime.currentDateTime().addMonths(-1));self.end=DateTimeField(QDateTime.currentDateTime())
        for x in (self.start,self.end):x.setCalendarPopup(True);x.setDisplayFormat('yyyy-MM-dd HH:mm');x.setEnabled(False)
        self.enabled.toggled.connect(self.start.setEnabled);self.enabled.toggled.connect(self.end.setEnabled)
        l.addWidget(self.enabled);l.addWidget(row(self.start,label('至'),self.end))
    def values(self):
        if not self.enabled.isChecked():return None,None
        if not self.start.normalize() or not self.end.normalize():raise ValueError('请填写有效的日期与时、分、秒')
        s,e=self.start.dateTime().toSecsSinceEpoch(),self.end.dateTime().toSecsSinceEpoch()
        if s>e:raise ValueError('开始时间不能晚于结束时间')
        return s,e

class Worker(QThread):
    progress=Signal(str,int,int);result=Signal(object);failed=Signal(str)
    def __init__(self,fn):
        super().__init__();self.fn=fn;self.cancel=threading.Event()
    def run(self):
        try:self.result.emit(self.fn(self.cancel,lambda s,d=0,t=0:self.progress.emit(s,int(d),int(t))))
        except Exception as exc:self.failed.emit(str(exc) or type(exc).__name__)

class JobDialog(FramedDialog):
    changed=Signal()
    def __init__(self,title,worker,parent):
        super().__init__(parent);self.worker=worker;self.started=time.monotonic();self.setWindowTitle(title);self.setMinimumWidth(480)
        self.setWindowModality(Qt.WindowModal if '导入' in title else Qt.NonModal);l=QVBoxLayout(self);l.setContentsMargins(24,24,24,24)
        l.addWidget(label(title,'title'));self.stage=label('正在准备…');l.addWidget(self.stage)
        self.elapsed=label('已执行 00:00','timer');l.addWidget(self.elapsed)
        self.bar=QProgressBar();self.bar.setRange(0,0);l.addWidget(self.bar)
        self.count_label=label('总量尚未确定','muted');l.addWidget(self.count_label)
        self.cancel_button=button('取消任务',self.cancel);self.hide_button=button('隐藏至底部',self.hide);l.addWidget(row(self.hide_button,self.cancel_button))
        worker.progress.connect(self.update_progress)
        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(250)
    def tick(self):
        n=int(time.monotonic()-self.started);self.elapsed.setText(f'已执行 {n//60:02d}:{n%60:02d}');self.changed.emit()
    def update_progress(self,s,d,t):
        self.stage.setText(s);self.bar.setRange(0,1000 if t else 0)
        self.count_label.setText(f'已处理 {d:,} / {t:,}' if t else (f'已处理 {d:,} · 总量尚未确定' if d else '总量尚未确定'))
        if t:self.bar.setValue(min(1000,int(d/t*1000)))
        self.changed.emit()
    def cancel(self):
        self.worker.cancel.set();self.cancel_button.setEnabled(False);self.stage.setText('正在安全取消任务，请稍候…')
    def reject(self):self.hide()

class ChatModel(QAbstractListModel):
    def __init__(self):super().__init__();self.rows=[];self.media={}
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid():return None
        m=self.rows[index.row()]
        if role==Qt.DisplayRole:return m['text']
        if role==Qt.UserRole:return m
        if role==Qt.ToolTipRole:return '双击查看完整消息、附件和来源'
    def replace(self,rows,media):
        self.beginResetModel();self.rows=rows;self.media=media;self.endResetModel()

class BubbleDelegate(QStyledItemDelegate):
    def __init__(self,model,view):super().__init__(view);self.model=model;self.view=view;self.cache={}
    def image(self,m):
        for info in self.model.media.get(m['id'],{}).values():
            path=info.get('abs_path')
            if path and info.get('kind')=='image':
                if path not in self.cache:
                    if len(self.cache)>80:self.cache.clear()
                    reader=QImageReader(path);reader.setAutoTransform(True);size=reader.size()
                    if size.isValid():reader.setScaledSize(size.scaled(220,150,Qt.KeepAspectRatio))
                    self.cache[path]=QPixmap.fromImage(reader.read())
                return self.cache[path]
        return QPixmap()
    def message_font(self):
        font=QFont('Microsoft YaHei UI');font.setPixelSize(max(10,min(28,getattr(self.view,'message_font_size',14))));return font
    def metrics(self,m,width):
        maxw=max(180,min(510,width-160));fm=QFontMetrics(self.message_font())
        text=m['text'][:1800] or '[暂无法显示的消息]'
        if len(m['text'])>1800:text+='\n…双击查看完整内容'
        rect=fm.boundingRect(0,0,maxw-30,1000,Qt.TextWordWrap,text)
        im=self.image(m);h=min(270,rect.height())+28+(im.height()+10 if not im.isNull() else 0)
        w=max(100,min(maxw,max(rect.width()+30,im.width()+30)))
        return text,w,h,im
    def sizeHint(self,opt,index):
        m=index.data(Qt.UserRole);return QSize(100,self.metrics(m,max(400,self.view.viewport().width()))[2]+76)
    def paint(self,p,opt,index):
        m=index.data(Qt.UserRole);text,w,h,im=self.metrics(m,opt.rect.width());me=m['is_self']
        x=opt.rect.right()-w-55 if me else opt.rect.left()+55;y=opt.rect.top()+24
        p.save();p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor('#9599a1'));p.setFont(QFont('Microsoft YaHei UI',8))
        name=m['sender_card'] or m['sender_name'] or m['sender_uin'] or '未知发送者'
        p.drawText(QRectF(x,y-20,w,18),Qt.AlignRight if me else Qt.AlignLeft,name+(' · 冲突' if m['conflict'] else ''))
        ax=opt.rect.right()-43 if me else opt.rect.left()+12
        p.setPen(Qt.NoPen);p.setBrush(QColor('#dadde3'));p.drawRoundedRect(QRectF(ax,y,32,32),9,9)
        p.setPen(QColor('#555b66'));p.drawText(QRectF(ax,y,32,32),Qt.AlignCenter,name[:1])
        avatar=m.get('avatar_path')
        if avatar:
            cache_key=('avatar',avatar)
            if cache_key not in self.cache:
                if len(self.cache)>80:self.cache.clear()
                reader=QImageReader(avatar);reader.setScaledSize(QSize(64,64));self.cache[cache_key]=QPixmap.fromImage(reader.read())
            pixmap=self.cache[cache_key]
            if not pixmap.isNull():
                from PySide6.QtGui import QPainterPath
                clip=QPainterPath();clip.addRoundedRect(QRectF(ax,y,32,32),9,9)
                p.save();p.setClipPath(clip);p.drawPixmap(int(ax),int(y),32,32,pixmap);p.restore()
        p.setPen(QColor('#e2e4e8'));p.setBrush(QColor('#292b30' if me else '#ffffff'))
        if opt.state & __import__('PySide6.QtWidgets',fromlist=['QStyle']).QStyle.State_Selected:p.setPen(QColor('#9298a3'))
        p.drawRoundedRect(QRectF(x,y,w,h),10,10);p.setPen(QColor('#ffffff' if me else '#202126'));p.setFont(self.message_font())
        th=h-24-(im.height()+10 if not im.isNull() else 0)
        p.setClipRect(QRectF(x+13,y+12,w-26,th));p.drawText(QRectF(x+13,y+12,w-26,th),Qt.TextWordWrap,text);p.setClipping(False)
        if not im.isNull():p.drawPixmap(int(x+13),int(y+h-im.height()-12),im)
        if getattr(self.view,'hovered_row',-1)==index.row():
            p.setPen(QColor('#6e7480'));p.setFont(QFont('Consolas',9));p.drawText(QRectF(x+w-210 if me else x,y+h+5,210,22),Qt.AlignRight if me else Qt.AlignLeft,stamp(m['time']))
        p.restore()

from .workflows import Workflows
from .flow_widgets import install_popup_motion
from .icons import line_icon
from .design import motion_ms
from .date_widgets import DateTimeField,stamp
from .message_view import MessageView
from .modeless import FileDialog as QFileDialog
from .session_groups import ordered_groups,move_to_group,move_many_to_group,GROUP_ROLE,SORTS,SessionSort

class MainWindow(Workflows,ChatActions,ChromeWindow):
    def __init__(self,cfg=None):
        super().__init__();install_popup_motion();self.cfg=cfg or load_config();ensure_layout(self.cfg.data_path)
        self.arc=Archive(self.cfg.data_path/'archive.db');self.keys=KeyStore(self.cfg.data_path/'keys')
        self.current=None;self.worker=None;self.sources=[];self.closing=False;self.seen_devices=set();self.chat_windows=ChatWindows(self);self.search_windows={}
        self.summary_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='archive-summary')
        self.summary_future=None;self.summary_cancel=threading.Event();self.summary_pending=False;self.summary_closed=False
        self.summary_timer=QTimer(self);self.summary_timer.timeout.connect(self.poll_summary);self.summary_timer.start(80)
        self.setWindowTitle('QQ聊天记录管理器 · '+VERSION);self.resize(1370,880);self.setMinimumSize(1060,720)
        outer=QWidget();self.install_chrome(outer);layout=QHBoxLayout(outer);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        rail=QFrame();rail.setObjectName('rail');rail.setFixedWidth(212);self.rail=rail;rl=QVBoxLayout(rail);rl.setContentsMargins(8,25,8,20);rl.setSpacing(12)
        brand=label('');brand.setPixmap(app_icon().pixmap(28,28));brand.setFixedWidth(30)
        brand_name=label('QQ聊天记录管理器','brandName');brand_name.setWordWrap(False)
        brand_row=row(brand,brand_name);brand_row.setObjectName('brandRow');brand_row.layout().setSpacing(8)
        rl.addWidget(brand_row);rl.addWidget(label('回忆 · 铭记 · 保存','muted'));rl.addSpacing(16)
        self.pages=QStackedWidget();self.nav=[None]*7
        for n,title in ((0,'聊天'),(1,'导入'),(2,'手机'),(3,'导出'),(4,'资料'),(6,'迁移监测'),(5,'设置')):
            b=button(title,lambda _=False,i=n:self.navigate(i));b.setObjectName('nav');b.setIcon(line_icon(('chat','import','phone','export','library','settings','activity')[n]));b.setIconSize(QSize(20,20));b.setCheckable(True);self.nav[n]=b;rl.addWidget(b)
        rl.addStretch();layout.addWidget(rail);main=QWidget();main_layout=QVBoxLayout(main);main_layout.setContentsMargins(0,0,0,0);main_layout.setSpacing(0);self.breadcrumb=label('回忆 · 铭记 · 保存  /  聊天记录','breadcrumb');self.breadcrumb.setContentsMargins(28,12,28,12);main_layout.addWidget(self.breadcrumb);main_layout.addWidget(self.pages,1);layout.addWidget(main,1)
        self.task_strip=QFrame();self.task_strip.setObjectName('taskStrip');task_layout=QHBoxLayout(self.task_strip);task_layout.setContentsMargins(20,8,20,8)
        self.task_text=label('','muted');self.task_bar=QProgressBar();self.task_bar.setFixedWidth(110);self.task_bar.setFixedHeight(6);self.task_bar.setTextVisible(False)
        task_layout.addWidget(self.task_text,1);task_layout.addWidget(self.task_bar);task_layout.addWidget(button('查看任务',self.restore_task));task_layout.addWidget(button('取消',lambda:self.job_dialog.cancel()))
        main_layout.addWidget(self.task_strip);self.task_strip.hide();self.read_jobs=set()
        self.build_chat();self.build_import();self.build_phone();self.build_export();self.build_library();self.build_settings()
        from .transfer_monitor import TransferPage,TransferController
        import os
        self.transfer_page=TransferPage();self.pages.addWidget(self.transfer_page)
        live=os.environ.get('QQARCHIVE_DISABLE_TRANSFER_DETECTION')!='1' and not os.environ.get('QT_QPA_PLATFORM','').startswith('offscreen')
        self.transfer_monitor=TransferController(self,self.transfer_page,autostart=live)
        self.navigate(0);self.refresh()
        self.tray=QSystemTrayIcon(app_icon(),self)
        menu=QMenu();menu.addAction('打开 QQ聊天记录管理器',self.restore);menu.addAction('关闭程序',self.request_exit);self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason:self.restore());self.tray.show()
        self.phone_timer=QTimer(self);self.phone_timer.timeout.connect(self.auto_phone);self.phone_timer.start(30000);self.init_key_monitor()
        QTimer.singleShot(self.SEARCH_INDEX_DELAY_MS,self.ensure_search_index)
    def navigate(self,n):
        changed=self.pages.currentIndex()!=n
        self.pages.setCurrentIndex(n)
        if changed:fade_in(self.pages.currentWidget())
        if n==0 and hasattr(self,'chat_tabs_animation'):
            self.chat_tabs_animation.stop();self.chat_tabs_animation.setStartValue(0);self.chat_tabs_animation.setEndValue(44);self.chat_tabs_animation.start()
        if hasattr(self,'breadcrumb'):self.breadcrumb.setText('回忆 · 铭记 · 保存  /  '+('聊天记录','导入记录','手机附件','导出迁移','资料库','设置','迁移监测')[n])
        for i,b in enumerate(self.nav):b.setChecked(i==n)
    def page(self,title,desc):
        w=QWidget();l=QVBoxLayout(w);l.setContentsMargins(30,26,30,24);l.setSpacing(20)
        l.setSizeConstraint(QLayout.SetMinimumSize)
        l.addWidget(label(title,'title'));l.addWidget(label(desc,'muted'))
        scroll=QScrollArea();scroll.setFrameShape(QFrame.NoFrame);scroll.setWidgetResizable(True);scroll.setWidget(w)
        self.pages.addWidget(scroll);return l
    def build_chat(self):
        w=QWidget();l=QHBoxLayout(w);l.setContentsMargins(0,0,0,0);l.setSpacing(0);self.pages.addWidget(w)
        split=QSplitter();l.addWidget(split)
        sessions=QFrame();sessions.setObjectName('sessions');sl=QVBoxLayout(sessions);sl.setContentsMargins(17,24,17,14)
        sl.addWidget(label('消息','title'));self.conv_filter=QLineEdit();self.conv_filter.setPlaceholderText('搜索聊天或聊天内容');self.conv_filter.textChanged.connect(self.filter_tree);self.conv_filter.returnPressed.connect(self.global_search);sl.addWidget(self.conv_filter);sl.addWidget(button('搜索聊天内容',self.global_search))
        self.session_sort=SessionSort()
        for title,key in SORTS:self.session_sort.addItem(title,key)
        self.session_sort.setCurrentIndex(max(0,self.session_sort.findData(self.cfg.conversation_sort)))
        self.session_sort.currentIndexChanged.connect(self.change_session_sort);sl.addWidget(self.session_sort)
        sl.addWidget(row(button('新建分组',self.new_group),button('批量添加',self.batch_group_picker)))
        self.tree=SessionTree();self.tree.setItemDelegate(SessionDelegate(self.tree));self.tree.setMouseTracking(True);self.tree.setIndentation(12);self.tree.setStyleSheet("QTreeView::branch:selected {background:transparent;}");self.tree.setContextMenuPolicy(Qt.CustomContextMenu);self.tree.customContextMenuRequested.connect(self.conversation_menu);self.tree.setSelectionMode(QAbstractItemView.SingleSelection);self.tree.orderChanged.connect(self.save_conversation_order);self.tree.itemDoubleClicked.connect(self.detach_item);self.tree.setHeaderHidden(True);self.tree.currentItemChanged.connect(lambda item,previous:self.select_conversation(item));sl.addWidget(self.tree,1)
        self.tree.groupMoved.connect(self.move_conversation_group)
        sl.addWidget(button('＋ 导入聊天记录',lambda:self.navigate(1)));split.addWidget(sessions)
        center=QWidget();cl=QVBoxLayout(center);cl.setContentsMargins(0,0,0,0);cl.setSpacing(0)
        self.chat_tabs=QTabBar();self.chat_tabs.setMovable(True);self.chat_tabs.setTabsClosable(False);self.chat_tabs.setExpanding(False);self.chat_tabs.addTab('聊天记录');self.chat_tabs.setTabData(0,None);self.chat_tabs.currentChanged.connect(self.switch_chat_tab);self.chat_tabs.tabCloseRequested.connect(self.close_chat_tab);cl.addWidget(self.chat_tabs)
        self.chat_tabs_animation=QPropertyAnimation(self.chat_tabs,b'maximumHeight',self);self.chat_tabs_animation.setDuration(motion_ms(220));self.chat_tabs_animation.setEasingCurve(QEasingCurve.OutCubic)
        head=QWidget();head.setObjectName('chatHeader');hl=QVBoxLayout(head);hl.setContentsMargins(25,20,22,17)
        self.chat_title=TitleLabel('你的聊天记录，留在自己手中');self.chat_title.setObjectName('title');self.chat_title.setToolTip('双击在独立窗口打开');self.chat_title.doubleClicked.connect(self.detach_current);self.chat_subtitle=label('选择左侧会话，或导入一份聊天记录','muted')
        hl.addWidget(self.chat_title);hl.addWidget(self.chat_subtitle)
        cl.addWidget(head)
        self.model=ChatModel();self.chat=MessageView();self.chat.message_font_size=self.cfg.chat_font_size;self.chat.setObjectName('messages');self.chat.setModel(self.model)
        self.chat.setItemDelegate(BubbleDelegate(self.model,self.chat));self.chat.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.chat.setWordWrap(True);self.chat.doubleClicked.connect(self.message_detail);cl.addWidget(self.chat,1)
        cl.addWidget(self.chat_toolbar());self.connect_chat_actions();self.chat_tabs.hide();split.addWidget(center)
        self.details=SlidePanel(center,290);dl=QVBoxLayout(self.details);dl.setContentsMargins(18,25,18,18)
        dl.addWidget(label('会话信息','title'));self.details_text=QTextEdit();self.details_text.setObjectName('detailsText');self.details_text.setReadOnly(True);dl.addWidget(self.details_text)
        dl.addWidget(button('指定本机头像',self.choose_conversation_avatar));dl.addWidget(button('导出当前会话',self.export_current));self.chat_split=split;split.setSizes([295,780]);self.details_animation=self.details.motion;dl.addWidget(button('关闭详情',lambda:self.details.reveal(False)))
    def build_library(self):
        l=self.page('记录与资料','每次处理独立留痕，按聊天解析和手机附件分别查看。')
        self.stats_label=label('','title');l.addWidget(self.stats_label)
        self.history_offset=0
        self.history_tabs=QTabBar();self.history_tabs.addTab('聊天记录解析');self.history_tabs.addTab('手机附件导入');self.history_tabs.currentChanged.connect(self.change_history_type);l.addWidget(self.history_tabs)
        self.history_list=QListWidget();self.history_list.setSpacing(8);self.history_list.setObjectName('historyList');self.history_list.itemClicked.connect(self.open_history);l.addWidget(self.history_list,1)
        self.history_data=[]
        self.history_back=button('上一页',lambda:self.page_history(-1));self.history_next=button('下一页',lambda:self.page_history(1));self.history_page=label('第 1 页','muted')
        l.addWidget(row(self.history_page,self.history_back,self.history_next))
        l.addWidget(button('补齐名称与头像',self.sync_identity))
        l.addWidget(row(button('校验归档',self.check_archive),button('登记本机附件目录',self.add_media),button('重新关联附件',self.relink_media),button('打开数据目录',lambda:self.open_folder(self.cfg.data_path))))
    def build_settings(self):
        l=self.page('设置','安装程序与聊天数据独立存放。卸载程序时保留聊天数据。')
        c,box=card('存储与连接');self.data_location=QLineEdit(str(self.cfg.data_path));self.data_location.setReadOnly(True);box.addWidget(self.data_location)
        box.addWidget(button('更换数据目录',self.change_data));self.adb_location=QLineEdit(self.cfg.adb_path);self.adb_location.setPlaceholderText('ADB 自动查找；也可指定 adb.exe')
        self.save_location=QLineEdit(self.cfg.save_folder);self.save_location.setReadOnly(True);self.save_location.setPlaceholderText('未设置，另存为时优先使用最近目录')
        box.addWidget(label('附件默认保存文件夹','fieldLabel'));box.addWidget(row(self.save_location,button('选择文件夹',self.choose_save_folder)))
        box.addWidget(row(self.adb_location,button('选择 ADB',self.choose_adb)))
        self.remember=QCheckBox('使用 Windows 账户加密保存已验证密钥');self.remember.setChecked(self.cfg.remember_keys);box.addWidget(self.remember)
        self.key_interval=QSpinBox();self.key_interval.setRange(1,1440);self.key_interval.setValue(self.cfg.key_check_minutes);self.key_interval.setSuffix(' 分钟');box.addWidget(row(label('已保存密钥检查间隔'),self.key_interval))
        box.addWidget(button('立即检查已保存密钥',self.check_saved_keys))
        self.font_size=QSpinBox();self.font_size.setRange(10,28);self.font_size.setSuffix(' px');self.font_size.setValue(self.cfg.chat_font_size)
        self.font_size.valueChanged.connect(self.apply_chat_font);box.addWidget(row(label('聊天记录字体大小'),self.font_size))
        box.addWidget(row(button('保存设置',self.save_settings,True),button('清除已保存密钥',self.forget_keys)));l.addWidget(c)
        l.addWidget(label('兼容性说明：免 Root 不能直接读取手机 QQ 私有消息数据库；自动取钥与 QQ 版本有关。消息解析未识别的部分会标记，不能据此宣称历史记录完整。','muted'));l.addStretch()
    def guard(self,fn):
        from .sources import UnsupportedSource
        try:return fn()
        # A recognised but unsupported source is not a failure; say so plainly.
        except UnsupportedSource as exc:QMessageBox.information(self,'该来源暂不支持导入',str(exc));return None
        except Exception as exc:QMessageBox.warning(self,'无法继续',str(exc));return None
    SEARCH_INDEX_DELAY_MS=1500
    def ensure_search_index(self):
        """Archives created before v18 lack the sender index. Build it as an ordinary
        serialized write job that starts hidden in the task strip and can be cancelled;
        chat and search keep reading committed data meanwhile."""
        if self.closing or self.arc.has_search_index():return
        if self.worker is not None:QTimer.singleShot(30000,self.ensure_search_index);return
        def task(cancel,status):
            status('为按发送人筛选建立索引 · 可继续查看和搜索聊天',0,0)
            try:return self.with_archive(lambda a:a.build_search_index(cancel))
            except InterruptedError as exc:return str(exc)
        def done(result):
            self.statusBar().showMessage(result if isinstance(result,str) else '搜索索引已就绪，按发送人筛选会更快',8000)
        self.run_job('优化搜索索引',task,done,background=True)
    def run_job(self,title,fn,done=None,dialog_parent=None,background=False):
        if self.worker is not None:
            self.statusBar().showMessage('已有任务正在执行，请等待完成；聊天和搜索仍可使用。',5000);return
        if getattr(self,'key_probe',None):
            if getattr(self,'_queued_job',False):return
            self._queued_job=True
            def retry():
                self._queued_job=False
                if not self.closing:self.run_job(title,fn,done,dialog_parent,background)
            QTimer.singleShot(100,retry);return
        self.summary_cancel.set()
        # Freeze only controls that can mutate the archive or switch task inputs.
        # Chat readers use their own committed WAL snapshot and stay available.
        for index in (1,2,3,5):self.pages.widget(index).setEnabled(False)
        worker=Worker(fn);self.worker=worker;self.job_dialog=JobDialog(title,worker,dialog_parent or self)
        dialog=self.job_dialog;dialog.changed.connect(self.update_task_strip);self.task_strip.show();self.update_task_strip()
        state={}
        worker.result.connect(lambda value:state.update(result=value));worker.failed.connect(lambda error:state.update(error=error))
        def finished():
            dialog.timer.stop();dialog.finished.connect(dialog.deleteLater);dialog.accept()
            for index in (1,2,3,5):self.pages.widget(index).setEnabled(True)
            self.task_strip.hide();self.worker=None
            worker.deleteLater();self.refresh()
            if self.closing:self.request_exit();return
            if 'error' in state:
                self.flow_running=False
                box=QMessageBox(self);box.setWindowTitle(title+' · 未完成');box.setText(state['error']);box.setIcon(QMessageBox.Warning);retry=box.addButton('重试',QMessageBox.ActionRole);box.addButton('关闭',QMessageBox.RejectRole);box.exec()
                if box.clickedButton()==retry:QTimer.singleShot(0,lambda:self.run_job(title,fn,done,dialog_parent,background))
            elif done:done(state.get('result'))
            else:QMessageBox.information(self,title,str(state.get('result') or '已完成'))
        worker.finished.connect(finished);worker.start()
        if not background:self.job_dialog.show()   # background jobs start in the task strip
    def update_task_strip(self):
        d=self.job_dialog
        self.task_text.setText(d.windowTitle()+' · '+d.stage.text()+'\n'+d.count_label.text()+' · '+d.elapsed.text())
        self.task_bar.setRange(d.bar.minimum(),d.bar.maximum());self.task_bar.setValue(d.bar.value())
    def restore_task(self):
        if self.worker:self.job_dialog.show();self.job_dialog.raise_();self.job_dialog.activateWindow()
    def run_read_job(self,title,fn,done):
        from .background import ReadJob
        reader=ReadJob(self.cfg.data_path/'archive.db',self);self.read_jobs.add(reader)
        def finish(value=None,error=None):
            reader.stop();self.read_jobs.discard(reader);reader.deleteLater()
            if self.closing:return
            if error:QMessageBox.warning(self,title,error)
            else:done(value)
        reader.result.connect(lambda value:finish(value));reader.error.connect(lambda error:finish(error=error))
        reader.elapsed.connect(lambda seconds:self.statusBar().showMessage(f'{title} · 已用 {seconds:.0f} 秒'))
        reader.submit(fn)
    def with_archive(self,action):
        arc=Archive(self.cfg.data_path/'archive.db')
        try:return action(arc)
        finally:arc.close()
    def refresh(self):
        selected={self.export_convs.item(i).data(Qt.UserRole) for i in range(self.export_convs.count()) if self.export_convs.item(i).checkState()==Qt.Checked}
        source_tree=self.arc.conversation_tree()
        self._session_records=[c for groups in source_tree.values() for convs in groups.values() for c in convs]
        self._session_rows={c['id']:session_data(self.arc,c) for c in self._session_records}
        self.render_conversations()
        self.export_convs.blockSignals(True);self.export_selection.suspended=True
        try:
            self.export_convs.clear()
            for c in self._session_records:
                ex=QListWidgetItem(f"{c['owner']} · {conversation_title(c)} · {c['msg_count']} 条");ex.setData(Qt.UserRole,c['id']);ex.setCheckState(Qt.Checked if c['id'] in selected else Qt.Unchecked);self.export_convs.addItem(ex)
        finally:
            self.export_convs.blockSignals(False);self.export_selection.suspended=False;self.export_selection.refresh()
        self.summary_cancel.set();self.summary_pending=True
        if not self.stats_label.text():self.stats_label.setText('正在读取归档统计…')
    def render_conversations(self,reveal_group=None):
        """Display-only changes never query SQLite or rebuild the export chooser."""
        expansion={}
        for i in range(self.tree.topLevelItemCount()):
            root=self.tree.topLevelItem(i);name=root.data(0,GROUP_ROLE);expansion[(name,None)]=root.isExpanded()
            for j in range(root.childCount()):
                child=root.child(j);expansion[(name,child.text(0))]=child.isExpanded()
        scroll=self.tree.verticalScrollBar().value();self.tree.smooth.animation.stop()
        animated=self.tree.isAnimated();self.tree.setAnimated(False);self.tree.setUpdatesEnabled(False)
        self.tree.blockSignals(True);self.tree.clear()
        display=ordered_groups(self._session_records,self.cfg)
        pins={cid:i for i,cid in enumerate(self.cfg.pinned_conversations)}
        for group_name,items in display.items():
            root=QTreeWidgetItem(self.tree,[f"{group_name or ('未分组' if self.cfg.conversation_groups else '全部聊天')} · {len(items)}"])
            root.setData(0,GROUP_ROLE,group_name);root.setFlags((root.flags() & ~Qt.ItemIsDragEnabled)|Qt.ItemIsDropEnabled)
            groups={'pinned':[c for c in items if c['id'] in pins],
                    'group':[c for c in items if c['kind']=='group' and c['id'] not in pins],
                    'private':[c for c in items if c['kind']=='private' and c['id'] not in pins]}
            if self.cfg.conversation_order or self.cfg.conversation_sort!='manual':
                groups={'pinned':groups['pinned'],'ordered':[c for c in items if c['id'] not in pins]}
            for kind,convs in groups.items():
                if not convs:continue
                cat=QTreeWidgetItem(root,[{'group':'群聊','private':'私聊','ordered':'组内聊天','pinned':'已置顶'}.get(kind,'私聊')])
                cat.setData(0,GROUP_ROLE,group_name);cat.setFlags((cat.flags() & ~Qt.ItemIsDragEnabled)|Qt.ItemIsDropEnabled)
                for c in convs:
                    own=c['owner'];badge='群聊' if c['kind']=='group' else '私聊'
                    item=QTreeWidgetItem(cat,[f"{conversation_title(c)}\n{badge} · {c['msg_count']:,} 条记录"])
                    item.setData(0,Qt.UserRole,c['id']);item.setFlags(item.flags() & ~Qt.ItemIsDropEnabled)
                    item.setData(0,ROW,dict(self._session_rows[c['id']],pinned=c['id'] in pins))
                    item.setToolTip(0,f"{c['peer']} · QQ {own}\n双击独立打开 · 拖动调整顺序")
                    if self.current and self.current['id']==c['id']:self.tree.setCurrentItem(item)
            root.setExpanded(True if group_name==reveal_group else expansion.get((group_name,None),True))
            for i in range(root.childCount()):
                child=root.child(i);child.setExpanded(True if group_name==reveal_group else expansion.get((group_name,child.text(0)),True))
        self.filter_tree(self.conv_filter.text())
        self.tree.doItemsLayout();self.tree.verticalScrollBar().setValue(scroll)
        self.tree.blockSignals(False);self.tree.setUpdatesEnabled(True);self.tree.setAnimated(animated)
    def poll_summary(self):
        if self.closing or self.summary_closed:return
        if self.summary_future and self.summary_future.done():
            future=self.summary_future;self.summary_future=None
            if not self.summary_cancel.is_set():
                try:self.show_summary(*future.result())
                except Exception as exc:self.stats_label.setText('统计暂未完成：'+str(exc))
        if self.summary_pending and self.summary_future is None and self.worker is None:
            self.summary_pending=False;self.summary_cancel=threading.Event()
            self.summary_future=self.summary_pool.submit(read_summary,self.cfg.data_path/'archive.db',self.summary_cancel)
    def stop_summary(self):
        if self.summary_closed:return
        self.summary_closed=True;self.summary_cancel.set();self.summary_timer.stop()
        self.summary_pool.shutdown(wait=False,cancel_futures=True)
    def show_summary(self,st,batches):
        self.stats_label.setText(f"{st['messages']:,} 条消息    {st['conversations']} 个会话    {st['accounts']} 个账号    {st['media']} 个附件")
        self.history_data=batches;self.render_history()
    def render_history(self,*_):
        if not hasattr(self,'history_list'):return
        self.history_list.clear();phone=self.history_tabs.currentIndex()==1
        rows=self.arc.phone_history(31,self.history_offset) if phone else self.arc.batches(limit=31,offset=self.history_offset)
        self.history_back.setEnabled(self.history_offset>0);self.history_next.setEnabled(len(rows)>30);self.history_page.setText(f'第 {self.history_offset//30+1} 页')
        rows=rows[:30]
        for entry in rows:
            state={'done':'已完成','failed':'未完成','cancelled':'已取消','partial':'部分完成'}.get(entry['status'],entry['status'])
            details=(f"已传输 {entry['files']:,} 个文件 · 跳过 {entry['skipped']:,} 个 · 失败 {entry['failed']:,} 个" if phone else f"新增 {entry['inserted']:,} 条 · 重复 {entry['duplicates']:,} 条 · 冲突 {entry['conflicts']:,} 条")
            item=QListWidgetItem(f"{'手机附件导入' if phone else '聊天记录解析'}  #{entry['id']}     {state}\n{stamp(entry['started_at'])}\n{details}")
            item.setData(Qt.UserRole,(phone,entry));self.history_list.addItem(item)
        if not rows:
            item=QListWidgetItem('暂无手机附件导入记录' if phone else '暂无聊天记录解析记录');item.setFlags(Qt.NoItemFlags);self.history_list.addItem(item)
        fade_in(self.history_list)
    def change_history_type(self,*_):
        self.history_offset=0;self.render_history()
    def page_history(self,direction):
        self.history_offset=max(0,self.history_offset+direction*30);self.render_history()
    def open_history(self,item):
        data=item.data(Qt.UserRole)
        if not data:return
        phone,entry=data;dialog=FramedDialog(self);dialog.setWindowTitle('处理记录详情');dialog.resize(620,500);layout=QVBoxLayout(dialog)
        text=QTextEdit();text.setReadOnly(True)
        lines=[f"记录编号：{entry['id']}",f"开始：{stamp(entry['started_at'])}",f"结束：{stamp(entry.get('finished_at'))}",f"状态：{entry['status']}"]
        if phone:lines += ['',entry['summary']]
        else:lines += [f"来源：{entry['source_label']}",f"读取 {entry['read_count']:,} 条",f"新增 {entry['inserted']:,} / 重复 {entry['duplicates']:,} / 冲突 {entry['conflicts']:,}",'',entry.get('note','')]
        text.setPlainText('\n'.join(lines));layout.addWidget(text);layout.addWidget(button('关闭',dialog.accept));dialog.exec()
    def save_conversation_order(self,ids):
        self.cfg.conversation_order=ids;self.cfg.pinned_conversations=[cid for cid in ids if cid in self.cfg.pinned_conversations];save_config(self.cfg);self.render_conversations()
    def change_session_sort(self,*_):
        self.cfg.conversation_sort=self.session_sort.currentData();save_config(self.cfg);self.render_conversations()
    def new_group(self):
        from .dialogs import InputDialog
        name,ok=InputDialog.getText(self,'新建聊天分组','分组名称')
        name=name.strip()
        if not ok or not name:return
        if name in self.cfg.conversation_groups:QMessageBox.information(self,'分组已存在','请使用不同的分组名称。');return
        self.cfg.conversation_groups[name]=[];save_config(self.cfg);self.render_conversations(reveal_group=name)
        if getattr(self,'group_picker',None) and self.group_picker.isVisible():self.group_picker.set_groups(self.cfg.conversation_groups,name)
    def rename_group(self,old):
        from .dialogs import InputDialog
        name,ok=InputDialog.getText(self,'重命名分组','新的分组名称',text=old);name=name.strip()
        if not ok or not name or name==old:return
        if name in self.cfg.conversation_groups:QMessageBox.information(self,'分组已存在','请使用不同的分组名称。');return
        if old not in self.cfg.conversation_groups:return
        self.cfg.conversation_groups={name if key==old else key:value for key,value in self.cfg.conversation_groups.items()};save_config(self.cfg);self.render_conversations(reveal_group=name)
        self.sync_group_picker()
    def remove_group(self,name):
        if QMessageBox.question(self,'移除分组','移除“'+name+'”？其中的聊天会回到未分组，聊天记录将保留。',QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)!=QMessageBox.Yes:return
        self.cfg.conversation_groups.pop(name,None);save_config(self.cfg);self.render_conversations();self.sync_group_picker()
    def move_conversation_group(self,cid,name,before=None):
        if name and name not in self.cfg.conversation_groups:return
        if cid not in self._session_rows:return
        ids=self.tree.ids();move_to_group(self.cfg,cid,name)
        if cid in ids:ids.remove(cid)
        ids.insert(ids.index(before) if before in ids else len(ids),cid)
        self.cfg.conversation_order=ids;save_config(self.cfg);self.render_conversations(reveal_group=name);self.sync_group_picker()
    def sync_group_picker(self):
        dialog=getattr(self,'group_picker',None)
        if dialog and dialog.isVisible():dialog.set_groups(self.cfg.conversation_groups)
    def batch_group_picker(self,target=''):
        from .group_picker import GroupPicker
        dialog=getattr(self,'group_picker',None)
        if dialog and dialog.isVisible():
            dialog.set_groups(self.cfg.conversation_groups,target or None);dialog.raise_();dialog.activateWindow();return dialog
        dialog=GroupPicker(self._session_records,self.cfg.conversation_groups,self,target=target);self.group_picker=dialog
        dialog.createRequested.connect(self.new_group)
        def apply(ids,name):
            if name not in self.cfg.conversation_groups:
                dialog.set_groups(self.cfg.conversation_groups);dialog.status.setText('目标分组已变更，请重新选择。');return
            ids=[cid for cid in ids if cid in self._session_rows]
            move_many_to_group(self.cfg,ids,name);save_config(self.cfg);self.render_conversations(reveal_group=name)
            self.statusBar().showMessage(f'已将 {len(ids)} 个聊天添加到“{name}”',5000);dialog.accept()
        dialog.applyRequested.connect(apply)
        dialog.finished.connect(lambda *_:setattr(self,'group_picker',None));dialog.finished.connect(dialog.deleteLater)
        dialog.show();return dialog
    def detach_item(self,item,column=0):
        cid=item.data(0,Qt.UserRole)
        if cid:return self.chat_windows.open(cid)
    def detach_current(self):
        if self.current:return self.chat_windows.open(self.current['id'])
    def toggle_details(self):self.details.reveal(not self.details.opened)
    def details_finished(self):pass
    def conversation_menu(self,point):
        item=self.tree.itemAt(point);cid=item.data(0,Qt.UserRole) if item else None
        if not cid:
            group=self.tree.group_of(item);menu=QMenu(self.tree)
            menu.addAction('新建分组',self.new_group)
            menu.addAction('批量添加聊天',lambda:self.batch_group_picker(group))
            if group:
                menu.addAction('重命名分组',lambda:self.rename_group(group));menu.addAction('移除分组',lambda:self.remove_group(group))
            self._group_menu=menu;menu.aboutToHide.connect(menu.deleteLater);menu.popup(self.tree.viewport().mapToGlobal(point));return
        menu=QMenu(self.tree);menu.addAction('取消置顶聊天' if cid in self.cfg.pinned_conversations else '置顶聊天',lambda:self.toggle_pin_conversation(cid))
        menu.addAction('独立打开聊天',lambda:self.chat_windows.open(cid));groups=menu.addMenu('移至分组')
        for name in self.cfg.conversation_groups:
            groups.addAction(name,lambda checked=False,n=name:self.move_conversation_group(cid,n))
        groups.addSeparator();groups.addAction('移出分组',lambda:self.move_conversation_group(cid,''));groups.addAction('新建分组',self.new_group)
        self._group_menu=menu;menu.aboutToHide.connect(menu.deleteLater);menu.popup(self.tree.viewport().mapToGlobal(point))
    def toggle_pin_conversation(self,cid):
        pins=self.cfg.pinned_conversations
        if cid in pins:pins.remove(cid)
        else:pins.append(cid)
        save_config(self.cfg);self.render_conversations()
        for window in self.chat_windows.windows:
            for pane in window.tabs.panes:window.tabs.update_row(pane)
            window.tabs.sort_pins()
    def apply_chat_font(self,size):
        self.cfg.chat_font_size=size
        for reader in [self,*self.chat_windows.panes.values()]:
            view=reader.chat;index=view.indexAt(__import__('PySide6.QtCore',fromlist=['QPoint']).QPoint(8,8))
            view.message_font_size=size;view.doItemsLayout()
            if index.isValid():view.scrollTo(index,QAbstractItemView.PositionAtTop)
            view.viewport().update()
        for search in self.search_windows.values():search.results.message_font_size=size;search.results.doItemsLayout();search.results.viewport().update()
    def global_search(self,senders=None,sender_meta=None):
        from .search_window import SearchWindow
        key=('global',None);window=self.search_windows.get(key)
        if window and window.isVisible():
            if senders:window.show_sender_history(senders,sender_meta)
            else:window.keyword.setText(self.conv_filter.text());window.search()
            window.raise_();window.activateWindow();return window
        window=SearchWindow(self,global_mode=True,keyword='' if senders else self.conv_filter.text(),senders=senders,sender_meta=sender_meta);self.search_windows[key]=window
        window.finished.connect(lambda *_:self.search_windows.pop(key,None));window.show();return window
    def filter_tree(self,text):
        needle=text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            account=self.tree.topLevelItem(i);total=0;visible=0
            for j in range(account.childCount()):
                cat=account.child(j)
                for k in range(cat.childCount()):
                    it=cat.child(k);it.setHidden(bool(needle and needle not in (it.text(0)+it.toolTip(0)).lower()));total+=1;visible+=not it.isHidden()
            name=account.data(0,GROUP_ROLE) or ('未分组' if self.cfg.conversation_groups else '全部聊天')
            account.setText(0,f'{name} · {visible} / {total}' if needle else f'{name} · {total}')
    def select_conversation(self,item,column=0):
        if item is None:return
        cid=item.data(0,Qt.UserRole)
        if not cid:return
        self.open_chat_tab(cid)
        # Never leave the previous conversation visible under the new title.
        self.current=self.arc.conversation(cid);self.chat_title.setText(conversation_title(self.current));self.chat_subtitle.setText(f"{self.current['peer']} · 归档账号 {self.current['owner']} · {self.current['msg_count']:,} 条")
        self.latest();first,last=self.arc.time_bounds(cid);self.details_text.setPlainText(f"归档账号：{self.current['owner']}\n会话：{self.current['peer']}\n消息：{self.current['msg_count']:,}\n\n最早有效时间\n{stamp(first)}\n\n最近有效时间\n{stamp(last)}\n\n无效时间显示为“时间未知”。覆盖区间表示已导入批次的时间范围，不能保证期间每条消息齐全。")
    def show_sources(self,sources):
        self.sources=sources;self.source_list.clear()
        self.source_list.setVisible(bool(sources))
        self.source_step.note.setText(f'找到 {len(sources)} 个来源' if sources else '未找到来源，可手动添加数据库或备份目录。')
        for c in sources:
            if c.kind=='official-backup':
                # Not "needs a key": no key would make this container importable today.
                self.source_list.addItem(f"{c.owner or '待指定账号'} · QQ 官方备份 · 暂不支持导入（选中查看原因）\n{c.path}");continue
            kind={'pc':'电脑 QQ','backup':'备份数据库','plain':'未加密数据库','package':'可迁移归档'}.get(c.kind,c.kind)
            status={'new':'未导入','imported':'已导入','changed':'有更新'}.get(c.status,c.status)
            self.source_list.addItem(f"{c.owner or '待指定账号'} · {kind} · {'需密钥' if c.needs_key else '可读取'} · {status}\n{c.path}")
        if sources:self.source_list.setCurrentRow(0)
        if getattr(self,'auto_after_scan',False):
            self.auto_after_scan=False
            if len(sources)==1:QTimer.singleShot(0,self.continue_flow)
            else:self.flow_hint.setText('发现多个来源，请明确选择要导入的数据库后点击继续。')
    def scan_sources(self):
        self.auto_after_scan=bool(self.flow_running)
        self.run_job('扫描来源',lambda c,s:self.with_archive(lambda a:discover(self.cfg.qq_roots or default_qq_roots(),self.cfg.backup_dirs,a,self.keys)),self.show_sources)
    def add_source(self):
        path,_=QFileDialog.getOpenFileName(self,'选择数据库或归档包','','数据库 / 归档 (*.db *.qqarchive.zip);;所有文件 (*)')
        if path:self.run_job('检查来源',lambda c,s:scan_backup_dir(Path(path)),lambda found:self.show_sources(self.sources+found))
    def add_source_dir(self):
        path=QFileDialog.getExistingDirectory(self,'选择备份或归档目录')
        if path:self.cfg.backup_dirs.append(path);save_config(self.cfg);self.scan_sources()
    def candidate(self):
        n=self.source_list.currentRow()
        if not 0<=n<len(self.sources):raise ValueError('请先选择来源')
        c=self.sources[n]
        if c.kind=='official-backup':
            from .sources import UnsupportedSource
            from .official_backup import ALTERNATIVE
            raise UnsupportedSource(c.note+'\n\n'+ALTERNATIVE)
        if c.kind!='package':
            owner=self.owner.text().strip()
            if not owner.isdigit():raise ValueError('请填写归档账号 QQ 号')
            c.owner=owner
        return c
    def start_import(self):
        c=self.guard(self.candidate);times=self.guard(self.import_dates.values)
        if not c or times is None:return
        selected={tuple(self.import_convs.item(i).data(Qt.UserRole)) for i in range(self.import_convs.count()) if self.import_convs.item(i).checkState()==Qt.Checked}
        flt=Filter(*times,selected or None)
        def task(cancel,status):
            started=time.monotonic()
            def progress(n,t):
                rate=n.read/max(.001,time.monotonic()-started)
                status(f'解密、读取与合并 · 平均 {rate:,.0f} 条/秒',n.read,t)
            def action(a):
                result=import_candidate(a,c,self.keys,flt=flt,work_dir=self.cfg.data_path/'tmp',cancel=cancel,progress=progress)
                from .profiles import sync_profiles
                try:identity=sync_profiles(a,c,self.keys,cancel=cancel,progress=status)
                except InterruptedError:return result.summary()+'\n正文已保存；资料和附件补齐已取消。'
                status('正文已保存 · 正在登记附件',0,0)
                for folder in c.extra.get('media_dirs',[]):
                    if cancel.is_set():break
                    register_folder(a,Path(folder),cancel=cancel,progress=lambda n,t,phase:status('正文已保存 · '+phase,n,t))
                try:link_media(a,cancel=cancel,progress=lambda n,t,phase:status('正文已保存 · '+phase,n,t))
                except InterruptedError:return result.summary()+'\n正文已保存；附件关联已取消，可在资料库重新关联，无需重新导入。'
                return result.summary()+f"\n补齐 {identity['updated']} 个会话名称。"+('\n'+'\n'.join(identity['errors']) if identity['errors'] else '')
            return self.with_archive(action)
        self.run_job('导入聊天记录',task)
    def refresh_devices(self):
        path=find_adb(self.adb_location.text().strip() or self.cfg.adb_path)
        if not path:QMessageBox.warning(self,'手机连接','未找到 ADB，请在设置选择官方 platform-tools 中的 adb.exe');return
        def done(devices):
            self.devices.clear()
            for d in devices:self.devices.addItem(d.label+(' · '+d.state if not d.ready else ''),d.serial if d.ready else None)
            i=self.devices.findData(self.cfg.phone_device)
            if i>=0:self.devices.setCurrentIndex(i)
            self.phone_options.unlock(bool(self.devices.currentData()))
            if not devices:self.phone_connect.note.setText('未检测到手机，请检查 USB 调试和授权。')
        self.run_job('检查手机连接',lambda c,s:Adb(path).list_devices(),done)
    def save_phone_options(self):
        self.cfg.phone_auto_on_connect=self.phone_auto.isChecked();self.cfg.phone_days=self.days.value()
        self.cfg.phone_include_cache=self.phone_cache.isChecked()
        self.cfg.phone_kinds=[k for k,cb in self.phone_kinds.items() if cb.isChecked()]
        if self.devices.currentData():self.cfg.phone_device=self.devices.currentData()
        save_config(self.cfg)
    def start_phone(self,automatic=False):
        self.save_phone_options()
        device=self.cfg.phone_device if automatic else self.devices.currentData()
        if not device:
            if not automatic:QMessageBox.information(self,'手机连接','请先刷新设备并选择已授权手机')
            return
        if not self.cfg.phone_kinds:QMessageBox.information(self,'选择内容','请至少选择一种附件类型');return
        def task(cancel,status):return self.with_archive(lambda a:pull_phone_media(self.cfg,a,device=device,status=status,cancel=cancel))
        def done(rep):self.phone_report.setPlainText(rep.text());self.phone_result.unlock(True);self.phone_result.expand(True);self.phone_options.expand(False);self.navigate(2)
        self.run_job('手机附件传输',task,done)
    def auto_phone(self):
        if self.worker or not self.cfg.phone_auto_on_connect or not self.cfg.phone_device:return
        path=find_adb(self.cfg.adb_path)
        if not path:return
        # A short device-only probe runs in a thread; it never scans QQ memory.
        if getattr(self,'probe',None):return
        probe=Worker(lambda c,s:Adb(path).authorized_devices());self.probe=probe
        found=[];probe.result.connect(lambda ds:found.extend(d.serial for d in ds))
        def complete():
            fresh=self.cfg.phone_device in found and self.cfg.phone_device not in self.seen_devices
            self.seen_devices=set(found);self.probe=None;probe.deleteLater()
            if fresh and not self.worker:self.start_phone(True)
        probe.finished.connect(complete);probe.start()
    def export_current(self):
        self.navigate(3)
        if self.current:
            for i in range(self.export_convs.count()):
                it=self.export_convs.item(i);it.setCheckState(Qt.Checked if it.data(Qt.UserRole)==self.current['id'] else Qt.Unchecked)
    def start_export(self):
        times=self.guard(self.export_dates.values)
        if times is None:return
        ids={self.export_convs.item(i).data(Qt.UserRole) for i in range(self.export_convs.count()) if self.export_convs.item(i).checkState()==Qt.Checked}
        if not ids:QMessageBox.information(self,'选择会话','请勾选要导出的会话');return
        fmt=self.export_format.currentData();media=self.include_media.isChecked();kind=self.export_kind.currentData()
        summary=f'已选 {len(ids)} 个会话 · {self.export_format.currentText()}\n时间：'+(f'{stamp(times[0])} 至 {stamp(times[1])}' if times[0] is not None else '全部时间')+'\n附件：'+('复制已关联附件' if self.include_media.isChecked() else '不包含附件文件')
        if QMessageBox.question(self,'确认导出',summary,QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel)!=QMessageBox.Yes:return
        parent=QFileDialog.getExistingDirectory(self,'选择导出位置')
        if not parent:return
        path=Path(parent)/('QQ聊天记录管理器-'+time.strftime('%Y%m%d-%H%M%S'))
        self.run_job('导出记录',lambda cancel,status:self.with_archive(lambda a:export_archive(a,path,fmt,conversation_ids=ids,start=times[0],end=times[1],kinds={kind} if kind else None,attachments=media,cancel=cancel,progress=lambda n,t:status('导出中',n,t))),lambda r:(QMessageBox.information(self,'导出完成',f"导出 {r['messages']} 条消息，{r['attachments']} 个附件\n{r['path']}"),self.open_folder(path)))
    def check_archive(self):self.run_job('校验归档',lambda c,s:self.with_archive(lambda a:'\n'.join(a.integrity_check())))
    def sync_identity(self):
        from .profiles import sync_profiles
        from .discovery import _candidate
        def task(cancel,status):
            def action(a):
                results=[]
                for path,owner in a.con.execute("SELECT DISTINCT source_label,owner FROM batches WHERE status='done' AND source_kind LIKE 'ntqq%'").fetchall():
                    if cancel.is_set():break
                    c=_candidate(Path(path),'pc',owner) if Path(path).is_file() else None
                    if c:results.append(sync_profiles(a,c,self.keys,cancel=cancel,progress=status))
                if not results:return '没有可用的电脑 QQ 来源，请先扫描并导入来源。'
                errors=[e for r in results for e in r['errors']]
                return f"已补齐 {sum(r['updated'] for r in results)} 个会话名称。\n"+'\n'.join(errors+[results[0]['avatar_note']])
            return self.with_archive(action)
        def done(result):
            if self.current:
                item=QTreeWidgetItem();item.setData(0,Qt.UserRole,self.current['id']);self.select_conversation(item)
            for i in range(self.chat_tabs.count()):
                cid=self.chat_tabs.tabData(i)
                if cid:self.chat_tabs.setTabText(i,conversation_title(self.arc.conversation(cid)))
            QMessageBox.information(self,'资料补齐',result)
        self.run_job('补齐会话资料',task,done)
    def choose_conversation_avatar(self):
        if not self.current:return
        if self.worker:self.statusBar().showMessage('当前任务结束后可更改头像',5000);return
        c=dict(self.current)
        path,_=QFileDialog.getOpenFileName(self,'选择该会话的头像（原址引用）','','图片 (*.png *.jpg *.jpeg *.webp *.bmp)')
        if not path:return
        reader=QImageReader(path)
        if not reader.canRead():QMessageBox.warning(self,'无法读取','请选择可读取的头像图片');return
        from .profiles import apply_profiles
        self.run_job('更新头像',lambda cancel,status:self.with_archive(lambda a:apply_profiles(a,[dict(owner=c['owner'],kind=c['kind'],peer=c['peer'],uid=c['peer_uid'],name=c['name'],avatar_path=path,source='user')])),lambda _:self.latest())
    def relink_media(self):
        self.run_job('关联现有附件',lambda c,s:self.with_archive(lambda a:link_media(a,cancel=c,progress=lambda n,t,phase:s(phase,n,t))))
    def add_media(self):
        path=QFileDialog.getExistingDirectory(self,'选择附件目录（只引用，不复制）')
        if path:
            def task(c,s):
                def action(a):
                    n=register_folder(a,Path(path),with_hash=True,cancel=c,progress=lambda i,t,n:s('登记附件',i,t));links=link_media(a,cancel=c,progress=lambda n,t,phase:s(phase,n,t));return f"登记 {n} 个附件，关联 {links['linked']} 个"
                return self.with_archive(action)
            self.run_job('登记附件',task)
    def choose_adb(self):
        p,_=QFileDialog.getOpenFileName(self,'选择 adb.exe','','adb.exe (adb.exe)')
        if p:self.adb_location.setText(p)
    def save_settings(self):
        self.cfg.save_folder=self.save_location.text().strip()
        self.cfg.adb_path=self.adb_location.text().strip();self.cfg.remember_keys=self.remember.isChecked();self.cfg.key_check_minutes=self.key_interval.value();self.key_timer.setInterval(self.cfg.key_check_minutes*60000);save_config(self.cfg);self.statusBar().showMessage('设置已保存',4000)
    def choose_save_folder(self):
        path=QFileDialog.getExistingDirectory(self,'选择附件默认保存文件夹',self.save_location.text())
        if path:self.save_location.setText(path)
    def forget_keys(self):
        if QMessageBox.question(self,'清除密钥','清除本次运行和本机保存的密钥？聊天记录保留。')!=QMessageBox.Yes:return
        for acc in set(self.keys._session)|{x['id'] for x in self.keys.saved_accounts()}:self.keys.forget(acc)
        self.verified_sources.clear();self.invalid_notified.clear();self.source_changed(self.source_list.currentRow());self.statusBar().showMessage('密钥已清除',4000)
    def change_data(self):
        p=QFileDialog.getExistingDirectory(self,'选择新的空数据目录')
        if not p:return
        choice=QMessageBox.question(self,'数据目录','将现有归档校验复制到新目录？选“否”使用新目录，原数据保留。',QMessageBox.Yes|QMessageBox.No|QMessageBox.Cancel)
        if choice==QMessageBox.Cancel:return
        if Path(p).resolve()==self.cfg.data_path.resolve():return
        if choice==QMessageBox.Yes:
            def task(c,s):
                # Readers keep their own connections. Never close/reopen a GUI
                # SQLite connection from this worker thread.
                migrate_data_dir(self.cfg.data_path,Path(p),lambda i,t,n:s('校验复制归档',i,t))
                return p
            self.run_job('迁移数据',task,self.apply_data)
        else:self.apply_data(p)
    def apply_data(self,p):
        self.cfg.pending_data_dir=str(p);save_config(self.cfg);QMessageBox.information(self,'目录已保存','下次启动将使用新的数据目录。旧目录仍然保留。')
    def open_folder(self,p):
        p=Path(p);p.mkdir(parents=True,exist_ok=True);QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
    def open_file(self,p):
        if not Path(p).is_file():QMessageBox.warning(self,'附件缺失','原文件已移动或不存在');return
        if Path(p).suffix.lower() in ('.exe','.bat','.cmd','.ps1','.vbs','.js','.lnk','.msi'):
            self.open_folder(Path(p).parent);return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
    def restore(self):
        self.showNormal();self.raise_();self.activateWindow()
        for w in self.chat_windows.windows:w.show()
    def request_exit(self):
        self.closing=True
        if hasattr(self,'transfer_monitor') and not self.transfer_monitor.shutdown():
            QTimer.singleShot(100,self.request_exit);return
        self.stop_summary()
        for reader in self.read_jobs:reader.stop()
        self.read_jobs.clear()
        if self.worker:self.worker.cancel.set();return
        probe=getattr(self,'probe',None)
        if probe and probe.isRunning():probe.cancel.set();QTimer.singleShot(100,self.request_exit);return
        key_probe=getattr(self,'key_probe',None)
        if key_probe and key_probe.isRunning():key_probe.cancel.set();QTimer.singleShot(100,self.request_exit);return
        self.key_timer.stop();self.phone_timer.stop();self.chat_windows.close_all();self.tray.hide()
        for dialog in list(self.search_windows.values()):dialog.stop();dialog.hide()
        self.search_windows.clear();self.arc.close();QApplication.quit()
    def closeEvent(self,event):
        if self.closing:
            self.stop_summary()
            if self.worker:event.ignore()
            else:event.accept()
            return
        event.ignore()
        box=QMessageBox(self);box.setWindowTitle('退出 QQ聊天记录管理器');box.setText('选择关闭程序，或隐藏到任务栏通知区域。')
        close=box.addButton('关闭程序',QMessageBox.AcceptRole);hide=box.addButton('隐藏到任务栏',QMessageBox.ActionRole);box.addButton('取消',QMessageBox.RejectRole);box.exec()
        if box.clickedButton()==close:self.request_exit()
        elif box.clickedButton()==hide:
            self.hide()
            for w in self.chat_windows.windows:w.hide()

    def open_chat_tab(self,cid):
        self.chat_tabs.blockSignals(True)
        while self.chat_tabs.count():self.chat_tabs.removeTab(0)
        index=next((i for i in range(self.chat_tabs.count()) if self.chat_tabs.tabData(i)==cid),-1)
        if index<0:
            if self.chat_tabs.count()==1 and self.chat_tabs.tabData(0) is None:self.chat_tabs.removeTab(0)
            c=self.arc.conversation(cid);index=self.chat_tabs.addTab(conversation_title(c));self.chat_tabs.setTabData(index,cid)
            close=AnimatedButton();close.setIcon(glyph('close'));close.setObjectName('tabClose');close.setFixedSize(24,24);close.setToolTip('关闭标签')
            close.clicked.connect(lambda _,c=cid:self.close_chat_id(c));self.chat_tabs.setTabButton(index,QTabBar.RightSide,close)
        self.chat_tabs.setCurrentIndex(index);self.chat_tabs.blockSignals(False)
    def switch_chat_tab(self,index):
        cid=self.chat_tabs.tabData(index) if index>=0 else None
        if not cid:return
        item=QTreeWidgetItem();item.setData(0,Qt.UserRole,cid);self.select_conversation(item)
    def close_chat_tab(self,index):
        self.chat_tabs.removeTab(index)
        if not self.chat_tabs.count():
            self.current=None;self.model.replace([],{});self.chat_title.setText('选择一个会话');self.chat_subtitle.setText('从左侧选择会话重新打开');self.chat_tabs.addTab('聊天记录');self.chat_tabs.setTabData(0,None)
    def close_chat_id(self,cid):
        index=next((i for i in range(self.chat_tabs.count()) if self.chat_tabs.tabData(i)==cid),-1)
        if index>=0:self.close_chat_tab(index)

