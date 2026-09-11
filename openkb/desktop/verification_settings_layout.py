"""Settings reachability at the actual embedding seam, including native wheel delivery."""

import json

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialogButtonBox, QScrollArea


def fully_visible(widget):
    if not widget.isVisible():
        return False
    rect = widget.rect()
    while not widget.isWindow():
        rect.translate(widget.pos())
        widget = widget.parentWidget()
        if not widget.rect().contains(rect):
            return False
    return True


def wheel(widget, delta=-120):
    point = widget.rect().center()
    event = QWheelEvent(
        point,
        widget.mapToGlobal(point),
        QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    QApplication.processEvents()


def settle():
    QTest.qWait(20)
    QApplication.processEvents()


def verify_settings_layout(window, kb, root, wait):
    from openkb.desktop.settings import SettingsDialog

    window.open_knowledge_base(kb)
    wait(lambda: window.kb == kb and window.page is not None)
    window.shell.navigate("设置")
    wait(lambda: len(window.workspaces.settings_tabs.findChildren(SettingsDialog)) == 2)
    failures, evidence = [], []

    def check(condition, message):
        if not condition:
            failures.append(message)

    for theme in ("浅色", "深色"):
        window.theme.setCurrentText(theme)
        for width, height in ((1366, 768), (1920, 1080), (900, 650), (720, 600)):
            window.resize(width, height)
            window.shell.navigate("设置")
            for scope in ("全局默认", "当前知识库"):
                panel, host = window.workspaces.panels[scope]
                window.workspaces.settings_tabs.setCurrentWidget(host)
                wait(lambda: panel._loaded)
                ocr = panel.fields["parsing"]
                cases = [(i, "system", False) for i in (0, 1, 3, 4)] + [
                    (2, "system", False),
                    (2, "off", False),
                    (2, "local", False),
                    (2, "service", False),
                    (2, "cloud", False),
                    (2, "local", True),
                    (2, "cloud", True),
                ]
                for index, backend, advanced in cases:
                    panel.editors.setCurrentIndex(index)
                    ocr.policy.setCurrentIndex(
                        ocr.policy.findData("off" if backend == "off" else "auto")
                    )
                    ocr.backend.setCurrentIndex(
                        ocr.backend.findData(
                            "local"
                            if backend == "service"
                            else "system"
                            if backend == "off"
                            else backend
                        )
                    )
                    ocr.execution.setCurrentIndex(
                        ocr.execution.findData("service" if backend == "service" else "runtime")
                    )
                    ocr.advanced.setChecked(advanced)
                    ocr.tabs.setCurrentIndex(1 if backend == "cloud" else 0)
                    for scroll in panel.findChildren(QScrollArea):
                        scroll.verticalScrollBar().setValue(0)
                    settle()
                    label = f"{theme}/{width}/{scope}/{index}/{backend}/{advanced}"
                    save = panel.buttons.button(QDialogButtonBox.StandardButton.Save)
                    check(window.size().toTuple() == (width, height), label + ": window grew")
                    check(fully_visible(save), label + ": save clipped")
                    check(fully_visible(panel.editors.tabBar()), label + ": tabs clipped")
                    scrolls = [s for s in panel.findChildren(QScrollArea) if s.isVisible()]
                    check(len(scrolls) == 1, label + ": nested content scroll areas")
                    if index == 2:
                        check(
                            ocr.source.height() <= ocr.source.fontMetrics().height() * 2,
                            label + ": stretched OCR label",
                        )
                    # All long content must remain reachable without moving the footer.
                    for scroll in scrolls:
                        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                    settle()
                    check(fully_visible(save), label + ": save clipped after scrolling")
                    check(
                        fully_visible(panel.editors.tabBar()),
                        label + ": tabs clipped after scrolling",
                    )
                    evidence.append(
                        {
                            "case": label,
                            "panel_height": panel.height(),
                            "save_visible": fully_visible(save),
                        }
                    )
                    if scope == "全局默认" and width in (1366, 720):
                        window.grab().save(
                            str(root / f"settings-{theme}-{width}-{index}-{backend}-{advanced}.png")
                        )
            window.shell.navigate("产物")
            wait(lambda: "产物" in window.workspaces.panels)
            settle()
            artifacts, _ = window.workspaces.panels["产物"]
            check(fully_visible(artifacts.export_button), f"{theme}/{width}: export clipped")
            check(fully_visible(artifacts.preview_button), f"{theme}/{width}: preview clipped")
            check(
                window.size().toTuple() == (width, height), f"{theme}/{width}: artifact window grew"
            )
            window.grab().save(str(root / f"artifacts-{theme}-{width}.png"))

    window.shell.navigate("设置")
    panel, host = window.workspaces.panels["全局默认"]
    window.workspaces.settings_tabs.setCurrentWidget(host)
    panel.editors.setCurrentIndex(2)
    ocr = panel.fields["parsing"]
    ocr.advanced.setChecked(False)
    ocr.backend.setCurrentIndex(ocr.backend.findData("local"))
    settle()
    for index in range(panel.editors.count()):
        panel.editors.setCurrentIndex(index)
        settle()
        combos = [
            c
            for c in panel.editors.currentWidget().findChildren(QComboBox)
            if c.isVisible() and c.isEnabled() and c.count() > 1
        ]
        for combo in combos:
            window.theme.setFocus()
            before = combo.currentIndex()
            wheel(combo, -120 if before < combo.count() - 1 else 120)
            check(
                combo.currentIndex() == before, f"wheel modified {index}/{combo.itemText(before)}"
            )
            combo.setCurrentIndex(before)
    # Ignoring wheel input must still scroll the form through the native parent chain.
    panel.editors.setCurrentIndex(1)
    settle()
    action = panel.fields["processing"].action
    scroll = action.parentWidget()
    while scroll is not None and not isinstance(scroll, QScrollArea):
        scroll = scroll.parentWidget()
    check(scroll is not None, "processing form has no scrolling owner")
    if scroll is not None:
        scroll.verticalScrollBar().setValue(0)
        window.theme.setFocus()
        before = action.currentIndex()
        # A window-level event exercises hit testing and Qt's ignored-wheel propagation.
        # sendEvent(control, event) only exercises that control's handler.
        # QtTest's window wheel injection takes native pixels, including at 150% scale.
        QTest.wheelEvent(
            window.windowHandle(),
            QPointF(action.mapTo(window, action.rect().center())) * window.devicePixelRatioF(),
            QPoint(0, -120),
        )
        settle()
        check(action.currentIndex() == before, "native wheel changed unfocused choice")
        check(scroll.verticalScrollBar().value() > 0, "wheel did not reach form scrollbar")
        scroll.ensureWidgetVisible(action)
    # Explicit keyboard and popup selection remain available.
    action.setFocus()
    action.setCurrentIndex(0)
    QTest.keyClick(action, Qt.Key.Key_Down)
    check(action.currentIndex() == 1, "keyboard selection broken")
    action.showPopup()
    QTest.keyClick(action, Qt.Key.Key_Down)
    QTest.keyClick(action, Qt.Key.Key_Return)
    action.hidePopup()
    check(action.currentIndex() == 2, "popup selection broken")
    action.setCurrentIndex(0)

    dialog = SettingsDialog(window.io, None, window)
    dialog.resize(760, 460)
    dialog.show()
    wait(lambda: dialog._loaded)
    settle()
    check(
        fully_visible(dialog.buttons.button(QDialogButtonBox.StandardButton.Save)),
        "standalone settings save clipped",
    )
    check(dialog.height() == 460, "standalone settings forced taller")
    dialog.reject()
    (root / "settings-layout.json").write_text(
        json.dumps({"cases": evidence, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    assert not failures, f"{len(failures)} layout/input failures: {failures[:16]}"
