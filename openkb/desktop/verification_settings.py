"""Exercise native settings controls against real files and LocalIO callbacks."""

from __future__ import annotations


def verify_settings(window, kb, wait_until):
    from openkb import config
    from openkb.application.settings import read_settings_view
    from openkb.desktop.settings import SettingsDialog
    from openkb.desktop.verification_workbench import button
    from openkb.locks import atomic_write_json
    from openkb.mutation import repair_marker

    window.open_knowledge_base(kb)
    wait_until(lambda: window.kb == kb and window.page is not None)
    button(window, "设置").click()
    window.workspaces.settings_tabs.setCurrentIndex(1)
    dialog = next(p for p in window.findChildren(SettingsDialog) if p.isVisible())
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
        verify_processing_settings(dialog, kb, wait_until)
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


def verify_processing_settings(dialog, kb, wait_until):
    """Exercise budget persistence and independent backend settings without OCR calls."""
    from openkb.application.settings import read_settings_view

    processing = dialog.fields["processing"]
    effective = read_settings_view(kb).values.processing
    assert processing.value() == effective
    if effective["max_tokens"] is None:
        assert processing.values.inputs["max_tokens"].text() == "0"
    budgets = {
        "context_tokens": 8192,
        "output_tokens": 512,
        "max_context_tokens": 8192,
        "max_output_tokens": 512,
        "request_timeout": 5,
        "stage_timeout": 30,
        "document_timeout": 60,
        "cleanup_timeout": 5,
        "max_attempts": 1,
        "max_requests": 5,
        "max_tokens": 20000,
        "concurrency": 1,
    }
    for name, value in budgets.items():
        processing.values.inputs[name].setText(str(value))
    processing.action.setCurrentIndex(1)
    ocr = dialog.fields["parsing"]
    ocr.backend.setCurrentIndex(1)
    ocr.enabled["cloud"].setChecked(True)
    for key, value in {
        "endpoint": "https://ocr.example.test/api/v2/ocr/jobs",
        "model": "PaddleOCR-VL-1.6",
    }.items():
        ocr.forms["cloud"]["identity"].inputs[key].setText(value)
    cloud_limits = {
        "seconds": 60,
        "max_pages": 1,
        "request_seconds": 5,
        "poll_seconds": 1,
        "max_requests": 3,
        "max_page_bytes": 1000000,
        "max_download_bytes": 1000000,
    }
    for key, value in cloud_limits.items():
        ocr.forms["cloud"]["limits"].inputs[key].setText(str(value))
    ocr.action.setCurrentIndex(1)
    key = dialog.fields["ocr_api_key"]
    key.action.setCurrentIndex(1)
    key.text.setText("native-ocr-key-fixture")
    dialog.save()
    wait_until(lambda: dialog.form.isEnabled())
    saved = read_settings_view(kb)
    assert saved.values.processing == budgets
    assert saved.values.parsing.ocr.backend == "cloud"
    assert saved.values.has_ocr_api_key and saved.sources["ocr_api_key"] == "kb"
    assert "native-ocr-key-fixture" not in saved.model_dump_json()
    assert not key.text.text() and "已设置" in key.text.placeholderText()
    assert key.text.echoMode() == key.text.EchoMode.Password
    assert "credential_env" not in ocr.forms["cloud"]["identity"].inputs
    key.action.setCurrentIndex(1)
    key.text.setText("native-ocr-key-rotated")
    dialog.save()
    wait_until(lambda: dialog.form.isEnabled())
    assert read_settings_view(kb).values.parsing == saved.values.parsing
    ocr.backend.setCurrentIndex(0)
    ocr.action.setCurrentIndex(1)
    dialog.save()
    wait_until(lambda: dialog.form.isEnabled())
    switched = read_settings_view(kb)
    assert switched.values.parsing.ocr.backend == "local"
    assert switched.values.parsing.ocr.cloud == saved.values.parsing.ocr.cloud
    navigation = dialog.fields["navigation"]
    navigation.enabled.setChecked(True)
    for name, value in budgets.items():
        navigation.budget.values.inputs[name].setText(str(value))
    navigation.action.setCurrentIndex(1)
    dialog.save()
    wait_until(lambda: dialog.form.isEnabled())
    saved_navigation = read_settings_view(kb)
    assert saved_navigation.values.navigation.enabled
    assert saved_navigation.values.navigation.processing == budgets
    assert saved_navigation.values.processing == budgets
    assert switched.values.has_ocr_api_key
    for field in (processing, ocr, navigation, key):
        field.action.setCurrentIndex(2)
    dialog.save()
    wait_until(lambda: dialog.form.isEnabled())
    cleared = read_settings_view(kb)
    assert cleared.sources["processing"] != "kb" and cleared.sources["parsing"] != "kb"
    assert cleared.sources["navigation"] != "kb"
    assert not cleared.values.has_ocr_api_key
