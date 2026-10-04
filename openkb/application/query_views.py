"""Resolve and pin permitted knowledge evidence before creating reading tools."""

import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from openkb.application.views import list_views, view_scope
from openkb.ingest_records import ImportUnit, KnowledgeRevision, UnitRevision
from openkb.knowledge_scope import KnowledgeScope, resolve_scope
from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.source_catalog import read_record, read_source_revision, record_path, write_record
from openkb.state import HashRegistry
from openkb.unit_publication import read_head, wiki_versions
from openkb.view_records import DocumentFamily, FamilyDefault, KnowledgeView


@dataclass(frozen=True)
class QueryView:
    scope: KnowledgeScope
    product: str | None
    applicable_versions: tuple[str, ...]
    knowledge_revision_id: str | None
    source_revision_ids: tuple[str, ...]
    files: dict[str, str]
    head_generation: int
    reference_only: bool = False
    validity: str = "current"

    @property
    def view_id(self) -> str:
        return self.scope.view_id

    @property
    def provenance(self) -> str:
        version = ", ".join(self.applicable_versions) or "unspecified version"
        return (
            "Historical reference only; not evidence for the requested version. "
            if self.reference_only
            else ""
        ) + (
            f"Evidence: {self.product or 'unspecified product'} / {version}; "
            f"view={self.view_id}; knowledge={self.knowledge_revision_id or 'legacy snapshot'}; "
            f"source revisions={', '.join(self.source_revision_ids) or 'legacy, unrecorded'}; "
            f"status={self.validity}"
        )


@dataclass(frozen=True)
class QuerySelection:
    kb_dir: Path
    views: tuple[QueryView, ...]
    missing: tuple[str, ...] = ()


def _input_family(kb_dir: Path, identity: str, view_id: str) -> str | None:
    from openkb.knowledge_evidence import validated_input

    annotation = validated_input(kb_dir, identity, view_id)[2]
    return annotation.family_id if annotation else None


def _published_inputs(kb_dir: Path, view_id: str) -> set[str]:
    inputs = set(read_head(kb_dir, view_id).inputs.values())
    for path in (kb_dir / ".openkb/knowledge" / view_id / "revisions").glob("*/manifest.json"):
        manifest = KnowledgeRevision.model_validate_json(path.read_text())
        if manifest.view_id != view_id or manifest.knowledge_revision_id != path.parent.name:
            raise ValueError("Knowledge snapshot belongs to another view")
        inputs.update(manifest.input_revisions)
    return inputs


def list_family_defaults(kb_dir: Path) -> tuple[dict, ...]:
    """Expose each series' explicit choice and its published version candidates."""
    with kb_read_lock(kb_dir / ".openkb"):
        families = [
            read_record(kb_dir, "families", path.stem, DocumentFamily)
            for path in sorted((kb_dir / ".openkb/catalog/families").glob("*.json"))
        ]
        views = list_views(kb_dir)
        result = []
        for family in families:
            path = record_path(kb_dir, "family-defaults", family.family_id)
            selected = (
                read_record(kb_dir, "family-defaults", family.family_id, FamilyDefault)
                if path.exists()
                else None
            )
            available = tuple(
                view
                for view in views
                if view.unknown_source_id is None
                and view.view_id != "legacy"
                and any(
                    _input_family(kb_dir, identity, view.view_id) == family.family_id
                    for identity in _published_inputs(kb_dir, view.view_id)
                )
            )
            if (
                selected
                and selected.view_id is not None
                and selected.view_id not in {view.view_id for view in available}
            ):
                raise ValueError("Stored default does not belong to its published document family")
            result.append(
                {
                    "family_id": family.family_id,
                    "purpose": family.purpose,
                    "product_id": family.product_id,
                    "view_id": selected.view_id if selected else None,
                    "available_views": available,
                }
            )
        return tuple(result)


