"""Native connection controls; all readiness, probing and records live in application."""

from __future__ import annotations

import threading
from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.model_connections import read_connection_state, verify_model_connection
from ultrakb.desktop.form_controls import FocusComboBox


class ConnectionControls(QWidget):
    verified = Signal(object)

    def __init__(self, editor, fields_widget):
        super().__init__(editor)
        self.editor = editor
        self._generation = 0
        self._testing = False
        self._stop = threading.Event()
        self._state = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("连接示例（可修改）"))
        self.service = FocusComboBox()
        self.service.setAccessibleName("模型服务或连接方式")
        self.service.addItems([template.name for template in CONNECTION_TEMPLATES])
        self.service.activated.connect(self.choose_template)
        row.addWidget(self.service, 1)
        layout.addLayout(row)
        self.help = QLabel(CONNECTION_TEMPLATES[0].explanation)
        self.help.setWordWrap(True)
        self.help.setOpenExternalLinks(True)
        layout.addWidget(self.help)
        layout.addWidget(fields_widget)
        self.status = QLabel("正在读取连接状态…")
        self.status.setWordWrap(True)
        self.status.setAccessibleName("连接验证状态")
        layout.addWidget(self.status)
        notice = QLabel("测试使用少量合成输入，可能产生少量用量；不发送您的资料。")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        actions = QHBoxLayout()
        self.test_button = QPushButton("保存并测试连接")
        self.test_button.setAccessibleName("测试模型连接")
        self.test_button.clicked.connect(self.begin)
        self.cancel_button = QPushButton("取消测试")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        actions.addWidget(self.test_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        layout.addLayout(actions)
        for key in ("model", "openai_api_base", "api_key"):
            field = editor.fields[key]
            field.text.textEdited.connect(self.changed)
            field.action.currentIndexChanged.connect(self.changed)
        self.destroyed.connect(lambda: self._stop.set())

    @property
    def testing(self):
        return self._testing

    def choose_template(self, index):
        template = CONNECTION_TEMPLATES[index]
        self.help.setText(
            template.explanation
            + (f' <a href="{template.guide}">官方准备指引</a>' if template.guide else "")
        )
        if template.model is None:
            return
        for key, value in (("model", template.model), ("openai_api_base", template.base_url)):
            field = self.editor.fields[key]
            field.action.setCurrentIndex(1)
            field.text.setText(value)
        from ultrakb.application.connection_capacity import template_capacity

        capacity = template_capacity(template)
        if capacity is not None:
            import json

            field = self.editor.fields["model_capacity"]
            field.action.setCurrentIndex(1)
            field.text.setText(json.dumps(capacity))
        self.changed()

    def changed(self, *_):
        if self.editor._updating:
            return
        self._generation += 1
        self._state = None
        if self._testing:
            self._stop.set()
            self.status.setText("连接已修改，正在结束旧测试。保存后请测试新连接。")
        else:
            self.status.setText("连接有修改；保存后需要测试当前生效连接。")

    def refresh(self):
        if self._testing:
            return
        self._generation += 1
        generation = self._generation

        def loaded(state, error):
            if error:
                self.status.setText("无法读取验证状态。可继续浏览，或检查并恢复设置。")
            else:
                self.show_state(state)

        self.editor.io.submit(
            lambda: read_connection_state(self.editor.kb),
            loaded,
            kb=self.editor.kb,
            exclusive=True,
            global_settings=True,
            obsolete=lambda: self.editor._closed or self._generation != generation,
        )

    def show_state(self, state):
        self._state = state
        message = f"模型：{state.model}\n{state.message}"
        if state.state == "passed":
            tested = datetime.fromisoformat(state.tested_at).astimezone().strftime("%Y-%m-%d %H:%M")
            message += f"\n验证时间：{tested}；已验证：{'、'.join(state.capabilities)}。"
        self.status.setText(message)

    def begin(self):
        if self._testing or self.editor._saving or not self.editor.form.isEnabled():
            return
        # Saving and probing remain separate operations. Test-and-continue first
        # saves pending explicit choices, and a failed probe does not undo them.
        self.editor.save(after_saved=self._start)

    def _start(self):
        if self.editor._closed or self._testing:
            return
        self._testing = True
        self._state = None
        self._generation += 1
        generation = self._generation
        self._stop = threading.Event()
        stop = self._stop
        self.test_button.setEnabled(False)
        self.cancel_button.show()
        self.status.setText("正在测试文本响应与工具调用…可取消；测试最多等待 20 秒。")

        def finished(state, error):
            self._testing = False
            self.test_button.setEnabled(True)
            self.cancel_button.hide()
            if generation != self._generation:
                self.status.setText("旧测试已结束；连接有修改，请保存并测试当前配置。")
            elif stop.is_set():
                self.status.setText("测试已取消；已保存的设置保留，可继续浏览。")
            elif error:
                self.status.setText("测试未完成，设置已保留。可检查服务状态或设置恢复后重试。")
            else:
                self.show_state(state)
                if state.state == "passed":
                    owner = self.editor.parentWidget()
                    while owner is not None and not hasattr(owner, "onboarding"):
                        owner = owner.parentWidget()
                    if owner is not None:
                        owner.onboarding.invalidate(self.editor.kb)
                    self.verified.emit(state)

        # The service captures its lease before the network and releases it.
        # Holding LocalIO's KB/global lease across a model call would block edits.
        self.editor.io.submit(
            lambda: verify_model_connection(
                self.editor.kb,
                cancelled=lambda: stop.is_set()
                or self.editor._closed
                or self.editor.io._stop.is_set(),
            ),
            finished,
            obsolete=lambda: self.editor._closed,
        )

    def cancel(self):
        self._stop.set()
        if self._testing:
            self.status.setText("正在取消测试并收尾；已保存的设置会保留。")
