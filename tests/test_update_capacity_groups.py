"""Capacity admission must include every old-page group and native selection."""

import json

import pytest
from test_vendor_sdk import response

pytest_plugins = ("test_vendor_sdk",)


def _request_scope(messages):
    windows = json.loads(messages[1]["content"][0]["text"])
    editable = next(
        message["content"]
        for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith("Temporary editable page blocks:\n")
    )
    page, _ = json.JSONDecoder().raw_decode(editable.split("\n", 1)[1])
    return windows, page["blocks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("irreducible", [True, False])
async def test_later_group_capacity_cannot_skip_necessary_selections(
    kb_dir, model_service, irreducible
):
    from openkb.agent.page_originals import PageOriginalContext
    from openkb.agent.page_response import PageResponseError
    from openkb.agent.page_updates import PageUpdate, UnchangedPage
    from openkb.agent.source_windows import CompileSourceWindows
    from openkb.agent.update_generation import generate_page_update
    from openkb.application.execution import ExecutionContext
    from openkb.locks import kb_ingest_lock

    (kb_dir / ".env").write_text(f"LLM_API_KEY=fixture\nOPENAI_API_BASE={model_service.url}\n")
    (kb_dir / ".openkb/config.yaml").write_text(
        "model: openai/gpt-4o-mini\nlanguage: en\nmodel_policy:\n"
        "  retries: 0\n  max_calls: 20\n  compile_repair_calls: 0\n"
    )
    wiki = kb_dir / "wiki"
    (wiki / "sources/manual.md").write_text("A" * 600 + "\n\n" + "B" * 600)
    page = wiki / "concepts/rules.md"
    page.write_text("# Rules\n\nEARLY_EDITABLE\n\nLATE_EDITABLE\n")
    before = page.read_bytes()
    patch = PageUpdate(page.read_text())
    reader = CompileSourceWindows(wiki, "manual", kb_dir=kb_dir)
    native = [
        {**window, "source_role": "current"}
        for selection in ("0:600", "602:1202")
        for window in reader.read_selection("sources/manual.md", {"chars": selection})
    ]
    originals = PageOriginalContext(
        message={
            "role": "user",
            "content": [{"type": "text", "text": json.dumps(native, ensure_ascii=False)}],
        },
        has_original=True,
        has_current_original=True,
        windows=tuple(native),
    )

    def fits(messages):
        windows, blocks = _request_scope(messages)
        later = any("LATE_EDITABLE" in block["text"] for block in blocks)
        characters = sum(len(window["content"]) for window in windows)
        return len(blocks) == 1 and (
            not windows or not later or (not irreducible and characters <= 400)
        )

    for _ in range(20):
        model_service.replies.append(
            (
                200,
                response(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "status": "unchanged",
                                "base_sha256": patch.digest,
                                "reason": "The supplied rules require no changes.",
                            }
                        ),
                    }
                ),
            )
        )
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".openkb"), context.begin(kb_dir):
        pending = generate_page_update(
            patch=patch,
            originals=originals,
            windows=reader,
            page_path=page,
            model="ignored",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "Rules"},
            task_message={"role": "user", "content": "Update both necessary native selections."},
            fits=fits,
        )
        if irreducible:
            with pytest.raises(PageResponseError, match="page_update_capacity_exceeded"):
                await pending
        else:
            assert isinstance(await pending, UnchangedPage)
    assert page.read_bytes() == before
    assert context.executor.calls == len(model_service.requests)
    if irreducible:
        assert not model_service.requests
    else:
        sent = [_request_scope(request["messages"]) for request in model_service.requests]
        assert all(fits(request["messages"]) for request in model_service.requests)
        for block in patch.blocks:
            covered = [
                window
                for windows, blocks in sent
                if any(item["id"] == block for item in blocks)
                for window in windows
            ]
            assert "".join(window["content"] for window in covered) == "A" * 600 + "B" * 600
        order = [blocks[0]["id"] for _, blocks in sent]
        assert order == sorted(order, key=lambda block: list(patch.blocks).index(block))
