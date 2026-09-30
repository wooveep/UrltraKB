"""Sparse worksheet evidence; coordinates refer to cells, never binary file offsets."""

from typing import Literal

from pydantic import Field, model_validator

from openkb.source_records import Digest, Record, RecordId

WORKBOOK_POLICY = "sparse-worksheet-openpyxl-3.1.5-v1"


class SheetCell(Record):
    coordinate: str
    row: int = Field(ge=1, le=1048576)
    column: int = Field(ge=1, le=16384)
    value: str | int | float | bool | None
    data_type: str
    number_format: str = "General"
    display: str = ""
    formula: str | None = None
    cached: str | int | float | bool | None = None
    cache_status: Literal["not_formula", "available", "missing"] = "not_formula"
    formula_status: Literal["not_formula", "available", "unavailable"] = "not_formula"
    hidden_row: bool = False
    hidden_column: bool = False

    @model_validator(mode="after")
    def validate_coordinate(self) -> "SheetCell":
        from openpyxl.utils.cell import get_column_letter

        if self.coordinate != f"{get_column_letter(self.column)}{self.row}":
            raise ValueError("Cell coordinate does not match its physical row and column")
        if self.formula_status == "available" and self.formula is None:
            raise ValueError("Available formula requires an expression")
        if self.cache_status == "available" and self.cached is None:
            raise ValueError("Available formula cache requires a stored value")
        return self


class SheetCellLocation(Record):
    sheet_key: str
    sheet_name: str
    cell: SheetCell


class SheetSnapshot(Record):
    key: str
    native_id: str | None
    name: str
    ordinal: int = Field(ge=1)
    state: Literal["visible", "hidden", "veryHidden"]
    cells: tuple[SheetCell, ...] = ()
    merged_ranges: tuple[str, ...] = ()
    hidden_rows: tuple[int, ...] = ()
    hidden_columns: tuple[tuple[int, int], ...] = ()
    has_objects: bool = False
    error: str | None = None
    diagnostics: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_cells(self) -> "SheetSnapshot":
        if (
            not self.key
            or not self.name
            or len({cell.coordinate for cell in self.cells}) != len(self.cells)
        ):
            raise ValueError("Sheet identity and cell coordinates must be unique and nonempty")
        if any(not 1 <= row <= 1048576 for row in self.hidden_rows) or any(
            not 1 <= start <= end <= 16384 for start, end in self.hidden_columns
        ):
            raise ValueError("Hidden dimensions are outside worksheet bounds")
        from openpyxl.utils.cell import range_boundaries

        for area in self.merged_ranges:
            left, top, right, bottom = range_boundaries(area)
            if (
                any(type(value) is not int for value in (left, top, right, bottom))
                or not 1 <= left <= right <= 16384
                or not 1 <= top <= bottom <= 1048576
            ):
                raise ValueError("Merged range is outside worksheet bounds")
        return self


class WorkbookSnapshot(Record):
    source_revision_id: RecordId
    digest: Digest
    policy: str = WORKBOOK_POLICY
    sheets: tuple[SheetSnapshot, ...]
    error: str | None = None

    @model_validator(mode="after")
    def validate_inventory(self) -> "WorkbookSnapshot":
        if not self.sheets and not self.error:
            raise ValueError("Workbook inventory contains no worksheets")
        for values in (
            [sheet.key for sheet in self.sheets],
            [sheet.name for sheet in self.sheets],
            [sheet.ordinal for sheet in self.sheets],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Workbook inventory has duplicate sheet identities")
        if self.error and self.sheets:
            raise ValueError("Failed inventory cannot claim a complete sheet list")
        return self
