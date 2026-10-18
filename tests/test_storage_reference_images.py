"""The real desktop reader renders images from the answer's fixed publication."""

import io
from time import monotonic

import pytest
from PIL import Image
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge


@pytest.mark.asyncio
async def test_old_answer_reference_keeps_old_pixels_after_a_new_publication(tmp_path, monkeypatch):
    from ultrakb.application.answer_citations import read_answer_reference
    from ultrakb.application.conversations import ask_question
    from ultrakb.application.views import storage_scope
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.wiki_projection import ProjectionStore

    def png(color):
        stream = io.BytesIO()
        Image.new("RGB", (80, 40), color).save(stream, format="PNG")
        return stream.getvalue()

    root, identity, _ = question_knowledge(tmp_path)
    unbound = tmp_path / "unbound-current.png"
    unbound.write_bytes(png("blue"))
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(),
            {
                "sources/fixture.md": (
                    "Old 17 ![figure](images/figure.png)\n"
                    f"![unbound]({unbound.as_uri()})\n\nMath: $x^2$\n"
                ).encode(),
                "sources/images/figure.png": png("red"),
            },
        )
    )
    with StreamTransport([{"content": "See [[sources/fixture]]."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await ask_question(root, "Read the source.", scope=storage_scope(root, identity))
    assert result.status == "completed", result.error
    [reference] = result.citations["references"]
    knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(),
            {
                "sources/fixture.md": b"New 99 ![figure](images/figure.png)",
                "sources/images/figure.png": png("blue"),
            },
        )
    )
    assert ProjectionStore(root, identity).sync().status == "synced"
    content, _, base = read_answer_reference(root, reference)
    assert content.startswith("Old 17")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from ultrakb.desktop.reader import MarkdownView

    app = QApplication.instance() or QApplication([])
    reader = MarkdownView()
    try:
        ready = []
        reader.show_markdown(content, base, on_ready=lambda: ready.append(True))
        deadline = monotonic() + 5
        while not ready and monotonic() < deadline:
            QTest.qWait(10)
        assert ready
        app.processEvents()
        url = reader.document().baseUrl().resolved(QUrl("images/figure.png"))
        picture = reader.document().resource(QTextDocument.ResourceType.ImageResource, url)
        assert picture is not None and not picture.isNull()
        assert picture.toImage().pixelColor(0, 0).name() == "#ff0000"
        unknown = reader.document().resource(
            QTextDocument.ResourceType.ImageResource, QUrl(unbound.as_uri())
        )
        assert unknown is not None and unknown.toImage().pixelColor(0, 0).alpha() == 0
        assert reader._inline_images
        for formula_url in reader._inline_images:
            formula = reader.document().resource(
                QTextDocument.ResourceType.ImageResource, QUrl(formula_url)
            )
            assert formula is not None and not formula.isNull() and formula.width() > 1
    finally:
        reader.stop_rendering()
        reader.close()
