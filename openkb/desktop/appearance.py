"""Application-local appearance with system tracking and shared native presentation."""

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
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
                ("#202020", "#262626", "#eeeeec", "#a4a4a0", "#3a3a38", "#d8dfd9", "#343634")
                if self.dark
                else ("#ffffff", "#fafaf9", "#262725", "#747671", "#e6e7e3", "#343d36", "#e9ece7")
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
            sidebar = "#181818" if self.dark else "#f5f5f3"
            composer = "#2a2a29" if self.dark else "#f6f6f4"
            inverse = "#202020" if self.dark else "#ffffff"
            self.app.setStyleSheet(f"""
                QWidget {{ color: {text}; }}
                QMainWindow, QDialog, QWidget#workspace {{ background: {bg}; }}
                QLabel {{ background: transparent; }}
                QLabel#brand {{ font-size: 16px; font-weight: 500; }}
                QLabel#pageTitle {{ font-size: 16px; font-weight: 500; }}
                QLabel#welcomeTitle {{ font-size: 32px; font-weight: 500; margin-top: 32px; }}
                QLabel#overviewStats {{ color: {text}; border: 0; padding: 24px 0;
                    font-size: 20px; }}
                QLabel#muted, QLabel#composerHint {{ color: {muted}; font-size: 12px; }}
                QLabel#chatWelcome {{ font-size: 28px; font-weight: 500; }}
                QLabel#drawerTitle {{ font-size: 17px; font-weight: 500; }}
                QFrame#navigation {{ background: {sidebar}; border: 0; }}
                QFrame#topbar {{ background: {bg}; border: 0; }}
                QPushButton, QToolButton, QComboBox {{ background: transparent;
                    border: 1px solid transparent; border-radius: 7px; padding: 7px 10px;
                    min-height: 20px; }}
                QToolButton {{ padding: 6px; }}
                QComboBox {{ padding-right: 24px; }}
                QComboBox::drop-down {{ border: 0; width: 24px; }}
                QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
                QToolButton::menu-indicator {{ image: none; }}
                QFrame#navigation QPushButton {{ text-align: left; padding: 7px 10px; }}
                QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {selection}; }}
                QPushButton:checked, QToolButton:checked {{ background: {selection};
                    color: {text}; font-weight: 500; }}
                QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
                QPlainTextEdit:focus, QTreeView:focus, QTableView:focus {{
                    border: 1px solid {muted}; }}
                QWidget:disabled {{ color: {muted}; }}
                QLineEdit, QPlainTextEdit {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; padding: 8px;
                    selection-background-color: {selection}; selection-color: {text}; }}
                QTextBrowser, QAbstractItemView {{ background: {bg}; border: 0; padding: 6px;
                    selection-background-color: {selection}; selection-color: {text}; }}
                QLineEdit {{ min-height: 22px; }}
                QAbstractItemView::item {{ padding: 8px; min-height: 22px; border: 0; }}
                QAbstractItemView::item:selected {{ background: {selection}; color: {text}; }}
                QAbstractItemView::item:hover {{ background: {surface}; }}
                QHeaderView::section {{ background: {bg}; color: {muted};
                    border: 0; border-bottom: 1px solid {border}; padding: 10px 8px; }}
                QTableView {{ gridline-color: {border}; }}
                QTabWidget::pane {{ border: 0; }}
                QTabBar::tab {{ background: transparent; color: {muted}; padding: 9px 14px;
                    border-bottom: 2px solid transparent; }}
                QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {text}; }}
                QSplitter::handle {{ background: {bg}; width: 12px; height: 12px; }}
                QScrollArea {{ background: transparent; border: 0; }}
                QScrollBar:vertical {{ background: transparent; width: 8px; margin: 3px 1px; }}
                QScrollBar::handle:vertical {{ background: {border}; min-height: 36px;
                    border-radius: 3px; }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
                QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
                QMenu {{ background: {surface}; border: 1px solid {border}; padding: 6px; }}
                QMenu::item {{ padding: 8px 20px; border-radius: 4px; }}
                QMenu::item:selected {{ background: {selection}; }}
                QToolTip {{ color: {text}; background: {surface};
                    border: 1px solid {border}; padding: 6px; }}
                QStatusBar {{ color: {muted}; background: {bg}; font-size: 11px; }}
                QFrame#composer {{ background: {composer}; border: 1px solid {border};
                    border-radius: 18px; }}
                QPlainTextEdit#question {{ background: transparent; border: 0; padding: 4px; }}
                QPushButton#sendButton {{ background: {text}; color: {inverse};
                    border: 0; border-radius: 16px; padding: 5px 15px; min-height: 24px; }}
                QPushButton#sendButton:disabled {{ background: {border}; color: {muted}; }}
                QWidget#drawerOverlay {{ background: rgba(0, 0, 0, 22); }}
                QFrame#drawerPanel {{ background: {bg}; border: 0;
                    border-left: 1px solid {border}; }}
            """)
            self.present_readers()
        finally:
            self._applying = False

    def present_readers(self):
        for view in self.window.findChildren(MarkdownView):
            view.set_presentation(dark=self.dark, scale=self.window.zoom.currentData())
