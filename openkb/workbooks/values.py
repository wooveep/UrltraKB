"""Conservative display of stored cell values; never evaluate Excel expressions."""

import re


def display_value(value, number_format: str) -> str:
    mask = re.sub(r"\[\$-[0-9A-Fa-f]+\]", "", number_format)
    integer = type(value) is int or (type(value) is float and value.is_integer())
    if integer and re.fullmatch(r"0+", mask):
        return ("-" if value < 0 else "") + str(abs(int(value))).zfill(len(mask))
    return str(value) if value is not None else ""
