import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import Qt,QPoint,QPointF
from PySide6.QtGui import QWheelEvent,QFontMetrics
from PySide6.QtWidgets import QAbstractItemView,QApplication
from PySide6.QtTest import QTest
from qqarchive.search_window import SearchWindow
from qqarchive.smooth_scroll import SmoothScroll
from test_v13_interactions import ui,spin
from test_v18_sender_search import idle,press_enter,chosen_item,add_profile,open_global

def wheel(view,steps=-120):
    point=QPointF(120,120)
    QApplication.sendEvent(view.viewport(),QWheelEvent(point,QPointF(view.viewport().mapToGlobal(point.toPoint())),QPoint(),QPoint(0,steps),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False))

def test_every_search_list_scrolls_by_animated_pixels(ui):
    app,w,t=ui;d=SearchWindow(w);d.show();spin(app,lambda:len(d.rows)==100);idle(app,d)
    for view in (d.results,d.groups,d.senders,d.sender_selected):
        assert view.verticalScrollMode()==QAbstractItemView.ScrollPerPixel
        assert sum(isinstance(c,SmoothScroll) for c in view.children())==1
    bar=d.results.verticalScrollBar();spin(app,lambda:bar.maximum()>400)   # delayed item layout
    bar.setValue(0);wheel(d.results)
    smooth=next(c for c in d.results.children() if isinstance(c,SmoothScroll))
    assert smooth.animation.state()==smooth.animation.State.Running and bar.value()<96
    QTest.qWait(300);assert bar.value()==96
    wheel(d.results);wheel(d.results);QTest.qWait(300);assert bar.value()==3*96
    d.reject();QTest.qWait(230)

def test_long_chosen_name_is_shortened_but_number_and_mark_stay(ui):
    app,w,t=ui;long_name='語り継がれる創造神・語り継がれる創造神・語り継がれる創造神'
    add_profile(w.arc,'1','2',long_name)
    d=open_global(app,w);press_enter(app,d,'2');QTest.qWait(50)
    item=chosen_item(d,'2');shown=d.chosen_display('2');view=d.sender_selected
    assert item.text()==long_name+' · 2  ×' and long_name in item.toolTip()
    prefix=shown.split('…')[0]
    assert shown.endswith(' · 2  ×') and '…' in shown and prefix and long_name.startswith(prefix)
    assert view.horizontalScrollBarPolicy()==Qt.ScrollBarAlwaysOff and view.visualItemRect(item).width()<=view.viewport().width()
    assert QFontMetrics(view.font()).horizontalAdvance(shown)<view.visualItemRect(item).width()
    d.reject();QTest.qWait(230)

def test_short_names_and_numbers_are_drawn_in_full(ui):
    app,w,t=ui;add_profile(w.arc,'1','2','乙')
    d=open_global(app,w);press_enter(app,d,'2');press_enter(app,d,'424242')
    assert chosen_item(d,'2').text()=='乙 · 2  ×' and d.chosen_display('2')=='乙 · 2  ×'
    assert d.chosen_display('424242')=='424242  ×'
    d.reject();QTest.qWait(230)
