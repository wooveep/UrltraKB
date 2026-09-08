"""Small vector navigation symbols; the approved brand artwork stays separate."""

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS = {
    "概览": '<path d="M3 11 12 3l9 8M5 10v11h5v-7h4v7h5V10"/>',
    "资料": '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>',
    "知识": '<path d="M12 5C9 3 5 3 3 4v16c3-1 6-1 9 1 3-2 6-2 9-1V4c-2-1-6-1-9 1v16"/>',
    "对话": '<path d="M4 4h16v12H9l-5 4zM8 8h8M8 12h5"/>',
    "产物": '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
    "任务": '<path d="m3 6 2 2 3-4m-5 9 2 2 3-4m-5 9 2 2 3-4M12 6h9M12 13h9M12 20h9"/>',
    "设置": '<path d="M3 6h6m4 0h8M3 12h12m4 0h2M3 18h3m4 0h11"/>'
    '<circle cx="11" cy="6" r="2"/><circle cx="17" cy="12" r="2"/>'
    '<circle cx="8" cy="18" r="2"/>',
}


def navigation_icon(name, dark=False):
    color = "#b3c3d6" if dark else "#536b83"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    icon = QIcon()
    from PySide6.QtCore import Qt

    for size in (20, 40, 60, 80):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
