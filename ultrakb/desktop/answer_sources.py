"""Expand actual references and read captured versions without leaving the answer."""

from pathlib import Path

from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from ultrakb.application.answer_citations import answer_citations, read_answer_reference
from ultrakb.desktop.reader import MarkdownView


class CitationControls(QWidget):
    def __init__(self, actions):
        super().__init__(actions)
        self.actions = actions
        self.groups = answer_citations(actions.data)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton(f"出处（{len(self.groups)}）")
        self.toggle.setFlat(True)
        self.toggle.setAccessibleName("展开此回答出处")
        self.toggle.setCheckable(True)
        self.list = QWidget()
        body = QVBoxLayout(self.list)
        body.setContentsMargins(8, 4, 8, 4)
        if not self.groups:
            label = QLabel("本条回答未提供本库出处链接。可从资料页阅读原文后继续提问。")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            body.addWidget(label)
        for group in self.groups:
            label = QLabel(
                group.name + (" · 旧回答缺少证据快照" if group.historical_unknown else "")
            )
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            body.addWidget(label)
            seen = set()
            for reference in group.references:
                key = reference.get("stored_path"), reference["path"]
                if key in seen:
                    continue
                seen.add(key)
                badge = (
                    "有本次原文读取记录" if reference["returned"] else "仅正文引用，读取记录未提供"
                )
                note = QLabel(f"{reference['path']} · {badge}")
                note.setWordWrap(True)
                note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                body.addWidget(note)
                button = QPushButton("核对原文")
                button.setAccessibleName(f"核对 {group.name} 原文")
                button.setToolTip(reference["target"])
                button.clicked.connect(
                    lambda _checked=False, reference=reference, group=group: open_reference(
                        actions.reader.owner.window,
                        actions.reader.owner,
                        actions.data,
                        reference,
                        historical_unknown=group.historical_unknown,
                    )
                )
                body.addWidget(button)
        layout.addWidget(self.list)
        self.hide()
        self.toggle.toggled.connect(self.setVisible)


def follow_answer_link(reader, url):
    data = reader._answer_data
    if data is None:
        reader.owner.anchorClicked.emit(url)
        return
    targets = {url.toString(), url.toString().removeprefix("ultrakb:")}
    for group in answer_citations(data):
        reference = next(
            (
                reference
                for reference in group.references
                if reference["target"] in targets
                or reference["path"].removesuffix(".md") == url.path().removesuffix(".md")
            ),
            None,
        )
        if reference:
            open_reference(
                reader.owner.window,
                reader.owner,
                data,
                reference,
                historical_unknown=group.historical_unknown,
            )
            return
    reader.owner.anchorClicked.emit(url)


def open_reference(window, transcript, data, reference, *, historical_unknown=False):
    root = Path(data.kb_dir)
    if root != window.kb or window._quitting:
        return
    position, identity = transcript.verticalScrollBar().value(), transcript._identity

    def loaded(value, error):
        dialog = QDialog(window)
        dialog.setWindowTitle(f"回答出处 · {reference['name']}")
        from ultrakb.desktop.dialog_geometry import resize_to_screen

        resize_to_screen(dialog, 820, 620)
        layout = QVBoxLayout(dialog)
        title = QLabel(f"知识库：{root}\n引用：{reference['target']}")
        title.setWordWrap(True)
        layout.addWidget(title)
        if error:
            note = QLabel(str(error))
            note.setWordWrap(True)
            layout.addWidget(note)
            recover = QPushButton("查看资料 / 核对原件")
            recover.clicked.connect(lambda: (dialog.accept(), window.shell.navigate("资料")))
            layout.addWidget(recover)
        else:
            content, description, base = value
            note = QLabel(description)
            note.setWordWrap(True)
            layout.addWidget(note)
            view = MarkdownView()
            view.show_markdown(
                content, base, dark=window.appearance.dark, scale=window.zoom.currentData()
            )
            layout.addWidget(view, 1)
            dialog.finished.connect(lambda _: view.stop_rendering())
            window._citation_readers.append(view)
        close = QPushButton("返回回答")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.finished.connect(
            lambda _: transcript.verticalScrollBar().setValue(position)
            if transcript._identity == identity
            else None
        )
        dialog.setModal(True)
        window._citation_dialog = dialog
        dialog.show()

    if not hasattr(window, "_citation_readers"):
        window._citation_readers = []
    window.io.submit(
        lambda: read_answer_reference(root, reference, historical_unknown=historical_unknown),
        loaded,
        kb=root,
        obsolete=lambda: window.kb != root or transcript._identity != identity or window._quitting,
    )
