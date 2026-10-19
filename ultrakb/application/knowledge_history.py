"""Read one retained revision and its assets without consulting the current wiki."""

from contextlib import contextmanager
from dataclasses import replace

from ultrakb.application.pages import read_page
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope


@contextmanager
def history_reading(scope, cancelled=None):
    """A cancellable real read, with no legacy generation or ingest binding."""
    if scope.knowledge is not None:
        from ultrakb.storage.coordination import storage_stage

        with storage_stage(
            scope.kb_dir, scope.knowledge.identity, read_only=True, cancelled=cancelled
        ):
            yield
    else:
        from ultrakb.locks import kb_read_lock

        with kb_read_lock(scope.kb_dir / ".ultrakb", cancelled=cancelled), reading_scope(scope):
            yield


def _selection(kb_dir, revision_id, scope):
    selected = resolve_scope(kb_dir, scope)
    if selected.knowledge is not None:
        from ultrakb.storage.knowledge_history import KnowledgeHistory

        bound = KnowledgeHistory(selected.knowledge).revision(revision_id)
        return KnowledgeScope(selected.kb_dir, selected.wiki_dir, selected.view_id, knowledge=bound)
    from .views import view_scope

    return view_scope(selected.kb_dir, selected.view_id, historical_revision=revision_id)


def history_pages(kb_dir, revision_id, *, scope, after=None, limit=63, cancelled=None):
    from ultrakb.storage.knowledge_history import KnowledgeHistory, require_limit

    selected = resolve_scope(kb_dir, scope)
    require_limit(limit)
    with history_reading(selected, cancelled):
        if selected.knowledge is not None:
            return KnowledgeHistory(selected.knowledge).pages(revision_id, after=after, limit=limit)
        from ultrakb.knowledge_scope import wiki_page_paths

        historical = _selection(kb_dir, revision_id, selected)
        return tuple(path for path in wiki_page_paths(historical) if path > (after or ""))[:limit]


def read_history_page(kb_dir, revision_id, path, *, scope, cancelled=None):
    """Return body and captured images from the exact same immutable publication."""
    selected = resolve_scope(kb_dir, scope)
    with history_reading(selected, cancelled):
        historical = _selection(kb_dir, revision_id, selected)
        page = replace(read_page(kb_dir, path, scope=historical), validity="historical")
        if historical.knowledge is not None:
            from .reading_assets import capture_reading_assets

            assets = capture_reading_assets(historical, page.path + ".md", page.body)
        else:
            assets = (historical.wiki_dir / page.path).parent
        return page, assets
