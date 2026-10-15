"""Document windows retain the frozen original, including whitespace between facts."""

import asyncio
import hashlib
import json

import pytest

from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.locks import atomic_write_text


@pytest.fixture(autouse=True)
def no_model_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Source-window conservation must be verified without a model")

    monkeypatch.setattr("litellm.completion", forbidden)
    monkeypatch.setattr("litellm.acompletion", forbidden)


def text_reader(tmp_path, text):
    wiki = tmp_path / "wiki"
    path = wiki / "sources/manual.md"
    atomic_write_text(path, text)
    return CompileSourceWindows(wiki, "manual"), path


def test_document_windows_preserve_internal_whitespace_between_original_facts(tmp_path):
    original = "A\n\n      \n\nB"
    reader, source = text_reader(tmp_path, original)
    before = source.read_bytes()

    windows = list(reader.iter_document_windows(fits=lambda window: len(window["content"]) <= 3))

    assert all(0 < len(window["content"]) <= 3 for window in windows)
    assert "".join(window["content"] for window in windows) == original
    cursor = 0
    for window in windows:
        assert window["selection_offset"] == cursor
        assert window["content"] == original[cursor : cursor + len(window["content"])]
        assert window["source_spans"] == [[cursor, cursor + len(window["content"])]]
        assert window["sha256"] == hashlib.sha256(before).hexdigest()
        cursor += len(window["content"])
        assert window.get("continuation") == (cursor if cursor < len(original) else None)
    assert cursor == len(original)
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    ("original", "limit"),
    [
        ("\t\r\nA😀\r\n\r\n \u2003\t\r\n中e\u0301\u2028B\r\n \t", 4),
        ("A\n\n      \n", 3),
    ],
    ids=["unicode-and-crlf", "whitespace-tail"],
)
def test_document_windows_keep_unicode_coordinates_and_whitespace_tails(tmp_path, original, limit):
    reader, source = text_reader(tmp_path, original)
    before = source.read_bytes()

    windows = list(
        reader.iter_document_windows(fits=lambda window: len(window["content"]) <= limit)
    )

    assert "".join(window["content"] for window in windows) == original
    cursor = 0
    for window in windows:
        end = cursor + len(window["content"])
        assert 0 < end - cursor <= limit
        assert window["selection_offset"] == cursor
        assert window["source_spans"] == [[cursor, end]]
        assert window["content"] == original[cursor:end]
        assert window.get("continuation") == (end if end < len(original) else None)
        assert window["sha256"] == hashlib.sha256(before).hexdigest()
        assert all(context["primary"] is False for context in window["supplementary_context"])
        cursor = end
    assert cursor == len(original)
    assert not windows[-1]["content"].strip()
    assert source.read_bytes() == before == original.encode("utf-8")


