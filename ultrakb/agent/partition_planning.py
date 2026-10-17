"""Cover native work IDs before any page is created when full planning overflows."""

from collections.abc import Callable

from ultrakb import frontmatter
from ultrakb.agent.compile_plan import CompilePlan, CompilePlanError, PlanGroup
from ultrakb.agent.planning_capacity import capacity_error
from ultrakb.agent.planning_work import PROTOCOL, json_message, request_json_work
from ultrakb.agent.source_windows import validate_reading_locations
from ultrakb.compilation_report import report_compile_work
from ultrakb.compile_checkpoints import digest
from ultrakb.model_tokenizer import ContextCapacityError

PARTITION_INSTRUCTION = """Plan candidate knowledge pages from this bounded source partition.
Navigation labels locate originals; they are not evidence. Keep all necessary concepts,
entities, prerequisites and conditions. Do not create pages yet or assume that a page
does not already exist: a later global identity pass scans the full frozen catalog.
Return JSON with coverage and candidates arrays. coverage has exactly one object per
work ID: {"id": "native node_id", "candidates": ["local candidate ID"], "reason": "decision"}.
Use an empty candidates array and a nonempty reason when no independent page is needed.
Each candidate is {"id": "local ID", "kind": "concepts or entities", "name": "slug",
"title": "page title", "brief": "meaning and proposed contribution", "type": "entity type",
"reading_locations": {"node_ids": ["native ID"]}}. Every candidate must be referenced
by coverage. Keep names with different meanings distinct; equivalent names may be
coordinated later. Existing source numbering and native positions remain unchanged.
Context entries are ancestors/neighbors, not additional required work IDs.
When work contains an original window, use its reading_locations, not the opaque
work ID as a node_id. Original window work IDs identify reader slices only.
"""


def partition_messages(system: dict, work: list[dict], context: list[dict], types: list[str]):
    return [
        system,
        {"role": "user", "content": PARTITION_INSTRUCTION + "\nEntity types: " + ", ".join(types)},
        json_message(f"Planning partition ({PROTOCOL}):", {"work": work, "context": context}),
    ]


def _coverage(value, work, *, types, sanitize, known_node_ids):
    if not isinstance(value, dict) or set(value) != {"coverage", "candidates"}:
        raise ValueError("Expected coverage and candidates arrays")
    receipts, candidates = value["coverage"], value["candidates"]
    if not isinstance(receipts, list) or not isinstance(candidates, list):
        raise ValueError("Expected coverage and candidates arrays")
    expected = {item["node_id"] for item in work}
    seen, referenced = set(), set()
    for item in receipts:
        if not isinstance(item, dict):
            raise ValueError("Invalid coverage receipt")
        identity = item.get("id")
        if not isinstance(identity, str) or identity not in expected or identity in seen:
            raise ValueError("Coverage contains an unexpected or duplicate native ID")
        seen.add(identity)
        ids = item.get("candidates")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise ValueError(f"Invalid candidate references for {identity}")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError(f"A coverage decision requires a reason for {identity}")
        referenced.update(ids)
    if seen != expected:
        missing = next(iter(expected - seen), "unexpected")
        raise ValueError(f"Missing coverage conclusion for native ID {missing}")
    result = []
    ids = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Invalid candidate object")
        cid, kind = candidate.get("id"), candidate.get("kind")
        if (
            not isinstance(cid, str)
            or not cid
            or cid in ids
            or kind not in {"concepts", "entities"}
        ):
            raise ValueError("Candidate IDs must be unique; kind must be concepts or entities")
        ids.add(cid)
        for key in ("name", "title", "brief"):
            if not isinstance(candidate.get(key), str) or not candidate[key].strip():
                raise ValueError(f"Candidate {cid} requires {key}")
        if kind == "entities" and candidate.get("type") not in types:
            raise ValueError(f"Candidate {cid} requires a supported entity type")
        location = candidate.get("reading_locations")
        if location is None:
            if all("original" in item for item in work):
                location = work[0]["reading_locations"]
            else:
                location = {"node_ids": [r["id"] for r in receipts if cid in r["candidates"]]}
        result.append(
            {
                **candidate,
                "name": sanitize(candidate["name"]),
                "reading_locations": validate_reading_locations(location),
                **(
                    {"aliases": frontmatter.alias_values(candidate["aliases"])}
                    if "aliases" in candidate
                    else {}
                ),
            }
        )
        if not result[-1]["name"]:
            raise ValueError(f"Candidate {cid} has no usable page name")
        if any(
            node not in known_node_ids
            for node in result[-1]["reading_locations"].get("node_ids", [])
        ):
            raise ValueError(f"Candidate {cid} uses an unknown native node ID")
    if ids != referenced:
        raise ValueError("Coverage candidate references must match every candidate ID exactly")
    return {"coverage": receipts, "candidates": result}


def _context(work: list[dict], nodes: list[dict]) -> list[dict]:
    by_id = {node["node_id"]: node for node in nodes}
    selected = {node["node_id"] for node in work}
    wanted = set()
    for index, node in enumerate(nodes):
        if node["node_id"] not in selected:
            continue
        parent = node.get("parent_node_id")
        chain = set()
        while parent in by_id and parent not in chain:
            chain.add(parent)
            wanted.add(parent)
            parent = by_id[parent].get("parent_node_id")
        if index:
            wanted.add(nodes[index - 1]["node_id"])
        if index + 1 < len(nodes):
            wanted.add(nodes[index + 1]["node_id"])
    return [node for node in nodes if node["node_id"] in wanted - selected]


