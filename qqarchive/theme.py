"""Matte white panels, graphite ink, quiet borders, inspired by the reference UI."""
STYLE='''
QWidget{font-family:"Microsoft YaHei";font-size:14px;color:#202126;background:#f8f9fb}
QWidget#windowShell{border:1px solid #d7d9df;background:#f8f9fb}
QWidget#titlebar{background:#f8f9fb;border-bottom:1px solid #e9eaed}
QLabel{background:transparent;border:none}QLabel#windowTitle{font-size:12px;color:#747780}
QLabel#title{font-size:27px;font-weight:600;letter-spacing:.3px}QLabel#muted{color:#868990;font-size:12px}
QFrame#rail{background:#f8f9fb;border:0;border-right:1px solid #e9eaed}
QWidget#brandRow{background:transparent;border:0}QLabel#brandName{font-size:16px;font-weight:600;background:transparent}
QFrame#sessions{background:#fff;border:0;border-right:1px solid #e7e9ed}
QFrame#details{background:#fff;border:0;border-left:1px solid #e7e9ed}
QFrame#card{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #fff,stop:1 #fcfcfd);border:1px solid #e9eaed;border-radius:12px}
QWidget#chatHeader{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #fff,stop:1 #f7f8fa);border-bottom:1px solid #e9ebef}
QPushButton{background:#fff;border:1px solid #dfe2e8;border-radius:9px;padding:8px 13px;min-height:19px}
QPushButton:hover{border-color:#ccd0d8}QPushButton:pressed{background:#e0e3e9}
QPushButton:focus{border-color:#838995}QPushButton:disabled{color:#a5a8b1;background:#f5f6f8;border-color:#e9ebef}
QPushButton#primary{background:#24262c;color:#fff;border-color:#24262c}QPushButton#primary:hover{background:#41444d}
QPushButton#nav{border:1px solid transparent;background:transparent;padding:12px 18px;text-align:left}
QPushButton#nav:hover{background:transparent}QPushButton#nav:checked{background:#f4f5f7;border:1px solid transparent;font-weight:600}
QPushButton#windowControl,QPushButton#windowClose{border:0;background:transparent;padding:0;min-height:0;border-radius:7px}
QPushButton#windowControl:hover,QPushButton#windowControl:checked{background:#e0e3ea}
QPushButton#windowClose:hover{background:#f2d8da}
QLineEdit,QComboBox,QDateTimeEdit,QSpinBox,QTextEdit,QListWidget{background:#fff;border:1px solid #dfe2e8;border-radius:8px;padding:8px;selection-background-color:#dde2eb;selection-color:#202126}
QLineEdit:focus,QTextEdit:focus{border-color:#9aa0ac}
QTreeWidget{background:#fff;border:0;outline:0;padding:4px 0}
QTreeWidget::item{height:43px;padding:5px;border-radius:7px;margin:2px 0}
QTreeWidget::item:hover{background:#f4f5f8}QTreeWidget::item:selected{background:#e9ecf2;color:#202126}
QListView#messages{border:0;background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #f7f8fa,stop:1 #eff1f5);outline:0;padding:10px 8px}
QListWidget::item{padding:9px;border-radius:6px}QListWidget::item:selected{background:#e8ebf1;color:#202126}
QSplitter::handle{background:#e4e7ed;width:1px}
QProgressBar{background:#edf0f4;border:0;border-radius:5px;text-align:center;min-height:10px}
QProgressBar::chunk{background:#484b55;border-radius:5px}
QCheckBox{spacing:8px;background:transparent}QCheckBox::indicator{width:15px;height:15px;border:1px solid #b5bac4;border-radius:4px;background:#fff}
QCheckBox::indicator:checked{background:#30343e;border:2px solid #30343e}
QTextEdit#detailsText{border:0;background:#fff}
QStatusBar{background:#f4f5f8;color:#9397a0;font-size:11px;border-top:1px solid #e8ebef}QStatusBar::item{border:0}
QScrollBar:vertical{background:transparent;width:9px;margin:2px}QScrollBar::handle:vertical{background:#cdd1da;border-radius:3px;min-height:35px}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent}
QTabWidget::pane{border:0;border-top:1px solid #e3e6ec}QTabBar::tab{background:#eef0f4;color:#727783;border:1px solid #e3e6ec;border-bottom:0;border-top-left-radius:8px;border-top-right-radius:8px;padding:10px 18px;margin:5px 2px 0 2px;min-width:90px;max-width:200px}
QTabBar::tab:selected{background:#fff;color:#202126}QTabBar::tab:hover{background:#f9fafc}
QMenu{background:#fff;border:1px solid #dee2e9;padding:6px}QMenu::item{padding:8px 22px;border-radius:5px}QMenu::item:selected{background:#edf0f5}
QToolTip{background:#292d36;color:#fff;border:0;padding:8px}
'''

