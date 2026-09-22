"""DOCX notes stay with their paragraph while preserving their original source role."""

import json

from openkb.application.documents import import_document
from openkb.evidence import ParseStore
from tests.docx_attachment_fixtures import docx_with_parts


def test_comment_role_reaches_every_compilation_stage(kb_dir, tmp_path, model_service):
    text = "Example value: 0x 000003d7 00000001 000000110"
    path = docx_with_parts(
        tmp_path / "comment.docx",
        '<w:p><w:r><w:t>File change record.</w:t><w:commentReference w:id="0"/></w:r></w:p>',
        parts={
            "word/comments.xml": (
                '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:comment w:id="0" w:author="Editor"><w:p><w:r><w:t>'
                + text
                + "</w:t></w:r></w:p></w:comment></w:comments>"
            ).encode()
        },
        relationships='<Relationship Id="comments" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" '
        'Target="comments.xml"/>',
    )
    result = import_document(kb_dir, path)
    assert result.knowledge_compilation == "completed", result
    parsed = ParseStore(kb_dir).load(result.parse_id)
    expected = [{"text": text, "source_kind": "editorial_comment"}]
    assert parsed.blocks[0].context_data["inline_annotations"] == expected
    seen = set()
    for request in model_service:
        payload = json.loads(request["messages"][-1]["content"])
        stage = payload.get("stage")
        if stage not in {"planning", "generation", "verification"}:
            continue
        rows = payload["evidence"]["blocks"]
        row = next(row for row in rows if "File change record." in row["text"])
        context = row["context_data"]
        if "context_ref" in context:
            context = payload["context_pool"][context["context_ref"]]
        assert context["inline_annotations"] == expected
        assert "inline_annotations" in request["messages"][0]["content"]
        seen.add(stage)
    assert seen == {"planning", "generation", "verification"}
