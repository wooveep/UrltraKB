"""Immutable per-page processing inputs, including an unchanged page's older basis."""

from pydantic import TypeAdapter

from ultrakb.source_records import RecordId

PAGE_INPUT_SCHEMA = (
    """CREATE TABLE ukb_revision_page_inputs (
        revision_id TEXT NOT NULL,
        path TEXT NOT NULL,
        unit_revision_id TEXT NOT NULL REFERENCES ukb_unit_revisions(unit_revision_id),
        PRIMARY KEY (revision_id, path, unit_revision_id),
        FOREIGN KEY (revision_id, path) REFERENCES ukb_revision_files(revision_id, path)
    )""",
    "CREATE INDEX ukb_page_input_unit ON ukb_revision_page_inputs(unit_revision_id, revision_id)",
    """CREATE TRIGGER ukb_page_input_draft BEFORE INSERT ON ukb_revision_page_inputs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = NEW.revision_id AND state = 'draft')
        BEGIN SELECT RAISE(ABORT, 'Page inputs require a draft revision'); END""",
    """CREATE TRIGGER ukb_page_input_immutable BEFORE UPDATE ON ukb_revision_page_inputs
        BEGIN SELECT RAISE(ABORT, 'Page inputs are immutable'); END""",
    """CREATE TRIGGER ukb_page_input_delete BEFORE DELETE ON ukb_revision_page_inputs
        WHEN NOT EXISTS (SELECT 1 FROM ukb_knowledge_revisions
                         WHERE revision_id = OLD.revision_id AND state = 'retiring')
        BEGIN SELECT RAISE(ABORT, 'Only retiring revisions release page inputs'); END""",
)


def source_paths(unit, processing, *, retained=False):
    prefix = f"sources/retained/{unit.unit_revision_id}" if retained else "sources"
    suffix = {"markdown": ".md", "text": ".content.json", "pages": ".json"}[processing.source_kind]
    native = (f"{prefix}/{unit.doc_name}{suffix}", processing.source_path)
    if processing.source_kind == "markdown":
        return (native,)
    return ((f"{prefix}/{unit.doc_name}.md", processing.body_path), native)


def image_paths(unit, processing, *, retained=False):
    from pathlib import PurePosixPath

    prefix = f"sources/retained/{unit.unit_revision_id}" if retained else "sources"
    result = []
    for file in processing.files:
        if PurePosixPath(file.path).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
        }:
            result.append((f"{prefix}/{file.path.removeprefix('sources/')}", file.path))
    return tuple(result)


def source_selector(unit, *, retained=False):
    return (
        f"sources/retained/{unit.unit_revision_id}/{unit.doc_name}" if retained else unit.doc_name
    )


def navigation_inputs(inputs, page_rows):
    complete = dict(inputs)
    for _, unit in page_rows:
        complete.setdefault(unit, None)
    return sorted(complete.items())


def validate_source_paths(db, files, inputs, page_rows):
    from .catalog_store import CatalogStore

    inventory = {path: digest for path, digest, _ in files}
    active = {unit for unit, _ in inputs}
    owners, selectors = {}, {}
    for unit_id in sorted(active | {unit for _, unit in page_rows}):
        unit = CatalogStore._unit_revision(db, unit_id)
        processing = CatalogStore._processing(db, unit.processing_id)
        selector = source_selector(unit, retained=unit_id not in active)
        if selector in selectors and selectors[selector] != unit_id:
            raise ValueError("Processing source selectors must identify exactly one unit revision")
        selectors[selector] = unit_id
        digests = {file.path: file.digest for file in processing.files}
        paths = source_paths(unit, processing, retained=unit_id not in active)
        for alias, _ in paths:
            if alias in owners and owners[alias] != unit_id:
                raise ValueError(
                    "Processing source aliases must identify exactly one unit revision"
                )
            owners[alias] = unit_id
        for alias, original in (
            *paths,
            *image_paths(unit, processing, retained=unit_id not in active),
        ):
            if alias in inventory and inventory[alias] != digests[original]:
                raise ValueError("Published source bytes differ from the retained processing input")
            inventory[alias] = digests[original]


def prepare_page_inputs(db, files, inputs, expected, view_id, explicit):
    pages = {path: digest for path, digest, _ in files if path.endswith(".md")}
    if explicit is not None and (
        not isinstance(explicit, dict) or any(path not in pages for path in explicit)
    ):
        raise ValueError("Page inputs must select Markdown paths in the complete revision")
    previous = {}
    previous_inputs = {}
    if expected is not None:
        previous = dict(
            db.connection.execute(
                "SELECT f.path, f.digest FROM ukb_publications p JOIN ukb_revision_files f "
                "ON f.revision_id = p.revision_id WHERE p.publication_id = ? AND p.view_id = ?",
                (expected, view_id),
            ).fetchall()
        )
        for path, unit in db.connection.execute(
            "SELECT i.path, i.unit_revision_id FROM ukb_publications p "
            "JOIN ukb_revision_page_inputs i "
            "ON i.revision_id = p.revision_id WHERE p.publication_id = ? AND p.view_id = ? "
            "ORDER BY i.path, i.unit_revision_id",
            (expected, view_id),
        ).fetchall():
            previous_inputs.setdefault(path, []).append(unit)
    defaults = tuple(unit for unit, _ in inputs)
    result = []
    selected = set()
    for path, digest in sorted(pages.items()):
        units = (explicit or {}).get(path)
        if units is None:
            units = (
                tuple(previous_inputs.get(path, ())) if previous.get(path) == digest else defaults
            )
        if not isinstance(units, tuple) or len(set(units)) != len(units):
            raise ValueError("Each page requires distinct immutable processing-unit inputs")
        for unit in sorted(units):
            TypeAdapter(RecordId).validate_python(unit)
            selected.add(unit)
            result.append((path, unit))
    for unit in selected:
        if not db.connection.execute(
            "SELECT 1 FROM ukb_unit_revisions u JOIN ukb_processing_versions p "
            "ON p.processing_id = u.processing_id "
            "WHERE u.unit_revision_id = ? AND p.state = 'ready'",
            (unit,),
        ).fetchone():
            raise ValueError("Page inputs require retained ready processing versions")
    return result
