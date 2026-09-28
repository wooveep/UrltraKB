"""Legacy physical-page text and an explicit mapping to the immutable block parse."""

import re

import litellm

from openkb.evidence import Evidence, complete_read_bound
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, content_id


class PDFPages:
    def __init__(self, kb, source, parsed, model, reader):
        from pageindex.index.utils import get_page_tokens

        self.source, self.parsed = source, parsed
        # Use the same native extractor as the original PageIndex PDF path.
        self.values = get_page_tokens(str(SourceStore(kb).original(source)), model=model)
        self.orders, self.matches = {}, {}
        self.origins = ["pdf_native_text"] * len(self.values)
        for block in parsed.blocks:
            page = block.location.get("page")
            if type(page) is int and "attachment" not in block.location:
                self.orders.setdefault(page, []).append(block.order)
        for number, (text, _) in enumerate(self.values, 1):
            processing_checkpoint("index_pdf_read")
            # Scanned pages retain already parsed OCR; no extra recognition call.
            if not text.strip() and number in self.orders:
                text = "\n\n".join(
                    reader.read(
                        Evidence(source.source_id, source.id, parsed.id, parsed.blocks[i].id),
                        max_chars=complete_read_bound(parsed.blocks[i]),
                    ).text
                    for i in self.orders[number]
                    if parsed.blocks[i].kind != "image"
                )
                self.values[number - 1] = (text, litellm.token_counter(model=model, text=text))
                self.origins[number - 1] = "saved_parse_text"
            self.matches.setdefault(text, []).append(number)

    def numbers(self, original):
        tagged = sorted({int(n) for n in re.findall(r"<physical_index_(\d+)>", original)})
        return tagged or self.matches.get(original, [])

    def evidence(self, numbers):
        numbers = sorted(set(numbers))
        if any(not 1 <= n <= len(self.values) for n in numbers):
            raise ValueError("PDF evidence page is outside the saved source")
        text = "".join(
            f"<physical_index_{n}>\n{self.values[n - 1][0]}\n<physical_index_{n}>\n\n"
            for n in numbers
        )
        identity = {
            "version": self.source.id,
            "parse": self.parsed.id,
            "pages": numbers,
            "text": text,
        }
        return {
            "group_id": content_id(identity),
            "source_id": self.source.source_id,
            "version_id": self.source.id,
            "parse_id": self.parsed.id,
            "document": self.source.name,
            "blocks": [],
            "pdf": {
                "physical_pages": numbers,
                "text": text,
                "text_origins": [self.origins[n - 1] for n in numbers],
            },
        }

    def bounds(self, first, last):
        orders = [i for n in range(first, last + 1) for i in self.orders.get(n, [])]
        return (min(orders), max(orders) + 1) if orders else None

    def manifest(self, groups, reason=None):
        windows, start = [], 0
        for i, numbers in enumerate(groups):
            bounds = self.bounds(min(numbers), max(numbers)) if numbers else None
            if not bounds or bounds[1] <= start:
                continue
            end = len(self.parsed.blocks) if i == len(groups) - 1 else bounds[1]
            windows.append(
                {
                    "evidence": evidence_descriptor(
                        self.source, self.parsed, min(start, bounds[0]), end
                    ),
                    "target_start": start,
                    "target_end": end,
                    "status": "basic" if reason else "complete",
                    "reason": reason,
                    "target_tokens": 20000,
                    "pdf_pages": [min(numbers), max(numbers)],
                    "reading_protocol": "legacy_pdf_pages",
                }
            )
            start = end
        if start < len(self.parsed.blocks):
            windows.append(
                {
                    "evidence": evidence_descriptor(
                        self.source, self.parsed, start, len(self.parsed.blocks)
                    ),
                    "target_start": start,
                    "target_end": len(self.parsed.blocks),
                    "status": "basic" if reason else "complete",
                    "reason": reason,
                    "target_tokens": 20000,
                    "reading_protocol": "legacy_pdf_pages",
                }
            )
        return windows
