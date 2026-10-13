"""Frozen Office PDFs use the same source roles in every import/read entrance."""

import base64
import hashlib
import io
import json

import pymupdf
import pytest
from pageindex import IndexConfig
from pageindex.llm import IndexResponse
from PIL import Image
from test_answer_evidence_regression import agent_for, invoke
from test_native_source_contract import LITERAL_TABLE
from test_pdf_tables import merged_table_pdf

from openkb.agent.source_windows import CompileSourceWindows
from openkb.images import convert_pdf_to_pages
from openkb.index_client import create_index_client
from openkb.index_pdf import NativePdfParser
from openkb.indexer import index_long_document
from openkb.locks import atomic_write_json, atomic_write_text
from openkb.office.records import Slide
from openkb.office.slide_content import attach_slides
from openkb.office.slide_package import SlidePackage


class SlideModel:
    def __init__(self):
        self.inputs = []

    def complete(self, messages, *, stage):
        prompt = messages[-1]["content"]
        self.inputs.append(prompt)
        if stage == "structure":
            first = "Before the table: keep this condition."
            return IndexResponse(
                json.dumps(
                    [
                        {
                            "structure": "1",
                            "title": "Presentation",
                            "title_origin": "generated",
                            "physical_index": 1,
                            "anchor": {
                                "unit": 1,
                                "part": "body",
                                "range": [0, len(first)],
                                "excerpt": first,
                            },
                        }
                    ]
                )
            )
        return IndexResponse('{"answer":"yes"}')

    async def acomplete(self, messages, *, stage):
        return self.complete(messages, stage=stage)


