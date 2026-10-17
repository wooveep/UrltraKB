"""Switch complete public reading contexts independently of sessions and settings."""

from pathlib import Path

from ultrakb.application.knowledge import KNOWLEDGE_LABELS, page_title
from ultrakb.application.knowledge_bases import get_kb_list, get_kb_status
from ultrakb.application.reading import read_knowledge_context
from ultrakb.application.sessions import list_conversations
from ultrakb.page_ops import EDITABLE_SECTIONS


def display_context(
    window, context, scope, *, anchor="", entries=None, on_ready=None, rendered=False
):
    page = context.page
    root, request_id = window.kb, window._page_request_id
    open_id = window._open_request_id

    def current():
        from ultrakb.lifecycle import current_generation
        from ultrakb.mutation import RecoveryRequired

        if (
            window.kb != root
            or window._page_request_id != request_id
            or window._open_request_id != open_id
            or root in window._deleting_kbs
        ):
            return False
        try:
            return current_generation(root) == scope.publication.generation
        except (OSError, ValueError, RecoveryRequired):
            return False

    def ready():
        window._keep_draft()
        window._knowledge_scope = scope
        window.page = page
        window._page_loading = False
        window._requested_page = None
        if entries is not None:
            window.workspaces.knowledge.set_entries(entries)
        window.workspaces.select_page(page.path)
        window.page_context.show_context(window.kb, context)
        editable = not scope.historical and page.path.split("/")[0] in EDITABLE_SECTIONS
        window.save_button.setEnabled(editable)
        window.editor.setReadOnly(not editable)
        draft = window._drafts.get((str(window.kb), window.view_id or "legacy", page.path))
        window.editor.blockSignals(True)
        window.editor.setPlainText(draft.body if draft else page.body)
        window.editor.blockSignals(False)
        section = KNOWLEDGE_LABELS.get(page.path.split("/")[0], "阅读")
        window.location.setText(f"{section}  /  {page_title(page.content, Path(page.path).name)}")
        window.workspaces.ask_page.setEnabled(True)
        panel = window.workspaces.panels.get("资料")
        if panel is not None and not panel[0]._closed:
            panel[0].reload(preserve_result=True, scope=scope)
        if on_ready is not None:
            on_ready()

    if rendered:
        ready()
        return
    window.reader.show_markdown(
        page.body,
        (scope.wiki_dir / page.path).parent,
        anchor=anchor,
        preserve=True,
        on_ready=ready,
        is_current=current,
    )


def apply_knowledge(window, knowledge, *, on_ready=None, rendered=False):
    window._keep_draft()
    window._page_request_id += 1
    if knowledge.context is not None:
        display_context(
            window,
            knowledge.context,
            knowledge.scope,
            entries=knowledge.entries,
            on_ready=on_ready,
            rendered=rendered,
        )
    else:
        window._page_loading = False
        window._requested_page = None
        window._knowledge_scope = knowledge.scope
        window.workspaces.knowledge.set_entries(knowledge.entries)
        window.page = None
        window.page_context.clear()
        window.location.setText("我的知识")
        window.reader.show_temporary(
            "还没有知识页面。\n\n从「资料」导入文件或网址，整理完成后即可阅读、关联和提问。"
        )
        panel = window.workspaces.panels.get("资料")
        if panel is not None and not panel[0]._closed:
            panel[0].reload(preserve_result=True, scope=knowledge.scope)
        if on_ready is not None:
            on_ready()


def refresh(window):
    root = window.kb
    if root is None or root in window._deleting_kbs:
        return
    view_id, request_id = window.view_id, window._open_request_id
    page_sequence = window._page_request_id
    requested_path = window.page.path if window.page else None
    window._knowledge_request_id += 1
    sequence = window._knowledge_request_id

    def obsolete():
        return (
            window.kb != root
            or window.view_id != view_id
            or window._open_request_id != request_id
            or window._knowledge_request_id != sequence
            or window._page_request_id != page_sequence
            or root in window._deleting_kbs
        )

    def loaded(knowledge, error):
        nonlocal page_sequence

        if obsolete() or window._error(error):
            return
        apply_knowledge(
            window, knowledge, on_ready=lambda: load_details(window, knowledge.scope, obsolete)
        )
        page_sequence = window._page_request_id

    window.io.submit(
        lambda: read_knowledge_context(root, requested_path, view_id=view_id or "legacy"),
        loaded,
        kb=root,
        snapshot=True,
        obsolete=obsolete,
    )
    load_sessions(window, root, view_id, obsolete)


def load_details(window, scope, obsolete):
    root = scope.kb_dir

    def loaded(value, error):
        if obsolete() or window._knowledge_scope != scope or window._error(error):
            return
        info, status = value
        window.workspaces.overview_loaded(info, status)
        window.statusBar().showMessage(f"{root.name} · {info.get('document_count', 0)} 份资料")

    window.io.submit(
        lambda: (get_kb_list(root, scope=scope), get_kb_status(root, scope=scope)),
        loaded,
        kb=root,
        snapshot=True,
        obsolete=obsolete,
    )


def load_sessions(window, root, view_id, obsolete):
    def loaded(sessions, error):
        if not obsolete() and not window._error(error):
            window.conversations.catalog_loaded(sessions)

    window.io.submit(
        lambda: list_conversations(root, view_id=view_id), loaded, kb=root, obsolete=obsolete
    )
