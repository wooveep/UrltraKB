"""Capture a source row as advisory chat focus, retaining KB and draft ownership."""

from ultrakb.application.focus import focus_documents


def ask_from_document(panel, identity):
    root, scope = panel.kb, panel.scope

    def loaded(values, error):
        if error or not values or not values[0].available:
            panel.status.setText(
                str(error) if error else values[0].reason if values else "没有可用资料。"
            )
            panel.details.show()
            return
        if panel.window.kb != root:
            return
        controller = panel.window.conversations
        if controller.active is None:
            controller.new()
        controller.active.focus = (identity,)
        controller.render(restore_draft=False)
        panel.window.shell.navigate("对话")
        panel.window.question.setFocus()

    from ultrakb.desktop.document_navigation import load_document

    load_document(
        panel,
        identity,
        "打开资料提问",
        lambda: focus_documents(root, (identity,), scope=scope),
        loaded,
    )


def update_header(controller):
    window, chat = controller.window, controller.active
    if not hasattr(window, "focus_caption"):
        return
    if not chat:
        window.focus_caption.setText("请先打开知识库；关注资料仅为检索提示。")
        window.focus_clear.hide()
        window.focus_recover.hide()
        return
    selected = chat.request_focus if chat.running else chat.focus
    key = id(chat), chat.root, selected
    if getattr(controller, "_focus_key", None) == key:
        return
    controller._focus_key = key
    window.focus_clear.setVisible(bool(selected) and not chat.running)
    window.focus_recover.setVisible(bool(selected))
    window.focus_caption.setText(
        f"当前知识库：{chat.root.name}\n关注资料："
        + ("正在核对…" if selected else "未选择；按本库提问。")
    )
    controller._focus_generation = getattr(controller, "_focus_generation", 0) + 1
    generation = controller._focus_generation
    if not selected:
        return
    scope = window.scope

    def loaded(values, error):
        if error:
            window.focus_caption.setText(
                f"当前知识库：{chat.root.name}\n关注资料读取失败，请到资料页核对。"
            )
            return
        names = "；".join(f"{value.name}（{value.status}）" for value in values)
        notes = "；".join(value.reason for value in values if not value.available)
        window.focus_caption.setText(
            f"当前知识库：{chat.root.name}\n关注资料：{names}\n优先检索；仍可使用本库其他相关资料。"
            + (f"\n{notes}" if notes else "")
        )

    window.io.submit(
        lambda: focus_documents(chat.root, selected, scope=scope),
        loaded,
        kb=chat.root,
        obsolete=lambda: controller.active is not chat
        or controller._focus_generation != generation
        or window.kb != chat.root,
    )
