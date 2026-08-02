"""Scrape and seed-build job endpoints.

Jobs run in the background and write to SQLite as they go, so a run that dies
partway keeps the colleges it already finished. Progress is polled from
scrape_runs rather than held in memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db.models import STREAM_VALUES
from ..db.repository import finish_run, get_college, start_run, update_run, upsert_college
from ..db.session import get_conn
from ..scraper.ollagraph_client import CreditCapExceeded, OllagraphClient, OllagraphError
from ..scraper.pipeline import process_college
from ..scraper.seed_builder import SeedCollege, build_seed_list
from .deps import get_connection, scrape_rate_limit, seed_rate_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["jobs"])


class SeedRequest(BaseModel):
    state: str = Field(min_length=2, max_length=80)
    stream: str = Field(default="Engineering")
    force_refresh: bool = False


class ScrapeRequest(BaseModel):
    state: str = Field(min_length=2, max_length=80)
    stream: str = Field(default="Engineering")
    limit: int = Field(default=25, ge=1, le=500)
    #: Colleges already scraped successfully are skipped unless this is set —
    #: AGENTS.md cost rule: don't re-scrape what already succeeded.
    force_refresh: bool = False


def _validate_stream(stream: str) -> str:
    if stream not in STREAM_VALUES:
        raise HTTPException(422, f"stream must be one of {STREAM_VALUES}")
    return stream


@router.post("/seed/build", dependencies=[Depends(seed_rate_limit)])
async def build_seed(request: SeedRequest) -> dict[str, Any]:
    """Build (or load from cache) the master college list for a state+stream."""
    _validate_stream(request.stream)
    try:
        colleges, meta = await build_seed_list(
            request.state, request.stream, force_refresh=request.force_refresh  # type: ignore[arg-type]
        )
    except CreditCapExceeded as exc:
        raise HTTPException(429, f"credit cap reached: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OllagraphError as exc:
        raise HTTPException(502, f"Ollagraph error: {exc}") from exc

    return {
        "state": request.state,
        "stream": request.stream,
        "count": len(colleges),
        "meta": meta,
        "sample": [c.college_name for c in colleges[:10]],
    }


async def _run_scrape(state: str, stream: str, limit: int, force_refresh: bool) -> None:
    """Background worker: seed -> pipeline -> store, one college at a time."""
    seeds, _ = await build_seed_list(state, stream)  # type: ignore[arg-type]
    if not seeds:
        log.warning("scrape run for %s/%s has an empty seed list", state, stream)
        return

    with get_conn() as conn:
        run_id = start_run(conn, state, stream, total=min(len(seeds), limit))

    processed = succeeded = failed = 0
    try:
        async with OllagraphClient(concurrency=3) as client:
            for seed in seeds[:limit]:
                try:
                    result = await process_college(client, seed)
                except CreditCapExceeded:
                    with get_conn() as conn:
                        finish_run(conn, run_id, "cancelled", "credit cap reached")
                    return
                except Exception as exc:  # noqa: BLE001 — one college must not kill the run
                    log.exception("pipeline failed for %s", seed.college_name)
                    processed += 1
                    failed += 1
                    continue

                with get_conn() as conn:
                    upsert_college(conn, {
                        "college_name": result.college_name,
                        "state": result.state,
                        "stream": result.stream,
                        "district": result.district,
                        "affiliation": result.affiliation,
                        "website": result.website,
                        "placement_officer_name": result.placement_officer_name,
                        "placement_email": result.placement_email,
                        "placement_phone": result.placement_phone,
                        "backup_emails_found": result.backup_emails_found,
                        "backup_phones_found": result.backup_phones_found,
                        "fallback_contact_email": result.fallback_contact_email,
                        "fallback_contact_phone": result.fallback_contact_phone,
                        "confidence_score": result.confidence_score,
                        "source_urls": result.source_urls,
                        "email_verified": result.email_verified,
                        "last_scraped": result.last_scraped,
                        "status": result.status,
                    })
                    processed += 1
                    if result.status == "Failed":
                        failed += 1
                    else:
                        succeeded += 1
                    update_run(conn, run_id, processed=processed,
                               succeeded=succeeded, failed=failed)

        with get_conn() as conn:
            finish_run(conn, run_id, "completed")
    except Exception as exc:  # noqa: BLE001
        log.exception("scrape run %d crashed", run_id)
        with get_conn() as conn:
            finish_run(conn, run_id, "failed", str(exc)[:500])


@router.post("/scrape/run", dependencies=[Depends(scrape_rate_limit)])
async def start_scrape(
    request: ScrapeRequest, background: BackgroundTasks,
) -> dict[str, Any]:
    """Kick off a scrape run in the background and return immediately."""
    _validate_stream(request.stream)
    background.add_task(
        _run_scrape, request.state, request.stream, request.limit, request.force_refresh
    )
    return {
        "started": True,
        "state": request.state,
        "stream": request.stream,
        "limit": request.limit,
    }


@router.post("/scrape/college/{college_id}", dependencies=[Depends(scrape_rate_limit)])
async def rescrape_college(college_id: int, conn=Depends(get_connection)) -> dict[str, Any]:
    """Re-scrape one college — the detail page's "re-scrape" button."""
    row = get_college(conn, college_id)
    if row is None:
        raise HTTPException(404, "college not found")

    seed = SeedCollege(
        college_name=row["college_name"], state=row["state"], stream=row["stream"],
        district=row["district"] or "", website=row["website"] or "",
        affiliation=row["affiliation"] or "",
    )

    try:
        async with OllagraphClient(concurrency=3) as client:
            result = await process_college(client, seed)
    except CreditCapExceeded as exc:
        raise HTTPException(429, f"credit cap reached: {exc}") from exc
    except OllagraphError as exc:
        raise HTTPException(502, f"Ollagraph error: {exc}") from exc

    upsert_college(conn, {
        "college_name": result.college_name, "state": result.state,
        "stream": result.stream, "district": result.district,
        "affiliation": result.affiliation, "website": result.website,
        "placement_officer_name": result.placement_officer_name,
        "placement_email": result.placement_email,
        "placement_phone": result.placement_phone,
        "backup_emails_found": result.backup_emails_found,
        "backup_phones_found": result.backup_phones_found,
        "fallback_contact_email": result.fallback_contact_email,
        "fallback_contact_phone": result.fallback_contact_phone,
        "confidence_score": result.confidence_score,
        "source_urls": result.source_urls, "email_verified": result.email_verified,
        "last_scraped": result.last_scraped, "status": result.status,
    })
    return {"college_id": college_id, "status": result.status, "notes": result.notes}


@router.get("/scrape/runs")
def list_runs(limit: int = 20, conn=Depends(get_connection)) -> dict[str, Any]:
    """Recent run history — admin/QA only."""
    rows = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (min(limit, 100),)
    ).fetchall()
    return {"runs": [dict(row) for row in rows]}
