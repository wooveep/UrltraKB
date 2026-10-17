"""Visible, cancellable reading requests, independent of removal confirmation state."""


def load_document(panel, identity, label, operation, callback, *, snapshot=False):
    window = panel.window
    generation = getattr(panel, "_navigation_generation", 0) + 1
    panel._navigation_generation = generation
    navigation = window.shell.navigation_revision

    def obsolete():
        return (
            panel._closed
            or window._quitting
            or panel.kb != window.kb
            or generation != panel._navigation_generation
            or navigation != window.shell.navigation_revision
            or identity not in panel.rows
        )

    def notice(text):
        panel._navigation_notice = text
        panel.status.setText(text)

    def loaded(value, error):
        panel._navigation_notice = None
        panel.status.setText(f"已完成{label}。")
        callback(value, error)

    panel._navigation_identity = identity
    notice(f"正在{label}，请稍候…")
    window.io.submit(
        operation,
        loaded,
        kb=panel.kb,
        snapshot=snapshot,
        obsolete=obsolete,
        on_wait=lambda: notice(f"正在等待知识库完成当前写入，随后{label}；可以继续浏览其他页面。"),
    )
