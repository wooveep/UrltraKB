"""Pinned openpyxl sparse reader, isolated from its dense row/merge APIs.

WorkSheetParser.parse yields only stored cells. ReadOnlyWorksheet.iter_rows fills
missing rows and columns, while ordinary worksheets expand merged ranges. Neither
is suitable for sparse source evidence. The private API is pinned and exercised
through real workbook imports; no macros, formula calculation, or link updates run.
"""

from datetime import date, datetime, time, timedelta
from pathlib import Path

from openkb.workbooks.records import SheetCell, SheetSnapshot
from openkb.workbooks.values import display_value


def scalar(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError(f"Unsupported cell value: {type(value).__name__}")


def read_xlsx(path: Path) -> tuple[SheetSnapshot, ...]:
    from openpyxl.reader.excel import ExcelReader
    from openpyxl.styles.stylesheet import apply_stylesheet

    reader = ExcelReader(path, read_only=True, data_only=False, keep_links=False)
    try:
        reader.read_manifest()
        reader.read_strings()
        reader.read_workbook()
        apply_stylesheet(reader.archive, reader.wb)
        entries = list(reader.parser.find_sheets())
        declared = reader.parser.sheets
        if len(entries) != len(declared):
            raise ValueError("Workbook sheet inventory is incomplete")
        names, identities = set(), set()
        for sheet, rel in entries:
            if (
                sheet.name in names
                or sheet.sheetId in identities
                or rel.target not in reader.valid_files
            ):
                raise ValueError("Workbook sheet inventory has duplicate or missing entries")
            names.add(sheet.name)
            identities.add(sheet.sheetId)
        result = []
        for ordinal, (sheet, rel) in enumerate(entries, 1):
            basis = {
                "key": f"xlsx:{sheet.sheetId}",
                "native_id": str(sheet.sheetId),
                "name": sheet.name,
                "ordinal": ordinal,
                "state": sheet.state,
            }
            try:
                if not rel.Type.endswith("/worksheet"):
                    raise ValueError("Non-cell chart sheet has no extracted cell evidence")
                result.append(_read_sheet(reader, rel.target, basis))
            except Exception as exc:
                result.append(SheetSnapshot(**basis, error=f"{type(exc).__name__}: {exc}"))
        return tuple(result)
    finally:
        reader.archive.close()


def _read_sheet(reader, path, basis):
    from openpyxl.cell.read_only import ReadOnlyCell
    from openpyxl.worksheet._reader import WorkSheetParser

    def parse(data_only):
        with reader.archive.open(path) as stream:
            parser = WorkSheetParser(
                stream,
                reader.shared_strings,
                data_only=data_only,
                epoch=reader.wb.epoch,
                date_formats=reader.wb._date_formats,
                timedelta_formats=reader.wb._timedelta_formats,
            )
            cells = [cell for _, row in parser.parse() for cell in row]
        return parser, cells

    parser, formulas = parse(False)
    _, values = parse(True)
    cached = {(cell["row"], cell["column"]): cell for cell in values}
    if len(cached) != len(formulas):
        raise ValueError("Duplicate or inconsistent worksheet coordinates")
    hidden_rows = tuple(
        sorted(
            int(row)
            for row, info in parser.row_dimensions.items()
            if info.get("hidden") in (True, "1", "true")
        )
    )
    hidden_columns = tuple(
        (int(info["min"]), int(info["max"]))
        for info in parser.column_dimensions.values()
        if info.get("hidden") in (True, "1", "true")
    )
    cells = []
    # ReadOnlyCell uses the workbook only for the pinned style tables.
    from types import SimpleNamespace

    parent = SimpleNamespace(parent=reader.wb)
    for value in formulas:
        cell = ReadOnlyCell(parent, **value)
        if cell.value is None:
            continue  # Styles without values cannot make a dense data rectangle.
        formula = None
        formula_status = "not_formula"
        cached_value = None
        cache_status = "not_formula"
        if cell.data_type == "f":
            formula = (
                cell.value if isinstance(cell.value, str) else getattr(cell.value, "text", None)
            )
            formula_status = "available" if formula is not None else "unavailable"
            cached_value = scalar(cached[(cell.row, cell.column)]["value"])
            cache_status = "available" if cached_value is not None else "missing"
            actual = formula
        else:
            actual = scalar(cell.value)
        display = display_value(actual, cell.number_format)
        cells.append(
            SheetCell(
                coordinate=cell.coordinate,
                row=cell.row,
                column=cell.column,
                value=actual,
                data_type=cell.data_type,
                number_format=cell.number_format,
                display=display,
                formula=formula,
                formula_status=formula_status,
                cached=cached_value,
                cache_status=cache_status,
                hidden_row=cell.row in hidden_rows,
                hidden_column=any(a <= cell.column <= b for a, b in hidden_columns),
            )
        )
    from defusedxml.ElementTree import fromstring

    tree = fromstring(reader.archive.read(path), forbid_dtd=True)
    has_objects = any(
        element.tag.rsplit("}", 1)[-1]
        in {
            "drawing",
            "legacyDrawing",
            "drawingHF",
            "legacyDrawingHF",
            "oleObjects",
            "picture",
            "controls",
            "extLst",
        }
        for element in tree.iter()
    )
    return SheetSnapshot(
        **basis,
        cells=tuple(sorted(cells, key=lambda cell: (cell.row, cell.column))),
        merged_ranges=tuple(str(item.ref) for item in parser.merged_cells.mergeCell)
        if parser.merged_cells
        else (),
        hidden_rows=hidden_rows,
        hidden_columns=hidden_columns,
        has_objects=has_objects,
    )
