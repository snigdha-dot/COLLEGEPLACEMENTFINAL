"""FastAPI entrypoint.

Run with:
    venv/Scripts/python.exe -m uvicorn backend.main:app --reload
"""

from __future__ import annotations

import logging
import os
import re
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

#: Any private-network origin, matched by pattern rather than a fixed address.
#: The LAN IP is assigned by DHCP and changes on reconnect (192.168.7.16 ->
#: 192.168.240.225 mid-session), which silently broke every client: browsers
#: got a CORS rejection and the table rendered empty. Matching the RFC1918
#: ranges keeps it working across reassignment without resorting to "*", which
#: would let any site a colleague visits read this API.
_LAN_ORIGIN = re.compile(
    r"^https?://("
    r"localhost|127\.0\.0\.1"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")(:\d+)?$"
)

log.warning(
    "CORS accepts any private-network origin. This app has NO authentication "
    "and serves contact PII — anyone on this WiFi can read and export it, and "
    "the admin view can edit records and trigger billed scrapes. Add auth "
    "before any wider rollout (AGENTS.md)."
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_origin_regex=_LAN_ORIGIN.pattern,
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
