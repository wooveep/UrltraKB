"""Retain a conversion independently of version clarification and model work."""

import hashlib
import json
from pathlib import Path
from typing import Callable

from pydantic import Field

from openkb.config import resolve_effective_config
from openkb.converter import ConvertResult, convert_document
from openkb.file_state import contained_paths
from openkb.inputs import TEXT_SOURCE_EXTENSIONS, PreparedInput
from openkb.mutation import _copy_file_atomic, mutation_scope
from openkb.processing_policy import ProcessingDecision
from openkb.source_catalog import Admission, read_record, record_path, write_record
from openkb.source_records import Digest, Record, RecordId, RelativePath
from openkb.unit_publication import copy_tree, wiki_versions


class NormalizedInput(Record):
    normalization_id: RecordId
    source_revision_id: RecordId
    fingerprint: str
    raw_path: RelativePath
    source_path: RelativePath | None
    is_long: bool
    files: dict[RelativePath, Digest]
    processing: ProcessingDecision | None = None
    pdf_path: RelativePath | None = Field(default=None, exclude_if=lambda value: value is None)
    office_path: RelativePath | None = Field(default=None, exclude_if=lambda value: value is None)
    cnki_path: RelativePath | None = Field(default=None, exclude_if=lambda value: value is None)


def normalization_fingerprint(
    kb_dir: Path,
    *,
    scope=None,
    bundle=None,
    source_revision=None,
    doc_name=None,
    resource_policy=None,
) -> str:
    from importlib.metadata import version

    from openkb.agent.compiler import get_agents_md, short_document_messages
    from openkb.config import resolve_credential_bundle
    from openkb.execution_capacity import capacity_policy
    from openkb.knowledge_scope import resolve_scope
    from openkb.pdf_recognition import PDF_RECOGNITION_POLICY

    config = resolve_effective_config(kb_dir)[0]
    credentials = bundle if bundle is not None else resolve_credential_bundle(kb_dir)
    request_basis = short_document_messages(
        "", "", get_agents_md(resolve_scope(kb_dir, scope).wiki_dir), config.get("language", "en")
    )
    text_policy = {}
    if source_revision and f".{source_revision.source_format}" in TEXT_SOURCE_EXTENSIONS:
        from openkb.content_blocks import BLOCK_POLICY
        from openkb.text_formats import text_normalization_policy
        from openkb.text_measurement import MEASUREMENT_FINGERPRINT

        text_policy = {
            "pipeline": text_normalization_policy(source_revision.source_format),
            "block_policy": BLOCK_POLICY,
            "classification": {"short_max_tokens": 5000, "measurement": MEASUREMENT_FINGERPRINT},
            "input": {
                "body": source_revision.digest,
                **(
                    {"decoding": source_revision.text_decoding.model_dump(mode="json")}
                    if source_revision.text_decoding
                    else {}
                ),
                "doc_name": doc_name,
                "assets": {
                    asset.original_reference: asset.digest for asset in source_revision.assets
                },
            },
        }
        if source_revision.source_format in {"html", "htm"}:
            from openkb.remote_assets import REMOTE_POLICY, resolve_resource_policy

            selected = resource_policy or resolve_resource_policy(kb_dir)
            text_policy["resources"] = {"policy": REMOTE_POLICY, **selected.model_dump(mode="json")}
    from openkb.inputs import OFFICE_SOURCE_EXTENSIONS

    office_policy: dict = {}
    if source_revision and source_revision.source_format in {
        *(extension[1:] for extension in TEXT_SOURCE_EXTENSIONS),
        "xlsx",
        "xls",
    }:
        from pageindex.index.block_policy import BLOCK_INDEX_POLICY

        office_policy["block_index_policy"] = BLOCK_INDEX_POLICY
    if source_revision and source_revision.source_format in {"xlsx", "xls"}:
        from openkb.workbooks.records import WORKBOOK_POLICY, XLS_POLICY

        office_policy["workbook_policy"] = (
            XLS_POLICY if source_revision.source_format == "xls" else WORKBOOK_POLICY
        )
    if source_revision and f".{source_revision.source_format}" in OFFICE_SOURCE_EXTENSIONS:
        from openkb.office.runtime import processing_identity

        office_policy = {"office": processing_identity(kb_dir)}
        if source_revision.source_format in {"pptx", "ppt"}:
            from pageindex.index.page_parts_policy import PAGE_PARTS_INDEX_POLICY

            from openkb.office.slide_content import NOTES_POLICY

            office_policy["notes_policy"] = NOTES_POLICY
            office_policy["page_parts_index_policy"] = PAGE_PARTS_INDEX_POLICY
    if source_revision and source_revision.source_format in {"caj", "kdh"}:
        from openkb.cnki.runtime import processing_identity as cnki_processing_identity

        office_policy["cnki"] = cnki_processing_identity()
    return json.dumps(
        {
            "pipeline": "pdf-physical-v3",
            "pdf_recognition_policy": PDF_RECOGNITION_POLICY,
            "classification": config["pdf_limit"],
            "index_model": config.get("model"),
            "index_policy": "content-based-physical-v1",
            # Runtime injection changed no successful tree content. Keep the
            # established content identity so old paid trees remain reusable.
            "index_sdk": (
                "0.3.0.dev3+urltrakb.5"
                if version("pageindex")
                in {
                    "0.3.0.dev3+urltrakb.6",
                    "0.3.0.dev3+urltrakb.7",
                    "0.3.0.dev3+urltrakb.8",
                }
                else version("pageindex")
            ),
            "index_endpoint": hashlib.sha256(
                (credentials.base_url or "provider-default").encode()
            ).hexdigest(),
            "capacity": capacity_policy(config, custom_endpoint=bool(credentials.base_url)),
            "request_basis": hashlib.sha256(
                json.dumps(request_basis, sort_keys=True).encode()
            ).hexdigest(),
            **text_policy,
            **office_policy,
        },
        sort_keys=True,
    )


