"""Original image identity survives normalization and optional OCR in public source reads."""

import asyncio
import hashlib
import io
import json

import pytest
from agents.tool_context import ToolContext
from PIL import Image, ImageDraw, PngImagePlugin

from openkb.application.documents import import_document
from openkb.evidence import BlockDraft, ParseStore
from openkb.sources import SourceStore
from tests.docx_attachment_fixtures import docx_with_parts
from tests.http_model_fixture import evidence_response


def _source(path, *, alt):
    picture = Image.new("RGB", (320, 180), "white")
    ImageDraw.Draw(picture).text((10, 10), "Inspect valve before startup.", fill="black")
    data = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text(
        "Description", "Original image bytes retained independently of display encoding"
    )
    picture.save(data, format="PNG", pnginfo=metadata)
    description = f'<wp:docPr id="1" name="Picture" descr="{alt}"/>' if alt else ""
    docx_with_parts(
        path,
        "<w:p><w:r><w:t>Inspect the valve before startup.</w:t><w:drawing>"
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/'
        'drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + description
        + '<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="panel"/>'
        "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>",
        parts={"word/media/panel.png": data.getvalue()},
        relationships='<Relationship Id="panel" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/panel.png"/>',
    )
    return data.getvalue()


@pytest.mark.parametrize("alt", ["Valve panel", "Original figure", None])
def test_image_origins_reach_compiler_and_bounded_query_without_guessing_from_alt(
    kb_dir, tmp_path, monkeypatch, model_service, alt
):
    import openkb.parsing as parsing
    from openkb.agent.source_tools import source_tools

    source = tmp_path / "panel.docx"
    original = _source(source, alt=alt)
    store = SourceStore(kb_dir)
    crop_data = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(crop_data, format="PNG")
    crop = store.put_bytes(crop_data.getvalue())

    class RegionOcr:
        def page(self, *args, **kwargs):
            return [
                BlockDraft(
                    f"Visible panel text.\n![Original figure](asset:{crop})",
                    "paragraph",
                    {"kind": "pdf", "page": 1},
                    (crop,),
                    "OCR layout block 0; label=image",
                )
            ], None

        def close(self):
            pass

    monkeypatch.setattr(parsing, "create_ocr", lambda *a, **k: RegionOcr())
    observed = []

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        if payload["stage"] in {"planning", "generation", "verification"}:
            observed.append(payload)
        return evidence_response(payload)

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.status == "added" and result.knowledge_compilation == "completed", result
    parsed = ParseStore(kb_dir).load(result.parse_id)
    block = next(b for b in parsed.blocks if crop in b.assets)
    assert block.context_data is not None, "Image origins must not disappear into mixed text."
    relation = block.context_data["image_relations"][0]
    assert relation["original_asset"] == hashlib.sha256(original).hexdigest()
    assert relation.get("source_alt") == alt
    assert len(relation["frames"]) == 1
    frame = relation["frames"][0]
    assert frame["number"] == 1 and frame["ocr_assets"] == [crop]
    assert frame["asset"] != relation["original_asset"]
    assert store.asset(relation["original_asset"]).read_bytes() == original
    with Image.open(io.BytesIO(original)) as raw, Image.open(store.asset(frame["asset"])) as shown:
        assert raw.convert("RGB").tobytes() == shown.convert("RGB").tobytes()
    assert {p["stage"] for p in observed} == {"planning", "generation", "verification"}
    for payload in observed:
        rows = payload["evidence"]["blocks"]
        row = next(r for r in rows if "Visible panel text." in r["text"])
        context = row["context_data"]
        if "context_ref" in context:
            context = payload["context_pool"][context["context_ref"]]
        assert context["image_relations"] == [relation]

    tools = {t.name: t for t in source_tools(kb_dir)[0]}

    def invoke(name, **args):
        return json.loads(
            asyncio.run(
                tools[name].on_invoke_tool(
                    ToolContext(
                        context=None,
                        tool_name=name,
                        tool_call_id="image-origin",
                        tool_arguments="{}",
                    ),
                    json.dumps(args),
                )
            )
        )

    tree = invoke("read_source_tree", source_id=result.source_id)
    node = next(n for n in tree["nodes"] if n["start"] <= block.order < n["end"])
    cursor = {"offset": block.order - node["start"], "start": 0}
    contexts = []
    for _ in range(60):
        response = invoke(
            "read_source_node",
            source_id=result.source_id,
            node_id=node["id"],
            max_chars=128,
            **cursor,
        )
        row = response["evidence"][0]
        assert len(row["text"]) + len(row["context"]) <= 128
        contexts.append(row["context"])
        images = {i["asset"]: i for i in row["images"]}
        assert images[relation["original_asset"]]["extent"] == "whole_original_image"
        assert images[frame["asset"]]["extent"] == "whole_rendered_frame"
        assert images[crop]["extent"] == "not_established_by_source_association"
        assert images[crop]["provenance"][0]["role"] == "ocr_output"
        if row["context_complete"]:
            break
        cursor = response["next"]
    else:
        pytest.fail("Image provenance pagination did not finish")
    assert json.loads("".join(contexts))["image_relations"] == [relation]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(source_excerpts="not a list"),
        lambda d: d["structure"].update(table=True),
        lambda d: d["image_relations"][0].update(source_alt=4),
        lambda d: d["image_relations"][0]["frames"][0].update(number=True),
        lambda d: d["image_relations"][0]["frames"].append(
            dict(d["image_relations"][0]["frames"][0])
        ),
        lambda d: d["image_relations"][0]["frames"][0].update(ocr_assets=["c" * 64]),
    ],
)
def test_image_context_rejects_malformed_or_unbound_relations(change):
    details = {
        "source_excerpts": [],
        "structure": {},
        "reader_status": {},
        "image_relations": [
            {
                "original_asset": "a" * 64,
                "frames": [{"number": 1, "asset": "b" * 64, "ocr_assets": []}],
            }
        ],
    }
    change(details)
    with pytest.raises(ValueError):
        BlockDraft(
            "Retained source text",
            "paragraph",
            {"kind": "docx", "paragraph": 1},
            ("a" * 64, "b" * 64),
            context_data=details,
        )