def select_default_view(kb_dir: Path, family_id: str, view_id: str | None) -> FamilyDefault:
    """Only an explicit selection changes a family's default; imports never call this."""
    from openkb.catalog_schema import CatalogSchema, catalog_schema_path

    with kb_ingest_lock(kb_dir / ".openkb"):
        choices = next(
            (item for item in list_family_defaults(kb_dir) if item["family_id"] == family_id), None
        )
        if choices is None or (
            view_id is not None
            and view_id not in {view.view_id for view in choices["available_views"]}
        ):
            raise ValueError("Default must be a published version belonging to this family")
        choice = FamilyDefault(family_id=family_id, view_id=view_id)
        path = record_path(kb_dir, "family-defaults", family_id)
        schema = catalog_schema_path(kb_dir)
        with mutation_scope(kb_dir, [path, schema], operation="select-default-version"):
            write_record(path, choice)
            write_record(schema, CatalogSchema())
        return choice


def _pin_view(
    kb_dir: Path,
    view: KnowledgeView,
    scope: KnowledgeScope | None = None,
    *,
    defaults: dict[str, str] | None = None,
) -> QueryView | None:
    head = read_head(kb_dir, view.view_id)
    if view.view_id != "legacy" and head.knowledge_revision_id is None:
        return None
    selected = (
        scope
        if scope and scope.read_only
        else view_scope(kb_dir, view.view_id, historical_revision=head.knowledge_revision_id)
    )
    manifest = (
        KnowledgeRevision.model_validate_json(
            (selected.wiki_dir.parent / "manifest.json").read_text()
        )
        if selected.read_only
        else None
    )
    if manifest and manifest.view_id != selected.view_id:
        raise ValueError("Knowledge snapshot belongs to another view")
    inputs = manifest.input_revisions if manifest else tuple(head.inputs.values())
    if view.view_id != "legacy" and not inputs:
        return None
    from openkb.source_changes import effective_input

    historical = bool(scope and scope.read_only)
    families = {identity: _input_family(kb_dir, identity, view.view_id) for identity in inputs}
    allowed = tuple(
        identity
        for identity in inputs
        if (not defaults or defaults.get(families[identity] or "", view.view_id) == view.view_id)
        and (historical or effective_input(kb_dir, view.view_id, identity))
    )
    if inputs and not allowed:
        return None
    sources = []
    source_paths: set[str] = set()
    image_roots: list[str] = []
    for identity in allowed:
        unit = read_record(kb_dir, "unit-revisions", identity, UnitRevision)
        sources.append(read_source_revision(kb_dir, unit.source_revision_id).source_revision_id)
        named = read_record(kb_dir, "units", unit.unit_id, ImportUnit)
        if selected.read_only:
            from openkb.application.sources import _snapshot_source
            from openkb.source_map import read_source_map

            saved = _snapshot_source(kb_dir, selected, named.unit_id)
            if saved and saved[1].source_map:
                read_source_map(selected.wiki_dir, saved[1].source_map, named.doc_name)
        source_paths.update((f"sources/{named.doc_name}.md", f"sources/{named.doc_name}.json"))
        source_paths.add(f"sources/{named.doc_name}.content.json")
        image_roots.append(f"sources/images/{named.doc_name}/")
    files = wiki_versions(kb_dir, selected.wiki_dir)
    if not historical:
        files = {name: digest for name, digest in files.items() if name not in head.needs_refresh}
    if manifest and set(inputs) != set(allowed):
        files = {
            name: digest
            for name, digest in files.items()
            if name in source_paths
            or any(name.startswith(prefix) for prefix in image_roots)
            or (
                name in manifest.page_dependencies
                and bool(manifest.page_dependencies[name])
                and set(manifest.page_dependencies[name]).issubset(allowed)
            )
        }
    return QueryView(
        selected,
        view.product,
        view.applicable_versions,
        manifest.knowledge_revision_id if manifest else head.knowledge_revision_id,
        tuple(sorted(set(sources))),
        files,
        head.generation,
        validity="historical" if historical else "current",
    )


