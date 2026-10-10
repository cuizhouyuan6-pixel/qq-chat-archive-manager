"""QQ-style search with visible multi-value conditions and cancellable local reads."""
from PySide6.QtCore import Qt,QTimer,QPropertyAnimation,QEasingCurve,QDateTime,QSize
from PySide6.QtGui import QIcon,QFontMetrics
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,QApplication,
                              QFrame,QCheckBox,QAbstractItemView,QScrollArea,QStyle,QStyleOptionViewItem,QStyledItemDelegate)
from .smooth_scroll import SmoothScroll
from .chrome import FramedDialog,AnimatedButton,fade_in
from .background import ReadJob
from .store import SearchQuery,conversation_title
from .date_widgets import DateTimeField,stamp
from .design import motion_ms
from .session_delegate import SessionDelegate,SenderDelegate,ROW,SENDER,session_data,avatar_pixmap
from dataclasses import replace
from .search_delegate import SearchResultDelegate,PREVIEW

CHOSEN=Qt.UserRole+34

class ChosenDelegate(QStyledItemDelegate):
    """Chosen sender entries keep their full text as item data. Only the drawn
    nickname is shortened, so the QQ number and "×" always stay visible, and no
    entry is wider than the list, so it never scrolls sideways."""
    def sizeHint(self,option,index):
        hint=super().sizeHint(option,index);return QSize(min(hint.width(),self.parent().viewport().width()),hint.height())
    def shown_text(self,option,index):
        o=QStyleOptionViewItem(option);QStyledItemDelegate.initStyleOption(self,o,index)
        widget=o.widget;style=widget.style() if widget else QApplication.style()
        room=style.subElementRect(QStyle.SE_ItemViewItemText,o,widget).width()-2*(style.pixelMetric(QStyle.PM_FocusFrameHMargin,None,widget)+1)
        metrics=QFontMetrics(o.font);name,uin=index.data(CHOSEN) or (o.text,'')
        if metrics.horizontalAdvance(o.text)<=room or name==uin:return o.text
        suffix=o.text[len(name):]
        return metrics.elidedText(name,Qt.ElideRight,max(metrics.horizontalAdvance('…'),room-metrics.horizontalAdvance(suffix)))+suffix
    def paint(self,painter,option,index):
        o=QStyleOptionViewItem(option);self.initStyleOption(o,index);o.text=self.shown_text(option,index)
        widget=o.widget;(widget.style() if widget else QApplication.style()).drawControl(QStyle.CE_ItemViewItem,o,painter,widget)

def roster(a,cid,term):
    """Sender roster of one conversation with that account's cached avatars."""
    rows=a.senders(cid,term,100);conv=a.conversation(cid) if cid is not None else None
    for r in rows:
        found=a.profile(conv['owner'],'private',r['sender_uin'],r.get('uid','')) if conv else {}
        r['avatar']=str(a.resolve_media_path(found['avatar_path'])) if found.get('avatar_path') else ''
    return dict(rows=rows,cid=cid)

