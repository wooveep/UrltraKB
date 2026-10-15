"""Exercise first use, real Qt setup, provider errors, creation and chat dispatch."""

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
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ["XDG_CONFIG_HOME"] = str(root / "profile")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_BASE",
        "ULTRAKB_DIR",
    ):
        os.environ.pop(key, None)
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from error_transport import ErrorTransport
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog, QMessageBox, QScrollArea
    from test_model_connections import retained_connection_proof

    from ultrakb import config
    from ultrakb.application.model_connections import capture_connection, read_connection_state
    from ultrakb.application.settings import (
        apply_global_config_patch,
        apply_kb_config_patch,
        read_settings_view,
    )
    from ultrakb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.window import Workbench

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    window = Workbench(history_dir=root / "tasks", first_use=True)
    window.resize(1366, 768)
    window.show()
    boundary = ErrorTransport(status=401)
    checks = []

    def wait(condition, timeout=20):
        deadline = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            assert not [
                w for w in app.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()
            ]
            if time.monotonic() > deadline:
                raise TimeoutError("Setup wizard verification timed out")
            time.sleep(0.01)
        app.processEvents()

    def capture(wizard, filename):
        app.processEvents()
        assert wizard.rect().contains(wizard.primary.geometry())
        assert wizard.primary.height() >= 44
        if wizard.pages.currentIndex() == 0:
            assert wizard.connection.status.isVisible()
            assert wizard.rect().contains(wizard.connection.status.geometry())
        assert all(
            scroll.horizontalScrollBar().maximum() == 0
            for scroll in wizard.findChildren(QScrollArea)
        )
        assert wizard.grab().save(str(root / f"{filename}.png"))

    try:
        wait(lambda: window._creation_dialog is not None)
        wizard = window._creation_dialog
        wait(lambda: wizard.connection.view is not None)
        assert wizard.step == 0 and not wizard.primary.isEnabled() and window.kb is None
        capture(wizard, "01-provider")
        checks.append("First launch opens independent setup; no KB is created before confirmation")
        wizard.connection.service.setFocus()
        QTest.keyClick(wizard.connection.service, Qt.Key.Key_Home)
        QTest.keyClick(wizard.connection.service, Qt.Key.Key_Tab)
        assert app.focusWidget() is not None
        wizard.connection.service.setCurrentIndex(2)
        wizard.primary.click()
        assert wizard.step == 1
        wizard.connection.base_url.setText("invalid-address")
        wizard.primary.click()
        assert wizard.step == 1 and "完整" in wizard.connection.status.text()
        wizard.connection.base_url.setText(boundary.url)
        wizard.connection.api_key.setText("fixture-private-key")
        wizard.connection.show_key.setChecked(True)
        wizard.connection.show_key.setChecked(False)
        capture(wizard, "02-credentials")
        wizard.primary.click()
        assert wizard.step == 2
        wizard.back.click()
        assert wizard.connection.base_url.text() == boundary.url
        assert wizard.connection.api_key.text() == "fixture-private-key"
        wizard.primary.click()
        wizard.connection.model.setEditText("deepseek-flash")
        wizard.connection.fetch_models.click()
        wait(lambda: not wizard.connection.busy)
        assert wizard.connection.model.currentText() == "deepseek-flash"
        assert not config.GLOBAL_CONFIG_PATH.exists()
        wizard.primary.click()
        wait(lambda: not wizard.connection.busy)
        assert wizard.step == 2 and wizard.connection.state.code == "authentication"
        assert len(boundary.requests) == 1
        assert read_connection_state().state == "failed"
        assert "fixture-private-key" not in wizard.connection.status.text()
        capture(wizard, "03-recoverable-error")
        checks.append(
            "Back preserves input; URL and provider errors stay inline; secrets stay hidden"
        )
        # A distinct connection identity starts the historical-readiness scenario.
        # Keep the real authentication failure; never relabel its result as passed.
        retained_key = "retained-private-key"
        apply_global_config_patch(GlobalConfigPatchRequest(api_key=retained_key))
        assert read_connection_state().state == "unverified"
        assert not capture_connection().missing
        retained_connection_proof()
        wizard.reject()
        window._create_kb()
        wizard = window._creation_dialog
        wait(lambda: wizard.step == 3)
        assert read_connection_state().state == "passed"
        assert read_settings_view().values.model == "deepseek/deepseek-flash"
        assert read_settings_view().values.model_capacity is not None
        assert not wizard.connection.api_key.text()
        checks.append(
            "A real SDK HTTP 401 stays failed; a distinct historical connection is reloaded"
        )
        wizard.name.setText("..")
        wizard.primary.click()
        assert wizard.step == 3 and wizard.kb_status.isVisible()
        assert wizard.rect().contains(wizard.kb_status.geometry())
        wizard.name.setText("首次知识库")
        wizard.directory.setText(str(root / "libraries"))
        wizard.language.setCurrentIndex(1)
        capture(wizard, "04-knowledge-base")
        wizard.primary.click()
        target = wizard.target()
        assert wizard.step == 4 and not target.exists()
        assert not wizard.kb_status.isVisible()
        capture(wizard, "05-review")
        wizard.reject()
        assert not target.exists() and read_connection_state().state == "passed"
        checks.append(
            "Cancelling at review creates no KB and retains the verified global connection"
        )
        requests = len(boundary.requests)
        window._create_kb()
        wizard = window._creation_dialog
        wait(lambda: wizard.step == 3)
        assert len(boundary.requests) == requests
        wizard.name.setText(target.name)
        wizard.directory.setText(str(target.parent))
        wizard.language.setCurrentIndex(1)
        wizard.primary.click()
        wizard.primary.click()
        wait(lambda: window.kb == target and not window.io._callbacks)
        assert read_connection_state(target).state == "passed"
        assert not (target / ".env").exists()
        checks.append(
            "New KB inherits the tested connection without another model request or credential copy"
        )

        # Reproduce the original mismatch: saving identical values at KB scope
        # must not turn a globally-tested connection into another setup prompt.
        apply_kb_config_patch(
            target,
            KbConfigPatchRequest(
                kb=str(target),
                config={"model": "deepseek/deepseek-flash"},
                api_key=retained_key,
                openai_api_base=boundary.url,
            ),
        )
        sent = []
        original_submit = window.manager.submit

        def submit(kb, units, **_):
            sent.append((kb, units))
            return "fixture-dispatch-" + str(len(sent))

        window.manager.submit = submit
        window.conversations.new()
        window.question.setPlainText("这个知识库有哪些资料？")
        window.conversations.send()
        wait(lambda: len(sent) == 1)
        assert sent[0][0] == target and sent[0][1][0].message == "这个知识库有哪些资料？"
        assert window.onboarding.dialog is None
        window.conversations.send()
        assert len(sent) == 1
        checks.append(
            "Actual chat Send dispatches exactly once after configuration, without reopening setup"
        )

        # A genuinely changed credential still needs a fresh proof for the
        # actual target, and completion resumes the captured question only once.
        apply_kb_config_patch(
            target, KbConfigPatchRequest(kb=str(target), api_key="fixture-rotated-key")
        )
        # SettingsDialog.save invalidates the UI cache after its application
        # patch. Use that same invalidation after this acceptance-side patch.
        window.onboarding.invalidate(target)
        window.conversations.new()
        window.question.setPlainText("保留并继续发送这个问题")
        window.conversations.send()
        wait(lambda: window.onboarding.dialog is not None)
        guide = window.onboarding.dialog
        wait(lambda: guide.connection.view is not None)
        assert guide.connection.kb == target
        assert not capture_connection(target).missing
        assert read_connection_state(target).state == "unverified"
        guide.reject()
        retained_connection_proof(target)
        window.onboarding.resume()
        guide = window.onboarding.dialog
        wait(lambda: guide.connection.state is not None)
        assert guide.connection.state.state == "passed"
        while guide.step < 2:
            guide.primary.click()
        guide.primary.click()
        wait(lambda: len(sent) == 2)
        window.onboarding.completed(guide)
        assert len(sent) == 2 and window.onboarding.dialog is None
        assert sent[1][1][0].message == "保留并继续发送这个问题"
        checks.append(
            "Changed credentials retain the question; historical proof resumes its target once"
        )
        window.manager.submit = original_submit

        window._create_kb()
        wizard = window._creation_dialog
        wait(lambda: wizard.step == 3)
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((720, 680), (480, 560)):
                wizard.resize(width, height)
                capture(wizard, f"layout-{theme}-{width}")
        wizard.reject()
        assert not any(w.isVisible() for w in app.topLevelWidgets() if isinstance(w, QDialog))
        checks.append("Light/dark and 480/720 widths keep navigation and primary actions reachable")
        assert len(boundary.requests) == requests == 1
        report = {
            "platform": app.platformName(),
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "checks": checks,
            "verification_mode": "retained-history",
            "model_probe_requests": len(boundary.requests),
            "model_probe_result": "HTTP 401 via the actual SDK; no successful model response",
            "chat_boundary": "production Conversations.send through submission; no paid model call",
        }
    finally:
        if window._creation_dialog:
            window._creation_dialog.reject()
        if window.onboarding.dialog:
            window.onboarding.dialog.reject()
        window.request_quit()
        wait(window.shutdown_complete)
        boundary.close()
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
