"""Display-only grouping. Message ownership and conversation IDs never change."""
from functools import cmp_to_key
from PySide6.QtCore import QCollator,QLocale,Qt,QPoint
from PySide6.QtGui import QColor,QPen,QPolygon
from PySide6.QtWidgets import QComboBox,QStylePainter,QStyleOptionComboBox,QStyle
from .store import conversation_title

GROUP_ROLE=int(Qt.UserRole)+20
SORTS=[('手动顺序','manual'),('记录数量 · 多到少','count'),('消息时间 · 新到旧','newest'),('消息时间 · 旧到新','oldest'),('名称 · 字母顺序','name')]

class SessionSort(QComboBox):
    """Draw the label explicitly so narrow/DPI-scaled rails never hide it."""
    def __init__(self):
        super().__init__();self.setMinimumHeight(40)
        self.setStyleSheet('QComboBox::drop-down{border:0;width:24px;} QComboBox::down-arrow{image:none;}')
    def paintEvent(self,event):
        option=QStyleOptionComboBox();self.initStyleOption(option);option.currentText=''
        painter=QStylePainter(self);painter.drawComplexControl(QStyle.CC_ComboBox,option)
        rect=self.rect().adjusted(9,0,-27,0);painter.setPen(QColor('#202126'));painter.setFont(self.font())
        painter.drawText(rect,Qt.AlignLeft|Qt.AlignVCenter,self.fontMetrics().elidedText(self.currentText(),Qt.ElideRight,rect.width()))
        x=self.width()-16;y=self.height()//2;painter.setPen(QPen(QColor('#6E7480'),1.5))
        painter.drawPolyline(QPolygon([QPoint(x-4,y-2),QPoint(x,y+2),QPoint(x+4,y-2)]))

def move_to_group(cfg,cid,name):
    move_many_to_group(cfg,[cid],name)

def move_many_to_group(cfg,cids,name):
    if name and name not in cfg.conversation_groups:raise ValueError('分组不存在')
    ordered=list(dict.fromkeys(cids));chosen=set(ordered)
    for group,ids in cfg.conversation_groups.items():
        cfg.conversation_groups[group]=[cid for cid in ids if cid not in chosen]
    if name:cfg.conversation_groups[name].extend(ordered)

def ordered_groups(conversations,cfg):
    positions={cid:i for i,cid in enumerate(cfg.conversation_order)}
    pins={cid:i for i,cid in enumerate(cfg.pinned_conversations)}
    groups={name:[] for name in cfg.conversation_groups};groups['']=[]
    membership={cid:name for name,ids in cfg.conversation_groups.items() for cid in ids}
    for conv in conversations:groups[membership.get(conv['id'],'')].append(conv)
    collator=QCollator(QLocale('zh_CN'));collator.setNumericMode(True);collator.setCaseSensitivity(Qt.CaseInsensitive)
    def compare(a,b):
        aid,bid=a['id'],b['id']
        if (aid in pins)!=(bid in pins):return -1 if aid in pins else 1
        mode=cfg.conversation_sort
        if mode=='manual' and aid in pins and bid in pins:return pins[aid]-pins[bid]
        if mode=='name':result=collator.compare(conversation_title(a),conversation_title(b))
        else:
            key={'count':lambda c:-c['msg_count'],'newest':lambda c:-(c['last_time'] or 0),
                 'oldest':lambda c:c['last_time'] if c['last_time'] is not None else float('inf'),
                 'manual':lambda c:positions.get(c['id'],len(positions))}.get(mode,lambda c:c['id'])
            av,bv=key(a),key(b);result=(av>bv)-(av<bv)
        return result or (aid>bid)-(aid<bid)
    for items in groups.values():items.sort(key=cmp_to_key(compare))
    return groups
