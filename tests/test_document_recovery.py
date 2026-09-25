"""Publication inheritance requires matching page generation inputs."""

from copy import deepcopy

import pytest

from openkb.agent.document_plan import DocumentPlan, PagePlan, to_dict
from openkb.agent.document_plan_annotations import ExternalReference, PageLimitation
from openkb.agent.document_recovery import inherit_publication_state


@pytest.mark.parametrize("change", ["none", "limitation", "reference", "title"])
def test_publication_state_inheritance_checks_generation_inputs(change):
    page = PagePlan(
        key="p1", kind="concept", name="concepts/first", title="First",
        purpose="Explain first", subject_ranges=[[0, 1]], quality="published",
        review_receipt={"accepted": True},
    )
    previous = DocumentPlan(
        metadata={
            "recovery_key": "recovery",
            "publication_receipt": {"pages": ["concepts/first.md"]},
        },
        pages=[page],
    )
    final = deepcopy(previous)
    final.pages[0].quality = "planned"
    final.pages[0].review_receipt = None
    if change == "limitation":
        final.pages[0].limitations.append(
            PageLimitation([[0, 1]], "Source caveat", "Original wording")
        )
    elif change == "reference":
        final.external_references.append(
            ExternalReference("external:1", [[0, 1]], "See manual", "Manual", None, ["p1"])
        )
    elif change == "title":
        final.pages[0].title = "Changed title"

    inherit_publication_state(final, to_dict(previous))

    assert (final.pages[0].quality == "published") is (change == "none")
    assert (final.pages[0].review_receipt is not None) is (change == "none")
