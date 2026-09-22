"""Content omissions retain their reason, never a completed batch's traceback bodies."""

import gc
import weakref

from openkb.application.documents import import_document


def test_failed_page_bodies_are_released_before_later_pages(kb_dir, tmp_path, monkeypatch):
    from openkb.agent import document_orchestrator, document_pages
    from openkb.agent.document_plan import DocumentPlan, OverviewPlan, PagePlan
    from openkb.processing import ProcessingIncomplete

    source = tmp_path / "omissions.md"
    source.write_text("\n\n".join(f"# Operation {n}\n\nKeep backup {n} intact." for n in range(4)))
    references, retained = [], []

    class Body:
        def __init__(self):
            self.text = "x" * 1_000_000

    def fail(*args, **kwargs):
        # At admission of the next page, no prior failed response is active.
        gc.collect()
        retained.append(sum(ref() is not None for ref in references))
        body = Body()
        references.append(weakref.ref(body))
        raise ProcessingIncomplete("document_generation_incomplete", "generation")

    def planned_document(_kb_dir, _workspace, _source, parsed, *_args, **_kwargs):
        page_count = min(4, len(parsed.blocks))
        return DocumentPlan(
            metadata={"protocol": "document-plan-v1", "recovery_key": "d" * 64},
            overview=OverviewPlan(
                text="Operations overview.",
                ranges=[[0, len(parsed.blocks)]],
                status="complete",
            ),
            pages=[
                PagePlan(
                    key=f"p{index}",
                    kind="concept",
                    name=f"concepts/operation-{index}",
                    title=f"Operation {index}",
                    purpose="Retain the operation instruction.",
                    subject_ranges=[
                        [
                            index * len(parsed.blocks) // page_count,
                            (index + 1) * len(parsed.blocks) // page_count,
                        ]
                    ],
                )
                for index in range(page_count)
            ],
        )

    monkeypatch.setattr(document_orchestrator, "plan_document", planned_document)
    monkeypatch.setattr(document_pages, "generate_document_page", fail)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert len(references) == 4
    assert retained and max(retained) == 0, retained
    assert any(
        row["stage"] == "generation" and row["reason"] == "document_generation_incomplete"
        for row in result.omissions
    )
