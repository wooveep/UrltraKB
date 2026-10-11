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


def test_equivalent_type_echo_does_not_create_a_page_metadata_conflict():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate(
        "---\ntype: Work\n---\n# Manual\n\nOld rule.\n",
        allowed_types=frozenset({"work", "other"}),
    )
    response = {
        "base_sha256": update.digest,
        "type": "work",
        "edits": [{"block_id": "b2", "operation": "replace", "content": "New rule.\n"}],
    }
    merged = merge_proposals(update, [UpdateProposal(0, response)])
    assert not merged.conflicts
    assert "type" not in merged.response
    assert update.apply(json.dumps(merged.response), current=update.original)["content"].endswith(
        "New rule.\n"
    )
    assert response["type"] == "work"  # Saved model response remains the audit evidence.


def test_same_type_change_in_different_case_merges_without_an_extra_request():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate(
        "---\ntype: Work\n---\n# Manual\n\nOld rule.\n",
        allowed_types=frozenset({"work", "other"}),
    )
    proposals = [
        UpdateProposal(
            order,
            {"base_sha256": update.digest, "type": value, "edits": []},
            complete_scope=True,
        )
        for order, value in enumerate(["Other", "other"])
    ]
    merged = merge_proposals(update, proposals)
    assert not merged.conflicts
    assert merged.response["type"] == "other"
    assert merged == merge_proposals(update, list(reversed(proposals)))


def test_conflict_resolves_all_edits_sharing_a_range_declaration():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate(
        "# Device\n\nInstall.\n\nMaintenance.\n", source_ranges=("install-a", "install-b", "care")
    )
    first = {
        "base_sha256": update.digest,
        "source_ranges": ["install-a", "care"],
        "edits": [
            {"block_id": "b2", "operation": "replace", "content": "Install A."},
            {"block_id": "b3", "operation": "replace", "content": "Maintenance details."},
        ],
    }
    second = {
        "base_sha256": update.digest,
        "source_ranges": ["install-b"],
        "edits": [{"block_id": "b2", "operation": "replace", "content": "Install B."}],
    }
    merged = merge_proposals(update, [UpdateProposal(0, first), UpdateProposal(1, second)])
    assert merged.response["source_ranges"] == []
    assert merged.response["edits"] == []
    assert len(merged.conflicts) == 1
    assert merged.conflicts[0].blocks == ("b2", "b3")
    assert merged.conflicts[0].batches == (0, 1)


def test_unrelated_range_conflicts_keep_separate_bounded_resolution_requests():
    from openkb.agent.update_proposals import UpdateProposal, merge_proposals

    update = PageUpdate("# Device\n\nInstall.\n\nCare.\n", source_ranges=("r0", "r1", "r2", "r3"))
    proposals = [
        UpdateProposal(
            i,
            {
                "base_sha256": update.digest,
                "source_ranges": [f"r{i}"],
                "edits": [
                    {
                        "block_id": "b2" if i < 2 else "b3",
                        "operation": "replace",
                        "content": f"Choice {i}",
                    }
                ],
            },
        )
        for i in range(4)
    ]
    merged = merge_proposals(update, proposals)
    assert [(c.blocks, c.batches) for c in merged.conflicts] == [
        (("b2",), (0, 1)),
        (("b3",), (2, 3)),
    ]
