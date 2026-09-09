"""Small valid Office inputs without an extra Office-writing dependency."""

from pathlib import Path
from zipfile import ZipFile


def write_docx(path: Path, body: str, *, footnotes: str | None = None) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels"
    ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>""".replace(
                "</Types>",
                '<Override PartName="/word/footnotes.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.footnotes+xml"/></Types>'
                if footnotes
                else "</Types>",
            ),
        )
        archive.writestr(
            "_rels/.rels",
            """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
    Target="styles.xml"/>
</Relationships>""".replace(
                "</Relationships>",
                '<Relationship Id="footnotes" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/footnotes" '
                'Target="footnotes.xml"/></Relationships>'
                if footnotes
                else "</Relationships>",
            ),
        )
        if footnotes:
            archive.writestr(
                "word/footnotes.xml",
                '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                + footnotes
                + "</w:footnotes>",
            )
        archive.writestr(
            "word/styles.xml",
            """
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
</w:styles>""",
        )
        archive.writestr(
            "word/document.xml",
            f"""
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body}<w:sectPr/></w:body>
</w:document>""",
        )
