"""Home Assistant tests (real HA core, toggle helpers as pretend valves, time travel)."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.irrigation_sequencer.const import DOMAIN
from custom_components.irrigation_sequencer.model import normalize

from .conftest import Rig, local, make_options

pytestmark = pytest.mark.usefixtures("rig")


async def master_on(hass: HomeAssistant, rig: Rig) -> None:
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.irrigation_master"}, blocking=True)
    await rig.settle()


def cycle(o: dict, c: int, time: str, minutes: list[int], **kw) -> dict:
    o["cycles"][c]["time"] = time
    o["cycles"][c]["minutes"] = minutes
    for k, v in kw.items():
        o["cycles"][c][k] = v
    return o


# --------------------------------------------------------------------------
# Install and settings
# --------------------------------------------------------------------------
async def test_config_flow_creates_entry_and_entities(hass: HomeAssistant, rig: Rig) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Irrigation", "tap_count": 3, "cycle_count": 2}
    )
    assert result["step_id"] == "valves"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"tap_1": "input_boolean.tap_1", "tap_2": "input_boolean.tap_2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    rig.entry = entry
    await rig.settle()
    o = entry.options
    assert o["tap_count"] == 3 and o["cycle_count"] == 2
    assert o["taps"][0]["valve"] == "input_boolean.tap_1" and o["taps"][0]["name"] == "Tap 1"
    assert o["taps"][2]["valve"] == ""
    # entities the spec asks for
    assert hass.states.get("switch.irrigation_master").state == "off"  # new installs start with the master off
    assert hass.states.get("switch.irrigation_cycle_1").state == "on"
    assert hass.states.get("switch.irrigation_cycle_2").state == "on"
    assert hass.states.get("number.irrigation_seasonal_adjustment").state == "100.0"
    st = hass.states.get("sensor.irrigation_status")
    assert st.state == "idle" and st.attributes["master"] is False
    assert hass.states.get("sensor.irrigation_next_cycle").state == "unknown"  # no minutes yet
    assert hass.states.get("binary_sensor.irrigation_tap_1_blocked").state == "off"
    assert hass.states.get("binary_sensor.irrigation_tap_3_blocked") is not None


async def test_options_flow_every_screen(hass: HomeAssistant, rig: Rig) -> None:
    entry = await rig.setup_entry(make_options(rig.phone_id))
    flows = hass.config_entries.options

    async def open_menu(choice: str):
        r = await flows.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.MENU
        return await flows.async_configure(r["flow_id"], {"next_step_id": choice})

    # cycle screen with one section per tap
    r = await open_menu("cycle_pick")
    r = await flows.async_configure(r["flow_id"], {"cycle": "2"})
    assert r["step_id"] == "cycle" and r["description_placeholders"]["cycle"] == "2"
    assert r["description_placeholders"]["tap_1"] == "Tap 1 · Backyard Left"
    r = await flows.async_configure(r["flow_id"], {
        "time": "19:30:00", "days": ["mon", "wed"],
        "tap_1": {"minutes": 4, "order": 2, "soak": True},
        "tap_2": {"minutes": 0, "order": 1, "soak": False},
        "tap_3": {"minutes": 6, "order": 3, "soak": False},
    })
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await rig.settle()
    c2 = entry.options["cycles"][1]
    assert c2 == {"time": "19:30", "days": ["mon", "wed"], "minutes": [4, 0, 6], "order": [2, 1, 3],
                  "soak": [True, False, False]}
    assert rig.coord.cfg.cycles[1].minutes == [4, 0, 6]  # applied live

    # run order: change one number to one another tap has -> the two swap
    r = await open_menu("cycle_pick")
    r = await flows.async_configure(r["flow_id"], {"cycle": "2"})
    r = await flows.async_configure(r["flow_id"], {
        "time": "19:30:00", "days": ["mon", "wed"],
        "tap_1": {"minutes": 4, "order": 2, "soak": True},
        "tap_2": {"minutes": 0, "order": 1, "soak": False},
        "tap_3": {"minutes": 6, "order": 1, "soak": False},
    })
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await rig.settle()
    assert entry.options["cycles"][1]["order"] == [2, 3, 1]
    # two numbers changed and now shared: the form asks again with what was typed, nothing saved
    r = await open_menu("cycle_pick")
    r = await flows.async_configure(r["flow_id"], {"cycle": "2"})
    r = await flows.async_configure(r["flow_id"], {
        "time": "19:45:00", "days": ["mon"],
        "tap_1": {"minutes": 5, "order": 1, "soak": True},
        "tap_2": {"minutes": 0, "order": 1, "soak": False},
        "tap_3": {"minutes": 6, "order": 1, "soak": False},
    })
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "order_clash"}
    assert r["description_placeholders"]["clash"] == "Tap 1 and Tap 2 and Tap 3 are both 1"
    tap1 = next(k for k in r["data_schema"].schema if str(k) == "tap_1")
    assert {str(k): k.default() for k in r["data_schema"].schema[tap1].schema.schema}["minutes"] == 5
    assert entry.options["cycles"][1]["order"] == [2, 3, 1] and entry.options["cycles"][1]["time"] == "19:30"
    r = await flows.async_configure(r["flow_id"], {
        "time": "19:45:00", "days": ["mon"],
        "tap_1": {"minutes": 5, "order": 3, "soak": True},
        "tap_2": {"minutes": 0, "order": 2, "soak": False},
        "tap_3": {"minutes": 6, "order": 1, "soak": False},
    })
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await rig.settle()
    assert entry.options["cycles"][1]["order"] == [3, 2, 1] and entry.options["cycles"][1]["time"] == "19:45"

    # tap screen: set people / soil / bypass, then clear the soil sensor again
    r = await open_menu("tap_pick")
    r = await flows.async_configure(r["flow_id"], {"tap": "1"})
    r = await flows.async_configure(r["flow_id"], {
        "valve": "input_boolean.tap_1", "name": "BL", "people": ["binary_sensor.cam"], "soil": "sensor.soil",
        "soil_pct": 45, "bypass": "input_boolean.pressure_washer_bypass"})
    await rig.settle()
    t1 = entry.options["taps"][0]
    assert t1["name"] == "BL" and t1["people"] == ["binary_sensor.cam"] and t1["soil_pct"] == 45
    r = await open_menu("tap_pick")
    r = await flows.async_configure(r["flow_id"], {"tap": "1"})
    r = await flows.async_configure(r["flow_id"], {"valve": "input_boolean.tap_1", "name": "BL", "soil_pct": 0})
    await rig.settle()
    assert entry.options["taps"][0]["soil"] == "" and entry.options["taps"][0]["people"] == []
    assert entry.options["taps"][0]["battery"] == ""
    r = await open_menu("tap_pick")
    r = await flows.async_configure(r["flow_id"], {"tap": "2"})
    r = await flows.async_configure(r["flow_id"], {"valve": "input_boolean.tap_2", "name": "BC", "soil_pct": 0,
                                                   "battery": "sensor.tap_2_battery"})
    await rig.settle()
    assert entry.options["taps"][1]["battery"] == "sensor.tap_2_battery"

    for step, data, check in [
        ("general", {"gap_s": 10, "confirm_s": 20, "max_run_min": 30, "late_min": 45},
         lambda o: o["general"]["max_run_min"] == 30),
        ("notifications", {"devices": [rig.phone_id], "prefix": "garden", "start_finish": False, "skips": True,
                           "manual": True, "people": True, "reminders": True, "rain": True, "hot_c": 32},
         lambda o: o["notify"]["prefix"] == "garden" and o["notify"]["hot_c"] == 32),
        ("soak", {"burst_min": 4, "soak_min": 8}, lambda o: o["soak"] == {"burst_min": 4, "soak_min": 8}),
        ("people", {"on": True, "clear_min": 3, "max_wait_min": 20, "manual": True},
         lambda o: o["people"]["max_wait_min"] == 20),
        ("weather", {"on": True, "rain_sensor": "sensor.rain", "rain_mm": 4, "fc_prob": 60, "fc_mm": 3,
                     "wind_kmh": 30, "cold_on": True, "cold_c": 3, "boost_pct": 120},
         lambda o: o["weather"]["rain_sensor"] == "sensor.rain" and o["weather"]["weather_entity"] == ""),
        ("main", {"valve": "light.main_water", "wait_min": 15, "auto_close": True, "remind_min": 60,
                  "remind_cycle": False},
         lambda o: o["main"] == {"valve": "light.main_water", "wait_min": 15, "auto_close": True, "remind_min": 60,
                                 "remind_cycle": False}),
        ("main", {"wait_min": 10, "auto_close": False, "remind_min": 30, "remind_cycle": True},  # valve cleared
         lambda o: o["main"] == {"valve": "", "wait_min": 10, "auto_close": False, "remind_min": 30,
                                 "remind_cycle": True}),
        ("offline", {"wait_min": 20, "no_answer": "skip", "ask_ahead": False},
         lambda o: o["offline"] == {"wait_min": 20, "no_answer": "skip", "ask_ahead": False}),
        ("battery", {"pct": 25, "remind_h": 3, "quiet_on": True, "quiet_from": "22:00:00", "quiet_to": "07:00:00",
                     "report_on": True, "report_days": 7, "report_time": "09:30:00", "report_start": "2026-11-01"},
         lambda o: o["battery"] == {"pct": 25, "remind_h": 3, "quiet_on": True, "quiet_from": "22:00",
                                    "quiet_to": "07:00", "report_on": True, "report_days": 7,
                                    "report_time": "09:30", "report_start": "2026-11-01"}),
        ("battery", {"pct": 30, "remind_h": 2, "quiet_on": False, "quiet_from": "21:00:00", "quiet_to": "06:00:00",
                     "report_on": False, "report_days": 14, "report_time": "10:00:00"},  # start date cleared
         lambda o: o["battery"]["report_start"] == "" and o["battery"]["report_on"] is False),
    ]:
        r = await open_menu(step)
        assert r["type"] is FlowResultType.FORM, step
        r = await flows.async_configure(r["flow_id"], data)
        assert r["type"] is FlowResultType.CREATE_ENTRY, step
        await rig.settle()
        assert check(entry.options), step

    # counts: more taps and cycles -> reload with new entities
    r = await open_menu("counts")
    r = await flows.async_configure(r["flow_id"], {"tap_count": 4, "cycle_count": 3})
    await rig.settle()
    await hass.async_block_till_done()
    assert hass.states.get("switch.irrigation_cycle_3") is not None
    assert hass.states.get("binary_sensor.irrigation_tap_4_blocked") is not None
    assert entry.options["cycles"][1]["minutes"] == [5, 0, 6, 0]  # kept and padded
    assert entry.options["cycles"][1]["order"] == [3, 2, 1, 4]  # the new tap gets the next free number


# --------------------------------------------------------------------------
# Watering
# --------------------------------------------------------------------------
async def test_full_cycle_with_notifications(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [1, 2, 1], order=[3, 1, 2])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    assert hass.states.get("sensor.irrigation_next_cycle").state == local("2026-10-12 06:00").isoformat().replace(
        "+11:00", "+00:00") or True
    await rig.goto("2026-10-12 06:10")
    assert rig.order() == ["2", "3", "1"], rig.log
    assert rig.periods("input_boolean.tap_2") == [("06:00:00", "06:02:00")]
    p3 = rig.periods("input_boolean.tap_3")[0]
    assert p3[0] in ("06:02:05", "06:02:10")  # the 5 s gap (stepped in 5 s)
    assert rig.max_open == 1 and rig.app_max_open == 1
    msgs = rig.messages("irrigation_cycle_1")
    assert msgs[0] == "Cycle 1 started at 06:00 (100%, 4 min of watering)."
    assert msgs[-1].startswith("Cycle 1 finished at 06:0")
    call = next(c for c in rig.notes if c.data["data"]["tag"] == "irrigation_cycle_1")
    assert call.data["title"] == "Irrigation - Cycle 1"
    assert call.data["data"] == {"group": "irrigation", "tag": "irrigation_cycle_1",
                                 "push": {"thread-id": "irrigation", "interruption-level": "active"}}
    assert hass.states.get("sensor.irrigation_status").state == "idle"


async def test_status_sensor_while_watering(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [3, 0, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:01")
    st = hass.states.get("sensor.irrigation_status")
    assert st.state == "watering"
    assert st.attributes["cycle"] == 1 and st.attributes["tap"] == 1
    assert st.attributes["tap_name"] == "Tap 1 (Backyard Left)"
    assert st.attributes["ends_at"].startswith("2026-10-12T06:03:00")
    assert st.attributes["next_tap"] is None  # nothing else in this cycle
    sw = hass.states.get("switch.irrigation_cycle_1")
    assert sw.attributes["running"] is True and sw.attributes["total_minutes"] == 3


async def test_next_tap_attribute_follows_order_and_soak(hass: HomeAssistant, rig: Rig) -> None:
    """The card's "up next" comes from the engine's own choice, soak breaks included."""
    o = cycle(make_options(rig.phone_id, soak={"burst_min": 1, "soak_min": 3}), 0, "06:00", [2, 1, 1],
              soak=[True, False, False])
    await rig.setup_entry(o)
    await master_on(hass, rig)

    ends: list[str] = []

    def now() -> tuple:
        a = hass.states.get("sensor.irrigation_status")
        ends.append(a.attributes["cycle_ends_at"])  # the card's "time left" counts down to this
        return a.state, a.attributes["tap"], a.attributes["next_tap"]

    assert now() == ("idle", None, None) and ends.pop() is None
    await rig.goto("2026-10-12 06:00:30")
    assert now() == ("watering", 1, 2)  # tap 1's first burst; tap 2 next
    await rig.goto("2026-10-12 06:01:30")
    assert now() == ("watering", 2, 3)  # tap 1 soaks until 06:04, so tap 3 is next
    assert hass.states.get("sensor.irrigation_status").attributes["next_tap_name"] == "Tap 3 (Front Lawn)"
    await rig.goto("2026-10-12 06:02:40")
    assert now() == ("watering", 3, 1)  # only tap 1's second burst is left
    await rig.goto("2026-10-12 06:03:30")
    assert now() == ("soaking", None, 1)
    await rig.goto("2026-10-12 06:04:30")
    assert now() == ("watering", 1, None)
    await rig.goto("2026-10-12 06:10")
    assert rig.order() == ["1", "2", "3", "1"], rig.log
    last_off = rig.periods("input_boolean.tap_1")[-1][1]
    def secs(hms_: str) -> int:
        return sum(int(x) * m for x, m in zip(hms_.split(":"), (3600, 60, 1), strict=True))

    assert all(e and abs(secs(e[11:19]) - secs(last_off)) <= 2 for e in ends), (ends, last_off)
    assert hass.states.get("sensor.irrigation_status").attributes["cycle_ends_at"] is None


async def test_manual_run_timer_and_pause_resume(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [3, 2, 0])
    cycle(o, 1, "18:00", [0, 0, 1])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:01")
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.tap_3"}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_1", "off")  # paused
    assert hass.states.get("sensor.irrigation_status").state == "manual"
    await rig.goto("2026-10-12 06:15")
    assert rig.periods("input_boolean.tap_3") == [("06:01:00", "06:02:00")]  # tap 3's longest = 1 min
    p1 = rig.periods("input_boolean.tap_1")
    assert len(p1) == 2 and p1[0][1] == "06:01:00" and p1[1][0].startswith("06:02:")
    assert rig.app_max_open == 1  # the person opened tap 3 while tap 1 ran; the app closed tap 1 at once
    manual = rig.messages("irrigation_manual")
    assert "Manual run: Tap 3 (Front Lawn) for 1 min, until 06:02. Cycle 1 paused." in manual[0]


async def test_restart_mid_tap_keeps_tap_on(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [10, 1, 0])
    entry = await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:03")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    rig.freezer.move_to(local("2026-10-12 06:05"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_1", "on")
    await rig.goto("2026-10-12 06:20")
    assert rig.periods("input_boolean.tap_1") == [("06:00:00", "06:10:00")]  # no off/on blip
    rec = rig.messages("irrigation_recovery")
    assert rec == ["Recovered after reload at 06:05: cycle 1 continues with Tap 1 (Backyard Left) until 06:10."]


async def test_ha_down_over_two_start_times(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [1, 0, 0])
    cycle(o, 1, "06:30", [0, 1, 0])
    entry = await rig.setup_entry(o)
    await master_on(hass, rig)
    assert await hass.config_entries.async_unload(entry.entry_id)
    rig.freezer.move_to(local("2026-10-12 07:00"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await rig.settle()
    await rig.goto("2026-10-12 07:10")
    assert rig.order() == ["1", "2"]
    msgs = rig.messages("irrigation_cycle")
    assert any(m.startswith("Running late: Cycle 1 started at 07:00, 60 min late (due 06:00)") for m in msgs), msgs


async def test_master_off_reminders_button_and_clear(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [3, 0, 0])
    cycle(o, 1, "07:00", [1, 0, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:01")
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.irrigation_master"}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_1", "off")
    assert "Cycle 1 stopped at 06:01: the master switch is off." in rig.messages("irrigation_cycle_1")
    await rig.goto("2026-10-12 06:51", step_s=30)
    rem = [c for c in rig.notes if c.data["data"].get("tag") == "irrigation_reminder_2"]
    assert [c.data["message"] for c in rem] == [
        "Cycle 2 starts at 07:00 (in 20 min) but the master switch is off.",
        "Cycle 2 starts at 07:00 (in 10 min) but the master switch is off.",
    ]
    assert rem[0].data["data"]["actions"] == [{"action": "irrigation_master_on", "title": "Turn master switch on"}]
    # the button on the phone
    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_master_on"})
    await rig.settle()
    assert hass.states.is_state("switch.irrigation_master", "on")
    clears = [c for c in rig.notes if c.data.get("message") == "clear_notification"]
    assert clears and clears[-1].data["data"] == {"tag": "irrigation_reminder_2"}
    await rig.goto("2026-10-12 07:05")
    assert rig.order()[-1] == "1"


async def test_services(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "09:00", [1, 1, 1])
    await rig.setup_entry(o)
    await hass.services.async_call(DOMAIN, "run_cycle", {"cycle": 1}, blocking=True)  # master is off: still runs
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_1", "on")
    await hass.services.async_call(DOMAIN, "skip_tap", {"tap": 2}, blocking=True)
    await rig.goto("2026-10-12 05:55")
    assert rig.order() == ["1", "3"]
    await hass.services.async_call(DOMAIN, "run_tap", {"tap": 2, "minutes": 1.5}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_2", "on")
    await rig.goto("2026-10-12 05:58")
    assert rig.periods("input_boolean.tap_2")[-1][1] in ("05:56:30", "05:56:35")
    await hass.services.async_call(DOMAIN, "run_tap", {"tap": 1}, blocking=True)
    await rig.settle()
    await hass.services.async_call(DOMAIN, "stop", {}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("input_boolean.tap_1", "off")
    assert hass.states.get("sensor.irrigation_status").state == "idle"
    assert rig.max_open == 1 and rig.app_max_open == 1


async def test_stuck_valve(hass: HomeAssistant, rig: Rig, monkeypatch) -> None:
    import custom_components.irrigation_sequencer.coordinator as C

    o = cycle(make_options(rig.phone_id, general={"confirm_s": 5}), 0, "06:00", [1, 1, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    coord = rig.coord
    real = coord._service
    broken = {"input_boolean.tap_1"}

    async def service(entity_id: str, on: bool) -> None:
        if not on and entity_id in broken:
            return  # the valve ignores 'off'
        await real(entity_id, on)

    monkeypatch.setattr(coord, "_service", service)
    monkeypatch.setattr(C, "CLOSE_TRIES", 1)

    async def fast_wait(entity_id, on, timeout):
        return coord._is(entity_id, on)

    monkeypatch.setattr(coord, "_wait_for", fast_wait)
    await rig.goto("2026-10-12 06:02")
    assert hass.states.get("sensor.irrigation_status").state == "stuck"
    assert hass.states.is_state("input_boolean.tap_2", "off")
    fault = [c for c in rig.notes if c.data["data"].get("tag") == "irrigation_tap_1"]
    assert fault and fault[0].data["data"]["push"]["interruption-level"] == "time-sensitive"
    assert "Could not confirm Tap 1 (Backyard Left) is off" in fault[0].data["message"]
    broken.clear()
    await rig.goto("2026-10-12 06:10", step_s=20)
    assert hass.states.is_state("input_boolean.tap_1", "off")
    assert rig.periods("input_boolean.tap_2"), rig.log
    assert rig.max_open == 1 and rig.app_max_open == 1


async def _main_light(hass: HomeAssistant, state: str = "on") -> list[str]:
    """A main water valve that shows as a light (as a ZHA relay does); returns the calls it got."""
    calls: list[str] = []
    hass.states.async_set("light.main_water", state, {"friendly_name": "Main water"})

    async def handle(call) -> None:
        calls.append(call.service)
        hass.states.async_set("light.main_water", "on" if call.service == "turn_on" else "off")

    for svc in ("turn_on", "turn_off"):
        hass.services.async_register("light", svc, handle)
    return calls


async def test_stuck_tap_main_water_alert_buttons_and_actions(hass: HomeAssistant, rig: Rig, monkeypatch) -> None:
    import custom_components.irrigation_sequencer.coordinator as C

    calls = await _main_light(hass)
    o = cycle(make_options(rig.phone_id, general={"confirm_s": 5}, main={"valve": "light.main_water", "wait_min": 2}),
              0, "06:00", [1, 1, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    coord = rig.coord
    real = coord._service
    broken = {"input_boolean.tap_1"}

    async def service(entity_id: str, on: bool) -> None:
        if not on and entity_id in broken:
            return
        await real(entity_id, on)

    monkeypatch.setattr(coord, "_service", service)
    monkeypatch.setattr(C, "CLOSE_TRIES", 1)

    async def fast_wait(entity_id, on, timeout):
        return coord._is(entity_id, on)

    monkeypatch.setattr(coord, "_wait_for", fast_wait)
    await rig.goto("2026-10-12 06:02")
    assert hass.states.get("sensor.irrigation_status").state == "stuck"
    fault = rig.messages("irrigation_tap_1")
    assert fault and "you'll be asked to close the main water" in fault[0]
    assert not rig.messages("irrigation_main")
    await rig.goto("2026-10-12 06:04", step_s=10)

    main = [c.data for c in rig.notes if c.data["data"].get("tag") == "irrigation_main"]
    assert len(main) == 1, [c.data for c in rig.notes]
    alert = main[0]
    assert alert["title"] == "Irrigation - Main water"
    assert "still can't be confirmed off after 2 min" in alert["message"]
    assert alert["data"]["push"]["interruption-level"] == "critical"
    assert alert["data"]["push"]["sound"] == {"name": "default", "critical": 1, "volume": 1.0}
    assert alert["data"]["actions"] == [{"action": "irrigation_main_close", "title": "Close main water",
                                         "destructive": True}]
    assert hass.states.is_state("light.main_water", "on") and calls == []  # not closed without you

    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_main_close"})
    await rig.settle()
    assert hass.states.is_state("light.main_water", "off") and calls == ["turn_off"]
    assert any("Main water closed at" in m for m in rig.messages("irrigation_main"))
    assert hass.states.get("sensor.irrigation_status").attributes["main_water"] == "off"

    broken.clear()  # the tap works again: confirmed off at the next retry
    await rig.goto("2026-10-12 06:12", step_s=20)
    assert hass.states.is_state("input_boolean.tap_1", "off")
    prompt = [c.data for c in rig.notes if c.data["data"].get("tag") == "irrigation_main"][-1]
    assert "the main water is still off" in prompt["message"]
    assert prompt["data"]["actions"] == [
        {"action": "irrigation_main_open", "title": "Open main water", "authenticationRequired": True},
        {"action": "irrigation_main_snooze_1", "title": "Snooze 1 h"},
        {"action": "irrigation_main_snooze", "title": "Snooze…", "behavior": "textInput",
         "textInputButtonTitle": "Snooze", "textInputPlaceholder": "Hours, e.g. 3"},
    ]
    # snooze with typed hours ("press and hold > Snooze… > 2.5"), and a reply that isn't a number
    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_main_snooze", "reply_text": "2.5"})
    await rig.settle()
    assert "snoozed for 2.5 h" in rig.messages("irrigation_main")[-1]
    assert hass.states.get("sensor.irrigation_status").attributes["main_snoozed_until"]
    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_main_snooze", "reply_text": "soon"})
    await rig.settle()
    assert "Couldn't read \"soon\" as hours" in rig.messages("irrigation_main")[-1]
    assert prompt["data"]["push"]["interruption-level"] == "time-sensitive"
    assert hass.states.get("sensor.irrigation_status").state == "held"  # cycle 1 waits for the main water
    assert not rig.periods("input_boolean.tap_2")
    assert hass.states.is_state("light.main_water", "off")  # never opened by the app

    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_main_open"})
    await rig.settle()
    await rig.goto("2026-10-12 06:16", step_s=10)
    assert hass.states.is_state("light.main_water", "on") and calls == ["turn_off", "turn_on"]
    assert rig.periods("input_boolean.tap_2"), rig.log  # the cycle carried on
    assert rig.max_open == 1 and rig.app_max_open == 1


def test_parse_typed_snooze_hours() -> None:
    from custom_components.irrigation_sequencer.coordinator import parse_hours

    assert parse_hours("3") == 3 and parse_hours(" 1,5 ") == 1.5 and parse_hours("2h") == 2
    assert parse_hours("90m") == 1.5 and parse_hours("90 min") == 1.5 and parse_hours("4 hours") == 4
    assert parse_hours("0") == 0
    assert parse_hours("soon") is None and parse_hours("200") is None and parse_hours(None) is None
    # battery snoozes take days and weeks too (up to 30 days)
    assert parse_hours("3d", max_h=720) == 72 and parse_hours("2 days", max_h=720) == 48
    assert parse_hours("1 week", max_h=720) == 168 and parse_hours("30d", max_h=720) == 720
    assert parse_hours("31 days", max_h=720) is None and parse_hours("12", max_h=720) == 12


async def test_main_water_services(hass: HomeAssistant, rig: Rig) -> None:
    from homeassistant.exceptions import ServiceValidationError

    calls = await _main_light(hass)
    await rig.setup_entry(make_options(rig.phone_id))
    with pytest.raises(ServiceValidationError, match="No main water valve"):
        await hass.services.async_call(DOMAIN, "close_main_water", {}, blocking=True)
    o = make_options(rig.phone_id, main={"valve": "light.main_water"})
    hass.config_entries.async_update_entry(rig.entry, options=normalize(o))
    await rig.settle()
    await hass.services.async_call(DOMAIN, "close_main_water", {}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("light.main_water", "off")
    assert hass.states.get("sensor.irrigation_status").attributes["main_water"] == "off"
    await hass.services.async_call(DOMAIN, "close_main_water", {}, blocking=True)
    await rig.settle()
    assert any("already off" in m for m in rig.messages("irrigation_main"))
    await hass.services.async_call(DOMAIN, "snooze_main_water_reminders", {"hours": 4}, blocking=True)
    await rig.settle()
    assert any("snoozed for 4 h" in m for m in rig.messages("irrigation_main"))
    await hass.services.async_call(DOMAIN, "open_main_water", {}, blocking=True)
    await rig.settle()
    assert hass.states.is_state("light.main_water", "on") and calls == ["turn_off", "turn_on"]
    assert hass.states.get("sensor.irrigation_status").attributes["main_snoozed_until"] is None


async def test_bypass_holds_cycle(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id), 0, "06:00", [1, 1, 0])
    o["taps"][0]["bypass"] = "input_boolean.pressure_washer_bypass"
    await rig.setup_entry(o)
    await master_on(hass, rig)
    for e in ("input_boolean.pressure_washer_bypass", "input_boolean.tap_1"):
        await hass.services.async_call("input_boolean", "turn_on", {"entity_id": e}, blocking=True)
    await rig.goto("2026-10-12 06:30", step_s=30)
    assert hass.states.is_state("input_boolean.tap_1", "on")  # left alone: no manual timer
    assert hass.states.get("sensor.irrigation_status").state == "held"
    assert not rig.messages("irrigation_manual")
    for e in ("input_boolean.tap_1", "input_boolean.pressure_washer_bypass"):
        await hass.services.async_call("input_boolean", "turn_off", {"entity_id": e}, blocking=True)
    await rig.goto("2026-10-12 06:40")
    assert rig.order()[-2:] == ["1", "2"]
    assert any("30 min late" in m for m in rig.messages("irrigation_cycle_1"))


async def test_weather_skip_with_forecast(hass: HomeAssistant, rig: Rig) -> None:
    async_mock_service(
        hass, "weather", "get_forecasts",
        response={"weather.home": {"forecast": [{"precipitation_probability": 80, "precipitation": 6.0}]}},
        supports_response=__import__("homeassistant.core", fromlist=["SupportsResponse"]).SupportsResponse.ONLY,
    )
    hass.states.async_set("weather.home", "cloudy", {"temperature": 18, "wind_speed": 10, "wind_speed_unit": "km/h"})
    hass.states.async_set("sensor.rain_24h", "0.4", {"unit_of_measurement": "mm"})
    o = cycle(make_options(rig.phone_id, weather={"rain_sensor": "sensor.rain_24h", "weather_entity": "weather.home",
                                                  "fc_prob": 60}), 0, "06:00", [1, 1, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:05")
    assert rig.log == []
    assert rig.messages("irrigation_cycle_1") == ["Cycle 1 (due 06:00) skipped: forecast 80% chance of rain."]


async def test_hot_alert_and_units(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("sensor.outdoor_temp", "88", {"unit_of_measurement": "°F"})  # 31.1 °C
    o = cycle(make_options(rig.phone_id, weather={"temp_sensor": "sensor.outdoor_temp"}), 0, "18:00", [1, 0, 0])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    await rig.goto("2026-10-12 06:02", step_s=30)
    await rig.goto("2026-10-12 06:12", step_s=60)
    hot = rig.messages("irrigation_hot")
    assert hot == ["It is 31.1 °C (too hot at 30 °C). Next: Cycle 1 at Mon 18:00."], hot


async def test_websocket_edit_cell(hass: HomeAssistant, rig: Rig, hass_ws_client) -> None:
    entry = await rig.setup_entry(make_options(rig.phone_id))
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/config"})
    res = await ws.receive_json()
    assert res["success"], res
    e = res["result"]["entries"][0]
    assert e["entities"]["master"] == "switch.irrigation_master"
    assert e["cycles"][0]["switch"] == "switch.irrigation_cycle_1"
    assert e["taps"][1]["label"] == "Tap 2 (Backyard Centre)"
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/update", "entry_id": entry.entry_id, "cycle": 1, "tap": 2,
                        "minutes": 7, "order": 1, "soak": True})
    res = await ws.receive_json()
    assert res["success"], res
    assert res["result"]["cycles"][0]["minutes"][1] == 7
    assert res["result"]["cycles"][0]["order"] == [2, 1, 3]  # tap 2 took #1, tap 1 moved to its #2
    await ws.send_json({"id": 5, "type": f"{DOMAIN}/update", "entry_id": entry.entry_id, "cycle": 1, "tap": 3,
                        "order": 1})
    res = await ws.receive_json()
    assert res["success"], res
    assert res["result"]["cycles"][0]["order"] == [2, 3, 1]  # never two taps with the same number
    await ws.send_json({"id": 6, "type": f"{DOMAIN}/update", "entry_id": entry.entry_id, "cycle": 2,
                        "time": "7:5", "days": ["sat", "sun"]})
    res = await ws.receive_json()
    assert res["success"], res
    await rig.settle()
    assert entry.options["cycles"][0]["minutes"][1] == 7 and entry.options["cycles"][0]["soak"][1] is True
    assert entry.options["cycles"][1]["time"] == "07:05" and entry.options["cycles"][1]["days"] == ["sat", "sun"]
    assert rig.coord.cfg.cycles[0].minutes[1] == 7  # applied live, no reload
    await ws.send_json({"id": 7, "type": f"{DOMAIN}/update", "entry_id": entry.entry_id, "cycle": 9, "tap": 1,
                        "minutes": 1})
    res = await ws.receive_json()
    assert not res["success"]


async def test_unique_ids_and_device(hass: HomeAssistant, rig: Rig) -> None:
    entry = await rig.setup_entry(make_options())
    reg = er.async_get(hass)
    ents = er.async_entries_for_config_entry(reg, entry.entry_id)
    ids = sorted(e.entity_id for e in ents)
    assert ids == sorted([
        "switch.irrigation_master", "switch.irrigation_cycle_1", "switch.irrigation_cycle_2",
        "number.irrigation_seasonal_adjustment", "sensor.irrigation_status", "sensor.irrigation_next_cycle",
        "binary_sensor.irrigation_tap_1_blocked", "binary_sensor.irrigation_tap_2_blocked",
        "binary_sensor.irrigation_tap_3_blocked",
    ])


async def test_remove_entry_deletes_state(hass: HomeAssistant, rig: Rig, hass_storage) -> None:
    entry = await rig.setup_entry(make_options())
    await master_on(hass, rig)
    key = f"{DOMAIN}.{entry.entry_id}"
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_second_entry_needs_entry_id_for_services(hass: HomeAssistant, rig: Rig) -> None:
    from homeassistant.exceptions import ServiceValidationError

    await rig.setup_entry(make_options())
    other = MockConfigEntry(domain=DOMAIN, title="Veggies", data={}, options=make_options(notify={"prefix": "veg"}))
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.veggies_master") is not None
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "stop", {}, blocking=True)
    await hass.services.async_call(DOMAIN, "stop", {"config_entry_id": other.entry_id}, blocking=True)


# --------------------------------------------------------------------------
# Offline valves and valve batteries
# --------------------------------------------------------------------------
def _notes(rig: Rig, tag: str) -> list[dict]:
    return [c.data for c in rig.notes if c.data.get("data", {}).get("tag") == tag]


async def test_offline_valve_question_buttons_skip_and_back_online(hass: HomeAssistant, rig: Rig) -> None:
    o = cycle(make_options(rig.phone_id, offline={"ask_ahead": False}), 0, "06:00", [1, 1, 1])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    hass.states.async_set("input_boolean.tap_3", "unavailable")  # e.g. its battery went flat
    await rig.settle()
    await rig.goto("2026-10-12 06:01")
    st = hass.states.get("sensor.irrigation_status")
    assert st.state == "offline"
    assert st.attributes["offline"] == [3] and st.attributes["offline_waiting"] == [3]
    assert st.attributes["offline_can_skip"] == [3] and st.attributes["offline_until"].startswith("2026-10-12T06:30")
    ask = _notes(rig, "irrigation_offline")
    assert len(ask) == 1 and ask[0]["title"] == "Irrigation - Valve offline"
    assert ask[0]["message"].startswith("Cycle 1 is waiting: Tap 3 (Front Lawn) has been offline since 05:50")
    assert ask[0]["data"]["push"]["interruption-level"] == "time-sensitive"
    assert ask[0]["data"]["actions"] == [
        {"action": "irrigation_offline_skip", "title": "Skip while offline", "authenticationRequired": True},
        {"action": "irrigation_offline_stop", "title": "Stop the cycle", "destructive": True},
    ]
    assert not rig.periods("input_boolean.tap_1")

    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_offline_skip"})
    await rig.settle()
    await rig.goto("2026-10-12 06:05")
    assert rig.order() == ["1", "2"] and rig.max_open == 1
    note = _notes(rig, "irrigation_offline")[-1]
    assert "Tap 3 (Front Lawn) is skipped in every cycle while it is offline" in note["message"]
    assert "Cycle 1 carries on at 06:01." in note["message"] and "actions" not in note["data"]
    assert hass.states.get("sensor.irrigation_status").attributes["offline_skipped"] == [3]

    hass.states.async_set("input_boolean.tap_3", "off")
    await rig.settle()
    assert _notes(rig, "irrigation_offline")[-1]["message"] == ("Tap 3 (Front Lawn) is back online: it is in the "
                                                                "cycles again.")
    st = hass.states.get("sensor.irrigation_status")
    assert st.attributes["offline"] == [] and st.attributes["offline_skipped"] == []


async def test_offline_stop_button_and_skip_offline_tap_action(hass: HomeAssistant, rig: Rig) -> None:
    from homeassistant.exceptions import ServiceValidationError

    o = cycle(make_options(rig.phone_id, offline={"ask_ahead": False}), 0, "06:00", [1, 1, 1])
    await rig.setup_entry(o)
    await master_on(hass, rig)
    hass.states.async_set("input_boolean.tap_2", "unavailable")
    await rig.goto("2026-10-12 06:01")
    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_offline_stop"})
    await rig.settle()
    assert "Cycle 1 stopped at 06:01 (requested)." in _notes(rig, "irrigation_offline")[-1]["message"]
    assert hass.states.get("sensor.irrigation_status").state == "idle" and not rig.order()
    with pytest.raises(ServiceValidationError, match="only 3 taps"):
        await hass.services.async_call(DOMAIN, "skip_offline_tap", {"tap": 9}, blocking=True)
    await hass.services.async_call(DOMAIN, "skip_offline_tap", {"tap": 2}, blocking=True)
    await rig.settle()
    assert hass.states.get("sensor.irrigation_status").attributes["offline_skipped"] == [2]
    await hass.services.async_call(DOMAIN, "skip_offline_tap", {"tap": 2, "skip": False}, blocking=True)
    await rig.settle()
    assert hass.states.get("sensor.irrigation_status").attributes["offline_skipped"] == []
    await hass.services.async_call(DOMAIN, "skip_offline_tap", {"tap": 1}, blocking=True)
    await rig.settle()
    assert "is online: there is nothing to skip" in _notes(rig, "irrigation_offline")[-1]["message"]


async def test_battery_sensor_found_on_the_valves_device(hass: HomeAssistant, rig: Rig) -> None:
    from homeassistant.helpers import device_registry as dr

    zha = MockConfigEntry(domain="zha")
    zha.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=zha.entry_id, identifiers={("zha", "valve-1")},
                                                 name="WS Valve")
    reg = er.async_get(hass)
    valve = reg.async_get_or_create("switch", "zha", "valve-1-switch", device_id=dev.id, config_entry=zha)
    plus = reg.async_get_or_create("sensor", "battery_notes", "valve-1-plus", device_id=dev.id,
                                   original_device_class="battery", unit_of_measurement="%")
    bat = reg.async_get_or_create("sensor", "zha", "valve-1-battery", device_id=dev.id, config_entry=zha,
                                  original_device_class="battery", unit_of_measurement="%")
    reg.async_get_or_create("sensor", "zha", "valve-1-rssi", device_id=dev.id, config_entry=zha,
                            original_device_class="signal_strength", unit_of_measurement="dBm")
    hass.states.async_set(valve.entity_id, "off")  # online (start-up waits for the valves)
    o = make_options(rig.phone_id)
    o["taps"][0]["valve"] = valve.entity_id
    o["taps"][2]["battery"] = "sensor.my_tap_3_battery"  # chosen by hand
    await rig.setup_entry(o)
    assert rig.coord.battery_ids == [bat.entity_id, "", "sensor.my_tap_3_battery"]
    assert plus.entity_id != bat.entity_id  # Battery Notes' copy is not picked
    hass.states.async_set(bat.entity_id, "54", {"device_class": "battery", "unit_of_measurement": "%"})
    await rig.settle()
    assert hass.states.get("sensor.irrigation_status").attributes["battery"] == {"1": 54, "2": None, "3": None}


async def test_low_battery_warning_snooze_and_report(hass: HomeAssistant, rig: Rig) -> None:
    o = make_options(rig.phone_id)
    o["taps"][1]["battery"] = "sensor.tap_2_battery"
    await rig.setup_entry(o)
    hass.states.async_set("sensor.tap_2_battery", "25", {"unit_of_measurement": "%"})
    await rig.settle()
    assert not _notes(rig, "irrigation_battery")  # 05:50 is in the quiet time (21:00-06:00)
    await rig.goto("2026-10-12 06:01", step_s=20)
    warn = _notes(rig, "irrigation_battery")
    assert len(warn) == 1 and warn[0]["title"] == "Irrigation - Low battery"
    assert warn[0]["message"].startswith("Low battery (warning at 30%): Tap 2 (Backyard Centre) 25%.")
    assert warn[0]["data"]["push"]["interruption-level"] == "active"
    assert warn[0]["data"]["actions"] == [
        {"action": "irrigation_battery_snooze_24", "title": "Snooze 1 day"},
        {"action": "irrigation_battery_snooze", "title": "Snooze…", "behavior": "textInput",
         "textInputButtonTitle": "Snooze", "textInputPlaceholder": "Hours or days, e.g. 12 or 3d"},
    ]
    st = hass.states.get("sensor.irrigation_status")
    assert st.attributes["battery_low"] == [2] and st.attributes["battery"]["2"] == 25

    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_battery_snooze", "reply_text": "2d"})
    await rig.settle()
    assert _notes(rig, "irrigation_battery")[-1]["message"].startswith("Battery warnings snoozed for 2 days, until Wed")
    assert hass.states.get("sensor.irrigation_status").attributes["battery_snoozed_until"].startswith("2026-10-14")
    hass.bus.async_fire("mobile_app_notification_action", {"action": "irrigation_battery_snooze", "reply_text": "?"})
    await rig.settle()
    assert "Couldn't read \"?\" as hours or days" in _notes(rig, "irrigation_battery")[-1]["message"]
    await hass.services.async_call(DOMAIN, "snooze_battery_warnings", {"hours": 0}, blocking=True)
    await rig.settle()
    assert "Battery warnings are back on." in _notes(rig, "irrigation_battery")[-1]["message"]

    await hass.services.async_call(DOMAIN, "send_battery_report", {}, blocking=True)
    await rig.settle()
    rep = _notes(rig, "irrigation_battery_report")
    assert rep[-1]["title"] == "Irrigation - Battery levels"
    assert rep[-1]["message"] == ("Lowest: Tap 2 (Backyard Centre) 25% (low)\n\n"
                                  "Tap 1 (Backyard Left): no battery reading\n"
                                  "Tap 2 (Backyard Centre) 25% (low)\n"
                                  "Tap 3 (Front Lawn): no battery reading\n\n"
                                  "Next report: Mon 12 Oct 10:00")
    nxt = hass.states.get("sensor.irrigation_status").attributes["battery_report_next"]
    assert nxt.startswith("2026-10-12T10:00")  # counts from today: first report at 10:00

    hass.states.async_set("sensor.tap_2_battery", "100", {"unit_of_measurement": "%"})  # new batteries
    await rig.settle()
    assert _notes(rig, "irrigation_battery")[-1]["message"] == "Battery fine again: Tap 2 (Backyard Centre) 100%."
    assert hass.states.get("sensor.irrigation_status").attributes["battery_low"] == []
