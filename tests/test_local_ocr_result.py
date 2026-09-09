"""Pinned local OCR output maps real crop pixels back to physical PDF positions."""

import json
from pathlib import Path

import pymupdf
import pytest

from openkb.sources import SourceStore


def test_local_layout_keeps_headings_and_unrotated_pdf_coordinates(kb_dir):
    from openkb.ocr.local_result import parse_local_page

    report = json.loads(
        (Path(__file__).parent / "fixtures/paddleocr-vl16-cpu-page.json").read_text()
    )
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.set_rotation(90)
        image_size = (report["result"]["width"], report["result"]["height"])

        def no_assets(name):
            raise AssertionError(f"Unexpected asset: {name}")

        blocks, reason = parse_local_page(
            report, page, 7, image_size, SourceStore(kb_dir), no_assets
        )
        assert reason is None
        title = next(block for block in blocks if block.kind == "heading")
        assert title.text == "Synthetic installation manual"
        assert title.location["page"] == 7
        # Actual worker fixture has a 612 x 792 raster. PDF rotation swaps
        # axes: (x, y) rendered becomes (y, 600 - x) on the original PDF.
        assert image_size == (612, 792)
        assert title.location["bbox"] == pytest.approx(
            [32.323232, 325.490196, 42.929293, 553.921569]
        )
        assert any("--timeout 42" in block.text for block in blocks)


def test_reparse_reuses_retained_local_output_after_adapter_upgrade(
    kb_dir, tmp_path, monkeypatch, model_service
):
    import subprocess
    import sys

    import yaml

    from openkb.application.documents import import_document
    from openkb.application.source_actions import reparse_source
    from openkb.application.source_cleanup import cleanup_history, preview_history_cleanup
    from openkb.application.source_history import source_status
    from openkb.evidence import ParseStore
    from openkb.ocr import assembly

    assets = tmp_path / "models"
    assets.mkdir()
    (assets / "manifest.json").write_text("{}")
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["parsing"] = {
        "ocr": {
            "backend": "local",
            "local": {
                "interpreter": sys.executable,
                "assets": str(assets),
                "assets_sha256": "0" * 64,
                "limits": {
                    "seconds": 10,
                    "cleanup_seconds": 3,
                    "max_pages": 1,
                    "memory_bytes": 256 * 1024**2,
                    "output_bytes": 1024**2,
                    "max_regions": 4,
                    "max_tokens": 1024,
                },
                "parameters": {
                    "max_new_tokens": 256,
                    "min_pixels": 112896,
                    "max_pixels": 524288,
                    "render_dpi": 72,
                    "threads": 1,
                },
            },
        }
    }
    config_path.write_text(yaml.safe_dump(config))
    original = tmp_path / "scan.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=612, height=792)
        pdf.save(original)
    fixture = Path(__file__).parent / "fixtures/paddleocr-vl16-cpu-page.json"
    worker = tmp_path / "optional-runtime-fixture.py"
    worker.write_text("""
import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
report = json.loads(Path(sys.argv[2]).read_text())
report.update(input_sha256=plan['input_sha256'], assets_sha256=plan['assets_sha256'])
output = Path(plan['output'])
(output / 'result.json').write_text(json.dumps(report))
(output / 'usage.json').write_text(json.dumps({'regions': 4, 'reserved_output_tokens': 1024}))
(output / 'supervision.json').write_text(json.dumps({
    'reaped': True, 'exit_code': 0, 'reason': None,
    'peak_memory_bytes': 0, 'peak_output_bytes': 0,
}))
""")
    launch = subprocess.Popen

    def optional_runtime(command, **kwargs):
        return launch([sys.executable, str(worker), command[-1], str(fixture)], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", optional_runtime)
    first = import_document(kb_dir, original)
    assert first.knowledge_compilation == "completed", first
    before = source_status(kb_dir, first.source_id)["local_ocr"]
    assert before["attempts"] == 1 and before["reserved_output_tokens"] == 1024
    cleanup_history(kb_dir, preview_history_cleanup(kb_dir).id)
    monkeypatch.setattr(assembly, "REVISION", "synthetic-next-contract")
    updated = reparse_source(kb_dir, first.source_id, version_id=first.input_version)
    assert updated.parse_id != first.parse_id
    assert ParseStore(kb_dir).load(first.parse_id).blocks
    assert source_status(kb_dir, first.source_id)["local_ocr"] == before
    texts = [
        SourceStore(kb_dir).asset(b.blob).read_text()
        for b in ParseStore(kb_dir).load(updated.parse_id).blocks
    ]
    assert any("--timeout 42" in text for text in texts)
