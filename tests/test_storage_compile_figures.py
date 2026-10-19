"""Real compiler updates retain old figure bytes and copy only needed attachments."""

import hashlib
import io
import json

import pytest
from figure_fixtures import png_bytes, record_markdown
from PIL import Image
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


def retained_picture(root, identity, directory, value, color, *, extension="png", payload=None):
    image = f"sources/images/figure.{extension}"
    picture = directory / image
    picture.parent.mkdir(parents=True)
    picture.write_bytes(payload if payload is not None else png_bytes(color))
    raw = record_markdown(
        directory,
        "sources/fixture.md",
        f"The value is {value}.\n\n![Figure](images/figure.{extension})\n",
    )
    raw["assets"] = {image: hashlib.sha256(picture.read_bytes()).hexdigest()}
    unused = directory / "sources/images/unreferenced.png"
    unused.write_bytes(png_bytes("green") * 10000)
    native = directory / "sources/fixture.content.json"
    atomic_write_json(native, raw)
    body = directory / "sources/fixture.md"
    catalog = CatalogStore(root, identity)
    source = catalog.admit(
        body, source_key="fixture", sidecars={f"images/figure.{extension}": picture}
    )
    processing = catalog.retain_processing(
        source.revision_id,
        {
            "fixture.md": body,
            "fixture.content.json": native,
            image: picture,
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
@pytest.mark.parametrize("extension", ["svg", "tif", "tiff", "ico"])
async def test_recompile_preserves_every_supported_referenced_image_format(tmp_path, extension):
    from ultrakb.agent.page_figures import split_gallery

    root, identity, _ = question_knowledge(tmp_path)
    if extension == "svg":
        payload = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            b'<rect width="10" height="10" fill="red"/></svg>'
        )
    else:
        data = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(data, "ICO" if extension == "ico" else "TIFF")
        payload = data.getvalue()
    unit = retained_picture(
        root, identity, tmp_path / "original", 17, "red", extension=extension, payload=payload
    )
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 17."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, unit.source_id, unit_id=unit.unit_id, scope=storage_scope(root, identity)
        )
    assert result.status == "compiled", result.message
    bound = KnowledgeStore(root, identity).bind()
    _, figures = split_gallery(bound.read("summaries/fixture.md").decode())
    assert len(figures) == 1
    assert bound.read(figures[0]["figure"]["asset_path"]) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("html", [False, True], ids=["markdown", "html"])
@pytest.mark.parametrize("factual_basis", [True, False], ids=["source-page", "manual-page"])
async def test_an_unchanged_inline_picture_keeps_its_old_bytes_when_the_source_advances(
    tmp_path, html, factual_basis
):
    from ultrakb.application.reading_assets import capture_reading_assets

    root, identity, _ = question_knowledge(tmp_path)
    old = retained_picture(root, identity, tmp_path / "old", 17, "red")
    knowledge = KnowledgeStore(root, identity)
    suffix = "?label=&quot;old&quot;#view" if html else "?raw=1#original-view"
    markup = (
        f'<img src="../sources/images/figure.png{suffix}" alt="Original">'
        if html
        else f"![Original](../sources/images/figure.png{suffix})"
    )
    original = (
        "---\ndescription: Original\nsources: [summaries/fixture.md]\n---\n"
        f"The value was 17.\n\n{markup}\n"
    )
    before = knowledge.publish(
        knowledge.prepare_revision(
            {
                "index.md": b"Fixture",
                "concepts/old-value.md": original.encode(),
                "sources/images/figure.png": png_bytes("red"),
            },
            expected=knowledge.bind().publication_id,
            unit_revisions=(old.unit_revision_id,),
            page_inputs={"concepts/old-value.md": (old.unit_revision_id,) if factual_basis else ()},
        )
    )
    current = retained_picture(root, identity, tmp_path / "new", 23, "blue")
    with StreamTransport(
        [
            {"content": json.dumps({"description": "Fixture", "content": "The value is 23."})},
            {"content": json.dumps({"concepts": [], "entities": []})},
        ]
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await recompile_document(
            root, current.source_id, unit_id=current.unit_id, scope=storage_scope(root, identity)
        )
    assert result.status == "compiled", result.message
    scope = storage_scope(root, identity)
    page = read_page(root, "concepts/old-value.md", scope=scope)
    assets = capture_reading_assets(scope, "concepts/old-value.md", page.body)
    assert len(assets.images) == 1
    assert next(iter(assets.images.values())) == png_bytes("red")
    assert "The value was 17." in page.body
    assert suffix in page.body
    assert knowledge.bind().page_input_revisions().get("concepts/old-value.md", ()) == (
        (old.unit_revision_id,) if factual_basis else ()
    )
    assert knowledge.bind(before.publication_id).read("concepts/old-value.md") == original.encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("factual_basis", [True, False], ids=["old-original", "historical-gallery"])
async def test_shared_update_retains_each_picture_version_and_excludes_unrelated_files(
    tmp_path, monkeypatch, factual_basis
):
    from ultrakb.agent.page_figures import split_gallery
    from ultrakb.application.proposals import accept_proposal, read_proposal
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
        # This concept was manually published above. An actual content update
        # requires review even though relocating identical image bytes does not.
        assert result.status == "blocked", result.message
        assert knowledge.bind().publication_id == before.publication_id
        scope = storage_scope(root, identity)
        proposal = read_proposal(root, result.proposal_id, scope=scope)
        assert proposal.conflicts == ("concepts/value.md",)
        requests = len(service.requests)
        accepted = accept_proposal(
            root, proposal.proposal_id, version=proposal.version, scope=scope
        )
        assert accepted.status == "added", accepted.message
        assert len(service.requests) == requests
    assert "images/unrelated.png" not in copied
    assert not any(path.endswith("unreferenced.png") for path in copied)
    assert not any(path.endswith((".sqlite", ".db")) for path in copied)
    page = read_page(root, "concepts/value.md", scope=storage_scope(root, identity))
    assert "The value is 23." in page.body
    assert set(knowledge.bind().page_input_revisions()["concepts/value.md"]) == (
        {old.unit_revision_id, current.unit_revision_id}
        if factual_basis
        else {current.unit_revision_id}
    )
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
