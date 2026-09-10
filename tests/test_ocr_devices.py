"""Device failures and OpenVINO evidence via document processing, at the process boundary."""

import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from openkb.application.documents import import_document
from openkb.application.settings import apply_kb_config_patch
from openkb.application.settings_data import KbConfigPatchRequest
from openkb.evidence import ParseStore
from openkb.sources import SourceStore


def setup_runtime(kb_dir, tmp_path, *, device="auto"):
    assets = tmp_path / "models"
    assets.mkdir()
    (assets / "manifest.json").write_text("{}")
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir),
            config={
                "parsing": {
                    "ocr": {
                        "policy": "auto",
                        "backend": "local",
                        "device": device,
                        "local": {
                            "runtime": "openvino",
                            "interpreter": sys.executable,
                            "assets": str(assets),
                            "assets_sha256": "0" * 64,
                            "limits": {
                                "seconds": 30,
                                "cleanup_seconds": 3,
                                "max_pages": 2,
                                "memory_bytes": 1024**3,
                                "output_bytes": 4 * 1024**2,
                                "max_regions": 8,
                                "max_tokens": 2048,
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
            },
        ),
    )
    original = tmp_path / "scan.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=400, height=600).draw_circle((100, 100), 20)
        pdf.save(original)
    return original


def external_runtime(tmp_path, monkeypatch, failure):
    worker = tmp_path / "fixture.py"
    worker.write_text("""
import json,sys
from pathlib import Path
from PIL import Image
plan=json.loads(Path(sys.argv[1]).read_text())
out=Path(plan['output'])
failed=plan['device']!='cpu' and sys.argv[2]!='none'
if failed:
 (out/'failure.json').write_text(json.dumps({'reason':sys.argv[2]}))
else:
 report={'input_sha256':plan['input_sha256'],'assets_sha256':plan['assets_sha256'],
  'runtime':{'engine':'paddleocr','runtime':'openvino','model':'PaddleOCR-VL-1.5',
   'devices':{'layout':'CPU','vision':'CPU','embedding':'CPU','language':'CPU','projection':'CPU'}},
  'result':{'contract':'openkb-openvino-page-v1','physical_page':plan['physical_page'],
   'width':Image.open(plan['input']).width,'height':Image.open(plan['input']).height,'blocks':[
    {'id':0,'order':0,'label':'doc_title','bbox':[20,30,300,70],
     'text':'A verified OpenVINO title','status':'completed','assets':[],'tokens':12},
    {'id':1,'order':1,'label':'text','bbox':[20,80,350,140],
     'text':'Useful partial text','status':'length','assets':[],'tokens':256}]},'assets':{}}
 (out/'result.json').write_text(json.dumps(report))
(out/'usage.json').write_text(json.dumps({'regions':1,'reserved_output_tokens':256}))
(out/'supervision.json').write_text(json.dumps({'reaped':True,'exit_code':1 if failed else 0,
 'reason':None,'peak_memory_bytes':0,'peak_output_bytes':0}))
""")
    launch = subprocess.Popen
    calls = []

    def start(command, **kwargs):
        plan = json.loads(Path(command[-1]).read_text())
        calls.append(plan)
        return launch([sys.executable, str(worker), command[-1], failure], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    return calls


def test_auto_device_failure_reaps_then_retries_cpu_with_remaining_budget(
    kb_dir, tmp_path, monkeypatch, model_service
):
    original = setup_runtime(kb_dir, tmp_path)
    calls = external_runtime(tmp_path, monkeypatch, "ocr_device_initialization_failed")
    result = import_document(kb_dir, original)
    assert [row["device"] for row in calls] == ["gpu", "cpu"], result
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"] - 256
    parsed = ParseStore(kb_dir).load(result.parse_id)
    assert any("ocr_output_incomplete" in q["reason"] for q in parsed.quality)
    texts = [SourceStore(kb_dir).asset(b.blob).read_text() for b in parsed.blocks]
    assert any("verified OpenVINO title" in text for text in texts)
    assert any("Useful partial text" in text for text in texts)
    block = next(b for b in parsed.blocks if b.kind == "heading")
    assert block.location["page"] == 1
    assert block.location["bbox"] == [20.0, 30.0, 300.0, 70.0]
    assert json.loads(block.context)["ocr"]["model"] == "PaddleOCR-VL-1.5"
    assert json.loads(block.context)["ocr"]["devices"]["language"] == "CPU"


@pytest.mark.parametrize(
    "device,failure,count",
    [
        ("gpu", "ocr_device_initialization_failed", 1),
        ("auto", "ocr_output_contract_unknown", 1),
        ("auto", "ocr_time_budget_exhausted", 1),
        ("cpu", "none", 1),
    ],
)
def test_explicit_device_and_non_device_failures_do_not_switch_execution(
    kb_dir, tmp_path, monkeypatch, model_service, device, failure, count
):
    original = setup_runtime(kb_dir, tmp_path, device=device)
    calls = external_runtime(tmp_path, monkeypatch, failure)
    result = import_document(kb_dir, original)
    assert len(calls) == count
    assert calls[0]["device"] == ("cpu" if device == "cpu" else "gpu")
    assert result.source_intake == "saved"


def test_successful_ocr_repair_retains_text_matching_invisible_layer(
    kb_dir, tmp_path, monkeypatch, model_service
):
    source = setup_runtime(kb_dir, tmp_path, device="cpu")
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_text((20, 40), "A verified OpenVINO title", render_mode=3)
        pdf.save(source)
    worker_calls = external_runtime(tmp_path, monkeypatch, "none")
    worker = tmp_path / "fixture.py"
    worker.write_text(worker.read_text().replace("'status':'length'", "'status':'completed'"))
    result = import_document(kb_dir, source)
    assert len(worker_calls) == 1
    parsed = ParseStore(kb_dir).load(result.parse_id)
    texts = [SourceStore(kb_dir).asset(b.blob).read_text() for b in parsed.blocks]
    assert sum("A verified OpenVINO title" in t for t in texts) == 1
    assert not any("ocr_missing_page" in q["reason"] for q in parsed.quality)
