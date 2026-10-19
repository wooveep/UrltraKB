"""Real compiler updates retain old figure bytes and copy only needed attachments."""

import hashlib
import json

import pytest
from figure_fixtures import png_bytes, record_markdown
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.pages import read_page
from ultrakb.application.recompilation import recompile_document
from ultrakb.application.views import storage_scope
from ultrakb.locks import PrivateMaterialization, atomic_write_json
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.knowledge_store import KnowledgeStore

pytestmark = pytest.mark.integration


def retained_picture(root, identity, directory, value, color):
    picture = directory / "sources/images/figure.png"
    picture.parent.mkdir(parents=True)
    picture.write_bytes(png_bytes(color))
    raw = record_markdown(
        directory, "sources/fixture.md", f"The value is {value}.\n\n![Figure](images/figure.png)\n"
    )
    raw["assets"] = {"sources/images/figure.png": hashlib.sha256(picture.read_bytes()).hexdigest()}
    unused = directory / "sources/images/unreferenced.png"
    unused.write_bytes(png_bytes("green") * 10000)
    native = directory / "sources/fixture.content.json"
    atomic_write_json(native, raw)
    body = directory / "sources/fixture.md"
    catalog = CatalogStore(root, identity)
    source = catalog.admit(body, source_key="fixture", sidecars={"images/figure.png": picture})
    processing = catalog.retain_processing(
        source.revision_id,
        {
            "fixture.md": body,
            "fixture.content.json": native,
            "sources/images/figure.png": picture,
            "sources/images/unreferenced.png": unused,
        },
        body_path="fixture.md",
        source_path="fixture.content.json",
        source_kind="text",
        fingerprint="native-figures-v1",
        operation_id=f"native-{value}",
        decision=classify_markdown_tokens(20),
    )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="fixture")
    body.unlink()
    native.unlink()
    picture.unlink()
    unused.unlink()
    return unit


@pytest.mark.asyncio
@pytest.mark.parametrize("factual_basis", [True, False], ids=["old-original", "historical-gallery"])
async def test_shared_update_retains_each_picture_version_and_excludes_unrelated_files(
    tmp_path, monkeypatch, factual_basis
):
    from ultrakb.agent.page_figures import split_gallery
    from ultrakb.config import save_config

    root, identity, _ = question_knowledge(tmp_path)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model_policy": {"max_calls": 10, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    old = retained_picture(root, identity, tmp_path / "old", 17, "red")
    summary = json.dumps({"description": "Fixture", "content": "The value is 17."})
    with StreamTransport(
        [{"content": summary}, {"content": json.dumps({"concepts": [], "entities": []})}]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, old.source_id, unit_id=old.unit_id, scope=storage_scope(root, identity)
        )
        assert result.status == "compiled", result.message
    knowledge = KnowledgeStore(root, identity)
    old_page = read_page(root, "summaries/fixture.md", scope=storage_scope(root, identity))
    _, old_figures = split_gallery(old_page.body)
    assert old_figures
    concept = (
        "---\ndescription: Recorded value\nsources: [summaries/fixture.md]\n---\n" + old_page.body
    )
    before = knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(),
            {
                "concepts/value.md": concept.encode(),
                "images/unrelated.png": b"unrelated image" * 10000,
            },
            page_inputs={"concepts/value.md": (old.unit_revision_id,) if factual_basis else ()},
        )
    )
    current = retained_picture(root, identity, tmp_path / "new", 23, "blue")
    copied, copy = [], PrivateMaterialization.copy

    def copied_input(self, source, relative, **options):
        copied.append(relative)
        return copy(self, source, relative, **options)

    monkeypatch.setattr(PrivateMaterialization, "copy", copied_input)
    plan = json.dumps(
        {
            "concepts": {
                "create": [],
                "related": [],
                "update": [{"name": "value", "title": "Value"}],
            },
            "entities": [],
        }
    )

    def response(request, number):
        update = next(
            (
                m["content"]
                for m in request["messages"]
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Temporary editable page blocks:\n")
            ),
            None,
        )
        if update:
            blocks, _ = json.JSONDecoder().raw_decode(update.split("\n", 1)[1])
            block = next(b["id"] for b in blocks["blocks"] if "The value is 17." in b["text"])
            figure_input = next(
                m["content"]
                for m in request["messages"]
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Figure source ranges:\n")
            )
            figures = json.loads(figure_input.split("\n", 1)[1])
            selected = [
                r["id"]
                for r in figures["ranges"]
                if figures["sources"][r["source"]].get("processing_revision_id")
                == current.unit_revision_id
            ]
            return {
                "content": json.dumps(
                    {
                        "status": "updated",
                        "base_sha256": blocks["base_sha256"],
                        "edits": [
                            {
                                "operation": "replace",
                                "block_id": block,
                                "content": "The value is 23.\n",
                            }
                        ],
                        "source_ranges": selected,
                    }
                )
            }
        return {
            "content": plan
            if number == 1
            else json.dumps({"description": "Fixture", "content": "The value is 23."})
        }

    with StreamTransport(response) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, current.source_id, unit_id=current.unit_id, scope=storage_scope(root, identity)
        )
        assert result.status == "compiled", result.message
    assert "images/unrelated.png" not in copied
    assert not any(path.endswith("unreferenced.png") for path in copied)
    assert not any(path.endswith((".sqlite", ".db")) for path in copied)
    page = read_page(root, "concepts/value.md", scope=storage_scope(root, identity))
    assert "The value is 23." in page.body
    _, figures = split_gallery(page.body)
    retained = {entry["figure"]["asset_digest"] for entry in figures}
    assert hashlib.sha256(png_bytes("red")).hexdigest() in retained
    assert hashlib.sha256(png_bytes("blue")).hexdigest() in retained
    for entry in figures:
        figure = entry["figure"]
        assert (
            hashlib.sha256(knowledge.bind().read(figure["asset_path"])).hexdigest()
            == figure["asset_digest"]
        )
    assert knowledge.bind(before.publication_id).read("concepts/value.md") == concept.encode()
