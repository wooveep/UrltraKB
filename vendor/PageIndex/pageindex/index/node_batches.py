"""Bound independent node work while retaining a receipt for each native location."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..config import get_llm_params
from ..errors import IndexCapacityError, IndexQualityError
from ..llm import current_client, llm_client_scope

CONTRACT = "independent-node-work-v1"
MAX_BATCH_NODES = 8
_BATCH_SIZE = ContextVar("pageindex_node_batch_size", default=MAX_BATCH_NODES)


@contextmanager
def node_batch_scope(size):
    """Change scheduling only; accepted node identities remain reusable."""
    if type(size) is not int or not 1 <= size <= MAX_BATCH_NODES:
        raise ValueError(f"node_batch_size must be between 1 and {MAX_BATCH_NODES}")
    token = _BATCH_SIZE.set(size)
    try:
        yield
    finally:
        _BATCH_SIZE.reset(token)


class NodeBatchError(IndexQualityError):
    """Only the named node/sub-batch needs recovery, never a document rebuild."""

    local_recovery_exhausted = True


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass
class NodeWork:
    key: str
    target: dict
    contexts: list[dict]

    @property
    def id(self):
        return "n-" + self.key[:20]


def node_work(kind, node, contexts, model, *, source, position, parent=None):
    identity = {
        "contract": CONTRACT,
        "kind": kind,
        "model": model,
        "settings": get_llm_params(),
        "source": source,
        "position": position,
        "node": node,
        "contexts": contexts,
        "parent": parent,
    }
    key = digest(identity)
    target = {k: v for k, v in node.items() if k != "text"}
    target["context_ids"] = [context["id"] for context in contexts]
    if parent:
        target["parent"] = parent
    return NodeWork(key, target, contexts)


def render(kind, works, repair=None):
    contexts = {context["id"]: context for work in works for context in work.contexts}
    payload = {
        "contract": CONTRACT,
        "operation": kind,
        "nodes": [{"id": work.id, **work.target} for work in works],
        "contexts": list(contexts.values()),
    }
    if repair:
        payload["repair"] = repair
    instruction = (
        "Check every independent section heading against its assigned original contexts. "
        "Physical indices are native one-based page positions, never printed labels. "
        "Only the node's declared physical_index can prove answer yes; adjacent "
        "pages provide context only. Repeated headers, TOC references and ordinary "
        "word occurrences are not section headings. Preserve the exact node IDs. "
        'Return JSON {"results":[{"id":"the supplied id","answer":"yes or no",'
        '"belongs_to_parent":true,"thinking":"brief reason"}]}. '
        "belongs_to_parent is true only when the heading belongs inside the given "
        "parent bounds. Return exactly one result per requested ID and no other IDs."
    )
    if kind == "summary":
        instruction = (
            "Generate a concise navigation summary for each independent node. Use "
            "only its assigned original contexts. Retain main topics, distinguishing "
            "conditions, exceptions and hierarchy relationships. These summaries "
            "are retrieval aids, not replacement original evidence. Preserve the "
            'exact node IDs. Return JSON {"results":[{"id":"the supplied id",'
            '"summary":"concise navigation summary"}]}. Return exactly one '
            "nonempty summary per requested ID and no other IDs."
        )
    elif kind == "start":
        instruction = (
            "Check independently whether each supplied section title begins its "
            "assigned original page. Earlier body text or another heading means "
            "start_begin no; ignore whitespace differences. Preserve exact node IDs. "
            'Return JSON {"results":[{"id":"the supplied id",'
            '"start_begin":"yes or no"}]}. Return exactly one result for '
            "every requested ID and no other IDs."
        )
    return instruction + "\nNode work:\n" + json.dumps(payload, ensure_ascii=False)


def validate_value(kind, value):
    if kind == "summary":
        summary = value.get("summary") if isinstance(value, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise NodeBatchError("Invalid per-node navigation summary")
        result = {"summary": summary}
        if value.get("summary_status") == "unavailable":
            result["summary_status"] = "unavailable"
        return result
    if kind == "start":
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("start_begin"), str)
            or value["start_begin"] not in {"yes", "no"}
        ):
            raise NodeBatchError("Invalid per-node page-start verdict")
        return {"start_begin": value["start_begin"]}
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("answer"), str)
        or value["answer"] not in {"yes", "no"}
    ):
        raise NodeBatchError("Invalid per-node heading verdict")
    if "belongs_to_parent" in value and type(value["belongs_to_parent"]) is not bool:
        raise NodeBatchError("Invalid per-node parent verdict")
    return {k: value[k] for k in ("answer", "belongs_to_parent") if k in value}


def decode_receipts(raw, works, kind):
    wanted = {work.id for work in works}
    try:
        value = json.loads(raw)
        rows = value["results"]
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or row["id"] not in wanted
            for row in rows
        ):
            raise ValueError("Out-of-range or malformed node ID")
    except (ValueError, KeyError, TypeError):
        return {}, {key: "invalid_receipt" for key in wanted}
    accepted, issues = {}, {}
    for key in wanted:
        matching = [row for row in rows if row["id"] == key]
        if len(matching) != 1:
            issues[key] = "missing_id" if not matching else "duplicate_id"
            continue
        try:
            accepted[key] = validate_value(kind, matching[0])
        except NodeBatchError:
            issues[key] = "invalid_value"
    return accepted, issues


class _RetainedMissing(Exception):
    pass


class _RetainedOnly:
    """Replay the original scalar algorithm without permitting any model send."""

    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    def fits(self, messages, *, stage):
        # Capacity restricts new sends. A delivered exact request can still be
        # validated after a smaller scheduling/request budget was selected.
        if self.client.retained_response(messages, stage=stage) is not None:
            return True
        measure = getattr(self.client, "fits", None)
        return measure(messages, stage=stage) if measure else True

    def complete(self, messages, *, stage):
        value = self.client.retained_response(messages, stage=stage)
        if value is None:
            raise _RetainedMissing()
        return value

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)


class NodeRequests:
    """Append-only exact dispatch manifests, separate from accepted node receipts."""

    def __init__(self, client, works, kind):
        self.client = client
        self.retained = getattr(client, "stage_result", None)
        self.works = {work.key: work for work in works}
        self.kind = kind
        self.key = "node-dispatch:" + digest(
            {"contract": CONTRACT, "kind": kind, "works": list(self.works)}
        )
        header = self.retained(self.key) if self.retained else None
        count = header.get("count") if isinstance(header, dict) else 0
        if type(count) is not int or count < 0:
            raise NodeBatchError("Invalid node dispatch manifest")
        self.entries = []
        self.seen = set()
        for index in range(count):
            entry = self.retained(f"{self.key}:{index}")
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("mode"), str)
                or entry["mode"] not in {"batch", "scalar"}
                or not isinstance(entry.get("keys"), list)
                or not entry["keys"]
                or any(not isinstance(key, str) or key not in self.works for key in entry["keys"])
                or len(set(entry["keys"])) != len(entry["keys"])
            ):
                raise NodeBatchError("Invalid frozen node dispatch")
            self.entries.append(entry)
            self.seen.add(digest(entry))

    def record(self, group, *, mode="batch", repair=None):
        if self.retained is None:
            return
        entry = {"mode": mode, "keys": [work.key for work in group]}
        if mode == "batch":
            entry.update(repair=repair, prompt_digest=digest(render(self.kind, group, repair)))
        signature = digest(entry)
        if signature in self.seen:
            return
        # Save the entry before its small head record. No ever-growing JSON
        # array is rewritten on each completed node or dispatch.
        self.retained(f"{self.key}:{len(self.entries)}", entry)
        self.retained(self.key, {"count": len(self.entries) + 1})
        self.entries.append(entry)
        self.seen.add(signature)

    async def replay(self, fallback, accept, results):
        reader = getattr(self.client, "retained_response", None)
        if reader is None:
            return
        stage = "summary" if self.kind == "summary" else "verification"
        for entry in self.entries:
            group = [self.works[key] for key in entry["keys"]]
            if all(work.id in results for work in group):
                continue
            if entry["mode"] == "scalar":
                if len(group) != 1 or fallback is None:
                    raise NodeBatchError("Invalid scalar node dispatch")
                try:
                    with llm_client_scope(_RetainedOnly(self.client)):
                        value = await fallback(group[0])
                except _RetainedMissing:
                    continue
                accept(group[0], validate_value(self.kind, value))
                continue
            prompt = render(self.kind, group, entry.get("repair"))
            if digest(prompt) != entry.get("prompt_digest"):
                # A changed request protocol must never masquerade as a saved
                # request. Per-node accepted results are independently retained.
                continue
            response = reader([{"role": "user", "content": prompt}], stage=stage)
            if response is None or response.finish_reason != "finished":
                continue
            accepted, _ = decode_receipts(response.text, group, self.kind)
            for work in group:
                if work.id in accepted:
                    accept(work, accepted[work.id])


async def run_node_batches(works, kind, model, fallback=None):
    from .utils import llm_acompletion

    if not works:
        return []
    client = current_client()
    retained = getattr(client, "stage_result", None)
    results, pending = {}, []
    if len({work.id for work in works}) != len(works):
        raise NodeBatchError("Node work identities must be unique")
    for work in works:
        previous = retained("node-work:" + work.key) if retained else None
        if previous is None:
            pending.append(work)
        else:
            results[work.id] = validate_value(kind, previous)

    def accept(work, value):
        if work.id not in results:
            if retained:
                retained("node-work:" + work.key, value)
            results[work.id] = value

    requests = NodeRequests(client, works, kind)
    await requests.replay(fallback, accept, results)
    pending = [work for work in pending if work.id not in results]

    async def process(group):
        if len(group) == 1 and fallback is not None and (_BATCH_SIZE.get() == 1 or len(works) == 1):
            requests.record(group, mode="scalar")
            value = validate_value(kind, await fallback(group[0]))
            accept(group[0], value)
            return
        repair = None
        for attempt in range(3):
            prompt = render(kind, group, repair)
            requests.record(group, repair=repair)
            try:
                stage = "summary" if kind == "summary" else "verification"
                reader = getattr(client, "retained_response", None)
                response = (
                    reader([{"role": "user", "content": prompt}], stage=stage) if reader else None
                )
                if response is not None:
                    if response.finish_reason != "finished":
                        raise IndexCapacityError("Retained node response did not finish")
                    raw = response.text
                else:
                    raw = await llm_acompletion(model, prompt, usage_stage=stage)
            except IndexCapacityError:
                if len(group) == 1:
                    if fallback is None:
                        raise
                    requests.record(group, mode="scalar")
                    value = validate_value(kind, await fallback(group[0]))
                    accept(group[0], value)
                    return
                middle = len(group) // 2
                await process(group[:middle])
                await process(group[middle:])
                return
            accepted, issues = decode_receipts(raw, group, kind)
            for work in group:
                if work.id in accepted:
                    accept(work, accepted[work.id])
            if not issues:
                return
            if retained:
                retained(
                    "node-batch-diagnostic:" + digest(prompt),
                    {"raw": raw, "issues": issues, "contract": CONTRACT},
                )
            group = [work for work in group if work.id in issues]
            repair = {"attempt": attempt + 1, "issues": issues}
        if len(group) == 1:
            raise NodeBatchError("Node receipt repair exhausted: " + group[0].id)
        middle = len(group) // 2
        await process(group[:middle])
        await process(group[middle:])

    size = _BATCH_SIZE.get()
    for offset in range(0, len(pending), size):
        await process(pending[offset : offset + size])
    return [results[work.id] for work in works]


def summary_identity(node):
    return {
        key: node[key]
        for key in (
            "title",
            "text",
            "line_num",
            "start_index",
            "end_index",
            "anchor",
            "end_anchor",
            "source_node_id",
            "structure",
            "title_origin",
        )
        if key in node
    }


async def summarize_nodes(nodes, model=None):
    """Summarize every node, sharing identical source contexts inside each batch."""
    from .request_budget import navigation_summary

    identities = [summary_identity(node) for node in nodes]
    # stage_result belongs to the host's frozen source/processing namespace
    # (IndexStages in UltraKB). Keep subset/order changes out of this semantic
    # identity; the complete original node text and native range remain in it.
    source = "frozen-index-original-node"
    works = [
        node_work(
            "summary",
            identity,
            [{"id": "s-" + digest(identity["text"])[:20], "text": identity["text"]}],
            model,
            source=source,
            position=(
                None
                if any(
                    identity.get(key) is not None
                    for key in (
                        "line_num",
                        "start_index",
                        "anchor",
                        "source_node_id",
                    )
                )
                else ordinal
            ),
        )
        for ordinal, identity in enumerate(identities)
    ]
    by_id = {work.id: node for work, node in zip(works, nodes)}

    async def fallback(work):
        node = by_id[work.id]
        try:
            return {"summary": await navigation_summary(node["text"], model)}
        except IndexCapacityError:
            return {"summary": node.get("title") or node["text"], "summary_status": "unavailable"}

    values = await run_node_batches(works, "summary", model, fallback)
    for node, value in zip(nodes, values):
        node.pop("summary_status", None)
        node.update(value)
    return nodes


async def verify_titles(items, page_list, start_index=1, model=None, policy=None, boundary=None):
    """Check local contracts first, then batch native or neighboring page contexts."""
    from .page_index import check_title_appearance
    from .physical_sections import finish_title_verification

    source = digest([page[0] for page in page_list])
    works, semantic, results = [], [], {}
    parent = (
        None
        if boundary is None
        else {
            "title": boundary.title,
            "start": boundary.start,
            "end": boundary.end,
            "start_confirmed": boundary.start_confirmed,
            "end_confirmed": boundary.end_confirmed,
        }
    )
    for ordinal, item in enumerate(items):
        page = item.get("physical_index")
        if (
            policy
            or type(page) is not int
            or not start_index <= page < start_index + len(page_list)
            or (boundary and boundary.is_parent_root(item, page_list, start_index))
        ):
            results[ordinal] = await check_title_appearance(
                item, page_list, start_index, model, policy, boundary
            )
            continue
        contexts = []
        for number in range(
            max(start_index, page - 1), min(start_index + len(page_list), page + 2)
        ):
            text = page_list[number - start_index][0]
            if boundary:
                text = boundary.page_text(text, number)
            contexts.append({"id": f"p-{number}", "physical_index": number, "text": text})
        target = {
            k: item[k] for k in ("title", "structure", "physical_index", "anchor") if k in item
        }
        work = node_work(
            "heading",
            target,
            contexts,
            model,
            source=source,
            position={"start_index": start_index},
            parent=parent,
        )
        works.append(work)
        semantic.append((ordinal, item))
    by_id = {work.id: item for work, (_, item) in zip(works, semantic)}

    async def fallback(work):
        return await check_title_appearance(
            by_id[work.id], page_list, start_index, model, policy, boundary
        )

    values = await run_node_batches(works, "heading", model, fallback)
    for (ordinal, item), value in zip(semantic, values):
        results[ordinal] = finish_title_verification(item, value, page_list, start_index, boundary)
    return [results[ordinal] for ordinal in range(len(items))]


async def verify_page_starts(items, page_list, model=None):
    from .page_index import check_title_appearance_in_start

    source = digest([page[0] for page in page_list])
    works = []
    for item in items:
        page = item["physical_index"]
        target = {
            key: item[key]
            for key in (
                "title",
                "physical_index",
                "structure",
                "anchor",
                "source_node_id",
            )
            if key in item
        }
        works.append(
            node_work(
                "start",
                target,
                [{"id": f"p-{page}", "physical_index": page, "text": page_list[page - 1][0]}],
                model,
                source=source,
                position={"physical_index": page},
            )
        )
    by_id = {work.id: item for work, item in zip(works, items)}

    async def fallback(work):
        item = by_id[work.id]
        result = await check_title_appearance_in_start(
            item["title"], page_list[item["physical_index"] - 1][0], model=model
        )
        return {"start_begin": result}

    values = await run_node_batches(works, "start", model, fallback)
    return [value["start_begin"] for value in values]
