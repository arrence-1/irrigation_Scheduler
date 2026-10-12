"""Seasonal adjustment slider (0-200 %)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import IrrigationConfigEntry
from .coordinator import IrrigationCoordinator
from .entity import IrrigationEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: IrrigationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([SeasonalAdjustment(entry.runtime_data)])


class SeasonalAdjustment(IrrigationEntity, NumberEntity):
    """Multiplies every scheduled duration. 0 % = cycles skipped."""

    _attr_translation_key = "seasonal_adjustment"
    _attr_native_min_value = 0
    _attr_native_max_value = 200
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator, "seasonal_adjustment")

    @property
    def native_value(self) -> float:
        return self.coordinator.state.seasonal

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        src = self.coordinator.cfg.seasonal_entity
        return {"used_instead": src} if src else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_seasonal(value)
