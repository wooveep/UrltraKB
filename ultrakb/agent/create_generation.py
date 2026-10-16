"""Capacity-aware new-page contributions, exact coverage and durable continuation."""

import json
from dataclasses import dataclass, replace
from typing import Any

from ultrakb.agent.compile_capacity import compile_capacity
from ultrakb.agent.compile_contract import legacy_prompts, request_version, require_frozen_request
from ultrakb.agent.compile_messages import build_page_messages
from ultrakb.agent.create_assembly import assemble_contributions
from ultrakb.agent.create_delivery import create_repairs
from ultrakb.agent.create_requests import coverage_id, original_message, partition_originals
from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.page_figures import PageFigures
from ultrakb.agent.page_input import page_input
from ultrakb.agent.page_response import PageResponseError, parse_page
from ultrakb.agent.retained_delivery import reuse_saved_response
from ultrakb.compile_checkpoints import digest, document_checkpoints
from ultrakb.llm_execution import check_model_stop

PROTOCOL = "compiler-create-contributions-v1"


@dataclass
class CreationBatch:
    key: str
    signature: str
    windows: list[dict]
    messages: list[dict]
    figures: PageFigures
    excerpts: Any
    coverage: tuple[str, ...]
    accepted: dict | None = None


def _validate(raw, batch):
    return parse_page(
        validate_page_delivery(
            raw,
            original_excerpts=batch.excerpts,
            figure_context=batch.figures if batch.figures.figures else None,
            expected_coverage=batch.coverage,
        )
    )


@dataclass
class PageCreation:
    page: str
    model: str
    bundle: Any
    checkpoints: Any
    batches: list[CreationBatch]
    fits: Any

    def stage(self, number):
        stage = "entity" if self.page.startswith("entities/") else "concept"
        stage += ": " + self.page.split("/")[-1].removesuffix(".md")
        if len(self.batches) > 1:
            stage += f" [batch {number}/{len(self.batches)}]"
        return stage

    @property
    def missing(self):
        return sum(b.accepted is None for b in self.batches)

    async def generate(self):
        from ultrakb.agent.compiler import _llm_call_page_async
        from ultrakb.llm_execution import active_executor

        check_model_stop()
        if executor := active_executor():
            executor.budget.require(
                "compile", self.missing + min(self.missing, executor.policy.compile_repair_calls)
            )
        values = []
        for i, batch in enumerate(self.batches, 1):
            check_model_stop()
            value = batch.accepted
            if value is None:
                if self.checkpoints:
                    self.checkpoints.require_generation(batch.key, batch.signature)
                stage = self.stage(i)
                if self.checkpoints:
                    self.checkpoints.claim_generation(
                        batch.key, batch.signature, stage=f"compile.{stage}"
                    )
                raw = await _llm_call_page_async(
                    self.model,
                    batch.messages,
                    stage,
                    bundle=self.bundle,
                    prompt_version=PROTOCOL,
                    response_format={"type": "json_object"},
                    figure_context=batch.figures if batch.figures.figures else None,
                    original_excerpts=batch.excerpts,
                    checkpoints=self.checkpoints,
                    checkpoint_key=batch.key,
                    create_delivery=batch.coverage,
                )
                value = _validate(raw, batch)
                if self.checkpoints:
                    self.checkpoints.save(
                        batch.key,
                        batch.signature,
                        value,
                        metadata={
                            "protocol": PROTOCOL,
                            "page": self.page,
                            "coverage": [coverage_id(w) for w in batch.windows],
                            "sources": [
                                {k: v for k, v in w.items() if k != "content"}
                                for w in batch.windows
                            ],
                        },
                    )
            batch.accepted = value
            values.append(value)
        if len(values) == 1:
            merged = dict(values[0])
        else:
            # Contributions retain their native operation headings and conditions.
            # No final request repeats all originals or rewrites accepted bodies.
            merged = {
                "description": values[0].get("description", ""),
                "content": assemble_contributions(values, [b.windows for b in self.batches]),
            }
            types = {v.get("type") for v in values if v.get("type") not in {None, "other"}}
            if self.page.startswith("entities/"):
                merged["type"] = next(iter(types)) if len(types) == 1 else "other"
            selected = {r for v in values for r in v.get("source_ranges", [])}
            if selected:
                merged["source_ranges"] = sorted(selected)
        figures = PageFigures(
            self.batches[0].figures.wiki,
            tuple({r["id"]: r for b in self.batches for r in b.figures.ranges}.values()),
            tuple({f.occurrence_id: f for b in self.batches for f in b.figures.figures}.values()),
        )
        return figures.decorate(self.page, merged)


