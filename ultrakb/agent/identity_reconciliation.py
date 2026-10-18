"""Capacity-bounded reasoning reduction; the complete receipts stay in checkpoints."""

from ultrakb.agent.planning_capacity import capacity_error
from ultrakb.agent.planning_work import PROTOCOL, json_message, request_json_work
from ultrakb.compile_checkpoints import digest
from ultrakb.model_tokenizer import ContextCapacityError


def fit_identity_decisions(
    entry, *, candidate, messages, system, fits, send, checkpoints, identity, force=False
):
    """Reduce explanations, never votes, IDs, actions or source reading selections.

    A binary description split produces two receipts, but either explanation can
    itself exceed the next request. Reduce all its parts, with strict shrinking
    output contracts, before attempting the semantic decision over both receipts.
    """

    def compact(reason, limit):
        request = [
            system,
            json_message(
                "Compact identity reasoning",
                {
                    "reason": reason,
                    "candidate": {
                        k: v
                        for k, v in candidate.items()
                        if k not in {"reading_locations", "evidence_units", "contributions"}
                    },
                    "target": {
                        k: v
                        for k, v in entry.items()
                        if k not in {"description", "fragment_decisions"}
                    },
                    "max_characters": limit,
                    "instruction": "Return JSON with only a nonempty reason string within the "
                    "character limit. Preserve every identity distinction, scope limitation and "
                    "counterexample. This is reasoning about finding aids, not source evidence. "
                    "Do not select a relation or discard a conflicting decision.",
                },
            ),
        ]

        def validate(value):
            if (
                not isinstance(value, dict)
                or set(value) != {"reason"}
                or not isinstance(value["reason"], str)
                or not value["reason"].strip()
                or len(value["reason"]) > limit
            ):
                raise ValueError(f"Return only a nonempty reason of at most {limit} characters")
            return value["reason"]

        key = "identity-reason:" + digest(request)
        split_signature = digest([PROTOCOL, request, identity])
        split = checkpoints.load(key + ":split", split_signature) if checkpoints else None
        if not split and fits(request):
            try:
                return request_json_work(
                    request,
                    key=key,
                    stage="concepts-plan-identity",
                    fits=fits,
                    send=send,
                    validate=validate,
                    checkpoints=checkpoints,
                    identity=identity,
                )
            except ContextCapacityError:
                if checkpoints:
                    checkpoints.save(key + ":split", split_signature, {"split": True})
        if len(reason) < 2 or limit < 2:
            raise capacity_error(
                "The minimum identity-reason reduction cannot fit; "
                "all fragment decisions are retained.",
                fits,
            )
        middle = len(reason) // 2
        # Each child contributes to the result; no prefix/suffix is dropped.
        half_limit = max(1, (limit - 1) // 2)
        return "\n".join(compact(part, half_limit) for part in (reason[:middle], reason[middle:]))

    decisions = entry["fragment_decisions"]
    while force or not fits(messages({**entry, "fragment_decisions": decisions})):
        force = False
        longest = max(len(receipt["reason"]) for receipt in decisions)
        if longest < 2:
            raise capacity_error(
                "The minimum identity receipts cannot fit; "
                "all catalog fragments and decisions are retained.",
                fits,
            )
        limit = max(1, longest // 2)
        decisions = [
            {**receipt, "reason": compact(receipt["reason"], limit)}
            if len(receipt["reason"]) > limit
            else receipt
            for receipt in decisions
        ]
    return {**entry, "fragment_decisions": decisions}
