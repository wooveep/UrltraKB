"""Budget-aware text partitions and navigation reduction; transport stays injected."""

import json

from ..errors import IndexCapacityError, IndexOutputLimitError
from ..llm import current_client


def fits(prompt, stage, history=None):
    client = current_client()
    if client is None or not hasattr(client, "fits"):
        return True
    return client.fits([*(history or []), {"role": "user", "content": prompt}], stage=stage)


def bisect_text(text):
    if len(text) < 2:
        raise IndexCapacityError("The minimal complete source request cannot fit")
    middle = len(text) // 2
    boundary = text.rfind("\n", max(1, middle // 2), middle + 1)
    offset = boundary + 1 if boundary >= 0 else middle
    first, second = text[:offset], text[offset:]
    # A page may span multiple requests, but it keeps its native physical tag.
    import re
    tags = list(re.finditer(r"<physical_index_\d+>", first))
    if tags and len(tags) % 2:
        tag = tags[-1].group()
        first += "\n" + tag
        second = tag + "\n" + second
    return first, second


def partition_call(text, run, combine):
    try:
        return run(text)
    except IndexCapacityError:
        first, second = bisect_text(text)
        # Source length must decrease even with repeated physical-page tags.
        if max(len(first), len(second)) >= len(text):
            raise IndexCapacityError("Minimal tagged source cannot fit the complete request")
        return combine(partition_call(first, run, combine), partition_call(second, run, combine))


async def partition_async(text, run, combine):
    try:
        return await run(text)
    except (IndexCapacityError, IndexOutputLimitError):
        first, second = bisect_text(text)
        if max(len(first), len(second)) >= len(text):
            raise IndexCapacityError("Minimal tagged source cannot fit the complete request")
        left = await partition_async(first, run, combine)
        right = await partition_async(second, run, combine)
        return combine(left, right)


def merge_entries(first, second):
    result = list(first)
    for item in second:
        key = (item.get("structure"), item.get("title"))
        previous = next((n for n in result if (
            key[0] is not None and (n.get("structure"), n.get("title")) == key
        ) or n == item), None)
        if previous is None:
            result.append(item)
        elif previous.get("physical_index") is None and item.get("physical_index") is not None:
            previous.update(item)
    return result


def navigation_context(entries):
    if not entries:
        return entries
    last = str(entries[-1].get("structure") or "")
    return [item for item in entries if item is entries[-1] or (
        item.get("structure") and last.startswith(str(item["structure"]) + ".")
    )]


def bounded_targets(targets, text, run):
    try:
        return run(targets, text)
    except IndexCapacityError:
        if isinstance(targets, list) and len(targets) > 1:
            middle = len(targets) // 2
            return merge_entries(
                bounded_targets(targets[:middle], text, run),
                bounded_targets(targets[middle:], text, run),
            )
        return partition_call(text, lambda part: run(targets, part), merge_entries)


def text_parts(text, render, stage):
    pending, result = [text], []
    while pending:
        current = pending.pop()
        if fits(render(current), stage):
            result.append(current)
        else:
            first, second = bisect_text(current)
            if max(len(first), len(second)) >= len(current):
                raise IndexCapacityError("Minimal tagged source cannot fit the complete request")
            pending.extend((second, first))
    return result


def description_prompt(structure):
    return (
        "You are an expert in generating descriptions for a document.\n"
        "Generate one concise sentence distinguishing this document from others. "
        "The supplied navigation is derived information, not original evidence.\n"
        f"Document Structure: {json.dumps(structure, ensure_ascii=False)}\n"
        "Directly return the description, do not include any other text."
    )


def describe(structure, model):
    from .utils import llm_completion

    items = []
    def collect(nodes):
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if isinstance(node, dict):
                items.append({k: node[k] for k in ("title", "summary", "prefix_summary") if k in node})
                collect(node.get("nodes", []))
    collect(structure)
    while True:
        groups, group = [], []
        for position, item in enumerate(items):
            if fits(description_prompt([*group, item]), "description"):
                group.append(item)
            else:
                if group:
                    groups.append(group)
                    group = []
                if not fits(description_prompt([item]), "description"):
                    # A single large navigation summary is reduced in complete
                    # text partitions. Original evidence stays in the index.
                    parts = text_parts(json.dumps(item, ensure_ascii=False), description_prompt, "description")
                    replacements = [
                        {"summary": llm_completion(model, description_prompt(p), usage_stage="description")}
                        for p in parts
                    ]
                    if len(json.dumps(replacements)) >= len(json.dumps(item)):
                        raise IndexCapacityError("A navigation item cannot be reduced within capacity")
                    items = replacements + items[position + 1:]
                    if groups:
                        items = [entry for g in groups for entry in g] + items
                    break
                group = [item]
        else:
            if group or not items:
                groups.append(group)
            descriptions = []
            for entries in groups:
                text, reason = llm_completion(
                    model, description_prompt(entries), return_finish_reason=True,
                    usage_stage="description",
                )
                if reason != "finished" or not text.strip():
                    raise IndexOutputLimitError("Document description was not completed")
                descriptions.append({"summary": text})
            if len(descriptions) == 1:
                return descriptions[0]["summary"]
            if len(descriptions) >= len(items):
                raise IndexCapacityError("Navigation reduction cannot make progress within capacity")
            items = descriptions


async def navigation_summary(text, model):
    from .utils import llm_acompletion

    def render(part):
        return (
            "Generate a concise navigation summary of this document part. "
            "Retain its main topics. Return only the summary.\nPartial Document Text: " + part
        )

    if fits(render(text), "summary"):
        try:
            return await llm_acompletion(model, render(text), usage_stage="summary")
        except IndexOutputLimitError:
            pass
    first, second = bisect_text(text)
    if max(len(first), len(second)) >= len(text):
        raise IndexCapacityError("Minimal navigation summary cannot be divided further")
    values = [await navigation_summary(part, model) for part in (first, second)]
    combined = "\n".join(values)
    if len(combined) >= len(text):
        raise IndexCapacityError("Navigation summary reduction cannot make progress")
    return await navigation_summary(combined, model)