STYLE += """
QWidget#stepBody{background:transparent}
QPushButton#tabClose{padding:0;border:0;background:transparent;min-height:0}
QPushButton#stepHeader{background:transparent;border:0;text-align:left;font-size:17px;font-weight:600;padding:10px 4px}
QPushButton#stepHeader:checked{color:#202126}
QLabel#sectionTitle{font-size:18px;font-weight:600}QLabel#fieldLabel{font-size:13px;color:#6e7480}
QLabel#timer{font-family:Consolas;font-size:14px;color:#6e7480}
QLabel#breadcrumb{font-size:12px;color:#6e7480;background:#fff;border-bottom:1px solid #e9eaed}
QLabel#selectionSummary{font-size:13px;color:#202126}
QDialog{background:#f8f9fb;border-radius:16px}
QTabBar::tab{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;margin:0;padding:12px 18px}
QTabBar::tab:selected{background:#fff;border-bottom:2px solid #30333b}
QTabBar::tab:hover{background:#f4f5f7}
QDialog#messageDialog{background:#ffffff;border:1px solid #e9eaed;border-radius:15px}
QDialog#messageDialog QWidget#titlebar{background:#ffffff;border:0}
QDialog#windowShell QWidget#titlebar{background:transparent;border:0}
QWidget#dialogBody,QScrollArea#dialogScroll,QWidget#dialogViewport{background:transparent;border:0}
QDialog#messageDialog QLabel#windowTitle{font-size:15px;font-weight:600;color:#202126}
QWidget#messageBody,QWidget#messageFooter,QDialogButtonBox{background:transparent;border:0}
QTextEdit#messageContent{background:transparent;border:0;border-radius:0;padding:0;color:#202126;font-size:14px;selection-background-color:#e5e8ed}
"""

STYLE += '''
QListView#messages{background:#F8F9FB;padding:16px 12px;border:0}
QWidget#chatHeader{background:#FFFFFF;border-bottom:1px solid #E9EAED}
QWidget#chatTools{background:#FFFFFF;border-top:1px solid #E9EAED}
QPushButton#chatTool{padding:4px;border:0;background:transparent;min-height:0;border-radius:8px}
QPushButton#searchCategory{border:0;background:transparent;border-radius:0;padding:8px 10px}
QPushButton#searchCategory:checked{color:#202126;font-weight:600;border-bottom:2px solid #24262C}
QPushButton#calendarDay{font-family:Consolas;padding:2px;background:#FFFFFF;border:1px solid #E9EAED;border-radius:7px;color:#202126}
QPushButton#calendarDay[chosen="true"]{background:#24262C;color:#FFFFFF;border-color:#24262C}
QPushButton#calendarUnavailable{font-family:Consolas;padding:2px;background:transparent;border:1px solid transparent;color:#A5A8B1}
QPushButton#calendarUnavailable:hover,QPushButton#calendarUnavailable:pressed{background:transparent;border-color:transparent}
QListWidget#historyList{border:0;background:transparent;padding:0}
QListWidget#historyList::item{background:#FFFFFF;border:1px solid #E9EAED;border-radius:12px;padding:18px;margin:3px 0}
QListWidget#historyList::item:hover{background:#F4F5F7}
QListWidget#historyList::item:selected{background:#F4F5F7;color:#202126;border-color:#B9BEC8}
QListWidget#searchResults{border:0;background:#FFFFFF;padding:8px}
QListWidget#searchResults::item{padding:16px;border-bottom:1px solid #E9EAED;border-radius:0}
QFrame#taskStrip{background:#FFFFFF;border-top:1px solid #E9EAED}
'''
