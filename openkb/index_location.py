"""A document index's database, managed inputs, and immutable owner."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openkb.file_state import contained_paths

INDEX_FORMAT = "openkb.condb-index.v1"


@dataclass(frozen=True)
class IndexLocation:
    database: Path
    inputs_root: Path
    format: str = INDEX_FORMAT
    read_only: bool = False
    owner_revision: str | None = None

    def __post_init__(self):
        if self.format != INDEX_FORMAT:
            raise ValueError("Unsupported document index format")
        object.__setattr__(self, "database", Path(self.database).resolve())
        object.__setattr__(self, "inputs_root", Path(self.inputs_root).resolve())
        if self.owner_revision is not None and not self.read_only:
            raise ValueError("A published index must be read-only")

    @classmethod
    def package(cls, root: Path) -> IndexLocation:
        root = root.resolve()
        if (root / "pageindex.db").exists():
            raise ValueError("Unsupported legacy PageIndex database; create a new knowledge base")
        marker = root / "index.json"
        if not marker.exists():
            contained_paths(root, [root / "context.sqlite", root / "files"])
            return cls(root / "context.sqlite", root / "files")
        value = json.loads(marker.read_text("utf-8"))
        if not isinstance(value, dict) or value.get("format") != INDEX_FORMAT:
            raise ValueError("Unsupported document index format")
        if type(value.get("read_only")) is not bool:
            raise ValueError("Invalid index ownership metadata")
        owner = value.get("owner_revision")
        if owner is not None and (not isinstance(owner, str) or not owner):
            raise ValueError("Invalid index revision owner")
        paths = []
        for key in ("database", "inputs_root"):
            relative = value.get(key)
            if (
                not isinstance(relative, str)
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                raise ValueError("Index paths must be managed relative references")
            path = root / relative
            contained_paths(root, [path])
            paths.append(path)
        return cls(paths[0], paths[1], value["format"], value["read_only"], owner)

    def input_path(self, reference: str) -> Path:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("Managed input must be a portable relative reference")
        target = self.inputs_root / relative
        contained_paths(self.inputs_root, [target])
        return target.resolve()

    def input_reference(self, path: str) -> str:
        resolved = Path(path).resolve()
        contained_paths(self.inputs_root, [resolved])
        if resolved == self.inputs_root:
            raise ValueError("Managed input must be a file")
        return resolved.relative_to(self.inputs_root).as_posix()

    def mutation_paths(self, *, include_inputs=False) -> list[Path]:
        paths = [Path(str(self.database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
        paths.append(self.database.parent / "index.json")
        if include_inputs:
            paths.append(self.inputs_root)
        return paths
