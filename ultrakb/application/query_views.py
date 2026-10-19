"""Resolve and pin permitted knowledge evidence before creating reading tools."""

from dataclasses import dataclass, field, replace
from pathlib import Path

from ultrakb.application.views import view_scope
from ultrakb.ingest_records import ImportUnit, KnowledgeRevision
from ultrakb.knowledge_scope import KnowledgeScope, reading_scope, resolve_scope
from ultrakb.locks import kb_read_lock
from ultrakb.source_catalog import read_record
from ultrakb.unit_publication import read_head, wiki_versions


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


def _pin_view(kb_dir: Path, scope: KnowledgeScope | None = None) -> QueryView | None:
    if scope is not None and scope.knowledge is not None:
        from ultrakb.storage.knowledge_validity import publication_validity

        return replace(
            scope.reading_view, validity=publication_validity(scope.knowledge)["validity"]
        )
    public = scope.publication if scope is not None else None
    head = public.head if public is not None else read_head(kb_dir)
    catalog = public.metadata_dir if public is not None else kb_dir
    selected = (
        scope
        if scope and scope.read_only
        else view_scope(kb_dir, "legacy", historical_revision=head.knowledge_revision_id)
    )
    manifest = (
        public.manifest
        if public is not None
        else KnowledgeRevision.model_validate_json(
            (selected.wiki_dir.parent / "manifest.json").read_text()
        )
        if selected.read_only
        else None
    )
    if manifest and manifest.view_id != selected.view_id:
        raise ValueError("Knowledge snapshot belongs to another view")
    inputs = manifest.input_revisions if manifest else tuple(head.inputs.values())
    from ultrakb.source_changes import effective_input

    historical = bool(scope and scope.historical)
    if public is not None:
        from ultrakb.application.published_sources import effective_input
    active = {
        identity
        for identity in inputs
        if historical or effective_input(catalog, selected.view_id, identity)
    }
    from ultrakb.retained_reprocessing import retained_reprocessing_input

    if public is not None:
        from ultrakb.application.published_sources import retained_reprocessing_input

    retained = {
        identity: status
        for identity in inputs
        if identity not in active
        and (status := retained_reprocessing_input(catalog, selected.view_id, identity))
    }
    allowed = tuple(identity for identity in inputs if identity in active or identity in retained)
    if inputs and not allowed:
        return None
    sources = []
    source_revisions_by_path: dict[str, str] = {}
    retained_source_status: dict[str, dict] = {}
    source_paths: set[str] = set()
    image_roots: list[str] = []
    image_revisions: dict[str, str] = {}
    native_file_roots: dict[str, Path] = {}
    retained_files: dict[str, str] = {}
    native_inventories: dict[str, set[str]] = {}
    for identity in allowed:
        from ultrakb.knowledge_evidence import validated_input

        unit, source = validated_input(catalog, identity, selected.view_id)
        sources.append(source.source_revision_id)
        named = read_record(catalog, "units", unit.unit_id, ImportUnit)
        if selected.read_only:
            from ultrakb.application.sources import _snapshot_source
            from ultrakb.source_map import read_source_map

            saved = _snapshot_source(kb_dir, selected, named.unit_id)
            if saved and saved[1].source_map:
                if identity in retained or historical:
                    from ultrakb.retained_reprocessing import retained_file_fallbacks

                    roots, files, permitted = retained_file_fallbacks(kb_dir, selected, named)
                    native_file_roots.update(roots)
                    retained_files.update(files)
                    native_inventories[named.doc_name] = permitted
                else:
                    read_source_map(selected.wiki_dir, saved[1].source_map, named.doc_name)
        source_paths.update((f"sources/{named.doc_name}.md", f"sources/{named.doc_name}.json"))
        source_paths.add(f"sources/{named.doc_name}.content.json")
        source_revisions_by_path.update(
            (f"sources/{named.doc_name}{suffix}", unit.source_revision_id)
            for suffix in (".md", ".json", ".content.json")
        )
        if identity in retained:
            retained_source_status.update(
                (f"sources/{named.doc_name}{suffix}", retained[identity])
                for suffix in (".md", ".json", ".content.json")
            )
        image_roots.append(f"sources/images/{named.doc_name}/")
        image_revisions[image_roots[-1]] = unit.source_revision_id
    files = wiki_versions(kb_dir, selected.wiki_dir)
    files.update(retained_files)
    for doc_name, permitted in native_inventories.items():
        aliases = {f"sources/{doc_name}{suffix}" for suffix in (".md", ".json", ".content.json")}
        files = {
            name: value
            for name, value in files.items()
            if name in permitted
            or (name not in aliases and not name.startswith(f"sources/images/{doc_name}/"))
        }
    if not historical:
        files = {
            name: digest
            for name, digest in files.items()
            if name not in head.needs_refresh or name in retained_source_status
        }
    if manifest and set(inputs) != active:
        files = {
            name: digest
            for name, digest in files.items()
            if name in source_paths
            or any(name.startswith(prefix) for prefix in image_roots)
            or (
                name in manifest.page_dependencies
                and bool(manifest.page_dependencies[name])
                and set(manifest.page_dependencies[name]).issubset(active)
            )
        }
    source_revisions_by_path.update(
        (name, revision)
        for name in files
        for prefix, revision in image_revisions.items()
        if name.startswith(prefix)
    )
    return QueryView(
        selected,
        manifest.knowledge_revision_id if manifest else head.knowledge_revision_id,
        tuple(sorted(set(sources))),
        files,
        head.generation,
        validity="historical"
        if historical
        else "current_with_retained_originals"
        if retained
        else "current",
        source_revisions_by_path=source_revisions_by_path,
        retained_source_status=retained_source_status,
        native_file_roots=native_file_roots,
    )


