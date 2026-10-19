"""Retain readable originals and figure access when native text cannot be compiled."""

import json
from dataclasses import replace
from pathlib import Path

from ultrakb.agent.page_figures import render_gallery
from ultrakb.figure_assets import freeze_figures
from ultrakb.import_text import TEXT_CHECK_POLICY
from ultrakb.locks import atomic_write_bytes, atomic_write_json, atomic_write_text
from ultrakb.mutation import mutation_scope
from ultrakb.source_catalog import admit_source_revision, record_path, write_record
from ultrakb.source_records import Record, RecordId


class RetainedDocument(Record):
    source_revision_id: RecordId
    unit_revision_id: RecordId
    observed_blank: bool


def capture_retained_content(
    kb_dir, prepared, doc_name, directory, check_stop, resource_policy, *, decoding=None
):
    from ultrakb.inputs import (
        CNKI_SOURCE_EXTENSIONS,
        OFFICE_SOURCE_EXTENSIONS,
        TEXT_SOURCE_EXTENSIONS,
    )

    extension = prepared.source.suffix.lower()
    if extension in TEXT_SOURCE_EXTENSIONS:
        from ultrakb.text_formats import normalize_text_document

        frozen = normalize_text_document(
            prepared,
            doc_name,
            directory,
            decoding=decoding,
            resource_policy=resource_policy,
        )
        return frozen.model_dump(mode="json"), not frozen.text.strip()
    pdf_path = prepared.path
    office_path = None
    if extension in OFFICE_SOURCE_EXTENSIONS:
        from ultrakb.office.convert import convert_office

        directory.mkdir(parents=True, exist_ok=True)
        pdf_path = directory / "display.pdf"
        office_path = convert_office(kb_dir, prepared.path, pdf_path, check_stop=check_stop)
    elif extension in CNKI_SOURCE_EXTENSIONS:
        from ultrakb.cnki.convert import prepare_cnki

        converted, provenance = prepare_cnki(kb_dir, prepared, check_stop=check_stop)
        pdf_path = directory / "display.pdf"
        atomic_write_bytes(pdf_path, converted.read_bytes())
        atomic_write_bytes(pdf_path.with_suffix(".cnki.json"), provenance.read_bytes())
    elif extension != ".pdf":
        # No text is not proof that an unsupported display format is blank.
        return "", False
    import pymupdf

    from ultrakb.images import extract_pdf_images

    images = extract_pdf_images(pdf_path, doc_name, directory / "sources/images" / doc_name)
    with pymupdf.open(pdf_path) as pdf:
        count = len(pdf)
        observed_blank = all(
            not page.get_text().strip()
            and not page.get_images()
            and not page.get_drawings()
            and not list(page.annots() or ())
            for page in pdf
        )
    pages = [
        {
            "page": i,
            "unit_kind": "page",
            "content": "",
            "images": [{"path": p} for p in images.get(i, [])],
        }
        for i in range(1, count + 1)
    ]
    if office_path:
        from ultrakb.office.slide_content import attach_slides, read_slides

        slides = read_slides(office_path)
        if slides:
            pages = attach_slides(pages, slides)
    return pages, observed_blank


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
    fingerprint: str,
):
    from ultrakb.application.ingestion import result_from_publication
    from ultrakb.knowledge_scope import resolve_scope
    from ultrakb.unit_publication import begin_unit_attempt, plan_import_units

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
        kb_dir,
        admission,
        json.dumps({"retained_only": TEXT_CHECK_POLICY, "normalization": fingerprint}),
    )
    state, _ = begin_unit_attempt(
        kb_dir,
        unit,
        revision,
        discovery_intent=admission.discovery_intent,
        view_id=selected.view_id,
        retry_confirmed=True,
    )
    directory = (
        kb_dir
        / ".ultrakb/retained-documents"
        / admission.revision.source_revision_id
        / revision.unit_revision_id
    )
    retained_target = directory.parent / "target.json"
    state_path = record_path(kb_dir, "publications", state.publication_id)
    attempt_path = record_path(kb_dir, "attempts", state.attempt_id)
    message = "资料已保存，未生成知识：没有可靠的原生文字；原件及可用原图供阅读。已有知识尚未刷新。"
    with mutation_scope(
        kb_dir,
        [directory, retained_target, state_path, attempt_path],
        operation="retain-without-knowledge",
    ):
        check_stop()
        entry = directory / "original-images.md"
        receipt = directory / "retention.json"
        if receipt.exists():
            saved = RetainedDocument.model_validate_json(receipt.read_text("utf-8"))
            read_retained_images(
                kb_dir,
                admission.revision.source_revision_id,
                unit_revision_id=revision.unit_revision_id,
            )
            observed_blank = saved.observed_blank
        else:
            raw, observed_blank = capture_retained_content(
                kb_dir,
                prepared,
                unit.doc_name,
                directory,
                check_stop,
                resource_policy,
                decoding=admission.revision.text_decoding,
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
            atomic_write_text(
                entry,
                render_gallery(
                    directory,
                    entry.name,
                    message,
                    [{"figure": f.model_dump(mode="json"), "historical": False} for f in figures],
                ),
            )
            saved = RetainedDocument(
                source_revision_id=admission.revision.source_revision_id,
                unit_revision_id=revision.unit_revision_id,
                observed_blank=observed_blank,
            )
            write_record(receipt, saved)
        write_record(retained_target, saved)
        if observed_blank:
            message = "资料已保存，未生成知识：原件页面为空白；未自动确认来源贡献为空。"
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


def read_retained_images(
    kb_dir: Path, source_revision_id: str, *, unit_revision_id: str, pages=None
):
    """Use the same native source reader in desktop, CLI and HTTP, without compiling."""
    from ultrakb.agent.page_figures import split_gallery
    from ultrakb.figure_assets import FigureOccurrence
    from ultrakb.file_state import contained_paths
    from ultrakb.source_pages import read_page_selection

    root = kb_dir / ".ultrakb/retained-documents" / source_revision_id
    directory = root / unit_revision_id
    if not directory.is_dir():
        target = root / "target.json"
        if not target.exists():
            return None
        selected = RetainedDocument.model_validate_json(target.read_text("utf-8"))
        if selected.source_revision_id != source_revision_id:
            raise ValueError("Retained target belongs to another source revision")
        directory = root / selected.unit_revision_id
    entry = directory / "original-images.md"
    contained_paths(kb_dir, [directory, entry])
    saved = RetainedDocument.model_validate_json((directory / "retention.json").read_text("utf-8"))
    if saved.source_revision_id != source_revision_id or saved.unit_revision_id != directory.name:
        raise ValueError("Retained images belong to another source or processing revision")
    paths = list((directory / "sources").glob("*.json"))
    if len(paths) != 1:
        raise ValueError("Retained figure source is incomplete")
    raw = json.loads(paths[0].read_text())
    body, entries = split_gallery(entry.read_text())
    for record in entries:
        figure = FigureOccurrence.model_validate(record["figure"])
        if (
            figure.source_revision_id != saved.source_revision_id
            or figure.processing_revision_id != saved.unit_revision_id
        ):
            raise ValueError("Retained figure belongs to another processing revision")
    if not isinstance(raw, list):
        if pages is not None:
            raise ValueError("This original has no physical PDF pages")
        return entry, {
            "content": render_gallery(directory, entry.name, body, entries),
            "unit_revision_id": saved.unit_revision_id,
            "pages": None,
            "page_range": [],
            "units": [],
        }
    selection = read_page_selection(raw, pages)
    pdf = directory / "display.pdf"
    if pdf.is_file():
        from ultrakb.conversion_artifacts import read_conversion_files

        office, cnki = pdf.with_suffix(".office.json"), pdf.with_suffix(".cnki.json")
        selection.update(
            read_conversion_files(
                kb_dir,
                pdf,
                office_path=office if office.is_file() else None,
                cnki_path=cnki if cnki.is_file() else None,
            )
        )
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
        "unit_revision_id": saved.unit_revision_id,
        "content": render_gallery(directory, entry.name, body, visible),
    }
