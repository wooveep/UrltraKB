"""Generation rereads the enclosing original code, including in snapshot workers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

from openkb.agent.evidence_pages import _evidence_windows
from openkb.evidence import BlockDraft, Evidence, ParseStore
from openkb.evidence_snapshot import EvidenceSnapshot
from openkb.locks import kb_ingest_lock
from openkb.sources import SourceStore
from tests.test_source_evidence import save_source


def prepared(kb_dir, tmp_path, lines, locations=None):
    file = tmp_path / "code.txt"
    file.write_text("original")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "paragraph-code"},
        [
            BlockDraft(
                line,
                "paragraph",
                {"kind": "docx", "paragraph": i + 1, **(locations or [{}] * len(lines))[i]},
            )
            for i, line in enumerate(lines)
        ],
    )
    ref = asdict(
        Evidence(version.source_id, version.id, parsed.id, parsed.blocks[-1].id, end=len(lines[-1]))
    )
    fact = {
        "id": "f",
        "scope": ref,
        "reference": ref,
        "quote": lines[-1],
        "statement": "Closes location",
    }
    return store.reader(version, parsed), fact


def windows(reader, fact, monkeypatch):
    monkeypatch.setattr("openkb.agent.evidence_pages._generation_fits", lambda *args: True)
    return list(_evidence_windows(fact, reader, {}, None, None))


@pytest.mark.parametrize("detached", [False, True])
def test_enclosing_config_reaches_generation_with_exact_original_references(
    kb_dir, tmp_path, monkeypatch, detached
):
    lines = [
        "location / {",
        '    add_header X "literal } and {";',
        "    root /usr/share/nginx;",
        "    index index.html;",
        "    try_files $uri /xxx.txt;",
        "}",
    ]
    reader, fact = prepared(kb_dir, tmp_path, lines)
    with kb_ingest_lock(kb_dir / ".openkb"):
        if detached:
            reader = EvidenceSnapshot(reader)
            monkeypatch.setattr(SourceStore, "asset", lambda *args: pytest.fail("Live worker read"))
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(windows, reader, fact, monkeypatch).result(timeout=2)
        else:
            result = windows(reader, fact, monkeypatch)
        neighbors = result[0]["neighbors"]
        assert [n["text"] for n in neighbors] == lines[:-1]
        for neighbor in neighbors:
            view = reader.read(Evidence(**neighbor["reference"]), max_chars=4096)
            assert view.text == neighbor["text"]
        assert result[0]["text"] == "}"


@pytest.mark.parametrize("boundary", ["heading", "attachment", "distance", "unbalanced"])
def test_code_context_never_crosses_source_scope_or_bounds(kb_dir, tmp_path, monkeypatch, boundary):
    lines = ["location / {", "    root /usr/share/nginx;", "}"]
    locations = [{"headings": ["Task"]} for _ in lines]
    if boundary == "heading":
        locations[0] = {"headings": ["Other task"]}
    elif boundary == "attachment":
        locations[0]["attachment"] = {
            "part": "object1",
            "name": "Other file",
            "blob": "a" * 64,
            "position": {"kind": "text"},
        }
    elif boundary == "distance":
        lines = [lines[0], *["    option on;"] * 33, "}"]
        locations = None
    else:
        lines[0] = "root /other;"
    reader, fact = prepared(kb_dir, tmp_path, lines, locations)
    assert windows(reader, fact, monkeypatch)[0]["neighbors"] == []
