"""Application-local appearance with system tracking and shared native presentation."""

from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from openkb.desktop.reader import MarkdownView
from openkb.desktop.theme import theme_colors


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
        if watched is self.window.question and event.type() in (
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        ):
            composer = watched.parentWidget()
            composer.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            composer.style().unpolish(composer)
            composer.style().polish(composer)
            composer.update()
        return False

    def apply(self):
        if self._applying:
            return
        self._applying = True
        try:
            mode = self.window.theme.currentData()
            self.dark = self.system_dark if mode == "system" else mode == "dark"
            colors = theme_colors(self.dark)
            bg, surface, text, muted, border, accent, selection = (
                colors.background,
                colors.surface,
                colors.text,
                colors.muted,
                colors.border,
                colors.accent,
                colors.selection,
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
                ("Accent", accent),
                ("PlaceholderText", muted),
            ):
                palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
            for role in ("Text", "WindowText", "ButtonText"):
                palette.setColor(
                    QPalette.ColorGroup.Disabled, getattr(QPalette.ColorRole, role), QColor(muted)
                )
            from openkb.desktop.navigation_icons import navigation_icon

            for buttons in (
                self.window.shell.buttons,
                self.window.workspaces.shortcut_buttons,
            ):
                for name, button in buttons.items():
                    button.setIcon(navigation_icon(name, self.dark))
            self.app.setPalette(palette)
            chevron = (Path(__file__).parent / "assets/chevron.svg").as_posix()
            self.app.setStyleSheet(f"""
                QWidget {{ color: {text}; }}
                QMainWindow, QDialog, QWidget#workspace {{ background: {bg}; }}
                QLabel {{ background: transparent; }}
                QLabel#brand {{ font-size: 17px; font-weight: 500; }}
                QLabel#pageTitle, QLabel#conversationTitle {{ font-size: 16px; font-weight: 500; }}
                QLabel#welcomeTitle {{ font-size: 32px; font-weight: 500; }}
                QLabel#eyebrow {{ color: {accent}; font-size: 13px; font-weight: 500; }}
                QLabel#sectionTitle {{ font-size: 14px; font-weight: 500; }}
                QLabel#overviewIntro {{ color: {muted}; font-size: 14px; }}
                QFrame#overviewSummary {{
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
                        stop: 0 {surface}, stop: 0.35 {surface}, stop: 1 {colors.hero_tint});
                    border: 1px solid {border}; border-radius: 14px; }}
                QLabel#overviewStats {{ color: {text}; border: 0; padding: 10px 0;
                    font-size: 22px; font-weight: 500; }}
                QLabel#muted, QLabel#composerHint {{ color: {muted}; font-size: 12px; }}
                QLabel#chatWelcome {{ font-size: 28px; font-weight: 500; }}
                QLabel#drawerTitle {{ font-size: 17px; font-weight: 500; }}
                QFrame#navigation {{ background: {colors.sidebar}; border: 0;
                    border-right: 1px solid {border}; }}
                QFrame#topbar {{ background: {bg}; border: 0;
                    border-bottom: 1px solid {border}; }}
                QPushButton, QToolButton, QComboBox {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; padding: 7px 12px;
                    min-height: 20px; }}
                QToolButton {{ padding: 6px; }}
                QComboBox {{ padding-right: 24px; }}
                QComboBox::drop-down {{ border: 0; width: 24px; }}
                QComboBox::down-arrow {{ image: url("{chevron}"); width: 12px; height: 12px; }}
                QToolButton::menu-indicator {{ image: none; }}
                QToolButton#navigationToggle, QToolButton#applicationMenu {{
                    background: transparent; border-color: transparent; }}
                QToolButton#navigationToggle:hover, QToolButton#applicationMenu:hover {{
                    background: {selection}; }}
                QToolButton#navigationToggle:focus, QToolButton#applicationMenu:focus {{
                    border-color: {accent}; }}
                QFrame#navigation QPushButton {{ text-align: left; padding: 7px 10px;
                    font-size: 18px; color: {muted}; background: transparent;
                    border-color: transparent; border-left: 3px solid transparent; }}
                QFrame#navigation QPushButton:hover {{ background: {selection}; }}
                QFrame#navigation QPushButton:checked {{
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                        stop: 0 {selection}, stop: 1 {colors.sidebar});
                    color: {accent}; border-left-color: {accent}; font-weight: 500; }}
                QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {selection}; }}
                QPushButton:checked, QToolButton:checked {{ background: {selection};
                    color: {accent}; font-weight: 500; }}
                QPushButton:focus, QToolButton:focus, QComboBox:focus, QLineEdit:focus,
                QPlainTextEdit:focus, QTreeView:focus, QTableView:focus {{
                    border-color: {accent}; }}
                QFrame#navigation QPushButton:focus {{ border-color: {accent}; }}
                QTreeView:focus, QTableView:focus {{ border: 1px solid {accent}; }}
                QWidget:disabled {{ color: {muted}; }}
                QLineEdit, QPlainTextEdit {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; padding: 8px;
                    selection-background-color: {selection}; selection-color: {text}; }}
                QTextBrowser, QAbstractItemView {{ background: {surface}; border: 0; padding: 6px;
                    selection-background-color: {selection}; selection-color: {text}; }}
                QLineEdit {{ min-height: 22px; }}
                QAbstractItemView::item {{ padding: 8px; min-height: 22px; border: 0; }}
                QAbstractItemView::item:selected {{ background: {selection}; color: {text}; }}
                QAbstractItemView::item:hover:!selected {{ background: {colors.subtle}; }}
                QHeaderView {{ padding: 0; }}
                QHeaderView::section {{ background: {bg}; color: {muted};
                    border: 0; border-bottom: 1px solid {border}; padding: 10px 8px; }}
                QTableView {{ gridline-color: {border}; }}
                QTableView#documentTable {{ padding: 0; border: 1px solid {border};
                    alternate-background-color: {bg}; }}
                QTableView#documentTable::item {{ padding: 8px 12px;
                    border-bottom: 1px solid {border}; }}
                QTableView#documentTable::item:selected {{ background: {selection}; }}
                QTableView#documentTable::item:hover:!selected {{ background: {surface}; }}
                QTableView#documentTable QHeaderView::section {{ background: {surface};
                    padding: 10px 12px; font-weight: 500; }}
                QFrame#documentRemoval {{ background: {surface};
                    border: 1px solid {border}; border-radius: 8px; }}
                QPushButton#primaryAction, QPushButton#sendButton {{
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
                        stop: 0 {colors.primary}, stop: 1 {colors.primary_end});
                    color: {colors.on_accent};
                    border: 1px solid {colors.primary_end}; font-weight: 500; }}
                QPushButton#primaryAction:hover, QPushButton#sendButton:hover {{
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
                        stop: 0 {colors.accent_hover}, stop: 1 {colors.accent_pressed});
                    border-color: {colors.accent_hover}; }}
                QPushButton#primaryAction:pressed, QPushButton#sendButton:pressed {{
                    background: {colors.accent_pressed}; }}
                QPushButton#primaryAction:focus, QPushButton#sendButton:focus {{
                    border: 2px solid {text}; padding: 6px 11px; }}
                QPushButton#primaryAction:disabled, QPushButton#sendButton:disabled {{
                    background: {colors.subtle};
                    color: {muted}; border-color: {border}; }}
                QPushButton#dangerAction {{ color: {colors.danger}; }}
                QPushButton#dangerAction:disabled {{ color: {muted}; }}
                QPushButton#taskStatus {{ background: {colors.subtle};
                    border-color: transparent; border-radius: 16px; font-size: 12px; }}
                QPushButton#taskStatus[status="running"] {{
                    color: {accent}; background: {selection}; }}
                QPushButton#taskStatus[status="attention"] {{
                    color: {colors.attention}; background: {colors.attention_surface}; }}
                QPushButton#taskStatus:focus {{ border-color: {accent}; }}
                QToolButton#overviewShortcut {{ padding: 16px 24px; border-radius: 12px; }}
                QToolButton#overviewShortcut:hover {{ border-color: {accent};
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
                        stop: 0 {surface}, stop: 1 {selection}); }}
                QTabWidget::pane {{ border: 0; }}
                QTabBar::tab {{ background: transparent; color: {muted}; padding: 9px 14px;
                    border-bottom: 2px solid transparent; }}
                QTabBar::tab:selected {{ color: {accent}; border-bottom: 2px solid {accent}; }}
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
                QProgressBar {{ background: {colors.subtle}; color: {text};
                    border: 1px solid {border}; border-radius: 6px; text-align: center; }}
                QProgressBar::chunk {{ background: {selection}; border-radius: 5px; }}
                QProgressBar#task-row-progress {{ border: 0; border-radius: 3px; }}
                QProgressBar#task-row-progress::chunk {{
                    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
                        stop: 0 {colors.primary}, stop: 1 {colors.primary_end});
                    border-radius: 3px; }}
                QFrame#composer {{ background: {surface}; border: 1px solid {border};
                    border-radius: 18px; }}
                QFrame#composer[focused="true"] {{ border-color: {accent}; }}
                QPlainTextEdit#question {{ background: transparent; border: 0; padding: 4px; }}
                QPushButton#sendButton {{ border-radius: 14px;
                    padding: 5px 15px; min-height: 24px; }}
                QPushButton#sendButton:focus {{ padding: 4px 14px; }}
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