def normalization_id(source_revision_id: str, fingerprint: str) -> str:
    return hashlib.sha256(f"{source_revision_id}:{fingerprint}".encode()).hexdigest()[:32]


def read_processing(kb_dir: Path, unit_revision_id: str | None) -> dict | None:
    """Project a retained target decision even before its first successful publication."""
    from openkb.ingest_records import UnitRevision

    if unit_revision_id is None:
        return None
    revision = read_record(kb_dir, "unit-revisions", unit_revision_id, UnitRevision)
    identity = normalization_id(revision.source_revision_id, revision.processing_fingerprint)
    if not record_path(kb_dir, "normalizations", identity).exists():
        return None
    processing = read_normalization(kb_dir, identity)[1].processing
    return processing.model_dump(mode="json") if processing else None


def read_normalization(kb_dir: Path, identity: str) -> tuple[Path, NormalizedInput]:
    saved = read_record(kb_dir, "normalizations", identity, NormalizedInput)
    directory = kb_dir / ".openkb/normalized" / identity
    contained_paths(kb_dir, [directory])
    if wiki_versions(kb_dir, directory) != saved.files:
        raise ValueError("Retained normalization changed or is incomplete")
    if saved.raw_path not in saved.files or (
        saved.source_path and saved.source_path not in saved.files
    ):
        raise ValueError("Retained normalization is missing its body")
    if any(
        path and path not in saved.files
        for path in (saved.pdf_path, saved.office_path, saved.cnki_path)
    ):
        raise ValueError("Retained normalization is missing its conversion artifacts")
    from openkb.office.validation import validate_office_input

    try:
        policy = json.loads(saved.fingerprint)
    except ValueError:
        policy = {}  # Older PDF normalizations retain a plain policy name.
    if saved.cnki_path or (isinstance(policy, dict) and policy.get("cnki")):
        from openkb.cnki.validation import validate_cnki_input

        validate_cnki_input(kb_dir, directory, saved)
    else:
        validate_office_input(kb_dir, directory, saved)
    return directory, saved


