"""Complete headed operations and ancestor conditions for generation windows."""

from openkb.agent.dependency_scope import _sections
from openkb.agent.dependency_sources import OriginalRows, SourceSelection


class OperationContext:
    def __init__(self, reader, source, parsed):
        self.rows = OriginalRows(reader, source, parsed)
        sections = _sections(SourceSelection(self.rows))
        self.paths = sections[0] if sections else {}
        self.scopes = {}
        for bid, path in self.paths.items():
            self.scopes.setdefault(path, []).append(bid)

    def read(self, reference):
        path = self.paths.get(reference["block_id"])
        if not path:
            return []
        keys = set()
        for end in range(len(path) + 1):
            keys.update(self.scopes.get(path[:end], ()))
        return [
            {**self.rows[bid], "relation": "operation_context"}
            for bid in self.rows
            if bid in keys and bid != reference["block_id"]
        ]