@pytest.mark.parametrize("middle", [" \r\n\t", ""], ids=["whitespace-page", "empty-page"])
def test_native_page_windows_preserve_body_characters_and_physical_coordinates(tmp_path, middle):
    bodies = {1: "A😀\r\n   \t\r\n中", 2: middle, 3: "B\u2028e\u0301\r\n"}
    raw = [{"page": page, "unit_kind": "page", "content": body} for page, body in bodies.items()]
    wiki = tmp_path / "wiki"
    source = wiki / "sources/manual.json"
    atomic_write_text(source, json.dumps(raw, ensure_ascii=False))
    before = source.read_bytes()
    expected = (
        "[Physical page 1]\n\nA😀\r\n   \t\r\n中\n\n---\n\n[Physical page 2]\n\n"
        + (middle or "[No extracted text or images]")
        + "\n\n---\n\n[Physical page 3]\n\nB\u2028e\u0301\r\n"
    )
    body_offsets = {
        1: 19,
        2: expected.index("[Physical page 2]\n\n") + 19,
        3: expected.index("B\u2028e\u0301\r\n"),
    }
    reader = CompileSourceWindows(wiki, "manual")

    windows = list(reader.iter_document_windows(fits=lambda window: len(window["content"]) <= 4))

    assert "".join(window["content"] for window in windows) == expected
    recovered = {page: "" for page in bodies}
    cursor = 0
    for window in windows:
        end = cursor + len(window["content"])
        assert window["selection_offset"] == cursor
        assert window["content"] == expected[cursor:end]
        assert 0 < end - cursor <= 4
        assert window.get("continuation") == (end if end < len(expected) else None)
        assert window["sha256"] == hashlib.sha256(before).hexdigest()
        spans = []
        for page, body in bodies.items():
            start = body_offsets[page]
            left, right = max(cursor, start), min(end, start + len(body))
            if left < right:
                spans.append({"page": page, "start": left - start, "end": right - start})
                excerpt = window["content"][left - cursor : right - cursor]
                assert excerpt == body[left - start : right - start]
                recovered[page] += excerpt
            elif not body and cursor <= start < end:
                # The placeholder retains the physical page's empty locator;
                # it cannot claim any placeholder characters as original body.
                spans.append({"page": page, "start": 0, "end": 0})
        assert window.get("page_spans", []) == spans
        cursor = end
    assert cursor == len(expected)
    assert recovered == bodies
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    ("suffix", "raw"),
    [
        ("md", ""),
        ("md", " \r\n\t\u00a0\u2003\u2028"),
        ("json", [{"page": 1, "unit_kind": "page", "content": ""}]),
        (
            "json",
            [
                {"page": 1, "unit_kind": "page", "content": " \r\n\t"},
                {"page": 2, "unit_kind": "page", "content": "\u2003\u2028"},
            ],
        ),
    ],
    ids=["empty-text", "whitespace-text", "empty-page", "whitespace-pages"],
)
def test_blank_originals_have_no_summary_generation_or_claimed_knowledge(tmp_path, suffix, raw):
    from ultrakb.agent.document_summary import build_document_summary

    wiki = tmp_path / "wiki"
    source = wiki / f"sources/manual.{suffix}"
    atomic_write_text(source, json.dumps(raw, ensure_ascii=False) if suffix == "json" else raw)
    before = source.read_bytes()
    reader = CompileSourceWindows(wiki, "manual")

    async def forbidden_generation(*args):
        raise AssertionError("Blank retained originals cannot generate source knowledge")

    assert list(reader.iter_document_windows(fits=lambda _: True)) == []
    draft = asyncio.run(
        build_document_summary(
            reader,
            system_message={"role": "system", "content": "Summarize the originals."},
            generate=forbidden_generation,
            fits=lambda _: True,
        )
    )
    assert draft.description == "Retained original has no nonempty text."
    assert draft.markdown == "# manual\n\nThe retained original has no nonempty text.\n"
    assert draft.ranges == ()
    assert source.read_bytes() == before


def test_summary_preparation_admits_only_nonblank_original_batches(tmp_path):
    from ultrakb.agent.document_summary import build_document_summary
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    reader, source = text_reader(tmp_path, "\n \t\nA\n\n    \nB")
    before = source.read_bytes()
    prepared = []
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=3, retries=0),
    )

    async def stop_before_provider(messages, stage, originals, sections):
        # Capture the public generation boundary; no model response is supplied.
        prepared.append((messages, originals, sections))
        raise ConnectionError("Stopped before provider transport")

    def fits(messages):
        window = json.loads(messages[1]["content"].split("\n", 1)[1])
        return len(window["content"]) <= 3

    # Two meaningful batches and one planning call fit. Whitespace batches must
    # retain read coverage without reserving or sending generation requests.
    with executor.activate(), pytest.raises(ConnectionError, match="before provider"):
        asyncio.run(
            build_document_summary(
                reader,
                system_message={"role": "system", "content": "Summarize the originals."},
                generate=stop_before_provider,
                fits=fits,
            )
        )
    assert executor.calls == 0
    assert len(prepared) == 1
    _, originals, sections = prepared[0]
    assert sections == ("section-1",)
    assert "A" in originals[0]["content"]
    assert source.read_bytes() == before
