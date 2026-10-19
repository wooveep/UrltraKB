"""Retain complete worksheet inventory independently of each sheet's compilation."""

from uuid import uuid4

from ultrakb.workbooks.identity import reconcile_sheets
from ultrakb.workbooks.records import WORKBOOK_POLICY, XLS_POLICY, WorkbookSnapshot

from .blobs import put_blob, read_bytes, retain_blob
from .catalog_store import CatalogStore
from .coordination import storage_stage


class WorkbookStore:
    def __init__(self, root, identity):
        self.root, self.identity = root.resolve(), identity

    @staticmethod
    def _read(db, revision_id, processing_key=""):
        row = None
        # The first complete acceptance wins. Failed attempts remain diagnostics,
        # not a permanent substitute for an accepted inventory.
        for complete, direction in ((1, "ASC"), (0, "DESC")):
            row = db.connection.execute(
                "SELECT payload_digest, complete FROM ukb_workbook_inventories "
                "WHERE source_revision_id = ? AND processing_key = ? AND complete = ? "
                f"ORDER BY sequence {direction} LIMIT 1",
                (revision_id, processing_key, complete),
            ).fetchone()
            if row is not None:
                break
        if row is None:
            return None
        value = WorkbookSnapshot.model_validate_json(read_bytes(db, row[0]))
        revision = CatalogStore._revision(db, revision_id)
        original = next(file for file in revision.files if file.kind == "main")
        if (
            value.source_revision_id != revision_id
            or value.digest != original.digest
            or row[1] != int(value.error is None)
        ):
            raise ValueError("Worksheet inventory belongs to another original")
        return value

    def read(self, revision_id, *, processing_key=""):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            return self._read(db, revision_id, processing_key)

    def retain(self, revision, prepared, *, processing_key=""):
        with storage_stage(self.root, self.identity, read_only=True) as db:
            if processing_key:
                request = db.connection.execute(
                    "SELECT source_revision_id FROM ukb_reprocessing_requests WHERE request_id = ?",
                    (processing_key,),
                ).fetchone()
                if request != (revision.revision_id,):
                    raise ValueError("Worksheet reprocessing requires its retained request")
            stored = self._read(db, revision.revision_id, processing_key)
            if stored is not None and stored.error is None:
                return stored
            original = next(file for file in revision.files if file.kind == "main")
            if CatalogStore._revision(db, revision.revision_id) != revision:
                raise ValueError("Workbook inventory requires this KB's exact source revision")
            if prepared.digest != original.digest:
                raise ValueError("Worksheet parser requires the admitted original")
            row = db.connection.execute(
                "SELECT r.revision_id, w.processing_key FROM ukb_source_revisions r "
                "JOIN ukb_workbook_inventories w ON w.source_revision_id = r.revision_id "
                "WHERE r.source_id = ? AND (r.ordinal < ? OR (r.ordinal = ? AND ? != '')) "
                "AND r.state = 'ready' "
                "AND w.complete = 1 "
                "ORDER BY r.ordinal DESC, w.sequence DESC LIMIT 1",
                (revision.source_id, revision.ordinal, revision.ordinal, processing_key),
            ).fetchone()
            previous = self._read(db, *row) if row else None
        from ultrakb.workbooks.xls import read_xls
        from ultrakb.workbooks.xlsx import read_xlsx

        binary = prepared.source.suffix.lower() == ".xls"
        policy = XLS_POLICY if binary else WORKBOOK_POLICY
        try:
            sheets = (read_xls if binary else read_xlsx)(prepared.path)
            value = WorkbookSnapshot(
                source_revision_id=revision.revision_id,
                digest=prepared.digest,
                policy=policy,
                sheets=reconcile_sheets(
                    sheets, previous if previous and not previous.error else None
                ),
            )
        except Exception as error:
            value = WorkbookSnapshot(
                source_revision_id=revision.revision_id,
                digest=prepared.digest,
                policy=policy,
                sheets=(),
                error=f"Workbook inventory: {type(error).__name__}: {error}",
            )
        payload = value.model_dump_json().encode()
        inventory_id, complete = uuid4().hex, int(value.error is None)
        with storage_stage(self.root, self.identity) as db:
            blob = put_blob(db, payload)
            with db.transaction() as connection:
                accepted = connection.execute(
                    "SELECT 1 FROM ukb_workbook_inventories WHERE source_revision_id = ? "
                    "AND processing_key = ? AND complete = 1 LIMIT 1",
                    (revision.revision_id, processing_key),
                ).fetchone()
                if not complete or accepted is None:
                    connection.execute(
                        "INSERT INTO ukb_workbook_inventories "
                        "(inventory_id, source_revision_id, processing_key, complete, "
                        "payload_digest) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (inventory_id, revision.revision_id, processing_key, complete, blob.digest),
                    )
                    retain_blob(
                        connection,
                        "workbook-inventory",
                        inventory_id,
                        "inventory",
                        blob,
                    )
            return self._read(db, revision.revision_id, processing_key)
