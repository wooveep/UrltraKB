"""Small, parseable examples for the planning response contracts.

Example identities are illustrative and never grant permissions for a live request.
"""

JSON_RULES = """Return exactly one complete JSON object matching the current output contract.
Do not return an empty or whitespace-only response, null, a top-level array,
Markdown fences, prose, or the API response_format object. Include every required
field. Empty arrays are allowed only where this contract permits them; they do not
mean pending work is complete. Use only identities and permissions supplied in the
actual request. Format examples are illustrative, not source evidence or permission.
Represent uncertainty using this task's allowed decisions or fields.
"""

PLAN_EXAMPLE = """{"overview":{"text":"An example procedure and its scope.",
"ranges":[{"from_block":"@e:examplea","through_block":"@e:examplea"}],
"limitations":[]},"page_changes":[{"local_key":"example_page","kind":"concept",
"title":"Example procedure","purpose":"Explain the supplied procedure.",
"subject_ranges":[{"from_block":"@e:examplea","through_block":"@e:examplea"}],
"necessary_context":[],"limitations":[]}],"source_only":[],"unresolved":[],
"resolutions":[],"external_references":[]}"""

PATCH_EXAMPLE = """{"repair_protocol":"document-plan-repair-v2",
"candidate_hash":"example-candidate-hash","operations":[{"issue_id":"example-issue-id",
"op":"remove_field","item_ref":"item:example","field":"type"}]}"""

ROUTING_EXAMPLE = """{"repair_protocol":"document-plan-repair-v3",
"candidate_hash":"example-candidate-hash","decisions":[{
"decision_id":"repair:example","decision":"route_source_only",
"retained_pieces":[],"reason":"The supplied pieces belong to the procedure body."}]}"""

REFERENCE_EXAMPLE = """{"check_protocol":"document-reference-check-v3",
"decisions":[{"reference_key":"reference:example","page_ref":"example_page",
"decision":"required_internal","reason":"The instruction requires preparation.",
"target_ranges":[{"from_block":"@e:exampleb","through_block":"@e:exampleb"}]}]}"""


def example_rules(example: str) -> str:
    """Attach one complete format example to the current response mode."""
    return f"\n{JSON_RULES}\nComplete format example (illustrative identities only):\n{example}\n"


def repair_task_rules(mode: str) -> str:
    """Return only the active repair contract and its complete example."""
    if mode == "syntax_repair":
        return (
            "Correct JSON syntax only in the nonempty rejected wire candidate. Preserve "
            "wire identities, field names, scalar values and array order. Return the complete "
            "corrected DocumentPlan." + example_rules(PLAN_EXAMPLE)
        )
    if mode == "routing_repair":
        return (
            "Choose one authorized route for each pending decision_id. Copy only supplied "
            "decision_id, piece_ref and page_ref values. Never return ranges, block IDs or a "
            "DocumentPlan. The program applies all decisions to one candidate atomically. "
            "An empty retained_pieces list can be valid; an empty decisions list cannot finish "
            "pending items." + example_rules(ROUTING_EXAMPLE)
        )
    if mode == "field_repair":
        return (
            "Repair the rejected candidate using repair_request.allowed_operations and "
            "repair_request.response_contract. Return only the repair patch, never a full "
            "DocumentPlan. Candidate fields illustrate value structure, not permission. "
            "remove_field forbids value; replace_field and append_item require value. "
            "operations=[] means no progress." + example_rules(PATCH_EXAMPLE)
        )
    raise ValueError(f"Unknown repair mode: {mode}")
