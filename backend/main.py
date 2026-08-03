"""FastAPI entrypoint.

Run with:
    venv/Scripts/python.exe -m uvicorn backend.main:app --reload
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.colleges import router as colleges_router
from .api.deps import ensure_schema
from .api.routes_export import router as export_router
from .api.scrape import router as jobs_router

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_schema()
    log.info("database ready")
    yield


app = FastAPI(
    title="College Placement Contact Intelligence",
    version="0.1.0",
    description=(
        "Builds a master list of Engineering and BCA colleges per Indian state, "
        "scrapes placement-cell contacts, and serves them to the marketing team."
    ),
    lifespan=lifespan,
)

# Restricted to known frontend origins — never "*". This service holds contact
# PII (AGENTS.md security rule).
#
# FRONTEND_ORIGIN accepts a comma-separated list so the same backend can serve
# both the local browser and other machines on the LAN:
#   FRONTEND_ORIGIN=http://localhost:3000,http://192.168.7.16:3000
# A LAN origin must be named explicitly. Falling back to "*" would let any site
# a colleague happens to visit read this API through their browser.
_origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGIN", "http://localhost:3000").split(",")
    if origin.strip()
]

if any(o.startswith("http://192.168.") or o.startswith("http://10.") for o in _origins):
    log.warning(
        "CORS allows a LAN origin (%s). This app has NO authentication and "
        "serves contact PII — anyone on this network can read and export it. "
        "Add auth before any wider rollout (AGENTS.md).",
        ", ".join(_origins),
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)

app.include_router(colleges_router)
app.include_router(export_router)
app.include_router(jobs_router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
