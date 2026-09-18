"""CSV records retain quoted newlines, field order and physical line extents."""

import csv
import io
import json

from openkb.docx_containers import decode_text
from openkb.evidence import BlockDraft
from openkb.parsing_failures import DocumentContentError
from openkb.processing import processing_checkpoint


def parse_csv(path, store):
    text = decode_text(path.read_bytes())
    lines = text.splitlines(keepends=True)
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
    blocks = []
    columns = []
    previous = 0
    try:
        for row, fields in enumerate(reader, 1):
            processing_checkpoint()
            start, previous = previous, reader.line_num
            if row == 1:
                columns = fields
            blocks.append(
                BlockDraft(
                    "".join(lines[start:previous]).rstrip("\r\n"),
                    "table",
                    {
                        "kind": "csv",
                        "row": row,
                        "line": start + 1,
                        "line_end": previous,
                        "columns": columns,
                        "delimiter": dialect.delimiter,
                    },
                    context=json.dumps({"headers": columns, "fields": fields}, ensure_ascii=False),
                )
            )
    except csv.Error as exc:
        raise DocumentContentError("Malformed CSV record") from exc
    return blocks, [] if blocks else [{"status": "needs_review", "reason": "empty_content"}]
