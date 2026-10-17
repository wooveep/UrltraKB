"""Validate each delivered fragment before generating more; repair native subtrees."""

import copy
import hashlib
import json
import re

from ..errors import IndexQualityError
from .body_locations import locate_body_entries, native_parent_boundary, position
from .physical_sections import PhysicalNavigationError


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class BodyFragments:
    def __init__(
        self, pages, start, lower, upper, density, model, logger, retained, verify, max_attempts=2
    ):
        self.pages, self.start = pages, start
        self.lower, self.upper, self.density = lower, upper, density
        self.model, self.logger, self.retained, self.verify = model, logger, retained, verify
        self.max_attempts = max_attempts
        self.entries, self.loaded = [], None
        self.failed, self.attempts = set(), {}
        self.identity = _digest(
            {"pages": pages, "start": start, "lower": lower, "upper": upper, "density": density}
        )

    def key(self, part):
        return "body-fragment-v1:" + _digest({"source": self.identity, "part": part})

    def load_fragment(self, part):
        self.loaded = self.retained(self.key(part)) if self.retained else None
        return copy.deepcopy(self.loaded.get("raw")) if isinstance(self.loaded, dict) else None

    def _literal(self, proposed, lower=None, upper=None):
        from .toc_recovery import _literal_entries

        return _literal_entries(
            proposed,
            self.pages,
            self.start,
            lower or self.lower,
            upper or self.upper,
            self.density,
            normalize_order=True,
        )

    def accept(self, incoming, part):
        from .utils import convert_physical_index_to_int

        if not isinstance(incoming, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("title"), str)
            for item in incoming
        ):
            raise PhysicalNavigationError("Body fragment requires an original-heading entry list")
        prefix = _digest(self.entries)
        combined = [
            *copy.deepcopy(self.entries),
            *convert_physical_index_to_int(copy.deepcopy(incoming)),
        ]
        known = []
        cached, self.loaded = self.loaded, None
        if (
            isinstance(cached, dict)
            and cached.get("state") == "verified"
            and cached.get("prefix") == prefix
        ):
            try:
                combined = self._literal(cached["entries"])
                known = copy.deepcopy(combined)
            except (IndexQualityError, KeyError, TypeError):
                self.logger.info(
                    {"physical_body_fragment": "invalid_checkpoint", "key": self.key(part)}
                )
        while True:
            try:
                recovered = self._literal(combined)
                self.verify(recovered, known)
                self.entries = recovered
                if self.retained:
                    self.retained(
                        self.key(part),
                        {
                            "state": "verified",
                            "prefix": prefix,
                            "raw": incoming,
                            "entries": recovered,
                        },
                    )
                self.logger.info(
                    {
                        "physical_body_fragment": "validated",
                        "entries": len(recovered),
                        "key": self.key(part),
                    }
                )
                return
            except IndexQualityError as error:
                if getattr(error, "local_recovery_exhausted", False):
                    raise
                item = getattr(error, "failed_entry", None)
                if item is None:
                    tags = [int(n) for n in re.findall(r"<physical_index_(\d+)>", part)]
                    span = f"{min(tags)}-{max(tags)}" if tags else str(self.start)
                    raise PhysicalNavigationError(
                        f"Body fragment recovery exhausted on physical pages {span}: {error}"
                    ) from error
                combined = self._repair(combined, item, error)

    def _repair(self, proposed, item, error):
        from .page_index import process_no_toc

        section = item.get("structure")
        located = locate_body_entries(proposed, self.pages, self.start)
        by_section = {
            entry.get("structure"): (entry, anchor) for entry, anchor in located if anchor
        }
        # A rejected role cannot attest its own parent boundary. Prefer the
        # closest enclosing native heading; retain the target only as a search
        # interval when there is no enclosing proposal.
        parent = section.rsplit(".", 1)[0] if isinstance(section, str) and "." in section else None
        while parent and parent not in by_section:
            parent = parent.rsplit(".", 1)[0] if "." in parent else None
        parent = parent or (section if section in by_section else None)
        if parent is None:
            raise PhysicalNavigationError(
                f"Body subtree lacks a native parent for {item['title']!r}: {error}"
            ) from error
        root, anchor = by_section[parent]
        lower, upper = position(anchor), self.upper
        depth = len(parent.split("."))
        for other, current in located:
            other_section = other.get("structure")
            if (
                not current
                or not isinstance(other_section, str)
                or other_section.startswith(parent + ".")
            ):
                continue
            if len(other_section.split(".")) <= depth and lower < position(current) < upper:
                upper = position(current)
        boundary = native_parent_boundary(root, anchor, self.pages, self.start, upper)
        lower, upper = boundary.start, boundary.end
        native = item.get("anchor")
        identity = (
            section,
            position(native) if native else item.get("physical_index"),
            lower,
            upper,
        )
        key = "body-failed-subtree-v1:" + _digest({"source": self.identity, "failure": identity})
        scope = (lower, upper)
        if identity in self.failed or self.attempts.get(scope, 0) >= self.max_attempts:
            raise PhysicalNavigationError(
                f"Body navigation recovery made no progress on physical pages "
                f"{lower[0]}-{upper[0]}: "
                f"unchanged rejected source locations or repair budget exhausted; {error}"
            ) from error
        self.failed.add(identity)
        self.attempts[scope] = self.attempts.get(scope, 0) + 1
        previous = self.retained(key) if self.retained else None
        if isinstance(previous, dict) and previous.get("state") == "generated":
            rebuilt = copy.deepcopy(previous["raw"])
        else:
            first, last = max(self.start, lower[0]), min(self.start + len(self.pages) - 1, upper[0])
            self.logger.info(
                {
                    "physical_body_subtree": "repair",
                    "section": parent,
                    "physical_range": [first, last],
                    "failure": str(error),
                }
            )
            rebuilt = process_no_toc(
                self.pages[first - self.start : last - self.start + 1],
                start_index=first,
                model=self.model,
                logger=self.logger,
                boundary=boundary,
                repair_context={
                    "failed_section": section,
                    "failed_title": item["title"],
                    "reason": str(error),
                },
            )
            if self.retained:
                # This is a delivered proposal, not a successful heading-role
                # verdict. Reuse it on resume without buying the same request.
                self.retained(key, {"state": "generated", "failure": str(error), "raw": rebuilt})
        recovered = self._literal(rebuilt, lower, upper)
        kept = [
            entry
            for entry in proposed
            if not (
                entry.get("structure") == parent
                or (
                    isinstance(entry.get("structure"), str)
                    and entry["structure"].startswith(parent + ".")
                )
            )
        ]
        return [*kept, *recovered]

    def result(self):
        return copy.deepcopy(self.entries)