def slide_fixture(tmp_path):
    pdf = merged_table_pdf(tmp_path / "manual.pdf")
    stream = io.BytesIO()
    image = Image.new("RGBA", (60, 40))
    for x in range(60):
        for y in range(40):
            image.putpixel((x, y), (255, 0, 0, (0, 128, 255)[x // 20]))
    image.save(stream, format="PNG")
    with pymupdf.open(pdf) as document:
        page = document[0]
        page.insert_text((60, 275), "Before figure")
        page.insert_text((60, 440), "After figure")
        page.insert_image(pymupdf.Rect(60, 300, 180, 400), stream=stream.getvalue())
        body = page.get_text()
        document.saveIncr()
    slides = [
        Slide(
            ordinal=1,
            page=1,
            name="Native",
            hidden=True,
            body=body,
            notes="Native speaker note.\n" + LITERAL_TABLE,
        )
    ]
    data = pdf.read_bytes()
    package = tmp_path / "manual.okpi"
    atomic_write_text(
        package,
        SlidePackage(
            pdf=base64.b64encode(data).decode(),
            pdf_digest=hashlib.sha256(data).hexdigest(),
            slides=slides,
        ).model_dump_json(),
    )
    return pdf, package, slides


@pytest.mark.asyncio
async def test_slide_body_index_compile_and_question_share_native_pdf_roles(kb_dir, tmp_path):
    pdf, package, slides = slide_fixture(tmp_path)
    original = package.read_bytes()
    pages = attach_slides(
        convert_pdf_to_pages(pdf, "manual", kb_dir / "wiki/sources/images/manual"), slides
    )
    atomic_write_json(kb_dir / "wiki/sources/manual.json", pages)
    native = NativePdfParser().parse(str(pdf), images_dir=str(tmp_path / "native-images"))
    runtime = SlideModel()
    options = IndexConfig(
        llm_client=runtime, if_add_node_summary=False, if_add_doc_description=False
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        collection = client.collection()
        identity = collection.add(str(package))
        indexed = collection.get_page_content(identity, "1")[0]
    for body in [pages[0]["parts"]["body"], indexed["parts"]["body"]]:
        assert 'rowspan="3"' in body
        assert body.index("Before figure") < body.index("![image]") < body.index("After figure")
    assert pages[0]["parts"]["body"] == native.nodes[0].content
    assert indexed["parts"]["body"] in indexed["content"]
    assert indexed["slide"]["hidden"] is True
    assert indexed["parts"]["notes"] == slides[0].notes
    for asset in [kb_dir / "wiki/sources/images/manual/p1_img1.png", indexed["images"][0]["path"]]:
        with Image.open(asset) as image:
            assert [image.convert("RGBA").getpixel((x, 20))[3] for x in (10, 30, 50)] == [
                0,
                128,
                255,
            ]
    windows = CompileSourceWindows(kb_dir / "wiki", "manual")
    for part, expected in [(None, 1), ("body", 1), ("notes", 0)]:
        compiled = windows.read_selection("sources/manual.json", {"pages": "1", "part": part})[0]
        response = await invoke(
            agent_for(kb_dir),
            "get_slide_content",
            doc_name="manual",
            slides="1",
            part=part or "all",
        )
        public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
        for payload in [compiled, public]:
            if part == "body":
                assert "Native speaker note" not in json.dumps(payload)
                assert LITERAL_TABLE not in json.dumps(payload)
            if part == "notes":
                assert "shared-group" not in json.dumps(payload)
            assert len(payload.get("native_tables", [])) == expected
            if expected:
                merged = next(c for c in payload["native_tables"][0]["cells"] if c["id"] == "r2c3")
                assert merged["value"] == "shared-group" and merged["rows"] == [2, 4]
            else:
                assert LITERAL_TABLE in payload["content"]
                assert not payload["units"][0]["images"]
    assert any('rowspan=\\"3\\"' in prompt for prompt in runtime.inputs)
    assert package.read_bytes() == original


@pytest.mark.parametrize("format", ["pdf", "pptx"])
def test_import_retains_the_contract_used_by_the_real_index(kb_dir, tmp_path, monkeypatch, format):
    from types import SimpleNamespace

    from test_index_pdf_layout import TableIndexModel

    pdf, package, slides = slide_fixture(tmp_path)
    runtime = SlideModel() if format == "pptx" else TableIndexModel()

    class LocalModel:
        def complete(self, role, request):
            response = runtime.complete(request.payload()["messages"], stage=request.stage)
            return SimpleNamespace(text=response.text, finish_reason="stop")

        async def acomplete(self, role, request):
            return self.complete(role, request)

    monkeypatch.setattr("openkb.llm_execution.compiler_executor", lambda *args: LocalModel())
    path = package if format == "pptx" else pdf
    result = index_long_document(path, kb_dir, doc_name="manual")
    pages = json.loads((kb_dir / "wiki/sources/manual.json").read_text())
    windows = CompileSourceWindows(kb_dir / "wiki", "manual")
    payload = windows.current_original_evidence()
    assert len(payload["native_tables"]) == 1
    assert len(windows.page_figures().figures) == 1
    assert 'rowspan="3"' in pages[0]["content"]
    if format == "pptx":
        assert 'rowspan="3"' in pages[0]["parts"]["body"]
        assert pages[0]["parts"]["notes"] == slides[0].notes
    with create_index_client(
        storage_path=kb_dir / ".openkb", index_config=IndexConfig(llm_client=runtime)
    ) as client:
        indexed = client.collection().get_page_content(result.doc_id, "1")[0]
        assert 'rowspan="3"' in indexed["content"]


@pytest.mark.parametrize("part", ["body", "notes"])
def test_normal_compile_plan_preserves_the_slide_partition(kb_dir, tmp_path, part):
    from openkb.agent.compile_plan import normalize_plan

    pdf, _, slides = slide_fixture(tmp_path)
    pages = attach_slides(
        convert_pdf_to_pages(pdf, "manual", kb_dir / "wiki/sources/images/manual"), slides
    )
    atomic_write_json(kb_dir / "wiki/sources/manual.json", pages)
    plan = normalize_plan(
        {
            "concepts": {
                "create": [{"name": "rule", "reading_locations": {"pages": "1", "part": part}}]
            }
        },
        existing={"concepts": set(), "entities": set()},
        entity_types=frozenset({"other"}),
        sanitize=str,
    )
    context = CompileSourceWindows(kb_dir / "wiki", "manual").prepare(
        plan.concepts.create[0], long=True
    )
    assert context.windows and all(w["native_location"]["part"] == part for w in context.windows)
    combined = json.dumps(context.windows)
    assert ("shared-group" in combined) == (part == "body")
    assert ("Native speaker note" in combined) == (part == "notes")


@pytest.mark.parametrize(
    "location",
    [
        {"node_ids": ["slide-1"], "part": "notes"},
        {"chars": "0:10", "part": "body"},
        {"pages": "1", "part": "all"},
        {"pages": "1", "part": 1},
        {"pages": "1", "part": ["body"]},
    ],
)
def test_normal_compile_plan_rejects_unproved_part_coordinates(location):
    from openkb.agent.compile_plan import normalize_plan

    with pytest.raises(ValueError, match="reading_locations.part"):
        normalize_plan(
            {"concepts": {"create": [{"name": "rule", "reading_locations": location}]}},
            existing={"concepts": set(), "entities": set()},
            entity_types=frozenset({"other"}),
            sanitize=str,
        )


def test_slide_index_rejects_generated_media_anchors_with_bounded_correction(tmp_path):
    from pageindex.index.page_parts_policy import PagePartsContractError

    _, package, _ = slide_fixture(tmp_path)
    before = package.read_bytes()

    class MediaAnchorModel(SlideModel):
        def __init__(self):
            super().__init__()
            self.anchor_requests = []

        def complete(self, messages, *, stage):
            if stage not in {"structure", "correction"}:
                return super().complete(messages, stage=stage)
            prompt = messages[-1]["content"]
            self.anchor_requests.append(stage)
            unit, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"unit": 1,') :])
            body = unit["parts"]["body"]
            start = body.index("![image](")
            end = body.index("\n", start)
            return IndexResponse(
                json.dumps(
                    [
                        {
                            "structure": "1",
                            "title": "Invented image chapter",
                            "title_origin": "generated",
                            "physical_index": 1,
                            "anchor": {
                                "unit": 1,
                                "part": "body",
                                "range": [start, end],
                                "excerpt": body[start:end],
                            },
                        }
                    ]
                )
            )

    runtime = MediaAnchorModel()
    options = IndexConfig(
        llm_client=runtime, if_add_node_summary=False, if_add_doc_description=False
    )
    with create_index_client(storage_path=tmp_path / "index", index_config=options) as client:
        with pytest.raises(PagePartsContractError, match="after correction"):
            client.collection().add(str(package))
    assert runtime.anchor_requests == ["structure", "correction", "correction", "correction"]
    assert package.read_bytes() == before
