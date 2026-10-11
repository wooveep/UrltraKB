"""All batches edit the same base; completion order cannot choose the winning facts."""

import json

from openkb.agent.page_updates import PageUpdate


def test_disjoint_and_identical_proposals_merge_in_source_order():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate(
        "# Device\n\nWinter rule.\n\nSummer rule.\n", source_ranges=("winter", "summer")
    )
    a = {
        "base_sha256": update.digest,
        "source_ranges": ["winter"],
        "edits": [
            {"block_id": "b2", "operation": "replace", "content": "Winter rule plus exception.\n"}
        ],
    }
    b = {
        "base_sha256": update.digest,
        "source_ranges": ["summer"],
        "edits": [
            {"block_id": "b3", "operation": "insert_after", "content": "\nNew summer condition.\n"}
        ],
    }
    proposals = [UpdateProposal(0, a), UpdateProposal(1, b), UpdateProposal(2, a)]
    first = merge_proposals(update, proposals)
    second = merge_proposals(update, list(reversed(proposals)))
    assert first == second and not first.conflicts
    result = update.apply(json.dumps(first.response), current=update.original)
    assert result["source_ranges"] == ["summer", "winter"]
    assert result["content"] == (
        "# Device\n\nWinter rule plus exception.\n\nSummer rule.\n\nNew summer condition.\n"
    )


def test_overlapping_replacements_and_page_metadata_need_explicit_resolution():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate("---\ndescription: Old\ntype: product\n---\n# Device\n\nRule.\n")

    def proposal(order, content, description, etype):
        return UpdateProposal(
            order,
            {
                "base_sha256": update.digest,
                "description": description,
                "type": etype,
                "edits": [{"block_id": "b2", "operation": "replace", "content": content}],
            },
        )

    proposals = [
        proposal(0, "New A", "Scope A", "product"),
        proposal(1, "New B", "Scope B", "other"),
    ]
    merged = merge_proposals(update, proposals)
    assert merged == merge_proposals(update, list(reversed(proposals)))
    assert {b for c in merged.conflicts for b in c.blocks} == {"b2"}
    assert any(c.metadata for c in merged.conflicts)
    assert merged.response["edits"] == []
    assert merged.response.get("description", "Old") == "Old"


def test_one_local_metadata_candidate_still_needs_whole_page_context():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate("---\ndescription: Old scope\n---\n# Device\n\nRule.\n")
    response = {"base_sha256": update.digest, "description": "Only local scope", "edits": []}
    assert merge_proposals(update, [UpdateProposal(0, response)]).conflicts
    complete = merge_proposals(update, [UpdateProposal(0, response, complete_scope=True)])
    assert not complete.conflicts and complete.response["description"] == "Only local scope"
