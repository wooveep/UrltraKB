"""Global title support stays distinct from each part's body evidence."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from openkb.agent.evidence_generation_protocol import generation_payload
from openkb.agent.evidence_title_context import topic_title_context
from openkb.evidence import BlockDraft, Evidence, ParseStore
from openkb.evidence_snapshot import EvidenceSnapshot
from openkb.sources import SourceStore
from tests.test_source_evidence import save_source


def test_title_quotes_are_original_deduplicated_and_scoped_in_snapshot_workers(
    kb_dir, tmp_path, monkeypatch
):
    file = tmp_path / "titles.txt"
    file.write_text("Original")
    version = save_source(kb_dir, file)
    store = ParseStore(kb_dir)
    parsed = store.save(
        version,
        {"parser": "title-context-test"},
        [
            BlockDraft(
                "Quoted action.",
                "paragraph",
                {"kind": "text", "line": i + 1, "headings": [heading]},
            )
            for i, heading in enumerate(["Task A", "Task A", "Task B"])
        ],
    )
    reader = EvidenceSnapshot(store.reader(version, parsed))
    facts = []
    for block in parsed.blocks:
        ref = asdict(Evidence(version.source_id, version.id, parsed.id, block.id, end=block.chars))
        facts.append(
            {"id": block.id, "reference": ref, "scope": ref, "quote": "Untrusted paraphrase"}
        )

    def forbidden(*args):
        raise AssertionError("Snapshot worker accessed live files")

    monkeypatch.setattr(SourceStore, "asset", forbidden)
    with ThreadPoolExecutor(max_workers=1) as pool:
        context = pool.submit(topic_title_context, facts, reader).result(timeout=2)
    assert context["passages"] == [
        {"scope": "t1", "text": "Quoted action."},
        {"scope": "t2", "text": "Quoted action."},
    ]
    assert [s["headings"] for s in context["scopes"]] == [["Task A"], ["Task B"]]
    base = {
        "title": "Tasks",
        "_topic_fact_ids": {f["id"] for f in facts},
        "_title_context": context,
    }
    partial = generation_payload(base, facts[:1], [])
    whole = generation_payload(base, facts, [])
    assert partial["title_context"] == context
    assert "title_context" not in whole
    assert not any(key.startswith("_") for key in partial)
    assert partial["facts"] == facts[:1]


def test_other_parts_original_text_never_enters_body_generation_or_correction():
    import json

    from openkb.agent.evidence_generation_protocol import messages

    context = {"scopes": [], "passages": [{"text": "Unrelated clone instructions."}]}
    base = {
        "stage": "generation",
        "title": "Cloning and login",
        "title_context": context,
        "facts": [],
        "evidence": [],
    }
    for revision in (None, {"title": base["title"], "content": "Login.", "reason": "Fix body"}):
        payload = {**base, "revision": revision}
        wire = json.loads(messages("System", payload)[-1]["content"])
        assert "title_context" not in wire
        assert "Unrelated clone instructions." not in str(wire)
        assert payload["title_context"] == context
    # A title-only repair cannot mutate body content, so it may inspect all title evidence.
    base["revision"] = {
        "title": base["title"],
        "content": "Login.",
        "issues": [{"kind": "title", "candidate": base["title"]}],
    }
    wire = json.loads(messages("System", base)[-1]["content"])
    assert wire["title_context"] == context
    assert "Return only" in wire["output_contract"]


def test_title_window_keeps_complete_contextual_passages_within_budget():
    import json
    from copy import deepcopy

    import litellm

    from openkb.agent.evidence_title_context import _bounded

    context = {
        "scopes": [{"id": "t1", "origin": "enclosing_document", "headings": ["Network"]}],
        "passages": [
            {
                "scope": "t1",
                "text": "Long conditional requirement. " * 300,
                "context": "This restriction applies only to a particular operation.",
            },
            {"scope": "t1", "text": "Restart the network service after the change."},
        ],
    }
    before = deepcopy(context)
    result = _bounded(context, "Restart network", "gpt-4o-mini", 180)
    assert result["passages"] == [context["passages"][1]]
    assert context == before
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert litellm.token_counter(model="gpt-4o-mini", text=text) <= 180
