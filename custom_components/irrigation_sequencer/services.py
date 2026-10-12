"""Actions: run_cycle, stop, skip_tap, run_tap."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:  # Home Assistant 2026.10+ validates with Probatio (same API)
    import probatio as vol  # type: ignore[import-not-found]
except ImportError:  # 2026.9 and older
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_RUN_CYCLE, SERVICE_RUN_TAP, SERVICE_SKIP_TAP, SERVICE_STOP

if TYPE_CHECKING:
    from .coordinator import IrrigationCoordinator

ATTR_ENTRY = "config_entry_id"

BASE = {vol.Optional(ATTR_ENTRY): cv.string}
SCHEMAS = {
    SERVICE_RUN_CYCLE: vol.Schema({**BASE, vol.Required("cycle"): vol.All(vol.Coerce(int), vol.Range(min=1, max=99))}),
    SERVICE_STOP: vol.Schema(BASE),
    SERVICE_SKIP_TAP: vol.Schema({**BASE, vol.Required("tap"): vol.All(vol.Coerce(int), vol.Range(min=1, max=99))}),
    SERVICE_RUN_TAP: vol.Schema(
        {
            **BASE,
            vol.Required("tap"): vol.All(vol.Coerce(int), vol.Range(min=1, max=99)),
            vol.Optional("minutes"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=240)),
        }
    ),
}


def get_coordinator(hass: HomeAssistant, entry_id: str | None) -> IrrigationCoordinator:
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
        if not entries:
            raise ServiceValidationError(f"Irrigation Sequencer entry {entry_id} is not loaded")
    if not entries:
        raise ServiceValidationError("Irrigation Sequencer is not set up")
    if len(entries) > 1:
        raise ServiceValidationError("More than one Irrigation Sequencer: give config_entry_id")
    return entries[0].runtime_data


@callback
def async_register_services(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> None:
        coord = get_coordinator(hass, call.data.get(ATTR_ENTRY))
        T, C = len(coord.cfg.taps), len(coord.cfg.cycles)
        if call.service == SERVICE_STOP:
            coord.request("service", {"type": "stop"})
        elif call.service == SERVICE_RUN_CYCLE:
            c = call.data["cycle"]
            if c > C:
                raise ServiceValidationError(f"There are only {C} cycles")
            coord.request("service", {"type": "run_cycle", "cycle": c - 1})
        elif call.service == SERVICE_SKIP_TAP:
            t = call.data["tap"]
            if t > T:
                raise ServiceValidationError(f"There are only {T} taps")
            coord.request("service", {"type": "skip_tap", "tap": t - 1})
        elif call.service == SERVICE_RUN_TAP:
            t = call.data["tap"]
            if t > T:
                raise ServiceValidationError(f"There are only {T} taps")
            if not coord.cfg.taps[t - 1].valve:
                raise ServiceValidationError(f"Tap {t} has no valve")
            from .engine import manual_seconds  # noqa: PLC0415

            minutes = call.data.get("minutes")
            secs = int(minutes * 60) if minutes else manual_seconds(coord.cfg, t - 1)
            coord.request("service", {"type": "run_tap", "tap": t - 1, "seconds": secs})
        await coord.async_wait_idle()

    for name, schema in SCHEMAS.items():
        if not hass.services.has_service(DOMAIN, name):
            hass.services.async_register(DOMAIN, name, handle, schema=schema)
