"""Fixtures for the Home Assistant tests: toggle helpers as pretend valves, a fake phone."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.irrigation_sequencer.const import DOMAIN
from custom_components.irrigation_sequencer.model import normalize

pytest_plugins = "pytest_homeassistant_custom_component"

SYD = ZoneInfo("Australia/Sydney")
TAPS = 3


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


# Home Assistant announces most breaking changes months ahead with a log line such as
# "Detected that custom integration 'irrigation_sequencer' ... This will stop working in Home Assistant 2027.4".
# Failing on it here turns the weekly test against the newest Home Assistant (GitHub Actions) into an early
# warning, long before an update actually breaks the integration.
EARLY_WARNING = ("detected that", "deprecat", "will stop working", "no longer supported")


@pytest.fixture(autouse=True)
def no_deprecation_reports(request, caplog):
    yield
    found = []
    for when in ("setup", "call"):
        for rec in caplog.get_records(when):
            text = rec.getMessage()
            low = text.lower()
            if "irrigation_sequencer" in low and any(w in low for w in EARLY_WARNING):
                found.append(text)
    if found:
        pytest.fail("Home Assistant warns that something this integration uses is going away:\n" + "\n".join(found))


def local(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M").replace(tzinfo=SYD)


class Rig:
    """Test rig: pretend valves, a valve log, the notifications sent, time travel."""

    def __init__(self, hass: HomeAssistant, freezer) -> None:
        self.hass = hass
        self.freezer = freezer
        self.log: list[tuple[datetime, str, str]] = []
        self.max_open = 0  # any moment (a person opening a tap counts too)
        self.app_max_open = 0  # at the moments the APP opened a tap: must always be 1
        self._app_opening: str | None = None
        self.notes = async_mock_service(hass, "notify", "mobile_app_test_phone")
        self.entry: MockConfigEntry | None = None
        self.phone_id = ""

    async def setup_valves(self, n: int = TAPS, extra: list[str] | None = None) -> None:
        conf = {f"tap_{i}": {"name": f"Tap {i}"} for i in range(1, n + 1)}
        for e in extra or []:
            conf[e] = {"name": e}
        assert await async_setup_component(self.hass, "input_boolean", {"input_boolean": conf})
        valves = [f"input_boolean.tap_{i}" for i in range(1, n + 1)]

        @callback
        def _log(event: Event) -> None:
            new = event.data["new_state"]
            self.log.append((datetime.now(SYD), event.data["entity_id"], new.state if new else "none"))
            n_open = sum(self.hass.states.is_state(v, "on") for v in valves)
            self.max_open = max(self.max_open, n_open)
            if new is not None and new.state == "on" and event.data["entity_id"] == self._app_opening:
                self.app_max_open = max(self.app_max_open, n_open)

        async_track_state_change_event(self.hass, valves, _log)

    async def setup_phone(self) -> None:
        phone = MockConfigEntry(domain="mobile_app", data={"device_name": "Test Phone"})
        phone.add_to_hass(self.hass)
        dev = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=phone.entry_id, identifiers={("mobile_app", "test-phone")}, name="Test Phone"
        )
        self.phone_id = dev.id

    async def setup_entry(self, options: dict, title: str = "Irrigation") -> MockConfigEntry:
        self.entry = MockConfigEntry(domain=DOMAIN, title=title, data={}, options=normalize(options))
        self.entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.settle()
        return self.entry

    @property
    def coord(self):
        return self.entry.runtime_data

    def _wrap(self) -> None:
        """Tag the valves the app itself opens (to check 'never two open' for the app's own actions)."""
        coord = self.coord
        if getattr(coord, "_rig_wrapped", False):
            return
        real = coord._service

        async def service(entity_id: str, on: bool) -> None:
            self._app_opening = entity_id if on else None
            try:
                await real(entity_id, on)
            finally:
                self._app_opening = None

        coord._service = service
        coord._rig_wrapped = True

    async def settle(self) -> None:
        if self.entry is not None and getattr(self.entry, "runtime_data", None) is not None:
            self._wrap()
        for _ in range(4):
            await self.hass.async_block_till_done()
            if self.entry is not None and getattr(self.entry, "runtime_data", None) is not None:
                await self.coord.async_wait_idle()
        await self.hass.async_block_till_done()

    async def goto(self, when: str, step_s: float = 5) -> None:
        """Move the clock forward to `when` in steps, firing timers on the way."""
        target = local(when)
        while datetime.now(SYD) < target:
            nxt = min(datetime.now(SYD) + timedelta(seconds=step_s), target)
            self.freezer.move_to(nxt)
            async_fire_time_changed(self.hass, nxt)
            await self.settle()

    def periods(self, entity: str) -> list[tuple[str, str]]:
        out, start = [], None
        for t, e, st in self.log:
            if e != entity:
                continue
            if st == "on":
                start = t
            elif st == "off" and start is not None:
                out.append((start.strftime("%H:%M:%S"), t.strftime("%H:%M:%S")))
                start = None
        return out

    def order(self) -> list[str]:
        return [e.split("_")[-1] for _t, e, st in self.log if st == "on"]

    def messages(self, tag_part: str = "") -> list[str]:
        return [c.data.get("message", "") for c in self.notes if tag_part in c.data.get("data", {}).get("tag", "")]


@pytest.fixture
async def rig(hass: HomeAssistant, freezer) -> Rig:
    await hass.config.async_set_time_zone("Australia/Sydney")
    freezer.move_to(local("2026-10-12 05:50"))
    r = Rig(hass, freezer)
    await r.setup_valves(extra=["pressure_washer_bypass"])
    await r.setup_phone()
    return r


def make_options(phone_id: str = "", taps: int = TAPS, cycles: int = 2, **groups) -> dict:
    o = normalize({"tap_count": taps, "cycle_count": cycles})
    for i in range(taps):
        o["taps"][i]["valve"] = f"input_boolean.tap_{i + 1}"
        o["taps"][i]["name"] = ["Backyard Left", "Backyard Centre", "Front Lawn", "Tap4", "Tap5", "Tap6"][i]
    if phone_id:
        o["notify"]["devices"] = [phone_id]
    for k, v in groups.items():
        if isinstance(v, dict):
            o[k].update(v)
        else:
            o[k] = v
    return o


Callable  # re-export for type hints in tests
