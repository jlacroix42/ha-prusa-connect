"""API client for Prusa Connect Mobile API.

Paths follow the OpenAPI document published at
https://connect-mobile-api.prusa3d.com/api/docs.jsonopenapi (read 2026-11-01):
everything lives under ``/api/v1``, printer commands are individual ``PUT``
endpoints, and job records use ``previewUrl``/``startAt``/``endAt``. The
client maps those job fields onto the names the entity platforms were
written for (``thumbnailUrl``/``startedAt``/``estimatedEnd``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed

from .auth import AuthenticationError, refresh_access_token
from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
API_PREFIX = "/api/v1"


def _epoch_to_iso(value: Any) -> str | None:
    """Turn an epoch timestamp (seconds) into an ISO 8601 UTC string."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def normalize_job(job: dict) -> dict:
    """Add the legacy field names the platforms read to a job record.

    The original keys stay in place; the aliases are only added when the
    record does not already carry them.
    """
    out = dict(job)
    if "thumbnailUrl" not in out and out.get("previewUrl"):
        out["thumbnailUrl"] = out["previewUrl"]
    if "displayName" not in out and out.get("fileName"):
        out["displayName"] = out["fileName"]
    if "startedAt" not in out:
        started = _epoch_to_iso(out.get("startAt"))
        if started:
            out["startedAt"] = started
    if "estimatedEnd" not in out:
        end = _epoch_to_iso(out.get("endAt"))
        if end:
            out["estimatedEnd"] = end
    return out


class PrusaConnectAPI:
    """Client for the Prusa Connect Mobile API."""

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
        """Make an authenticated API request.

        Automatically refreshes token on 401 and retries once.
        Asks for plain JSON; still unwraps hydra/member collections in case
        the server answers with JSON-LD.
        """
        url = f"{API_BASE_URL}{API_PREFIX}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

        async with self._session.request(
            method,
            url,
            headers=headers,
            json=json,
            data=data,
            params=params,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 401 and retry_on_401:
                # Try to refresh the token
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

            # Unwrap collection wrappers (JSON-LD / API Platform)
            if isinstance(result, dict):
                for key in ("hydra:member", "member"):
                    if key in result:
                        return result[key]

            return result

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
        """Get the authenticated user's profile."""
        return await self._request("GET", "/user")

    # --- Printers ---

    async def get_printers(self) -> list[dict]:
        """Get all printers for the user."""
        return await self._request("GET", "/printers")

    async def get_printer(self, uuid: str) -> dict:
        """Get detailed info for a printer: telemetry, cameras, jobs, flags."""
        return await self._request("GET", f"/printers/{uuid}/detail")

    async def update_printer(self, uuid: str, data: dict) -> dict:
        """Update printer settings."""
        return await self._request("PATCH", f"/printers/{uuid}", json=data)

    # --- Commands ---

    async def pause_print(self, uuid: str) -> None:
        """Pause current print on a printer."""
        await self._request("PUT", f"/printers/{uuid}/command/pause")

    async def resume_print(self, uuid: str) -> None:
        """Resume a paused print."""
        await self._request("PUT", f"/printers/{uuid}/command/resume")

    async def stop_print(self, uuid: str) -> None:
        """Stop current print."""
        await self._request("PUT", f"/printers/{uuid}/command/stop")

    async def set_ready(self, uuid: str) -> None:
        """Mark printer as ready (pick up finished print)."""
        await self._request("PUT", f"/printers/{uuid}/command/ready")

    async def set_unready(self, uuid: str) -> None:
        """Cancel ready state."""
        await self._request("PUT", f"/printers/{uuid}/command/unready")

    async def start_print_cloud(
        self, uuid: str, file_hash: str, team_id: int
    ) -> None:
        """Start a print from cloud storage."""
        await self._request(
            "PUT",
            f"/printers/{uuid}/command/start/cloud",
            json={"hash": file_hash, "teamId": team_id, "printNow": True},
        )

    async def start_print_usb(self, uuid: str, path: str) -> None:
        """Start a print from the printer's own storage."""
        await self._request(
            "PUT",
            f"/printers/{uuid}/command/start/files",
            json={"path": path, "printNow": True},
        )

    async def start_print_url(self, uuid: str, url: str) -> None:
        """Start a print from a Printables URL."""
        await self._request(
            "PUT",
            f"/printers/{uuid}/command/start/printables",
            json={"fileUrl": url, "printNow": True},
        )

    async def send_control_command(self, uuid: str, command: str) -> None:
        """Send a generic control command."""
        await self._request(
            "POST",
            f"/printers/{uuid}/command/control",
            json={"command": command},
        )

    async def respond_to_dialog(
        self, uuid: str, dialog_id: int, button: str
    ) -> None:
        """Respond to a printer dialog."""
        await self._request(
            "PUT",
            f"/printers/{uuid}/command/dialog",
            json={"dialogId": dialog_id, "button": button},
        )

    # --- Jobs ---

    async def get_jobs(self, **params: Any) -> list[dict]:
        """Get jobs, optionally filtered by query params."""
        jobs = await self._request("GET", "/jobs", params=params or None)
        return [normalize_job(j) for j in (jobs or [])]

    async def get_job(self, job_id: int | str) -> dict:
        """Get a specific job by ID."""
        job = await self._request("GET", f"/jobs/{job_id}")
        return normalize_job(job) if isinstance(job, dict) else job

    async def get_queue(self, printer_uuid: str) -> list[dict]:
        """Get the print queue for a printer."""
        return await self._request("GET", f"/printers/{printer_uuid}/queue")

    async def delete_queued_job(self, printer_uuid: str, job_id: int) -> None:
        """Delete a queued job from a printer's queue."""
        await self._request(
            "DELETE", f"/printers/{printer_uuid}/queue/{job_id}"
        )

    # --- Cameras ---

    async def get_cameras(self) -> list[dict]:
        """Get all cameras."""
        return await self._request("GET", "/cameras")

    async def get_camera(self, token: str) -> dict:
        """Get a specific camera by token."""
        return await self._request("GET", f"/cameras/{token}")

    # --- Notifications ---

    async def get_notifications(self, **params: Any) -> list[dict]:
        """Get notifications."""
        return await self._request(
            "GET", "/notifications", params=params or None
        )

    async def get_unseen_count(self) -> int:
        """Get the count of unseen notifications."""
        result = await self._request("GET", "/notifications/unseen")
        if isinstance(result, dict):
            return int(result.get("unseen", 0) or 0)
        return 0

    # --- Storage ---

    async def get_cloud_files(self, team_id: int) -> list[dict]:
        """Get cloud storage files for a team."""
        return await self._request("GET", f"/storage/{team_id}")

    async def get_printer_files(self, uuid: str) -> list[dict]:
        """Get files on the printer's own storage."""
        return await self._request("GET", f"/storage/printer/{uuid}")

    # --- Teams ---

    async def get_teams(self) -> list[dict]:
        """Get all teams the user belongs to."""
        return await self._request("GET", "/teams")

    async def get_team(self, team_id: int) -> dict:
        """Get a specific team."""
        return await self._request("GET", f"/teams/{team_id}")

    async def get_team_members(self, team_id: int) -> list[dict]:
        """Get members of a team."""
        return await self._request("GET", f"/teams/{team_id}/members")