def _version_key(value: str) -> str:
    return re.sub(r"^v(?=\d)", "", value.rstrip("."), flags=re.IGNORECASE).casefold()


def _requested_versions(question: str, labels: set[str]) -> set[str]:
    values = re.findall(
        r"(?:\bv(?=\d)|\bversions?\s*[:：]?\s*|版本\s*[:：]?\s*)"
        r"([\"'‘“]?)([A-Za-z0-9][A-Za-z0-9_.-]*)",
        question,
        re.IGNORECASE,
    )
    known = {_version_key(label) for label in labels}
    requested = {
        _version_key(value)
        for quoted, value in values
        if quoted or any(char.isdigit() for char in value) or _version_key(value) in known
    }
    for label in labels:
        if re.search(
            r"(?:\bversions?\s*[:：]?\s*|版本\s*[:：]?\s*)[\"'‘“]?"
            + re.escape(label)
            + r"(?=$|[\s.,?!，。？！\"'’”])",
            question,
            re.IGNORECASE,
        ):
            requested.add(_version_key(label))
    return requested


def _resolve_product_candidates(
    views: tuple[KnowledgeView, ...], question: str
) -> tuple[set[str], tuple[str, ...]]:
    """Discover names without treating spelling variants as confirmed identities."""

    def normalize(value: str) -> str:
        return re.sub(r"[\s\-‐‑‒–—]+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()

    text = normalize(question)
    names = {
        view.product_id: normalize(view.product)
        for view in views
        if view.product_id and view.product
    }
    mentions: list[tuple[int, int, str]] = []
    for name in set(names.values()):
        pattern = re.escape(name)
        if re.match(r"[a-z0-9_]", name):
            pattern = r"(?<![a-z0-9_])" + pattern
        if re.search(r"[a-z0-9_]$", name):
            pattern += r"(?![a-z0-9_])"
        mentions.extend((match.start(), match.end(), name) for match in re.finditer(pattern, text))
    # A full longer name supersedes a short name at the same location, while
    # separate mentions remain separate (including multi-product comparisons).
    mentions = [
        mention
        for mention in mentions
        if not any(
            other[0] <= mention[0] and mention[1] <= other[1] and other != mention
            for other in mentions
        )
    ]
    selected: set[str] = set()
    ambiguous: set[str] = set()
    for _, _, name in mentions:
        candidates = {
            identity
            for identity, candidate in names.items()
            if candidate == name or candidate.startswith(name + " ")
        }
        selected.update(candidates)
        if len(candidates) > 1:
            ambiguous.update(candidates)
    if not ambiguous:
        return selected, ()
    details = tuple(
        f"Candidate product: {view.product}; product_id={view.product_id}; "
        f"view={view.view_id}; "
        f"applicable versions={', '.join(view.applicable_versions) or 'unknown'}."
        for view in sorted(views, key=lambda view: (view.product or "", view.view_id))
        if view.product_id in ambiguous
    )
    return selected, (
        "Ambiguous product names; no evidence scope was selected.",
        *details,
        "Select a view explicitly (--view / scope.view_id), or confirm product metadata "
        "through the source version review before querying across sources.",
    )


def resolve_query_views(
    kb_dir: Path, question: str = "", *, scope: KnowledgeScope | None = None
) -> QuerySelection:
    """An unspecified selection keeps each available applicability scope separate."""
    root = kb_dir.resolve()
    if scope is not None:
        scope = resolve_scope(root, scope)
    with kb_read_lock(root / ".openkb"):
        views = list_views(root)
        if scope:
            views = tuple(view for view in views if view.view_id == scope.view_id)
        requested = _requested_versions(
            question, {v for view in views for v in view.applicable_versions}
        )
        compare = bool(
            re.search(r"\b(?:compare|versus|vs)\b|比较|对比|差异", question, re.IGNORECASE)
        )
        candidates = views
        if scope is None:
            from openkb.source_catalog import list_sources
            from openkb.source_changes import source_view

            current_views = {
                source_view(root, source) for source in list_sources(root) if not source.removed
            }
            current_products = {view.product_id for view in views if view.view_id in current_views}
            # Old views preserve exact prior applicability after a metadata
            # correction. Infer names from currently confirmed product identities;
            # explicit scopes still retain those older views and their boundaries.
            candidates = tuple(view for view in views if view.product_id in current_products)
        products, ambiguity = (
            _resolve_product_candidates(candidates, question) if scope is None else (set(), ())
        )
        if ambiguity:
            return QuerySelection(root, (), ambiguity)
        history = bool(re.search(r"historical|history|legacy|历史|旧库", question, re.IGNORECASE))
        historical = (
            tuple(
                view
                for view in views
                if (view.unknown_source_id or view.view_id == "legacy")
                and (not products or view.product is None or view.product_id in products)
            )
            if requested and history
            else ()
        )
        if not scope and any(view.view_id != "legacy" for view in views):
            views = tuple(view for view in views if view.view_id != "legacy")
        if products:
            views = tuple(view for view in views if view.product_id in products)
        if requested:
            views = tuple(
                view
                for view in views
                if not view.unknown_source_id
                and view.view_id != "legacy"
                and requested.intersection(_version_key(v) for v in view.applicable_versions)
            )
        defaults = (
            {
                item["family_id"]: item["view_id"]
                for item in list_family_defaults(root)
                if item["view_id"]
            }
            if scope is None and not requested and not compare
            else {}
        )
        selected = tuple(
            pinned
            for view in views
            if (pinned := _pin_view(root, view, scope, defaults=defaults)) is not None
        )
        if not selected and scope is None and not requested:
            selected = tuple(
                pinned
                for view in list_views(root)
                if view.view_id == "legacy" and (pinned := _pin_view(root, view)) is not None
            )
        missing = tuple(
            f"No current evidence for requested version {version}."
            for version in sorted(requested)
            if not any(
                version in {_version_key(v) for v in view.applicable_versions} for view in selected
            )
        )
        if scope is not None and not selected and not missing:
            missing = (f"No permitted published evidence for selected view {scope.view_id}.",)
        selected += tuple(
            replace(pinned, reference_only=True)
            for view in historical
            if (pinned := _pin_view(root, view, scope)) is not None
        )
        return QuerySelection(root, selected, missing)


def read_query_page(selection: QuerySelection, path: str, *, view_id: str) -> str:
    """A caller can only read the evidence captured in this selection."""
    view = next((view for view in selection.views if view.view_id == view_id), None)
    if view is None:
        raise ValueError("Requested view is outside this question's evidence scope")
    target = (view.scope.wiki_dir / path).resolve()
    if not target.is_relative_to(view.scope.wiki_dir) or path not in view.files:
        return "No permitted evidence for this page."
    if not target.is_file() or HashRegistry.hash_file(target) != view.files[path]:
        return "Evidence changed after selection; start a new question."
    if path.endswith(".md"):
        from openkb.knowledge_evidence import page_evidence

        evidence = page_evidence(view.scope, path)
        if evidence["source_revision_ids"]:
            view = replace(view, source_revision_ids=evidence["source_revision_ids"])
    return f"{view.provenance}\n\n{target.read_bytes().decode('utf-8')}"


def selection_current(selection: QuerySelection) -> bool:
    """A finished answer must not claim verification against a changed knowledge head."""
    with kb_read_lock(selection.kb_dir / ".openkb"):
        for view in selection.views:
            if read_head(selection.kb_dir, view.view_id).generation != view.head_generation:
                return False
            if (
                not view.scope.read_only
                and wiki_versions(selection.kb_dir, view.scope.wiki_dir) != view.files
            ):
                return False
        return True
