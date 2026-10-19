"""Retain existing document conversion outputs without an index package or registry."""

from ultrakb.converter import convert_document
from ultrakb.execution_capacity import select_execution_mode
from ultrakb.images import convert_pdf_to_pages
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.source_pages import read_page_selection


def convert_native_input(
    scope, ready, baseline, private, doc_name, context, bundle, *, policy=None
):
    converted = convert_document(
        ready.source,
        scope.kb_dir,
        staging_dir=baseline.directory,
        private_workspace=baseline.private,
        prepared=ready,
        doc_name=doc_name,
        check_stop=context.check_stop,
        office_identity=(policy or {}).get("office"),
        cnki_identity=(policy or {}).get("cnki"),
    )
    wiki = baseline.directory / "wiki"
    body = wiki / f"sources/{doc_name}.md"
    native = body.with_suffix(".json")
    decision = converted.processing
    if decision is None:
        raise ValueError("This input has no retained native processing decision")
    if converted.source_path is None:
        pdf = converted.pdf_path or converted.raw_path
        if pdf is None:
            raise ValueError("The conversion did not retain its PDF input")
        pages = convert_pdf_to_pages(
            pdf,
            doc_name,
            wiki / f"sources/images/{doc_name}",
            allow_empty_text=bool(converted.office_path),
        )
        if converted.office_path:
            from ultrakb.office.slide_content import attach_slides, read_slides

            slides = read_slides(converted.office_path)
            if slides:
                pages = attach_slides(pages, slides)
        atomic_write_json(native, pages)
        atomic_write_text(body, read_page_selection(pages)["content"])
    else:
        decision = select_execution_mode(
            scope.kb_dir, decision, body, doc_name, scope=private, bundle=bundle
        )
    files = {path.relative_to(wiki).as_posix(): path for path in wiki.rglob("*") if path.is_file()}
    for artifact in (converted.pdf_path, converted.office_path, converted.cnki_path):
        if artifact is not None:
            files["conversion/" + artifact.name] = artifact
    return decision, body, native, files
