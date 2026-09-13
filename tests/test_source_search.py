"""Original literal search retains every row, independently of navigation summaries."""

import asyncio
import json

from openpyxl import Workbook

from openkb.application.conversations import ask_question
from openkb.application.documents import import_document


def test_original_search_paginates_all_matching_cells_with_row_context(
    kb_dir, tmp_path, model_service
):
    book = Workbook()
    sheet = book.active
    sheet.title = "Endpoints"
    sheet.append(["Component", "Port", "Address", "Protocol"])
    sheet.append(["collector", 4100, "127.0.0.1", "HTTP"])
    sheet.append(["database", 5200, "container network", "TCP"])
    sheet.append(["maintenance", 6300, "127.0.0.1", "ssh"])
    source = tmp_path / "endpoints.xlsx"
    book.save(source)
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    observed = []

    def chat(body):
        outputs = [m for m in body["messages"] if m["role"] == "tool"]
        offset = 0
        if outputs:
            result = json.loads(outputs[-1]["content"])
            assert result["total_matches"] == 2
            observed.extend(result["evidence"])
            offset = result["next_offset"]
            if offset is None:
                return {"role": "assistant", "content": "Literal matches returned."}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"search-{offset}",
                    "type": "function",
                    "function": {
                        "name": "search_source_text",
                        "arguments": json.dumps(
                            {
                                "source_id": imported.source_id,
                                "query": "127.0.0.1",
                                "offset": offset,
                                "limit": 1,
                            }
                        ),
                    },
                }
            ],
        }

    model_service.chat_response = chat
    result = asyncio.run(ask_question(kb_dir, "Which rows literally contain 127.0.0.1?"))
    assert result.status == "completed", result
    assert [r["location"]["cell_address"] for r in observed] == ["C2", "C4"]
    assert all(r["text"] == "127.0.0.1" and r["context_complete"] for r in observed)
    assert "collector" in observed[0]["context"] and "4100" in observed[0]["context"]
    assert "maintenance" in observed[1]["context"] and "6300" in observed[1]["context"]
    assert all(r["reference"]["parse_id"] == imported.parse_id for r in observed)
    assert all("#block-" in r["citation"] for r in observed)
