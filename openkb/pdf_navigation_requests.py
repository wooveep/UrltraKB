"""The legacy PDF algorithm uses the normal bounded, checkpointed model transport."""

import json
import re
from contextlib import contextmanager
from threading import BoundedSemaphore

from json_repair import loads as repair_loads

from openkb.agent.analysis_flights import claim
from openkb.agent.model_json import json_text
from openkb.agent.source_protocol import SYSTEM, source_messages
from openkb.config import compilation_model_options
from openkb.navigation_enhancement import IndexAllowanceExceeded
from openkb.processing import InputTooLarge, OutputTruncated, processing_checkpoint


class PDFContentError(Exception):
    """A bounded model/content failure; never a provider or storage exception."""


@contextmanager
def _single_request(checkpoints, key, stage):
    # Shared-page nodes can ask the identical summary question concurrently.
    # Own the full retry/save sequence, reusing the existing cancellable flight.
    flight = claim((str(checkpoints.root), key))
    try:
        if not flight.owner:
            flight.wait(stage)
            if checkpoints.load(key) is None:
                raise PDFContentError("pdf_shared_request_unavailable")
        yield
    finally:
        flight.finish()


TEXT_TASKS = {"extract_toc_content", "generate_node_summary", "generate_doc_description"}
ARRAY_TASKS = {
    "generate_toc_init",
    "generate_toc_continue",
    "toc_index_extractor",
    "add_page_number_to_toc",
}
ANSWER_FIELDS = {
    "toc_detector_single_page": "toc_detected",
    "check_title_appearance": "answer",
    "check_title_appearance_in_start": "start_begin",
    "check_if_toc_extraction_is_complete": "completed",
    "check_if_toc_transformation_is_complete": "completed",
    "detect_page_index": "page_index_given_in_toc",
}


def _page_number(value):
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str):
        match = re.fullmatch(r"(?:<?physical_index_)?(\d+)>?", value.strip())
        if match:
            return int(match[1]) or None
    return None


def _rows(value):
    result = []
    for row in value:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
        ):
            continue
        row = dict(row)
        structure = row.get("structure")
        row["structure"] = str(structure) if isinstance(structure, (str, int, float)) else None
        for key in ("physical_index", "page"):
            if key in row:
                row[key] = _page_number(row[key])
        result.append(row)
    return result


def accepted_text(task, raw):
    if not raw.strip() or getattr(raw, "finish_reason", None) == "length":
        raise PDFContentError("empty_or_truncated_pdf_response")
    if task in TEXT_TASKS:
        return str(raw).strip()
    try:
        value = repair_loads(json_text(str(raw)))
    except (ValueError, TypeError) as exc:
        raise PDFContentError("invalid_pdf_response") from exc
    if task in ARRAY_TASKS:
        if isinstance(value, dict):
            value = value.get(
                "table_of_contents", value.get("sections", [value] if "title" in value else None)
            )
        if not isinstance(value, list):
            raise PDFContentError("invalid_pdf_structure")
        value = _rows(value)
        if task == "add_page_number_to_toc" and not value:
            raise PDFContentError("empty_pdf_location_result")
    elif task in ANSWER_FIELDS:
        key = ANSWER_FIELDS[task]
        if not isinstance(value, dict) or str(value.get(key, "")).lower() not in {"yes", "no"}:
            raise PDFContentError("invalid_pdf_decision")
        value[key] = value[key].lower()
    elif task == "toc_transformer":
        if isinstance(value, list):
            value = {"table_of_contents": value}
        if not isinstance(value, dict) or not isinstance(value.get("table_of_contents"), list):
            raise PDFContentError("invalid_pdf_contents")
        value["table_of_contents"] = _rows(value["table_of_contents"])
    elif task == "single_toc_item_index_fixer":
        if not isinstance(value, dict) or "physical_index" not in value:
            raise PDFContentError("invalid_pdf_location")
        value["physical_index"] = _page_number(value["physical_index"])
    return json.dumps(value, ensure_ascii=False)


class PDFRequests:
    def __init__(self, pages, settings, bundle, allowance, checkpoints, profile):
        self.pages, self.settings, self.bundle = pages, settings, bundle
        self.allowance, self.checkpoints, self.profile = allowance, checkpoints, profile
        self.slots = BoundedSemaphore(allowance.limits.concurrency)

    def call(self, scope, prompt, history=None):
        from openkb.agent.compiler import _llm_call

        name, original, numbers = scope
        evidence = self.pages.evidence(numbers)

        # Move only a known original argument, never arbitrary prompt substrings.
        def suffix(text):
            return (
                text.replace(original, "[Read the original PDF pages in evidence.pdf]")
                if original
                else text
            )

        task = {"stage": "index_pdf_" + name, "legacy_task": name}
        if history:
            task["history"] = [{**item, "content": suffix(item["content"])} for item in history]
        rules = suffix(prompt)
        options = {"temperature": 0, **compilation_model_options(self.settings)}
        with self.slots:
            processing_checkpoint(task["stage"])
            messages = source_messages(evidence, task, rules)
            with (
                self.checkpoints.request(
                    SYSTEM,
                    {"stage": task["stage"], "messages": list(messages)},
                    dependencies={"profile": self.profile, "options": options},
                ) as key,
                _single_request(self.checkpoints, key, task["stage"]),
            ):
                saved = self.checkpoints.load(key)
                if saved is not None:
                    if saved.get("error"):
                        raise PDFContentError(saved["error"])
                    return accepted_text(name, saved["content"])
                reason = "invalid_pdf_response"
                for attempt in range(self.allowance.limits.max_attempts):
                    if attempt:
                        messages = source_messages(evidence, {**task, "recovery": reason}, rules)
                    try:
                        self.allowance.request(self.settings["model"], messages)
                        with self.allowance.enforce():
                            raw = _llm_call(
                                self.settings["model"],
                                messages,
                                task["stage"],
                                bundle=self.bundle,
                                decode_response=False,
                                max_tokens=min(
                                    self.allowance.limits.output_tokens,
                                    self.allowance.budget.limits.output_tokens,
                                ),
                                **options,
                            )
                        text = accepted_text(name, raw)
                        self.checkpoints.save(key, {"content": text})
                        return text
                    except (PDFContentError, OutputTruncated) as exc:
                        reason = getattr(exc, "reason", str(exc))
                    except (InputTooLarge, IndexAllowanceExceeded) as exc:
                        raise PDFContentError(getattr(exc, "reason", str(exc))) from exc
                self.checkpoints.save(key, {"error": reason})
                raise PDFContentError(reason)
