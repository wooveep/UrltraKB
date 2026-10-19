"""Capacity-aware generation and bounded conflict resolution for one existing page."""

from __future__ import annotations

import json
from dataclasses import replace

from ultrakb.agent.compile_contract import request_version, require_frozen_request
from ultrakb.agent.compile_messages import build_page_messages
from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.page_originals import bounded_short_originals
from ultrakb.agent.page_response import PageResponseError, update_repair_messages
from ultrakb.agent.page_updates import PageUpdate, UnchangedPage
from ultrakb.agent.retained_delivery import reuse_saved_response
from ultrakb.agent.update_proposals import UpdateProposal, merge_proposals
from ultrakb.compile_checkpoints import digest, document_checkpoints
from ultrakb.llm_execution import active_executor, check_model_stop


def update_capacity(kb_dir, model, bundle):
    from ultrakb.agent.compile_capacity import compile_capacity

    sizing = compile_capacity(kb_dir, model, bundle)
    if sizing.measure is None:
        from ultrakb.compilation_report import report_compile_issue

        report_compile_issue("page_update_capacity_unknown")
    return sizing


def capacity_error(detail="", *, field_errors=()):
    return PageResponseError(
        "page_update_capacity_exceeded",
        "Necessary originals and an indivisible editing target exceed request capacity"
        + (f": {detail}; original retained" if detail else ""),
        field_errors=field_errors,
    )


