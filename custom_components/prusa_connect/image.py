"""Image platform for Prusa Connect — print preview thumbnails."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PrusaConnectJobCoordinator, PrusaConnectPrinterCoordinator
from .entity import PrusaConnectEntity

if TYPE_CHECKING:
    from . import PrusaConnectConfigEntry


class PrusaConnectPrintPreview(PrusaConnectEntity, ImageEntity):
    """Image entity showing the thumbnail of the current print job."""

    _attr_content_type = "image/png"
    _attr_translation_key = "print_preview"

    def __init__(
        self,
        coordinator: PrusaConnectPrinterCoordinator,
        job_coordinator: PrusaConnectJobCoordinator,
        printer_uuid: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the print preview image entity."""
        PrusaConnectEntity.__init__(self, coordinator, printer_uuid)
        ImageEntity.__init__(self, hass)
        self._job_coordinator = job_coordinator
        self._attr_unique_id = f"{printer_uuid}_print_preview"

    async def async_added_to_hass(self) -> None:
        """Register listeners for both coordinators."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._job_coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from either coordinator."""
        self.async_write_ha_state()

    @property
    def image_url(self) -> str | None:
        """Return the job's thumbnail URL from the API."""
        if not self._job_coordinator.data:
            return None
        job = self._job_coordinator.data.get(self._printer_uuid)
        if job:
            return job.get("thumbnailUrl")
        return None

    async def async_image(self) -> bytes | None:
        """Fetch the preview through the API client (the URL needs the token)."""
        url = self.image_url
        if not url:
            return None
        return await self.coordinator.api.fetch_bytes(url)

    @property
    def image_last_updated(self) -> datetime | None:
        """Track when the image changes (new job = new image)."""
        if not self._job_coordinator.data:
            return None
        job = self._job_coordinator.data.get(self._printer_uuid)
        if job and job.get("startedAt"):
            try:
                return datetime.fromisoformat(job["startedAt"])
            except (ValueError, TypeError):
                pass
        return None

    @property
    def available(self) -> bool:
        """Only available when a job is active."""
        if not super().available:
            return False
        if not self._job_coordinator.data:
            return False
        job = self._job_coordinator.data.get(self._printer_uuid)
        return job is not None and job.get("state") in ("PRINTING", "PAUSED")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PrusaConnectConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Prusa Connect print preview images."""
    data = entry.runtime_data
    printer_coordinator = data.printer_coordinator
    job_coordinator = data.job_coordinator

    entities: list[PrusaConnectPrintPreview] = []

    for printer_uuid in printer_coordinator.data:
        entities.append(
            PrusaConnectPrintPreview(
                printer_coordinator,
                job_coordinator,
                printer_uuid,
                hass,
            )
        )

    async_add_entities(entities)
