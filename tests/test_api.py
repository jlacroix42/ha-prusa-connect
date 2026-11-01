"""Tests for the Prusa Connect web API client: paths, GraphQL cameras, normalisers."""

from __future__ import annotations

import pytest

from custom_components.prusa_connect.api import (
    PrusaConnectAPI,
    normalize_job,
    normalize_printer,
)
from custom_components.prusa_connect.const import API_BASE_URL, GRAPHQL_URL


class _FakeResponse:
    def __init__(self, status: int, payload: object, body: bytes = b"") -> None:
        self.status = status
        self._payload = payload
        self._body = body
        self.content_length = None

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type: str | None = None) -> object:
        return self._payload

    async def read(self) -> bytes:
        return self._body


class _FakeSession:
    """Answers request()/post()/get() from a URL-keyed table and records calls."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.calls: list[dict] = []
        self._routes = routes

    def _answer(self, method: str, url: str, kwargs: dict) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        entry = self._routes.get(url, {})
        if isinstance(entry, _FakeResponse):
            return entry
        return _FakeResponse(200, entry)

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        return self._answer(method, url, kwargs)

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        return self._answer("POST", url, kwargs)

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        return self._answer("GET", url, kwargs)


PRINTER_UUID = "f538fc7b-0000-0000-0000-000000000000"
SNAPSHOT = "https://camera-service-snapshots.prusa3d.com/v1/snapshot/cam-1"


@pytest.mark.asyncio
async def test_user_comes_from_login_endpoint_with_bearer_and_json() -> None:
    """GET /app/login returns {"user": {...}}; the client unwraps it."""
    session = _FakeSession({f"{API_BASE_URL}/app/login": {"user": {"id": 419770, "email": "x@example.com"}}})
    api = PrusaConnectAPI(session, "access", "refresh")

    user = await api.get_user()

    assert user == {"id": 419770, "email": "x@example.com"}
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == "Bearer access"
    assert call["headers"]["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_printer_detail_merges_graphql_cameras() -> None:
    """Printer detail is REST; cameras and snapshot URLs come from GraphQL."""
    session = _FakeSession(
        {
            f"{API_BASE_URL}/app/printers/{PRINTER_UUID}": {
                "uuid": PRINTER_UUID,
                "name": "CoreOne",
                "printer_state": "IDLE",
                "connect_state": "IDLE",
                "printer_type": "8.1.0",
                "printer_type_name": "Prusa CORE One L",
                "sn": "SN-1",
                "temp": {"temp_nozzle": 24.1, "temp_bed": 20.1, "target_nozzle": 0.0, "target_bed": 0.0},
                "speed": 100,
                "flow": 100,
                "axis_z": 0.0,
                "enclosure": {"present": False},
                "tools": {"1": {"active": True, "material": "PETG"}},
            },
            GRAPHQL_URL: {
                "data": {
                    "camera": {
                        "camerasConnection": {
                            "__typename": "CameraConnection",
                            "edges": [
                                {"node": {"id": "cam-1", "name": "Buddy3D Camera", "type": "Buddy3D",
                                          "firmwareVersion": "3.1.6", "snapshots": {"lastSnapshotUrl": SNAPSHOT}}}
                            ],
                        }
                    }
                }
            },
        }
    )
    api = PrusaConnectAPI(session, "access", "refresh")

    printer = await api.get_printer(PRINTER_UUID)

    assert printer["state"] == "IDLE"
    assert printer["printerTypeName"] == "Prusa CORE One L"
    assert printer["serialNumber"] == "SN-1"
    assert printer["material"] == "PETG"
    assert printer["hasEnclosure"] is False
    assert printer["telemetry"] == {
        "temp_nozzle": 24.1, "temp_bed": 20.1, "target_nozzle": 0.0, "target_bed": 0.0,
        "printing_speed": 100, "flow_factor": 100, "pos_z_mm": 0.0,
    }
    assert printer["cameras"] == [
        {"id": "cam-1", "name": "Buddy3D Camera", "type": "Buddy3D", "firmwareVersion": "3.1.6", "snapshotUrl": SNAPSHOT}
    ]
    gql_call = next(c for c in session.calls if c["url"] == GRAPHQL_URL)
    assert gql_call["json"]["variables"] == {"printerUuid": PRINTER_UUID}
    assert gql_call["headers"]["Origin"] == API_BASE_URL


@pytest.mark.asyncio
async def test_printers_list_is_unwrapped_and_normalised() -> None:
    """GET /app/printers?limit=… answers {"printers": [...], "pager": {...}}."""
    session = _FakeSession(
        {f"{API_BASE_URL}/app/printers": {"printers": [{"uuid": "p1", "printer_state": "MANIPULATING", "connect_state": "IDLE"}], "pager": {}}}
    )
    api = PrusaConnectAPI(session, "access", "refresh")

    printers = await api.get_printers()

    assert printers[0]["uuid"] == "p1"
    assert printers[0]["state"] == "BUSY"
    assert session.calls[0]["params"] == {"limit": 100}


@pytest.mark.asyncio
async def test_offline_connect_state_wins() -> None:
    """A printer the cloud reports OFFLINE is offline whatever its last state was."""
    assert normalize_printer({"printer_state": "IDLE", "connect_state": "OFFLINE"})["state"] == "OFFLINE"


@pytest.mark.asyncio
async def test_pause_posts_named_command() -> None:
    """Commands are POST /app/printers/{uuid}/commands with the web API names."""
    session = _FakeSession({})
    api = PrusaConnectAPI(session, "access", "refresh")

    await api.pause_print(PRINTER_UUID)
    await api.start_print_usb(PRINTER_UUID, "/usb/FILE.BGC")

    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["url"] == f"{API_BASE_URL}/app/printers/{PRINTER_UUID}/commands"
    assert session.calls[0]["json"] == {"command": "PAUSE_PRINT"}
    assert session.calls[1]["json"] == {"command": "START_PRINT", "args": {"path": "/usb/FILE.BGC"}}


@pytest.mark.asyncio
async def test_fetch_bytes_sends_bearer() -> None:
    """Snapshot bytes are fetched with the bearer token."""
    session = _FakeSession({SNAPSHOT: _FakeResponse(200, None, b"\xff\xd8jpeg")})
    api = PrusaConnectAPI(session, "access", "refresh")

    assert await api.fetch_bytes(SNAPSHOT) == b"\xff\xd8jpeg"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer access"


def test_normalize_job_maps_web_fields() -> None:
    """Web job records get the names the sensors and preview image read."""
    job = normalize_job(
        {
            "id": 325,
            "printer_uuid": PRINTER_UUID,
            "state": "FIN_OK",
            "start": 1_700_000_000,
            "end": 1_700_003_600,
            "file": {"name": "a.bgcode", "display_name": "Bottle Holder", "preview_url": "/app/teams/1/files/h/preview"},
        }
    )

    assert job["printerUuid"] == PRINTER_UUID
    assert job["state"] == "FINISHED"
    assert job["fileName"] == "Bottle Holder"
    assert job["thumbnailUrl"] == f"{API_BASE_URL}/app/teams/1/files/h/preview"
    assert job["startedAt"] == "2023-11-14T22:13:20+00:00"
    assert job["estimatedEnd"] == "2023-11-14T23:13:20+00:00"


def test_normalize_job_keeps_active_state_and_remaining_time() -> None:
    """Active states pass through and time_remaining becomes timeRemaining."""
    job = normalize_job({"printer_uuid": "p", "state": "PRINTING", "time_remaining": 120, "progress": 42.5})

    assert job["state"] == "PRINTING"
    assert job["timeRemaining"] == 120
    assert job["progress"] == 42.5
