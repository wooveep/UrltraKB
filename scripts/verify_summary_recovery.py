"""Exercise native summary-retry confirmations without dispatching any model work."""

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ULTRAKB_CONFIG_DIR"] = str(output / "settings")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QToolButton, QWidget
    from test_storage_conversation_writes import StreamTransport

    from ultrakb import config
    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.views import published_scope
    from ultrakb.config import save_config
    from ultrakb.desktop.document_rows import select_identity
    from ultrakb.desktop.documents import DocumentsDialog
    from ultrakb.desktop.verification import create_application
    from ultrakb.llm_execution import CompletionExecutor
    from ultrakb.storage.receipt_store import ReceiptStore
    from ultrakb.storage.usage_store import UsageStore

    def forbidden(*args, **kwargs):
        raise AssertionError("Native acceptance must not call a model executor")

    config.GLOBAL_CONFIG_DIR = output / "settings"
    config.GLOBAL_CONFIG_PATH = output / "settings/global.yaml"
    kb = output / "恢复验证库"
    initialize_kb(kb, seed_environment=False)
    save_config(
        kb / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": 4, "retries": 0},
        },
    )
    source = output / "summary-input.md"
    source.write_text("# Retained rule\n\nClose the inlet before maintenance.")
    context = ExecutionContext()
    with StreamTransport([{"content": "invalid summary"}] * 2) as service:
        (kb / ".env").write_text(
            f"LLM_API_KEY=offline-summary-key\nOPENAI_API_BASE={service.url}\n"
        )
        result = import_document(kb, source, context=context)
        assert result.status == "failed", result.message
        assert len(service.requests) == 2
    fixture = SimpleNamespace(result=result)
    native_identity = published_scope(kb).knowledge.identity
    usage = UsageStore(kb, native_identity)
    receipts = ReceiptStore(kb, native_identity)

    def ledger_bytes():
        rows = usage.requests(execution_id=context.storage_execution.execution_id).records
        return {
            row.request_attempt_id: (row.model_dump_json(), receipts.read(row.request_attempt_id))
            for row in rows
        }

    retained = ledger_bytes()
    CompletionExecutor.complete = forbidden
    CompletionExecutor.acomplete = forbidden
    app = create_application(settings_dir=output / "qt-settings")
    app.setQuitOnLastWindowClosed(False)
    submissions, errors, checks = [], [], []

    class InlineIO:
        def submit(self, function, callback, **options):
            try:
                result = function()
            except Exception as error:
                callback(None, error)
            else:
                callback(result, None)

    def submit(root, requests, **options):
        submissions.append(tuple(requests))
        return "f" * 32

    window = QWidget()
    window.kb, window.scope, window._quitting = kb, None, False
    window.io = InlineIO()
    window.manager = SimpleNamespace(submit=submit)
    window.onboarding = SimpleNamespace(ensure=lambda root, description, callback, **kw: callback())
    panel = DocumentsDialog(window, kb)
    panel.timer.stop()
    panel.resize(820, 650)
    panel.show()
    app.processEvents()
    [identity] = panel.rows
    assert select_identity(panel, identity)

    def run(*, opt_in, accept, batch=False, screenshot=None):
        panel._task = None
        captured = []

        def confirm():
            box = next(
                (
                    widget
                    for widget in QApplication.topLevelWidgets()
                    if isinstance(widget, QMessageBox) and widget.isVisible()
                ),
                None,
            )
            if box is None:
                return
            timer.stop()
            try:
                checkbox = box.checkBox()
                assert checkbox is not None and not checkbox.isChecked()
                assert "重试失败摘要" in checkbox.text()
                assert "一次" in box.informativeText()
                if opt_in:
                    QTest.mouseClick(checkbox, Qt.MouseButton.LeftButton)
                assert checkbox.isChecked() is opt_in
                assert checkbox.width() >= checkbox.fontMetrics().horizontalAdvance(checkbox.text())
                assert box.width() <= box.screen().availableGeometry().width()
                if screenshot:
                    assert box.grab().save(str(output / screenshot))
                captured.append(True)
            except Exception:
                import traceback

                errors.append(traceback.format_exc())
                accept_result = QMessageBox.StandardButton.No
            else:
                accept_result = (
                    QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No
                )
            box.done(accept_result)

        timer = QTimer()
        timer.timeout.connect(confirm)
        timer.start(10)
        if batch:
            from ultrakb.desktop.document_selection import update

            panel.selection.checked = {identity}
            update(panel)
            QTest.mouseClick(panel.bulk_retry, Qt.MouseButton.LeftButton)
        else:
            controls = panel.table.cellWidget(0, 3)
            QTest.mouseClick(controls.findChild(QToolButton), Qt.MouseButton.LeftButton)
            action = next(
                action for action in panel._row_menu.actions() if action.text() == "重试未完成项"
            )
            panel._row_menu.close()
            action.trigger()
        timer.stop()
        assert not errors, errors
        assert captured, "The row and bulk actions must both show the confirmation"

    try:
        run(opt_in=True, accept=False)
        assert not submissions
        checks.append("Cancel submits nothing even after selecting failed-summary recovery")
        run(opt_in=False, accept=True)
        assert len(submissions) == 1 and submissions[-1][0].retry_failed_response is False
        checks.append("Ordinary row retry defaults to no additional summary recovery")
        run(opt_in=True, accept=True, screenshot="summary-retry-confirmation.png")
        assert len(submissions) == 2 and submissions[-1][0].retry_failed_response is True
        request = submissions[-1][0]
        assert request.source_id == fixture.result.source_id
        assert request.source_revision_id == fixture.result.source_revision_id
        assert request.unit_revision_id == fixture.result.units[0].target_revision_id
        checks.append("Row opt-in submits a source/revision-bound recovery request")
        run(opt_in=True, accept=True, batch=True)
        assert len(submissions) == 3 and submissions[-1][0].retry_failed_response is True
        assert ledger_bytes() == retained
        checks.append("Bulk retry shares the opt-in and never changes existing model ledgers")
        (output / "result.json").write_text(
            json.dumps(
                {
                    "checks": checks,
                    "platform": app.platformName(),
                    "model_sends": 2,
                    "confirmation_model_sends": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    finally:
        panel.done(0)
        window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
