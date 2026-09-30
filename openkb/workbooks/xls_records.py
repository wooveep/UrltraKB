"""Pinned xlrd 2.0.2 record observations, without global monkeypatches or evaluation.

The loader follows xlrd.book.open_workbook_xls using a Book subclass so all BIFF
versions retain formula presence before xlrd discards expressions. xlrd owns BIFF
and CFB parsing; observations only identify formula cells and non-cell content.
"""

# Loader adapted from xlrd.book, Copyright (c) 2005-2012 Stephen John Machin,
# Lingfo Pty Ltd. BSD-style terms are retained in xlrd-LICENSE.txt.

import io
from struct import unpack_from

from xlrd.biffh import (
    XL_FORMULA_OPCODES,
    XL_MSO_DRAWING,
    XL_OBJ,
    XL_WORKBOOK_GLOBALS,
    unpack_string,
    unpack_unicode,
)
from xlrd.book import SUPPORTED_VERSIONS, Book


class ObservedBook(Book):
    def __init__(self):
        super().__init__()
        self.active_sheet = None
        self.formulas = {}
        self.objects = set()
        self.inventory = []

    def get_sheet(self, sh_number, update_pos=True):
        previous = self.active_sheet
        self.active_sheet = sh_number
        self.formulas.setdefault(sh_number, set())
        try:
            return super().get_sheet(sh_number, update_pos)
        finally:
            self.active_sheet = previous

    def get_record_parts(self):
        code, size, data = super().get_record_parts()
        if len(data) != size:
            raise ValueError("Truncated BIFF record")
        if self.active_sheet is not None:
            if code in XL_FORMULA_OPCODES:
                coordinate = unpack_from("<HH", data)
                if coordinate in self.formulas[self.active_sheet]:
                    raise ValueError("Duplicate formula coordinate")
                self.formulas[self.active_sheet].add(coordinate)
            if code in {XL_OBJ, XL_MSO_DRAWING}:
                self.objects.add(self.active_sheet)
        return code, size, data

    def handle_boundsheet(self, data):
        super().handle_boundsheet(data)
        if self.biff_version == 45:
            name, visibility, kind = unpack_string(data, 0, self.encoding, lenlen=1), 0, 0
        else:
            visibility, kind = data[4], data[5]
            name = (
                unpack_unicode(data, 6, lenlen=1)
                if self.biff_version >= 80
                else unpack_string(data, 6, self.encoding, lenlen=1)
            )
        self.inventory.append((name, visibility, kind, self._all_sheets_map[-1]))


def open_observed_book(path):
    book = ObservedBook()
    try:
        book.biff2_8_load(
            filename=str(path),
            logfile=io.StringIO(),
            formatting_info=True,
            on_demand=True,
            ragged_rows=True,
            ignore_workbook_corruption=False,
        )
        version = book.getbof(XL_WORKBOOK_GLOBALS)
        if version not in SUPPORTED_VERSIONS:
            raise ValueError(f"Unsupported XLS BIFF version: {version}")
        book.biff_version = version
        if version <= 40:
            book.on_demand = False
            book.fake_globals_get_sheet()
        else:
            book.parse_globals()
            if version == 45:
                book.on_demand = False
            else:
                book._sheet_list = [None for _ in book._sheet_names]
        book.nsheets = len(book._sheet_list)
        if not book.inventory:
            book.inventory = [(name, 0, 0, index) for index, name in enumerate(book.sheet_names())]
        return book
    except BaseException:
        book.release_resources()
        raise
