"""College list/search/filter endpoints, split by view.

Two views over the same table:

  view=marketing  clean schema, complete rows only, no pipeline internals
  view=admin      full internal record, for QA

The split is enforced in the repository layer, not here — this module chooses
which function to call and never assembles a payload by hand, so there is no
route through which an internal column can reach a marketing response.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db.models import OUTREACH_VALUES, STATUS_VALUES, STREAM_VALUES
from ..db.repository import (
    SORTABLE_COLUMNS,
    admin_rows,
    counts_by_status,
    get_college,
    marketing_rows,
    update_college,
)
from .deps import get_connection

router = APIRouter(prefix="/api/colleges", tags=["colleges"])


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


@router.get("")
def list_colleges(
    view: Literal["marketing", "admin"] = "marketing",
    state: str | None = Query(None, max_length=80),
    stream: str | None = Query(None, max_length=20),
    status: str | None = Query(None, max_length=30),
    outreach_status: str | None = Query(None, max_length=30),
    search: str | None = Query(None, max_length=120),
    sort: str | None = Query(None, max_length=40),
    direction: Literal["asc", "desc"] = "asc",
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    conn=Depends(get_connection),
) -> dict[str, Any]:
    """List colleges for the requested view.

    `status` is accepted only for the admin view. Silently ignoring it for
    marketing would be worse than rejecting it: a caller who thinks they are
    filtering by status but is not would misread the results.
    """
    if stream and stream not in STREAM_VALUES:
        raise HTTPException(422, f"stream must be one of {STREAM_VALUES}")
    if status and status not in STATUS_VALUES:
        raise HTTPException(422, f"status must be one of {STATUS_VALUES}")
    if outreach_status and outreach_status not in OUTREACH_VALUES:
        raise HTTPException(422, f"outreach_status must be one of {OUTREACH_VALUES}")
    if sort and sort not in SORTABLE_COLUMNS:
        raise HTTPException(422, f"sort must be one of {sorted(SORTABLE_COLUMNS)}")

    if view == "marketing":
        if status:
            raise HTTPException(
                422, "status is not available in the marketing view"
            )
        rows = marketing_rows(
            conn, state=state, stream=stream, outreach_status=outreach_status,
            search=search, sort=sort, direction=direction, limit=limit, offset=offset,
        )
    else:
        rows = admin_rows(
            conn, state=state, stream=stream, status=status,
            outreach_status=outreach_status, search=search, sort=sort,
            direction=direction, limit=limit, offset=offset,
        )

    return {"view": view, "count": len(rows), "results": _rows_to_dicts(rows)}


@router.get("/stats")
def college_stats(
    state: str | None = Query(None, max_length=80),
    conn=Depends(get_connection),
) -> dict[str, Any]:
    """Pipeline health counts. Admin/QA only — never surfaced in marketing."""
    return {"state": state, "by_status": counts_by_status(conn, state)}


@router.get("/{college_id}")
def college_detail(college_id: int, conn=Depends(get_connection)) -> dict[str, Any]:
    """Full record for the detail page, which is an admin/QA surface."""
    row = get_college(conn, college_id)
    if row is None:
        raise HTTPException(404, "college not found")
    return dict(row)


@router.patch("/{college_id}")
def edit_college(
    college_id: int, changes: dict[str, Any], conn=Depends(get_connection),
) -> dict[str, Any]:
    """Apply a manual correction.

    The repository restricts which fields are writable; anything the pipeline
    owns (status, confidence_score, timestamps) is silently dropped rather
    than applied.
    """
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(422, "request body must be a non-empty object")

    outreach = changes.get("outreach_status")
    if outreach is not None and outreach not in OUTREACH_VALUES:
        raise HTTPException(422, f"outreach_status must be one of {OUTREACH_VALUES}")

    if get_college(conn, college_id) is None:
        raise HTTPException(404, "college not found")

    updated = update_college(conn, college_id, changes)
    if updated == 0:
        raise HTTPException(422, "no editable fields in request")
    return dict(get_college(conn, college_id))
