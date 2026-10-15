"""Native selected retries: a local failure continues; shared SDK fault halts only this batch."""

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
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from batch_retry_fixtures import retain_failure
    from publication_fixtures import publish_document
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QMessageBox, QPlainTextEdit

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.model_connections import verify_model_connection
    from ultrakb.application.settings import apply_global_config_patch
    from ultrakb.application.settings_data import GlobalConfigPatchRequest
    from ultrakb.desktop.task_details import show_task_details
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.runtime.records import TERMINAL
    from ultrakb.source_catalog import read_source_revision

    live = boundary = None
    if args.real:
        from live_llm import LiveLLM
        from llm_test_budget import TestRunBudget

        live = LiveLLM(TestRunBudget(root / "cost.json"))
        credentials = live.credentials
        model = "deepseek/deepseek-flash"
    else:
        from connection_probe_server import ProbeServer

        from ultrakb.config import LlmCredentialBundle

        boundary = ProbeServer()
        credentials = LlmCredentialBundle(api_key="fixture-private-key", base_url=boundary.url)
        model = "gpt-5.4"
    os.environ["LLM_API_KEY"] = credentials.api_key
    os.environ["OPENAI_API_BASE"] = credentials.base_url
    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    apply_global_config_patch(GlobalConfigPatchRequest(config={"model": model}))
    kb = root / "批量重试库"
    initialize_kb(kb, seed_environment=False)
    sources = []
    for index in range(2 if args.real else 3):
        path = kb / f"raw/未完成-{index}.md"
        with kb_ingest_lock(kb / ".ultrakb"):
            atomic_write_text(
                path, f"# Operating policy {index}\n\nReview retained records every week."
            )
        sources.append(retain_failure(kb, path))
    finished = kb / "raw/已完成.md"
    with kb_ingest_lock(kb / ".ultrakb"):
        atomic_write_text(finished, "# Finished\n\nPreviously committed operating policy.")
    publish_document(kb, finished, "Previously committed summary.", bundle=credentials)
    # A genuine missing retained original creates an independent local failure.
    sources.sort(key=lambda source: source.source_id)
    original = kb / read_source_revision(kb, sources[0].target_revision_id).original
    with kb_ingest_lock(kb / ".ultrakb"):
        original.unlink()
    assert verify_model_connection(kb).state == "passed"
    if boundary:
        boundary.select("quota")
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    checks = []

    def wait(condition, timeout=45):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError("Native batch retry timed out")
        app.processEvents()

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        button(window, "资料").click()
        wait(lambda: "资料" in window.workspaces.panels)
        panel = window.workspaces.panels["资料"][0]
        wait(lambda: len(panel.rows) == len(sources) + 1)
        panel.select_visible_button.click()
        assert panel.bulk_retry.isEnabled()
        reviews = []

        def confirm():
            dialog = next(
                (w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()),
                None,
            )
            if dialog:
                reviews.append(dialog.text())
                dialog.done(QMessageBox.StandardButton.Yes)

        timer = QTimer()
        timer.timeout.connect(confirm)
        timer.start(20)
        panel.bulk_retry.click()
        wait(lambda: len(window.manager.tasks()) == 1)
        timer.stop()
        assert len(reviews) == 1
        task = window.manager.tasks()[0]
        wait(lambda: window.manager.get(task.id).state in TERMINAL, timeout=60)
        task = window.manager.get(task.id)
        assert task.results[0].status == "failed" and not task.results[0].halt
        assert len(task.not_applicable) == 1 and "已完成" in task.not_applicable[0].name
        if args.real:
            assert len(task.results) == 2 and task.succeeded == 1 and task.state == "partial", task
            checks.append(
                "A10 real service: missing original failed; next unfinished source committed; "
                "finished source excluded"
            )
        else:
            assert len(task.results) == 2 and task.results[1].halt and task.total == 3
            assert task.state == "blocked" and "额度" in task.error
            checks.append(
                "A10/A11 boundary: local failure continued, SDK quota halted third unstarted source"
            )
        assert task.processes_reaped
        checked_details = []

        def inspect_details():
            dialog = next(
                (
                    w
                    for w in app.topLevelWidgets()
                    if isinstance(w, QDialog) and w.windowTitle() == "任务结果" and w.isVisible()
                ),
                None,
            )
            if dialog:
                text = dialog.findChild(QPlainTextEdit).toPlainText()
                checked_details.append(text)
                dialog.grab().save(str(root / "task-details.png"))
                dialog.accept()

        inspect = QTimer()
        inspect.timeout.connect(inspect_details)
        inspect.start(20)
        show_task_details(task, window)
        inspect.stop()
        assert checked_details
        assert "不适用（未提交）" in checked_details[0]
        if not args.real:
            assert "第 3 项：未执行" in checked_details[0]
        checks.append(
            "Task detail distinguishes attempted failure, "
            "excluded completed input and not-run remainder"
        )
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (720, 600)):
                window.resize(width, height)
                app.processEvents()
                window.grab().save(str(root / f"{theme}-{width}.png"))
        root.joinpath("task-result.json").write_text(
            json.dumps(task.summary(), ensure_ascii=False, indent=2)
        )
        report = {
            "platform": app.platformName(),
            "headless": args.headless,
            "checks": checks,
            "service": "official DeepSeek" if live else "loopback SDK quota boundary",
            "fixture": "zero-send retained state and one deliberately removed frozen original",
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
        if boundary:
            boundary.close()
    root.joinpath("result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
