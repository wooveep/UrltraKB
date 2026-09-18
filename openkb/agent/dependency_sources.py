"""Lazy, identity-bound original ranges used by dependency routing and review."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict

from openkb.evidence import Evidence, complete_read_bound
from openkb.source_context import context_fields


class OriginalRows(Mapping):
    def __init__(self, reader, source, parsed):
        self.reader = reader
        self.identity = (source.source_id, source.id, parsed.id)
        self.blocks = {block.id: block for block in parsed.blocks}

    def __getitem__(self, key):
        block = self.blocks[key]
        reference = Evidence(*self.identity, block.id)
        view = self.reader.read(reference, max_chars=complete_read_bound(block))
        return {
            "reference": asdict(reference),
            "kind": block.kind,
            "location": view.location,
            **context_fields(view),
            "text": view.text,
        }

    def __iter__(self):
        return iter(self.blocks)

    def __len__(self):
        return len(self.blocks)


class SourceSelection(Sequence):
    def __init__(self, rows, keys=None):
        self.by_id = rows
        self.keys = tuple(rows if keys is None else keys)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SourceSelection(self.by_id, self.keys[index])
        return self.by_id[self.keys[index]]

    def __len__(self):
        return len(self.keys)

    def select(self, keys):
        return SourceSelection(self.by_id, keys)
