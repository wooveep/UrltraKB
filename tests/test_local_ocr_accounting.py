"""Optional-runtime failure accounting through the shared document operation.

Set OCR_RUNTIME_PYTHON to the locked optional interpreter. The deliberately
missing asset fails before importing Paddle or loading any models.
"""

import hashlib
import json
import os
from pathlib import Path

import pymupdf
import pytest
import yaml

from openkb.application.documents import import_document
from openkb.application.source_history import source_status


@pytest.mark.skipif(
    not os.environ.get("OCR_RUNTIME_PYTHON"), reason="optional OCR interpreter required"
)
def test_unknown_local_failure_keeps_cumulative_reservation_across_pages(kb_dir, tmp_path):
    interpreter = Path(os.environ["OCR_RUNTIME_PYTHON"]).absolute()
    assets = tmp_path / "assets"
    assets.mkdir()
    manifest = assets / "manifest.json"
    manifest.write_text(json.dumps({"files": {"missing-model-file": {"sha256": "0" * 64}}}))
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["parsing"] = {
        "ocr": {
            "backend": "local",
            "local": {
                "interpreter": str(interpreter),
                "assets": str(assets),
                "assets_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "limits": {
                    "seconds": 10,
                    "cleanup_seconds": 3,
                    "max_pages": 2,
                    "memory_bytes": 256 * 1024**2,
                    "output_bytes": 1024**2,
                    "max_regions": 2,
                    "max_tokens": 1024,
                },
                "parameters": {
                    "max_new_tokens": 512,
                    "min_pixels": 112896,
                    "max_pixels": 524288,
                    "render_dpi": 72,
                    "threads": 1,
                },
            },
        }
    }
    config_path.write_text(yaml.safe_dump(config))
    original = tmp_path / "two-pages.pdf"
    with pymupdf.open() as document:
        document.new_page(width=100, height=100)
        document.new_page(width=100, height=100)
        document.save(original)
    result = import_document(kb_dir, original)
    assert result.source_intake == "saved" and result.knowledge_compilation == "unfinished"
    usage = source_status(kb_dir, result.source_id)["local_ocr"]
    assert usage["attempts"] == 1 and usage["unknown_usage"] == 1
    assert usage["reserved_output_tokens"] == 1024 and usage["regions"] == 2
    assert usage["runs"][0]["page"] == 1 and usage["runs"][0]["reaped"]
    assert "ocr_recognition_budget_exhausted" in result.quality
    assert not list((kb_dir / "wiki/summaries").glob("*.md"))
    again = import_document(kb_dir, original)
    assert again.source_id == result.source_id and again.input_version == result.input_version
    total = source_status(kb_dir, result.source_id)["local_ocr"]
    assert total["attempts"] == 2 and total["unknown_usage"] == 2
    assert total["reserved_output_tokens"] == 2048
