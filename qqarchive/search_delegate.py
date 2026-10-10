"""Compact message previews with sender avatars and literal keyword emphasis."""
import html,re
from PySide6.QtCore import Qt,QSize,QRectF
from PySide6.QtGui import QColor,QFont,QFontMetrics,QPainter,QPainterPath,QTextDocument,QImageReader,QPixmap
from PySide6.QtWidgets import QStyledItemDelegate,QStyle
PREVIEW=Qt.UserRole+32

class SearchResultDelegate(QStyledItemDelegate):
    def __init__(self,view):super().__init__(view);self.view=view;self.images={};self.documents={}
    def picture(self,path):
        if not path:return QPixmap()
        if path not in self.images:
            if len(self.images)>180:self.images.clear()
            reader=QImageReader(path);reader.setAutoTransform(True);size=reader.size()
            if size.isValid():reader.setScaledSize(size.scaled(180,140,Qt.KeepAspectRatio))
            self.images[path]=QPixmap.fromImage(reader.read())
        return self.images[path]
    def document(self,d,width):
        size=getattr(self.view,'message_font_size',14);key=(d['id'],d['text'],tuple(d['terms']),width,size)
        if key not in self.documents:
            if len(self.documents)>220:self.documents.clear()
            font=QFont('Microsoft YaHei UI');font.setPixelSize(size);doc=QTextDocument();doc.setDefaultFont(font);doc.setDocumentMargin(0)
            text=d['text'][:1200] or '[无文本消息]';terms=sorted({t for t in d['terms'] if t},key=len,reverse=True)
            if terms:
                pattern=re.compile('|'.join(re.escape(t) for t in terms),re.I);parts=[];last=0
                for match in pattern.finditer(text):
                    parts.extend([html.escape(text[last:match.start()]),'<b style="color:#202126;background:#E9ECF0">'+html.escape(match.group())+'</b>']);last=match.end()
                parts.append(html.escape(text[last:]));body=''.join(parts)
            else:body=html.escape(text)
            doc.setHtml('<span style="color:#202126">'+body.replace('\n','<br>')+'</span>');doc.setTextWidth(max(60,width));self.documents[key]=doc
        return self.documents[key]
    def sizeHint(self,opt,index):
        d=index.data(PREVIEW)
        if not d:return super().sizeHint(opt,index)
        width=max(140,self.view.viewport().width()-88);doc=self.document(d,width);image=self.picture(d.get('image'))
        return QSize(220,round(54+min(190,doc.size().height())+(image.height()+10 if not image.isNull() else 0)+18))
    def paint(self,p,opt,index):
        d=index.data(PREVIEW)
        if not d:return super().paint(p,opt,index)
        p.save();p.setRenderHint(QPainter.Antialiasing);rect=QRectF(opt.rect);p.setPen(Qt.NoPen)
        p.setBrush(QColor('#E9ECF0' if opt.state&QStyle.State_Selected else '#F4F5F7' if opt.state&QStyle.State_MouseOver else '#FFFFFF'));p.drawRoundedRect(rect,9,9)
        ar=QRectF(rect.left()+14,rect.top()+18,34,34);clip=QPainterPath();clip.addRoundedRect(ar,10,10)
        p.setBrush(QColor('#E4E7EB'));p.drawPath(clip);avatar=self.picture(d.get('avatar'))
        if not avatar.isNull():p.save();p.setClipPath(clip);p.drawPixmap(ar.toRect(),avatar);p.restore()
        else:p.setPen(QColor('#6E7480'));p.drawText(ar,Qt.AlignCenter,d['name'][:1])
        x=rect.left()+62;width=max(60,rect.width()-80);font=QFont('Microsoft YaHei UI');font.setPixelSize(12);p.setFont(font);p.setPen(QColor('#6E7480'))
        p.drawText(QRectF(x,rect.top()+16,width,20),Qt.AlignVCenter,QFontMetrics(font).elidedText(d['name']+'  ·  '+d['stamp'],Qt.ElideRight,int(width)))
        doc=self.document(d,width);height=min(190,doc.size().height());p.save();p.translate(x,rect.top()+44);p.setClipRect(QRectF(0,0,width,height));doc.drawContents(p);p.restore()
        image=self.picture(d.get('image'))
        if not image.isNull():p.drawPixmap(int(x),int(rect.top()+54+height),image)
        p.setPen(QColor('#E9EAED'));p.drawLine(int(x),opt.rect.bottom(),opt.rect.right()-12,opt.rect.bottom());p.restore()
