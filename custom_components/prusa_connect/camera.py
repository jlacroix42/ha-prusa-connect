"""Camera platform for Prusa Connect — printer snapshots."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry

_LOGGER = logging.getLogger(__name__)


class PrusaConnectCamera(PrusaConnectEntity, Camera):
    """Camera entity showing the printer's latest snapshot."""

    _attr_translation_key = "camera"
    _attr_is_streaming = False

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        printer_uuid: str,
        hass: HomeAssistant,
        camera_index: int = 0,
    ) -> None:
        """Initialize the camera entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        Camera.__init__(self)
        self._camera_index = camera_index
        self._hass = hass
        suffix = f"_camera_{camera_index}" if camera_index > 0 else "_camera"
        self._attr_unique_id = f"{printer_uuid}{suffix}"
        self._attr_frame_interval = 30

    def _get_snapshot_url(self) -> str | None:
        """Get the snapshot URL from printer data."""
        printer = self._printer_data

        # Try cameras list first (printer detail carries Camera resources)
        cameras = printer.get("cameras") or []
        if cameras and self._camera_index < len(cameras):
            cam = cameras[self._camera_index]
            if isinstance(cam, dict):
                url = cam.get("snapshotUrl") or cam.get("imageUrl")
                if url:
                    return url

        # The printer's default snapshot (mobile API, 2026)
        default = printer.get("defaultSnapshot")
        if isinstance(default, dict):
            url = default.get("snapshotUrl") or default.get("url")
            if url:
                return url
        snapshots = printer.get("snapshots")
        if isinstance(snapshots, dict):
            for entry in snapshots.values():
                if isinstance(entry, dict):
                    url = entry.get("snapshotUrl") or entry.get("url")
                    if url:
                        return url
                elif isinstance(entry, str) and entry.startswith("http"):
                    return entry

        # Fall back to top-level snapshot URL (older shapes)
        return printer.get("snapshotUrl") or printer.get("cameraUrl")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the current camera snapshot as bytes."""
        url = self._get_snapshot_url()
        if not url:
            return None

        try:
            # Snapshot URLs on camera-service-snapshots.prusa3d.com require
            # the same bearer token as the API, so go through the client.
            image = await self.coordinator.api.fetch_bytes(url)
            if image:
                return image
            _LOGGER.debug("No snapshot returned for %s", self._printer_uuid)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "Error fetching snapshot for %s: %s",
                self._printer_uuid,
                err,
            )

        return None

    @property
    def available(self) -> bool:
        """Return True if we have a snapshot URL."""
        if not super().available:
            return False
        return self._get_snapshot_url() is not None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect cameras."""
    printer_coordinator = entry.runtime_data.printer_coordinator

    entities: list[PrusaConnectCamera] = []

    for printer_uuid, printer_data in printer_coordinator.data.items():
        cameras = printer_data.get("cameras") or []
        if cameras:
            for idx in range(len(cameras)):
                entities.append(
                    PrusaConnectCamera(
                        printer_coordinator, printer_uuid, hass, idx
                    )
                )
        else:
            # Create a single camera entity using the top-level snapshot URL
            snapshot_url = printer_data.get("snapshotUrl") or printer_data.get(
                "cameraUrl"
            )
            if snapshot_url:
                entities.append(
                    PrusaConnectCamera(
                        printer_coordinator, printer_uuid, hass
                    )
                )

    async_add_entities(entities)
