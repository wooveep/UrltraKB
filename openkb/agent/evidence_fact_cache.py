"""Recover verified source units across batch splits and known compiler upgrades."""

from openkb.agent.evidence_retry import ResponseIncomplete
from openkb.implementation import module_revision
from openkb.processing import processing_checkpoint
from openkb.sources import content_id

_LEGACY_BASE = {
    "evidence_units": "11b13587a64c24ed430ae1786bc805491b9b35952595126e1662dc267d9a42fe",
    "evidence_retry": "51ed29d4e493c8ebec15c71f0479a5b50067eddf1e472cfc0849d8908c07d476",
    "evidence_coverage": "41d9efc8652192d5f26802165496c015f15b7bc5d9edef4e057291ce694e4885",
}
_LEGACY_CONTRACTS = (
    {
        "evidence_facts": "30417286876bd6ea015ed4bf4d9470790fc8bccbfce79c16b0c6c747973db55c",
        "evidence_fact_cache": "7f6317e159653b9b9b293c927ff330607446206d3db5855138bf03036a42bdea",
        "evidence_units": _LEGACY_BASE["evidence_units"],
        "evidence_retry": "5b3f7fd79ecf6e56c2d5de6d946eb0a70ca4f34d96d0d3b4bdf605b72cedc199",
        "evidence_coverage": _LEGACY_BASE["evidence_coverage"],
        "evidence_quotes": "de71f31b83cac612125d9c2417b83f45c4f82ee8ef10cbd1600d602abf261bcc",
    },
    {
        **_LEGACY_BASE,
        "evidence_compiler": "38b378aab4024e13bb00017353ab71669654be6748eb15e48aaccf81c5c3afc2",
        "evidence_quotes": "de71f31b83cac612125d9c2417b83f45c4f82ee8ef10cbd1600d602abf261bcc",
    },
    {
        **_LEGACY_BASE,
        "evidence_compiler": "7e84bc184c96861287f21855197af0929d20447b77f31898c62147d10d302aad",
    },
)
_LEGACY_COMPILER = "833ae7bc677a7b004b141e689a8ca67974aa39584da2e2791101eacef87f6e27"


class FactCache:
    def __init__(self, checkpoints, system, units, validate):
        from openkb.agent.shared_analysis import SharedAnalysis

        self.shared = SharedAnalysis(checkpoints, "facts", system)
        self.shared_hits = set()
        self.checkpoints, self.system, self.validate = checkpoints, system, validate
        self.units = {unit["id"]: unit for unit in units}
        self.order = {unit["id"]: index for index, unit in enumerate(units)}
        self.rows = {}
        self.contexts = {}
        self._restore()

    def set_batch(self, units):
        from openkb.agent.shared_analysis import fact_input

        context = [fact_input(unit) for unit in units]
        identity = self.shared.input_reference(context)
        for index, unit in enumerate(units):
            self.contexts[unit["id"]] = {"input": identity, "occurrence": index}

    def payload(self, unit):
        from openkb.agent.shared_analysis import fact_input

        return {"unit": fact_input(unit), "batch": self.contexts[unit["id"]]}

    def _key(self, units):
        return self.checkpoints.key(self.system, {"stage": "facts", "units": units})

    def _restore(self):
        cp = self.checkpoints
        keys = cp.checkpoint_keys("facts")
        if not isinstance(keys, list):
            raise ValueError("Invalid fact checkpoint index")
        for key in keys:
            processing_checkpoint("facts")
            record = cp.record(key)
            if record is None:
                continue
            from openkb.agent.table_recovery import restore_prose

            restore_prose(self, key, record)
            value = record["value"]
            rows = value.get("units") if isinstance(value, dict) else None
            if not isinstance(rows, list) or not rows:
                continue
            ids = [row.get("id") for row in rows if isinstance(row, dict)]
            if len(ids) != len(rows) or any(
                not isinstance(uid, str) or uid not in self.units for uid in ids
            ):
                continue
            if len(set(ids)) != len(ids):
                continue
            units = [self.units[uid] for uid in sorted(ids, key=self.order.__getitem__)]
            contract = cp._key_record(self.system, {"stage": "facts", "units": units})
            accepted = key in {
                content_id(contract),
                cp.previous_fact_key(self.system, {"stage": "facts", "units": units}),
            }
            if (
                not accepted
                and contract["implementation"] == _LEGACY_COMPILER
                and module_revision("openkb.agent.evidence_units") == _LEGACY_BASE["evidence_units"]
            ):
                accepted = any(
                    content_id({**contract, "stage_implementation": old}) == key
                    for old in _LEGACY_CONTRACTS
                )
            if not accepted:
                continue  # A unit ID alone never authorizes using another execution contract.
            for row in rows:
                try:
                    self.validate(self.units[row["id"]], row)
                except ResponseIncomplete:
                    continue  # Old extraction rules may have admitted an unsupported empty unit.
                self.rows.setdefault(row["id"], row)
        from openkb.agent.fact_resume import restore_unchanged

        restore_unchanged(self, keys)

    def get(self, unit):
        row = self.rows.get(unit["id"])
        if row is not None:
            return row
        value = self.checkpoints.load(self._key([unit]))
        if value is not None:
            rows = value.get("units") if isinstance(value, dict) else None
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
                or rows[0].get("id") != unit["id"]
            ):
                raise ValueError("Invalid single-unit checkpoint")
            self.validate(unit, rows[0])
            return rows[0]
        payload = self.payload(unit)
        output_tokens = self.shared.output_tokens()
        shared = self.shared.load(payload, output_tokens=output_tokens)
        if shared is not None:
            row = {**shared, "id": unit["id"]}
            try:
                self.validate(unit, row)  # Rebind every quotation and context to current originals.
            except ResponseIncomplete:
                return None
            self.shared.bind(payload, unit, output_tokens=output_tokens)
            self.rows[unit["id"]] = row
            self.shared_hits.add(unit["id"])
            return row
        return None

    def save(self, units, rows, *, receipt=None):
        for unit, row in zip(units, rows, strict=True):
            self.validate(unit, row)
        self.checkpoints.save(self._key(units), {"units": rows}, receipt=receipt)
        self.rows.update({row["id"]: row for row in rows})
        actual = getattr(receipt, "output_tokens", None)
        if type(actual) is not int or actual <= 0:
            return
        for unit, row in zip(units, rows, strict=True):
            if not row["facts"]:
                # An empty result can depend on another occurrence in its batch.
                # It remains recoverable for this source, never proof for another.
                continue
            payload = self.payload(unit)
            self.shared.save(
                payload,
                {key: value for key, value in row.items() if key != "id"},
                output_tokens=actual,
            )
            self.shared.bind(payload, unit, output_tokens=actual)
