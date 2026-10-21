"""Notifications: the ONE place every message goes out.

Each message type has its own tag, so a newer message replaces the older
one of the same type; all messages share one group / iOS thread. Messages
with the same tag in one pass are merged into one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import slugify

from .const import EVENT_MESSAGE

if TYPE_CHECKING:
    from .model import Cfg

_LOGGER = logging.getLogger(__name__)

TITLES = {
    "cycle": "Cycle {n}",
    "skip": "Cycle {n}",
    "reminder": "Cycle {n} reminder",
    "manual": "Manual run",
    "tap": "Tap {n} problem",
    "people": "Tap {n} people guard",
    "recovery": "Recovered",
    "config": "Settings",
    "hot": "Too hot",
    "rain": "Raining",
    "main": "Main water",
}
NUMBERED = ("cycle", "reminder", "tap", "people")
# main water messages share one tag, so each replaces the one before (alert -> closed -> open again)
MAIN_KINDS = ("main", "main_alert", "main_auto", "main_open")
# iOS: a critical alert sounds even on silent / Do Not Disturb (the Companion app asks for this permission)
CRITICAL = ("main_alert", "main_auto")
TIME_SENSITIVE = ("tap", "main", "main_open")


def tag_for(prefix: str, kind: str, n: int) -> str:
    kind = "cycle" if kind == "skip" else ("main" if kind in MAIN_KINDS else kind)
    return f"{prefix}_{kind}" + (f"_{n}" if kind in NUMBERED else "")


def _push(prefix: str, kind: str) -> dict[str, Any]:
    if kind in CRITICAL:
        return {"thread-id": prefix, "interruption-level": "critical",
                "sound": {"name": "default", "critical": 1, "volume": 1.0}}
    return {"thread-id": prefix, "interruption-level": "time-sensitive" if kind in TIME_SENSITIVE else "active"}


def build_outbox(cfg: Cfg, msgs: list[tuple[str, int, str]], master_on: bool) -> list[dict[str, Any]]:
    """Turn engine messages into notify payloads (filtered, merged per tag)."""
    allow = {
        "manual": cfg.n_manual,
        "reminder": cfg.n_reminders,
        "skip": cfg.n_skips,
        "rain": cfg.n_rain,
        "people": cfg.n_people,
        "hot": cfg.hot_c > 0,
    }
    prefix = cfg.n_prefix or "irrigation"
    title_prefix = prefix.replace("_", " ").title()
    out: dict[str, dict[str, Any]] = {}
    for kind, n, text in msgs:
        if not allow.get(kind, True):
            continue
        tag = tag_for(prefix, kind, n)
        if tag in out:
            out[tag]["message"] += " " + text
            continue
        data: dict[str, Any] = {"group": prefix, "tag": tag, "push": _push(prefix, kind)}
        if kind in ("reminder", "hot") and not master_on:
            data["actions"] = [{"action": f"{prefix}_master_on", "title": "Turn master switch on"}]
        elif kind == "main_alert":  # closing is the safe direction: no unlock needed
            data["actions"] = [{"action": f"{prefix}_main_close", "title": "Close main water", "destructive": True}]
        elif kind == "main_open":  # opening lets water flow again: the phone must be unlocked
            data["actions"] = [{"action": f"{prefix}_main_open", "title": "Open main water",
                                "authenticationRequired": True}]
        title_kind = "main" if kind in MAIN_KINDS else kind
        out[tag] = {
            "title": f"{title_prefix} - " + TITLES.get(title_kind, title_kind.title()).format(n=n),
            "message": text,
            "data": data,
        }
    return list(out.values())


def notify_services(hass: HomeAssistant, device_ids: list[str]) -> list[str]:
    """Companion app devices -> notify service names (mobile_app_<device name>)."""
    reg = dr.async_get(hass)
    services: list[str] = []
    for device_id in device_ids:
        dev = reg.async_get(device_id)
        if dev is None:
            continue
        names: list[str] = []
        entry_id = getattr(dev, "config_entry_id", None)
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is not None and entry.domain == "mobile_app" and entry.data.get("device_name"):
            names.append(entry.data["device_name"])
        names += [n for n in (dev.name, dev.name_by_user) if n]
        for name in names:
            svc = f"mobile_app_{slugify(name)}"
            if hass.services.has_service("notify", svc):
                services.append(svc)
                break
        else:
            if names:
                _LOGGER.warning("No notify service found for device %s (tried mobile_app_%s)", device_id, slugify(names[0]))
    return list(dict.fromkeys(services))


async def async_send(
    hass: HomeAssistant,
    cfg: Cfg,
    msgs: list[tuple[str, int, str]],
    clears: list[str],
    master_on: bool,
    entry_id: str,
) -> list[dict[str, Any]]:
    """Send messages and clears to every chosen phone. Returns the outbox (for logs/tests)."""
    outbox = build_outbox(cfg, msgs, master_on)
    for kind, n, text in msgs:
        _LOGGER.info("[%s %s] %s", kind, n, text)
        hass.bus.async_fire(EVENT_MESSAGE, {"entry_id": entry_id, "kind": kind, "number": n, "message": text})
    targets = notify_services(hass, cfg.n_devices) if (outbox or clears) else []
    for payload in outbox:
        for svc in targets:
            try:
                await hass.services.async_call("notify", svc, payload, blocking=False)
            except Exception:  # noqa: BLE001 - a phone problem must never stop the engine
                _LOGGER.exception("Could not send notification via notify.%s", svc)
    prefix = cfg.n_prefix or "irrigation"
    for suffix in clears:
        for svc in targets:
            try:
                await hass.services.async_call(
                    "notify", svc, {"message": "clear_notification", "data": {"tag": f"{prefix}_{suffix}"}}, blocking=False
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Could not clear notification via notify.%s", svc)
    return outbox
