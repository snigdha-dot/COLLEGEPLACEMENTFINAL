"""Tests for the Ollagraph client's cost and failure guards.

All mocked — these must never spend credits. The two behaviors under test were
both found the hard way against the live API on 2026-08-02:

  - gmaps charged 30 credits/call, not the documented 1, so a cap has to count
    credits from response headers rather than assume a per-call price.
  - gmaps reported an upstream Apify billing failure as HTTP 200 with
    ok=false, so status code alone is not proof of success.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.scraper.ollagraph_client import (  # noqa: E402
    CreditCapExceeded,
    OllagraphAuthError,
    OllagraphClient,
    UpstreamActorError,
)


def _client_with(handler, **kwargs) -> OllagraphClient:
    client = OllagraphClient(api_key="osk_test", **kwargs)
    client._client = httpx.AsyncClient(
        base_url="https://api.ollagraph.com",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer osk_test"},
    )
    return client


def test_credit_cap_aborts_run() -> None:
    """Cap counts credits, not calls — 30/call means 4 calls breach a 100 cap."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "results": []},
            headers={"x-credits-charged": "30", "x-credits-balance": "1000"},
        )

    completed = {"n": 0}

    async def run() -> None:
        client = _client_with(handler, credit_cap=100)
        async with client:
            for _ in range(10):
                await client.gmaps_search("q")
                completed["n"] += 1
            raise AssertionError("cap did not fire")

    try:
        asyncio.run(run())
    except CreditCapExceeded as exc:
        assert "cap" in str(exc)
    else:
        raise AssertionError("expected CreditCapExceeded")

    # 30 credits/call against a 100 cap. The check runs BEFORE each call and
    # cannot know that call's cost in advance, so: calls 1-4 are each permitted
    # (ledger 0/30/60/90, all under 100) and leave it at 120; the 5th is
    # blocked. The cap therefore overshoots by at most one call's cost — with
    # gmaps at 30 credits, a 5000 cap can settle around 5030. That is the
    # intended trade-off; the alternative is refusing calls that would have fit.
    assert completed["n"] == 4, f"expected 4 calls to succeed, got {completed['n']}"


def test_cap_checked_before_call_not_after() -> None:
    """A breach must stop the NEXT call, not merely report after overspending."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"ok": True}, headers={"x-credits-charged": "60"}
        )

    async def run() -> None:
        client = _client_with(handler, credit_cap=50)
        async with client:
            await client.search("first")   # allowed: ledger still at 0
            await client.search("second")  # must abort: ledger now 60 >= 50

    try:
        asyncio.run(run())
    except CreditCapExceeded:
        pass
    else:
        raise AssertionError("expected CreditCapExceeded")
    assert calls["n"] == 1, f"expected exactly 1 network call, got {calls['n']}"


def test_actor_ok_false_raises_despite_http_200() -> None:
    """The real gmaps failure mode: HTTP 200, ok=false, empty results."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "count": 0,
                "results": [],
                "error": 'apify HTTP 402: {"error": {"type": "not-enough-usage-to-run-paid-actor"}}',
            },
            headers={"x-credits-charged": "30"},
        )

    async def run() -> None:
        client = _client_with(handler)
        async with client:
            await client.gmaps_search("engineering colleges")

    try:
        asyncio.run(run())
    except UpstreamActorError as exc:
        assert "402" in str(exc) or "usage" in str(exc)
    else:
        raise AssertionError("ok=false was not detected")


def test_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "invalid key"})

    async def run() -> None:
        client = _client_with(handler, max_retries=3)
        async with client:
            await client.search("q")

    try:
        asyncio.run(run())
    except OllagraphAuthError:
        pass
    else:
        raise AssertionError("expected OllagraphAuthError")
    assert calls["n"] == 1, f"auth error retried {calls['n']} times; must not retry"


def test_missing_key_fails_fast() -> None:
    try:
        OllagraphClient(api_key="")
    except OllagraphAuthError:
        pass
    else:
        raise AssertionError("empty key should raise")


def test_ledger_tracks_per_endpoint_credits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cost = "30" if "gmaps" in str(request.url) else "3"
        return httpx.Response(
            200, json={"ok": True}, headers={"x-credits-charged": cost,
                                             "x-credits-balance": "500"}
        )

    async def run() -> None:
        client = _client_with(handler, credit_cap=10_000)
        async with client:
            await client.search("a")
            await client.gmaps_search("b")
            assert client.ledger.credits_charged == 33
            assert client.ledger.per_endpoint_credits["/v1/search"] == 3
            assert client.ledger.per_endpoint_credits["/v1/actors/gmaps/search"] == 30
            assert client.ledger.balance == 500

    asyncio.run(run())


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} passed")
