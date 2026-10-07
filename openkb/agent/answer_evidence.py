"""Evidence rules shared by query/chat and deterministic version-claim checks."""

import re

from openkb.version_labels import version_key

ANSWER_EVIDENCE_RULES = """
Treat the user's requested output as a contract: include only requested categories.
If only files/paths are requested, return only files/paths and citations, no restore
commands, operational procedures or adjacent advice. Do not broaden a lookup into
instructions. Before finishing, compare each answer section to that contract.
For a files/paths-only request, the ENTIRE answer is one table: requested file,
required path, source. The file cell may label its purpose with a noun phrase
(e.g. "token source"). Put necessary conditions in the path cell. Do not include
headings, introductions, supplements, command fragments/options, ways to read
the file, or other files merely found alongside the requested ones. An unknown
destination is "not specified"; do not invent a destination from a shell command.
For a question about one subject, omit supplements about other configuration
subjects unless the user explicitly requests comparison. A shared value does
not establish a shared rule or subject. Do not synthesize an extra "common default".

For every configuration or conditional conclusion, retain four separate facts:
subject, configuration item, condition, original source locator. Different subjects
(e.g. a compute node's upstream versus a platform's external server) are distinct
rules. Never transfer a condition/default between subjects. For multi-source rules,
use record_source_fact with an exact source quote before synthesizing the answer.
After a failed fact check, correct that fact at most once; if still unverified,
omit it or state the gap. Never describe a failed fact check as verified.
In the final answer, give each requested rule its own row: subject, setting/value,
condition, source. Use the subject and condition from THAT rule's quote. Do not
add a combined conclusion below the rows. A condition for a configuration screen
is not a condition for a managed node, even if both mention the same server or
address. Exclude facts about other subjects; several documents in a question do
not authorize changing its target subject. An additional rule must independently
answer the user's question before it can be included.

Original page images may contain the only command or YAML hierarchy. Read the
images attached to the selected page before declaring evidence absent. If some
images are not attached, use get_image on the listed relevant paths. Text-only
extraction cannot establish that a pictured configuration is absent.
Use images to resolve the requested field or hierarchy. Do not transcribe
incidental example IP addresses, object IDs or credentials unless asked for them.
If a requested literal is visually ambiguous, state that gap rather than guessing.

Only verified_applicable_versions establish applicability. Filename strings and
generated titles are hints; do not turn them into version claims. For unspecified
versions, state that applicability is unconfirmed. Generated navigation titles
are not original section headings; physical page 50 never means section 50.
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
    """Check explicit numeric version assertions, excluding filenames and negative statements.

    This is a narrow lexical guard, not a semantic truth checker. It deliberately
    leaves package/configuration numbers without a version marker alone.
    """
    verified = {
        version_key(v)
        for view in selection.views
        if not view.reference_only
        for v in view.applicable_versions
    }
    unsupported: set[str] = set()
    for line in re.split(r"\n|(?<=[。！？])", answer):
        # Quoted filenames and link targets identify evidence; they do not assert applicability.
        line = re.sub(r"\]\([^)]*\)", "]", line)
        line = re.sub(r"[^\s`\[\]<>\"']+\.(?:pdf|docx|pptx|xlsx|md)\b", "", line, flags=re.I)
        if re.search(
            r"未确认|未核实|无法确认|未知版本|不适用|缺少.*证据|"
            r"未(?:提供|获得|给出).{0,30}(?:已验证|已核实|经核实)|"
            r"不能据此断言.*版本|unverified|unconfirmed|"
            r"unspecified|not verified|no evidence",
            line,
            re.I,
        ):
            continue
        matches = re.findall(
            r"(?i)(?:\bV\s*|版本\s*[:：]?\s*[vV]?|\bversion\s+[vV]?)(\d+(?:\.\d+){1,3})",
            line,
        )
        unsupported.update(v for v in matches if version_key(v) not in verified)
    return tuple(sorted(unsupported))


def version_rejection(versions: tuple[str, ...]) -> str:
    return (
        "本次回答未通过证据检查：生成内容把未确认的版本（"
        + "、".join(versions)
        + "）当作了适用版本。请确认资料版本或选择已验证的知识视图后重新提问。"
    )
