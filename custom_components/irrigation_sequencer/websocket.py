"""Websocket API for the dashboard card: read the grid, edit cells and cycles."""

from __future__ import annotations

import copy
from typing import Any

try:  # Home Assistant 2026.10+ validates with Probatio (same API)
    import probatio as vol  # type: ignore[import-not-found]
except ImportError:  # 2026.9 and older
    import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .model import (
    DAYS,
    DEFAULT_BATTERY,
    DEFAULT_GENERAL,
    DEFAULT_MAIN,
    DEFAULT_NOTIFY,
    DEFAULT_OFFLINE,
    DEFAULT_PEOPLE,
    DEFAULT_SOAK,
    DEFAULT_TAP,
    DEFAULT_WEATHER,
    MAX_MINUTES,
    normalize,
    normalize_time,
    swap_order,
)


def _loaded(hass: HomeAssistant) -> list:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


def _entity_ids(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    reg = er.async_get(hass)
    out = {}
    for ent in er.async_entries_for_config_entry(reg, entry_id):
        key = ent.unique_id.removeprefix(f"{entry_id}_")
        out[key] = ent.entity_id
    return out


def describe(hass: HomeAssistant, entry) -> dict[str, Any]:
    o = normalize(entry.options)  # the entry is the source of truth (the live copy may lag a moment)
    ids = _entity_ids(hass, entry.entry_id)
    bats = getattr(getattr(entry, "runtime_data", None), "battery_ids", None) or []
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        # settings version: the card compares it with the status sensor's config_rev to spot edits made elsewhere
        "config_rev": getattr(getattr(entry, "runtime_data", None), "config_rev", 0),
        "max_run_min": o["general"]["max_run_min"],
        "main_valve": o["main"]["valve"],  # the main water valve (empty = not set)
        "offline_wait_min": o["offline"]["wait_min"],
        "battery_pct": o["battery"]["pct"],
        "taps": [
            {"n": i + 1, "label": f"Tap {i + 1} ({t['name']})" if t["name"] else f"Tap {i + 1}",
             "name": t["name"], "valve": t["valve"],
             "battery": bats[i] if i < len(bats) else "",  # its valve's battery sensor ("" = none found)
             "blocked_entity": ids.get(f"tap_{i + 1}_blocked")}
            for i, t in enumerate(o["taps"])
        ],
        "cycles": [
            {"n": c + 1, **cyc, "switch": ids.get(f"cycle_{c + 1}")}
            for c, cyc in enumerate(o["cycles"])
        ],
        "entities": {
            "master": ids.get("master"),
            "status": ids.get("status"),
            "next_cycle": ids.get("next_cycle"),
            "seasonal": ids.get("seasonal_adjustment"),
        },
    }


# settings groups the sidebar page / card settings can save: options key -> allowed fields
GROUPS: dict[str, tuple[str, ...]] = {
    "general": tuple(DEFAULT_GENERAL),
    "notify": tuple(DEFAULT_NOTIFY),
    "soak": tuple(DEFAULT_SOAK),
    "people": tuple(DEFAULT_PEOPLE),
    "weather": tuple(DEFAULT_WEATHER),
    "main": tuple(DEFAULT_MAIN),
    "offline": tuple(DEFAULT_OFFLINE),
    "battery": tuple(DEFAULT_BATTERY),
    "tap": tuple(DEFAULT_TAP),
    "counts": ("tap_count", "cycle_count"),
}


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_config)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_settings)
    websocket_api.async_register_command(hass, ws_save_settings)


def _entry(hass: HomeAssistant, connection, msg: dict[str, Any]):
    entries = [e for e in _loaded(hass) if e.entry_id == msg["entry_id"]]
    if not entries:
        connection.send_error(msg["id"], "not_found", "Irrigation Scheduler entry not loaded")
        return None
    return entries[0]


