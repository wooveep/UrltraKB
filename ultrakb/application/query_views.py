"""Resolve and pin permitted knowledge evidence before creating reading tools."""

from dataclasses import dataclass, field, replace
from pathlib import Path

from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope


@dataclass(frozen=True)
class QueryView:
    scope: KnowledgeScope
    knowledge_revision_id: str | None
    source_revision_ids: tuple[str, ...]
    files: dict[str, str]
    head_generation: int
    validity: str = "current"
    source_revisions_by_path: dict[str, str] = field(default_factory=dict)
    retained_source_status: dict[str, dict] = field(default_factory=dict)
    native_file_roots: dict[str, Path] = field(default_factory=dict)
    native_inputs: dict[str, tuple[str, str]] = field(default_factory=dict)
    native_source_paths: tuple[str, ...] = ()
    source_selectors: dict[str, str] = field(default_factory=dict)
    source_paths_by_unit: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def view_id(self) -> str:
        return self.scope.view_id

    @property
    def provenance(self) -> str:
        return (
            f"Source revisions: {', '.join(self.source_revision_ids) or 'unrecorded'}; "
            f"knowledge={self.knowledge_revision_id or 'current wiki'}; status={self.validity}"
        )


@dataclass(frozen=True)
class QuerySelection:
    kb_dir: Path
    views: tuple[QueryView, ...]
    missing: tuple[str, ...] = ()

    def __post_init__(self):
        if len(self.views) > 1:
            raise ValueError("Multiple source collections are unsupported; bind one at application")

    @property
    def has_evidence(self) -> bool:
        return any(
            name.startswith("sources/") and name.endswith((".md", ".json"))
            for view in self.views
            for name in view.files
        )


def _pin_view(kb_dir: Path, scope: KnowledgeScope) -> QueryView:
    from ultrakb.storage.knowledge_validity import publication_validity

    assert scope.knowledge is not None
    return replace(
        scope.reading_view,
        validity="historical"
        if scope.historical
        else publication_validity(scope.knowledge)["validity"],
    )


def resolve_query_views(
    kb_dir: Path, question: str = "", *, scope: KnowledgeScope | None = None
) -> QuerySelection:
    """Pin the shared main-store view before creating any model reading tools."""
    scope = resolve_scope(kb_dir, scope)
    with reading_scope(scope):
        return QuerySelection(scope.kb_dir, (_pin_view(scope.kb_dir, scope),))


def resolve_read_path(view: QueryView, path: str) -> str:
    """Resolve only an exact, same-directory Markdown alias in the frozen inventory."""
    if Path(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Access denied: use a relative path from index.md.")
    if path in view.files:
        return path
    if (
        path.split("/", 1)[0] in {"sources", "summaries", "concepts", "entities"}
        and not path.endswith(".md")
        and path + ".md" in view.files
    ):
        return path + ".md"
    raise ValueError("Document not found in this reading snapshot. Read index.md for exact paths.")


def read_selected_file(view: QueryView, path: str) -> bytes:
    """Read and verify the exact bytes bound to this request, including catalog inputs."""
    import hashlib

    assert view.scope.knowledge is not None
    if path not in view.files:
        raise ValueError("Document or image is outside this reading snapshot")
    if path in view.native_inputs:
        unit, original = view.native_inputs[path]
        data = view.scope.knowledge.read_source(unit, original)
    else:
        data = view.scope.knowledge.read(path)
    if hashlib.sha256(data).hexdigest() != view.files[path]:
        raise ValueError("Evidence does not match its publication manifest")
    return data


def read_query_page(
    selection: QuerySelection, path: str, *, view_id: str, strict: bool = False
) -> str:
    """A caller can only read the evidence captured in this selection."""
    view = next((view for view in selection.views if view.view_id == view_id), None)
    if view is None:
        raise ValueError("Requested view is outside this question's evidence scope")
    try:
        data = read_selected_file(view, path)
    except ValueError as error:
        if strict:
            raise
        return "No permitted evidence for this page. " + str(error)
    if path.endswith(".md"):
        from ultrakb.knowledge_evidence import page_evidence

        evidence = page_evidence(view.scope, path)
        if view.scope.knowledge is not None:
            view = replace(view, validity=evidence["validity"])
        if evidence["source_revision_ids"]:
            view = replace(view, source_revision_ids=evidence["source_revision_ids"])
    import json

    from ultrakb.source_context import reading_metadata

    metadata = json.dumps(reading_metadata(view, path, data), ensure_ascii=False)
    return f"{view.provenance}\nReading metadata: {metadata}\n\n{data.decode('utf-8')}"


def selection_current(selection: QuerySelection) -> bool:
    """A finished answer must not claim verification against a changed knowledge head."""
    from ultrakb.storage.knowledge_store import KnowledgeStore

    for view in selection.views:
        bound = view.scope.knowledge
        if bound is None:
            return False
        if KnowledgeStore(bound.root, bound.identity).bind(view_id=view.view_id).publication_id != (
            bound.publication_id
        ):
            return False
    return True
