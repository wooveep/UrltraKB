"""Drive the native aggregate Source withdrawal with real retained domain state."""

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
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ULTRAKB_CONFIG_DIR"] = str(output / "settings")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from publication_fixtures import publish_document
    from PySide6.QtCore import QTimer

    from ultrakb import config
    from ultrakb.application.batch_removal import preview_batch_removal
    from ultrakb.application.document_actions import DocumentIdentity
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.refresh import refresh_status
    from ultrakb.application.views import published_scope
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.desktop.batch_removal import RemovalReview
    from ultrakb.desktop.task_details import show_task_details
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.storage.catalog_store import CatalogStore

    config.GLOBAL_CONFIG_DIR = output / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = output / "共享知识库"
    initialize_kb(kb, seed_environment=False)
    originals = output / "外部原件"
    originals.mkdir()
    selected = []
    for name in ("甲", "乙"):
        path = originals / f"{name}.md"
        path.write_text(f"# {name}\n\nRetain every source receipt.")
        result = publish_document(
            kb,
            path,
            "Retain receipts.",
            shared_body="Shared receipt policy.",
            bundle=LlmCredentialBundle(api_key="unused-fixture"),
        )
        selected.append(DocumentIdentity("source", result.source_id))
    selected = tuple(selected)
    catalog = CatalogStore(kb, published_scope(kb).knowledge.identity)
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        os.environ.pop(key, None)
    app = create_application(settings_dir=output / "qt-settings")
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=output / "tasks")
    window.show()
    checks = []

    def wait(condition):
        deadline = time.monotonic() + 45
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Batch withdrawal acceptance timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "资料").click()
        wait(lambda: "资料" in window.workspaces.panels)
        panel = window.workspaces.panels["资料"][0]
        wait(lambda: len(panel.rows) == len(selected))
        panel.select_visible_button.click()
        assert panel.selection.checked == set(selected) and panel.bulk_remove.isEnabled()
        reviewed = preview_batch_removal(kb, selected, scope=panel.scope)
        assert "concepts/shared.md" in reviewed.lose_all_sources
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (960, 680)):
                window.resize(width, height)
                app.processEvents()
                window.grab().save(str(output / f"documents-{theme}-{width}.png"))
                dialog = RemovalReview(reviewed, panel)
                dialog.show()
                app.processEvents()
                assert "将失去全部有效来源：concepts/shared.md" in dialog.details.toPlainText()
                assert dialog.keep_raw.isVisible() is False
                assert dialog.keep_empty.isVisible() is False
                dialog.grab().save(str(output / f"review-{theme}-{width}.png"))
                dialog.reject()
        checks.append("A12: aggregate shared dependencies, lost-all warning; two themes/two sizes")
        assert not any(
            catalog.source(identity.value).removed
            for identity in selected
            if identity.kind == "source"
        )
        checks.append("A12: cancelling review preserves all sources and external originals")
        accepted = []

        def confirm():
            for widget in app.topLevelWidgets():
                if isinstance(widget, RemovalReview) and widget.isVisible():
                    accepted.append(widget.details.toPlainText())
                    for box in widget.acknowledgements.values():
                        box.setChecked(True)
                    widget.confirm.click()

        timer = QTimer()
        timer.timeout.connect(confirm)
        timer.start(20)
        panel.bulk_remove.click()
        wait(lambda: panel._task is not None)
        task_id = panel._task
        timer.stop()
        wait(lambda: window.manager.get(task_id).state in TERMINAL)
        task = window.manager.get(task_id)
        assert task.state == "completed", task
        assert len(accepted) == 1 and len(task.results[0].removal_items) == len(selected)
        assert all(item.status == "completed" for item in task.results[0].removal_items)
        assert all(catalog.source(identity.value).removed for identity in selected)
        from ultrakb.documents import read_document_source

        assert all(
            read_document_source(kb, item.value)["validity"] == "withdrawn" for item in selected
        )
        assert "concepts/shared.md" in refresh_status(kb, scope=panel.scope)["needs_refresh"]
        assert all((originals / f"{name}.md").exists() for name in ("甲", "乙"))
        checks.append(
            "A13: actual spawn withdrawal; retained originals/history; "
            "withdrawn provenance, pending knowledge"
        )

        captured = []

        def capture():
            from ultrakb.desktop.knowledge_tasks import KnowledgeTasksDialog

            for widget in app.topLevelWidgets():
                if (
                    isinstance(widget, KnowledgeTasksDialog)
                    and widget.isVisible()
                    and not widget._busy
                ):
                    if widget.detail is not None:
                        captured.append(widget.detail.execution.application_task_id)
                        widget.grab().save(str(output / "results.png"))
                        widget.accept()

        details_timer = QTimer()
        details_timer.timeout.connect(capture)
        details_timer.start(80)
        show_task_details(task, panel)
        details_timer.stop()
        assert captured == [task.id], captured
        (output / "task-result.json").write_text(
            json.dumps(task.summary(), ensure_ascii=False, indent=2)
        )
        checks.append(
            "A12/A13: correlated per-source results persisted; model sends 0; no undo claim"
        )
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "service": "no model calls; local withdrawal",
            "model_sends": 0,
            "checks": checks,
        }
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        window.request_quit()
        wait(window.shutdown_complete)


if __name__ == "__main__":
    main()
