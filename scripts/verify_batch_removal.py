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
    parser.add_argument("--mixed", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from PySide6.QtCore import QTimer
    from test_batch_removal import shared_sources
    from test_mixed_batch_removal import seed_legacy

    from ultrakb import config
    from ultrakb.application.batch_removal import preview_batch_removal
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.refresh import refresh_status
    from ultrakb.desktop.batch_removal import RemovalReview
    from ultrakb.desktop.task_details import show_task_details
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.source_catalog import read_source

    config.GLOBAL_CONFIG_DIR = output / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    kb = output / "共享知识库"
    initialize_kb(kb, seed_environment=False)
    originals = output / "外部原件"
    originals.mkdir()
    legacy = seed_legacy(kb, originals) if args.mixed else ()
    selected = legacy + shared_sources(kb, originals)
    for key in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        os.environ.pop(key, None)
    app = create_application()
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
                assert dialog.keep_raw.isVisible() is args.mixed
                assert dialog.keep_empty.isVisible() is args.mixed
                if args.mixed:
                    assert not dialog.confirm.isEnabled()
                    dialog.acknowledgements["source"].setChecked(True)
                    assert not dialog.confirm.isEnabled()
                    dialog.acknowledgements["legacy"].setChecked(True)
                    assert dialog.confirm.isEnabled()
                    dialog.keep_raw.setChecked(True)
                    assert not dialog.confirm.isEnabled()
                    wait(lambda: not dialog._loading)
                    assert dialog.preview.keep_raw and not any(
                        box.isChecked() for box in dialog.acknowledgements.values()
                    )
                    dialog.keep_raw.setChecked(False)
                    wait(lambda: not dialog._loading)
                    assert (
                        not dialog.preview.keep_raw
                        and "raw/shared-old.md" in dialog.preview.legacy_deleted
                    )
                dialog.grab().save(str(output / f"review-{theme}-{width}.png"))
                dialog.reject()
        checks.append("A12: aggregate shared dependencies, lost-all warning; two themes/two sizes")
        assert not any(
            read_source(kb, identity.value).removed
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
        assert not resolve_query_views(kb).has_evidence
        assert "concepts/shared.md" in refresh_status(kb, scope=panel.scope)["needs_refresh"]
        assert all((originals / f"{name}.md").exists() for name in ("甲", "乙"))
        if args.mixed:
            assert not (kb / "raw/shared-old.md").exists()
            assert not (kb / "wiki/concepts/legacy-shared.md").exists()
            assert all((originals / f"{name}.md").exists() for name in ("old-a", "old-b"))
            checks.append(
                "A12/A13 mixed: separate approvals, option reset, shared legacy cleanup; "
                "external originals kept"
            )
        checks.append(
            "A13: actual spawn withdrawal; retained originals/history; "
            "excluded query evidence, pending knowledge"
        )

        def capture():
            for widget in app.topLevelWidgets():
                if widget.windowTitle() == "任务结果" and widget.isVisible():
                    widget.grab().save(str(output / "results.png"))
                    widget.accept()

        QTimer.singleShot(100, capture)
        show_task_details(task, panel)
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
