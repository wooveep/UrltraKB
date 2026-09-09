"""Optional local PageIndex enhancement of immutable, complete source positions."""

from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import version as package_version

from openkb.cancellation import OperationCancelled
from openkb.evidence import Evidence, ParseStore
from openkb.implementation import module_revision
from openkb.locks import LockCancelled, atomic_write_json
from openkb.processing import (
    ProcessingIncomplete,
    independent_processing_scope,
    navigation_execution,
    processing_checkpoint,
    validate_usage,
)
from openkb.sources import SourceStore, content_id, read_object


def _location_rows(source, parsed):
    return [
        {
            "block_id": block.id,
            "kind": block.kind,
            "location": block.location,
            "reference": asdict(Evidence(source.source_id, source.id, parsed.id, block.id)),
            "order": block.order,
        }
        for block in parsed.blocks
    ]


def read_navigation(kb_dir, source, *, offset=0, limit=100):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Invalid navigation window")
    store = SourceStore(kb_dir)
    root = store.owned_path(store.root / "navigation")
    pointer = store.owned_path(root / "latest" / f"{source.id}.json")
    if not pointer.exists():
        return None
    selected = read_object(pointer)
    if set(selected) != {"navigation"}:
        raise ValueError("Invalid navigation pointer")
    identity = selected["navigation"]
    from openkb.sources import valid_id

    record = read_object(store.owned_path(root / f"{valid_id(identity)}.json"))
    if (
        set(record)
        != {"source_id", "version", "parse", "profile", "status", "reason", "positions", "usage"}
        or content_id(record) != identity
        or record.get("version") != source.id
        or record.get("source_id") != source.source_id
        or not isinstance(record.get("status"), str)
        or record["status"] not in {"basic", "enhanced", "degraded"}
        or (record.get("reason") is not None and not isinstance(record["reason"], str))
    ):
        raise ValueError("Navigation identity mismatch")
    valid_id(record["profile"])
    parsed = ParseStore(kb_dir).load(valid_id(record["parse"]))
    if parsed.input_key != source.input_key:
        raise ValueError("Navigation parsing identity mismatch")
    rows = record["positions"]
    expected = _location_rows(source, parsed)
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("Invalid navigation coverage")
    for row, original in zip(rows, expected):
        if (
            not isinstance(row, dict)
            or {key: row.get(key) for key in original} != original
            or set(row) - original.keys() - {"title", "summary", "parents"}
            or any(key in row and not isinstance(row[key], str) for key in ("title", "summary"))
            or not isinstance(row.get("parents", []), list)
            or any(
                type(parent) is not int or not 1 <= parent <= len(rows)
                for parent in row.get("parents", [])
            )
        ):
            raise ValueError("Invalid navigation position")
    validate_usage(record["usage"])
    return {
        **record,
        "id": identity,
        "positions": rows[offset : offset + limit],
        "total_positions": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }


def build_navigation(kb_dir, source, parsed, settings, *, bundle=None):
    """Run only after mandatory publication, or as an explicit separate rebuild."""
    store = SourceStore(kb_dir)
    positions = _location_rows(source, parsed)
    options = settings.get("navigation") or {}
    record = {
        "source_id": source.source_id,
        "version": source.id,
        "parse": parsed.id,
        "profile": content_id(
            {
                "options": options,
                "model": settings.get("model"),
                "pageindex": package_version("pageindex"),
                "adapter": module_revision(__name__),
                "endpoint": content_id(getattr(bundle, "base_url", None)),
                "headers": content_id(getattr(bundle, "extra_headers", None)),
            }
        ),
        "status": "basic",
        "reason": None,
        "positions": positions,
        "usage": {},
    }
    budget = None
    run = None
    if options.get("enabled") is True:
        record.update(status="degraded", reason="navigation_interrupted")
    _save_navigation(store, source, record)
    try:
        if options.get("enabled") is True:
            with independent_processing_scope(options) as budget, navigation_execution():
                from openkb.navigation_usage import NavigationRun

                run = NavigationRun(store, source, parsed, record["profile"])
                budget.on_observation = run.observe
                processing_checkpoint("navigation")
                _enhance(positions, kb_dir, source, parsed, settings, bundle, budget.limits)
                record["status"] = "enhanced"
                record["reason"] = None
    except (OperationCancelled, LockCancelled):
        record.update(status="degraded", reason="navigation_stopped")
    except ProcessingIncomplete as exc:
        record.update(status="degraded", reason=exc.reason)
    except Exception as exc:
        record.update(status="degraded", reason=f"navigation_failed:{type(exc).__name__}")
    if budget is not None and run is not None:
        record["usage"] = run.finish(budget)
    _save_navigation(store, source, record)
    return record


def _save_navigation(store, source, record):
    identity = content_id(record)
    root = store.owned_path(store.root / "navigation")
    atomic_write_json(store.owned_path(root / f"{identity}.json"), record)
    atomic_write_json(
        store.owned_path(root / "latest" / f"{source.id}.json"), {"navigation": identity}
    )


def _enhance(positions, kb_dir, source, parsed, settings, bundle, limits):
    from pageindex import IndexConfig
    from pageindex.index.pipeline import build_index
    from pageindex.parser.protocol import ContentNode, ParsedDocument
    from pageindex.tokens import count_tokens

    reader = ParseStore(kb_dir).reader(source, parsed)
    nodes = []
    for number, block in enumerate(parsed.blocks, 1):
        processing_checkpoint()
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=max(block.chars, len(block.context), 1),
        )
        level = max(1, len(block.location.get("headings", [])))
        nodes.append(
            ContentNode(
                content=view.text,
                tokens=count_tokens(view.text, settings["model"]),
                title=view.text if block.kind == "heading" else f"Block {number}",
                index=number,
                level=level if block.kind == "heading" else level + 1,
            )
        )
    config = IndexConfig(
        model=settings["model"],
        max_concurrency=limits.concurrency,
        if_add_node_text=False,
        if_add_doc_description=False,
        llm_params={
            "api_key": getattr(bundle, "api_key", None),
            "api_base": getattr(bundle, "base_url", None),
            "extra_headers": getattr(bundle, "extra_headers", None),
            "timeout": limits.request_timeout,
        },
    )
    # This is the pinned local pipeline, with our validated parser output. It
    # neither uploads originals nor invokes SDK backend auto-selection.
    tree = build_index(ParsedDocument(source.name, nodes), model=settings["model"], opt=config)
    seen = set()
    enhancements = {}

    def map_nodes(values, parents):
        if not isinstance(values, list):
            raise ValueError("Invalid local navigation nodes")
        for node in values:
            if not isinstance(node, dict) or type(node.get("line_num")) is not int:
                raise ValueError("Invalid local navigation position")
            number = node["line_num"]
            if not 1 <= number <= len(positions) or number in seen:
                raise ValueError("Invalid local navigation coverage")
            seen.add(number)
            if not isinstance(node.get("title"), str) or not isinstance(
                node.get("summary", ""), str
            ):
                raise ValueError("Invalid local navigation description")
            enhancements[number - 1] = {
                "title": node["title"],
                "summary": node.get("summary", ""),
                "parents": list(parents),
            }
            map_nodes(node.get("nodes", []), [*parents, number])

    map_nodes(tree["structure"], [])
    if seen != set(range(1, len(positions) + 1)):
        raise ValueError("Incomplete local navigation coverage")
    for index, value in enhancements.items():
        positions[index].update(value)
