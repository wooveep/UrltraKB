"""Coordinate candidates against a frozen full catalog through bounded comparisons."""

import re
from pathlib import Path

from ultrakb import frontmatter
from ultrakb.agent.compile_plan import CompilePlan, CompilePlanError, PlanGroup
from ultrakb.agent.planning_capacity import capacity_error
from ultrakb.agent.planning_work import PROTOCOL, json_message, request_json_work
from ultrakb.compilation_report import report_compile_work
from ultrakb.compile_checkpoints import digest
from ultrakb.model_tokenizer import ContextCapacityError

IDENTITY_INSTRUCTION = """Resolve the candidate's identity against this catalog partition.
The full catalog stays local. This is one partition, so absence here does not establish
that the candidate is new. Compare every supplied ID, including synonyms. A shared
title or alias alone does not establish the same meaning. Preserve homonyms, entity
types and the distinction between abstract concepts and specific named entities.
Return JSON {"matches": [{"id": "catalog ID", "relation": "same or different",
"action": "update or related", "reason": "identity decision"}]} with exactly one
receipt per supplied catalog ID. Use same only for the same object/meaning and scope.
Use related when this candidate adds navigation only, update for a necessary source
contribution. A proposed earlier candidate has existing=false; same then merges its
source contributions before page generation. Catalog descriptions are finding aids,
not evidence for page bodies. Description fragments must be considered together;
different on a fragment means no positive identity established by that fragment.
"""


def frozen_catalog(wiki: Path) -> list[dict]:
    """Retain every catalog identity and metadata entry, including aliases and type."""
    entries = []
    for kind in ("concepts", "entities"):
        for path in sorted((wiki / kind).glob("*.md")):
            text = path.read_text("utf-8")
            metadata = frontmatter.parse(text)
            try:
                aliases = frontmatter.alias_values(metadata.get("aliases"), allow_scalar=True)
            except ValueError as error:
                raise CompilePlanError(
                    f"Invalid aliases in existing catalog page {kind}/{path.stem}",
                    "compile_identity_catalog_invalid",
                ) from error
            description = metadata.get("description", metadata.get("brief", ""))
            if description is None:
                description = ""
            if not isinstance(description, str):
                raise CompilePlanError(
                    f"Invalid description in existing catalog page {kind}/{path.stem}",
                    "compile_identity_catalog_invalid",
                )
            if not description.strip():
                parts = frontmatter.split(text)
                description = parts[1] if parts is not None else text
            entries.append(
                {
                    "id": f"{kind}/{path.stem}",
                    "kind": kind,
                    "name": path.stem,
                    "title": str(metadata.get("title", path.stem)),
                    "aliases": aliases,
                    "description": description,
                    "type": str(metadata.get("type") or "other").strip().lower()
                    if kind == "entities"
                    else None,
                    "existing": True,
                    "base_sha256": digest(text),
                }
            )
    return entries


def identity_messages(system, candidate, entries):
    descriptor = {
        key: value
        for key, value in candidate.items()
        if key not in {"reading_locations", "evidence_units", "contributions"}
    }
    return [
        system,
        {"role": "user", "content": IDENTITY_INSTRUCTION},
        json_message(
            "Candidate identity comparison:", {"candidate": descriptor, "catalog": entries}
        ),
    ]