def retain_normalization(
    kb_dir: Path,
    admission: Admission,
    prepared: PreparedInput,
    fingerprint: str,
    *,
    check_stop: Callable[[], None] = lambda: None,
    scope=None,
    bundle=None,
    resource_policy=None,
    unit=None,
    sheet=None,
) -> tuple[Path, NormalizedInput]:
    doc_name = unit.doc_name if unit else admission.source.doc_name
    identity = normalization_id(admission.revision.source_revision_id, fingerprint)
    record = record_path(kb_dir, "normalizations", identity)
    if record.exists():
        directory, saved = read_normalization(kb_dir, identity)
        if (
            saved.source_revision_id != admission.revision.source_revision_id
            or saved.fingerprint != fingerprint
        ):
            raise ValueError("Normalization belongs to another input")
        return directory, saved
    current_policy = json.loads(
        normalization_fingerprint(
            kb_dir,
            scope=scope,
            bundle=bundle,
            source_revision=admission.revision,
            doc_name=admission.source.doc_name,
            resource_policy=resource_policy,
        )
    )
    saved_policy = json.loads(fingerprint)
    saved_policy.pop("sheet", None)
    if saved_policy != current_policy:
        from openkb.processing_policy import ReprocessingRequired

        raise ReprocessingRequired(
            "Saved normalization is unavailable and processing policy changed; "
            "preview reprocess explicitly"
        )
    directory = kb_dir / ".openkb/normalized" / identity
    contained_paths(kb_dir, [directory])
    with (
        prepared.conversion_tasks,
        mutation_scope(kb_dir, [directory, record], operation="retain-normalization"),
    ):
        if sheet is not None:
            from openkb.workbooks.normalization import convert_sheet

            converted = convert_sheet(prepared, sheet, doc_name, directory)
        else:
            converted = convert_document(
                prepared.source,
                kb_dir,
                staging_dir=directory,
                prepared=prepared,
                doc_name=doc_name,
                decoding=admission.revision.text_decoding,
                resource_policy=resource_policy,
                check_stop=check_stop,
                office_identity=json.loads(fingerprint).get("office"),
                cnki_identity=json.loads(fingerprint).get("cnki"),
            )
        if converted.raw_path is None:
            raise ValueError("Conversion did not retain its input")
        if converted.processing and converted.source_path:
            from openkb.execution_capacity import select_execution_mode

            converted.processing = select_execution_mode(
                kb_dir,
                converted.processing,
                converted.source_path,
                doc_name,
                scope=scope,
                bundle=bundle,
            )
            converted.is_long_doc = converted.processing.execution_mode == "segmented"
        if converted.is_long_doc and (
            sheet is not None or f".{admission.revision.source_format}" in TEXT_SOURCE_EXTENSIONS
        ):
            from openkb.block_package import freeze_block_package

            if converted.source_path is None:
                raise ValueError("Normalized text is missing")
            freeze_block_package(converted.source_path, doc_name)
        if converted.is_long_doc and converted.pdf_path and converted.office_path:
            from openkb.office.slide_package import freeze_slide_package

            freeze_slide_package(converted.pdf_path, converted.office_path)
        saved = NormalizedInput(
            normalization_id=identity,
            source_revision_id=admission.revision.source_revision_id,
            fingerprint=fingerprint,
            raw_path=converted.raw_path.relative_to(directory).as_posix(),
            source_path=converted.source_path.relative_to(directory).as_posix()
            if converted.source_path
            else None,
            is_long=converted.is_long_doc,
            processing=converted.processing,
            pdf_path=converted.pdf_path.relative_to(directory).as_posix()
            if converted.pdf_path
            else None,
            office_path=converted.office_path.relative_to(directory).as_posix()
            if converted.office_path
            else None,
            cnki_path=converted.cnki_path.relative_to(directory).as_posix()
            if converted.cnki_path
            else None,
            files=wiki_versions(kb_dir, directory),
        )
        write_record(record, saved)
        check_stop()
    return directory, saved


def restore_normalization(directory: Path, saved: NormalizedInput, working: Path) -> ConvertResult:
    copy_tree(directory, working)
    return ConvertResult(
        raw_path=working / saved.raw_path,
        source_path=working / saved.source_path if saved.source_path else None,
        is_long_doc=saved.is_long,
        processing=saved.processing,
        pdf_path=working / saved.pdf_path if saved.pdf_path else None,
        office_path=working / saved.office_path if saved.office_path else None,
        cnki_path=working / saved.cnki_path if saved.cnki_path else None,
    )


