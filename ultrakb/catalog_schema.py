"""Prevent unsupported writers from mutating the source and publication catalog."""

from pathlib import Path

from pydantic import Field

from ultrakb.file_state import contained_paths
from ultrakb.source_records import Record

WRITER_VERSION = 3


class UnsupportedCatalogWriter(ValueError):
    """This program must not write the catalog, including recovery or control signals."""


SUPPORTED_CAPABILITIES = frozenset(
    {
        "source-revisions-v1",
        "unit-publications-v1",
        "worksheet-units-v1",
        "worksheet-lifecycle-v1",
        "durable-import-dispatch-v1",
        "shared-knowledge-v1",
        "knowledge-refresh-v1",
        "frozen-normalization-v1",
        "multi-format-sources-v1",
        "explicit-reprocessing-v1",
        "embedded-discovery-v3",
        "pending-import-receipts-v1",
        "html-empty-admission-v1",
        "cnki-conversion-v1",
    }
)


class CatalogSchema(Record):
    minimum_writer_version: int = Field(default=WRITER_VERSION, ge=1)
    required_capabilities: tuple[str, ...] = tuple(sorted(SUPPORTED_CAPABILITIES))


def catalog_schema_path(kb_dir: Path) -> Path:
    path = kb_dir / ".ultrakb/catalog/schema.json"
    contained_paths(kb_dir, [path])
    return path


def validate_catalog_writer(kb_dir: Path) -> None:
    path = catalog_schema_path(kb_dir)
    if path.exists():
        try:
            schema = CatalogSchema.model_validate_json(path.read_text("utf-8"))
        except ValueError as exc:
            raise UnsupportedCatalogWriter(
                "Knowledge catalog schema is unsupported or invalid"
            ) from exc
        if schema.minimum_writer_version > WRITER_VERSION:
            raise UnsupportedCatalogWriter(
                f"Knowledge catalog requires writer version {schema.minimum_writer_version}; "
                f"this writer supports {WRITER_VERSION}"
            )
        unknown = set(schema.required_capabilities) - SUPPORTED_CAPABILITIES
        if unknown:
            raise UnsupportedCatalogWriter(
                f"Unsupported knowledge catalog capabilities: {', '.join(sorted(unknown))}"
            )


def upgrade_catalog_writer(kb_dir: Path) -> None:
    """Caller holds the write lease after validation and journal recovery."""
    from ultrakb.locks import atomic_write_json

    # Recovery may have restored a different marker than the one checked at acquisition.
    validate_catalog_writer(kb_dir)
    path = catalog_schema_path(kb_dir)
    if path.exists():
        current = CatalogSchema.model_validate_json(path.read_text("utf-8"))
        upgraded = CatalogSchema()
        if current != upgraded or "minimum_writer_version" not in current.model_fields_set:
            atomic_write_json(path, upgraded.model_dump(mode="json"))
