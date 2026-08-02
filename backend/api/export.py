"""Excel/CSV export — always the marketing schema, always filtered.

There is exactly one export shape, regardless of which UI view triggered it:
the marketing projection with the completeness filter applied. The admin view
can *see* incomplete rows and pipeline status, but it cannot export them —
anything downloaded may end up forwarded to the marketing team, so status,
confidence_score, and last_scraped must not be in a file at all.

The filter and projection come from db/models.py via repository.marketing_rows;
this module only formats. That keeps one definition of "what marketing sees".
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from ..db.models import MARKETING_COLUMNS
from ..db.repository import marketing_rows

#: Human-friendly headers. The DB column names are snake_case; a spreadsheet
#: going to a non-technical team should not be.
COLUMN_LABELS = {
    "college_name": "College Name",
    "state": "State",
    "stream": "Stream",
    "affiliation": "Affiliation",
    "website": "Website",
    "email": "Email",
    "phone": "Phone",
    "all_emails_found": "All Emails Found",
    "all_phones_found": "All Phones Found",
    "outreach_status": "Outreach Status",
}


def export_filename(state: str | None, stream: str | None, extension: str) -> str:
    parts = ["college_contacts"]
    if state:
        parts.append(state.lower().replace(" ", "_"))
    if stream:
        parts.append(stream.lower())
    parts.append(datetime.now(timezone.utc).strftime("%Y%m%d"))
    return f"{'_'.join(parts)}.{extension}"


def _rows_to_frame(rows: list[sqlite3.Row]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [{column: row[column] for column in MARKETING_COLUMNS} for row in rows],
        columns=list(MARKETING_COLUMNS),
    )
    return frame.rename(columns=COLUMN_LABELS)


def build_export_frame(
    conn: sqlite3.Connection, *, state: str | None = None, stream: str | None = None,
    outreach_status: str | None = None, search: str | None = None,
) -> pd.DataFrame:
    """Fetch the exportable rows as a DataFrame.

    Filters mirror the marketing UI so "export what I'm looking at" holds —
    except that completeness is always enforced server-side, whatever the UI
    happened to be showing.
    """
    rows = marketing_rows(
        conn, state=state, stream=stream, outreach_status=outreach_status,
        search=search, limit=100_000,
    )
    return _rows_to_frame(rows)


def to_excel_bytes(frame: pd.DataFrame) -> bytes:
    """Render an .xlsx with sensible column widths."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Colleges")
        worksheet = writer.sheets["Colleges"]
        for index, column in enumerate(frame.columns, start=1):
            longest = max(
                [len(str(column))] + [len(str(v)) for v in frame[column].head(200)]
            )
            worksheet.column_dimensions[
                worksheet.cell(row=1, column=index).column_letter
            ].width = min(max(longest + 2, 12), 55)
    return buffer.getvalue()


def to_csv_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.StringIO()
    # QUOTE_MINIMAL would still be correct, but the comma-joined
    # all_emails_found column makes quoting the common case anyway.
    frame.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
    # utf-8-sig so Excel opens Indian names and ₹ symbols correctly on Windows.
    return buffer.getvalue().encode("utf-8-sig")
