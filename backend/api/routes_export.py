"""Download endpoints. Always the marketing schema, always filtered."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from ..db.models import OUTREACH_VALUES, STREAM_VALUES
from .deps import export_rate_limit, get_connection
from .export import build_export_frame, export_filename, to_csv_bytes, to_excel_bytes

router = APIRouter(prefix="/api/export", tags=["export"])

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("", dependencies=[Depends(export_rate_limit)])
def export_colleges(
    format: Literal["xlsx", "csv"] = "xlsx",
    state: str | None = Query(None, max_length=80),
    stream: str | None = Query(None, max_length=20),
    outreach_status: str | None = Query(None, max_length=30),
    search: str | None = Query(None, max_length=120),
    conn=Depends(get_connection),
) -> Response:
    """Download the currently filtered, complete rows.

    Note what is NOT a parameter: `view` and `status`. The export is the
    marketing schema whichever UI triggered it, and the completeness filter is
    applied server-side regardless of what was on screen.
    """
    if stream and stream not in STREAM_VALUES:
        stream = None
    if outreach_status and outreach_status not in OUTREACH_VALUES:
        outreach_status = None

    frame = build_export_frame(
        conn, state=state, stream=stream,
        outreach_status=outreach_status, search=search,
    )

    if format == "csv":
        body, media = to_csv_bytes(frame), "text/csv; charset=utf-8"
    else:
        body, media = to_excel_bytes(frame), _XLSX_MEDIA

    filename = export_filename(state, stream, format)
    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Lets the browser read the row count without parsing the file.
            "X-Row-Count": str(len(frame)),
        },
    )
