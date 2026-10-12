"""Diagnostics download (Settings > Devices & services > Irrigation Scheduler > ... > Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import IrrigationConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: IrrigationConfigEntry) -> dict[str, Any]:
    coord = entry.runtime_data
    sn = coord.snapshot
    return {
        "options": coord.options,
        "state": coord.state.to_dict(),
        "status": coord.status(),
        "next_wake": coord.next_wake,
        "snapshot": None if sn is None else {
            "valves": sn.valves, "people": sn.people, "bypass": sn.bypass, "soil": sn.soil,
            "rain_state": sn.rain_state, "weather_state": sn.weather_state, "wind": sn.wind, "temp": sn.temp,
        },
        "recent_messages": list(coord.messages),
        "last_notifications": coord.last_outbox,
    }
