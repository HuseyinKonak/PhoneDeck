"""CSV cells that spreadsheet programs will not run as formulas.

Message bodies and contact names come from other people. Excel and similar programs
evaluate a cell that starts with `=`, `+`, `-` or `@`, so such cells get a leading
apostrophe, which makes them plain text.
"""

from __future__ import annotations

import re

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
TEXT_MARKER = "'"
# Phone numbers legitimately start with "+"; digits and separators alone cannot do harm.
_PHONE_LIKE = re.compile(r"[+\-0-9 ();*#]+")


def safe_cell(value: str) -> str:
    if value.startswith(FORMULA_PREFIXES) and not _PHONE_LIKE.fullmatch(value):
        return TEXT_MARKER + value
    return value


def safe_row(row: list[str]) -> list[str]:
    return [safe_cell(str(cell)) for cell in row]
