"""People guard state per tap (useful on dashboards)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IrrigationConfigEntry
from .coordinator import IrrigationCoordinator
from .entity import IrrigationEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: IrrigationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities(TapBlocked(coord, i) for i in range(len(coord.cfg.taps)))


class TapBlocked(IrrigationEntity, BinarySensorEntity):
    """On while the people guard holds this tap back (someone there, or not clear long enough)."""

    _attr_translation_key = "tap_blocked"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, coordinator: IrrigationCoordinator, index: int) -> None:
        super().__init__(coordinator, f"tap_{index + 1}_blocked")
        self.index = index
        self._attr_translation_placeholders = {"n": str(index + 1)}

    @property
    def is_on(self) -> bool:
        sn = self.coordinator.snapshot
        return bool(sn and self.coordinator.cfg.people_on and self.index < len(sn.people) and sn.people[self.index] > 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self.coordinator.cfg
        t = cfg.taps[self.index] if self.index < len(cfg.taps) else None
        sn = self.coordinator.snapshot
        return {
            "tap": cfg.label(self.index),
            "people_sensors": t.people if t else [],
            "detected_by": (sn.people_who[self.index] or None) if sn and self.index < len(sn.people_who) else None,
        }
