# Dumps the Morning Prayer song-log sheet WITH cell background colors.
#
# The praise sheet marks absences by color, not by text. Everything else
# in this codebase reads Sheets through the values API, which returns
# contents only and cannot see formatting at all, so those absences have
# been invisible to the code since it was written. The few that
# registered were the ones someone also typed into the Comments column.
#
# This exists to map color to meaning from the sheet itself rather than
# by guessing hex codes. Run it, read the legend block it prints, and
# the colors on the weekly rows become interpretable.
#
# Usage, from the repo root with the venv active and a .env present:
#
#   python -m scripts.dump_worship_colors
#   python -m scripts.dump_worship_colors --rows 80
#   python -m scripts.dump_worship_colors --all-cells
#
# Reads only. Writes nothing, sends nothing.

from __future__ import annotations

import argparse
import sys

import google.auth
from googleapiclient.discovery import build

from functions.prayer.morning import (
    WORSHIP_SHEET_ID,
    WORSHIP_TAB,
    WORSHIP_TAB_FALLBACK,
)

_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Google returns absent channels rather than zeros, so an unset
# background is {} and has to be read as white.
_WHITE = (1.0, 1.0, 1.0)


def _client():
    credentials, _ = google.auth.default(scopes=[_SCOPE])
    return build("sheets", "v4", credentials=credentials)


def _rgb(cell: dict) -> tuple[float, float, float]:
    fmt = (cell or {}).get("effectiveFormat") or {}
    bg = fmt.get("backgroundColor") or {}
    return (
        round(bg.get("red", 1.0), 3),
        round(bg.get("green", 1.0), 3),
        round(bg.get("blue", 1.0), 3),
    )


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{int(round(c * 255)):02X}" for c in rgb)


def _col_letter(index: int) -> str:
    letters, index = "", index + 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=60, help="how many rows to read")
    parser.add_argument(
        "--all-cells",
        action="store_true",
        help="print every cell, not only the colored ones",
    )
    args = parser.parse_args()

    service = _client()
    grid = None
    used_tab = None
    for tab in (WORSHIP_TAB, WORSHIP_TAB_FALLBACK):
        try:
            result = (
                service.spreadsheets()
                .get(
                    spreadsheetId=WORSHIP_SHEET_ID,
                    ranges=[f"'{tab}'!A1:L{args.rows}"],
                    includeGridData=True,
                )
                .execute()
            )
            grid = result["sheets"][0]["data"][0].get("rowData", [])
            used_tab = tab
            break
        except Exception as exc:
            print(f"  (tab {tab!r} failed: {exc})")

    if grid is None:
        print("Could not read the worship sheet at all.")
        return 1

    print(f"Tab: {used_tab}   rows read: {len(grid)}")
    print("=" * 78)

    palette: dict[str, list[str]] = {}

    for row_index, row in enumerate(grid, start=1):
        cells = row.get("values", []) or []
        printable = []
        for col_index, cell in enumerate(cells):
            value = (cell or {}).get("formattedValue", "")
            rgb = _rgb(cell)
            colored = rgb != _WHITE
            if not value and not colored:
                continue
            if colored:
                palette.setdefault(_hex(rgb), []).append(
                    f"{_col_letter(col_index)}{row_index}"
                    + (f" {value!r}" if value else " (empty)")
                )
            if colored or args.all_cells:
                tag = _hex(rgb) if colored else "        "
                printable.append(
                    f"{_col_letter(col_index)}{row_index}={value!r} [{tag}]"
                )
        if printable:
            print(f"row {row_index:>3}: " + "  ".join(printable))

    print()
    print("COLORS FOUND, and where")
    print("=" * 78)
    for color, where in sorted(palette.items(), key=lambda kv: -len(kv[1])):
        print(f"  {color}  ({len(where)} cells)")
        for location in where[:12]:
            print(f"      {location}")
        if len(where) > 12:
            print(f"      ... and {len(where) - 12} more")
    print()
    print("Match these against the legend block (ABSENCE / NEED SUB / OTHER SUB)")
    print("to learn which color means what.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
