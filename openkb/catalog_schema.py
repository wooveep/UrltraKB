"""Prevent unsupported writers from mutating versioned source knowledge."""

from pathlib import Path

from openkb.file_state import contained_paths
from openkb.source_records import Record

SUPPORTED_CAPABILITIES = frozenset({"source-revisions-v1", "unit-publications-v1"})


class CatalogSchema(Record):
    required_capabilities: tuple[str, ...] = tuple(sorted(SUPPORTED_CAPABILITIES))


def catalog_schema_path(kb_dir: Path) -> Path:
    path = kb_dir / ".openkb/catalog/schema.json"
    contained_paths(kb_dir, [path])
    return path


def validate_catalog_writer(kb_dir: Path) -> None:
    path = catalog_schema_path(kb_dir)
    if path.exists():
        schema = CatalogSchema.model_validate_json(path.read_text("utf-8"))
        unknown = set(schema.required_capabilities) - SUPPORTED_CAPABILITIES
        if unknown:
            raise ValueError(
                f"Unsupported knowledge catalog capabilities: {', '.join(sorted(unknown))}"
            )
