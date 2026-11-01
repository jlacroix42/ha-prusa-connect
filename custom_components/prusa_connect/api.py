"""API client for Prusa Connect.

Talks to the Prusa Connect *web* application's API, the one the browser
uses, with the OAuth token the login flow obtains:

* REST at ``https://connect.prusa3d.com/app/...`` for the user, printers,
  jobs, commands, queue, files and notifications (observed 2026-11-01).
* GraphQL at ``https://connect-api.prusa3d.com/graphql`` for camera
  snapshot URLs (``camera { camerasConnection(printerUuid:) ... snapshots
  { lastSnapshotUrl } }``); the snapshot URL itself requires the same
  bearer token, so images are fetched through :meth:`fetch_bytes`.

The mobile gateway (``connect-mobile-api.prusa3d.com``) only accepts tokens
of the mobile app's own OAuth client, which is why it is not used.

Records are normalised to the field names the entity platforms were
written for (``state``, ``telemetry.temp_nozzle``, ``printerTypeName``,
``cameras[].snapshotUrl``, ``thumbnailUrl``, ``startedAt`` ...).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed

from .auth import AuthenticationError, refresh_access_token
from .const import API_BASE_URL, API_PREFIX, GRAPHQL_URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
SNAPSHOT_TIMEOUT = aiohttp.ClientTimeout(total=15)
PRINTERS_PAGE_LIMIT = 100
JOBS_PAGE_LIMIT = 20

_JOB_STATE_MAP = {
    "FIN_OK": "FINISHED",
    "FIN_STOPPED": "STOPPED",
    "FIN_ERROR": "ERROR",
    "FINISHED": "FINISHED",
    "STOPPED": "STOPPED",
    "ERROR": "ERROR",
    "PRINTING": "PRINTING",
    "PAUSED": "PAUSED",
    "QUEUED": "QUEUED",
}
_PRINTER_STATE_MAP = {"MANIPULATING": "BUSY"}

CAMERAS_QUERY = """
query PrinterCameras($printerUuid: UUID!) {
  camera {
    camerasConnection(printerUuid: $printerUuid, first: 20) {
      __typename
      ... on CameraConnection {
        edges {
          node {
            id
            name
            type
            firmwareVersion
            snapshots { lastSnapshotUrl }
          }
        }
      }
    }
  }
}
"""


def _epoch_to_iso(value: Any) -> str | None:
    """Turn an epoch timestamp (seconds) into an ISO 8601 UTC string."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _absolute(url: str | None) -> str | None:
    """Make an API-relative URL absolute."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{API_BASE_URL}{url}"
    return url


def normalize_printer(printer: dict, cameras: list[dict] | None = None) -> dict:
    """Map a web-API printer record onto the platform field names."""
    out = dict(printer)

    state = printer.get("printer_state") or printer.get("state")
    if printer.get("connect_state") == "OFFLINE" or not state:
        state = printer.get("connect_state") or state
    out["state"] = _PRINTER_STATE_MAP.get(state, state)

    out.setdefault("printerType", printer.get("printer_type"))
    out.setdefault("printerTypeName", printer.get("printer_type_name"))
    out.setdefault("serialNumber", printer.get("sn"))

    material = None
    filament = printer.get("filament")
    if isinstance(filament, dict):
        material = filament.get("material")
    tools = printer.get("tools")
    if not material and isinstance(tools, dict):
        for tool in tools.values():
            if isinstance(tool, dict) and tool.get("material"):
                if tool.get("active") or material is None:
                    material = tool.get("material")
    if material:
        out["material"] = material

    enclosure = printer.get("enclosure")
    if isinstance(enclosure, dict):
        out["hasEnclosure"] = bool(enclosure.get("present"))
    mmu = printer.get("mmu")
    if isinstance(mmu, dict):
        out["hasMmuEnabled"] = bool(mmu.get("enabled"))

    telemetry: dict[str, Any] = {}
    existing = printer.get("telemetry")
    if isinstance(existing, dict):
        telemetry.update(existing)
    temp = printer.get("temp")
    if isinstance(temp, dict):
        for key in ("temp_nozzle", "temp_bed", "target_nozzle", "target_bed"):
            if temp.get(key) is not None:
                telemetry[key] = temp[key]
    if printer.get("speed") is not None:
        telemetry["printing_speed"] = printer["speed"]
    if printer.get("flow") is not None:
        telemetry["flow_factor"] = printer["flow"]
    if printer.get("axis_z") is not None:
        telemetry["pos_z_mm"] = printer["axis_z"]
    chamber = printer.get("chamber")
    if isinstance(chamber, dict) and chamber.get("temp") is not None:
        telemetry["temp_chamber"] = chamber["temp"]
    out["telemetry"] = telemetry

    if cameras is not None:
        out["cameras"] = cameras
    return out


def normalize_camera(node: dict) -> dict:
    """Map a GraphQL camera node onto the platform field names."""
    snapshots = node.get("snapshots") or {}
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "type": node.get("type"),
        "firmwareVersion": node.get("firmwareVersion"),
        "snapshotUrl": snapshots.get("lastSnapshotUrl"),
    }


def normalize_job(job: dict) -> dict:
    """Map a web-API job record onto the platform field names."""
    out = dict(job)
    out["printerUuid"] = job.get("printer_uuid") or job.get("printerUuid")
    state = job.get("state")
    if isinstance(state, str):
        out["state"] = _JOB_STATE_MAP.get(state, state)

    file = job.get("file") if isinstance(job.get("file"), dict) else {}
    name = file.get("display_name") or file.get("name") or job.get("path")
    if name:
        out.setdefault("fileName", name)
        out.setdefault("displayName", name)
    preview = _absolute(file.get("preview_url"))
    if preview and "thumbnailUrl" not in out:
        out["thumbnailUrl"] = preview

    if "startedAt" not in out:
        started = _epoch_to_iso(job.get("start"))
        if started:
            out["startedAt"] = started
    if "estimatedEnd" not in out:
        end = _epoch_to_iso(job.get("end"))
        if end:
            out["estimatedEnd"] = end
    if "timeRemaining" not in out and job.get("time_remaining") is not None:
        out["timeRemaining"] = job["time_remaining"]
    return out


class PrusaConnectAPI:
    """Client for the Prusa Connect web API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        token_update_callback: Any = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_update_callback = token_update_callback
        self._refresh_lock = asyncio.Lock()

    def update_tokens(
        self, access_token: str, refresh_token: str
    ) -> None:
        """Update stored tokens."""
        self._access_token = access_token
        self._refresh_token = refresh_token

    def _headers(self, **extra: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        params: dict | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Make an authenticated REST request.

        Automatically refreshes the token on 401 and retries once.
        """
        url = f"{API_BASE_URL}{API_PREFIX}{path}"

        async with self._session.request(
            method,
            url,
            headers=self._headers(),
            json=json,
            data=data,
            params=params,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401 and retry_on_401:
                await self._refresh_token_and_persist()
                return await self._request(
                    method,
                    path,
                    json=json,
                    data=data,
                    params=params,
                    retry_on_401=False,
                )

            if resp.status == 401:
                raise ConfigEntryAuthFailed(
                    "Authentication failed after token refresh"
                )

            if resp.status == 204:
                return None

            resp.raise_for_status()

            if resp.content_length == 0:
                return None
            try:
                result = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                return None

            if isinstance(result, dict):
                for key in ("hydra:member", "member"):
                    if key in result:
                        return result[key]
            return result

    async def _graphql(self, query: str, variables: dict | None = None,
                       retry_on_401: bool = True) -> dict:
        """Run a GraphQL query against the Connect GraphQL API."""
        async with self._session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers=self._headers(**{"Origin": API_BASE_URL}),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401 and retry_on_401:
                await self._refresh_token_and_persist()
                return await self._graphql(query, variables, retry_on_401=False)
            if resp.status == 401:
                raise ConfigEntryAuthFailed(
                    "Authentication failed after token refresh"
                )
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        if isinstance(body, dict) and body.get("errors") and not body.get("data"):
            messages = "; ".join(
                str(e.get("message", "")) for e in body["errors"][:3]
            )
            raise aiohttp.ClientError(f"GraphQL error: {messages}")
        return body.get("data") or {} if isinstance(body, dict) else {}

    async def fetch_bytes(self, url: str, retry_on_401: bool = True) -> bytes | None:
        """Fetch a binary resource (snapshot, preview) with the bearer token."""
        async with self._session.get(
            url,
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=SNAPSHOT_TIMEOUT,
        ) as resp:
            if resp.status == 401 and retry_on_401:
                await self._refresh_token_and_persist()
                return await self.fetch_bytes(url, retry_on_401=False)
            if resp.status == 200:
                return await resp.read()
            _LOGGER.debug("Fetching %s failed: HTTP %s", url, resp.status)
            return None

    async def _refresh_token_and_persist(self) -> None:
        """Refresh the access token and persist the new tokens.

        Uses a lock to prevent concurrent refresh attempts from racing.
        """
        async with self._refresh_lock:
            try:
                tokens = await refresh_access_token(
                    self._session, self._refresh_token
                )
                self._access_token = tokens["access_token"]
                self._refresh_token = tokens["refresh_token"]

                if self._token_update_callback:
                    await self._token_update_callback(tokens)
            except AuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    "Token refresh failed — re-authentication required"
                ) from err

    # --- User ---

    async def get_user(self) -> dict:
        """Get the authenticated user's profile (id, email, ...)."""
        result = await self._request("GET", "/login")
        if isinstance(result, dict) and isinstance(result.get("user"), dict):
            return result["user"]
        return result or {}

    # --- Printers ---

    async def get_printers(self) -> list[dict]:
        """Get all printers for the user (list view, no cameras)."""
        result = await self._request(
            "GET", "/printers", params={"limit": PRINTERS_PAGE_LIMIT}
        )
        printers = result.get("printers", []) if isinstance(result, dict) else result
        return [normalize_printer(p) for p in (printers or [])]

    async def get_printer(self, uuid: str) -> dict:
        """Get printer detail with telemetry, flags and cameras."""
        detail = await self._request("GET", f"/printers/{uuid}")
        cameras: list[dict] = []
        try:
            cameras = await self.get_printer_cameras(uuid)
        except ConfigEntryAuthFailed:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Cameras for %s unavailable: %s", uuid, err)
        return normalize_printer(detail or {}, cameras)

    async def update_printer(self, uuid: str, data: dict) -> dict:
        """Update printer settings."""
        return await self._request("PATCH", f"/printers/{uuid}", json=data)

    # --- Cameras ---

    async def get_printer_cameras(self, uuid: str) -> list[dict]:
        """Get the cameras of a printer with their latest snapshot URLs."""
        data = await self._graphql(CAMERAS_QUERY, {"printerUuid": uuid})
        connection = (data.get("camera") or {}).get("camerasConnection") or {}
        edges = connection.get("edges") or []
        return [
            normalize_camera(edge.get("node") or {})
            for edge in edges
            if isinstance(edge, dict)
        ]

    async def get_cameras(self) -> list[dict]:
        """Get all cameras the user can see (REST, without snapshot URLs)."""
        result = await self._request("GET", "/cameras")
        return result.get("cameras", []) if isinstance(result, dict) else (result or [])

    # --- Commands ---

    async def send_command(
        self, uuid: str, command: str, args: dict | None = None
    ) -> Any:
        """Send a printer command (names as in /printers/{uuid}/supported-commands)."""
        payload: dict[str, Any] = {"command": command}
        if args:
            payload["args"] = args
        return await self._request("POST", f"/printers/{uuid}/commands", json=payload)

    async def pause_print(self, uuid: str) -> None:
        """Pause current print on a printer."""
        await self.send_command(uuid, "PAUSE_PRINT")

    async def resume_print(self, uuid: str) -> None:
        """Resume a paused print."""
        await self.send_command(uuid, "RESUME_PRINT")

    async def stop_print(self, uuid: str) -> None:
        """Stop current print."""
        await self.send_command(uuid, "STOP_PRINT")

    async def set_ready(self, uuid: str) -> None:
        """Mark printer as ready (pick up finished print)."""
        await self.send_command(uuid, "SET_PRINTER_READY")

    async def set_unready(self, uuid: str) -> None:
        """Cancel ready state."""
        await self.send_command(uuid, "CANCEL_PRINTER_READY")

    async def start_print_usb(self, uuid: str, path: str) -> None:
        """Start a print from the printer's own storage."""
        await self.send_command(uuid, "START_PRINT", {"path": path})

    async def start_print_cloud(
        self, uuid: str, file_hash: str, team_id: int
    ) -> None:
        """Queue a cloud file for printing (unverified against the live API)."""
        await self._request(
            "POST",
            f"/printers/{uuid}/queue",
            json={"hash": file_hash, "team_id": team_id, "print_now": True},
        )

    async def start_print_url(self, uuid: str, url: str) -> None:
        """Download a file by URL to the printer (unverified against the live API)."""
        await self._request(
            "POST", f"/printers/{uuid}/download-queue", json={"url": url}
        )

    async def send_control_command(self, uuid: str, command: str) -> None:
        """Send a generic control command by name."""
        await self.send_command(uuid, command)

    async def respond_to_dialog(
        self, uuid: str, dialog_id: int, button: str
    ) -> None:
        """Respond to a printer dialog (unverified against the live API)."""
        await self.send_command(
            uuid, "DIALOG_ACTION", {"dialog_id": dialog_id, "button": button}
        )

    # --- Jobs ---

    async def get_jobs(self, **params: Any) -> list[dict]:
        """Get recent jobs across printers."""
        query = {"limit": JOBS_PAGE_LIMIT}
        query.update(params)
        result = await self._request("GET", "/jobs", params=query)
        jobs = result.get("jobs", []) if isinstance(result, dict) else result
        return [normalize_job(j) for j in (jobs or [])]

    async def get_job(self, printer_uuid: str, job_id: int | str) -> dict:
        """Get a specific job of a printer."""
        job = await self._request("GET", f"/printers/{printer_uuid}/jobs/{job_id}")
        return normalize_job(job) if isinstance(job, dict) else job

    async def get_queue(self, printer_uuid: str) -> list[dict]:
        """Get the print queue for a printer."""
        result = await self._request("GET", f"/printers/{printer_uuid}/queue")
        if isinstance(result, dict):
            for key in ("planned", "queue", "jobs"):
                if isinstance(result.get(key), list):
                    return result[key]
            return []
        return result or []

    async def delete_queued_job(self, printer_uuid: str, job_id: int) -> None:
        """Delete a queued job from a printer's queue."""
        await self._request("DELETE", f"/printers/{printer_uuid}/queue/{job_id}")

    # --- Notifications ---

    async def get_notifications(self, **params: Any) -> list[dict]:
        """Get notifications."""
        result = await self._request("GET", "/notifications", params=params or None)
        if isinstance(result, dict):
            return result.get("notifications", [])
        return result or []

    async def get_unseen_count(self) -> int:
        """Get the count of unseen notifications."""
        result = await self._request("GET", "/notifications/unseen")
        if isinstance(result, dict):
            return int(result.get("unseen", 0) or 0)
        return 0

    # --- Storage ---

    async def get_cloud_files(self, team_id: int) -> list[dict]:
        """Get cloud storage files for a team."""
        result = await self._request("GET", f"/teams/{team_id}/files")
        return result.get("files", []) if isinstance(result, dict) else (result or [])

    async def get_printer_files(self, uuid: str) -> list[dict]:
        """Get files on the printer's own storage."""
        result = await self._request("GET", f"/printers/{uuid}/files")
        return result.get("files", []) if isinstance(result, dict) else (result or [])

    # --- Teams ---

    async def get_teams(self) -> list[dict]:
        """Get all teams the user belongs to."""
        result = await self._request("GET", "/users/teams")
        return result.get("teams", []) if isinstance(result, dict) else (result or [])

    async def get_team(self, team_id: int) -> dict:
        """Get a specific team."""
        for team in await self.get_teams():
            if str(team.get("id")) == str(team_id):
                return team
        return {}

    async def get_team_members(self, team_id: int) -> list[dict]:
        """Get members of a team."""
        result = await self._request("GET", f"/users/teams/{team_id}")
        if isinstance(result, dict):
            return result.get("members", []) or result.get("users", [])
        return result or []
