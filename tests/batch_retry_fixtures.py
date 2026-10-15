"""Retain a failed input with the captured real adapter shape, making zero sends."""

from ultrakb.locks import kb_ingest_lock


def retain_failure(kb, path):
    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.config_state import capture_config
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        executor_from_snapshot,
    )
    from ultrakb.source_catalog import list_sources
    from ultrakb.unit_publication import (
        list_source_units,
        read_unit_publication,
        record_unit_failure,
    )

    with kb_ingest_lock(kb / ".ultrakb"):
        snapshot = capture_config(kb)
    bindings = executor_from_snapshot(snapshot, lambda: False).bindings
    context = ExecutionContext(
        snapshot=snapshot,
        executor=CompletionExecutor(bindings, ModelCallPolicy(max_calls=0, retries=0)),
    )
    try:
        import_document(kb, path, context=context)
    except ModelBudgetExceeded:
        pass
    else:
        raise AssertionError("Fixture must stop before a model send")
    source = next(
        source for source in list_sources(kb) if source.identity == str(path.relative_to(kb))
    )
    [unit] = list_source_units(kb, source.source_id)
    assert context.executor.calls == 0
    with kb_ingest_lock(kb / ".ultrakb"):
        state = read_unit_publication(kb, unit.unit_id)
        record_unit_failure(kb, state, "delivery", ValueError("Local fixture delivery interrupted"))
    return source
