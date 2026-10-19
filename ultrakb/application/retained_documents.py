"""Retain readable originals and figure access when native text cannot be compiled."""

from ultrakb.locks import atomic_write_bytes


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
