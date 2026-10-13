"""PageIndex adapter for OpenKB's shared native PDF page extraction."""

from pathlib import Path

from pageindex.backend.input_package import IMAGE_REFERENCE_POLICY
from pageindex.parser.protocol import ContentNode, ParsedDocument
from pageindex.tokens import count_tokens

from openkb.images import PDF_IMAGE_POLICY, convert_pdf_to_pages
from openkb.pdf_layout import PDF_LAYOUT_POLICY
from openkb.source_contract import NATIVE_PAGE_POLICY


class NativePdfParser:
    """Keep tables and figure order identical in indexing and source reading."""

    previous_policy = (
        f"openkb-native-pdf-v1:{PDF_LAYOUT_POLICY}:{PDF_IMAGE_POLICY}:{IMAGE_REFERENCE_POLICY}"
    )
    policy = f"{previous_policy}:{NATIVE_PAGE_POLICY}"

    def __init__(self, *, legacy=False):
        self.legacy = legacy
        self.policy = self.previous_policy if legacy else type(self).policy

    def supported_extensions(self) -> list[str]:
        return [".pdf"]

    def parse(self, file_path: str, **kwargs) -> ParsedDocument:
        path = Path(file_path)
        name = kwargs.get("doc_name", path.stem)
        images_dir = Path(kwargs["images_dir"])
        model = kwargs.get("model")
        pages = convert_pdf_to_pages(path, name, images_dir, allow_empty_text=True)
        nodes = []
        for page in pages:
            content = page["content"]
            images = []
            for image in page["images"]:
                managed = str((images_dir / Path(image["path"]).name).resolve())
                images.append({**image, "path": managed, "content_reference": image["path"]})
            nodes.append(
                ContentNode(
                    content=content,
                    tokens=count_tokens(content, model=model),
                    index=page["page"],
                    images=images or None,
                    metadata={
                        "unit_kind": "page",
                        "printed_page_label": page["printed_page_label"],
                        "image_reference_spans": page["image_reference_spans"],
                        **(
                            {key: page[key] for key in ("source_structure_policy", "content_spans")}
                            if not self.legacy
                            else {}
                        ),
                    },
                )
            )
        return ParsedDocument(
            doc_name=name,
            nodes=nodes,
            metadata={
                "unit_kind": "page",
                "unit_count": len(nodes),
                "coverage": "complete",
                "image_reference_policy": IMAGE_REFERENCE_POLICY,
            },
        )
