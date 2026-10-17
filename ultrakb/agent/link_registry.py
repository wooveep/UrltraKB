"""Frozen, full page identities with bounded writing hints and exact link resolution."""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.agent.page_response import PageResponseError, load_protocol_json
from ultrakb.lint import _normalize_target, _wikilink_matches


@dataclass(frozen=True)
class LinkTarget:
    path: str
    names: tuple[str, ...] = ()
    entity_type: str = ""
    description: str = ""

    @property
    def identity(self):
        return "link:" + hashlib.sha256(self.path.encode()).hexdigest()[:20]


class FrozenLinkRegistry:
    def __init__(self, targets):
        self.targets = tuple(sorted(targets, key=lambda item: item.path))
        self.catalogue_id = hashlib.sha256(
            json.dumps(
                [
                    (target.path, target.names, target.entity_type, target.description)
                    for target in self.targets
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        self.paths = frozenset(target.path for target in self.targets)
        self.identities = {target.identity: target.path for target in self.targets}
        self._persisted: set[str] = set()
        self.names: dict[str, set[str]] = defaultdict(set)
        for target in self.targets:
            kind, _, stem = target.path.rpartition("/")
            self.names[_normalize_target(target.path)].add(target.path)
            for name in (stem, *target.names):
                if name:
                    self.names[_normalize_target(name)].add(target.path)
                    if kind:
                        self.names[_normalize_target(f"{kind}/{name}")].add(target.path)

    @classmethod
    def capture(cls, wiki: Path, targets, *, planned=()):
        metadata = {item["path"]: item for item in planned}
        records = []
        for target in sorted(targets):
            values = metadata.get(target, {})
            path = wiki / f"{target}.md"
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                values = {**frontmatter.parse(text), **values}
                heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                if heading:
                    values = {"title": heading.group(1), **values}
            try:
                aliases = frontmatter.alias_values(values.get("aliases"), allow_scalar=True)
            except ValueError as error:
                raise ValueError(f"Invalid aliases metadata for {target}: {error}") from error
            names = tuple(
                value
                for value in (values.get("title"), values.get("name"), *(aliases or ()))
                if isinstance(value, str) and value
            )
            records.append(
                LinkTarget(
                    target,
                    names,
                    str(values.get("type", "")),
                    str(values.get("description") or values.get("brief") or ""),
                )
            )
        return cls(records)

    @classmethod
    def from_message(cls, message):
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content)
        if "COMPLETE list" not in content:
            return None
        targets = re.findall(r"^- ((?:concepts|entities|summaries)/[^\n]+|index)$", content, re.M)
        return cls(LinkTarget(target) for target in targets) if targets else None

    def persist(self, checkpoints):
        if checkpoints is None:
            return
        from ultrakb.compile_checkpoints import digest

        owner = str(checkpoints.path)
        if owner in self._persisted:
            return
        records = [
            {
                "id": target.identity,
                "path": target.path,
                "names": target.names,
                "type": target.entity_type,
                "description": target.description,
            }
            for target in self.targets
        ]
        checkpoints.save(
            "links:catalogue",
            digest(records),
            {"targets": records},
            metadata={"protocol": "links-v1"},
        )
        self._persisted.add(owner)

    def message(self, context, *, limit=64, max_chars=6000):
        """Candidates are hints; absence never establishes that a page is missing."""
        context = _normalize_target(context)
        ranked = sorted(
            self.targets,
            key=lambda target: (
                not any(
                    name and _normalize_target(name) in context
                    for name in (target.path, target.path.rsplit("/", 1)[-1], *target.names)
                ),
                target.path,
            ),
        )
        records, length = [], 0
        for target in ranked[:limit]:
            record = {"id": target.identity, "path": target.path}
            if target.names:
                record["names"] = list(target.names)
            if target.entity_type:
                record["type"] = target.entity_type
            if target.description:
                record["description"] = target.description
            size = len(json.dumps(record, ensure_ascii=False))
            if length + size > max_chars:
                continue
            records.append(record)
            length += size
        return {
            "role": "user",
            "content": (
                "Bounded link candidates from the frozen full catalogue (links-v1, "
                + self.catalogue_id
                + "):\n"
                + json.dumps(records, ensure_ascii=False, separators=(",", ":"))
                + "\nThese candidates are incomplete. An omitted page may still exist. "
                "Use an exact supplied path or [[link:ID|display]] identity. For other supported "
                "relationships use [[exact name|display]]; the application resolves names and "
                "declared aliases against the full frozen catalogue. Never guess a slug. "
                "Unresolved names receive bounded full-catalogue semantic matching. They remain "
                "prose only after every catalogue partition explicitly finds no matching identity."
            ),
        }

    def render(self, content, *, field="content", resolutions=None):
        chunks, cursor, errors = [], 0, []
        for match in _wikilink_matches(content):
            raw, _, alias = match.group(1).partition("|")
            raw, alias = raw.strip(), alias.strip()
            if raw.startswith(("original:", "figure:")):
                continue
            identity = hashlib.sha256(f"{field}:{match.start()}".encode()).hexdigest()[:20]
            chosen = (resolutions or {}).get(identity)
            if resolutions is not None and identity in resolutions:
                if chosen is not None and chosen not in self.paths:
                    raise PageResponseError("page_link_invalid_resolution", "Unknown frozen path")
                resolved = chosen
            elif raw in self.paths:
                resolved = raw
            elif raw in self.identities:
                resolved = self.identities[raw]
            else:
                matches = self.names.get(_normalize_target(raw), set())
                resolved = next(iter(matches)) if len(matches) == 1 else None
                if len(matches) > 1 or (not matches and self.paths):
                    errors.append(
                        {
                            "id": identity,
                            "code": "page_link_ambiguous" if matches else "page_link_unresolved",
                            "allow_unmatched": not matches,
                            "name": raw,
                            "context": content[max(0, match.start() - 160) : match.end() + 160],
                            "targets": sorted(matches or self.paths),
                        }
                    )
            display = alias or raw.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
            rendered = f"[[{resolved}{'|' + alias if alias else ''}]]" if resolved else display
            chunks.extend((content[cursor : match.start()], rendered))
            cursor = match.end()
        chunks.append(content[cursor:])
        if errors:
            raise PageResponseError(
                "page_link_unresolved"
                if any(error["allow_unmatched"] for error in errors)
                else "page_link_ambiguous",
                "Each unresolved link requires a complete frozen identity decision",
                field_errors=tuple(errors),
            )
        return "".join(chunks)

    def response(self, raw, *, resolutions=None):
        try:
            value = load_protocol_json(raw)
        except ValueError:
            return raw  # The page protocol validator owns malformed JSON.
        if not isinstance(value, dict):
            return raw
        errors = []

        def render(content, field):
            try:
                return self.render(content, field=field, resolutions=resolutions)
            except PageResponseError as error:
                if error.code not in {"page_link_ambiguous", "page_link_unresolved"}:
                    raise
                errors.extend(error.field_errors)
                return content

        for key in ("content", "description"):
            if isinstance(value.get(key), str):
                value[key] = render(value[key], key)
        if isinstance(value.get("edits"), list):
            for number, edit in enumerate(value["edits"]):
                if isinstance(edit, dict) and isinstance(edit.get("content"), str):
                    edit["content"] = render(edit["content"], f"edits:{number}")
        if errors:
            raise PageResponseError(
                "page_link_unresolved"
                if any(error["allow_unmatched"] for error in errors)
                else "page_link_ambiguous",
                "Each unresolved link requires a complete frozen identity decision",
                field_errors=tuple(errors),
            )
        return json.dumps(value, ensure_ascii=False)


def writing_catalogue(message, *, context, registry=None):
    registry = registry or FrozenLinkRegistry.from_message(message)
    if registry and (len(registry.targets) > 64 or registry.from_message(message) is None):
        return registry.message(context), registry
    return message, registry
