"""Drive A03/A04 through the production Qt workbench using isolated settings.

Run on an actual desktop for visual evidence; --headless is a separate regression.
No model request or user credential is used in this scenario.
"""

import argparse
import json
import os
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
    for key in ("LLM_API_KEY", "OPENAI_API_BASE", "ULTRAKB_DIR"):
        os.environ.pop(key, None)
    if args.headless:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialogButtonBox, QScrollArea

    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.application.settings import apply_global_config_patch, read_settings_view
    from ultrakb.application.settings_data import GlobalConfigPatchRequest
    from ultrakb.desktop.settings import SettingsDialog
    from ultrakb.desktop.verification import create_application
    from ultrakb.desktop.verification_workbench import button
    from ultrakb.desktop.window import Workbench

    config.GLOBAL_CONFIG_DIR = root / "settings"
    config.GLOBAL_CONFIG_PATH = config.GLOBAL_CONFIG_DIR / "global.yaml"
    apply_global_config_patch(
        GlobalConfigPatchRequest(
            config={"model": "global-model", "language": "zh"},
            api_key="fixture-global-private-key",
            openai_api_base="https://global.example/v1",
        )
    )
    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    platform = app.platformName()
    assert args.headless or platform not in {"offscreen", "minimal"}, platform
    window = Workbench(history_dir=root / "tasks")
    window.resize(1366, 768)
    window.show()
    checks = []

    def wait(condition):
        deadline = time.monotonic() + 30
        while not condition():
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Native settings verification timed out; current KB: {window.kb}; "
                    f"pending reads: {len(window.io._callbacks)}; "
                    f"status: {window.statusBar().currentMessage()}"
                )
        app.processEvents()

    def panel(index):
        button(window, "设置").click()
        window.workspaces.settings_scope.setCurrentIndex(index)
        selected = next(p for p in window.findChildren(SettingsDialog) if p.isVisible())
        wait(lambda: selected.form.isEnabled() and not selected._loading)
        return selected

    def save(selected):
        selected.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        wait(lambda: selected.form.isEnabled() and not selected._saving)
        assert "已保存" in selected.status.text()

    def set_value(selected, key, value):
        field = selected.fields[key]
        for scroll in selected.findChildren(QScrollArea):
            scroll.ensureWidgetVisible(field)
        field.text.setFocus()
        QTest.keyClicks(field.text, value)
        save(selected)
        assert not field.text.text()

    def capture(selected, name):
        for scroll in selected.findChildren(QScrollArea):
            scroll.verticalScrollBar().setValue(0)
        QTest.qWait(60)
        assert window.grab().save(str(root / f"{name}.png"))

    try:
        kb = root / "继承库"
        window._create_kb()
        wizard = window._creation_dialog
        wait(lambda: not wizard.connection.busy)
        wizard.skip.click()
        wizard.name.setText(kb.name)
        wizard.directory.setText(str(root))
        wizard.primary.click()
        wizard.primary.click()
        wait(lambda: window.kb == kb and not window.io._callbacks)
        assert not (kb / ".env").exists()
        assert (kb / ".ultrakb/config.yaml").read_text().strip() == "{}"
        current = panel(1)
        assert current.fields["model"].text.placeholderText() == "global-model"
        assert current.fields["model"].source.text() == "继承全局默认"
        assert current.fields["api_key"].source.text() == "继承全局默认"
        capture(current, "a03-new-kb-inherits")
        checks.append("A03: native empty-directory creation inherits model and key without copies")

        global_panel = panel(0)
        set_value(global_panel, "model", "updated-global")
        current = panel(1)
        assert current.fields["model"].text.placeholderText() == "updated-global"
        checks.append("A03: global save updates the inheriting KB on re-entry")
        set_value(current, "model", "local-model")
        assert current.fields["model"].source.text() == "本库覆盖"
        assert read_settings_view().values.model == "updated-global"
        capture(current, "a04-local-override")
        current.fields["model"].action.setFocus()
        QTest.keyClick(current.fields["model"].action, Qt.Key.Key_End)
        save(current)
        assert current.fields["model"].text.placeholderText() == "updated-global"
        assert current.fields["model"].source.text() == "继承全局默认"
        checks.append("A04: keyboard restore removes only the local override")

        os.environ["LLM_API_KEY"] = "fixture-environment-private-key"
        os.environ["OPENAI_API_BASE"] = "https://environment.example/v1"
        global_panel = panel(0)
        global_panel.reload()
        wait(lambda: not global_panel._loading)
        assert global_panel.fields["api_key"].source.text() == "启动环境"
        assert "优先于全局保存值" in global_panel.status.text()
        set_value(global_panel, "api_key", "fixture-updated-global-private-key")
        assert global_panel.fields["api_key"].source.text() == "启动环境"
        capture(global_panel, "a04-environment-global")
        current = panel(1)
        assert current.fields["api_key"].source.text() == "启动环境"
        assert "private-key" not in read_settings_view(kb).model_dump_json()
        checks.append("A04: environment precedence remains visible after saving global credentials")

        for theme in ("浅色", "深色"):
            window.theme.setCurrentText(theme)
            for width, height in ((1366, 768), (720, 600)):
                window.resize(width, height)
                capture(current, f"settings-{theme}-{width}")
                assert current.buttons.button(QDialogButtonBox.StandardButton.Save).isVisible()
                model = current.fields["model"].text
                assert (
                    model.fontMetrics().horizontalAdvance(model.placeholderText()) < model.width()
                )
        checks.append("Actual Qt: light/dark, 1366×768 and 720×600, save remains reachable")

        legacy = root / "显式配置库"
        initialize_kb(legacy, model="preserved-model", seed_environment=False)
        before = (legacy / ".ultrakb/config.yaml").read_bytes()
        window.open_knowledge_base(legacy)
        wait(lambda: window.kb == legacy and not window.io._callbacks)
        current = panel(1)
        assert current.fields["model"].text.placeholderText() == "preserved-model"
        assert (legacy / ".ultrakb/config.yaml").read_bytes() == before
        checks.append("A04: opening an existing explicit configuration does not migrate it")
        result = {
            "platform": platform,
            "scale": os.environ.get("QT_SCALE_FACTOR", "1"),
            "headless": args.headless,
            "checks": checks,
            "real_service": "not_required: configuration inheritance makes no model request",
        }
    finally:
        window.request_quit()
        wait(window.shutdown_complete)
    (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
