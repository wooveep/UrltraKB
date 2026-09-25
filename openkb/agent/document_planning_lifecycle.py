"""Initialize, validate, and publish the durable document-planning handoff."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from openkb.agent import document_planning_support
from openkb.agent.document_plan import DocumentPlan
from openkb.agent.document_plan_preview import (
    planning_execution_metrics,
    save_empty_planning_report,
    save_final_plan,
)
from openkb.agent.document_planning_result import PlanningResult
from openkb.execution_measurement import request_marker


@dataclass
class PlanningLifecycle:
    ledger: Any
    checkpoints: Any
    retained_key: str
    wiki: Any
    source: Any
    parsed: Any
    navigation: Any
    settings: dict[str, Any]
    schema: str
    rules_rev: str
    windowing_rev: str
    planning_revisions: dict[str, str]
    recovery_contract: dict[str, Any]
    entity_types: list[str]
    planning_limits: Any
    plan_only: bool
    return_result: bool = False
    started_at: float = 0.0
    request_start: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    report_ref: str | None = None

    def public_result(self, plan: DocumentPlan | None) -> DocumentPlan | PlanningResult | None:
        return PlanningResult.from_plan(plan, self.report_ref) if self.return_result else plan

    def adopt_terminal(self, windows: list[dict[str, Any]], on_event: Any) -> DocumentPlan | None:
        """Return a validated prior terminal result without dispatching model work."""
        from openkb.agent.document_planning_events import (
            emit_planning_observation,
            planning_observation,
        )

        restored = self.finalize(windows)
        receipt = self.ledger.receipt(len(windows)) or {}
        on_event({
            "stage": "planning", "cached": True, "retained": True,
            "pages": self.ledger.page_count(),
        })
        if windows:
            emit_planning_observation(
                on_event,
                planning_observation(
                    "adopted", windows[-1], windows, completed=len(windows),
                    carry_pages=self.ledger.page_count(),
                    carry_unresolved=self.ledger.open_unresolved_count(),
                    pages=self.ledger.page_count(),
                    unresolved=self.ledger.open_unresolved_count(),
                    checkpoint=receipt.get("checkpoint"), result=receipt.get("result"),
                    attempt=receipt.get("attempt", 0), cached=True,
                ),
            )
        return restored

    def settle_content_failure(
        self,
        window: dict[str, Any],
        *,
        windows: list[dict[str, Any]],
        window_index: int,
        reason: str,
        attempts: int,
        predecessor: str,
        on_event: Any,
    ) -> None:
        from openkb.compilation_report import report_content_omission
        from openkb.processing import ProcessingIncomplete

        if not self.ledger._v2():
            raise ProcessingIncomplete(reason, "planning")
        omission = self.ledger.settle_skipped(
            window, windows=windows, completed=window_index + 1,
            reason=reason, attempts=attempts, predecessor=predecessor,
            diagnostic_ref=None,
        )
        report_content_omission("planning", reason, [omission.key])
        self.checkpoints.save_recovery(
            self.retained_key, "plan", self.ledger.progress_preview()
        )
        on_event({
            "stage": "planning", "operation": "settled_skipped",
            "window": window_index + 1, "reason": reason, "omission": omission.key,
        })

    def initialize(self, *, reset: bool = False) -> tuple[dict[str, Any], Any]:
        if reset:
            self.ledger.reset()
        self.ledger.initialize(wiki=self.wiki, source=self.source, parsed=self.parsed)
        self.metadata = document_planning_support.planning_metadata(
            source=self.source,
            parsed=self.parsed,
            navigation=self.navigation,
            catalog_window="",
            catalog_targets=set(),
            catalog_entries=[],
            schema=self.schema,
            language=self.settings.get("language"),
            rules=self.rules_rev,
            windowing=self.windowing_rev,
            implementation=self.planning_revisions,
            recovery_key=self.retained_key,
            contract=self.recovery_contract,
            entity_types=self.entity_types,
            catalog_manifest=self.ledger.catalog_manifest(),
        )
        self.metadata["effective_planning_contract"] = (
            document_planning_support.planning_contract(self.settings, self.planning_limits)
        )
        self.ledger.bind_metadata(self.metadata)
        return self.metadata, self.ledger.overview()

    def reset(self) -> Any:
        self.ledger.reset()
        self.ledger.initialize(wiki=self.wiki, source=self.source, parsed=self.parsed)
        manifest = self.ledger.catalog_manifest()
        self.metadata.update({
            "catalog_snapshot": manifest["snapshot"],
            "catalog_count": manifest["count"],
            "catalog_ledger": manifest["ledger"],
        })
        self.ledger.bind_metadata(self.metadata)
        return self.ledger.overview()

    def remember_publication_state(self) -> None:
        """Keep the last formal plan while an explicit omission retry is pending."""
        from openkb.agent.document_plan import from_dict

        saved = self.checkpoints.load_recovery(self.retained_key, "plan")
        try:
            prior = from_dict(saved)
        except (AttributeError, KeyError, TypeError, ValueError):
            return
        if (
            prior.metadata.get("recovery_key") == self.retained_key
            and isinstance(prior.metadata.get("publication_receipt"), dict)
        ):
            self.checkpoints.save_recovery(self.retained_key, "plan_baseline", saved)

    def terminal_recovery_valid(self) -> bool:
        saved = self.checkpoints.load_recovery(self.retained_key, "plan")
        if saved is None or not isinstance(saved, dict) or "metadata" not in saved:
            return True
        try:
            from openkb.agent.document_plan import derive_page_states, from_dict, validate_plan

            restored = from_dict(saved)
            states = {page.key: page.state for page in restored.pages}
            validate_plan(
                restored, self.parsed, self.entity_types,
                self.ledger.final_catalog_targets(),
            )
            derive_page_states(restored.pages, restored.unresolved)
            return all(page.state == states[page.key] for page in restored.pages)
        except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
            return False

    def finalize(self, windows: list[dict[str, Any]]) -> DocumentPlan | None:
        from openkb.agent.document_recovery import inherit_publication_state
        from openkb.planning_coverage import planning_coverage

        self.ledger.mark_accepted(windows)
        materialized = self.ledger.materialize()
        previous = self.checkpoints.load_recovery(self.retained_key, "plan")
        if not isinstance(previous, dict) or "metadata" not in previous:
            previous = self.checkpoints.load_recovery(self.retained_key, "plan_baseline")
        prior_execution = (
            previous.get("metadata", {}).get("planning_execution")
            if isinstance(previous, dict) else None
        )
        if not isinstance(prior_execution, dict):
            prior_report = self.checkpoints.load_recovery(self.retained_key, "plan_report")
            prior_execution = (
                prior_report.get("planning_execution")
                if isinstance(prior_report, dict) else None
            )
        prior_execution = planning_execution_metrics(prior_execution)
        new_requests = max(0, request_marker() - self.request_start)
        execution = {
            "planning_requests": prior_execution.get("planning_requests", 0) + new_requests,
            "elapsed_seconds": prior_execution.get("elapsed_seconds", 0.0) + (
                max(0.0, time.monotonic() - self.started_at)
                if new_requests or not prior_execution else 0.0
            ),
        }
        if windows and all(
            receipt["status"] == "skipped" for receipt in self.ledger.receipts()
        ):
            self.report_ref = save_empty_planning_report(
                self.checkpoints, self.retained_key, self.metadata,
                materialized.planning_omissions, parsed=self.parsed,
                execution=execution,
            )
            return None
        final = document_planning_support.final_document_plan(
            metadata={
                **self.metadata,
                "planning_execution": execution,
                **self.ledger.final_catalog_metadata(),
                "accepted_window_receipts": self.ledger.receipts(),
            },
            windows=windows,
            overview=materialized.overview,
            pages=materialized.pages,
            source_only=materialized.source_only,
            unresolved=materialized.unresolved,
            resolutions=materialized.resolutions,
            external_references=materialized.external_references,
            planning_omissions=materialized.planning_omissions,
            plan_only=self.plan_only,
            parsed=self.parsed,
            entity_types=self.entity_types,
            existing_targets=self.ledger.final_catalog_targets(),
        )
        inherit_publication_state(final, previous)
        final.metadata["planning_coverage"] = planning_coverage(final, self.parsed)
        save_final_plan(self.checkpoints, self.retained_key, final)
        return final
