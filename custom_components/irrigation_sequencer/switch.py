"""Master switch and one switch per cycle."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import IrrigationConfigEntry
from . import engine as E
from .coordinator import IrrigationCoordinator
from .entity import IrrigationEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: IrrigationConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coord = entry.runtime_data
    entities: list[SwitchEntity] = [MasterSwitch(coord)]
    entities += [CycleSwitch(coord, i) for i in range(len(coord.cfg.cycles))]
    async_add_entities(entities)


class MasterSwitch(IrrigationEntity, SwitchEntity):
    """Off = no scheduled cycles run (manual runs still work)."""

    _attr_translation_key = "master"

    def __init__(self, coordinator: IrrigationCoordinator) -> None:
        super().__init__(coordinator, "master")

    @property
    def is_on(self) -> bool:
        return self.coordinator.state.master

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_master(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_master(False)


class CycleSwitch(IrrigationEntity, SwitchEntity):
    """A cycle's own on/off switch."""

    _attr_translation_key = "cycle"

    def __init__(self, coordinator: IrrigationCoordinator, index: int) -> None:
        super().__init__(coordinator, f"cycle_{index + 1}")
        self.index = index
        self._attr_translation_placeholders = {"n": str(index + 1)}

    @property
    def is_on(self) -> bool:
        on = self.coordinator.state.cycle_on
        return self.index < len(on) and on[self.index]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self.coordinator.cfg
        if self.index >= len(cfg.cycles):
            return {}
        c = cfg.cycles[self.index]
        tz = dt_util.get_default_time_zone()
        nxt = E.next_occurrence(c, dt_util.utcnow().timestamp(), tz)
        return {
            "start_time": c.time,
            "days": c.days,
            "total_minutes": sum(m for i, m in enumerate(c.minutes) if cfg.taps[i].valve),
            "taps": {cfg.label(i): m for i, m in enumerate(c.minutes) if m > 0 and cfg.taps[i].valve},
            "next_start": dt_util.utc_from_timestamp(nxt).astimezone(tz).isoformat() if nxt else None,
            "running": self.coordinator.state.cycle == self.index,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_cycle(self.index, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_cycle(self.index, False)
