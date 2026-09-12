"""Keyboard-accessible native processing flow shared by inventory and source detail."""

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from openkb.desktop.source_flow_state import STAGES, STATE_LABELS, flow_steps
from openkb.desktop.theme import theme_colors


class SourceFlow(QWidget):
    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sourceFlow")
        self.buttons, self._steps, self._selected = {}, (), None
        self._source_identity, self._shown = None, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.caption = QLabel("选择一份资料，查看处理流程")
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        scroll = self.scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for index, (key, title, hint) in enumerate(STAGES):
            if index:
                arrow = QLabel("→")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                row.addWidget(arrow)
            button = QPushButton(title)
            button.setObjectName("sourceStage_" + key)
            button.setMinimumSize(106, 78)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setToolTip(hint + "\n点击查看该阶段的内容与操作。")
            button.clicked.connect(lambda checked=False, key=key: self.select(key, emit=True))
            row.addWidget(button, 1)
            self.buttons[key] = button
        scroll.setWidget(strip)
        scroll.setFixedHeight(101)
        layout.addWidget(scroll)
        self.setEnabled(False)

    def display(self, saved, activity=None, *, selected=None):
        self.setEnabled(saved is not None)
        self._steps = flow_steps(saved, activity)
        source = saved["source"] if saved else {}
        identity = (source.get("source_id"), source.get("id"))
        if identity != self._source_identity:
            self._selected, self._source_identity = None, identity
        if selected is not None:
            self._selected = selected
        elif self._selected is None:
            self._selected = next((s.key for s in self._steps if s.current), "intake")
        current = next((s for s in self._steps if s.current), None)
        name = saved["source"]["name"] if saved else "请选择一份资料"
        description = (
            (current.title + " · " + STATE_LABELS[current.state])
            if current
            else "当前细分阶段暂无记录"
        )
        if activity and activity.state in {"queued", "waiting"} and current is None:
            description = "等待执行 · 将从已保存进度继续"
        shown = (self._steps, self._selected, name, description)
        if shown == self._shown:
            return
        self._shown = shown
        self.caption.setText(name + "  /  " + description)
        self._paint_steps()
        QTimer.singleShot(0, self.reveal_selected)

    def select(self, key, *, emit=False):
        if key not in self.buttons:
            return
        self._selected = key
        self._paint_steps()
        self.reveal_selected()
        if emit:
            self.activated.emit(key)

    def reveal_selected(self):
        if self._selected in self.buttons:
            self.scroll.ensureWidgetVisible(self.buttons[self._selected], 12, 0)

    def _paint_steps(self):
        colors = theme_colors(self.palette().color(QPalette.ColorRole.Window).lightness() < 128)
        for index, step in enumerate(self._steps, 1):
            button = self.buttons[step.key]
            selected = step.key == self._selected
            ink, fill = colors.muted, colors.surface
            if step.state == "completed":
                ink = colors.accent
            elif step.state in {"running", "stopping"}:
                ink, fill = colors.accent, colors.selection
            elif step.state in {"paused", "review", "failed"}:
                ink, fill = colors.attention, colors.attention_surface
            marker = "✓" if step.state == "completed" else str(index).zfill(2)
            suffix = " · 当前位置" if step.current and step.state != "completed" else ""
            button.setText(
                f"{marker}  {step.title}\n{step.progress or STATE_LABELS[step.state]}{suffix}"
            )
            button.setAccessibleName(step.title + "，" + STATE_LABELS[step.state] + suffix)
            button.setChecked(selected)
            button.setStyleSheet(
                f"QPushButton {{color:{ink}; background:{fill}; border-radius:9px;"
                f"border:{2 if selected else 1}px solid "
                f"{colors.accent if selected else colors.border};"
                "padding:7px 5px; font-size:12px;}"
                f"QPushButton:hover {{background:{colors.selection};}}"
                f"QPushButton:focus {{border:2px solid {colors.accent};}}"
            )

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_steps"):
            self._paint_steps()
        super().changeEvent(event)
