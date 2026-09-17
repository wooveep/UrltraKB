"""Only descendants within the parser's depth limit can invalidate an OCR cache."""

import pytest
import yaml

from openkb.docx_containers import MAX_EMBEDDED_DEPTH
from openkb.evidence import BlockDraft, ParseStore
from openkb.locks import atomic_write_json
from openkb.ocr.config import parsing_settings
from openkb.ocr.recovery import has_resumable_jobs
from openkb.sources import SourceStore, content_id
from tests.document_fixtures import write_docx
from tests.test_cloud_ocr import cloud_settings
from tests.test_source_evidence import save_source


@pytest.mark.parametrize("legacy_root", [False, True])
def test_out_of_depth_descendant_does_not_invalidate_parent_cache(kb_dir, tmp_path, legacy_root):
    cloud_settings(kb_dir)
    settings = parsing_settings(
        yaml.safe_load((kb_dir / ".openkb/config.yaml").read_text())["parsing"]
    ).ocr
    source = tmp_path / "parent.docx"
    write_docx(source, "<w:p><w:r><w:t>Retained body.</w:t></w:r></w:p>")
    versions = [save_source(kb_dir, source)]
    store, parses = SourceStore(kb_dir), ParseStore(kb_dir)
    part = "word/embeddings/child.docx"
    for depth in range(1, MAX_EMBEDDED_DEPTH + 2):
        versions.append(
            store.intake_attachment(
                versions[-1], part=part, name=f"child-{depth}.docx", content=source.read_bytes()
            )
        )

    def legacy(child, position):
        return {
            "kind": "docx",
            "paragraph": 1,
            "attachment": {
                "part": part,
                "name": child.name,
                "blob": child.blob,
                "position": position,
            },
        }

    for depth, version in reversed(list(enumerate(versions))):
        location, assets = {"kind": "docx", "paragraph": 1}, ()
        if depth + 1 < len(versions):
            child = versions[depth + 1]
            assets = (child.blob,)
            if depth == 0 and legacy_root:
                location = legacy(child, legacy(versions[2], location))
            else:
                location["attachment_files"] = [
                    {"part": part, "name": child.name, "blob": child.blob, "parseable": True}
                ]
        parsed = parses.save(
            version,
            {"test": "retained-attachment-chain"},
            [BlockDraft("Retained body.", "paragraph", location, assets)],
        )
        parses.select(version, parsed)

    def pending_job(version):
        intent = {"source": version.id, "page": 1, "profile": {"ocr": settings.cloud.profile()}}
        identity = content_id(intent)
        atomic_write_json(
            store.root / "cloud-jobs" / f"{identity}.json",
            {"identity": identity, "input": intent, "state": "submitted"},
        )

    parent = parses.selected(versions[0])
    pending_job(versions[MAX_EMBEDDED_DEPTH + 1])
    assert not has_resumable_jobs(store, versions[0], parent, settings, {}, {})
    pending_job(versions[MAX_EMBEDDED_DEPTH])
    assert has_resumable_jobs(store, versions[0], parent, settings, {}, {})
