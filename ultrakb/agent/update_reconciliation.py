"""Reconcile immutable-page proposals by target and source, without a global prompt."""

import json
from collections import deque
from dataclasses import replace

from ultrakb.compile_checkpoints import digest
from ultrakb.llm_execution import check_model_stop


def _draft(response, blocks, metadata):
    """Project drafts, not evidence or page-wide range claims, onto one editing target."""
    return {
        "edits": [e for e in response.get("edits", []) if e["block_id"] in blocks],
        **(
            {key: response[key] for key in ("description", "type") if key in response}
            if metadata
            else {}
        ),
    }


def _covers(original, windows):
    """Compare actual source spans, not the presence of a path or a generated claim."""

    def spans(window):
        if window.get("page_spans"):
            return [(part["page"], part["start"], part["end"]) for part in window["page_spans"]]
        return [(None, start, end) for start, end in window.get("source_spans", [])]

    supplied = sorted(
        (
            part
            for window in windows
            if (window["path"], window["sha256"]) == (original["path"], original["sha256"])
            for part in spans(window)
        ),
        key=lambda part: (str(part[0]), part[1]),
    )
    needed = spans(original)
    if not needed:
        return not original["content"].strip()
    for page, start, end in needed:
        cursor = start
        for actual_page, left, right in supplied:
            if actual_page == page and left <= cursor:
                cursor = max(cursor, right)
        if cursor < end:
            return False
    return True