class SearchWindow(FramedDialog):
    def __init__(self,reader,initial_types=(),*,global_mode=False,keyword='',senders=None,sender_meta=None):
        super().__init__(None);self.fixed_body=True;self.reader=reader;self.host=reader.archive_host;self.global_mode=global_mode;self.modeless=True
        self.setWindowFlag(Qt.Window,True);self.setWindowModality(Qt.NonModal);self.setModal(False);self.setAttribute(Qt.WA_DeleteOnClose)
        if reader is not self.host:reader.destroyed.connect(lambda *_:setattr(self,'reader',self.host))
        self.cid=reader.current['id'] if reader.current and not global_mode else None;self.offset=0;self.chosen_senders=dict(senders or {});self.terms=[];self.rows=[]
        # Per chosen QQ: origin 'roster' (ticked, carries its conversation) or 'manual'
        # (typed); owner/avatar are filled once a profile is found for a manual entry.
        self.sender_meta={u:dict((sender_meta or {}).get(u) or {'origin':'manual'}) for u in self.chosen_senders};self.roster_cid=None
        self.setWindowTitle('搜索全部聊天记录' if global_mode else (conversation_title(reader.current) if reader.current else '搜索聊天记录'));self.resize(1180 if global_mode else 1040,780)
        self.setMinimumSize(720,580);l=QVBoxLayout(self);l.setContentsMargins(24,14,24,22);l.setSpacing(14)
        top=QHBoxLayout();self.keyword=QLineEdit();self.keyword.setPlaceholderText('搜索消息内容；可添加多个条件');self.keyword.setText(keyword)
        add=AnimatedButton('添加条件');query=AnimatedButton('搜索');query.setObjectName('primary');top.addWidget(self.keyword,1);top.addWidget(add);top.addWidget(query);l.addLayout(top)
        self.term_list=QListWidget();self.term_list.setFlow(QListWidget.LeftToRight);self.term_list.setWrapping(True);self.term_list.setMaximumHeight(70);self.term_list.setToolTip('单击条件即可移除');self.term_list.hide();l.addWidget(self.term_list)
        self.term_list.itemClicked.connect(self.remove_term);self.keyword.returnPressed.connect(self.search);add.clicked.connect(self.add_term);query.clicked.connect(self.search)
        tabs=QHBoxLayout();self.type_buttons={}
        for title,kinds in [('全部',()),('图片 / 视频',('image','video')),('表情',('face','mface')),('文件',('file',)),('链接',('link',))]:
            b=AnimatedButton(title);b.setCheckable(True);b.setObjectName('searchCategory');b.setChecked(bool(set(kinds)&set(initial_types)) if kinds else not initial_types)
            b.clicked.connect(lambda checked=False,k=kinds:self.select_type(k));tabs.addWidget(b);self.type_buttons[kinds]=b
        tabs.addStretch();toggle=AnimatedButton('筛选');toggle.clicked.connect(self.toggle_filters);tabs.addWidget(toggle);l.addLayout(tabs)
        body=QHBoxLayout();self.groups=QListWidget();self.groups.setFixedWidth(264);self.groups.setMouseTracking(True);self.groups.setItemDelegate(SessionDelegate(self.groups))
        self.groups.currentItemChanged.connect(self.group_selected);self.groups.setVisible(global_mode);body.addWidget(self.groups)
        self.results=QListWidget();self.results.setWordWrap(True);self.results.setObjectName('searchResults');self.results.setSpacing(4);self.results.setMouseTracking(True);self.results.message_font_size=self.host.cfg.chat_font_size;self.results.setItemDelegate(SearchResultDelegate(self.results))
        # Sender-only results must say so; an empty keyword box otherwise looks broken.
        column=QVBoxLayout();column.setSpacing(6);self.mode_label=QLabel();self.mode_label.setObjectName('muted');self.mode_label.setWordWrap(True);self.mode_label.hide()
        column.addWidget(self.mode_label);column.addWidget(self.results,1)
        self.results.itemDoubleClicked.connect(self.jump);body.addLayout(column,1)
        self.filters=QScrollArea();self.filters.setObjectName('filterScroll');self.filters.setFixedWidth(300);self.filters.setWidgetResizable(True);self.filters.setFrameShape(QFrame.NoFrame);self.filters.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.filter_panel=QFrame();self.filter_panel.setObjectName('card');self.filters.setWidget(self.filter_panel)
        f=QVBoxLayout(self.filter_panel);f.setContentsMargins(12,14,12,14);f.setSpacing(8)
        title=QLabel('筛选条件');title.setObjectName('sectionTitle');f.addWidget(title)
        self.dates_enabled=QCheckBox('限定发送时间');f.addWidget(self.dates_enabled)
        self.start=DateTimeField();self.end=DateTimeField()
        for title,field in [('开始时间',self.start),('结束时间',self.end)]:
            label=QLabel(title);label.setObjectName('fieldLabel');f.addWidget(label);f.addWidget(field);field.bind_archive(reader.arc,lambda:self.cid);field.setEnabled(False)
            self.dates_enabled.toggled.connect(field.setEnabled)
        self.dates_enabled.toggled.connect(self.initialize_dates)
        f.addWidget(QLabel('发送人 · 可多选'))
        self.sender_input=QLineEdit();self.sender_input.setPlaceholderText('输入昵称或 QQ 号查找');f.addWidget(self.sender_input)
        self.sender_selected=QListWidget();self.sender_selected.setMaximumHeight(102);self.sender_selected.setToolTip('单击已选发送人即可移除');self.sender_selected.hide();f.addWidget(self.sender_selected)
        self.sender_selected.setIconSize(QSize(22,22));self.sender_selected.setContextMenuPolicy(Qt.CustomContextMenu);self.sender_selected.customContextMenuRequested.connect(self.chosen_menu)
        self.sender_selected.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.sender_selected.setItemDelegate(ChosenDelegate(self.sender_selected))
        self.sender_selected.itemClicked.connect(self.remove_sender)
        self.everywhere=AnimatedButton('在全部聊天中查看此人发言');self.everywhere.clicked.connect(self.search_everywhere);self.everywhere.hide();f.addWidget(self.everywhere)
        self.senders=QListWidget();self.senders.setMinimumHeight(100);self.senders.setMouseTracking(True);self.senders.setItemDelegate(SenderDelegate(self.senders));self.senders.itemChanged.connect(self.check_sender);f.addWidget(self.senders,1)
        self.senders.setContextMenuPolicy(Qt.CustomContextMenu);self.senders.customContextMenuRequested.connect(self.roster_menu)
        self.sender_state=QLabel('正在读取发送人…');self.sender_state.setWordWrap(True);self.sender_state.setObjectName('muted');f.addWidget(self.sender_state)
        clear=AnimatedButton('清空筛选');clear.clicked.connect(self.clear_filters);f.addWidget(clear);body.addWidget(self.filters);self.filters.setVisible(not global_mode);self.filters_hidden=global_mode;l.addLayout(body,1)
        footer=QHBoxLayout();self.state=QLabel('添加多个内容条件时，消息须同时满足全部条件');self.state.setWordWrap(True);self.state.setObjectName('muted');footer.addWidget(self.state,1)
        self.cancel_search=AnimatedButton('取消查询');self.cancel_search.clicked.connect(self.cancel_query);self.cancel_search.hide();footer.addWidget(self.cancel_search)
        self.back=AnimatedButton('上一页');self.back.clicked.connect(lambda:self.search(-1));footer.addWidget(self.back)
        self.more=AnimatedButton('下一页');self.more.clicked.connect(lambda:self.search(True));footer.addWidget(self.more);enter=AnimatedButton('进入聊天');enter.setObjectName('primary');enter.clicked.connect(lambda:self.jump(self.results.currentItem()));footer.addWidget(enter);close=AnimatedButton('关闭');close.clicked.connect(self.reject);footer.addWidget(close);l.addLayout(footer)
        # Default per-item wheel steps jumped whole result cards; use the app's pixel
        # scrolling (as in the conversation lists) for every list in this window.
        self.smooth=[SmoothScroll(view) for view in (self.results,self.groups,self.senders,self.sender_selected)]
        self.read=ReadJob(reader.arc.path,self);self.people=ReadJob(reader.arc.path,self)
        self.read.busy.connect(self.cancel_search.setVisible);self.read.result.connect(self.show_results);self.read.error.connect(lambda s:self.state.setText('查询失败：'+s));self.read.busy.connect(lambda busy:self.more.setEnabled(not busy));self.read.elapsed.connect(lambda elapsed:self.state.setText(f'正在查询聊天记录 · 已用 {elapsed:.1f} 秒'))
        self.people.result.connect(self.show_senders);self.people.error.connect(lambda s:self.sender_state.setText('发送人读取失败：'+s));self.people.elapsed.connect(lambda elapsed:self.sender_state.setText(f'正在查询发送人 · {elapsed:.1f} 秒'))
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(350);self.debounce.timeout.connect(self.load_senders)
        self.sender_input.textChanged.connect(lambda:self.debounce.start());self.sender_input.returnPressed.connect(self.select_typed_sender)
        if self.chosen_senders:
            self.render_senders()
            if self.filters_hidden:self.toggle_filters()
        self.finished.connect(self.stop);self.load_senders();self.search()
    def initialize_dates(self,enabled):
        if enabled and not getattr(self,'dates_initialized',False):
            low,high=self.reader.arc.time_bounds(self.cid)
            if low is not None:
                self.start.setDateTime(QDateTime.fromSecsSinceEpoch(low));self.end.setDateTime(QDateTime.fromSecsSinceEpoch(high));self.dates_initialized=True
    def cancel_query(self):
        self.read.cancel.set();self.read.pending=None;self.cancel_search.hide();self.state.setText('查询已取消，当前结果保留')
    def stop(self,*_):self.debounce.stop();self.read.stop();self.people.stop()
    def add_term(self):
        value=self.keyword.text().strip()
        if value and value not in self.terms:self.terms.append(value)
        self.keyword.clear();self.render_terms()
    def render_terms(self):
        self.term_list.clear();self.term_list.addItems([v+'  ×' for v in self.terms]);self.term_list.setVisible(bool(self.terms))
    def remove_term(self,item):self.terms.pop(self.term_list.row(item));self.render_terms()
    def select_type(self,kinds):
        if not kinds:
            for key,b in self.type_buttons.items():b.setChecked(not key)
        else:self.type_buttons[()].setChecked(not any(b.isChecked() for key,b in self.type_buttons.items() if key))
        self.search()
    def selected_types(self):return tuple(k for kinds,b in self.type_buttons.items() if kinds and b.isChecked() for k in kinds)
    def toggle_filters(self):
        # Reveal a full-width, already-laid-out panel; never animate its text width.
        self.filters_hidden=not self.filters_hidden;self.filters.setVisible(not self.filters_hidden)
        if not self.filters_hidden:fade_in(self.filter_panel)
    def load_senders(self):
        term=self.sender_input.text().strip();cid=self.cid
        if self.global_mode and cid is None:self.sender_state.setText('选择左侧聊天后可勾选发送人，也可输入完整 QQ 号');return
        self.people.submit(lambda a:roster(a,cid,term))
    def show_senders(self,result):
        rows=result['rows'];self.roster_cid=result['cid'];self.senders.blockSignals(True);self.senders.clear()
        for row in rows:
            uin=row['sender_uin'];name=row['card'] or row['nickname'] or uin;item=QListWidgetItem(name+'\n'+uin);item.setData(Qt.UserRole,(uin,name))
            item.setData(SENDER,dict(name=name,uin=uin,uid=row.get('uid',''),nickname=row['nickname'],card=row['card'],avatar=row.get('avatar','')))
            item.setCheckState(Qt.Checked if uin in self.chosen_senders else Qt.Unchecked);self.senders.addItem(item)
        self.senders.blockSignals(False);self.sender_state.setText(f'显示 {len(rows)} 位 · 已选 {len(self.chosen_senders)} 位'+('，可输入缩小范围' if len(rows)==100 else ''))
    # Choosing or removing a sender searches at once; the keyword box may stay empty.
    def check_sender(self,item):
        uin,name=item.data(Qt.UserRole);d=item.data(SENDER) or {}
        if item.checkState()==Qt.Checked:
            self.chosen_senders[uin]=name;self.sender_meta[uin]=dict(origin='roster',conversation_id=self.roster_cid,uid=d.get('uid',''),avatar=d.get('avatar',''),nickname=d.get('nickname',''),card=d.get('card',''))
        else:self.chosen_senders.pop(uin,None);self.sender_meta.pop(uin,None)
        self.render_senders();self.search()
    def render_senders(self):
        self.sender_selected.clear();ratio=self.devicePixelRatioF()
        for uin,name in self.chosen_senders.items():
            meta=self.sender_meta.get(uin,{});full=f'{name} · {uin}' if name!=uin else uin
            item=QListWidgetItem(full+'  ×');item.setData(Qt.UserRole,uin)
            item.setIcon(QIcon(avatar_pixmap(meta.get('avatar',''),name,22,ratio)))
            if meta.get('origin')=='manual':item.setToolTip(full+'\n'+(f"资料来自归档账号 {meta['owner']} · 单击移除" if meta.get('owner') else '手动添加的 QQ 号，未找到资料 · 单击移除'))
            else:item.setToolTip(full+'\n单击移除')
            item.setData(CHOSEN,(name,uin));self.sender_selected.addItem(item)
        self.sender_selected.setVisible(bool(self.chosen_senders));self.sender_state.setText(f'已选择 {len(self.chosen_senders)} 位发送人')
        self.everywhere.setVisible(bool(self.chosen_senders) and not self.global_mode)
    def chosen_display(self,uin):
        """The text actually drawn for a chosen entry at its current width."""
        view=self.sender_selected;item=next(view.item(i) for i in range(view.count()) if view.item(i).data(Qt.UserRole)==uin)
        opt=QStyleOptionViewItem();opt.initFrom(view.viewport());opt.widget=view;opt.font=view.font();opt.rect=view.visualItemRect(item);opt.decorationSize=view.iconSize()
        return view.itemDelegate().shown_text(opt,view.indexFromItem(item))
    def remove_sender(self,item):
        uin=item.data(Qt.UserRole);self.chosen_senders.pop(uin,None);self.sender_meta.pop(uin,None);self.render_senders()
        self.senders.blockSignals(True)
        for i in range(self.senders.count()):
            it=self.senders.item(i);it.setCheckState(Qt.Checked if it.data(Qt.UserRole)[0] in self.chosen_senders else Qt.Unchecked)
        self.senders.blockSignals(False);self.search()
    def select_typed_sender(self):
        # Any complete QQ number becomes a chosen entry, including people outside
        # this roster; the field clears so further numbers can be added.
        value=self.sender_input.text().strip()
        if value.isdigit():
            if value not in self.chosen_senders:self.chosen_senders[value]=value;self.sender_meta[value]={'origin':'manual'}
            self.render_senders();self.sender_input.clear();self.search()
        elif self.senders.count()==1:self.senders.item(0).setCheckState(Qt.Checked)
        else:self.sender_state.setText('请从匹配列表勾选，或输入完整 QQ 号后按 Enter')
    def sender_label(self):
        names=[uin if name==uin else f'{name}（{uin}）' for uin,name in self.chosen_senders.items()]
        return 'QQ '+'、'.join(names[:3])+(f' 等 {len(names)} 人' if len(names)>3 else '')
    def menu_reader(self):return self.host if self.global_mode else self.reader
    def roster_menu(self,pos):
        # Roster rows always belong to the conversation the roster was read from.
        item=self.senders.itemAt(pos);d=item.data(SENDER) if item else None
        if not d:return None
        from .chat_tools import avatar_menu
        message=dict(conversation_id=self.roster_cid,sender_uin=d['uin'],sender_uid=d.get('uid',''),sender_name=d.get('nickname',''),sender_card=d.get('card',''),is_self=False)
        self.context_menu=avatar_menu(self.menu_reader(),message,self.senders.viewport().mapToGlobal(pos));return self.context_menu
    def chosen_menu(self,pos):
        # Rule for chosen entries: a QQ number typed by the user is only a search
        # condition, not archived identity data, so it offers just "复制 QQ 号".
        # Once a profile is found it is a full user entry and also offers
        # "查看详细信息" for that profile's account (no conversation: last message
        # hidden). Entries ticked in the roster keep their conversation.
        item=self.sender_selected.itemAt(pos)
        if not item:return None
        from .chat_tools import avatar_menu
        uin=item.data(Qt.UserRole);meta=self.sender_meta.get(uin,{});name=self.chosen_senders.get(uin,uin)
        message=dict(conversation_id=meta.get('conversation_id') if meta.get('origin')=='roster' else None,sender_uin=uin,sender_uid=meta.get('uid',''),
                     sender_name=meta.get('nickname') or name,sender_card=meta.get('card',''),is_self=False)
        full=meta.get('origin')=='roster' or bool(meta.get('owner'))
        self.context_menu=avatar_menu(self.menu_reader(),message,self.sender_selected.viewport().mapToGlobal(pos),owner=meta.get('owner'),details=full)
        return self.context_menu
    def search_everywhere(self):
        if self.chosen_senders:self.host.global_search(senders=dict(self.chosen_senders),sender_meta={u:dict(m) for u,m in self.sender_meta.items()})
    def show_sender_history(self,senders,sender_meta=None):
        """Reset content conditions and list these senders' messages across all chats."""
        self.terms.clear();self.keyword.clear();self.render_terms();self.dates_enabled.setChecked(False)
        for key,b in self.type_buttons.items():b.setChecked(not key)
        self.chosen_senders=dict(senders);self.sender_meta={u:dict((sender_meta or {}).get(u) or {'origin':'manual'}) for u in self.chosen_senders};self.render_senders()
        if self.filters_hidden:self.toggle_filters()
        self.search()
    def clear_filters(self):
        self.terms.clear();self.chosen_senders.clear();self.sender_meta.clear();self.keyword.clear();self.sender_input.clear();self.dates_enabled.setChecked(False);self.render_terms();self.render_senders();self.select_type(());self.load_senders()
    def search(self,more=False):
        # clicked(bool) is not a paging request; the next-page callback passes True.
        start=end=None
        if self.dates_enabled.isChecked():
            if not self.start.normalize() or not self.end.normalize():self.state.setText('请先修正发送时间');return
            start=self.start._value.toSecsSinceEpoch();end=self.end._value.toSecsSinceEpoch()
            if start>end:self.state.setText('开始时间不能晚于结束时间');return
        signature=(self.keyword.text().strip(),tuple(self.terms),tuple(self.chosen_senders),self.selected_types(),start,end)
        self.offset=max(0,self.offset+(-100 if more==-1 else 100)) if more in (-1,True) and signature==getattr(self,'query_signature',None) else 0
        self.query_signature=signature;self.back.setEnabled(False)
        cid=self.cid
        q=SearchQuery(keyword=self.keyword.text().strip(),keywords=tuple(self.terms),senders=tuple(self.chosen_senders),types=self.selected_types(),start=start,end=end,conversation_id=cid,offset=self.offset,limit=100)
        if self.global_mode and not any((q.keyword,q.keywords,q.senders,q.types,q.start is not None)):
            # clear() emits currentItemChanged with an already deleted previous
            # item; converting it in PySide crashed the process (清空筛选).
            self.rows=[];self.results.clear();self.groups.blockSignals(True);self.groups.clear();self.groups.blockSignals(False);self.group_rows=None
            self.mode_label.hide();self.state.setText('输入消息内容后按 Enter，或选择筛选条件搜索全部聊天');return
        self.result_flags=(bool(q.senders) and not (q.keyword or q.keywords or q.types),start is not None)
        rebuild=self.global_mode and (not more or not getattr(self,'group_rows',None))
        # Typed QQ numbers still lacking a profile; account order follows the agreed
        # priority: selected conversation, accounts where this person appears in the
        # results, then every other archived account (Archive.sender_profile).
        pending=[u for u,m in self.sender_meta.items() if m.get('origin')=='manual' and not m.get('owner')]
        known_owners=[c['owner'] for c in getattr(self,'group_rows',None) or []]
        def query(a):
            groups=a.search_conversations(replace(q,conversation_id=None,offset=0)) if rebuild else None
            target=cid
            if groups is not None:
                target=cid if any(g['id']==cid for g in groups) else (groups[0]['id'] if groups else None)
            rows=a.search(replace(q,conversation_id=target)) if not self.global_mode or target is not None else []
            profiles={}
            if pending:
                conv=a.conversation(target) if target is not None else None;group_owners=[g['owner'] for g in groups] if groups is not None else known_owners
                for uin in pending:
                    mine=[r for r in rows if r['sender_uin']==uin]
                    found=a.sender_profile(uin,[conv['owner'] if conv else '',*(r['owner'] for r in mine),*group_owners],next((r['sender_uid'] for r in mine if r.get('sender_uid')),''))
                    if found:profiles[uin]=dict(found,avatar=str(a.resolve_media_path(found['avatar_path'])) if found.get('avatar_path') else '')
            return dict(rows=rows,media=a.media_for_messages([r['id'] for r in rows]),groups=groups,cid=target,profiles=profiles)
        self.read.submit(query)
    def group_selected(self,item,previous=None):
        if not item:return
        self.cid=item.data(Qt.UserRole);self.offset=0;self.dates_initialized=False;self.load_senders()
        self.search(more=2)
    def show_results(self,result):
        rows,media=result['rows'],result['media'];self.rows=rows;self.results.clear()
        if result.get('groups') is not None:
            self.group_rows=result['groups'];self.cid=result['cid'];self.groups.blockSignals(True);self.groups.clear()
            for c in self.group_rows:
                item=QListWidgetItem(conversation_title(c));item.setData(Qt.UserRole,c['id']);data=session_data(self.reader.arc,c)
                data['subtitle']=f"{c['matches']:,} 条相关聊天记录";item.setData(ROW,data);self.groups.addItem(item)
                if c['id']==self.cid:self.groups.setCurrentItem(item)
            self.groups.blockSignals(False);self.load_senders()
        profile_cache={}
        for m in rows:
            identity=(m['owner'],m['sender_uin'],m.get('sender_uid',''))
            if identity not in profile_cache:profile_cache[identity]=self.reader.arc.profile(identity[0],'private',identity[1],identity[2])
            profile=profile_cache[identity];avatar=profile.get('avatar_path','')
            avatar=str(self.reader.arc.resolve_media_path(avatar)) if avatar else ''
            name=m['sender_card'] or m['sender_name'] or m['sender_uin'] or '未知发送者'
            item=QListWidgetItem(f"{stamp(m['time'])}   {name}\n{m['text'][:700] or '[无文本消息]'}");item.setData(Qt.UserRole,m);self.results.addItem(item)
            images=[p for p in media.get(m['id'],{}).values() if p.get('kind')=='image' and p.get('abs_path')]
            item.setData(PREVIEW,dict(id=m['id'],name=name,stamp=stamp(m['time']),text=m['text'],avatar=avatar,image=images[0]['abs_path'] if images else '',terms=[*self.terms,*self.keyword.text().split()]))
        upgraded=False
        for uin,found in result.get('profiles',{}).items():
            meta=self.sender_meta.get(uin)
            # Only typed entries still in the selection are upgraded, in place.
            if meta is None or meta.get('origin')!='manual' or meta.get('owner'):continue
            meta.update(owner=found['owner'],avatar=found['avatar'],uid=found.get('uid',''),nickname=found.get('name',''))
            self.chosen_senders[uin]=found.get('name') or uin;upgraded=True
        if upgraded:self.render_senders()
        sender_only,dated=getattr(self,'result_flags',(False,False))
        mode=((f'正在显示 {self.sender_label()} 的全部发言，不限会话' if self.global_mode else f'正在显示 {self.sender_label()} 在本会话的发言')+(' · 已限定发送时间' if dated else '')) if sender_only and self.chosen_senders else None
        if mode and self.global_mode and getattr(self,'group_rows',None):
            mode+=f" · 共 {sum(c['matches'] for c in self.group_rows):,} 条，分布在 {len(self.group_rows)} 个聊天，左侧切换"
        self.mode_label.setText(mode or '');self.mode_label.setVisible(bool(mode))
        empty='没有符合全部条件的记录，请调整筛选'
        if mode and not self.global_mode:empty='此人在本会话没有符合条件的发言，可点击“在全部聊天中查看此人发言”'
        self.back.setEnabled(self.offset>0);self.more.setEnabled(len(rows)==100);self.state.setText(f'第 {self.offset//100+1} 页 · {len(rows)} 条记录 · 双击定位原消息' if rows else empty);fade_in(self.results)
    def jump(self,item):
        if item is None:return
        message=item.data(Qt.UserRole)
        if not message:return
        reader=self.host if self.global_mode else self.reader
        reader.open_message(message)
        if reader is self.host:self.host.navigate(0)
        reader.window().show();reader.window().raise_();reader.window().activateWindow()
        self.state.setText('已定位原消息；搜索条件和结果已保留，可继续查看其他结果')
