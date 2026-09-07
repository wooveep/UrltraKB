"""Exercise native settings controls against real files and LocalIO callbacks."""

from __future__ import annotations


def verify_settings(window, kb, wait_until):
    from openkb import config
    from openkb.application.settings import read_settings_view
    from openkb.desktop.settings import SettingsDialog
    from openkb.locks import atomic_write_json
    from openkb.mutation import repair_marker

    dialog = SettingsDialog(window.io, kb, window)
    dialog.show()
    wait_until(lambda: dialog.form.isEnabled())
    try:
        language, key = dialog.fields["language"], dialog.fields["api_key"]
        language.action.setCurrentIndex(1)
        language.text.setText("Chinese")
        key.action.setCurrentIndex(1)
        key.text.setText("native-settings-fixture-secret")
        dialog.save()
        wait_until(lambda: dialog.form.isEnabled())
        saved = read_settings_view(kb)
        assert saved.values.language == "Chinese" and saved.sources["language"] == "kb"
        assert saved.values.has_api_key and saved.sources["api_key"] == "kb"
        assert "native-settings-fixture-secret" not in saved.model_dump_json()
        assert not key.text.text()
        language.action.setCurrentIndex(2)
        key.action.setCurrentIndex(2)
        dialog.save()
        wait_until(lambda: dialog.form.isEnabled())
        cleared = read_settings_view(kb)
        assert cleared.sources["language"] != "kb" and cleared.sources["api_key"] != "kb"
    finally:
        dialog.reject()

    with config._with_global_config_lock():
        atomic_write_json(repair_marker(config.GLOBAL_CONFIG_DIR), {"error_type": "Fixture"})
    dialog = SettingsDialog(window.io, None, window)
    dialog.show()
    try:
        wait_until(lambda: "失败" in dialog.status.text())
        assert not dialog.form.isEnabled()
        dialog.repair()
        wait_until(lambda: dialog.form.isEnabled())
        assert not repair_marker(config.GLOBAL_CONFIG_DIR).exists()
    finally:
        dialog.reject()

    dialog = SettingsDialog(window.io, None, window)
    dialog.show()
    wait_until(lambda: dialog.form.isEnabled())
    try:
        language = dialog.fields["language"]
        language.action.setCurrentIndex(1)
        language.text.setText("English")
        dialog.save()
        wait_until(lambda: dialog.form.isEnabled())
        saved = read_settings_view()
        assert saved.values.language == "English" and saved.sources["language"] == "global"
        language.action.setCurrentIndex(2)
        dialog.save()
        wait_until(lambda: dialog.form.isEnabled())
        assert read_settings_view().sources["language"] == "default"
    finally:
        dialog.reject()