async def reconcile_update(
    *,
    patch,
    conflict,
    requests,
    proposals,
    originals,
    windows,
    scoped,
    messages,
    fits,
    generate,
    key,
    include_current=False,
    checkpoints=None,
):
    """Carry a draft through every necessary source; publish only the complete result.

    Body targets and global metadata have independent drafts. Source ranges stay
    local until every step has been accepted. A growing draft causes finer source
    partitions; only an indivisible target plus minimum evidence may stop progress.
    Stable keys retain each accepted step across interruption and retry.
    """
    from ultrakb.agent.compiler import TruncatedResponseError
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.source_fragments import (
        fragment_layouts,
        selection_windows,
        split_fragment_batch,
    )
    from ultrakb.agent.update_generation import prepare_update_requests
    from ultrakb.agent.update_proposals import UpdateProposal, merge_proposals
    from ultrakb.model_tokenizer import ContextCapacityError

    frozen_windows = {digest(w): w for w in originals.windows}
    frozen_windows.update({digest(w): w for batch, _ in requests for w in batch})
    layouts = fragment_layouts(selection_windows(windows, frozen_windows.values()))

    by_order = {p.order: p.response for p in proposals}
    body_proposals = []
    for order in conflict.batches:
        response = by_order[order]
        edits = _draft(response, conflict.blocks, False)["edits"]
        body_proposals.append(
            UpdateProposal(
                order,
                {
                    "status": "updated",
                    "base_sha256": patch.digest,
                    "edits": edits,
                    **(
                        {"source_ranges": response["source_ranges"]}
                        if patch.source_ranges is not None
                        else {}
                    ),
                }
                if edits
                else {
                    "status": "unchanged",
                    "base_sha256": patch.digest,
                    "reason": "This proposal has no body edits for this target.",
                },
            )
        )
    body = merge_proposals(patch, body_proposals)
    # Page-wide metadata uncertainty does not invalidate independent accepted
    # body edits. Keep their bytes/ranges; only genuine body conflicts need work.
    result = {
        "status": "updated",
        "base_sha256": patch.digest,
        "edits": list(body.response.get("edits", [])),
    }
    ranges = set(body.response.get("source_ranges", []))
    work = [(order, requests[order][0]) for order in conflict.batches]
    if include_current:
        represented = [w for _, batch in work for w in batch]
        missing = [
            w
            for w in originals.windows
            if w["path"] == windows.original_path and not _covers(w, represented)
        ]
        if missing:
            # A short request carried this source in a separate full-original
            # message. Removing that message must not remove its evidence.
            work.insert(0, ("current", missing))
    disputed_blocks = {block for part in body.conflicts for block in part.blocks}
    targets = [((block,), False) for block in conflict.blocks if block in disputed_blocks]
    if conflict.metadata:
        targets.append(((), True))
    for blocks, metadata in targets:
        state = {"edits": []}
        target = scoped(blocks, metadata=metadata)
        for order, source_batch in work:
            check_model_stop()
            candidate = _draft(by_order.get(order, {}), blocks, metadata)
            pending = deque([source_batch])
            while pending:
                batch = pending.popleft()
                if batch and all(
                    w.get("content")
                    and not w["content"].strip()
                    and not any(w.get(field) for field in ("units", "cells", "images", "figures"))
                    for w in batch
                ):
                    # The frozen gap was read. Plain separators add no source
                    # contribution and have no figure range for a model to select.
                    continue
                extra = "\nBounded update reconciliation:\n" + json.dumps(
                    {
                        "accumulated_draft": state,
                        "source_proposal": candidate,
                        "metadata_target": metadata,
                        "instruction": "Drafts are not evidence. Integrate this source into the "
                        "accumulated draft using only the supplied originals. "
                        + (
                            "Return complete metadata for this target with edits=[]; "
                            "body edits and invented block IDs are forbidden. Preserve earlier "
                            if metadata
                            else "Return COMPLETE edits for this target against the captured "
                            "base, preserving earlier "
                        )
                        + "source-specific conditions unless these originals justify "
                        "changing them. "
                        "For metadata, consider all contributions read so far. "
                        "Do not generalize a local condition. Return unchanged to retain the "
                        "accumulated draft. Return only this batch's source_ranges; prior ranges "
                        "are retained locally. Nothing is published until every source is read.",
                    },
                    ensure_ascii=False,
                    sort_keys=getattr(checkpoints, "canonical_drafts", False),
                )
                partitions = prepare_update_requests(
                    target,
                    replace(originals, windows=tuple(batch)),
                    windows,
                    messages=lambda selected, editable: messages(selected, editable, extra),
                    fits=fits,
                    scoped=lambda selected: scoped(selected, metadata=metadata),
                    layouts=layouts,
                )
                selected, editable = partitions[0]
                pending.extendleft(reversed([b for b, _ in partitions[1:]]))
                step_key = key + ":" + digest([blocks, metadata, order, selected, state])
                split_signature = digest([selected, state, candidate])
                split_key = step_key + ":split"
                split = checkpoints.load(split_key, split_signature) if checkpoints else None
                try:
                    if split:
                        raise ContextCapacityError("Resume the retained source subdivision")
                    response = await generate(selected, editable, step_key, extra)
                except (ContextCapacityError, TruncatedResponseError, PageResponseError) as error:
                    if isinstance(error, PageResponseError) and error.code not in {
                        "page_update_capacity_exceeded",
                        "page_truncated_response",
                    }:
                        raise
                    children = split_fragment_batch(
                        selection_windows(windows, selected), windows, layouts, prefix="page_update"
                    )
                    if checkpoints and not split:
                        checkpoints.save(split_key, split_signature, {"split": True})
                    pending.extendleft(reversed(children))
                    continue
                if response.get("status") != "unchanged":
                    state = _draft(response, blocks, metadata)
                    ranges.update(response.get("source_ranges", []))
        result["edits"].extend(state["edits"])
        result.update({k: state[k] for k in ("description", "type") if k in state})
    if patch.source_ranges is not None:
        result["source_ranges"] = sorted(ranges)
    if not result["edits"] and not any(k in result for k in ("description", "type")):
        return {
            "status": "unchanged",
            "base_sha256": patch.digest,
            "reason": "Every necessary source confirms no change to the captured target.",
        }
    return result