def prepare_page_creation(
    *,
    windows,
    originals,
    model,
    bundle,
    system_message,
    current_original,
    known_targets_message,
    task_message,
    page,
    checkpoints=None,
    fits=None,
):
    """Plan all complete requests before the caller starts concurrent generation."""
    fits = fits or compile_capacity(windows.kb_dir, model, bundle)
    checkpoints = checkpoints or document_checkpoints(
        windows.kb_dir, windows.view_id, windows.doc_name
    )

    def build(batch, multipart):
        check_model_stop()
        context = replace(originals, windows=tuple(batch))
        long = current_original is None
        figures = windows.page_figures(batch, long=long)
        excerpts = windows.original_excerpts(context, long=long)
        excerpts.request_version = request_version(checkpoints)
        coverage = tuple(coverage_id(w) for w in batch) if multipart else ()
        extra = ""
        if multipart:
            extra = (
                "\nWrite only this batch's contribution to the page, preserving all relevant "
                "facts, prerequisites, exceptions, native table headers and operation headings. "
                "Other batches are not evidence of absence. Do not repeat the page H1 or "
                "invent a whole-page conclusion. Description identifies this page's topic. "
                "Return covered_ranges acknowledging every supplied batch ID exactly once; "
                "the program also checks contiguous coverage and assembles contributions.\n"
                + json.dumps({"required_covered_ranges": list(coverage)})
            )
        messages = build_page_messages(
            system_message=system_message,
            current_original=current_original,
            original_context=original_message(originals, batch),
            known_targets_message=known_targets_message,
            task_message={**task_message, "content": task_message["content"] + extra},
        )
        messages = page_input(
            messages, figures=figures if figures.figures else None, excerpts=excerpts, compact=True
        )
        return CreationBatch(
            "",
            digest(messages),
            batch,
            messages,
            figures,
            excerpts,
            coverage,
        )

    def admitted(batch, multipart):
        planned = build(batch, multipart)
        repairs = (
            legacy_prompts()["create_repairs"]["multiple" if planned.coverage else "single"]
            if request_version(checkpoints) == 1
            else create_repairs(planned.coverage)
        )
        return fits(planned.messages) and all(
            fits([*planned.messages, repair]) for repair in repairs
        )

    groups = partition_originals(originals, windows, admitted)
    batches = []
    for i, group in enumerate(groups):
        b = build(group, len(groups) > 1)
        b.key = f"create:{page}:batch:{i}"
        require_frozen_request(checkpoints, b.key, b.signature)
        cached = checkpoints.load(b.key, b.signature) if checkpoints else None
        if cached is not None:
            b.accepted = _validate(json.dumps(cached, ensure_ascii=False), b)
        if not admitted(group, len(groups) > 1):
            raise PageResponseError(
                "page_create_capacity_exceeded", "Complete creation input cannot fit"
            )
        batches.append(b)
    creation = PageCreation(page, model, bundle, checkpoints, batches, fits)
    # Replay before budget admission as well as before the per-response repair gate.
    if checkpoints:
        for number, batch in enumerate(batches, 1):
            if batch.accepted is None:
                batch.accepted = reuse_saved_response(
                    checkpoints,
                    batch.key,
                    batch.signature,
                    protocol="page-json",
                    stage=creation.stage(number),
                    validate=lambda raw, batch=batch: validate_page_delivery(
                        raw,
                        original_excerpts=batch.excerpts,
                        figure_context=batch.figures if batch.figures.figures else None,
                        expected_coverage=batch.coverage,
                    ),
                    excerpts=batch.excerpts,
                    metadata={
                        "protocol": PROTOCOL,
                        "page": page,
                        "coverage": [coverage_id(w) for w in batch.windows],
                        "sources": [
                            {k: v for k, v in w.items() if k != "content"} for w in batch.windows
                        ],
                    },
                )
    return creation
