"""Explicit, auditable alias confirmation shared by every application adapter."""

import uuid
from pathlib import Path

from openkb.locks import kb_ingest_lock, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.product_identity import product_key
from openkb.source_catalog import read_record, record_path, write_record
from openkb.source_records import Record, RecordId
from openkb.view_records import Label, Product


class ProductConfirmation(Record):
    confirmation_id: RecordId
    product_id: RecordId
    aliases: tuple[Label, ...]
    previous_aliases: tuple[Label, ...]
    retired_product_ids: tuple[RecordId, ...] = ()


def list_products(kb_dir: Path) -> tuple[Product, ...]:
    with kb_read_lock(kb_dir / ".openkb"):
        return tuple(
            read_record(kb_dir, "products", p.stem, Product)
            for p in sorted((kb_dir / ".openkb/catalog/products").glob("*.json"))
        )


def confirm_product_aliases(kb_dir: Path, product_id: str, aliases: tuple[str, ...]) -> Product:
    """Confirm names for future binding; existing sources move only through version review."""
    with kb_ingest_lock(kb_dir / ".openkb"):
        product = read_record(kb_dir, "products", product_id, Product)
        if product.retired_into:
            raise ValueError("Cannot confirm aliases on a retired product identity")
        updated = Product(
            product_id=product_id,
            name=product.name,
            aliases=tuple(dict.fromkeys((*product.aliases, *aliases))),
        )
        unique: dict[str, str] = {}
        for alias in updated.aliases:
            unique.setdefault(product_key(alias), alias)
        updated = updated.model_copy(update={"aliases": tuple(unique.values())})
        keys = {product_key(a) for a in updated.aliases}
        for other in list_products(kb_dir):
            if (
                other.product_id != product_id
                and other.retired_into != product_id
                and keys.intersection(product_key(a) for a in (other.name, *other.aliases))
            ):
                raise ValueError("Product alias already confirmed for another identity")
        if updated == product:
            return product
        confirmation = ProductConfirmation(
            confirmation_id=uuid.uuid4().hex,
            product_id=product_id,
            aliases=updated.aliases,
            previous_aliases=product.aliases,
        )
        path = record_path(kb_dir, "products", product_id)
        audit = record_path(kb_dir, "product-confirmations", confirmation.confirmation_id)
        with mutation_scope(kb_dir, [path, audit], operation="confirm-product-aliases"):
            write_record(path, updated)
            write_record(audit, confirmation)
        return updated


def retire_product_identity(kb_dir: Path, product_id: str, target_id: str) -> Product:
    """Audit an explicit identity correction after every live source has moved.

    Historical annotations retain their IDs and names. Retirement does not move
    any source, choose defaults, or authorize a merge based on fuzzy spelling.
    """
    from openkb.source_catalog import list_sources
    from openkb.view_records import KnowledgeView, VersionAnnotation

    with kb_ingest_lock(kb_dir / ".openkb"):
        previous = read_record(kb_dir, "products", product_id, Product)
        target = read_record(kb_dir, "products", target_id, Product)
        if product_id == target_id or target.retired_into:
            raise ValueError("Retirement requires a different active target identity")
        if previous.retired_into == target_id:
            return target
        if previous.retired_into:
            raise ValueError("Identity has already been retired into another product")
        for source in list_sources(kb_dir):
            if source.removed or not source.annotation_id:
                continue
            annotation = read_record(kb_dir, "annotations", source.annotation_id, VersionAnnotation)
            if annotation.view_id == "legacy":
                continue
            view = read_record(kb_dir, "views", annotation.view_id, KnowledgeView)
            if view.product_id == product_id:
                raise ValueError(
                    "Live sources still use this identity; finish their version reviews"
                )
        aliases = tuple(dict.fromkeys((*target.aliases, previous.name, *previous.aliases)))
        keys = {product_key(a) for a in aliases}
        for other in list_products(kb_dir):
            if other.product_id in {product_id, target_id} or other.retired_into == target_id:
                continue
            if keys.intersection(product_key(a) for a in (other.name, *other.aliases)):
                raise ValueError("Retired alias conflicts with another product identity")
        updated = target.model_copy(update={"aliases": aliases})
        audit = ProductConfirmation(
            confirmation_id=uuid.uuid4().hex,
            product_id=target_id,
            aliases=aliases,
            previous_aliases=target.aliases,
            retired_product_ids=(product_id,),
        )
        records = {
            record_path(kb_dir, "products", product_id): previous.model_copy(
                update={"retired_into": target_id}
            ),
            record_path(kb_dir, "products", target_id): updated,
            record_path(kb_dir, "product-confirmations", audit.confirmation_id): audit,
        }
        with mutation_scope(kb_dir, list(records), operation="retire-product-identity"):
            for path, value in records.items():
                write_record(path, value)
        return updated
