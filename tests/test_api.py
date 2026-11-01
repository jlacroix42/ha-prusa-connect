"""Tests for the Prusa Connect mobile API client paths and job normalisation."""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.api import PrusaConnectAPI, normalize_job
from custom_components.prusa_connect.const import API_BASE_URL


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload
        self.content_length = None

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type: str | None = None) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, payload: object) -> None:
        self.calls: list[dict] = []
        self._payload = payload

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(200, self._payload)


@pytest.mark.asyncio
async def test_user_request_uses_api_v1_prefix_and_plain_json() -> None:
    """GET /user must go to <base>/api/v1/user and ask for application/json."""
    session = _FakeSession({"id": 1, "email": "x@example.com"})
    api = PrusaConnectAPI(session, "access", "refresh")

    user = await api.get_user()

    assert user == {"id": 1, "email": "x@example.com"}
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{API_BASE_URL}/api/v1/user"
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["Authorization"] == "Bearer access"


@pytest.mark.asyncio
async def test_printer_detail_and_pause_paths() -> None:
    """Printer detail comes from /detail; pause is a PUT to its own endpoint."""
    session = _FakeSession({})
    api = PrusaConnectAPI(session, "access", "refresh")

    await api.get_printer("abc")
    await api.pause_print("abc")

    assert session.calls[0]["url"].endswith("/api/v1/printers/abc/detail")
    assert session.calls[1]["method"] == "PUT"
    assert session.calls[1]["url"].endswith("/api/v1/printers/abc/command/pause")


@pytest.mark.asyncio
async def test_member_collections_are_unwrapped() -> None:
    """JSON-LD style collections are unwrapped to the plain list."""
    session = _FakeSession({"member": [{"uuid": "p1"}]})
    api = PrusaConnectAPI(session, "access", "refresh")

    assert await api.get_printers() == [{"uuid": "p1"}]


def test_normalize_job_adds_legacy_aliases() -> None:
    """New job fields get the names the sensors and image platform read."""
    job = normalize_job(
        {
            "id": "j1",
            "fileName": "benchy.gcode",
            "previewUrl": "https://example.com/p.png",
            "startAt": 1_700_000_000,
            "endAt": 1_700_003_600,
        }
    )

    assert job["thumbnailUrl"] == "https://example.com/p.png"
    assert job["displayName"] == "benchy.gcode"
    assert job["startedAt"] == "2023-11-14T22:13:20+00:00"
    assert job["estimatedEnd"] == "2023-11-14T23:13:20+00:00"
    # originals are kept
    assert job["previewUrl"] == "https://example.com/p.png"


def test_normalize_job_keeps_existing_values_and_tolerates_gaps() -> None:
    """Existing legacy keys win, and missing timestamps add nothing."""
    job = normalize_job({"id": "j2", "thumbnailUrl": "keep", "previewUrl": "new"})

    assert job["thumbnailUrl"] == "keep"
    assert "startedAt" not in job
    assert "estimatedEnd" not in job
