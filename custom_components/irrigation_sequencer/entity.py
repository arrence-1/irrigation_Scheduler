"""Base entity for Irrigation Sequencer."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from .coordinator import IrrigationCoordinator


class IrrigationEntity(Entity):
    """An entity whose state comes from the coordinator."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: IrrigationCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.add_listener(self.async_write_ha_state))
