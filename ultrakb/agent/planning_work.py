"""Bounded, persisted JSON work for partition planning and identity decisions."""

import json
from collections.abc import Callable

from ultrakb.agent.compile_plan import CompilePlanError
from ultrakb.compile_checkpoints import digest
from ultrakb.model_tokenizer import ContextCapacityError

PROTOCOL = "planning-partitions-v1"


def json_message(label: str, value: object) -> dict:
    return {"role": "user", "content": label + "\n" + json.dumps(value, ensure_ascii=False)}


def request_json_work(
    messages: list[dict],
    *,
    key: str,
    stage: str,
    send: Callable,
    fits: Callable,
    validate: Callable,
    checkpoints=None,
    identity=None,
):
    """Persist each accepted unit; repair only that unit with a concise diagnostic."""
    signature = digest([PROTOCOL, messages, identity])
    key = f"partition:{key}"
    correction: list[dict] = []
    if checkpoints:
        cached = checkpoints.load(key, signature)
        if cached is not None:
            try:
                return validate(json.loads(cached))
            except (ValueError, TypeError, KeyError) as error:
                raise CompilePlanError(str(error), "compile_partition_invalid") from error
        retained = checkpoints.retained_response(
            key, signature, protocol=PROTOCOL, sections=(), stage=f"compile.{stage}"
        )
        if retained:
            raw, receipt = retained
            try:
                value = validate(json.loads(raw))
            except (ValueError, TypeError, KeyError) as error:
                correction = [
                    json_message(
                        "Repair only this planning work. Return complete JSON.",
                        {"code": "compile_partition_invalid", "issue": str(error)},
                    )
                ]
            else:
                checkpoints.save(key, signature, raw, reused_response=receipt)
                return value
        checkpoints.require_generation(key, signature)
    for attempt in range(1 if correction else 0, 2):
        request = [*messages, *correction]
        if not fits(request):
            raise ContextCapacityError(f"Planning work {key} requires a smaller partition")
        if attempt and checkpoints:
            checkpoints.claim_repair(key, signature)
        elif checkpoints:
            checkpoints.claim_generation(key, signature, stage="compile." + stage)
        raw = send(request, stage)
        if checkpoints:
            checkpoints.record_received_response(
                key,
                signature,
                raw,
                stage=f"compile.{stage}",
                response_protocol=PROTOCOL,
                expected_sections=(),
                code="response_received",
                attempt=attempt,
            )
        try:
            value = validate(json.loads(raw))
        except (ValueError, TypeError, KeyError) as error:
            if checkpoints:
                checkpoints.record_invalid_response(
                    key,
                    signature,
                    raw,
                    stage=f"compile.{stage}",
                    response_protocol=PROTOCOL,
                    expected_sections=(),
                    code="compile_partition_invalid",
                    detail=str(error),
                    attempt=attempt,
                )
            if attempt:
                if checkpoints:
                    checkpoints.record_failure(key, signature, "compile_partition_invalid")
                raise CompilePlanError(str(error), "compile_partition_invalid") from error
            correction = [
                json_message(
                    "Repair only this planning work. Return its complete JSON result.",
                    {"code": "compile_partition_invalid", "issue": str(error)},
                )
            ]
        else:
            if checkpoints:
                checkpoints.save(key, signature, raw)
            return value
    raise AssertionError("Unreachable planning repair")
