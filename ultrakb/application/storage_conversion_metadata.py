"""Read conversion provenance only from the selected immutable processing input."""


def conversion_metadata(catalog, target, processing):
    from ultrakb.cnki.records import CNKIConversion
    from ultrakb.office.records import OfficeConversion

    metadata = {}
    for kind, schema in (("office", OfficeConversion), ("cnki", CNKIConversion)):
        receipts = [
            file
            for file in processing.files
            if file.path.startswith("conversion/") and file.path.endswith(f".{kind}.json")
        ]
        if not receipts:
            continue
        if len(receipts) != 1:
            raise ValueError("Conversion provenance must identify one retained receipt")
        original = catalog.revision(target.source_revision_id)
        main = next(file for file in original.files if file.kind == "main")
        receipt = schema.model_validate_json(
            catalog.read_processed(processing.processing_id, receipts[0].path)
        )
        if receipt.input_digest != main.digest or not any(
            file.path.startswith("conversion/")
            and file.path.endswith(".pdf")
            and file.digest == receipt.pdf_digest
            for file in processing.files
        ):
            raise ValueError("Conversion provenance differs from its retained original or PDF")
        metadata[kind] = receipt.model_dump(mode="json")
    return metadata
