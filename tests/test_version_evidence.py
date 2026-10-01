"""Cover candidates retain visible evidence and leave ordinary prose unknown."""

import pytest

from openkb.version_evidence import pdf_title_candidates


@pytest.mark.parametrize(
    "cover,product,family",
    [
        (
            "CNware WinSphere V9.4.0\n产品白皮书\n云宏服务器虚拟化产品\n日期：2026-4\n版本：V9.4.0",
            "CNware WinSphere",
            "white paper",
        ),
        (
            "运维手册\nCNware WinStack 虚拟化云平台\n日期：2026-05-15\n版本：V9.4.0",
            "CNware WinStack 虚拟化云平台",
            "operations",
        ),
        (
            "CNware-WinStack-V9.4.0-安装手册\n版权所有©云宏信息科技股份有限公司\n第1页\nCNware-WinStack-V9.4.0\n安装手册",
            "CNware-WinStack",
            "installation",
        ),
        ("CNware WinStack V9.4.0 最佳实践\n日期：2024-12-26", "CNware WinStack", "best practices"),
    ],
)
def test_visible_multiline_cover_identifies_version_without_filename(
    tmp_path, cover, product, family
):
    import pymupdf

    path = tmp_path / "unrelated-name.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox((40, 40, 550, 400), cover, fontname="china-s")
        pdf.save(path)
    candidates = pdf_title_candidates(path)
    assert {c.values for c in candidates if c.field == "product"} == {(product,)}
    assert {c.values for c in candidates if c.field == "family"} == {(family,)}
    assert {c.values for c in candidates if c.field == "applicable_versions"} == {("9.4.0",)}
    assert all(c.excerpt and c.location.startswith("pdf.page[1]") for c in candidates)


def test_bottom_cover_keeps_version_and_revision_evidence_separate(tmp_path):
    import pymupdf

    path = tmp_path / "unrelated.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(
            (40, 650, 550, 825),
            "运维手册\nCNware WinStack 虚拟化云平台\n版本：V9.4.0\n文档修订：R2",
            fontname="china-s",
        )
        pdf.save(path)
    candidates = pdf_title_candidates(path)
    assert {c.values for c in candidates if c.field == "applicable_versions"} == {("9.4.0",)}
    assert {c.values for c in candidates if c.field == "document_revision"} == {("R2",)}


def test_cover_conflicts_remain_candidates_and_prose_is_not_a_family(tmp_path):
    import pymupdf

    path = tmp_path / "conflict.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(
            (40, 40, 550, 400),
            "CNware WinStack V9.4.0 最佳实践\n版本：V9.5.0\n文档修订：R3",
            fontname="china-s",
        )
        pdf.save(path)
    candidates = pdf_title_candidates(path)
    assert {c.values for c in candidates if c.field == "applicable_versions"} == {
        ("9.4.0",),
        ("9.5.0",),
    }
    other = tmp_path / "prose.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox(
            (40, 40, 550, 400),
            "User feedback is considered\nSome Product\nVersion: V9.4.0",
        )
        pdf.save(other)
    assert not pdf_title_candidates(other)
