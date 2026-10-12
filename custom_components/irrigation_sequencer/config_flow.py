"""Config flow (install) and options flow (all settings, one screen per group)."""

from __future__ import annotations

import copy
from typing import Any

try:  # Home Assistant 2026.10+ validates with Probatio (same API)
    import probatio as vol  # type: ignore[import-not-found]
except ImportError:  # 2026.9 and older
    import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import DOMAIN
from .model import DAYS, MAX_CYCLES, MAX_MINUTES, MAX_TAPS, default_options, normalize, resolve_order

DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
VALVE_DOMAINS = ["switch", "valve", "input_boolean"]


def _num(lo: float, hi: float, step: float = 1, unit: str | None = None, slider: bool = False) -> NumberSelector:
    conf = NumberSelectorConfig(
        min=lo, max=hi, step=step, mode=NumberSelectorMode.SLIDER if slider else NumberSelectorMode.BOX
    )
    if unit:
        conf["unit_of_measurement"] = unit
    return NumberSelector(conf)


def _entity(domains: list[str] | str, multiple: bool = False) -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domains, multiple=multiple))


def _tap_name_from(hass, entity_id: str) -> str:
    s = hass.states.get(entity_id) if entity_id else None
    name = (s.attributes.get("friendly_name") if s else "") or ""
    for suffix in (" Switch", " switch", " Valve", " valve"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def _clean(user_input: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Optional entity / text fields that were cleared come back missing: store ''."""
    out = dict(user_input)
    for k in keys:
        out.setdefault(k, "")
    return out


class IrrigationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Install: a name, how many taps and cycles, then the valves."""

    VERSION = 1

    def __init__(self) -> None:
        self._base: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IrrigationOptionsFlow:
        return IrrigationOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._base = user_input
            return await self.async_step_valves()
        schema = vol.Schema(
            {
                vol.Required("name", default="Irrigation"): TextSelector(),
                vol.Required("tap_count", default=6): _num(1, MAX_TAPS),
                vol.Required("cycle_count", default=4): _num(1, MAX_CYCLES),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_valves(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        T = int(self._base["tap_count"])
        C = int(self._base["cycle_count"])
        if user_input is not None:
            opts = default_options(T, C)
            for i in range(T):
                valve = user_input.get(f"tap_{i + 1}", "") or ""
                opts["taps"][i]["valve"] = valve
                opts["taps"][i]["name"] = _tap_name_from(self.hass, valve)
            return self.async_create_entry(title=self._base["name"].strip() or "Irrigation", data={}, options=opts)
        schema = vol.Schema({vol.Optional(f"tap_{i + 1}"): _entity(VALVE_DOMAINS) for i in range(T)})
        return self.async_show_form(step_id="valves", data_schema=schema)


class IrrigationOptionsFlow(OptionsFlow):
    """Every setting, grouped. Each screen saves when you press Submit."""

    def __init__(self) -> None:
        self._tap = 0
        self._cycle = 0

    @property
    def _opts(self) -> dict[str, Any]:
        return copy.deepcopy(normalize(self.config_entry.options))

    def _save(self, opts: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data=normalize(opts))

    def _tap_label(self, o: dict[str, Any], i: int) -> str:
        name = o["taps"][i]["name"]
        return f"Tap {i + 1} ({name})" if name else f"Tap {i + 1}"

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["cycle_pick", "tap_pick", "general", "notifications", "soak", "people", "weather", "counts"],
        )

    # ---------------- cycles ----------------
    async def async_step_cycle_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            self._cycle = int(user_input["cycle"]) - 1
            return await self.async_step_cycle()
        options = []
        for c, cyc in enumerate(o["cycles"]):
            total = sum(m for i, m in enumerate(cyc["minutes"]) if o["taps"][i]["valve"])
            days = "every day" if len(cyc["days"]) == 7 else (", ".join(d.title() for d in cyc["days"]) or "no days")
            options.append(SelectOptionDict(value=str(c + 1),
                                            label=f"Cycle {c + 1} · {cyc['time']} · {days} · {total} min"))
        schema = vol.Schema({vol.Required("cycle", default="1"): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))})
        return self.async_show_form(step_id="cycle_pick", data_schema=schema)

    async def async_step_cycle(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        ci = self._cycle
        T = o["tap_count"]
        cyc = o["cycles"][ci]
        errors: dict[str, str] = {}
        clash = ""
        shown = cyc
        if user_input is not None:
            secs = [user_input.get(f"tap_{i + 1}", {}) for i in range(T)]
            wanted = [int(sec.get("order", cyc["order"][i])) for i, sec in enumerate(secs)]
            order = resolve_order(cyc["order"], wanted)
            if order is not None:
                cyc["time"] = user_input["time"]
                cyc["days"] = user_input.get("days", [])
                for i, sec in enumerate(secs):
                    cyc["minutes"][i] = int(sec.get("minutes", 0))
                    cyc["soak"][i] = bool(sec.get("soak", False))
                cyc["order"] = order
                return self._save(o)
            # two or more numbers changed and some are now shared: ask again, keeping what was typed
            errors["base"] = "order_clash"
            clash = "; ".join(
                " and ".join(f"Tap {i + 1}" for i in range(T) if wanted[i] == n) + f" are both {n}"
                for n in sorted(set(wanted)) if wanted.count(n) > 1
            )
            shown = {
                "time": str(user_input.get("time", cyc["time"]))[:5],
                "days": user_input.get("days", []),
                "minutes": [int(sec.get("minutes", cyc["minutes"][i])) for i, sec in enumerate(secs)],
                "order": wanted,
                "soak": [bool(sec.get("soak", cyc["soak"][i])) for i, sec in enumerate(secs)],
            }
        fields: dict[Any, Any] = {
            vol.Required("time", default=f"{shown['time']}:00"): TimeSelector(),
            vol.Optional("days", default=shown["days"]): SelectSelector(
                SelectSelectorConfig(
                    options=[SelectOptionDict(value=d, label=lab) for d, lab in zip(DAYS, DAY_LABELS, strict=True)],
                    multiple=True, mode=SelectSelectorMode.LIST,
                )
            ),
        }
        for i in range(T):
            fields[vol.Required(f"tap_{i + 1}")] = section(
                vol.Schema(
                    {
                        vol.Required("minutes", default=shown["minutes"][i]): _num(0, MAX_MINUTES, 1, "min"),
                        vol.Required("order", default=shown["order"][i]): _num(1, T),
                        vol.Required("soak", default=shown["soak"][i]): BooleanSelector(),
                    }
                ),
                {"collapsed": False},
            )
        # section titles: "Tap 1 · Backyard Left" (the frontend fills them from these placeholders)
        names = {
            f"tap_{i + 1}": f"Tap {i + 1}" + (f" · {o['taps'][i]['name']}" if o["taps"][i]["name"] else "")
            + ("" if o["taps"][i]["valve"] else " (no valve)")
            for i in range(T)
        }
        return self.async_show_form(
            step_id="cycle", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"cycle": str(ci + 1), "clash": clash, **names},
        )

    # ---------------- taps ----------------
    async def async_step_tap_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            self._tap = int(user_input["tap"]) - 1
            return await self.async_step_tap()
        options = [
            SelectOptionDict(value=str(i + 1), label=self._tap_label(o, i) + (f" · {t['valve']}" if t["valve"] else " · no valve"))
            for i, t in enumerate(o["taps"])
        ]
        schema = vol.Schema({vol.Required("tap", default="1"): SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST))})
        return self.async_show_form(step_id="tap_pick", data_schema=schema)

    async def async_step_tap(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        i = self._tap
        if user_input is not None:
            u = _clean(user_input, ["valve", "name", "soil", "bypass"])
            u.setdefault("people", [])
            o["taps"][i].update(u)
            return self._save(o)
        schema = vol.Schema(
            {
                vol.Optional("valve"): _entity(VALVE_DOMAINS),
                vol.Optional("name"): TextSelector(),
                vol.Optional("people"): _entity(["binary_sensor", "sensor"], multiple=True),
                vol.Optional("soil"): _entity("sensor"),
                vol.Required("soil_pct"): _num(0, 100, 1, "%"),
                vol.Optional("bypass"): _entity(["input_boolean", "switch", "binary_sensor"]),
            }
        )
        t = o["taps"][i]
        suggested = {k: v for k, v in t.items() if v not in ("", [], None)}
        suggested["soil_pct"] = t["soil_pct"]
        return self.async_show_form(
            step_id="tap", data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={"tap": f"Tap {i + 1}" + (f" · {t['name']}" if t["name"] else "")},
        )

    # ---------------- groups ----------------
    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            o["general"].update(_clean(user_input, ["seasonal_entity"]))
            return self._save(o)
        schema = vol.Schema(
            {
                vol.Required("gap_s"): _num(0, 60, 1, "s"),
                vol.Required("confirm_s"): _num(5, 120, 1, "s"),
                vol.Required("max_run_min"): _num(1, 240, 1, "min"),
                vol.Required("late_min"): _num(1, 720, 1, "min"),
                vol.Optional("seasonal_entity"): _entity(["input_number", "number", "sensor"]),
            }
        )
        return self.async_show_form(step_id="general", data_schema=self.add_suggested_values_to_schema(
            schema, {k: v for k, v in o["general"].items() if v != ""}))

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            u = dict(user_input)
            u.setdefault("devices", [])
            o["notify"].update(u)
            return self._save(o)
        schema = vol.Schema(
            {
                vol.Optional("devices"): DeviceSelector(DeviceSelectorConfig(integration="mobile_app", multiple=True)),
                vol.Required("prefix"): TextSelector(),
                vol.Required("start_finish"): BooleanSelector(),
                vol.Required("skips"): BooleanSelector(),
                vol.Required("manual"): BooleanSelector(),
                vol.Required("people"): BooleanSelector(),
                vol.Required("reminders"): BooleanSelector(),
                vol.Required("rain"): BooleanSelector(),
                vol.Required("hot_c"): _num(0, 50, 1, "°C"),
            }
        )
        return self.async_show_form(step_id="notifications",
                                    data_schema=self.add_suggested_values_to_schema(schema, o["notify"]))

    async def async_step_soak(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            o["soak"].update(user_input)
            return self._save(o)
        schema = vol.Schema({vol.Required("burst_min"): _num(1, 60, 1, "min"), vol.Required("soak_min"): _num(0, 120, 1, "min")})
        return self.async_show_form(step_id="soak", data_schema=self.add_suggested_values_to_schema(schema, o["soak"]))

    async def async_step_people(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            o["people"].update(user_input)
            return self._save(o)
        schema = vol.Schema(
            {
                vol.Required("on"): BooleanSelector(),
                vol.Required("clear_min"): _num(0, 30, 0.5, "min"),
                vol.Required("max_wait_min"): _num(0, 240, 5, "min"),
                vol.Required("manual"): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="people", data_schema=self.add_suggested_values_to_schema(schema, o["people"]))

    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            o["weather"].update(_clean(user_input, ["rain_sensor", "weather_entity", "wind_sensor", "temp_sensor"]))
            return self._save(o)
        schema = vol.Schema(
            {
                vol.Required("on"): BooleanSelector(),
                vol.Optional("rain_sensor"): _entity(["sensor", "binary_sensor"]),
                vol.Required("rain_mm"): _num(0.5, 100, 0.5, "mm"),
                vol.Optional("weather_entity"): _entity("weather"),
                vol.Required("fc_prob"): _num(0, 100, 5, "%"),
                vol.Required("fc_mm"): _num(0, 50, 0.5, "mm"),
                vol.Optional("wind_sensor"): _entity("sensor"),
                vol.Required("wind_kmh"): _num(0, 100, 1, "km/h"),
                vol.Optional("temp_sensor"): _entity("sensor"),
                vol.Required("cold_on"): BooleanSelector(),
                vol.Required("cold_c"): _num(-10, 15, 1, "°C"),
                vol.Required("boost_pct"): _num(100, 200, 5, "%"),
            }
        )
        return self.async_show_form(step_id="weather", data_schema=self.add_suggested_values_to_schema(
            schema, {k: v for k, v in o["weather"].items() if v != ""}))

    async def async_step_counts(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        o = self._opts
        if user_input is not None:
            o["tap_count"] = int(user_input["tap_count"])
            o["cycle_count"] = int(user_input["cycle_count"])
            return self._save(o)
        schema = vol.Schema({vol.Required("tap_count"): _num(1, MAX_TAPS), vol.Required("cycle_count"): _num(1, MAX_CYCLES)})
        return self.async_show_form(step_id="counts", data_schema=self.add_suggested_values_to_schema(
            schema, {"tap_count": o["tap_count"], "cycle_count": o["cycle_count"]}))
