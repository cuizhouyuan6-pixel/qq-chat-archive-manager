"""Nonmodal, bounded batch conversation picker with persistent visible selection."""
from PySide6.QtCore import Qt,QSize,Signal
from PySide6.QtWidgets import (QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QComboBox,
    QListWidget,QListWidgetItem,QAbstractItemView,QSplitter,QWidget)
from .chrome import FramedDialog,AnimatedButton
from .store import conversation_title
from .smooth_scroll import SmoothScroll
from .session_groups import SessionSort

class GroupPicker(FramedDialog):
    applyRequested=Signal(object,str)
    createRequested=Signal()
    def __init__(self,conversations,groups,parent=None,target=''):
        super().__init__(parent);self.fixed_body=True;self.setWindowTitle('批量添加聊天到分组')
        self.resize(800,610);self.setMinimumSize(520,420)
        self.records={c['id']:dict(c) for c in conversations};self.chosen=set();self.groups={}
        layout=QVBoxLayout(self);layout.setContentsMargins(24,16,24,24);layout.setSpacing(12)
        intro=QLabel('选择目标分组，再勾选需要添加的群聊或私聊。原分组中的所选聊天会一同移入。');intro.setWordWrap(True);layout.addWidget(intro)
        target_row=QHBoxLayout();target_label=QLabel('目标分组');self.target=SessionSort();self.target.setAccessibleName('目标分组');target_label.setBuddy(self.target)
        target_row.addWidget(target_label);target_row.addWidget(self.target,1)
        create=AnimatedButton('新建分组');create.clicked.connect(self.createRequested);target_row.addWidget(create);layout.addLayout(target_row)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索名称、群号、QQ 号或归档账号');self.search.setClearButtonEnabled(True);self.search.setAccessibleName('搜索待添加聊天');layout.addWidget(self.search)
        split=QSplitter(Qt.Horizontal);split.setHandleWidth(16);split.setChildrenCollapsible(False)
        split.setStyleSheet('QSplitter::handle{background:transparent;width:16px;}');layout.addWidget(split,1)
        available=QWidget();available.setMinimumWidth(240);al=QVBoxLayout(available);al.setContentsMargins(0,0,0,0)
        tools=QHBoxLayout();self.matches=QLabel();tools.addWidget(self.matches,1)
        self.select_all=AnimatedButton('全选结果');self.select_all.clicked.connect(lambda:self.choose_visible(True));tools.addWidget(self.select_all);al.addLayout(tools)
        self.list=QListWidget();self.list.setSelectionMode(QAbstractItemView.NoSelection);self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.list.setTextElideMode(Qt.ElideRight)
        self.smooth=SmoothScroll(self.list);al.addWidget(self.list,1);split.addWidget(available)
        selected=QWidget();selected.setMinimumWidth(190);sl=QVBoxLayout(selected);sl.setContentsMargins(0,0,0,0)
        summary=QHBoxLayout();self.summary=QLabel('已选 0 项');summary.addWidget(self.summary,1)
        clear=AnimatedButton('清空');clear.clicked.connect(self.clear);summary.addWidget(clear);sl.addLayout(summary)
        self.selected=QListWidget();self.selected.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.selected.setTextElideMode(Qt.ElideRight);self.selected.setAccessibleName('已选聊天，双击移除')
        self.selected_smooth=SmoothScroll(self.selected);self.selected.itemDoubleClicked.connect(self.remove_item);sl.addWidget(self.selected,1)
        remove=AnimatedButton('移除选中项');remove.clicked.connect(lambda:self.remove_item(self.selected.currentItem()));sl.addWidget(remove);split.addWidget(selected);split.setSizes([470,250])
        self.status=QLabel('');self.status.setWordWrap(True);self.status.setObjectName('muted');layout.addWidget(self.status)
        footer=QHBoxLayout();footer.addStretch();cancel=AnimatedButton('取消');cancel.clicked.connect(self.reject);footer.addWidget(cancel)
        self.apply=AnimatedButton('添加到分组');self.apply.setObjectName('primary');self.apply.clicked.connect(self.submit);footer.addWidget(self.apply);layout.addLayout(footer)
        self.items={}
        for cid,c in self.records.items():
            item=QListWidgetItem(self._description(c));item.setToolTip(self._description(c));item.setData(Qt.UserRole,cid);item.setSizeHint(QSize(200,64))
            item.setFlags(Qt.ItemIsEnabled|Qt.ItemIsUserCheckable);item.setCheckState(Qt.Unchecked);self.list.addItem(item);self.items[cid]=item
        self.list.itemChanged.connect(self.selection_changed);self.search.textChanged.connect(self.filter)
        self.target.currentIndexChanged.connect(self.target_changed);self.set_groups(groups,target)
    def _description(self,c):
        return f"{conversation_title(c)}\n{'群聊' if c['kind']=='group' else '私聊'} · {c['peer']} · 账号 {c['owner']}"
    def set_groups(self,groups,target=None):
        old=self.target.currentData() if target is None else target
        self.groups={name:list(ids) for name,ids in groups.items()}
        self.target.blockSignals(True);self.target.clear()
        for name in groups:self.target.addItem(name,name)
        if old in groups:self.target.setCurrentIndex(self.target.findData(old))
        self.target.blockSignals(False);self.target_changed()
    def target_changed(self,*_):
        members=set(self.groups.get(self.target.currentData(),()))
        self.chosen.difference_update(members);self.list.blockSignals(True)
        for cid,item in self.items.items():
            enabled=cid not in members;item.setFlags((Qt.ItemIsEnabled|Qt.ItemIsUserCheckable) if enabled else Qt.NoItemFlags)
            item.setText(self._description(self.records[cid])+(' · 已在此分组' if not enabled else ''))
            item.setCheckState(Qt.Checked if cid in self.chosen else Qt.Unchecked)
        self.list.blockSignals(False);self.filter();self.update_selection()
    def filter(self,*_):
        needle=self.search.text().strip().casefold();count=0
        for cid,item in self.items.items():
            visible=not needle or needle in self._description(self.records[cid]).casefold()
            item.setHidden(not visible);count+=visible
        self.matches.setText(f'聊天 · {count} / {len(self.items)}')
    def choose_visible(self,checked):
        self.list.blockSignals(True)
        for cid,item in self.items.items():
            if not item.isHidden() and item.flags()&Qt.ItemIsEnabled:
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                self.chosen.add(cid) if checked else self.chosen.discard(cid)
        self.list.blockSignals(False);self.update_selection()
    def selection_changed(self,item):
        cid=item.data(Qt.UserRole)
        self.chosen.add(cid) if item.checkState()==Qt.Checked else self.chosen.discard(cid)
        self.update_selection()
    def update_selection(self):
        self.selected.clear()
        for cid in self.records:
            if cid in self.chosen:
                item=QListWidgetItem(self._description(self.records[cid]));item.setToolTip(item.text());item.setData(Qt.UserRole,cid);item.setSizeHint(QSize(180,64));self.selected.addItem(item)
        self.summary.setText(f'已选 {len(self.chosen)} 项');self.apply.setEnabled(bool(self.chosen and self.target.currentData()))
        self.status.setText('搜索不会取消已选聊天，可在右侧查看和移除。' if self.groups else '请先新建一个分组。')
    def remove_item(self,item):
        if item:self.items[item.data(Qt.UserRole)].setCheckState(Qt.Unchecked)
    def clear(self):
        self.list.blockSignals(True)
        for cid in self.chosen:self.items[cid].setCheckState(Qt.Unchecked)
        self.chosen.clear();self.list.blockSignals(False);self.update_selection()
    def submit(self):
        if not self.apply.isEnabled():return
        self.apply.setEnabled(False)
        self.applyRequested.emit([cid for cid in self.records if cid in self.chosen],self.target.currentData())
