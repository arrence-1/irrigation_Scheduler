"""More Home Assistant tests: valve entities, start-up, sensors read from the state machine."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, callback

from custom_components.irrigation_sequencer import engine as E
from custom_components.irrigation_sequencer.const import DOMAIN

from .conftest import Rig, local, make_options
from .test_ha import cycle, master_on

pytestmark = pytest.mark.usefixtures("rig")


async def test_valve_domain_entities(hass: HomeAssistant, rig: Rig) -> None:
    """A real valve.* entity: open_valve / close_valve, 'open' / 'closed' states."""
    calls: list[str] = []

    @callback
    def handler(call: ServiceCall) -> None:
        for eid in call.data["entity_id"] if isinstance(call.data["entity_id"], list) else [call.data["entity_id"]]:
            calls.append(f"{call.service} {eid}")
            hass.states.async_set(eid, "open" if call.service == "open_valve" else "closed")

    hass.services.async_register("valve", "open_valve", handler)
    hass.services.async_register("valve", "close_valve", handler)
    hass.states.async_set("valve.front_garden", "closed")
    hass.states.async_set("valve.back_garden", "closed")
    o = make_options(rig.phone_id, taps=2)
    o["taps"][0]["valve"] = "valve.front_garden"
    o["taps"][1]["valve"] = "valve.back_garden"
    cycle(o, 0, "06:00", [1, 1])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:01:30")
    assert hass.states.is_state("valve.back_garden", "open")
    await rig.goto("2026-10-12 06:05")
    assert calls == ["open_valve valve.front_garden", "close_valve valve.front_garden",
                     "open_valve valve.back_garden", "close_valve valve.back_garden"]
    # a valve opened by hand ('open') is a manual run
    hass.states.async_set("valve.front_garden", "open")
    await rig.settle()
    assert hass.states.get("sensor.irrigation_status").state == "manual"


async def test_startup_waits_for_valves_then_recovers(hass: HomeAssistant, rig: Rig, hass_storage) -> None:
    """HA starting up: wait for valves to come online, then recover the saved run."""
    o = cycle(make_options(rig.phone_id), 0, "06:00", [10, 2, 0])
    # first run to build a real saved state with tap 1 watering until 06:10
    entry = await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:02")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    saved = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert saved["mode"] == "watering" and saved["tap"] == 0

    # "restart": HA not running yet, tap 2 still offline
    hass.set_state(CoreState.not_running)
    hass.states.async_set("input_boolean.tap_2", "unavailable")

    async def spin(n: int = 20) -> None:  # let background tasks run (timers are frozen in tests)
        for _ in range(n):
            await asyncio.sleep(0)
    rig.freezer.move_to(local("2026-10-12 06:04"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not rig.coord.ready  # waiting for HA to start
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await spin()
    assert not rig.coord.ready  # waiting for tap 2 to come online
    hass.states.async_set("input_boolean.tap_2", "off")
    await spin()
    await rig.settle()
    assert rig.coord.ready
    rec = rig.messages("irrigation_recovery")
    assert rec == ["Recovered after restart at 06:04: cycle 1 continues with Tap 1 (Backyard Left) until 06:10."], rec
    assert hass.states.is_state("input_boolean.tap_1", "on")


async def test_people_soil_seasonal_entity_from_states(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("binary_sensor.backyard_person_occupancy", "on", {"friendly_name": "Backyard person"})
    hass.states.async_set("sensor.soil_2", "61", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.irrigation_pct", "unavailable")
    o = make_options(rig.phone_id, general={"seasonal_entity": "sensor.irrigation_pct"},
                     people={"on": True, "clear_min": 1, "max_wait_min": 5})
    o["taps"][0]["people"] = ["binary_sensor.backyard_person_occupancy"]
    o["taps"][1].update(soil="sensor.soil_2", soil_pct=50)
    cycle(o, 0, "06:00", [1, 1, 1])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    assert hass.states.is_state("binary_sensor.irrigation_tap_1_blocked", "on")
    blocked = hass.states.get("binary_sensor.irrigation_tap_1_blocked")
    assert blocked.attributes["detected_by"] == "Backyard person"
    await rig.goto("2026-10-12 06:03")
    assert rig.order() == ["3"]  # tap 1 held back (person), tap 2 sat out (soil)
    assert any("Skipped Tap 2 (Backyard Centre) (soil 61%, wet enough at 50%)" in m
               for m in rig.messages("irrigation_cycle_1"))
    assert any("Seasonal adjustment is unavailable; using 100%." in m for m in rig.messages("irrigation_config"))
    assert hass.states.get("sensor.irrigation_status").state == "waiting"
    hass.states.async_set("binary_sensor.backyard_person_occupancy", "off", {"friendly_name": "Backyard person"})
    await rig.goto("2026-10-12 06:08")
    assert rig.order() == ["3", "1"], rig.log  # ran once clear for 1 min
    waits = rig.messages("irrigation_people_1")
    assert waits and waits[0].startswith("Cycle 1 is waiting: people near Tap 1 (Backyard Left).")


async def test_rain_alert_from_flickering_sensor(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("binary_sensor.rain_state", "off")
    o = make_options(rig.phone_id, weather={"rain_sensor": "binary_sensor.rain_state"}, notify={"rain": True})
    cycle(o, 0, "18:00", [1, 0, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    for k in range(6):
        hass.states.async_set("binary_sensor.rain_state", "on" if k % 2 == 0 else "off")
        await rig.goto(f"2026-10-12 05:5{1 + k}")
    assert len(rig.messages("irrigation_rain")) == 1
    assert rig.messages("irrigation_rain")[0].startswith("It has started raining (rain sensor: on).")


async def test_state_survives_reload_and_switch_states(hass: HomeAssistant, rig: Rig) -> None:
    entry = await rig.setup_entry(make_options())
    await master_on(hass, rig)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.irrigation_cycle_2"}, blocking=True)
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.irrigation_seasonal_adjustment", "value": 85}, blocking=True)
    await rig.settle()
    await hass.config_entries.async_reload(entry.entry_id)
    await rig.settle()
    assert hass.states.is_state("switch.irrigation_master", "on")
    assert hass.states.is_state("switch.irrigation_cycle_2", "off")
    assert hass.states.get("number.irrigation_seasonal_adjustment").state == "85.0"


async def test_corrupt_saved_state_resets_with_warning(hass: HomeAssistant, rig: Rig, hass_storage) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.irrigation_sequencer.model import normalize

    entry = MockConfigEntry(domain=DOMAIN, title="Irrigation", data={}, options=normalize(make_options(rig.phone_id)))
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1, "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"version": 1, "mode": "nonsense", "master": True},
    }
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.tap_2"}, blocking=True)
    rig.entry = entry
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await rig.settle()
    assert "Saved state was not readable and has been reset." in rig.messages("irrigation_config")
    assert rig.coord.state.mode == E.MANUAL  # the valve still on is treated as a manual run
    assert hass.states.is_state("switch.irrigation_master", "on")  # the user's switch survived


async def test_diagnostics(hass: HomeAssistant, rig: Rig) -> None:
    from custom_components.irrigation_sequencer.diagnostics import async_get_config_entry_diagnostics

    entry = await rig.setup_entry(make_options(rig.phone_id))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["status"] == "idle" and diag["options"]["tap_count"] == 3
    assert diag["snapshot"]["valves"] == ["off", "off", "off"]


async def test_card_resource_registration(hass: HomeAssistant, rig: Rig, monkeypatch) -> None:
    """Storage-mode dashboards: the card is added as a module resource, updated, and removed with the last entry."""
    import custom_components.irrigation_sequencer as I
    from custom_components.irrigation_sequencer.const import CARD_URL, VERSION

    class FakeResources:
        def __init__(self) -> None:
            self.items = [{"id": "old", "type": "module", "url": f"{CARD_URL}?v=0.9.0"},
                          {"id": "dup", "type": "module", "url": CARD_URL}]

        async def async_get_info(self):
            return {"resources": len(self.items)}

        def async_items(self):
            return list(self.items)

        async def async_create_item(self, data):
            self.items.append({"id": "new", "type": data["res_type"], "url": data["url"]})

        async def async_update_item(self, item_id, updates):
            for i in self.items:
                if i["id"] == item_id:
                    i.update(type=updates["res_type"], url=updates["url"])

        async def async_delete_item(self, item_id):
            self.items = [i for i in self.items if i["id"] != item_id]

    res = FakeResources()
    monkeypatch.setattr(I, "_lovelace_resources", lambda _hass: res)

    class FakeHttp:
        async def async_register_static_paths(self, paths):
            self.paths = paths

    hass.http = FakeHttp()
    await I._async_register_card(hass)
    assert res.items == [{"id": "old", "type": "module", "url": f"{CARD_URL}?v={VERSION}"}]  # updated, duplicate removed
    res.items = []
    await I._async_register_card(hass)
    assert res.items == [{"id": "new", "type": "module", "url": f"{CARD_URL}?v={VERSION}"}]  # created
    entry = await rig.setup_entry(make_options())
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert res.items == []  # removed with the last entry


async def test_settings_websocket_every_group(hass: HomeAssistant, rig: Rig, hass_ws_client) -> None:
    """The Irrigation page / card settings: read all, save each group like its Configure screen."""
    entry = await rig.setup_entry(make_options(rig.phone_id))
    ws = await hass_ws_client(hass)
    n = iter(range(1, 100))

    async def call(**msg):
        await ws.send_json({"id": next(n), **msg})
        return await ws.receive_json()

    res = await call(type=f"{DOMAIN}/settings", entry_id=entry.entry_id)
    assert res["success"], res
    assert res["result"]["options"]["general"]["gap_s"] == 5 and len(res["result"]["options"]["taps"]) == 3
    assert res["result"]["status_entity"] == "sensor.irrigation_status"
    rev0 = res["result"]["config_rev"]

    async def save(group: str, data: dict, **kw) -> dict:
        r = await call(type=f"{DOMAIN}/save_settings", entry_id=entry.entry_id, group=group, data=data, **kw)
        assert r["success"], r
        await rig.settle()
        return r["result"]

    out = await save("general", {"gap_s": 12, "confirm_s": 20, "max_run_min": 30, "late_min": 45,
                                 "seasonal_entity": "", "evil": "ignored"})
    assert out["options"]["general"]["gap_s"] == 12 and "evil" not in out["options"]["general"]
    assert out["reloading"] is False
    assert rig.coord.cfg.gap_s == 12  # applied live
    assert hass.states.get("sensor.irrigation_status").attributes["config_rev"] > rev0  # open screens refresh
    out = await save("tap", {"valve": "input_boolean.tap_1", "name": "Back Left", "people": ["binary_sensor.cam"],
                             "soil": "sensor.soil", "soil_pct": 40, "bypass": "input_boolean.pressure_washer_bypass"},
                     tap=1)
    t1 = out["options"]["taps"][0]
    assert t1["name"] == "Back Left" and t1["people"] == ["binary_sensor.cam"] and t1["soil_pct"] == 40
    assert rig.coord.cfg.taps[0].bypass == "input_boolean.pressure_washer_bypass"
    out = await save("tap", {"valve": "input_boolean.tap_1", "name": "Back Left", "people": [], "soil": "",
                             "soil_pct": 0, "bypass": ""}, tap=1)  # cleared fields come as empty values
    assert out["options"]["taps"][0]["soil"] == "" and out["options"]["taps"][0]["people"] == []
    out = await save("notify", {"devices": [rig.phone_id], "prefix": "Garden Test", "start_finish": False,
                                "skips": True, "manual": True, "people": True, "reminders": False, "rain": True,
                                "hot_c": 33})
    assert out["options"]["notify"]["prefix"] == "garden_test" and out["options"]["notify"]["hot_c"] == 33
    out = await save("soak", {"burst_min": 4, "soak_min": 9})
    assert out["options"]["soak"] == {"burst_min": 4, "soak_min": 9}
    out = await save("people", {"on": False, "clear_min": 3.5, "max_wait_min": 20, "manual": True})
    assert out["options"]["people"]["clear_min"] == 3.5 and out["options"]["people"]["on"] is False
    out = await save("weather", {"on": True, "rain_sensor": "sensor.rain", "rain_mm": 4, "weather_entity": "",
                                 "fc_prob": 60, "fc_mm": 3, "wind_sensor": "", "wind_kmh": 500, "temp_sensor": "",
                                 "cold_on": True, "cold_c": 3, "boost_pct": 120})
    assert out["options"]["weather"]["wind_kmh"] == 100  # clamped like the Configure screen's limits
    assert entry.options["weather"]["rain_sensor"] == "sensor.rain"

    # bad requests
    r = await call(type=f"{DOMAIN}/save_settings", entry_id=entry.entry_id, group="tap", data={"name": "x"}, tap=9)
    assert not r["success"] and r["error"]["code"] == "invalid"
    r = await call(type=f"{DOMAIN}/save_settings", entry_id=entry.entry_id, group="nope", data={})
    assert not r["success"]
    r = await call(type=f"{DOMAIN}/settings", entry_id="missing")
    assert not r["success"] and r["error"]["code"] == "not_found"

    # number of taps / cycles: reloads with the new entities
    rev_before = hass.states.get("sensor.irrigation_status").attributes["config_rev"]
    out = await save("counts", {"tap_count": 4, "cycle_count": 3})
    assert out["reloading"] is True
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.irrigation_tap_4_blocked") is not None
    assert hass.states.get("switch.irrigation_cycle_3") is not None
    assert entry.options["general"]["gap_s"] == 12  # everything else kept
    # the reload starts a new coordinator: its settings version is new too, so open cards and
    # settings screens fetch the grid with the new tap again (1.2.0 restarted from the same number)
    rev_after = hass.states.get("sensor.irrigation_status").attributes["config_rev"]
    assert rev_after > rev_before
    res = await call(type=f"{DOMAIN}/config", entry_id=entry.entry_id)
    assert res["result"]["entries"][0]["config_rev"] == rev_after and len(res["result"]["entries"][0]["taps"]) == 4


async def test_settings_need_admin(hass: HomeAssistant, rig: Rig, hass_ws_client, hass_read_only_access_token) -> None:
    entry = await rig.setup_entry(make_options())
    ws = await hass_ws_client(hass, hass_read_only_access_token)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/settings", "entry_id": entry.entry_id})
    res = await ws.receive_json()
    assert not res["success"] and res["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/save_settings", "entry_id": entry.entry_id, "group": "general",
                        "data": {"gap_s": 1}})
    res = await ws.receive_json()
    assert not res["success"] and res["error"]["code"] == "unauthorized"
    await ws.send_json({"id": 3, "type": f"{DOMAIN}/config"})  # the grid itself is readable for everyone
    res = await ws.receive_json()
    assert res["success"]


async def test_sidebar_page_registered_and_removed(hass: HomeAssistant, rig: Rig, monkeypatch) -> None:
    """The Irrigation page: added with the first entry (admins only), once, removed with the last entry."""
    import custom_components.irrigation_sequencer as I
    from homeassistant.components import frontend, panel_custom

    from custom_components.irrigation_sequencer.const import CARD_URL, PANEL_URL, VERSION

    calls: list[dict] = []
    panels: dict = {}

    async def register(hass_, **kw):
        calls.append(kw)
        panels[kw["frontend_url_path"]] = kw

    monkeypatch.setattr(panel_custom, "async_register_panel", register)
    monkeypatch.setattr(frontend, "async_remove_panel", lambda hass_, url, warn_if_unknown=True: panels.pop(url, None))
    monkeypatch.setattr(frontend, "add_extra_js_url", lambda hass_, url: None)
    monkeypatch.setattr(I, "_lovelace_resources", lambda hass_: None)
    hass.data[frontend.DATA_PANELS] = panels
    hass.config.components.add("frontend")

    class FakeHttp:
        async def async_register_static_paths(self, paths):
            pass

    hass.http = FakeHttp()
    e1 = await rig.setup_entry(make_options())
    assert calls == [{"frontend_url_path": PANEL_URL, "webcomponent_name": "irrigation-sequencer-panel",
                      "sidebar_title": "Irrigation", "sidebar_icon": "mdi:sprinkler-variant",
                      "module_url": f"{CARD_URL}?v={VERSION}", "require_admin": True, "config": {}}]
    await I._async_register_panel(hass)  # a second entry / a reload: not added twice
    assert len(calls) == 1
    assert await hass.config_entries.async_remove(e1.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL not in panels


async def test_next_cycle_reason(hass: HomeAssistant, rig: Rig) -> None:
    """The next-cycle sensor says why there is no next start (the card shows it instead of 'Unknown')."""
    o = make_options(rig.phone_id)
    entry = await rig.setup_entry(o)

    def nxt():
        s = hass.states.get("sensor.irrigation_next_cycle")
        return s.state, s.attributes["reason"]

    assert nxt() == ("unknown", "No cycle has minutes yet")
    o = cycle(make_options(rig.phone_id), 0, "06:00", [2, 0, 0])
    hass.config_entries.async_update_entry(entry, options=o)
    await rig.settle()
    assert nxt()[1] is None and nxt()[0] != "unknown"
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.irrigation_cycle_1"}, blocking=True)
    await rig.settle()
    assert nxt() == ("unknown", "The cycles with minutes are switched off")
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.irrigation_cycle_1"}, blocking=True)
    o = cycle(make_options(rig.phone_id), 0, "06:00", [2, 0, 0], days=[])
    hass.config_entries.async_update_entry(entry, options=o)
    await rig.settle()
    assert nxt() == ("unknown", "No days are selected for the cycles with minutes")


async def test_unload_after_start_leaves_no_listener_error(hass: HomeAssistant, rig: Rig, caplog) -> None:
    """Set up while HA starts, HA started, then the entry is disabled / unloaded: no listener error.

    1.2.x tried to remove its one-time "HA started" listener again on unload, after it had fired,
    and HA logged "Unable to remove unknown job listener".
    """
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    hass.set_state(CoreState.starting)
    entry = await rig.setup_entry(make_options())
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await rig.settle()
    assert rig.coord.ready
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    # unloaded before HA finished starting: the listener is still there and is removed cleanly
    hass.set_state(CoreState.starting)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "Unable to remove unknown job listener" not in caplog.text
    hass.set_state(CoreState.running)
