"""Evidence rules shared by query/chat and deterministic version-claim checks."""

from openkb.agent.version_claims import unsupported_versions

ANSWER_EVIDENCE_RULES = """
Treat the user's requested output as a contract: include only requested categories
and preserve the requested format and level of detail. Do not broaden a lookup
into operational instructions or adjacent advice. Before finishing, compare each
answer section to that contract. State an unresolved requested value as a gap;
do not infer it from an incidental example or an unrelated operation.
For a question about one subject, omit supplements about other configuration
subjects unless the user explicitly requests comparison. A shared value does
not establish a shared rule or subject. Do not synthesize an extra "common default".

For every configuration or conditional conclusion, retain four separate facts:
subject, configuration item, condition, original source locator. Rules about
different subjects remain distinct even when they share terminology or values.
Never transfer a condition/default between subjects. For multi-source rules,
use record_source_fact with an exact source quote before synthesizing the answer.
After a failed fact check, correct that fact at most once; if still unverified,
omit it or state the gap. Never describe a failed fact check as verified.
In the final answer, give each requested rule its own row: subject, setting/value,
condition, source. Use the subject and condition from THAT rule's quote. Do not
add a combined conclusion that merges independently scoped rules. Shared fields,
values or resources do not make their conditions interchangeable.
Exclude facts about other subjects; several documents in a question do
not authorize changing its target subject. An additional rule must independently
answer the user's question before it can be included.
Keep the answer short: normally one compact table covering the requested facts,
then only essential gaps. Do not add a second explanation of the same table,
unrequested missing fields or checks of neighboring topics. An empty condition
means not yet extracted; write "the cited rule gives no further condition" only
after reading that rule, never generalize it to "unconditional".

Original page images may contain information or structure absent from text. Read the
images attached to the selected page before declaring evidence absent. If some
images are not attached, use get_image on the listed relevant paths. Text-only
extraction cannot establish that a pictured configuration is absent.
Use images to resolve the requested field or hierarchy. Do not transcribe
incidental example IP addresses, object IDs or credentials unless asked for them.
If a requested literal is visually ambiguous, state that gap rather than guessing.

Only verified_applicable_versions establish applicability. Filename strings and
generated titles are hints; do not turn them into version claims. For unspecified
versions, state that applicability is unconfirmed. Generated navigation titles
are not original section headings; a physical page number is not a section number.
Present product/version applicability and citations in ordinary user language.
Do not print internal keys such as verified_applicable_versions, doc_name, view_id,
tool names or evidence registry entries in the answer.
"""


def original_quote(quote: str, source: str) -> str | None:
    """Accept a double-escaped newline only if the decoded quote exists verbatim."""
    for candidate in (quote, quote.replace("\\n", "\n")):
        if candidate.strip() and candidate in source:
            return candidate
    return None


def unsupported_version_claims(answer, selection) -> tuple[str, ...]:
    return unsupported_versions(answer, selection)


def version_rejection(versions: tuple[str, ...]) -> str:
    return (
        "本次回答未通过证据检查：生成内容把未确认的版本（"
        + "、".join(versions)
        + "）当作了适用版本。请确认资料版本或选择已验证的知识视图后重新提问。"
    )
