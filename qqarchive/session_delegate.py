"""Conversation rows painted without the platform's selected-icon tint."""
from PySide6.QtCore import Qt,QSize,QRect,QRectF
from PySide6.QtGui import QColor,QFont,QFontMetrics,QPainter,QPainterPath,QImageReader,QPixmap
from PySide6.QtWidgets import QStyledItemDelegate,QStyle,QStyleOptionViewItem
from .store import conversation_title

ROW=Qt.UserRole+31
SENDER=Qt.UserRole+33

def draw_avatar(p,rect,pix,title):
    """Shared placeholder rule: rounded #E4E7EB tile with the name's first character."""
    clip=QPainterPath();clip.addRoundedRect(QRectF(rect),10,10)
    p.save();p.setPen(Qt.NoPen);p.setBrush(QColor('#E4E7EB'));p.drawPath(clip)
    if pix is not None and not pix.isNull():p.setClipPath(clip);p.drawPixmap(QRectF(rect).toRect(),pix)
    else:p.setPen(QColor('#6E7480'));p.drawText(QRectF(rect),Qt.AlignCenter,(title or '')[:1])
    p.restore()

def load_avatar(cache,path,size=80):
    if not path:return None
    if path not in cache:
        if len(cache)>256:cache.clear()
        reader=QImageReader(path);reader.setScaledSize(QSize(size,size));cache[path]=QPixmap.fromImage(reader.read())
    return cache[path]

