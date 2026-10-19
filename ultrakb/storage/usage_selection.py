"""Union overlapping usage selections using their indexed aggregate intersections."""

from ultrakb.llm_usage_models import UsageAggregate, UsageReceipt

from .catalog_store import CatalogStore
from .coordination import storage_stage
from .database import database_path


def combine_usage(store, receipts):
    receipts = [UsageReceipt.model_validate(receipt) for receipt in receipts]
    if any(
        receipt.ledger != str(database_path(store.root))
        or not receipt.selections
        or (receipt.kb_id, receipt.generation) != (store.identity.kb_id, store.identity.generation)
        for receipt in receipts
    ):
        raise ValueError("Main-store usage requires explicit selections from this ledger")
    selections = {
        (item.execution_id, item.source_id, item.unit_id)
        for receipt in receipts
        for item in receipt.selections
    }
    catalog = CatalogStore(store.root, store.identity)
    unit_sources = {
        unit: catalog.unit(unit).source_id for _, _, unit in selections if unit is not None
    }
    if any(
        source is not None and unit is not None and unit_sources[unit] != source
        for _, source, unit in selections
    ):
        raise ValueError("Usage unit belongs to another source")
    whole = {execution for execution, source, unit in selections if source is None and unit is None}
    source_pairs = {
        (execution, source)
        for execution, source, unit in selections
        if source and unit is None and execution not in whole
    }
    unit_pairs = {
        (execution, unit)
        for execution, _, unit in selections
        if unit and execution not in whole and (execution, unit_sources[unit]) not in source_pairs
    }
    sources = {source for _, source, unit in selections if source and unit is None}
    units = {unit for _, _, unit in selections if unit and unit_sources[unit] not in sources}
    with storage_stage(store.root, store.identity, read_only=True) as db:
        db.connection.execute("BEGIN")
        current = store.selected_summary(
            [
                ("execution", whole),
                (
                    "execution_source",
                    {f"{execution}:{source}" for execution, source in source_pairs},
                ),
                ("execution_unit", {f"{execution}:{unit}" for execution, unit in unit_pairs}),
            ],
            database=db,
        )
        cumulative = store.selected_summary(
            [("execution", whole), ("source", sources), ("unit", units)],
            subtract=[("execution_source", whole, sources), ("execution_unit", whole, units)],
            database=db,
        )
    return UsageReceipt(
        kb_id=store.identity.kb_id,
        generation=store.identity.generation,
        execution_ids=sorted({execution for execution, _, _ in selections}),
        source_ids=sorted({source for _, source, _ in selections if source}),
        unit_ids=sorted({unit for _, _, unit in selections if unit}),
        selections=[
            {"execution_id": execution, "source_id": source, "unit_id": unit}
            for execution, source, unit in sorted(
                selections, key=lambda item: tuple(value or "" for value in item)
            )
        ],
        ledger=str(database_path(store.root)),
        current=UsageAggregate.model_validate(current),
        cumulative=UsageAggregate.model_validate(cumulative),
        history_status="recorded" if sources or units else "execution_only",
    ).model_dump(mode="json")
