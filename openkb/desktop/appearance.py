"""Application-local appearance with system tracking and shared native presentation."""

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from openkb.desktop.reader import MarkdownView


class Appearance(QObject):
    def __init__(self, window, preferences):
        super().__init__(window)
        self.window, self.preferences = window, preferences
        self.app = QApplication.instance()
        self._applying = False
        self.system_dark = self.app.palette().color(QPalette.ColorRole.Window).lightness() < 128
        scheme = self.app.styleHints().colorScheme()
        if scheme != Qt.ColorScheme.Unknown:
            self.system_dark = scheme == Qt.ColorScheme.Dark
        self.dark = self.system_dark
        mode = preferences.value("appearance/theme", "system")
        index = window.theme.findData(mode)
        window.theme.setCurrentIndex(max(0, index))
        self.app.installEventFilter(self)
        self.app.styleHints().colorSchemeChanged.connect(self.system_changed)
        window.theme.currentIndexChanged.connect(self.choose)
        font = QFont("Noto Sans CJK SC")
        font.setPixelSize(14)
        self.app.setFont(font)
        self.apply()

    def choose(self):
        self.preferences.setValue("appearance/theme", self.window.theme.currentData())
        self.preferences.sync()
        self.apply()

    def system_changed(self, scheme):
        self.system_dark = (
            scheme == Qt.ColorScheme.Dark
            if scheme != Qt.ColorScheme.Unknown
            else self.app.style().standardPalette().color(QPalette.ColorRole.Window).lightness()
            < 128
        )
        if self.window.theme.currentData() == "system":
            self.apply()

    def eventFilter(self, watched, event):
        if watched is self.app and event.type() == QEvent.Type.ApplicationPaletteChange:
            if not self._applying:
                self.system_dark = (
                    self.app.palette().color(QPalette.ColorRole.Window).lightness() < 128
                )
                self.apply()
        if event.type() == QEvent.Type.Show and isinstance(watched, MarkdownView):
            watched.set_presentation(dark=self.dark, scale=self.window.zoom.currentData())
        return False

    def apply(self):
        if self._applying:
            return
        self._applying = True
        try:
            mode = self.window.theme.currentData()
            self.dark = self.system_dark if mode == "system" else mode == "dark"
            bg, surface, text, muted, border, accent, selection = (
                ("#101722", "#192230", "#edf2f8", "#a9b7c9", "#324154", "#5fc4ca", "#254459")
                if self.dark
                else ("#f2f6fa", "#ffffff", "#172439", "#53657b", "#d5dfe9", "#296575", "#dceff2")
            )
            palette = QPalette()
            for role, color in (
                ("Window", bg),
                ("Base", surface),
                ("AlternateBase", bg),
                ("WindowText", text),
                ("Text", text),
                ("Button", surface),
                ("ButtonText", text),
                ("ToolTipBase", surface),
                ("ToolTipText", text),
                ("Highlight", selection),
                ("HighlightedText", text),
                ("Link", accent),
                ("PlaceholderText", muted),
            ):
                palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
            for role in ("Text", "WindowText", "ButtonText"):
                palette.setColor(
                    QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor(muted)
                )
            from openkb.desktop.navigation_icons import navigation_icon

            for name, button in self.window.shell.buttons.items():
                button.setIcon(navigation_icon(name, self.dark))
            self.app.setPalette(palette)
            chevron = (Path(__file__).parent / "assets/chevron.svg").as_posix()
            self.app.setStyleSheet(f"""
                QWidget {{ color: {text}; }}
                QMainWindow, QDialog {{ background: {bg}; }}
                QLabel {{ background: transparent; }}
                QLabel#brand {{ font-size: 18px; font-weight: 700; }}
                QLabel#pageTitle {{ font-size: 24px; font-weight: 700; }}
                QLabel#welcomeTitle {{ font-size: 28px; font-weight: 600; margin-top: 24px; }}
                QLabel#overviewStats {{ background: {surface}; border: 1px solid {border};
                    border-radius: 8px; padding: 28px 18px; font-size: 20px; }}
                QFrame#navigation, QFrame#topbar {{ background: {surface}; border: 0;
                    border-right: 1px solid {border}; border-bottom: 1px solid {border}; }}
                QPushButton, QToolButton, QComboBox {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; padding: 7px 10px;
                    min-height: 20px; }}
                QToolButton {{ padding: 6px; }}
                QComboBox {{ padding-right: 24px; }}
                QComboBox::drop-down {{ border: 0; width: 24px; }}
                QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
                QToolButton::menu-indicator {{ image: none; }}
                QFrame#navigation QToolButton {{ border: 1px solid transparent; text-align: left; }}
                QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {selection}; }}
                QToolButton:checked {{ background: {selection}; color: {accent};
                    font-weight: 600; }}
                QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
                QPlainTextEdit:focus, QTreeView:focus, QTableView:focus {{
                    border: 2px solid {accent}; }}
                QWidget:disabled {{ color: {muted}; }}
                QLineEdit, QPlainTextEdit, QTextBrowser, QAbstractItemView {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; padding: 6px;
                    selection-background-color: {selection}; selection-color: {text}; }}
                QLineEdit {{ min-height: 22px; }}
                QAbstractItemView::item {{ padding: 6px; min-height: 22px; }}
                QAbstractItemView::item:selected {{ background: {selection}; color: {text}; }}
                QHeaderView::section {{ background: {bg}; color: {muted};
                    border: 0; border-bottom: 1px solid {border}; padding: 8px; }}
                QTabWidget::pane {{ border: 0; }}
                QTabBar::tab {{ background: transparent; color: {muted}; padding: 9px 14px;
                    border-bottom: 2px solid transparent; }}
                QTabBar::tab:selected {{ color: {accent}; border-bottom: 2px solid {accent}; }}
                QSplitter::handle {{ background: {bg}; width: 8px; height: 8px; }}
                QScrollArea {{ background: transparent; border: 0; }}
                QMenu {{ background: {surface}; border: 1px solid {border}; padding: 6px; }}
                QMenu::item {{ padding: 8px 20px; }}
                QMenu::item:selected {{ background: {selection}; }}
                QToolTip {{ color: {text}; background: {surface};
                    border: 1px solid {border}; padding: 6px; }}
                QStatusBar {{ color: {muted}; background: {bg}; font-size: 12px; }}
            """)
            self.present_readers()
        finally:
            self._applying = False

    def present_readers(self):
        for view in self.window.findChildren(MarkdownView):
            view.set_presentation(dark=self.dark, scale=self.window.zoom.currentData())
