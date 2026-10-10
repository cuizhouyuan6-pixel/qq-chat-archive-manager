"""Original uniform line SVG icons rendered by Qt."""
from PySide6.QtCore import QByteArray,Qt
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtGui import QPixmap,QPainter,QIcon
PATHS={
 'activity':'<path d="M3 19h18M3 19V5m1 8 4-4 4 6 4-10 4 7"/>',
 'calendar':'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M3 10h18M7 3v4M17 3v4M7 14h2M12 14h2M7 18h2"/>',
 'search':'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
 'image':'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="2"/><path d="m4 18 6-6 4 4 3-3 4 5"/>',
 'file':'<path d="M4 6h6l2 3h8v11H4Z"/>',
 'clock':'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
 'info':'<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v1"/>',
 'latest':'<path d="M5 4v14h14M8 9l4 4 4-4M12 3v10"/>',
 'filter':'<path d="M3 4h18l-7 8v7l-4 2v-9Z"/>',
 'expand':'<path d="m6 9 6 6 6-6"/>',
 'collapse':'<path d="m9 6 6 6-6 6"/>',
 'chat':'<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3H3V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/>',
 'import':'<path d="M4 14v5h16v-5M12 3v11m-5-5 5 5 5-5"/>',
 'phone':'<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4M10 5h4"/>',
 'export':'<path d="M4 14v5h16v-5M12 14V3m-5 5 5-5 5 5"/>',
 'library':'<path d="M4 4h5v16H4zM11 4h4v16h-4zM17 4l4 1-1 15-4-1Z"/>',
 'settings':'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="white"/><circle cx="15" cy="12" r="2" fill="white"/><circle cx="8" cy="18" r="2" fill="white"/>'}
def line_icon(name):
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><g fill="none" stroke="#45474e" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</g></svg>'
    p=QPixmap(24,24);p.fill(Qt.transparent);paint=QPainter(p);QSvgRenderer(QByteArray(svg.encode())).render(paint);paint.end();return QIcon(p)