def _split(work):
    """Prefer original parent-section boundaries, then divide the native order."""
    middle = len(work) // 2
    boundaries = [
        index
        for index in range(1, len(work))
        if work[index].get("parent_node_id") != work[index - 1].get("parent_node_id")
    ]
    cut = min(boundaries, key=lambda index: abs(index - middle)) if boundaries else middle
    return work[:cut], work[cut:]


def _original_work(window):
    if pages := window.get("page_spans"):
        location = {
            "pages": ",".join(str(page) for page in dict.fromkeys(p["page"] for p in pages))
        }
    elif blocks := window.get("block_range"):
        location = {"blocks": ",".join(map(str, blocks))}
    elif cells := window.get("cell_range"):
        location = {"cells": ":".join(cells) if isinstance(cells, list) else cells}
    else:
        spans = window.get("source_spans", [])
        start = min((span[0] for span in spans), default=window.get("selection_offset", 0))
        end = max((span[1] for span in spans), default=start + len(window["content"]))
        location = {"chars": f"{start}:{end}"}
    return {"node_id": digest(window), "reading_locations": location, "original": window}


def partitioned_plan(
    *,
    system: dict,
    windows,
    fits: Callable,
    send: Callable,
    entity_types: list[str],
    sanitize: Callable,
    checkpoints=None,
    wiki_dir=None,
) -> CompilePlan:
    """Produce a complete, globally coordinated plan from bounded persisted work."""
    # CompileSourceWindows exposes a flat native order while retaining each
    # node's children. Traversing that list recursively would duplicate IDs.
    parents = {
        child.get("node_id"): node.get("node_id")
        for node in windows.navigation
        for child in node.get("nodes", [])
    }
    nodes = []
    for node in windows.navigation:
        item = {k: node[k] for k in ("node_id", "title", "start_index", "end_index") if k in node}
        if parent := parents.get(node.get("node_id")):
            item["parent_node_id"] = parent
        nodes.append(item)
    ids = [node.get("node_id") for node in nodes]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ContextCapacityError("Partition planning requires unique retained native node IDs")
    identity = windows.original_identity()
    candidates = []
    completed = splits = 0
    pending = len(nodes)

    def progress(*, in_flight=0):
        report_compile_work(
            "planning", completed=completed, pending=pending, in_flight=in_flight, splits=splits
        )

    def complete(work, path):
        nonlocal completed, pending, splits
        context = _context(work, nodes) if nodes else []
        messages = partition_messages(system, work, context, entity_types)
        key = "candidates:" + digest([identity, path, work])
        signature = digest([PROTOCOL, identity, path, work])
        split = checkpoints.load("plan-split:" + key, signature) if checkpoints else None
        if not split:
            try:
                progress(in_flight=1)
                result = request_json_work(
                    messages,
                    key=key,
                    stage="concepts-plan-partition",
                    send=send,
                    fits=fits,
                    checkpoints=checkpoints,
                    identity=identity,
                    validate=lambda value: _coverage(
                        value,
                        work,
                        types=entity_types,
                        sanitize=sanitize,
                        known_node_ids=set(ids),
                    ),
                )
            except ContextCapacityError:
                pass
            except CompilePlanError:
                if len(work) < 2:
                    raise
            else:
                candidates.extend(
                    {**candidate, "id": digest([key, candidate["id"]])}
                    for candidate in result["candidates"]
                )
                completed += len(work)
                pending -= len(work)
                progress()
                return
        if len(work) < 2:
            raise capacity_error(
                f"Native planning work {work[0]['node_id']} with required ancestors/neighbor "
                "context cannot fit captured capacity. The original and checkpoints are retained.",
                fits,
            )
        children = _split(work)
        splits += 1
        progress()
        if checkpoints and not split:
            checkpoints.save("plan-split:" + key, signature, {"children": ["0", "1"]})
        for index, child in enumerate(children):
            complete(child, f"{path}.{index}")

    if nodes:
        progress()
        complete(nodes, "navigation")
    else:

        def fits_original(window):
            work = [_original_work(window)]
            return fits(partition_messages(system, work, [], entity_types))

        originals = list(windows.iter_document_windows(fits=fits_original))
        pending = len(originals)
        progress()
        for index, window in enumerate(originals):
            work = [_original_work(window)]
            complete(work, f"original.{index}")
    if not candidates:
        return CompilePlan(PlanGroup(), PlanGroup())
    from ultrakb.agent.planning_identity import coordinate_candidates

    def resolve_locations(location):
        if set(location) - {"node_ids", "offset", "part"}:
            return {
                key: value for key, value in location.items() if key not in {"node_ids", "offset"}
            }
        requested = set(location.get("node_ids", []))
        selected = set(requested)
        for node in nodes:
            if node.get("parent_node_id") in selected:
                selected.add(node["node_id"])
        ranges = [
            f"{node['start_index']}-{node['end_index']}"
            for node in nodes
            if node["node_id"] in selected
        ]
        key = "blocks" if windows.original_path.endswith(".content.json") else "pages"
        return {key: ",".join(ranges)}

    return coordinate_candidates(
        candidates,
        wiki_dir=wiki_dir or windows.wiki,
        system=system,
        fits=fits,
        send=send,
        checkpoints=checkpoints,
        identity=identity,
        sanitize=sanitize,
        resolve_locations=resolve_locations,
    )
