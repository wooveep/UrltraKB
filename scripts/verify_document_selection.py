"""Exercise native checkbox intent and optionally execute a real two-document batch."""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        os.environ.pop(key, None)
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from publication_fixtures import fail_document, publish_document
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox

    from ultrakb import config
    from ultrakb.application.document_batches import preview_document_batch
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.desktop.document_rows import IDENTITY_ROLE
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.state import HashRegistry

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = root / "批量选择库"
    initialize_kb(kb, seed_environment=False)
    bundle = LlmCredentialBundle(api_key="unused-fixture")
    published = []
    for name, body in (
        ("项目甲", "Project A has a seven-day review period."),
        ("项目乙", "Project B archives records every month."),
    ):
        path = kb / "raw" / name / "manual.md"
        with kb_ingest_lock(kb / ".ultrakb"):
            path.parent.mkdir()
            atomic_write_text(path, "# Operating policy\n\n" + body)
        published.append(publish_document(kb, path, body, bundle=bundle).source_id)
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(kb / "raw/未完成.md", "# Unfinished input\n\nLocal delivery interrupted.")
        HashRegistry(kb / ".ultrakb/hashes.json").add(
            "f" * 64, {"name": "正文缺失.pdf", "doc_name": "missing", "type": "pdf"}
        )
    fail_document(kb, kb / "raw/未完成.md", bundle=bundle)
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    checks, live = [], None

    def wait(condition, timeout=35):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Document selection acceptance timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "资料").click()
        wait(lambda: "资料" in window.workspaces.panels)
        panel = window.workspaces.panels["资料"][0]
        wait(lambda: panel.table.rowCount() == 4)
        first = next(
            row.identity for row in panel.rows.values() if row.identity.value == published[0]
        )
        second = next(
            row.identity for row in panel.rows.values() if row.identity.value == published[1]
        )

        def item_for(identity):
            return next(
                panel.table.item(i, 0)
                for i in range(panel.table.rowCount())
                if panel.table.item(i, 0).data(IDENTITY_ROLE) == identity
            )

        panel.table.setCurrentItem(item_for(first))
        panel.table.setFocus()
        QTest.keyClick(panel.table, Qt.Key.Key_Space)
        assert panel.selection.checked == {first}
        panel.table.setCurrentItem(item_for(second))
        assert panel.selection.checked == {first}
        QTest.mouseClick(
            panel.table.viewport(),
            Qt.MouseButton.RightButton,
            pos=panel.table.visualItemRect(item_for(second)).center(),
        )
        # QTest's synthetic mouse release does not request a platform context
        # event on X11/offscreen. Deliver that distinct Qt event to the viewport.
        position = panel.table.visualItemRect(item_for(second)).center()
        QApplication.sendEvent(
            panel.table.viewport(),
            QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                position,
                panel.table.viewport().mapToGlobal(position),
            ),
        )
        assert (
            panel.selection.checked == {first} and "仅此资料" in panel._row_menu.actions()[0].text()
        )
        panel._row_menu.close()
        checks.append("A08/A09: checkbox intent independent of row focus and per-row context menu")
        panel.search.setText("manual")
        assert not panel.selection.checked and "清空勾选" in panel.status.text()
        panel.search.setFocus()
        QTest.keyClick(panel.search, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert panel.search.selectedText() == "manual" and not panel.selection.checked
        panel.table.setFocus()
        QTest.keyClick(panel.table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert panel.selection.checked == {first, second}
        panel.table.sortItems(0, Qt.SortOrder.DescendingOrder)
        panel.reload()
        wait(lambda: not window.io._callbacks)
        assert panel.selection.checked == {first, second}
        assert (
            item_for(first).checkState() == item_for(second).checkState() == Qt.CheckState.Checked
        )
        checks.append(
            "A08: filtered Ctrl+A, input shortcut isolation; "
            "duplicate names and refresh/sort preserve IDs"
        )
        panel.search.clear()
        assert not panel.selection.checked
        panel.select_visible_button.click()
        assert len(panel.selection.checked) == 4 and "2 项可重新生成" in panel.bulk_count.text()
        panel.bulk_reasons.click()
        assert "未完成" in panel.details.toPlainText() and "正文缺失" in panel.details.toPlainText()
        reviewed = preview_document_batch(kb, tuple(panel.selection.checked), scope=panel.scope)
        assert {identity.value for identity in reviewed.approved} == set(published)
        assert len(reviewed.skipped) == 2 and all(skip.reason for skip in reviewed.skipped)
        assert {target.name for target in reviewed.targets} and all(
            "项目" in target.name for target in reviewed.targets
        )
        panel.grab().save(str(root / "mixed-selection.png"))
        checks.append("A09: selected 4/applicable 2; exact typed subset and per-item skip reasons")
        with kb_ingest_lock(kb / ".ultrakb"):
            HashRegistry(kb / ".ultrakb/hashes.json").remove_by_hash("f" * 64)
        panel.reload()
        wait(lambda: not window.io._callbacks)
        assert len(panel.selection.checked) == 3 and "消失资料" in panel.status.text()
        panel.state_filter.setCurrentText("未完成")
        assert not panel.selection.checked and "清空勾选" in panel.status.text()
        panel.select_visible_button.click()
        assert len(panel.selection.checked) == 1 and not panel.bulk_recompile.isEnabled()
        assert panel.bulk_remove.isEnabled()
        checks.append(
            "A08/A09: disappeared target removed, filter clears and ineligible-only batch disabled"
        )
        panel.state_filter.setCurrentText("全部")
        panel.select_visible_button.click()
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (720, 600)):
                window.resize(width, height)
                app.processEvents()
                assert panel.bulk_bar.isVisible() and panel.bulk_recompile.isVisible()
                window.grab().save(str(root / f"{theme}-{width}.png"))
        if args.real:
            from live_llm import LiveLLM
            from llm_test_budget import TestRunBudget

            live = LiveLLM(TestRunBudget(root / "cost.json"))
            # Compiler fixture setup can load the repository's CLI .env. Bind
            # this test process and spawn children to the accounted gateway;
            # production SDK serialization and worker code remain untouched.
            os.environ["LLM_API_KEY"] = live.credentials.api_key
            os.environ["OPENAI_API_BASE"] = live.credentials.base_url
            confirmation = []

            def confirm():
                box = next(
                    (
                        w
                        for w in app.topLevelWidgets()
                        if isinstance(w, QMessageBox) and w.isVisible()
                    ),
                    None,
                )
                if box:
                    confirmation.append(box.text())
                    box.done(QMessageBox.StandardButton.Yes)

            timer = QTimer()
            timer.timeout.connect(confirm)
            timer.start(20)
            panel.bulk_recompile.click()
            wait(lambda: window.onboarding.dialog is not None)
            timer.stop()
            assert len(confirmation) == 1 and "选中 3 项，仅执行 2 项" in confirmation[0]
            editor = window.onboarding.dialog.editors[0]
            wait(lambda: editor.form.isEnabled())
            for key, value in (
                ("model", "deepseek/deepseek-flash"),
                ("api_key", live.credentials.api_key),
                ("openai_api_base", live.credentials.base_url),
            ):
                editor.fields[key].action.setCurrentIndex(1)
                editor.fields[key].text.setText(value)
            editor.connection.test_button.click()
            wait(lambda: len(window.manager.tasks()) == 1)
            task = window.manager.tasks()[0]
            wait(lambda: window.manager.get(task.id).state in TERMINAL, timeout=55)
            task = window.manager.get(task.id)
            assert task.total == 2 and len(task.results) == 2, task
            assert task.succeeded == 2 and task.processes_reaped, task
            assert all(result.import_units for result in task.results)
            assert live.requests and live.budget.report()["provider_requests"] > 0
            root.joinpath("task-result.json").write_text(
                json.dumps(task.summary(), ensure_ascii=False, indent=2)
            )
            checks.append(
                "Real production SDK/runtime: only the 2 approved sources regenerate; "
                "per-unit durable results"
            )
        else:
            assert not window.manager.tasks()
        report = {
            "platform": app.platformName(),
            "headless": args.headless,
            "checks": checks,
            "fixture": "real publication/local failure; initial publication has zero model sends",
            "service": "official DeepSeek regeneration" if live else "no model calls",
            "context_menu_input": "distinct Qt context event injected; no OS mouse-event claim",
        }
        if live:
            report["cost"] = live.budget.report()
    finally:
        if window.onboarding.dialog:
            window.onboarding.dialog.reject()
        window.request_quit()
        wait(window.shutdown_complete)
        if live:
            live.close()
    root.joinpath("result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
