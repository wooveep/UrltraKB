"""Prepare real worksheet publication state without generating model responses.

Identity, source reading and retirement tests consume source data directly. The
live workbook imports separately cover compiler and index delivery.
"""

import hashlib
import json
from contextlib import contextmanager

from ultrakb.locks import atomic_write_text, kb_ingest_lock


@contextmanager
def _publication_context(kb, context, *, record_model_task):
    if record_model_task:
        with context.begin(kb):
            yield
    else:
        # Preparing manual domain state does not consume a model task. Capture
        # real settings under the lease and keep a zero-send executor active.
        from ultrakb.config_state import capture_config

        snapshot = capture_config(kb)
        with snapshot.activate(), context.executor.activate():
            yield


def publish_workbook(
    kb,
    path,
    *,
    bundle=None,
    admission=None,
    retry_confirmed=False,
    sheet_names=None,
    record_model_task=True,
):
    from ultrakb.agent.compiler import _write_summary
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.ingestion import result_from_publication
    from ultrakb.application.views import view_scope
    from ultrakb.application.workbook_ingestion import aggregate_units
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.inputs import prepared_input
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.normalization import (
        normalization_fingerprint,
        restore_normalization,
        retain_normalization,
    )
    from ultrakb.selected_refresh import _refresh_index
    from ultrakb.source_catalog import admit_source_revision
    from ultrakb.source_map import freeze_text_map
    from ultrakb.unit_publication import (
        begin_unit_attempt,
        plan_import_units,
        prepare_compile_view,
        publish_unit_revision,
        record_unit_failure,
    )
    from ultrakb.workbooks.catalog import retain_workbook
    from ultrakb.workbooks.lifecycle import retire_missing_sheets, retire_sheet

    if bundle is None:
        bundle = LlmCredentialBundle(api_key="local-worksheet-domain")
    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", bundle), ModelCallPolicy(max_calls=0, retries=0)
    )
    context = ExecutionContext(executor=executor)
    with (
        kb_ingest_lock(kb / ".ultrakb"),
        _publication_context(kb, context, record_model_task=record_model_task),
        prepared_input(path) as prepared,
    ):
        if admission is None:
            admission = admit_source_revision(kb, prepared)
        else:
            assert prepared.digest == admission.revision.digest
            assert prepared.source.suffix[1:].lower() == admission.revision.source_format
        scope = view_scope(kb, "legacy")
        fingerprint = normalization_fingerprint(
            kb,
            scope=scope,
            bundle=bundle,
            source_revision=admission.revision,
            doc_name=admission.source.doc_name,
        )
        workbook = retain_workbook(kb, admission, prepared)
        assert workbook.sheets and not workbook.error, workbook
        if sheet_names is not None:
            assert set(sheet_names) <= {sheet.name for sheet in workbook.sheets}
        results = retire_missing_sheets(kb, admission, workbook, fingerprint, view_id=scope.view_id)
        for sheet in workbook.sheets:
            if sheet_names is not None and sheet.name not in sheet_names:
                continue
            selected = json.dumps(
                {**json.loads(fingerprint), "sheet": {"key": sheet.key, "policy": workbook.policy}},
                sort_keys=True,
            )
            unit, revision = plan_import_units(
                kb,
                admission,
                selected,
                key=sheet.key,
                name=sheet.name,
                doc_name=admission.source.doc_name
                + "-sheet-"
                + hashlib.sha256(sheet.key.encode()).hexdigest()[:12],
            )
            state, runnable = begin_unit_attempt(
                kb,
                unit,
                revision,
                discovery_intent=admission.discovery_intent,
                view_id=scope.view_id,
                retry_confirmed=retry_confirmed,
            )
            if not runnable:
                results.append(result_from_publication(kb, admission, state, status="skipped"))
                continue
            stage = "conversion"
            try:
                directory, normalized = retain_normalization(
                    kb,
                    admission,
                    prepared,
                    selected,
                    scope=scope,
                    bundle=bundle,
                    unit=unit,
                    sheet=sheet,
                )
                if sheet.content_state == "empty":
                    state = retire_sheet(kb, admission, unit, state, "empty")
                else:
                    with prepare_compile_view(kb, scope.view_id) as view:
                        converted = restore_normalization(
                            directory, normalized, view.scope.wiki_dir.parent
                        )
                        assert not converted.is_long_doc, (
                            "Domain fixture requires a short worksheet"
                        )
                        assert converted.source_path is not None
                        wiki = view.scope.wiki_dir
                        source = wiki / f"sources/{unit.doc_name}.md"
                        body = converted.source_path.read_text("utf-8")
                        atomic_write_text(source, body)
                        _write_summary(
                            wiki,
                            unit.doc_name,
                            body,
                            description=sheet.name,
                            full_text=f"sources/{unit.doc_name}.md",
                        )
                        _refresh_index(wiki)
                        stage = "publication"
                        state = publish_unit_revision(
                            kb,
                            admission,
                            unit,
                            revision,
                            state,
                            view,
                            normalized_source=f"sources/{unit.doc_name}.md",
                            source_map=freeze_text_map(wiki, unit.doc_name),
                            processing=converted.processing,
                            normalized_format="markdown",
                            is_long=False,
                        )
                results.append(result_from_publication(kb, admission, state, status="added"))
            except RecoveryRequired:
                raise
            except Exception as error:
                failed = record_unit_failure(kb, state, stage, error)
                results.append(result_from_publication(kb, admission, failed, status="failed"))
        assert executor.calls == 0
        return aggregate_units(results)
