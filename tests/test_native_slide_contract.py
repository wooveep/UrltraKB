"""Frozen Office PDFs use the same source roles in every import/read entrance."""

import base64
import hashlib
import io
import json

import pymupdf
import pytest
from native_source_fixtures import LITERAL_TABLE
from PIL import Image
from test_answer_evidence_regression import invoke
from test_pdf_tables import merged_table_pdf

from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.images import convert_pdf_to_pages
from ultrakb.index_pdf import NativePdfParser
from ultrakb.locks import atomic_write_json, atomic_write_text
from ultrakb.office.records import Slide
from ultrakb.office.slide_content import attach_slides
from ultrakb.office.slide_package import SlidePackage


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
@pytest.mark.parametrize("retained_format", ["pdf", "slides"])
async def test_slide_body_index_compile_and_question_share_native_pdf_roles(
    kb_dir, tmp_path, live_llm, monkeypatch, retained_format
):
    from agents import Agent

    from ultrakb.agent.query_evidence import restrict_query_agent
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.query_views import resolve_query_views
    from ultrakb.application.recompilation import recompile_document
    from ultrakb.application.views import published_scope
    from ultrakb.config import load_config, save_config
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.processing_policy import PdfLimit, classify_pdf
    from ultrakb.storage.catalog_store import CatalogStore
    from ultrakb.storage.compile_inputs import materialize_inputs
    from ultrakb.storage.index_store import IndexStore

    live_llm.install(monkeypatch)
    pdf, package, slides = slide_fixture(tmp_path)
    original = package.read_bytes()
    base = published_scope(kb_dir).knowledge
    catalog = CatalogStore(kb_dir, base.identity)
    admitted = catalog.admit(package if retained_format == "slides" else pdf, source_key="manual")
    with base.compile_workspace() as workspace:
        wiki = workspace.directory
        pages = convert_pdf_to_pages(pdf, "manual", wiki / "sources/images/manual")
        if retained_format == "slides":
            pages = attach_slides(pages, slides)
        atomic_write_json(wiki / "sources/manual.json", pages)
        atomic_write_text(wiki / "sources/manual.md", pages[0]["content"])
        files = {
            path.relative_to(wiki / "sources").as_posix(): path
            for path in (wiki / "sources").rglob("*")
            if path.is_file()
        }
        processing = catalog.retain_processing(
            admitted.revision_id,
            files,
            body_path="manual.md",
            source_path="manual.json",
            source_kind="pages",
            fingerprint="native-slide-contract-v1",
            operation_id="manual",
            decision=classify_pdf(
                1, {"pdf_limit": PdfLimit(short_max_pages=0, source="kb").model_dump()}
            ),
        )
    unit = catalog.plan_unit(processing.processing_id, key="body", doc_name="manual")
    config_path = kb_dir / ".ultrakb/config.yaml"
    save_config(
        config_path,
        {
            **load_config(config_path),
            "model": "deepseek/deepseek-flash",
            "model_policy": {"max_calls": 30, "retries": 0},
        },
    )
    context = ExecutionContext(
        executor=CompletionExecutor(
            RoleBindings("deepseek/deepseek-flash", live_llm.credentials),
            ModelCallPolicy(max_calls=30, retries=0),
        )
    )
    result = await recompile_document(kb_dir, unit.source_id, unit_id=unit.unit_id, context=context)
    assert result.status == "compiled", result.message
    scope = published_scope(kb_dir)
    [index] = scope.knowledge.index_revisions()
    indexed = IndexStore(kb_dir, base.identity).document(index)["pages"][0]
    assert 'rowspan="3"' in indexed["content"]
    if retained_format == "slides":
        assert indexed["parts"]["notes"] == slides[0].notes
        assert indexed["slide"]["hidden"] is True
    native = NativePdfParser().parse(str(pdf), images_dir=str(tmp_path / "native-images"))
    agent = restrict_query_agent(
        Agent(name="fixture", instructions=""), resolve_query_views(kb_dir)
    )
    with scope.knowledge.compile_workspace() as baseline:
        materialize_inputs(scope.knowledge, baseline, unit)
        windows = CompileSourceWindows(baseline.directory, "manual")
        parts = (
            [(None, 1), ("body", 1), ("notes", 0)] if retained_format == "slides" else [(None, 1)]
        )
        for part, expected in parts:
            compiled = windows.read_selection("sources/manual.json", {"pages": "1", "part": part})[
                0
            ]
            response = await invoke(
                agent,
                "get_slide_content" if retained_format == "slides" else "get_page_content",
                **(
                    {"doc_name": "manual", "slides": "1", "part": part or "all"}
                    if retained_format == "slides"
                    else {"doc_name": "manual", "pages": "1"}
                ),
            )
            public = json.loads(response.split("\n\n", 1)[1].split("\nSource context: ", 1)[0])
            for payload in (compiled, public):
                assert len(payload.get("native_tables", [])) == expected
                if expected:
                    merged = next(
                        c for c in payload["native_tables"][0]["cells"] if c["id"] == "r2c3"
                    )
                    assert merged["value"] == "shared-group" and merged["rows"] == [2, 4]
                else:
                    assert LITERAL_TABLE in payload["content"]
                    assert not payload["units"][0]["images"]
                if part == "body":
                    assert "Native speaker note" not in json.dumps(payload)
            if part is None and retained_format == "slides":
                assert indexed["parts"]["body"] == native.nodes[0].content
        image_path = next((baseline.directory / "sources/images/manual").glob("*.png"))
        with Image.open(image_path) as image:
            assert [image.convert("RGBA").getpixel((x, 20))[3] for x in (10, 30, 50)] == [
                0,
                128,
                255,
            ]
    assert package.read_bytes() == original
    assert not list((kb_dir / ".ultrakb").glob("**/context.sqlite"))


@pytest.mark.parametrize("part", ["body", "notes"])
def test_normal_compile_plan_preserves_the_slide_partition(kb_dir, tmp_path, part):
    from ultrakb.agent.compile_plan import normalize_plan

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
    from ultrakb.agent.compile_plan import normalize_plan

    with pytest.raises(ValueError, match="reading_locations.part"):
        normalize_plan(
            {"concepts": {"create": [{"name": "rule", "reading_locations": location}]}},
            existing={"concepts": set(), "entities": set()},
            entity_types=frozenset({"other"}),
            sanitize=str,
        )


def test_slide_index_rejects_generated_media_anchors_after_correction(tmp_path):
    from pageindex.index.page_parts_policy import PagePartsContractError, PagePartsPolicy
    from pageindex.index.physical_sections import require_corrected_navigation

    from ultrakb.office.slide_package import FrozenSlideParser

    _, package, _ = slide_fixture(tmp_path)
    before = package.read_bytes()
    parsed = FrozenSlideParser().parse(str(package), images_dir=str(tmp_path / "images"))
    policy = PagePartsPolicy(
        {
            "unit_count": len(parsed.nodes),
            "page_parts": [node.metadata["parts"] for node in parsed.nodes],
            "page_generated_spans": [
                [
                    span["generated_span"]
                    for span in node.metadata["content_spans"]
                    if "generated_span" in span
                ]
                for node in parsed.nodes
            ],
        }
    )
    body = policy.parts[0]["body"]
    start = body.index("![image](")
    end = body.index("\n", start)
    untrusted = {
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
    assert not policy.valid(untrusted)
    with pytest.raises(ValueError, match="Unverified"):
        policy.validate_order([untrusted])
    with pytest.raises(PagePartsContractError, match="after correction"):
        require_corrected_navigation([{"list_index": 0}], policy)
    assert package.read_bytes() == before