def _matches(value, entries, candidate):
    if (
        not isinstance(value, dict)
        or set(value) != {"matches"}
        or not isinstance(value["matches"], list)
    ):
        raise ValueError("Expected a matches array")
    expected = {item["id"]: item for item in entries}
    seen = set()
    for item in value["matches"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid identity receipt")
        identity = item.get("id")
        if not isinstance(identity, str) or identity not in expected or identity in seen:
            raise ValueError("Identity receipts contain an unexpected or duplicate ID")
        seen.add(identity)
        if item.get("relation") not in {"same", "different"} or item.get("action") not in {
            "update",
            "related",
        }:
            raise ValueError(f"Identity {identity} requires relation and action")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError(f"Identity {identity} requires a decision reason")
        target = expected[identity]
        if item["relation"] == "same" and (
            candidate["kind"] != target["kind"]
            or candidate.get("type", "other") != target.get("type", "other")
            and candidate["kind"] == "entities"
            and "other" not in (candidate.get("type"), target.get("type"))
        ):
            raise ValueError(
                f"Identity {identity} crosses concept/entity or entity-type boundaries"
            )
    if seen != set(expected):
        raise ValueError("An identity receipt is required for every supplied catalog ID")
    return value["matches"]


def _compare(candidate, entries, *, system, fits, send, checkpoints, identity):
    """Exhaustive partition scan is the correctness fallback after exact names/aliases."""
    if not entries:
        return []
    messages = identity_messages(system, candidate, entries)
    split_key = "identity-split:" + digest([candidate["id"], entries])
    split_signature = digest([PROTOCOL, messages, identity])
    split = checkpoints.load(split_key, split_signature) if checkpoints else None
    if not split and fits(messages):
        try:
            return request_json_work(
                messages,
                key="identity:" + digest([candidate["id"], entries]),
                stage="concepts-plan-identity",
                fits=fits,
                send=send,
                checkpoints=checkpoints,
                identity=identity,
                validate=lambda value: _matches(value, entries, candidate),
            )

        except ContextCapacityError:
            pass
        except CompilePlanError:
            if len(entries) < 2:
                raise
    if checkpoints and not split:
        checkpoints.save(split_key, split_signature, {"split": True})
    if len(entries) > 1:
        middle = len(entries) // 2
        return [
            receipt
            for part in (entries[:middle], entries[middle:])
            for receipt in _compare(
                candidate,
                part,
                system=system,
                fits=fits,
                send=send,
                checkpoints=checkpoints,
                identity=identity,
            )
        ]
    entry = entries[0]
    description = entry.get("description", "")
    if len(description) < 2:
        raise capacity_error(
            f"Identity comparison for {candidate['id']} and {entry['id']} cannot fit "
            "its minimum names, aliases, types and rules; all candidates are retained.",
            fits,
        )
    cut = len(description) // 2
    matches = [
        receipt
        for index, fragment in enumerate((description[:cut], description[cut:]))
        for receipt in _compare(
            candidate,
            [
                {
                    **entry,
                    "description": fragment,
                    "fragment": entry.get("fragment", "") + str(index),
                }
            ],
            system=system,
            fits=fits,
            send=send,
            checkpoints=checkpoints,
            identity=identity,
        )
    ]
    # A positive fragment cannot override another fragment's scope or meaning.
    # Ask for the final identity decision over every retained fragment receipt.
    # These generated decisions coordinate identity; they never become page evidence.
    decision_entry = {
        **{key: value for key, value in entry.items() if key not in {"description", "fragment"}},
        "description": "Generated identity receipts for all description fragments; "
        "reconcile every scope/meaning decision before selecting same or different.",
        "fragment_decisions": matches,
    }
    while True:
        reduction = identity_messages(system, candidate, [decision_entry])
        key = "identity-reconcile:" + digest([candidate["id"], decision_entry])
        split_signature = digest([PROTOCOL, reduction, identity])
        split = checkpoints.load(key + ":reduce", split_signature) if checkpoints else None
        try:
            if split:
                raise ContextCapacityError("Resume the retained identity reduction")
            return request_json_work(
                reduction,
                key=key,
                stage="concepts-plan-identity",
                fits=fits,
                send=send,
                checkpoints=checkpoints,
                identity=identity,
                validate=lambda value: _matches(value, [decision_entry], candidate),
            )
        except ContextCapacityError:
            if checkpoints and not split:
                checkpoints.save(key + ":reduce", split_signature, {"reduce": True})
            from ultrakb.agent.identity_reconciliation import fit_identity_decisions

            decision_entry = fit_identity_decisions(
                decision_entry,
                candidate=candidate,
                messages=lambda entry: identity_messages(system, candidate, [entry]),
                system=system,
                fits=fits,
                send=send,
                checkpoints=checkpoints,
                identity=identity,
                force=True,
            )


def _merge_locations(first: dict, second: dict, *, resolve=None) -> dict:
    if first == second:
        return first
    if "node_ids" in first or "node_ids" in second:
        if set(first) == set(second) == {"node_ids"}:
            return {"node_ids": list(dict.fromkeys([*first["node_ids"], *second["node_ids"]]))}
        if resolve is None:
            raise CompilePlanError(
                "Mixed identity reading coordinate systems", "compile_identity_scope"
            )
        return _merge_locations(resolve(first), resolve(second))
    for key in ("pages", "blocks"):
        if key in first and key in second:
            from pageindex.index.utils import parse_pages

            return {
                key: ",".join(
                    map(str, sorted(set(parse_pages(first[key])) | set(parse_pages(second[key]))))
                )
            }
    if "chars" in first and "chars" in second:
        ranges = [[int(n) for n in value["chars"].split(":")] for value in (first, second)]
        return {"chars": f"{min(r[0] for r in ranges)}:{max(r[1] for r in ranges)}"}
    if "cells" in first and "cells" in second:
        # The enclosing rectangle retains both selections, including headers between them.
        points = re.findall(r"([A-Za-z]+)([1-9][0-9]*)", first["cells"] + ":" + second["cells"])
        if points:

            def column(label):
                number = 0
                for char in label.upper():
                    number = number * 26 + ord(char) - 64
                return number

            columns = sorted((column(label), label.upper()) for label, _ in points)
            rows = [int(row) for _, row in points]
            return {"cells": f"{columns[0][1]}{min(rows)}:{columns[-1][1]}{max(rows)}"}
    raise CompilePlanError("Incompatible identity source selections", "compile_identity_scope")


def coordinate_candidates(
    candidates,
    *,
    wiki_dir,
    system,
    fits,
    send,
    checkpoints,
    identity,
    sanitize,
    resolve_locations=None,
) -> CompilePlan:
    """Finish all identity decisions before any page generation or disk write."""
    frozen = frozen_catalog(wiki_dir)
    catalog = [dict(entry) for entry in frozen]
    base_descriptions = {entry["id"]: entry["description"] for entry in frozen}
    occupied = {entry["name"] for entry in catalog}
    accepted: dict[str, dict] = {}
    actions: dict[str, str] = {}
    for index, candidate in enumerate(candidates):
        report_compile_work(
            "planning-identities", completed=index, pending=len(candidates) - index, in_flight=1
        )
        # Names and aliases are examined first, but a negative shortlist always
        # continues through the complete catalog, including earlier candidates.
        names = {
            value.casefold()
            for value in (candidate["name"], candidate["title"], *candidate.get("aliases", []))
        }
        targets = [entry for entry in catalog if entry["kind"] == candidate["kind"]]
        exact: list[dict] = []
        rest: list[dict] = []
        for entry in targets:
            entry_names = [entry["name"], entry["title"], *entry.get("aliases", [])]
            (exact if names.intersection(n.casefold() for n in entry_names) else rest).append(entry)
        receipts = _compare(
            candidate,
            exact,
            system=system,
            fits=fits,
            send=send,
            checkpoints=checkpoints,
            identity=identity,
        )
        matches = [item for item in receipts if item["relation"] == "same"]
        if not matches:
            receipts = _compare(
                candidate,
                rest,
                system=system,
                fits=fits,
                send=send,
                checkpoints=checkpoints,
                identity=identity,
            )
            matches = [item for item in receipts if item["relation"] == "same"]
        if len(matches) > 1:
            raise CompilePlanError(
                "Multiple existing identities match candidate "
                + candidate["id"]
                + "; explicit canonical identity is required: "
                + ", ".join(m["id"] for m in matches),
                "compile_identity_ambiguous",
            )
        if matches:
            match = matches[0]
            target = next(entry for entry in targets if entry["id"] == match["id"])
            target_id = target["id"]
            item = {**candidate, "name": target["name"], "title": target["title"]}
            action = match["action"] if target["existing"] else "create"
            if target_id in accepted:
                previous = accepted[target_id]
                item = {
                    **previous,
                    "brief": previous["brief"] + "\n" + candidate["brief"],
                    "reading_locations": _merge_locations(
                        previous["reading_locations"],
                        candidate["reading_locations"],
                        resolve=resolve_locations,
                    ),
                }
                if actions[target_id] in {"create", "update"}:
                    action = actions[target_id]
        else:
            name = candidate["name"]
            if name in occupied:
                name = sanitize(name + "-" + digest([candidate["kind"], candidate["id"]])[:10])
            if name in occupied:
                raise CompilePlanError(
                    "Unresolved planned path collision", "compile_identity_collision"
                )
            occupied.add(name)
            item = {**candidate, "name": name}
            target_id, action = f"{candidate['kind']}/{name}", "create"
            catalog.append(
                {
                    "id": target_id,
                    "kind": candidate["kind"],
                    "name": name,
                    "title": candidate["title"],
                    "aliases": [],
                    "description": candidate["brief"],
                    "type": candidate.get("type"),
                    "existing": False,
                }
            )
        accepted[target_id], actions[target_id] = item, action
        descriptor = next(entry for entry in catalog if entry["id"] == target_id)
        descriptor["aliases"] = list(
            dict.fromkeys(
                [
                    *descriptor.get("aliases", []),
                    candidate["name"],
                    candidate["title"],
                    *candidate.get("aliases", []),
                ]
            )
        )
        descriptor["description"] = base_descriptions.get(target_id, "") + (
            "\nProposed source contributions (generated identity aids, not evidence):\n"
            + item["brief"]
        )
        item["aliases"] = descriptor["aliases"]
        report_compile_work(
            "planning-identities", completed=index + 1, pending=len(candidates) - index - 1
        )
    groups = {"concepts": PlanGroup(), "entities": PlanGroup()}
    for target_id, item in accepted.items():
        group, action = groups[item["kind"]], actions[target_id]
        if action == "related":
            group.related.append(item["name"])
        else:
            getattr(group, action).append(
                {
                    k: v
                    for k, v in item.items()
                    if k
                    in {
                        "name",
                        "title",
                        "type",
                        "brief",
                        "reading_locations",
                        "evidence_units",
                        "aliases",
                    }
                }
            )
    return CompilePlan(groups["concepts"], groups["entities"])