def settings(hass: HomeAssistant, entry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "options": normalize(entry.options),
        # lets an open settings screen notice changes made elsewhere and refresh
        "config_rev": getattr(getattr(entry, "runtime_data", None), "config_rev", 0),
        "status_entity": _entity_ids(hass, entry.entry_id).get("status"),
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings", vol.Required("entry_id"): str})
@websocket_api.require_admin
@callback
def ws_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Every setting of one entry (for the Irrigation page and the card's settings)."""
    if (entry := _entry(hass, connection, msg)) is not None:
        connection.send_result(msg["id"], settings(hass, entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_settings",
        vol.Required("entry_id"): str,
        vol.Required("group"): vol.In(list(GROUPS)),
        vol.Optional("tap"): vol.All(int, vol.Range(min=1, max=99)),
        vol.Required("data"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Save one settings group, the same as the matching Configure screen.

    Unknown fields are ignored; every value is checked and clamped by
    normalize(). Changing the tap / cycle count reloads the integration.
    """
    if (entry := _entry(hass, connection, msg)) is None:
        return
    group = msg["group"]
    data = {k: v for k, v in msg["data"].items() if k in GROUPS[group]}
    before = normalize(entry.options)
    opts = copy.deepcopy(before)
    if group == "tap":
        ti = msg.get("tap", 0) - 1
        if not 0 <= ti < opts["tap_count"]:
            connection.send_error(msg["id"], "invalid", "No such tap")
            return
        opts["taps"][ti].update(data)
    elif group == "counts":
        opts.update(data)
    else:
        opts[group].update(data)
    new = normalize(opts)
    reload = (new["tap_count"], new["cycle_count"]) != (before["tap_count"], before["cycle_count"])
    hass.config_entries.async_update_entry(entry, options=new)
    if not reload:
        await entry.runtime_data.async_wait_idle()
    connection.send_result(msg["id"], {**settings(hass, entry), "reloading": reload})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/config", vol.Optional("entry_id"): str})
@callback
def ws_config(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entries = _loaded(hass)
    if msg.get("entry_id"):
        entries = [e for e in entries if e.entry_id == msg["entry_id"]]
    connection.send_result(msg["id"], {"entries": [describe(hass, e) for e in entries]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/update",
        vol.Required("entry_id"): str,
        vol.Required("cycle"): vol.All(int, vol.Range(min=1, max=99)),
        vol.Optional("tap"): vol.All(int, vol.Range(min=1, max=99)),
        vol.Optional("minutes"): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_MINUTES)),
        vol.Optional("order"): vol.All(vol.Coerce(int), vol.Range(min=1, max=99)),
        vol.Optional("soak"): bool,
        vol.Optional("time"): str,
        vol.Optional("days"): [vol.In(DAYS)],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entries = [e for e in _loaded(hass) if e.entry_id == msg["entry_id"]]
    if not entries:
        connection.send_error(msg["id"], "not_found", "Irrigation Scheduler entry not loaded")
        return
    entry = entries[0]
    opts = copy.deepcopy(normalize(entry.options))
    ci = msg["cycle"] - 1
    if ci >= opts["cycle_count"]:
        connection.send_error(msg["id"], "invalid", "No such cycle")
        return
    cyc = opts["cycles"][ci]
    if "tap" in msg:
        ti = msg["tap"] - 1
        if ti >= opts["tap_count"]:
            connection.send_error(msg["id"], "invalid", "No such tap")
            return
        if "minutes" in msg:
            cyc["minutes"][ti] = msg["minutes"]
        if "order" in msg:  # every tap keeps its own number: the tap that had it takes the old one
            cyc["order"] = swap_order(cyc["order"], ti, msg["order"])
        if "soak" in msg:
            cyc["soak"][ti] = msg["soak"]
    if "time" in msg:
        t = normalize_time(msg["time"], "")
        if not t:
            connection.send_error(msg["id"], "invalid", "Time must be HH:MM")
            return
        cyc["time"] = t
    if "days" in msg:
        cyc["days"] = [d for d in DAYS if d in msg["days"]]
    hass.config_entries.async_update_entry(entry, options=normalize(opts))
    await entry.runtime_data.async_wait_idle()
    connection.send_result(msg["id"], describe(hass, entry))
