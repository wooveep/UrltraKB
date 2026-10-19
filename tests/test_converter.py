"""Parser behavior and the explicit private workspace boundary."""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from ultrakb.converter import _sanitize_stem, convert_document, get_pdf_page_count
from ultrakb.file_digest import hash_file
from ultrakb.locks import PrivateMaterialization


@pytest.fixture
def private_conversion(kb_dir):
    with PrivateMaterialization() as workspace:

        def convert(source):
            return convert_document(
                source,
                kb_dir,
                staging_dir=workspace.directory,
                private_workspace=workspace,
                doc_name=_sanitize_stem(source.stem),
            )

        yield convert


class TestGetPdfPageCount:
    def test_returns_page_count(self, tmp_path):
        """Mock pymupdf to return a doc with 5 pages."""
        fake_doc = MagicMock()
        fake_doc.page_count = 5
        fake_doc.__enter__ = MagicMock(return_value=fake_doc)
        fake_doc.__exit__ = MagicMock(return_value=False)
        with patch("ultrakb.converter.pymupdf.open", return_value=fake_doc):
            count = get_pdf_page_count(tmp_path / "fake.pdf")
        assert count == 5


class TestConvertDocumentMarkdown:
    def test_md_file_copied_to_wiki_sources(self, kb_dir, private_conversion):
        """A .md file is read and saved under wiki/sources/."""
        src = kb_dir / "raw" / "notes.md"
        src.write_text("# Notes\n\nSome content here.", encoding="utf-8")

        result = private_conversion(src)

        assert result.skipped is False
        assert result.is_long_doc is False
        assert result.source_path is not None
        assert result.source_path.exists()
        assert result.source_path.read_text(encoding="utf-8").startswith("# Notes")

    def test_md_raw_file_copied(self, kb_dir, private_conversion):
        """The original file should also be copied to raw/."""
        src = kb_dir / "input" / "notes.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("# Notes\n", encoding="utf-8")

        result = private_conversion(src)

        assert result.raw_path is not None
        assert result.raw_path.exists()


def _readable_pdf(path, pages):
    import pymupdf

    with pymupdf.open() as pdf:
        for ordinal in range(1, pages + 1):
            pdf.new_page().insert_text((30, 30), f"Source page {ordinal}")
        pdf.save(path)


class TestConvertDocumentPdfShort:
    def test_short_pdf_converted_via_pymupdf(self, kb_dir, tmp_path, private_conversion):
        """PDF under threshold is converted with pymupdf (convert_pdf_with_images)."""
        src = tmp_path / "short.pdf"
        _readable_pdf(src, 5)
        with patch(
            "ultrakb.converter.convert_pdf_with_images", return_value="# Short PDF\n\nConverted."
        ) as mock_cpwi:
            result = private_conversion(src)

        mock_cpwi.assert_called_once()
        assert result.skipped is False
        assert result.is_long_doc is False
        assert result.source_path is not None
        assert result.source_path.exists()


class TestConvertDocumentPdfLong:
    def test_long_pdf_returns_is_long_doc(self, kb_dir, tmp_path, private_conversion):
        """PDF >= threshold pages returns is_long_doc=True, source_path=None."""
        src = tmp_path / "long.pdf"
        _readable_pdf(src, 200)
        result = private_conversion(src)

        assert result.is_long_doc is True
        assert result.source_path is None
        assert result.skipped is False
        assert result.raw_path is not None


class TestConvertDocumentMarkItDown:
    def test_docx_conversion_uses_frozen_bytes_and_private_office_pdf(
        self, kb_dir, tmp_path, private_conversion
    ):
        import pymupdf

        src = tmp_path / "report.docx"
        src.write_bytes(b"frozen docx")

        def office(root, prepared, output, **kwargs):
            src.write_bytes(b"changed during conversion")
            assert prepared.read_bytes() == b"frozen docx"
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72, 72), "Visible Office text")
                pdf.save(output)
            return None

        with patch("ultrakb.office.convert.convert_office", side_effect=office):
            result = private_conversion(src)
        assert not result.skipped and not result.is_long_doc
        assert "Visible Office text" in result.source_path.read_text(encoding="utf-8")
        assert result.raw_path.read_bytes() == b"frozen docx"
        assert result.file_hash == hash_file(result.raw_path)
        assert result.processing.measurement_value == 1


class TestLazyMarkItDownImport:
    def test_importing_converter_does_not_load_markitdown(self):
        """`import ultrakb.converter` must not pull in markitdown/magika/onnxruntime.

        markitdown is imported lazily inside convert_document's Office-format
        branch so `import ultrakb` stays cheap and a packaged build can drop
        magika/onnxruntime. This runs in a fresh interpreter (a clean
        sys.modules) so the assertion is deterministic regardless of what other
        tests in this process already imported.
        """
        heavy = ("markitdown", "magika", "onnxruntime")
        code = (
            "import sys, ultrakb.converter; "
            f"print(','.join(m for m in {heavy!r} if m in sys.modules))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        loaded = proc.stdout.strip()
        assert loaded == "", f"import ultrakb.converter eagerly loaded: {loaded}"


def test_converter_rejects_a_public_target_before_any_input_or_storage_write(kb_dir):
    source = kb_dir / "input.md"
    source.write_text("A source that must remain untouched.")
    before = {
        path.relative_to(kb_dir): path.read_bytes() for path in kb_dir.rglob("*") if path.is_file()
    }
    with pytest.raises(ValueError, match="Private conversion"):
        convert_document(source, kb_dir)
    assert {
        path.relative_to(kb_dir): path.read_bytes() for path in kb_dir.rglob("*") if path.is_file()
    } == before