def avatar_pixmap(path,title,size,ratio=1.):
    """A list icon using the same tile as the conversation list."""
    pix=QPixmap(round(size*ratio),round(size*ratio));pix.setDevicePixelRatio(ratio);pix.fill(Qt.transparent)
    p=QPainter(pix);p.setRenderHint(QPainter.Antialiasing);font=QFont('Microsoft YaHei UI');font.setPixelSize(max(9,size//2));p.setFont(font)
    draw_avatar(p,QRectF(0,0,size,size),load_avatar({},path,round(size*ratio*2)),title);p.end();return pix

def session_data(archive,c,pinned=False,closable=False):
    profile=archive.profile(c['owner'],c['kind'],c['peer'],c.get('peer_uid',''))
    path=profile.get('avatar_path','')
    return dict(title=conversation_title(c),subtitle=('群聊' if c['kind']=='group' else '私聊')+f" · {c['msg_count']:,} 条记录",
                avatar=str(archive.resolve_media_path(path)) if path else '',pinned=pinned,closable=closable)

class SessionDelegate(QStyledItemDelegate):
    def __init__(self,parent=None):super().__init__(parent);self.images={}
    def sizeHint(self,option,index):
        return QSize(210,76) if index.data(ROW) else QSize(180,32)
    def paint(self,p,opt,index):
        data=index.data(ROW)
        if not data:
            p.save();font=QFont('Microsoft YaHei UI');font.setPixelSize(12);p.setFont(font);p.setPen(QColor('#6E7480'))
            p.drawText(opt.rect.adjusted(4,0,-4,0),Qt.AlignVCenter,str(index.data() or ''));p.restore();return
        p.save();p.setRenderHint(QPainter.Antialiasing);rect=opt.rect.adjusted(1,2,-2,-2)
        bg='#E9ECF0' if opt.state&QStyle.State_Selected else ('#F4F5F7' if opt.state&QStyle.State_MouseOver else '#FFFFFF')
        p.setPen(Qt.NoPen);p.setBrush(QColor(bg));p.drawRoundedRect(QRectF(rect),9,9)
        ar=QRectF(rect.left()+9,rect.top()+15,40,40)
        draw_avatar(p,ar,load_avatar(self.images,data.get('avatar')),data['title'])
        x=ar.right()+10;space=max(20,int(rect.right()-x-(26 if data.get('closable') else 8)))
        font=QFont('Microsoft YaHei UI');font.setPixelSize(14);p.setFont(font);p.setPen(QColor('#202126'))
        p.drawText(QRectF(x,rect.top()+13,space,24),Qt.AlignVCenter,QFontMetrics(font).elidedText(data['title'],Qt.ElideRight,space))
        font.setPixelSize(12);p.setFont(font);p.setPen(QColor('#6E7480'))
        sub=('置顶 · ' if data.get('pinned') else '')+data.get('subtitle','')
        p.drawText(QRectF(x,rect.top()+39,space,21),Qt.AlignVCenter,QFontMetrics(font).elidedText(sub,Qt.ElideRight,space))
        if data.get('closable'):p.drawText(QRectF(rect.right()-25,rect.top()+23,22,24),Qt.AlignCenter,'×')
        p.setPen(QColor('#E9EAED'));p.drawLine(rect.left()+59,opt.rect.bottom(),rect.right()-8,opt.rect.bottom());p.restore()

class SenderDelegate(QStyledItemDelegate):
    """Search roster rows: check box, avatar, semi-bold name and a smaller grey QQ number.

    The check box is painted at the style's own indicator rectangle, which is the
    rectangle QStyledItemDelegate.editorEvent hit-tests, so ticking is unchanged."""
    def __init__(self,parent=None):super().__init__(parent);self.images={}
    @staticmethod
    def fonts():
        # Microsoft YaHei ships Regular/Bold only: on the Windows platform any weight
        # >=600 already resolves to the Bold face (ink 570 -> 906 px for a sample
        # name, measured 2026-10-10), while the offscreen QA platform keeps 600
        # regular. Bold is named explicitly so QA renders what Windows shows.
        name=QFont('Microsoft YaHei UI');name.setPixelSize(14);name.setWeight(QFont.Weight.Bold)
        uin=QFont('Microsoft YaHei UI');uin.setPixelSize(12);return name,uin
    def sizeHint(self,option,index):
        name,uin=self.fonts();return QSize(200,max(46,QFontMetrics(name).height()+QFontMetrics(uin).height()+16))
    def parts(self,opt,index):
        o=QStyleOptionViewItem(opt);self.initStyleOption(o,index);widget=o.widget
        style=widget.style() if widget else None;rect=o.rect
        check=style.subElementRect(QStyle.SE_ItemViewItemCheckIndicator,o,widget) if style and o.features&QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator else QRect()
        left=check.right()+8 if check.isValid() and not check.isEmpty() else rect.left()+8
        avatar=QRect(left,rect.top()+(rect.height()-32)//2,32,32);x=avatar.right()+11;width=max(10,rect.right()-8-x)
        name,uin=self.fonts();h1=QFontMetrics(name).height();h2=QFontMetrics(uin).height();top=rect.top()+(rect.height()-h1-h2-2)//2
        return dict(option=o,check=check,avatar=avatar,name=QRect(x,top,width,h1),uin=QRect(x,top+h1+2,width,h2))
    def paint(self,p,opt,index):
        d=index.data(SENDER)
        if not d:return super().paint(p,opt,index)
        parts=self.parts(opt,index);o=parts['option'];widget=o.widget
        p.save();p.setRenderHint(QPainter.Antialiasing)
        if o.state&(QStyle.State_Selected|QStyle.State_MouseOver):
            p.setPen(Qt.NoPen);p.setBrush(QColor('#E9ECF0' if o.state&QStyle.State_Selected else '#F4F5F7'));p.drawRoundedRect(QRectF(o.rect).adjusted(1,1,-1,-1),6,6)
        if not parts['check'].isEmpty():
            box=QStyleOptionViewItem(o);box.rect=parts['check'];box.state=(o.state&~QStyle.State_HasFocus)|(QStyle.State_On if o.checkState==Qt.Checked else QStyle.State_Off)
            widget.style().drawPrimitive(QStyle.PE_IndicatorItemViewItemCheck,box,p,widget)
        name,uin=self.fonts();small=QFont(name);small.setWeight(QFont.Weight.Normal);p.setFont(small)
        draw_avatar(p,parts['avatar'],load_avatar(self.images,d.get('avatar')),d['name'])
        p.setFont(name);p.setPen(QColor('#202126'))
        p.drawText(parts['name'],Qt.AlignLeft|Qt.AlignVCenter,QFontMetrics(name).elidedText(d['name'],Qt.ElideRight,parts['name'].width()))
        p.setFont(uin);p.setPen(QColor('#868990'))
        p.drawText(parts['uin'],Qt.AlignLeft|Qt.AlignVCenter,QFontMetrics(uin).elidedText(d['uin'],Qt.ElideRight,parts['uin'].width()));p.restore()
    def problems(self,view):
        """Layout self-check used at 100/150/200 % scale: no overlap, clipping or elision."""
        out=[];name_font,uin_font=self.fonts();fm1=QFontMetrics(name_font);fm2=QFontMetrics(uin_font)
        for row in range(view.count()):
            item=view.item(row);d=item.data(SENDER);index=view.indexFromItem(item)
            if not d:continue
            opt=QStyleOptionViewItem();opt.initFrom(view.viewport());opt.rect=view.visualItemRect(item);opt.widget=view;opt.font=view.font()
            parts=self.parts(opt,index);rect=opt.rect;label=d['uin']
            if rect.height()<self.sizeHint(opt,index).height():out.append(label+': row clipped')
            for key in ('check','avatar','name','uin'):
                if not parts[key].isEmpty() and not rect.contains(parts[key]):out.append(f'{label}: {key} outside row')
            if parts['check'].isEmpty():out.append(label+': no check box')
            if parts['check'].intersects(parts['avatar']) or parts['avatar'].intersects(parts['name']) or parts['avatar'].intersects(parts['uin']) or parts['name'].intersects(parts['uin']):out.append(label+': overlap')
            if parts['name'].height()<fm1.height() or parts['uin'].height()<fm2.height():out.append(label+': text clipped vertically')
            if fm1.horizontalAdvance(d['name'])>parts['name'].width() or fm2.horizontalAdvance(d['uin'])>parts['uin'].width():out.append(label+': text truncated')
        return out
