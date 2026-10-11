"""Retain readable originals and figure access when native text cannot be compiled."""

import json
from dataclasses import replace
from pathlib import Path

from openkb.agent.page_figures import render_gallery
from openkb.figure_assets import freeze_figures
from openkb.import_text import TEXT_CHECK_POLICY
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.mutation import mutation_scope
from openkb.source_catalog import admit_source_revision, record_path, write_record


def _retain_original_content(
    kb_dir, prepared, admission, unit, directory, check_stop, resource_policy
):
    from openkb.inputs import (
        CNKI_SOURCE_EXTENSIONS,
        OFFICE_SOURCE_EXTENSIONS,
        TEXT_SOURCE_EXTENSIONS,
    )

    extension = prepared.source.suffix.lower()
    if extension in TEXT_SOURCE_EXTENSIONS:
        from openkb.text_formats import normalize_text_document

        frozen = normalize_text_document(
            prepared,
            unit.doc_name,
            directory,
            decoding=admission.revision.text_decoding,
            resource_policy=resource_policy,
        )
        return frozen.model_dump(mode="json"), not frozen.text.strip()
    pdf_path = prepared.path
    if extension in OFFICE_SOURCE_EXTENSIONS:
        from openkb.office.convert import convert_office

        pdf_path = directory / "display.pdf"
        convert_office(kb_dir, prepared.path, pdf_path, check_stop=check_stop)
    elif extension in CNKI_SOURCE_EXTENSIONS:
        from openkb.cnki.convert import prepare_cnki

        pdf_path, _ = prepare_cnki(kb_dir, prepared, check_stop=check_stop)
    elif extension != ".pdf":
        # No text is not proof that an unsupported display format is blank.
        return "", False
    import pymupdf

    from openkb.images import extract_pdf_images

    images = extract_pdf_images(
        pdf_path, unit.doc_name, directory / "sources/images" / unit.doc_name
    )
    with pymupdf.open(pdf_path) as pdf:
        count = len(pdf)
        observed_blank = all(
            not page.get_text().strip()
            and not page.get_images()
            and not page.get_drawings()
            and not list(page.annots() or ())
            for page in pdf
        )
    return [
        {"page": i, "content": "", "images": [{"path": p} for p in images.get(i, [])]}
        for i in range(1, count + 1)
    ], observed_blank


def retain_without_knowledge(
    kb_dir: Path,
    prepared,
    assessment,
    *,
    admission=None,
    identity=None,
    scope=None,
    check_stop=lambda: None,
    resource_policy=None,
):
    from openkb.application.ingestion import result_from_publication
    from openkb.knowledge_scope import resolve_scope
    from openkb.unit_publication import begin_unit_attempt, plan_import_units

    selected = resolve_scope(kb_dir, scope, writable=True)
    admission = admission or admit_source_revision(
        kb_dir,
        prepared,
        identity=identity,
        check_stop=check_stop,
        text_assessment=assessment,
        retain_only=True,
    )
    unit, revision = plan_import_units(
        kb_dir, admission, json.dumps({"retained_only": TEXT_CHECK_POLICY})
    )
    state, _ = begin_unit_attempt(
        kb_dir,
        unit,
        revision,
        discovery_intent=admission.discovery_intent,
        view_id=selected.view_id,
        retry_confirmed=True,
    )
    directory = kb_dir / ".openkb/retained-documents" / admission.revision.source_revision_id
    state_path = record_path(kb_dir, "publications", state.publication_id)
    attempt_path = record_path(kb_dir, "attempts", state.attempt_id)
    message = "资料已保存，未生成知识：没有可靠的原生文字；原件及可用原图供阅读。已有知识尚未刷新。"
    with mutation_scope(
        kb_dir, [directory, state_path, attempt_path], operation="retain-without-knowledge"
    ):
        check_stop()
        raw, observed_blank = _retain_original_content(
            kb_dir, prepared, admission, unit, directory, check_stop, resource_policy
        )
        if observed_blank:
            message = "资料已保存，未生成知识：原件页面为空白；未自动确认来源贡献为空。"
        path = f"sources/{unit.doc_name}.json"
        atomic_write_json(directory / path, raw)
        figures = freeze_figures(
            directory,
            path,
            raw,
            {
                "source_revision_id": admission.revision.source_revision_id,
                "processing_revision_id": revision.unit_revision_id,
            },
        )
        entry = directory / "original-images.md"
        atomic_write_text(
            entry,
            render_gallery(
                directory,
                entry.name,
                message,
                [{"figure": f.model_dump(mode="json"), "historical": False} for f in figures],
            ),
        )
        state = state.model_copy(
            update={
                "status": "blocked",
                "stage": "text_reading",
                "message": message,
                "error_type": "NoReliableText",
                "quality": ("blank_original" if observed_blank else "no_reliable_text",),
                "unfinished": ("text_reading",),
                "completed_stages": ("original_retained", "figures_retained"),
            }
        )
        write_record(state_path, state)
        write_record(attempt_path, state)
        check_stop()
    result = result_from_publication(kb_dir, admission, state, status="blocked")
    return replace(result, resources=(*result.resources, str(entry)), message=message)


def read_retained_images(kb_dir: Path, source_revision_id: str, *, pages=None):
    """Use the same native source reader in desktop, CLI and HTTP, without compiling."""
    from openkb.agent.page_figures import split_gallery
    from openkb.figure_assets import FigureOccurrence
    from openkb.file_state import contained_paths
    from openkb.source_pages import read_page_selection

    directory = kb_dir / ".openkb/retained-documents" / source_revision_id
    entry = directory / "original-images.md"
    contained_paths(kb_dir, [directory, entry])
    if not entry.is_file():
        return None
    paths = list((directory / "sources").glob("*.json"))
    if len(paths) != 1:
        raise ValueError("Retained figure source is incomplete")
    raw = json.loads(paths[0].read_text())
    body, entries = split_gallery(entry.read_text())
    if not isinstance(raw, list):
        if pages is not None:
            raise ValueError("This original has no physical PDF pages")
        return entry, {"content": entry.read_text(), "pages": None, "page_range": [], "units": []}
    selection = read_page_selection(raw, pages)
    visible = []
    for record in entries:
        figure = FigureOccurrence.model_validate(record["figure"])
        if figure.location.get("page") in selection["page_range"]:
            visible.append(record)
    for unit in selection["units"]:
        unit["content"] = render_gallery(
            directory,
            entry.name,
            body,
            [record for record in visible if record["figure"]["location"]["page"] == unit["page"]],
        )
    return entry, {
        **selection,
        "content": render_gallery(directory, entry.name, body, visible),
    }
