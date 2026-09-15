"""Recover complete planning windows without shifting them when topics are added."""

from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.evidence import ParseStore
from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object, valid_id

PREVIOUS_PLANNERS = {
    "c754adf22420b349d98114a88901cc1df8305e4929f2c503acab5af270189fe6",
    "fc602bbd9c064cc294ddffd20c1d54ee278eabee51a0af32e739b1594c422d87",
}


def completed_windows(checkpoints, system, topics, payload, validate):
    """Retain complete candidate groups whose source-topic members still exist.

    Recovered windows remain candidates for normal cross-window reconciliation.
    They are never adopted as a settled plan or a publication decision.
    Validate the full original response and its original request contract first;
    changed schema, model or navigation hints invalidate that window. An absent
    member invalidates its whole group, not other completed groups in the window.
    """
    parser = ParseStore(checkpoints.store.kb_dir)
    current = parser.load(checkpoints.input["parse"])
    parses = {current.id: True}
    available = set(topics)
    for key in checkpoints.checkpoint_keys("planning"):
        processing_checkpoint("planning")
        try:
            record = read_object(
                checkpoints.store.owned_path(checkpoints.root / f"{valid_id(key)}.json")
            )
            before, contract = record.get("input"), record.get("contract")
            if not isinstance(before, dict) or not isinstance(contract, dict):
                continue
            if {**before, "parse": current.id} != checkpoints.input:
                continue
            request = contract.get("payload")
            if not isinstance(request, dict) or request.get("stage") != "planning":
                continue
            members = request.get("topics")
            if (
                "mode" in request
                or not isinstance(members, list)
                or not members
                or any(not isinstance(member, str) for member in members)
                or len(set(members)) != len(members)
                or not set(members) & available
                or members != sorted(members)
                or record.get("key") != key
                or contract.get("input") != before
                or content_id(contract) != key
                or content_id(record.get("value")) != record.get("value_digest")
            ):
                continue
            expected_payload = payload(members)
            dependencies = {
                "catalog_window": expected_payload["existing_pages"],
                "schema": expected_payload["schema"],
            }
            expected = checkpoints._key_record(system, expected_payload, dependencies=dependencies)
            expected["input"] = before
            revisions = contract.get("stage_implementation", {})
            if isinstance(revisions, dict) and revisions.get("evidence_plan") in PREVIOUS_PLANNERS:
                expected["stage_implementation"]["evidence_plan"] = revisions["evidence_plan"]
                expected["stage_implementation"].pop("planning_resume")
            if expected != contract:
                continue
            previous = valid_id(before["parse"])
            if previous not in parses:
                parsed = parser.load(previous)
                parses[previous] = (
                    parsed.input_key == current.input_key and parsed.profile == current.profile
                )
            if not parses[previous]:
                continue
            groups = [
                group
                for group in validate(record["value"], members)
                if set(group["members"]) <= available
            ]
            members = sorted(member for group in groups for member in group["members"])
            if not members:
                continue
            available.difference_update(members)
            yield members, groups
        except (ValueError, KeyError, TypeError, FileNotFoundError, ResponseIncomplete):
            continue
