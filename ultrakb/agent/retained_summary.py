"""Summary-specific decoding over the shared authenticated response replay."""

from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.page_response import parse_summary
from ultrakb.agent.retained_delivery import reuse_saved_response
from ultrakb.agent.source_excerpts import SourceExcerpts


def reuse_saved_summary(
    checkpoints, key, request_digest, batch, *, protocol: str, section_ranges: list[dict]
) -> dict | None:
    sections = tuple(section["id"] for section in section_ranges)
    excerpts = SourceExcerpts([batch, *batch["supplementary_context"]])
    return reuse_saved_response(
        checkpoints,
        key,
        request_digest,
        protocol=protocol,
        sections=sections,
        stage=key.replace("summary:", "document-summary:", 1),
        validate=lambda raw: validate_page_delivery(
            raw,
            response_protocol=protocol,
            expected_sections=sections,
            original_excerpts=excerpts,
        ),
        decode=lambda raw, delivered: parse_summary(
            raw if protocol == "summary-parts-json" else delivered, sections
        ),
        excerpts=excerpts,
        metadata={**batch, "output_sections": section_ranges},
    )
