"""Bounded identity decisions for every ambiguous link occurrence and candidate."""

import json

from ultrakb.agent.page_response import PageResponseError, load_protocol_json
from ultrakb.compile_checkpoints import digest
from ultrakb.llm_execution import check_model_stop


async def resolve_response_links(
    raw, registry, problems, *, model, bundle, call, fits, checkpoints, key, stage, options
):
    """Scan all matching identities; a candidate omitted from a group is still possible."""
    options = dict(options)
    options.setdefault("response_format", {"type": "json_object"})
    records = {target.path: target for target in registry.targets}
    resolutions = {}

    repair_message = {
        "role": "user",
        "content": "The link-resolution JSON was invalid or incomplete. Return exactly the "
        "requested resolutions schema and ID, using only the supplied frozen paths. "
        "Include one explicit complete decision. Return the complete JSON object only.",
    }

    async def decide(problem, candidates, round_number, group_number):
        from ultrakb.agent.compiler import TruncatedResponseError
        from ultrakb.compilation_report import report_compile_repair
        from ultrakb.execution_timing import phase_observation

        request = request_for(problem, candidates)
        signature = digest([request, options])
        group_key = f"{key}:links:{problem['id']}:{round_number}:{group_number}"
        link_stage = stage + ":links"
        result = checkpoints.load(group_key, signature) if checkpoints else None
        retained = (
            checkpoints.retained_response(
                group_key,
                signature,
                protocol="link-resolution-json",
                sections=(problem["id"],),
                stage=link_stage,
            )
            if checkpoints and result is None
            else None
        )
        if result is not None:
            validate(result, problem, candidates)
            return result["resolutions"][0]

        for attempt in range(2):
            received = None

            def capture(response):
                nonlocal received
                received = response.choices[0].message.content or ""
                if checkpoints:
                    checkpoints.record_received_response(
                        group_key,
                        signature,
                        received,
                        stage=link_stage,
                        response_protocol="link-resolution-json",
                        expected_sections=(problem["id"],),
                        code="page_truncated_response"
                        if response.choices[0].finish_reason == "length"
                        else "response_received",
                        attempt=attempt + 1,
                        response_format=options.get("response_format"),
                    )

            delivered = None
            try:
                if attempt == 0 and retained is not None:
                    delivered = retained[0]
                else:
                    if checkpoints:
                        if attempt:
                            checkpoints.claim_repair(group_key, signature)
                        else:
                            checkpoints.claim_generation(
                                group_key, signature, stage="compile." + link_stage
                            )
                    report_compile_repair()
                    delivered = await call(
                        model,
                        request + ([repair_message] if attempt else []),
                        link_stage,
                        bundle=bundle,
                        on_response=capture,
                        raise_on_truncation=True,
                        **options,
                    )
                with phase_observation("validation", stage=link_stage, work_id=group_key):
                    result = load_protocol_json(delivered)
                    validate(result, problem, candidates)
            except PageResponseError:
                raise
            except (ValueError, TypeError, KeyError, TruncatedResponseError) as error:
                raw_response = received if received is not None else delivered
                if checkpoints and raw_response is not None:
                    checkpoints.record_invalid_response(
                        group_key,
                        signature,
                        raw_response,
                        stage=link_stage,
                        response_protocol="link-resolution-json",
                        expected_sections=(problem["id"],),
                        code="page_truncated_response"
                        if isinstance(error, TruncatedResponseError)
                        else "page_link_invalid_resolution",
                        detail=str(error),
                        attempt=attempt + 1,
                    )
                if not attempt:
                    continue
                if checkpoints:
                    checkpoints.record_failure(group_key, signature, "page_link_invalid_resolution")
                raise PageResponseError("page_link_invalid_resolution", str(error)) from error
            if checkpoints:
                checkpoints.save(
                    group_key,
                    signature,
                    result,
                    metadata={
                        "protocol": "links-v2" if problem.get("allow_unmatched") else "links-v1",
                        "candidates": candidates,
                    },
                    reused_response=retained[1] if retained is not None and not attempt else None,
                )
            return result["resolutions"][0]

    def request_for(problem, candidates):
        if problem.get("allow_unmatched"):
            instruction = (
                "Match the unresolved link name and surrounding context against every supplied "
                "frozen page identity in this catalogue partition, including possible synonyms. "
                "Each partition must have an explicit conclusion. Omitted partitions remain "
                "possible. Never invent paths. Return exactly one JSON object with resolutions: "
                "[{id: supplied ID, status: matched|no_match|ambiguous, paths: [exact paths]}]. "
                "Use status=matched with exactly one path only when that identity fits the "
                "name and context. Use status=no_match with an empty paths array only when "
                "NONE of the supplied identities fits. Use status=ambiguous with at least two "
                "distinct supplied paths if the context cannot distinguish fitting identities. "
                "Page descriptions describe identity, not additional source facts."
            )
        else:
            instruction = (
                "Choose a path only if the supplied context identifies it unambiguously within "
                "this partition; otherwise return null. Never invent paths. "
                "Return exactly one JSON object: "
                '{"resolutions":[{"id":"the supplied ID","path":"exact path or null"}]}. '
            )
        return [
            {
                "role": "system",
                "content": "Resolve one wiki link identity from frozen page names and context. "
                "The surrounding generated prose is a draft, not evidence for additional facts. "
                "Candidates are one partition; omission never establishes nonexistence. "
                + instruction,
            },
            {
                "role": "user",
                "content": problem["code"]
                + ": frozen identity decision\n"
                + json.dumps(
                    {
                        "id": problem["id"],
                        "name": problem["name"],
                        "context": problem["context"],
                        "candidates": [
                            {
                                "path": path,
                                "names": records[path].names,
                                "type": records[path].entity_type,
                            }
                            | (
                                {"description": records[path].description}
                                if records[path].description
                                else {}
                            )
                            for path in candidates
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]

    def admitted(problem, candidates):
        request = request_for(problem, candidates)
        return fits(request, options) and fits(request + [repair_message], options)

    def validate(result, problem, candidates):
        if (
            not isinstance(result, dict)
            or set(result) != {"resolutions"}
            or not isinstance(result["resolutions"], list)
            or len(result["resolutions"]) != 1
        ):
            raise ValueError("Every requested link work ID requires exactly one resolution")
        value = result["resolutions"][0]
        if problem.get("allow_unmatched"):
            if (
                not isinstance(value, dict)
                or set(value) != {"id", "status", "paths"}
                or value["id"] != problem["id"]
                or value["status"] not in {"matched", "no_match", "ambiguous"}
                or not isinstance(value["paths"], list)
                or any(path not in candidates for path in value["paths"])
                or len(value["paths"]) != len(set(value["paths"]))
                or (value["status"] == "matched" and len(value["paths"]) != 1)
                or (value["status"] == "no_match" and value["paths"])
                or (value["status"] == "ambiguous" and len(value["paths"]) < 2)
            ):
                raise ValueError(
                    "Every catalogue group requires a complete, consistent identity conclusion"
                )
            return
        if (
            not isinstance(value, dict)
            or set(value) != {"id", "path"}
            or value["id"] != problem["id"]
            or (value["path"] is not None and value["path"] not in candidates)
        ):
            raise ValueError("Resolution ID and path must belong to the frozen candidate group")

    from ultrakb.compilation_report import report_compile_work

    for ordinal, problem in enumerate(problems):
        report_compile_work("links", completed=ordinal, pending=len(problems) - ordinal)
        check_model_stop()
        candidates = problem["targets"]
        for round_number in range(len(candidates)):
            groups, packed = [], []
            for path in candidates:
                if not admitted(problem, [path]):
                    raise PageResponseError(
                        "page_link_indivisible_identity",
                        f"Link {problem['id']} and candidate {path} cannot fit; originals retained",
                    )
                if packed and not admitted(problem, [*packed, path]):
                    groups.append(packed)
                    packed = []
                packed.append(path)
            if packed:
                groups.append(packed)
            selected, ambiguous = [], False
            for number, group in enumerate(groups):
                check_model_stop()
                report_compile_work(
                    "link_catalogue", completed=number, pending=len(groups) - number
                )
                decision = await decide(problem, group, round_number, number)
                if problem.get("allow_unmatched"):
                    ambiguous = ambiguous or decision["status"] == "ambiguous"
                    selected.extend(decision["paths"])
                elif decision["path"] is not None:
                    selected.append(decision["path"])
            report_compile_work("link_catalogue", completed=len(groups), pending=0)
            if ambiguous:
                raise PageResponseError(
                    "page_link_ambiguous",
                    "Catalogue matching retained ambiguous identities",
                    field_errors=(problem,),
                )
            if not selected and problem.get("allow_unmatched") and round_number == 0:
                resolutions[problem["id"]] = None
                break
            if len(selected) == 1:
                resolutions[problem["id"]] = selected[0]
                break
            if not selected or len(selected) >= len(candidates):
                raise PageResponseError(
                    "page_link_ambiguous",
                    f"Link {problem['id']} remains ambiguous after all candidate groups; "
                    "page retained",
                    field_errors=(problem,),
                )
            candidates = selected
        else:
            raise PageResponseError("page_link_ambiguous", "Identity resolution made no progress")
    report_compile_work("links", completed=len(problems), pending=0)
    return registry.response(raw, resolutions=resolutions)


def retained_link_response(checkpoints, key, signature, *, stage, registry):
    """Resume incomplete link work from its authenticated page response without regeneration."""
    if not checkpoints or not registry:
        return None
    captured = checkpoints.retained_response(
        key, signature, protocol="page-json", sections=(), stage=stage
    )
    if captured is not None:
        raw, _ = captured
        try:
            registry.response(raw)
        except PageResponseError as error:
            if error.code in {"page_link_ambiguous", "page_link_unresolved"}:
                return raw
    return None


def request_admitted(sizer, messages, options):
    """The production sizer receives exact transport options; custom probes keep their seam."""
    from ultrakb.agent.compile_capacity import CompileCapacity

    if isinstance(sizer, CompileCapacity):
        return sizer(
            messages, {key: value for key, value in options.items() if key != "prompt_version"}
        )
    return sizer(messages)
