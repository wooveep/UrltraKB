"""Per-question evidence ledger shared by restricted tools and final review."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from openkb.agent.answer_evidence import original_quote
from openkb.agent.evidence_budget import EvidenceBudget
from openkb.application.query_views import QuerySelection, QueryView

MAX_TOOL_CALLS = 48
MAX_READ_CHARS = 160000
MAX_FACT_ATTEMPTS = 24


@dataclass(frozen=True)
class EvidenceRead:
    read_id: str
    view_id: str
    product_id: str | None
    path: str
    digest: str
    source_revision_id: str | None
    locator: str
    content: str
    spans: tuple[tuple[int, int], ...] = ()
    kind: str = "text"
    image_url: str | None = None


class EvidenceSession:
    """Only this question's actual, hash-checked reads can supply facts.

    Registration checks provenance. Semantic acceptance happens at final review;
    a tool's success response never asserts that a rule interpretation is true.
    """

    def __init__(self, selection: QuerySelection):
        from openkb.agent.answer_writes import AnswerWrites

        self.selection = selection
        self.writes = AnswerWrites(selection.kb_dir)
        self.budget = EvidenceBudget()
        self.reads: dict[str, EvidenceRead] = {}
        self.locators: dict[tuple[str, str], str] = {}
        self.facts: dict[str, dict] = {}
        self.accepted: set[str] = set()
        self.reviewed_images: set[str] = set()
        self.review_audit: list[dict] = []
        self.drafts: list[str] = []
        self.audit_path: str | None = None
        self.attempts: dict[str, int] = {}
        self.failures: dict[str, str] = {}
        self.source_rules: dict[tuple[str, str], tuple[str, ...]] = {}
        self.tool_calls = self.read_chars = self.fact_attempts = 0

    def consume_tool(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > MAX_TOOL_CALLS:
            raise ValueError(
                "Question tool budget exhausted; finish with verified evidence and gaps"
            )

    def register_read(
        self,
        view: QueryView,
        path: str,
        locator: str,
        content: str,
        *,
        spans=(),
        kind="text",
        image_url=None,
    ) -> EvidenceRead:
        revision = view.source_revisions_by_path.get(path)
        identity = hashlib.sha256(
            json.dumps(
                [
                    view.view_id,
                    revision,
                    view.files[path],
                    locator,
                    content,
                    kind,
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()[:24]
        if identity not in self.reads:
            if self.read_chars + len(content) > MAX_READ_CHARS:
                raise ValueError(
                    "Question source-text budget exhausted; narrow the requested facts"
                )
            self.read_chars += len(content)
            self.reads[identity] = EvidenceRead(
                identity,
                view.view_id,
                view.product_id,
                path,
                view.files[path],
                revision,
                locator,
                content,
                tuple(tuple(span) for span in spans),
                kind,
                image_url,
            )
        self.locators[view.view_id, locator] = identity
        self.locators[view.view_id, identity] = identity
        return self.reads[identity]

    def check_quote(self, read_id: str, quote: str) -> tuple[EvidenceRead, str] | None:
        read = self.reads.get(read_id)
        if read is None or read.kind != "text" or len(quote) > 8000:
            return None
        matched = original_quote(quote, read.content)
        return (read, matched) if matched else None

    def fact_slot(self, read_id: str, subject: str) -> str:
        """Bind retries to an original rule, independent of its reading locator."""
        from openkb.agent.evidence_rules import original_rules, rule_anchor

        read = self.reads.get(read_id)
        anchor = -1
        if read and read.kind == "text":
            key = (read.view_id, read.path)
            if key not in self.source_rules:
                view = next(v for v in self.selection.views if v.view_id == read.view_id)
                self.source_rules[key] = original_rules(view, read.path)
            anchor = rule_anchor(self.source_rules[key], read.content, subject)
        identity = (
            (
                read.view_id,
                read.source_revision_id,
                read.path,
                read.digest,
                anchor,
            )
            if read
            else ("unread",)
        )
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]

    def fact_exhausted(self, read_id: str, subject: str) -> bool:
        return bool(self.attempts) and self.attempts.get(self.fact_slot(read_id, subject), 0) >= 2

    def record_fact(
        self,
        view_id: str,
        *,
        subject: str,
        setting: str,
        condition: str,
        quote: str,
        locator: str,
        slot_id: str = "",
    ) -> str:
        locator = locator.removeprefix("Evidence locator:").strip()
        read_id = self.locators.get((view_id, locator), "")
        slot = self.fact_slot(read_id, subject)
        invalid_slot = bool(slot_id and slot_id != slot)
        fingerprint = hashlib.sha256(
            json.dumps([slot, subject, setting, condition, quote, invalid_slot]).encode()
        ).hexdigest()
        if fingerprint in self.failures:
            return self.failures[fingerprint]
        self.fact_attempts += 1
        if self.attempts.get(slot, 0) >= 2 or self.fact_attempts > MAX_FACT_ATTEMPTS:
            return f"Unverified source fact: retry budget exhausted; slot_id={slot}. State the gap."
        checked = self.check_quote(read_id, quote)
        error = ""
        if invalid_slot:
            error = "slot_id does not identify this original rule. Use the returned slot_id."
        elif checked is None:
            error = "quote/locator does not match an original read. Read the source again."
        elif (
            not subject.strip()
            or not setting.strip()
            or any(value not in checked[1] for value in (subject, setting, condition))
        ):
            error = (
                "subject, setting and condition must come from the same exact quote. "
                "Do not transfer conditions between rules."
            )
        elif not _same_rule(checked[1], subject, setting, condition):
            error = "The quote joins distinct rules. Do not transfer conditions between rules."
        if error:
            self.attempts[slot] = self.attempts.get(slot, 0) + 1
            result = (
                f"Unverified source fact: {error} slot_id={slot}; "
                f"corrections_left={max(0, 2 - self.attempts[slot])}"
            )
            self.failures[fingerprint] = result
            return result
        assert checked is not None
        fact = {
            "fact_id": "fact-" + fingerprint[:20],
            "slot_id": slot,
            "read_id": read_id,
            "subject": subject,
            "setting": setting,
            "condition": condition,
            "condition_state": "explicit" if condition else "not_extracted",
            "quote": checked[1],
            "locator": locator,
            "view_id": view_id,
            "status": "provenance_checked",
        }
        self.facts[fact["fact_id"]] = fact
        return json.dumps(fact, ensure_ascii=False)

    def review_packet(self) -> dict:
        reads = []
        for read in self.reads.values():
            item = asdict(read)
            item.pop("image_url")
            if read.kind in {"text", "image"}:
                reads.append(item)
        return {"reads": reads, "registered_facts": list(self.facts.values())}


def _same_rule(quote: str, subject: str, setting: str, condition: str) -> bool:
    rules = re.split(r"[。！？;；]|\.\s+", quote)
    return any(subject in rule and setting in rule and condition in rule for rule in rules)
