"""Capacity admission must include every old-page group and native selection."""

import json

import pytest


def _request_scope(messages):
    raw = messages[1]["content"][0]["text"]
    if raw.startswith("Original source windows"):
        raw = raw.split("\n", 1)[1]
    windows, _ = json.JSONDecoder().raw_decode(raw)
    editable = next(
        message["content"]
        for message in messages
        if isinstance(message.get("content"), str)
        and message["content"].startswith("Temporary editable page blocks:\n")
    )
    page, _ = json.JSONDecoder().raw_decode(editable.split("\n", 1)[1])
    return windows, page["blocks"]


@pytest.mark.parametrize("irreducible", [True, False])
def test_later_group_capacity_cannot_skip_necessary_selections(kb_dir, irreducible):
    from dataclasses import replace

    from publication_inputs import update_inputs

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.update_generation import prepare_update_requests

    source = "A" * 600 + "\n\n" + "B" * 600
    page, reader, originals, patch, scoped, messages = update_inputs(
        kb_dir,
        "# Rules\n\nEARLY_EDITABLE\n\nLATE_EDITABLE\n",
        source,
    )
    before = page.read_bytes()
    native = [
        {**window, "source_role": "current"}
        for selection in ("0:600", "602:1202")
        for window in reader.read_selection("sources/manual.md", {"chars": selection})
    ]
    originals = replace(originals, windows=tuple(native))

    def fits(request):
        windows, blocks = _request_scope(request)
        later = any("LATE_EDITABLE" in block["text"] for block in blocks)
        characters = sum(len(window["content"]) for window in windows)
        return len(blocks) == 1 and (
            not windows or not later or (not irreducible and characters <= 400)
        )

    if irreducible:
        with pytest.raises(PageResponseError, match="page_update_capacity_exceeded"):
            prepare_update_requests(
                patch, originals, reader, messages=messages, fits=fits, scoped=scoped
            )
    else:
        requests = prepare_update_requests(
            patch,
            originals,
            reader,
            messages=messages,
            fits=fits,
            scoped=scoped,
        )
        sent = [_request_scope(messages(batch, editable)) for batch, editable in requests]
        assert all(fits(messages(batch, editable)) for batch, editable in requests)
        for block in patch.blocks:
            covered = [
                window
                for windows, blocks in sent
                if any(candidate["id"] == block for candidate in blocks)
                for window in windows
            ]
            # Every page target sees the complete required source selections.
            assert "".join(window["content"] for window in covered) == "A" * 600 + "B" * 600
            spans = [span for window in covered for span in window["source_spans"]]
            assert spans[0][0] == 0 and spans[-1][1] == 1202
    assert page.read_bytes() == before


def test_partitioned_update_never_reopens_protected_page_blocks(kb_dir):
    from publication_inputs import update_inputs

    from ultrakb.agent.update_generation import prepare_update_requests

    page, reader, originals, patch, scoped, messages = update_inputs(
        kb_dir,
        "# Rules\n\nEditable rule.\n\nHuman addition.\n",
        "Required winter inspection.\n\nRequired summer interlock.\n",
    )
    patch.allowed_blocks = frozenset({"b2"})

    def fits(request):
        windows, blocks = _request_scope(request)
        return len(blocks) == 1 and sum(len(w["content"]) for w in windows) <= 32

    requests = prepare_update_requests(
        patch,
        originals,
        reader,
        messages=messages,
        fits=fits,
        scoped=scoped,
    )
    assert len(requests) > 1
    assert all(editable.allowed_blocks == frozenset({"b2"}) for _, editable in requests)
    assert page.read_text().endswith("Human addition.\n")
