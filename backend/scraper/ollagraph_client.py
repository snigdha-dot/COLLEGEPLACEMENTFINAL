"""Thin async wrapper around the Ollagraph endpoints this project uses.

Contract verified against https://api.ollagraph.com/openapi.json (spec version
1.0.0, checked 2026-08-02) rather than assumed — see context.md "Ollagraph
notes" for what that check changed.

Billing: successful calls cost 1 credit (some premium endpoints 3–5);
server-side failures refund automatically. Every response carries
x-credits-cost / x-credits-charged / x-credits-balance, which this client
records so a run's true cost is observable rather than estimated.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

BASE_URL = os.getenv("OLLAGRAPH_BASE_URL", "https://api.ollagraph.com")

ScrapeFormat = Literal["markdown", "html", "text", "links"]


class OllagraphError(RuntimeError):
    """An Ollagraph call failed in a way the caller should handle."""

    def __init__(self, message: str, *, status: int | None = None, endpoint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


class OllagraphAuthError(OllagraphError):
    """401/403 — key missing, invalid, or out of credits. Never retry."""


class CreditCapExceeded(OllagraphError):
    """The run hit its configured credit ceiling and was aborted.

    Deliberately fatal: a runaway loop is exactly the failure this guards
    against, so it stops the run rather than logging and continuing.
    """


class UpstreamActorError(OllagraphError):
    """An actor endpoint returned HTTP 200 with ok=false.

    Ollagraph's Apify-backed actors (notably gmaps) report upstream failures
    in the body rather than the status code, so a 200 is not proof of success.
    Confirmed 2026-08-02: gmaps returned `apify HTTP 402:
    not-enough-usage-to-run-paid-actor` this way, charging 30 credits per call
    (refunded asynchronously). Callers must treat this as a failure.
    """


@dataclass
class CreditLedger:
    """Running tally of what a session actually spent.

    The brief treats cost as a real control, so spend is measured from response
    headers rather than inferred from a call count.
    """

    calls: int = 0
    credits_charged: float = 0.0
    balance: float | None = None
    per_endpoint: dict[str, int] = field(default_factory=dict)
    per_endpoint_credits: dict[str, float] = field(default_factory=dict)

    def record(self, endpoint: str, headers: httpx.Headers) -> None:
        self.calls += 1
        self.per_endpoint[endpoint] = self.per_endpoint.get(endpoint, 0) + 1
        try:
            charged = float(headers.get("x-credits-charged", 0) or 0)
            self.credits_charged += charged
            self.per_endpoint_credits[endpoint] = (
                self.per_endpoint_credits.get(endpoint, 0.0) + charged
            )
        except ValueError:
            pass
        raw_balance = headers.get("x-credits-balance")
        if raw_balance:
            try:
                self.balance = float(raw_balance)
            except ValueError:
                pass

    def summary(self) -> str:
        top = sorted(self.per_endpoint.items(), key=lambda kv: -kv[1])
        breakdown = ", ".join(
            f"{ep}={n}@{self.per_endpoint_credits.get(ep, 0):g}cr" for ep, n in top
        )
        bal = f"{self.balance:g}" if self.balance is not None else "unknown"
        return (
            f"{self.calls} calls, {self.credits_charged:g} credits charged, "
            f"balance {bal} | {breakdown}"
        )


class OllagraphClient:
    """Async client. Use as a context manager so the connection pool closes.

    Concurrency is bounded by a semaphore: the pipeline fans out across
    hundreds of colleges, and an unbounded fan-out would hammer the API and
    make a runaway spend harder to notice.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        concurrency: int = 5,
        timeout: float = 120.0,
        max_retries: int = 3,
        credit_cap: float | None = None,
    ) -> None:
        # `is None` rather than falsy: an explicitly-passed empty key must fail
        # loudly, not silently fall through to the ambient .env key. Otherwise a
        # test meant to run without credentials picks up the real one and spends.
        self.api_key = api_key if api_key is not None else os.getenv("OLLAGRAPH_API_KEY", "")
        if not self.api_key:
            raise OllagraphAuthError(
                "OLLAGRAPH_API_KEY is not set. Copy .env.example to .env and add the key."
            )
        self.ledger = CreditLedger()
        # Hard ceiling on a single run's spend. Checked before every call, and
        # a breach aborts rather than warns — a runaway loop is the failure
        # this exists to stop. Endpoint costs are not uniform (gmaps observed
        # at 30 credits/call vs 3 for search), so this counts credits, not calls.
        #
        # The check cannot know a call's cost before making it, so the cap can
        # overshoot by at most one call (a 5000 cap may settle near 5030 with
        # gmaps). Sizing the cap with that slack is intended; the alternative
        # is pre-emptively refusing calls that would have fit.
        self.credit_cap = (
            credit_cap if credit_cap is not None
            else float(os.getenv("OLLAGRAPH_CREDIT_CAP", "5000"))
        )
        self._max_retries = max_retries
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> "OllagraphClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # --- transport ---------------------------------------------------------

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded concurrency and retry on transient failures.

        Retries 429 and 5xx with exponential backoff. Never retries 4xx auth
        errors — a bad key will not fix itself and retrying just stalls the run.
        """
        if self.ledger.credits_charged >= self.credit_cap:
            raise CreditCapExceeded(
                f"run aborted: {self.ledger.credits_charged:g} credits charged reaches the "
                f"{self.credit_cap:g} cap. Raise OLLAGRAPH_CREDIT_CAP to continue. "
                f"Spend so far — {self.ledger.summary()}",
                endpoint=endpoint,
            )

        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            async with self._sem:
                try:
                    response = await self._client.post(endpoint, json=payload)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    log.warning("%s transport error (attempt %d): %s", endpoint, attempt + 1, exc)
                    await asyncio.sleep(2**attempt)
                    continue

            if response.status_code in (401, 403):
                raise OllagraphAuthError(
                    f"auth failed ({response.status_code}) — key invalid or out of credits",
                    status=response.status_code,
                    endpoint=endpoint,
                )

            if response.status_code == 429 or response.status_code >= 500:
                last_error = OllagraphError(
                    f"{endpoint} returned {response.status_code}",
                    status=response.status_code,
                    endpoint=endpoint,
                )
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                log.warning("%s %s, retrying in %.0fs", endpoint, response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            # Charged calls only — failures are refunded server-side.
            self.ledger.record(endpoint, response.headers)

            if response.status_code >= 400:
                raise OllagraphError(
                    f"{endpoint} returned {response.status_code}: {response.text[:300]}",
                    status=response.status_code,
                    endpoint=endpoint,
                )
            return response.json()

        raise OllagraphError(
            f"{endpoint} failed after {self._max_retries} attempts: {last_error}",
            endpoint=endpoint,
        )

    # --- discovery ---------------------------------------------------------

    async def search(
        self, query: str, *, limit: int = 10, deep: bool = False,
        engines: list[str] | None = None,
    ) -> dict[str, Any]:
        """General web search. Defaults to bing + duckduckgo server-side.

        Worth noting: because DuckDuckGo is already one of the default engines,
        the "DuckDuckGo fallback" in the brief overlaps with this considerably.
        """
        payload: dict[str, Any] = {"query": query, "limit": limit, "deep": deep}
        if engines:
            payload["engines"] = engines
        return await self._post("/v1/search", payload)

    @staticmethod
    def _check_actor_ok(endpoint: str, response: dict[str, Any]) -> dict[str, Any]:
        """Actors report upstream failure in the body, not the status code."""
        if response.get("ok") is False:
            raise UpstreamActorError(
                f"{endpoint} upstream failure: {str(response.get('error'))[:300]}",
                endpoint=endpoint,
            )
        return response

    async def gmaps_search(
        self, query: str, *, location: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "limit": limit}
        if location:
            payload["location"] = location
        return self._check_actor_ok(
            "/v1/actors/gmaps/search", await self._post("/v1/actors/gmaps/search", payload)
        )

    async def gmaps_place(
        self, *, place_url: str | None = None, cid: str | None = None,
        name: str | None = None, location: str | None = None,
    ) -> dict[str, Any]:
        """Place details — the fallback contact source (phone/address)."""
        payload = {
            k: v for k, v in
            {"place_url": place_url, "cid": cid, "name": name, "location": location}.items()
            if v
        }
        if not payload:
            raise ValueError("gmaps_place needs at least one of place_url/cid/name")
        return self._check_actor_ok(
            "/v1/actors/gmaps/place", await self._post("/v1/actors/gmaps/place", payload)
        )

    # --- fetching ----------------------------------------------------------

    async def scrape(
        self, url: str, *, format: ScrapeFormat = "markdown",
        stealth: bool = True, timeout: int = 30,
    ) -> dict[str, Any]:
        """Fetch one page.

        Use format="html" when the raw markup matters — notably for
        Cloudflare-obfuscated emails, where the data-cfemail attribute is lost
        in markdown/text conversion.

        Per the brief, prefer this over /v1/scrape/batch, which returned page
        titles only rather than usable content.
        """
        return await self._post(
            "/v1/scrape",
            {"url": url, "format": format, "stealth": stealth, "timeout": timeout},
        )

    async def scrape_smart(
        self, url: str, *, format: ScrapeFormat = "markdown", timeout: int = 30
    ) -> dict[str, Any]:
        return await self._post(
            "/v1/scrape/smart", {"url": url, "format": format, "timeout": timeout}
        )

    async def crawl_start(
        self, url: str, *, max_pages: int = 40, depth: int = 3,
        concurrency: int = 5, respect_robots: bool = True,
    ) -> str:
        """Queue a crawl job and return its job_id.

        /v1/crawl is ASYNCHRONOUS — confirmed 2026-08-02. It responds
        {"status": "queued", "job_id": ...} and does no crawling inline. This
        very likely explains the brief's prior finding that crawl "did not
        follow internal links beyond the seed page": the caller read the
        immediate response, which contains no pages at all.

        Results come from get_job(job_id) once status is "completed".
        """
        response = await self._post(
            "/v1/crawl",
            {
                "url": url,
                "max_pages": max_pages,
                "depth": depth,
                "concurrency": concurrency,
                "respect_robots": respect_robots,
            },
        )
        job_id = response.get("job_id")
        if not job_id:
            raise OllagraphError(
                f"/v1/crawl did not return a job_id: {str(response)[:200]}",
                endpoint="/v1/crawl",
            )
        return job_id

    async def get_job(self, job_id: str) -> dict[str, Any]:
        """Fetch an async job's state. GET, and free — no credits charged."""
        async with self._sem:
            response = await self._client.get(f"/v1/jobs/{job_id}")
        if response.status_code in (401, 403):
            raise OllagraphAuthError(
                f"auth failed ({response.status_code}) polling job {job_id}",
                status=response.status_code, endpoint="/v1/jobs",
            )
        if response.status_code >= 400:
            raise OllagraphError(
                f"/v1/jobs/{job_id} returned {response.status_code}",
                status=response.status_code, endpoint="/v1/jobs",
            )
        return response.json()

    async def crawl(
        self, url: str, *, max_pages: int = 40, depth: int = 3,
        concurrency: int = 5, respect_robots: bool = True,
        poll_interval: float = 5.0, timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Crawl a site and wait for the result.

        Convenience wrapper over crawl_start + polling. Returns the job's
        `result` payload, which carries `urls` and `pages_crawled` (NOT `pages`
        or `results` — verified against a live job).
        """
        job_id = await self.crawl_start(
            url, max_pages=max_pages, depth=depth,
            concurrency=concurrency, respect_robots=respect_robots,
        )

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            job = await self.get_job(job_id)
            status = job.get("status")
            if status == "completed":
                return job.get("result") or {}
            if status in ("failed", "error", "cancelled"):
                raise OllagraphError(
                    f"crawl job {job_id} ended as {status}: {str(job)[:200]}",
                    endpoint="/v1/crawl",
                )

        raise OllagraphError(
            f"crawl job {job_id} did not finish within {timeout:g}s",
            endpoint="/v1/crawl",
        )

    # --- extraction --------------------------------------------------------

    async def extract_contacts(
        self, text: str, *, include_phones: bool = True, include_socials: bool = True
    ) -> dict[str, Any]:
        """Extract emails/phones/socials from an HTML or text blob (1 MB limit)."""
        return await self._post(
            "/v1/extract/contacts",
            {
                "text": text[:1_000_000],
                "include_phones": include_phones,
                "include_socials": include_socials,
            },
        )

    async def extract_tables(
        self, html: str, *, min_rows: int = 2, min_columns: int = 2
    ) -> dict[str, Any]:
        """Pull structured tables out of HTML — used for DTE/AICTE directories."""
        return await self._post(
            "/v1/extract/tables",
            {"html": html[:1_000_000], "min_rows": min_rows, "min_columns": min_columns},
        )

    async def extract_clean(self, url: str, *, timeout: int = 30) -> dict[str, Any]:
        """Clean article text — useful for 'About the Placement Cell' pages."""
        return await self._post("/v1/extract/clean", {"url": url, "timeout": timeout})

    # --- verification ------------------------------------------------------

    async def verify_email(self, email: str) -> dict[str, Any]:
        """Validate deliverability before a contact reaches marketing."""
        return await self._post("/v1/verify/email", {"email": email})


async def health() -> dict[str, Any]:
    """Unauthenticated liveness check. Free — costs no credits."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20) as client:
        response = await client.get("/health")
        response.raise_for_status()
        return response.json()
