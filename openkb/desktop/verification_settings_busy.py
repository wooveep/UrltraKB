"""Settings must remain readable while a document task owns the KB write lease."""

from __future__ import annotations

import threading


def verify_settings_busy(window, kb, wait_until):
    from openkb.desktop.settings import SettingsDialog
    from openkb.desktop.verification_workbench import button
    from openkb.locks import kb_ingest_lock
    from openkb.mutation import mutation_scope

    window.open_knowledge_base(kb)
    wait_until(lambda: window.kb == kb and window.page is not None)
    busy, release = threading.Event(), threading.Event()

    def task():
        with (
            kb_ingest_lock(kb / ".openkb"),
            mutation_scope(kb, [kb / "wiki/index.md"], operation="compile"),
        ):
            busy.set()
            release.wait(20)

    holder = threading.Thread(target=task)
    holder.start()
    try:
        assert busy.wait(5)
        # Exercise the same navigation and callback chain as the user's click.
        button(window, "设置").click()
        window.workspaces.settings_tabs.setCurrentIndex(1)
        panel = next(p for p in window.findChildren(SettingsDialog) if p.isVisible())
        wait_until(lambda: panel._loaded, timeout=3)
        assert holder.is_alive() and not release.is_set()
        assert panel.form.isEnabled() and panel.editors.isEnabled()
        assert "正在读取" not in panel.status.text()
        language = panel.fields["language"]
        language.action.setCurrentIndex(1)
        language.text.setText("Chinese")
        panel.save()
        assert panel._saving
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()
    wait_until(lambda: not panel._saving)
    assert panel._loaded and panel.form.isEnabled()
    assert panel.fields["language"].text.placeholderText() == "Chinese"
