"""Capacity-aware generation and bounded conflict resolution for one existing page."""

from __future__ import annotations

import json

from openkb.agent.compile_messages import build_page_messages
from openkb.agent.page_response import PageResponseError
from openkb.agent.page_updates import PageUpdate, UnchangedPage
from openkb.agent.update_proposals import UpdateProposal, merge_proposals
from openkb.compile_checkpoints import digest, document_checkpoints
from openkb.llm_execution import active_executor, check_model_stop


def update_capacity(kb_dir, model, bundle):
    from openkb.config import resolve_effective_config
    from openkb.config_state import active_values
    from openkb.execution_capacity import capacity_policy
    from openkb.model_tokenizer import TokenMeasure

    captured = active_values(kb_dir)
    config = captured["effective"] if captured else resolve_effective_config(kb_dir)[0]
    executor = active_executor()
    if executor:
        model, bundle = executor.bindings.model("compile"), executor.bindings.credentials()
    policy = capacity_policy(
        {**config, "model": model},
        custom_endpoint=bool(bundle and bundle.base_url),
        tokenizer_snapshot=captured.get("tokenizer_asset") if captured else None,
    )
    if policy["unknown_reason"]:
        from openkb.compilation_report import report_compile_issue

        report_compile_issue("page_update_capacity_unknown")
        return lambda messages: len(json.dumps(messages, ensure_ascii=False)) <= 24000
    measure = TokenMeasure(policy)
    options = executor.bindings.generation_options("compile") if executor else {}
    return (
        lambda messages: measure.payload_tokens(
            {
                "messages": messages,
                **options,
                "response_format": {"type": "json_object"},
            }
        )
        <= measure.input_limit
    )


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
) -> dict | UnchangedPage:
    from openkb.agent.compiler import _llm_call_page_async

    fits = fits or update_capacity(windows.kb_dir, model, bundle)
    checkpoints = document_checkpoints(windows.kb_dir, windows.view_id, windows.doc_name)
    page_key = f"page:{page_path.parent.name}/{page_path.name}:{patch.digest}"
    if checkpoints:
        checkpoints.bind_page(f"{page_path.parent.name}/{page_path.name}", patch.digest)
    all_blocks = tuple(patch.blocks)

    def scoped(blocks, *, metadata=True):
        return PageUpdate(
            patch.original,
            known_targets=patch.known_targets,
            originals=patch.originals,
            allowed_types=patch.allowed_types,
            allowed_blocks=frozenset(blocks),
            allow_metadata=metadata,
        )

    def messages(batch, editable, extra=""):
        # Keep source conditions, prior revision attribution and images from the
        # existing context, changing only the actual window list supplied here.
        old = json.dumps(list(originals.windows), ensure_ascii=False)
        new = json.dumps(batch, ensure_ascii=False)
        images = {
            entry["path"] for window in batch for entry in window.get("images", [])
        } | {path for window in batch for path in window.get("assets", {})}
        blocks, include_image = [], False
        for block in originals.message["content"]:
            if block.get("type") == "text":
                text = block["text"]
                if text.startswith("Original image: "):
                    include_image = text.removeprefix("Original image: ") in images
                    if not include_image:
                        continue
                blocks.append({**block, "text": text.replace(old, new)})
            elif block.get("type") != "image_url" or include_image:
                blocks.append(block)
        context = {
            **originals.message,
            "content": blocks,
        }
        return build_page_messages(
            system_message=system_message,
            current_original=current_original,
            original_context=context,
            known_targets_message=known_targets_message,
            task_message={**task_message, "content": task_message["content"] + extra},
            update_message=editable.message(),
        )

    def capacity_error():
        return PageResponseError(
            "page_update_capacity_exceeded",
            "Necessary originals and an indivisible editing target exceed request capacity",
        )

    batch_windows = list(originals.windows)
    if fits(messages(batch_windows, patch)):
        requests = [(batch_windows, patch)]
    else:
        # Partition the old editing view without losing or renumbering targets.
        groups: list[list[str]] = [[]]
        for block in all_blocks:
            if not fits(messages([], scoped([block]))):
                raise capacity_error()
            if groups[-1] and not fits(messages([], scoped([*groups[-1], block]))):
                groups.append([])
            groups[-1].append(block)
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
                raise capacity_error()
            budget //= 2
            selections: dict[tuple[str, str], tuple[dict, dict]] = {}
            for window in originals.windows:
                location = {
                    k: v for k, v in window.get("native_location", {}).items() if k != "offset"
                }
                selections.setdefault((window["path"], digest(location)), (window, location))
            batch_windows = []
            for window, location in selections.values():
                for part in windows.read_selection(window["path"], location, budget=budget):
                    batch_windows.append({**part, "source_role": window["source_role"]})
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

    async def generate(batch, editable, key, extra=""):
        check_model_stop()
        if page_path.read_bytes().decode("utf-8") != patch.original:
            raise PageResponseError("page_update_stale", "Captured page changed")
        request = messages(batch, editable, extra)
        if not fits(request):
            raise capacity_error()
        signature = digest(request)
        value = checkpoints.load(key, signature) if checkpoints else None
        if value is None:
            if checkpoints:
                checkpoints.require_generation(key, signature)
                if ":conflict:" in key:
                    checkpoints.claim_conflict(key, signature)
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
        checkpoints is None
        or checkpoints.load(f"{page_key}:batch:{i}", digest(messages(b, p))) is None
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
            complete_scope=len(requests) == 1,
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
        if (
            checkpoints is None
            or checkpoints.load(conflict_key, digest(messages(selected, editable, extra))) is None
        ) and (executor := active_executor()):
            executor.budget.require("compile", 1 + min(1, executor.policy.compile_repair_calls))
        resolved = await generate(selected, editable, conflict_key, extra)
        if resolved.get("status") != "unchanged":
            result.setdefault("edits", []).extend(resolved["edits"])
            if conflict.metadata:
                result.update({k: resolved[k] for k in ("description", "type") if k in resolved})
    if not result.get("edits") and not any(k in result for k in ("description", "type")):
        result = {
            "status": "unchanged",
            "base_sha256": patch.digest,
            "reason": "The complete necessary contribution is already covered.",
        }
    check_model_stop()
    return patch.apply(json.dumps(result), current=page_path.read_bytes().decode("utf-8"))
