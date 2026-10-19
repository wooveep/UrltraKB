"""Native acceptance inputs obey the same retained-reader contracts as imports."""

import time

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.sources import source_inventory
from ultrakb.application.views import published_scope
from ultrakb.desktop.verification_data import retain_conversation, retain_document


def test_native_acceptance_sources_and_conversations_are_readable(tmp_path):
    from ultrakb.application.conversations import read_conversation
    from ultrakb.documents import read_document_source

    root = tmp_path / "kb"
    initialize_kb(root)
    retain_document(root, "short.md", "short", "A retained short source.")
    retain_document(
        root,
        "long.pdf",
        "long",
        "Retained pages.",
        pages=[
            {"page": 1, "content": "First retained page."},
            {"page": 2, "content": "Second retained page."},
        ],
    )
    documents = source_inventory(root)
    assert {item["doc_name"] for item in documents} == {"short", "long"}
    assert all(item["status"] == "completed" for item in documents)
    long_source = next(item for item in documents if item["doc_name"] == "long")
    assert "Second retained page" in read_document_source(root, long_source["hash"])["content"]
    session, _ = retain_conversation(root, [("Question", "Answer")])
    conversation = read_conversation(root, session)
    assert conversation.turns == (("Question", "Answer"),)
    assert published_scope(root).knowledge.read("sources/short.md") == b"A retained short source."


def test_watch_stops_after_database_replacement_in_the_same_directory(tmp_path):
    from ultrakb.runtime.watch import NativeWatch

    root, replacement = tmp_path / "kb", tmp_path / "replacement"
    initialize_kb(root)
    initialize_kb(replacement)
    watch = NativeWatch(root, object(), debounce=0.02, scan_interval=0.02)
    try:
        deadline = time.monotonic() + 5
        while not watch.view().scans and time.monotonic() < deadline:
            time.sleep(0.02)
        assert watch.view().scans
        (root / ".ultrakb").rename(root / ".retired-state")
        (replacement / ".ultrakb").rename(root / ".ultrakb")
        assert watch.join(5)
        assert watch.view().state == "failed"
        assert watch.view().submitted == 0
    finally:
        watch.stop()
        assert watch.join(5)
