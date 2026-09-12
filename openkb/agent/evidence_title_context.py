"""Compact original evidence for a public title shared by separately verified parts."""

import json
import re

from openkb.agent.evidence_generation_protocol import source_mapping
from openkb.evidence import Evidence

TITLE_CONTEXT_SYSTEM = """
When title_context is supplied, it contains a selected window of original quotations
and headings from the WHOLE topic, including other parts. It is not an exhaustive inventory.
Use it ONLY to assess the shared public title, alongside the current original evidence.
The current body
must still be supported by its own evidence, source scopes and fragment bindings, and
cover its own required facts. Do not use a different part's quotation to validate a body
claim, move an operation to another source, or require unrelated facts in this part.
A neutral title listing separate sourced tasks does not make their actors or requirements
interchangeable. Do not reject a shared title merely because its other task is in another
part when title_context explicitly supplies that task's original evidence.
"""


def topic_title_context(facts, reader, *, title="", limits=None, model=None):
    """Reread complete original scopes with their own contextual evidence."""
    evidence = []
    for fact in facts:
        scope = Evidence(**fact["scope"])
        view = reader.read(scope, max_chars=max(4096, scope.end - scope.start))
        if view.next_start is not None:
            raise ValueError("Incomplete original title evidence")
        contextual = {}
        if view.context:
            contextual["context"] = view.context
        neighbors = []
        for item in fact.get("context_evidence", []):
            ref = Evidence(**item["reference"])
            neighbor = reader.read(ref, max_chars=max(4096, ref.end - ref.start))
            neighbors.append(
                {
                    "relation": item["relation"],
                    "text": neighbor.text,
                    "location": neighbor.location,
                    "context": neighbor.context,
                }
            )
        if view.text.strip() in {"}", "};"}:
            from openkb.evidence_context import enclosing_code

            neighbors.extend(
                {k: v for k, v in n.items() if k != "reference"}
                for n in enclosing_code(reader, scope)
            )
        if neighbors:
            contextual["neighbors"] = neighbors
        evidence.append(
            {
                "id": fact["id"],
                "text": view.text,
                "location": view.location,
                "reference": fact["scope"],
                **contextual,
            }
        )
    mapping = source_mapping(evidence)
    scopes = [
        {"id": "t" + scope["id"][1:], "origin": scope["origin"], "headings": scope["headings"]}
        for scope in mapping["source_scopes"]
    ]
    passages, seen = [], set()
    for item, occurrence in zip(evidence, mapping["occurrences"]):
        scope = "t" + occurrence["scope"][1:]
        passage = {
            "scope": scope,
            "text": item["text"],
            **{k: item[k] for k in ("context", "neighbors") if k in item},
        }
        key = json.dumps(passage, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            passages.append(passage)
            seen.add(key)
    context = {"scopes": scopes, "passages": passages}
    if limits is None:
        return context
    return _bounded(
        context,
        title,
        model,
        # Keep this auxiliary retrieval below one twelfth of the input budget;
        # generation, correction and review must still fit intact body quotes.
        max(1, min(4096, (limits.context_tokens - limits.output_tokens) // 12)),
    )


def _bounded(context, title, model, allowance):
    """Select complete quotes within a small auxiliary budget; never clip a claim."""
    import litellm

    def tokens(value):
        return litellm.token_counter(
            model=model, text=json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )

    if tokens(context) <= allowance:
        return context
    terms = set(re.findall(r"[a-zA-Z0-9_]+", title.casefold()))
    terms.update(
        title[i : i + 2].casefold()
        for i in range(len(title) - 1)
        if all("一" <= c <= "鿿" for c in title[i : i + 2])
    )
    scopes = {s["id"]: s for s in context["scopes"]}
    ranked = sorted(
        context["passages"],
        key=lambda p: -sum(
            t in (p["text"] + str(scopes[p["scope"]]["headings"])).casefold() for t in terms
        ),
    )
    first, rest, represented = [], [], set()
    for passage in ranked:
        (rest if passage["scope"] in represented else first).append(passage)
        represented.add(passage["scope"])
    chosen, selected_scopes = [], set()
    used = tokens({"scopes": [], "passages": []}) + 8
    scope_costs = {key: tokens(value) + 8 for key, value in scopes.items()}
    for passage in [*first, *rest]:
        cost = tokens(passage) + 8
        if passage["scope"] not in selected_scopes:
            cost += scope_costs[passage["scope"]]
        if used + cost <= allowance:
            chosen.append(passage)
            selected_scopes.add(passage["scope"])
            used += cost
    # A heading remains useful title evidence even when its quote is too long.
    for scope in context["scopes"]:
        if (
            scope["headings"]
            and scope["id"] not in selected_scopes
            and used + scope_costs[scope["id"]] <= allowance
        ):
            selected_scopes.add(scope["id"])
            used += scope_costs[scope["id"]]
    result = {
        "scopes": [s for s in context["scopes"] if s["id"] in selected_scopes],
        "passages": chosen,
    }
    if tokens(result) > allowance:
        raise ValueError("Title context exceeds its auxiliary budget")
    return result if selected_scopes or chosen else None