def prepare_update_requests(
    patch, originals, windows, *, messages, fits, scoped, include_repairs=True, layouts=None
):
    if include_repairs:
        initial_fits = fits
        corrections = update_repair_messages()

        def fits(request):
            return initial_fits(request) and all(
                initial_fits([*request, correction]) for correction in corrections
            )

    """Admit every frozen source selection and editing group before generation."""
    all_blocks = tuple(
        block
        for block in patch.blocks
        if patch.allowed_blocks is None or block in patch.allowed_blocks
    )

    batch_windows = list(originals.windows)
    if fits(messages(batch_windows, patch)):
        requests = [(batch_windows, patch)]
    else:
        # Partition the old editing view without losing or renumbering targets.
        groups: list[list[str]] = [[]]
        for block in all_blocks:
            if not fits(messages([], scoped([block]))):
                raise capacity_error(
                    f"captured base {patch.digest}, block {block}, "
                    f"body characters {patch.blocks[block]}",
                    field_errors=(
                        {
                            "base_sha256": patch.digest,
                            "block_id": block,
                            "body_span": list(patch.blocks[block]),
                        },
                    ),
                )
            if groups[-1] and not fits(messages([], scoped([*groups[-1], block]))):
                groups.append([])
            groups[-1].append(block)
        from ultrakb.agent.source_fragments import (
            fragment_layouts,
            fragment_selections,
            iter_fragments,
            require_splittable,
            selection_windows,
        )

        selections = selection_windows(windows, originals.windows)
        layouts = fragment_layouts(selections) if layouts is None else layouts
        budget = 24000
        while any(
            not fits(messages([window], scoped(group)))
            for group in groups
            for window in batch_windows
        ):
            # An old-page group that fits alone may leave no space for evidence.
            # Split editable targets first, still visiting every original block.
            if any(len(group) > 1 for group in groups):
                groups = [
                    part
                    for group in groups
                    for part in (group[: max(1, len(group) // 2)], group[max(1, len(group) // 2) :])
                    if part
                ]
                continue
            if budget <= 32:
                raise capacity_error(
                    ", ".join(
                        f"{part['path']} at {part.get('native_location')}" for part in batch_windows
                    ),
                    field_errors=tuple(
                        {
                            "path": part["path"],
                            "native_location": part.get("native_location"),
                            "selection_offset": part.get("selection_offset"),
                        }
                        for part in batch_windows
                    ),
                )
            budget //= 2
            batch_windows = []
            from ultrakb.agent.create_requests import selection_key

            for window, start, stop in fragment_selections(selections):
                layout = layouts[selection_key(window)]
                for part in iter_fragments(
                    windows, window, budget=budget, layout=layout, start=start, stop=stop
                ):
                    batch_windows.append(part)
                    if not fits(messages([part], scoped(groups[0]))):
                        require_splittable(part, layout, prefix="page_update")
                        # This subdivision already fails. Retry the full frozen
                        # selections before measuring every old-page combination.
                        # The outer condition checks every group before admission.
                        break
                else:
                    continue
                break
        requests = []
        for group in groups:
            editable = scoped(group)
            packed: list[dict] = []
            for window in batch_windows:
                if packed and not fits(messages([*packed, window], editable)):
                    requests.append((packed, editable))
                    packed = []
                packed.append(window)
            requests.append((packed, editable))

    return requests


async def generate_page_update(
    *,
    patch,
    originals,
    windows,
    page_path,
    model,
    bundle,
    system_message,
    current_original,
    known_targets_message,
    task_message,
    fits=None,
    link_registry=None,
) -> dict | UnchangedPage:
    from ultrakb.agent.compiler import TruncatedResponseError, _llm_call_page_async

    fits = fits or update_capacity(windows.kb_dir, model, bundle)
    checkpoints = document_checkpoints(windows.kb_dir, windows.view_id, windows.doc_name)
    from ultrakb.agent.link_registry import FrozenLinkRegistry, writing_catalogue
    from ultrakb.agent.link_resolution import request_admitted, retained_link_response

    if link_registry is None and patch.known_targets is not None:
        link_registry = FrozenLinkRegistry.capture(windows.wiki, patch.known_targets)
    known_targets_message, link_registry = writing_catalogue(
        known_targets_message,
        context=json.dumps([task_message, originals.message], ensure_ascii=False),
        registry=link_registry,
    )
    if link_registry:
        link_registry.persist(checkpoints)
    page_key = f"page:{page_path.parent.name}/{page_path.name}:{patch.digest}"
    if checkpoints:
        checkpoints.bind_page(
            f"{page_path.parent.name}/{page_path.name}", patch.digest, page_path=page_path
        )
    all_blocks = tuple(
        block
        for block in patch.blocks
        if patch.allowed_blocks is None or block in patch.allowed_blocks
    )

    def scoped(blocks, *, metadata=True, ranges=None):
        return PageUpdate(
            patch.original,
            known_targets=patch.known_targets,
            allowed_types=patch.allowed_types,
            preserved_fragments=patch.preserved_fragments,
            allowed_blocks=(
                frozenset(blocks)
                if patch.allowed_blocks is None
                else frozenset(blocks) & patch.allowed_blocks
            ),
            allow_metadata=metadata,
            source_ranges=ranges if ranges is not None else patch.source_ranges,
        )

    bounded_original = False

    def evidence_long():
        return current_original is None or bounded_original

    def excerpts(batch):
        result = windows.original_excerpts(
            replace(originals, windows=tuple(batch)), long=evidence_long()
        )
        result.request_version = request_version(checkpoints)
        return result

    def messages(batch, editable, extra=""):
        check_model_stop()
        result = build_page_messages(
            system_message=system_message,
            current_original=None if bounded_original else current_original,
            original_context=originals.for_windows(batch),
            known_targets_message=known_targets_message,
            task_message={**task_message, "content": task_message["content"] + extra},
            update_message=editable.message(),
        )
        if patch.source_ranges is not None:
            result.append(
                windows.page_figures(batch, long=evidence_long()).message(
                    compact=request_version(checkpoints) >= 3,
                    bounded_catalogue=request_version(checkpoints) >= 3,
                )
            )
        return excerpts(batch).messages(result)

    def prepare():
        return prepare_update_requests(
            patch,
            originals,
            windows,
            messages=messages,
            fits=fits,
            scoped=scoped,
            include_repairs=request_version(checkpoints) != 1,
        )

    try:
        requests = prepare()
    except PageResponseError as error:
        if current_original is None or not (
            error.code == "page_update_capacity_exceeded"
            or error.code.startswith("page_update_indivisible_")
        ):
            raise
        # Preserve fitting historical request bytes and accepted editing groups.
        # Only a complete capacity failure activates the bounded source view.
        originals = bounded_short_originals(originals, windows)
        bounded_original = True
        requests = prepare()

    combined_figures = None
    if patch.source_ranges is not None:
        from ultrakb.agent.page_figures import PageFigures

        contexts = [windows.page_figures(batch, long=evidence_long()) for batch, _ in requests]
        combined_figures = PageFigures(
            windows.wiki,
            tuple({r["id"]: r for context in contexts for r in context.ranges}.values()),
            tuple({f.occurrence_id: f for context in contexts for f in context.figures}.values()),
        )
        patch.source_ranges = combined_figures.ids

    def for_batch(batch, editable):
        nonlocal combined_figures

        if patch.source_ranges is not None:
            from ultrakb.agent.page_figures import PageFigures

            context = windows.page_figures(batch, long=evidence_long())
            assert combined_figures is not None
            # Coordination can split a formerly fitting source window further.
            # Keep those actual frozen ranges for final validation and galleries.
            combined_figures = PageFigures(
                windows.wiki,
                tuple({r["id"]: r for r in (*combined_figures.ranges, *context.ranges)}.values()),
                tuple(
                    {
                        f.occurrence_id: f for f in (*combined_figures.figures, *context.figures)
                    }.values()
                ),
            )
            patch.source_ranges = combined_figures.ids
            return scoped(
                all_blocks if editable.allowed_blocks is None else editable.allowed_blocks,
                metadata=editable.allow_metadata,
                ranges=context.ids,
            )
        return editable

    def accepted(key, signature, editable, batch):
        require_frozen_request(checkpoints, key, signature)
        original_excerpts = excerpts(batch)

        def validate(raw):
            if link_registry:
                raw = link_registry.response(raw)
            return validate_page_delivery(
                raw,
                page_update=editable,
                read_current=lambda: page_path.read_bytes().decode("utf-8"),
                proposal_only=True,
                original_excerpts=original_excerpts,
            )

        value = checkpoints.load(key, signature) if checkpoints else None
        if value is not None:
            try:
                validate(json.dumps(value))
                return value
            except PageResponseError as error:
                if error.code == "page_update_stale":
                    raise
        if checkpoints:
            return reuse_saved_response(
                checkpoints,
                key,
                signature,
                protocol="page-json",
                stage=key,
                validate=validate,
                excerpts=original_excerpts,
                metadata={
                    "base_sha256": patch.digest,
                    "blocks": editable.request()["blocks"],
                    "sources": [{k: v for k, v in w.items() if k != "content"} for w in batch],
                },
            )
        return None

    async def generate(batch, editable, key, extra=""):
        check_model_stop()
        editable = for_batch(batch, editable)
        if page_path.read_bytes().decode("utf-8") != patch.original:
            raise PageResponseError("page_update_stale", "Captured page changed")
        request = messages(batch, editable, extra)
        signature = digest(request)
        value = accepted(key, signature, editable, batch)
        if value is None:
            if not fits(request):
                raise capacity_error()
            # Historical grouping keeps its frozen signature. Missing historical
            # results still need room for the current, bounded protocol repair.
            if not all(fits([*request, repair]) for repair in update_repair_messages()):
                raise capacity_error()
            if executor := active_executor():
                executor.budget.require("compile", 1 + min(1, executor.policy.compile_repair_calls))
            if checkpoints:
                checkpoints.require_generation(key, signature)
                if ":conflict:" in key:
                    checkpoints.claim_conflict(key, signature)
                else:
                    checkpoints.claim_generation(key, signature, stage="compile." + key)
            result = await _llm_call_page_async(
                model,
                request,
                key,
                response_format={"type": "json_object"},
                bundle=bundle,
                prompt_version="compiler-update-proposals-v1",
                page_update=editable,
                read_current=lambda: page_path.read_bytes().decode("utf-8"),
                proposal_only=True,
                checkpoints=checkpoints,
                checkpoint_key=key,
                original_excerpts=excerpts(batch),
                link_registry=link_registry,
                request_fits=lambda messages, options: request_admitted(fits, messages, options),
                initial_response=retained_link_response(
                    checkpoints, key, signature, stage=key, registry=link_registry
                ),
            )
            value = (
                {"status": "unchanged", "base_sha256": result.base_sha256, "reason": result.reason}
                if isinstance(result, UnchangedPage)
                else json.loads(result.strip().removeprefix("```json").removesuffix("```"))
            )
            if checkpoints:
                checkpoints.save(
                    key,
                    signature,
                    value,
                    metadata={
                        "base_sha256": patch.digest,
                        "blocks": editable.request()["blocks"],
                        "sources": [{k: v for k, v in w.items() if k != "content"} for w in batch],
                    },
                )
        check_model_stop()
        editable.apply(json.dumps(value), current=page_path.read_bytes().decode("utf-8"))
        return value

    missing = sum(
        accepted(f"{page_key}:batch:{i}", digest(messages(b, p)), for_batch(b, p), b) is None
        for i, (b, p) in enumerate(requests)
    )
    if missing and (executor := active_executor()):
        executor.budget.require(
            "compile", missing + min(missing, executor.policy.compile_repair_calls)
        )
    proposals = [
        UpdateProposal(
            i,
            await generate(batch, editable, f"{page_key}:batch:{i}"),
            # Splitting the old editing view does not narrow original evidence.
            # Identical metadata proposals can cover the page even when its
            # independently editable old blocks needed several requests.
            complete_scope=batch == list(originals.windows),
        )
        for i, (batch, editable) in enumerate(requests)
    ]
    merged = merge_proposals(patch, proposals)
    result = dict(merged.response)
    for index, conflict in enumerate(merged.conflicts):
        selected, seen = [], set()
        for order in conflict.batches:
            for window in requests[order][0]:
                if (identity := digest(window)) not in seen:
                    seen.add(identity)
                    selected.append(window)
        extra = (
            "\nResolve these conflicting proposals once, using the original source conditions. "
            "Proposals and retained edits are drafts, not evidence. Return only changes to the "
            "supplied targets, accounting for all necessary contributions. Page metadata, when "
            "allowed, must cover the whole page.\n"
            + json.dumps(
                {
                    "proposals": [p.response for p in proposals if p.order in conflict.batches],
                    "retained_edits": merged.response.get("edits", []),
                    "metadata_target": conflict.metadata,
                },
                ensure_ascii=False,
            )
        )
        # Metadata needs the whole editing view; body editing remains restricted.
        editable = scoped(conflict.blocks, metadata=conflict.metadata)
        if conflict.metadata:
            extra += "\nFinal page context (draft):\n" + json.dumps(
                patch.request(), ensure_ascii=False
            )
        conflict_key = f"{page_key}:conflict:{index}"
        split_key = conflict_key + ":bounded-v1"
        signature = digest(messages(selected, editable, extra))
        split = checkpoints.load(split_key, signature) if checkpoints else None
        from ultrakb.model_tokenizer import ContextCapacityError

        try:
            if split:
                raise capacity_error()
            resolved = await generate(selected, editable, conflict_key, extra)
        except (PageResponseError, ContextCapacityError, TruncatedResponseError) as error:
            if isinstance(error, PageResponseError) and error.code not in {
                "page_update_capacity_exceeded",
                "page_truncated_response",
            }:
                raise
            if checkpoints and not split:
                checkpoints.save(split_key, signature, {"bounded": True})
            from ultrakb.agent.update_reconciliation import reconcile_update

            if current_original is not None:
                originals = bounded_short_originals(originals, windows)
                bounded_original = True
            resolved = await reconcile_update(
                patch=patch,
                conflict=conflict,
                requests=requests,
                proposals=proposals,
                originals=originals,
                windows=windows,
                scoped=scoped,
                messages=messages,
                fits=fits,
                generate=generate,
                key=conflict_key + ":steps-v1",
                include_current=current_original is not None,
                checkpoints=checkpoints,
            )
        if resolved.get("status") != "unchanged":
            result.setdefault("edits", []).extend(resolved["edits"])
            if patch.source_ranges is not None:
                result["source_ranges"] = sorted(
                    set(result.get("source_ranges", [])) | set(resolved["source_ranges"])
                )
            if conflict.metadata:
                result.update({k: resolved[k] for k in ("description", "type") if k in resolved})
    if not result.get("edits") and not any(k in result for k in ("description", "type")):
        result = {
            "status": "unchanged",
            "base_sha256": patch.digest,
            "reason": "The complete necessary contribution is already covered.",
        }
    check_model_stop()
    applied = patch.apply(json.dumps(result), current=page_path.read_bytes().decode("utf-8"))
    if isinstance(applied, UnchangedPage) or patch.source_ranges is None:
        return applied
    assert combined_figures is not None
    return combined_figures.decorate(
        page_path.relative_to(windows.wiki).as_posix(),
        applied,
        previous=patch.original,
        replaced=any(e["operation"] in {"replace", "delete"} for e in result["edits"]),
    )