def resolve_query_views(
    kb_dir: Path, question: str = "", *, scope: KnowledgeScope | None = None
) -> QuerySelection:
    """Read the KB by default; only an explicit selection narrows its storage scope.

    Product names and version strings in a question are retrieval terms. They do
    not establish a second admission policy before the agent can read documents.
    Ordinary imports and questions use the shared wiki in a freshly built KB.
    """
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)

    with reading_scope(scope) if scope is not None else kb_read_lock(root / ".ultrakb"):
        pinned = _pin_view(root, scope)
        selected = (pinned,) if pinned is not None else ()
        missing = (
            (f"No published documents in selected collection {scope.view_id}.",)
            if scope is not None and not selected
            else ()
        )
        return QuerySelection(root, selected, missing)


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

    if view.scope.knowledge is not None:
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
    root = view.native_file_roots.get(path, view.scope.wiki_dir).resolve()
    target = (root / path).resolve()
    if Path(path).is_absolute() or not target.is_relative_to(root):
        raise ValueError("Access denied: path escapes wiki root. Use a path from index.md.")
    if path not in view.files:
        raise ValueError("Document or image not found in this reading snapshot. Read index.md.")
    if not target.is_file():
        raise ValueError("Selected document or image is missing; start a new question.")
    data = target.read_bytes()
    if hashlib.sha256(data).hexdigest() != view.files[path]:
        raise ValueError("Evidence changed after selection; start a new question.")
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
    if any(view.scope.knowledge is not None for view in selection.views):
        from ultrakb.storage.knowledge_store import KnowledgeStore

        for view in selection.views:
            bound = view.scope.knowledge
            if bound is None:
                return False
            if KnowledgeStore(bound.root, bound.identity).bind(
                view_id=view.view_id
            ).publication_id != (bound.publication_id):
                return False
        return True
    public_views = [view for view in selection.views if view.scope.publication is not None]
    if public_views:
        from ultrakb.mutation import RecoveryRequired
        from ultrakb.published_knowledge import PublicationUnavailable, read_publication

        try:
            return all(
                read_publication(selection.kb_dir, view_id=view.view_id).publication_id
                == view.scope.publication_id
                for view in public_views
            )
        except (FileNotFoundError, PublicationUnavailable, RecoveryRequired):
            return False
    with kb_read_lock(selection.kb_dir / ".ultrakb"):
        for view in selection.views:
            if read_head(selection.kb_dir, view.view_id).generation != view.head_generation:
                return False
            if (
                not view.scope.read_only
                and wiki_versions(selection.kb_dir, view.scope.wiki_dir) != view.files
            ):
                return False
        return True