def test_long_original_alt_stays_in_bounded_context_not_image_shortcuts(
    kb_dir, tmp_path, model_service
):
    from openkb.agent.source_tools import source_tools
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest

    # This checks native DOCX metadata, not the host's optional OCR engine or startup time.
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"parsing": {"ocr": {"policy": "off"}}})
    )

    original_alt = "Native description. " * 1000
    source = tmp_path / "long-description.docx"
    _source(source, alt=original_alt)
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed"
    parsed = ParseStore(kb_dir).load(imported.parse_id)
    block = next(b for b in parsed.blocks if b.assets)
    tools = {t.name: t for t in source_tools(kb_dir)[0]}

    def invoke(name, **args):
        return json.loads(
            asyncio.run(
                tools[name].on_invoke_tool(
                    ToolContext(
                        context=None, tool_name=name, tool_call_id="long-alt", tool_arguments="{}"
                    ),
                    json.dumps(args),
                )
            )
        )

    tree = invoke("read_source_tree", source_id=imported.source_id)
    node = next(n for n in tree["nodes"] if n["start"] <= block.order < n["end"])
    row = invoke(
        "read_source_node", source_id=imported.source_id, node_id=node["id"], max_chars=128
    )["evidence"][0]
    assert len(row["text"]) + len(row["context"]) <= 128
    assert row["next_start"] is not None
    for image in row["images"]:
        assert len(json.dumps(image["provenance"])) < 120
        assert original_alt not in json.dumps(image)
    assert block.context_data["image_relations"][0]["source_alt"] == original_alt
