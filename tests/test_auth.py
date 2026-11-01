"""Tests for the Prusa Account login redirect handling."""

from __future__ import annotations

from urllib.parse import quote

import pytest

from custom_components.prusa_connect.auth import _follow_redirects_for_code

AUTHORIZE = (
    "/o/authorize/?response_type=code&client_id=abc"
    "&redirect_uri=https%3A%2F%2Fconnect.prusa3d.com%2Flogin%2Fauth-callback"
)
CALLBACK = "https://connect.prusa3d.com/login/auth-callback?code=the-code"


class _FakeResponse:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Answers GETs from a table and records the URLs it was asked for."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self._routes = routes
        self.requested: list[str] = []

    def get(self, url: str, allow_redirects: bool = False) -> _FakeResponse:
        self.requested.append(url)
        return self._routes.get(url, _FakeResponse(404))


@pytest.mark.asyncio
async def test_passkey_offer_interstitial_is_skipped_via_next() -> None:
    """After login Prusa Account may show /passkeys/offer/?next=...; follow next."""
    offer = f"https://account.prusa3d.com/passkeys/offer/?next={quote(AUTHORIZE, safe='')}"
    session = _FakeSession(
        {
            offer: _FakeResponse(200),
            f"https://account.prusa3d.com{AUTHORIZE}": _FakeResponse(302, CALLBACK),
        }
    )

    code = await _follow_redirects_for_code(session, offer)

    assert code == "the-code"
    assert session.requested == [offer, f"https://account.prusa3d.com{AUTHORIZE}"]


@pytest.mark.asyncio
async def test_plain_page_without_next_still_returns_none() -> None:
    """A 200 page that carries no next parameter ends the chain as before."""
    page = "https://account.prusa3d.com/some/page/"
    session = _FakeSession({page: _FakeResponse(200)})

    assert await _follow_redirects_for_code(session, page) is None
    assert session.requested == [page]


@pytest.mark.asyncio
async def test_next_is_followed_only_once() -> None:
    """A page whose next leads back to another next page must not loop forever."""
    second = "https://account.prusa3d.com/second/?next=%2Fo%2Fauthorize%2F"
    first = f"https://account.prusa3d.com/first/?next={quote(second, safe='')}"
    session = _FakeSession({first: _FakeResponse(200), second: _FakeResponse(200)})

    assert await _follow_redirects_for_code(session, first) is None
    assert session.requested == [first, second]
