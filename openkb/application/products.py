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
            if other.product_id != product_id and keys.intersection(
                product_key(a) for a in other.aliases
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
