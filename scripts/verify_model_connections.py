"""Drive native A05/A06 controls; --real uses the official budgeted DeepSeek service.

Provider errors and delayed replies use a separately labelled loopback boundary.
Actual Qt, real provider requests and headless runs are recorded independently.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
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

    from connection_probe_server import ProbeServer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialogButtonBox

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.model_connections import read_connection_state
    from ultrakb.application.settings import apply_global_config_patch
    from ultrakb.application.settings_data import GlobalConfigPatchRequest
    from ultrakb.desktop.settings import SettingsDialog
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench
    from ultrakb.locks import atomic_write_text, kb_ingest_lock
    from ultrakb.mutation import mutation_scope

    boundary = ProbeServer()
    live = None
    if args.real:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from live_llm import LiveLLM
        from llm_test_budget import TestRunBudget

        live = LiveLLM(TestRunBudget(root / "cost.json"))
        credentials = live.credentials
        os.environ["LLM_API_KEY"] = credentials.api_key
        os.environ["OPENAI_API_BASE"] = credentials.base_url
    else:
        os.environ["LLM_API_KEY"] = "fixture-starting-key"
        os.environ["OPENAI_API_BASE"] = boundary.url
    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={"model": "deepseek/deepseek-flash" if args.real else "gpt-5.4"}
        )
    )
    kb = root / "连接验证库"
    initialize_kb(kb, seed_environment=False)
    private = kb / "raw/private.txt"
    with (
        kb_ingest_lock(kb / ".ultrakb"),
        mutation_scope(kb, [private], operation="acceptance-input"),
    ):
        atomic_write_text(private, "PRIVATE_SOURCE_DO_NOT_SEND")
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    assert args.headless or app.platformName() not in {"offscreen", "minimal"}
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    if args.diagnostics:
        import faulthandler

        faulthandler.dump_traceback_later(25)

        def observe(sequence, value, error):
            entry = {
                "sequence": sequence,
                "value_type": type(value).__name__,
                "error_type": type(error).__name__,
                "panels": [
                    (p._closed, p.connection.testing) for p in window.findChildren(SettingsDialog)
                ],
            }
            with (root / "io.jsonl").open("a") as stream:
                stream.write(json.dumps(entry) + "\n")

        window.io.completed.connect(observe)
    checks = {"native_ui": [], "real_service": [], "provider_boundary": []}

    def wait(condition, timeout=40):
        end = time.monotonic() + timeout
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > end:
                panels = [
                    (p._closed, p.connection.testing, p.connection.status.text())
                    for p in window.findChildren(SettingsDialog)
                ]
                raise TimeoutError(
                    f"Connection control acceptance did not finish; panels={panels}; "
                    f"pending={len(window.io._callbacks)}"
                )
        app.processEvents()

    def panel(index):
        button(window, "设置").click()
        window.workspaces.settings_scope.setCurrentIndex(index)
        selected = next(p for p in window.findChildren(SettingsDialog) if p.isVisible())
        wait(lambda: selected.form.isEnabled() and not selected._loading)
        wait(lambda: selected.connection._state is not None)
        return selected

    def run(selected):
        previous = selected.connection._state
        QTest.mouseClick(selected.connection.test_button, Qt.MouseButton.LeftButton)
        wait(
            lambda: not selected.connection._testing
            and selected.connection._state is not None
            and selected.connection._state is not previous
        )
        return selected.connection._state

    def snapshot(name):
        QTest.qWait(80)
        assert window.grab().save(str(root / f"{name}.png"))

    try:
        window.open_knowledge_base(kb)
        wait(lambda: window.kb == kb and not window.io._callbacks)
        current = panel(0)
        assert current.connection._state.state == "unverified"
        assert not current.advanced.isVisible()
        assert not boundary.requests and (live is None or not live.requests)
        checks["native_ui"].append(
            "Opening settings does not test automatically; advanced controls start folded"
        )
        current.connection.service.setFocus()
        QTest.keyClick(current.connection.service, Qt.Key.Key_Home)
        for _ in range(2 if args.real else 1):
            QTest.keyClick(current.connection.service, Qt.Key.Key_Down)
        assert "官方准备指引" in current.connection.help.text()
        result = run(current)
        assert result.state == "passed", result
        verified_connection = asdict(result)
        assert "已验证" in current.connection.status.text()
        snapshot("a05-verified-global")
        if live:
            assert len(live.requests) == 2
            assert "PRIVATE_SOURCE_DO_NOT_SEND" not in json.dumps(live.requests)
            checks["real_service"].append(
                "Actual Qt Test-and-continue: 2 official DeepSeek requests through production SDK; "
                "synthetic text/tool round trip"
            )
        else:
            checks["provider_boundary"].append(
                "Production SDK text and function round trip against loopback provider"
            )
        current = panel(1)
        assert current.connection._state.state == "passed"
        checks["native_ui"].append("KB inherits a global pass without another probe")

        os.environ["LLM_API_KEY"] = "fixture-private-key"
        os.environ["OPENAI_API_BASE"] = boundary.url
        current = panel(0)
        current.connection.refresh()
        wait(lambda: current.connection._state.state == "unverified")
        checks["native_ui"].append(
            "Effective credential/address changes invalidate the previous pass"
        )

        for mode in ("authentication", "model", "permission", "quota", "rate_limit"):
            boundary.select(mode)
            result = run(current)
            assert result.state == "failed" and result.code == mode, result
            assert "fixture-private-key" not in current.connection.status.text()
            assert current.form.isEnabled()
            snapshot(f"a06-{mode}")
            checks["provider_boundary"].append(
                f"Native {mode} error is actionable, redacted and preserves editing"
            )

        boundary.select("wait")
        QTest.mouseClick(current.connection.test_button, Qt.MouseButton.LeftButton)
        wait(lambda: current.connection._testing and boundary.started.is_set())
        QTest.mouseClick(current.connection.cancel_button, Qt.MouseButton.LeftButton)
        wait(lambda: not current.connection._testing)
        boundary.release.set()
        assert "已取消" in current.connection.status.text()
        assert read_connection_state().state != "passed"
        checks["provider_boundary"].append(
            "Cancel stops a pending production SDK request and leaves the connection unverified"
        )

        boundary.select("wait")
        QTest.mouseClick(current.connection.test_button, Qt.MouseButton.LeftButton)
        wait(lambda: current.connection._testing and boundary.started.is_set())
        current.fields["model"].text.setFocus()
        QTest.keyClicks(current.fields["model"].text, "openai/changed")
        current.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        wait(lambda: not current._saving and not current.connection._testing)
        boundary.release.set()
        assert read_connection_state().state == "unverified"
        checks["native_ui"].append(
            "Editing/saving during a pending test cannot admit its late result for the new model"
        )

        boundary.select("wait")
        result = run(current)
        boundary.release.set()
        assert result.code == "timeout" and result.state == "failed", result
        snapshot("a06-timeout")
        checks["provider_boundary"].append(
            "20-second deadline cleans pending SDK work and reports a recoverable timeout"
        )

        current.advanced_toggle.setFocus()
        QTest.keyClick(current.advanced_toggle, Qt.Key.Key_Space)
        assert current.advanced.isVisible()
        QTest.keyClick(current.advanced_toggle, Qt.Key.Key_Space)
        assert not current.advanced.isVisible()
        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (720, 600)):
                window.resize(width, height)
                snapshot(f"connection-{theme}-{width}")
        checks["native_ui"].append(
            "Keyboard advanced disclosure and light/dark layout at 1366×768 and 720×600"
        )
        assert "PRIVATE_SOURCE_DO_NOT_SEND" not in json.dumps(boundary.requests)
        evidence = {
            "platform": app.platformName(),
            "headless": args.headless,
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "checks": checks,
            "passed_connection": verified_connection,
            "templates": (
                "OpenAI/DeepSeek/Ollama examples checked against official docs; "
                "live service coverage is DeepSeek only"
            ),
        }
        if live:
            evidence["cost"] = live.budget.report()
    finally:
        boundary.release.set()
        window.request_quit()
        wait(window.shutdown_complete)
        boundary.close()
        if live:
            live.close()
    (root / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