def read_retained_source(
    kb_dir: Path,
    unit_revision_id: str,
    source_revision_id: str,
    doc_name: str,
    *,
    chars: str | None = None,
    pages: str | None = None,
    blocks: str | None = None,
) -> tuple[Path, dict] | None:
    """Read a frozen, unpublished input without inventing a knowledge revision."""
    from openkb.ingest_records import UnitRevision
    from openkb.source_map import freeze_pdf_map, freeze_text_map, read_source_map

    used = read_record(kb_dir, "unit-revisions", unit_revision_id, UnitRevision)
    if used.source_revision_id != source_revision_id:
        return None
    identity = normalization_id(source_revision_id, used.processing_fingerprint)
    if not record_path(kb_dir, "normalizations", identity).exists():
        return None
    directory, saved = read_normalization(kb_dir, identity)
    if saved.pdf_path:
        path = f"wiki/sources/{doc_name}.json"
        if path not in saved.files:
            # Long documents have no extracted page body until index construction.
            # The frozen PDF remains available without inventing an unpublished map.
            return None
        reference = freeze_pdf_map(directory / "wiki", doc_name, directory / saved.pdf_path)
        return directory / path, read_source_map(
            directory / "wiki", reference, doc_name, pages, chars=chars, blocks=blocks
        )
    path = f"wiki/sources/{doc_name}.content.json"
    if path not in saved.files:
        return None
    reference = freeze_text_map(directory / "wiki", doc_name)
    return directory / path, read_source_map(
        directory / "wiki", reference, doc_name, pages, chars=chars, blocks=blocks
    )


def retain_published_normalization(kb_dir: Path, admission: Admission, unit, view_id: str) -> str:
    """Upgrade previously published inputs without parsing or relabeling their knowledge."""
    from openkb.application.sources import _manifest
    from openkb.ingest_records import UnitRevision
    from openkb.state import HashRegistry
    from openkb.unit_publication import read_unit_publication

    used = read_record(kb_dir, "unit-revisions", unit.target_revision_id, UnitRevision)
    identity = normalization_id(admission.revision.source_revision_id, used.processing_fingerprint)
    path = record_path(kb_dir, "normalizations", identity)
    if path.exists():
        read_normalization(kb_dir, identity)
        return identity
    state = read_unit_publication(kb_dir, unit.unit_id, view_id)
    actual = _manifest(kb_dir, state)
    if (
        actual is None
        or state.successful_revision_id != used.unit_revision_id
        or used.source_revision_id != admission.revision.source_revision_id
    ):
        raise ValueError("No retained normalization for the current source revision")
    published, manifest = actual
    original = kb_dir / admission.revision.original
    if HashRegistry.hash_file(original) != admission.revision.digest:
        raise ValueError("Retained original changed")
    directory = kb_dir / ".openkb/normalized" / identity
    raw = f"raw/{unit.doc_name}.{admission.revision.source_format}"
    source = f"wiki/{manifest.normalized_source}" if manifest.execution_mode == "full" else None
    if source and manifest.normalized_source is None:
        raise ValueError("Published normalization has no source body")
    copies = {directory / raw: original}
    if source:
        copies[directory / source] = published / source
    if manifest.source_map:
        from openkb.source_map import read_source_map

        read_source_map(published / "wiki", manifest.source_map, unit.doc_name)
        source_map = f"wiki/{manifest.source_map.path}"
        copies[directory / source_map] = published / source_map
    images = f"wiki/sources/images/{unit.doc_name}"
    contained_paths(kb_dir, [directory, *copies.values(), published / images])
    with mutation_scope(kb_dir, [directory, path], operation="retain-published-normalization"):
        for target, origin in copies.items():
            _copy_file_atomic(origin, target)
        if (published / images).exists():
            copy_tree(published / images, directory / images)
        saved = NormalizedInput(
            normalization_id=identity,
            source_revision_id=admission.revision.source_revision_id,
            fingerprint=used.processing_fingerprint,
            raw_path=raw,
            source_path=source,
            is_long=manifest.execution_mode == "segmented",
            processing=manifest.processing,
            files=wiki_versions(kb_dir, directory),
        )
        write_record(path, saved)
    return identity
