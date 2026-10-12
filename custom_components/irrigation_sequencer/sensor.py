"""Status and next-cycle sensors."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import IrrigationConfigEntry
from .const import STATUS_OPTIONS
from .coordinator import IrrigationCoordinator
from .entity import IrrigationEntity
from .estimate import cycle_end_estimate


async def async_setup_entry(
    hass: HomeAssistant, entry: IrrigationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    async_add_entities([StatusSensor(coord), NextCycleSensor(coord)])


class StatusSensor(IrrigationEntity, SensorEntity):
    """idle / watering / gap / soaking / waiting / held / manual / stuck."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    # detail that changes often: shown live, not written to the recorder database
    _unrecorded_attributes = frozenset(
        {"remaining_min", "pending", "next_step", "last_message", "config_rev", "cycle_ends_at"}
    )

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.status()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self.coordinator
        end = cycle_end_estimate(coord.state, coord.cfg, time.time())
        return {
            **coord.status_attributes(),
            # when the running cycle should finish watering: the card's "time left" counts down to it
            "cycle_ends_at": datetime.fromtimestamp(end, dt_util.get_default_time_zone()).isoformat() if end else None,
        }


class NextCycleSensor(IrrigationEntity, SensorEntity):
    """Next scheduled start (of a cycle that is switched on and has minutes)."""

    _attr_translation_key = "next_cycle"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator, "next_cycle")

    @property
    def native_value(self) -> datetime | None:
        nxt = self.coordinator.next_cycle()
        return dt_util.utc_from_timestamp(nxt[0]) if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = self.coordinator.next_cycle()
        return {
            "cycle": nxt[1] + 1 if nxt else None,
            "master": self.coordinator.state.master,
            "reason": self.coordinator.next_cycle_reason(),  # why there is none ("Unknown")
        }
