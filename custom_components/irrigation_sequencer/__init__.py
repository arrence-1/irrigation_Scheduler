"""Irrigation Scheduler: waters a garden one tap at a time over scheduled cycles.

Replaces the Irrigation Sequencer blueprint (v1.5.1). The engine
(engine.py) holds every rule; this module wires it into Home Assistant.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CARD_URL,
    DOMAIN,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL,
    PLATFORMS,
    URL_BASE,
    VERSION,
)
from .coordinator import IrrigationCoordinator
from .model import normalize
from .services import async_register_services
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type IrrigationConfigEntry = ConfigEntry[IrrigationCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the services, the websocket API and the dashboard card (once)."""
    async_register_services(hass)
    async_register_websocket(hass)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and load it as a dashboard resource.

    Since HA 2026.8 the frontend installs its own custom-element registry
    when the app loads. A card injected early (add_extra_js_url) can define
    itself before that and is then reported as "Custom element doesn't
    exist", so the card is registered as a Lovelace resource (storage mode),
    which the dashboards load after the app is ready. YAML-mode resources
    can't be edited, so there we fall back to add_extra_js_url.
    """
    if hass.http is None:  # e.g. in tests without the http component
        return
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    path = Path(__file__).parent / "frontend"
    try:
        await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(path), False)])
    except RuntimeError:
        pass  # already registered (integration reloaded)

    async def _register(_event: Event | None = None) -> None:
        url = f"{CARD_URL}?v={VERSION}"
        resources = _lovelace_resources(hass)
        if resources is None:
            if "frontend" in hass.config.components:
                from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

                try:
                    add_extra_js_url(hass, url)
                except Exception:  # noqa: BLE001 - the card is optional; never block setup
                    _LOGGER.exception("Could not load the dashboard card %s", url)
            _LOGGER.info(
                "Dashboard resources are in YAML mode: add %s (type module) to them for the card", CARD_URL
            )
            return
        try:
            await resources.async_get_info()  # loads the collection
            mine = [i for i in resources.async_items() if str(i.get("url", "")).split("?")[0] == CARD_URL]
            if not mine:
                await resources.async_create_item({"res_type": "module", "url": url})
                _LOGGER.info("Added dashboard resource %s", url)
            else:
                if mine[0].get("url") != url or mine[0].get("type") != "module":
                    await resources.async_update_item(mine[0]["id"], {"res_type": "module", "url": url})
                for extra in mine[1:]:
                    await resources.async_delete_item(extra["id"])
        except Exception:  # noqa: BLE001 - the card is optional; never block setup
            _LOGGER.exception("Could not register the dashboard card resource %s", url)

    if hass.state is CoreState.running:
        await _register()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register)


async def _async_register_panel(hass: HomeAssistant) -> None:
    """The "Irrigation" page in the sidebar: the grid plus every setting (admins only).

    Registered with the first entry, kept while any entry exists. Uses the
    same JS module as the card.
    """
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend, panel_custom  # noqa: PLC0415

    if PANEL_URL in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{CARD_URL}?v={VERSION}",
        require_admin=True,
        config={},
    )


@callback
def _remove_panel(hass: HomeAssistant) -> None:
    if "frontend" not in hass.config.components:
        return
    from homeassistant.components import frontend  # noqa: PLC0415

    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)


def _lovelace_resources(hass: HomeAssistant):
    """The storage-mode resource collection, or None (YAML mode / dashboards not loaded)."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415
    except ImportError:
        return None
    data = hass.data.get(LOVELACE_DATA)
    if data is None or getattr(data, "resource_mode", "yaml") != "storage":
        return None
    return getattr(data, "resources", None)


async def async_setup_entry(hass: HomeAssistant, entry: IrrigationConfigEntry) -> bool:
    coordinator = IrrigationCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    try:
        await _async_register_panel(hass)
    except Exception:  # noqa: BLE001 - the page is optional; never block setup
        _LOGGER.exception("Could not add the Irrigation page to the sidebar")
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: IrrigationConfigEntry) -> None:
    """Options changed: reload when the tap or cycle count changed, else apply live."""
    coordinator = entry.runtime_data
    new = normalize(entry.options)
    if (new["tap_count"], new["cycle_count"]) != (len(coordinator.cfg.taps), len(coordinator.cfg.cycles)):
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    await coordinator.async_apply_options(new)


async def async_unload_entry(hass: HomeAssistant, entry: IrrigationConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the saved run state; drop the card resource when the last entry goes."""
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from .const import STORAGE_VERSION  # noqa: PLC0415

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    if any(e.entry_id != entry.entry_id for e in hass.config_entries.async_entries(DOMAIN)):
        return
    _remove_panel(hass)
    resources = _lovelace_resources(hass)
    if resources is None:
        return
    try:
        await resources.async_get_info()
        for item in resources.async_items():
            if str(item.get("url", "")).split("?")[0] == CARD_URL:
                await resources.async_delete_item(item["id"])
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not remove the dashboard card resource")
