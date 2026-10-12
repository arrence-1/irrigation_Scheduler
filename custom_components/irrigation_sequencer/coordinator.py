"""Coordinator: Home Assistant glue around the engine.

* Every event (valve / switch / sensor change, the next-step timer, the
  1-minute watchdog, start-up, a service call) only *requests* a reconcile.
* One worker task runs the requests one at a time (so two events never act
  on the same state), builds a Snapshot, runs engine.reconcile, saves the
  state first, then closes valves (confirming each, 3 tries), opens a valve
  (confirming it), sends notifications and sets the next wake-up.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
import itertools
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from . import engine as E
from .const import (
    CLOSE_TRIES,
    DOMAIN,
    FORECAST_CACHE_S,
    NAME,
    OFF_STATES,
    ON_STATES,
    STARTUP_VALVE_WAIT_S,
    STORAGE_VERSION,
    VERSION,
    WATCHDOG_S,
)
from .model import Cfg, normalize, parse
from .notify import async_send

_LOGGER = logging.getLogger(__name__)

# Settings version for the card (status attribute config_rev). One rising number for every load and
# every live edit: a reload (new number of taps / cycles) starts a new coordinator, and an open card or
# settings screen must notice that too, so a new coordinator never starts again from the same value.
_REVS = itertools.count(int(time.time()))


def _float(state_str: Any) -> float | None:
    try:
        v = float(state_str)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


class IrrigationCoordinator:
    """Owns the engine state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.options: dict[str, Any] = normalize(entry.options)
        self.cfg: Cfg = parse(self.options)
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.state = E.State()
        self.snapshot: E.Snapshot | None = None
        self.config_rev = next(_REVS)
        self.messages: deque[dict[str, Any]] = deque(maxlen=20)
        self.last_outbox: list[dict[str, Any]] = []
        self.ready = False
        self._reasons: set[str] = set()
        self._requests: list[dict[str, Any]] = []
        self._wake = asyncio.Event()
        self._entity_unsubs: list[CALLBACK_TYPE] = []
        self._timer_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._started_unsub: CALLBACK_TYPE | None = None  # one-time "HA started" listener, until it fires
        self._listeners: list[Callable[[], None]] = []
        self._forecast: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._saved: dict[str, Any] | None = None
        self._startup_msgs: list[tuple[str, int, str]] = []
        self._running = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self.next_wake: float | None = None

    # ------------------------------------------------------------------
    # Life cycle
    # ------------------------------------------------------------------
    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            manufacturer=NAME,
            model="Scheduler",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_load(self) -> None:
        data = await self.store.async_load()
        if data is None:
            st = E.State()
            st.master = False  # a new install starts with the master switch off
        else:
            st, ok = E.State.from_dict(data)
            if not ok:
                self._startup_msgs.append(("config", 0, "Saved state was not readable and has been reset."))
        E.fit(st, self.cfg)
        self.state = st
        self._saved = st.to_dict()

    async def async_start(self) -> None:
        """Subscribe to events and start the worker."""
        startup = self.hass.state is not CoreState.running
        self._subscribe_entities()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._on_watchdog, timedelta(seconds=WATCHDOG_S))
        )
        self._unsubs.append(self.hass.bus.async_listen("mobile_app_notification_action", self._on_action))
        if startup:
            self._started_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)
        else:
            self._running.set()
        self.entry.async_create_background_task(
            self.hass, self._worker("startup" if startup else "reload"), f"{DOMAIN}_worker_{self.entry.entry_id}"
        )

    async def async_stop(self) -> None:
        """Unload: stop listening. Valves are left as they are (recovery handles them)."""
        for unsub in self._entity_unsubs + self._unsubs:
            unsub()
        self._entity_unsubs, self._unsubs = [], []
        if self._started_unsub:  # only while it has not fired: a fired one-time listener is already gone
            self._started_unsub()
            self._started_unsub = None
        if self._timer_unsub:
            self._timer_unsub()
            self._timer_unsub = None
        await self._save(force=True)

    @callback
    def _on_started(self, _event: Event) -> None:
        self._started_unsub = None
        self._running.set()

    def _subscribe_entities(self) -> None:
        for unsub in self._entity_unsubs:
            unsub()
        self._entity_unsubs = []
        cfg = self.cfg
        ids: set[str] = set()
        for t in cfg.taps:
            ids.update(x for x in [t.valve, t.bypass, *t.people] if x)
        ids.update(x for x in [cfg.rain_sensor, cfg.temp_sensor] if x)
        if not cfg.temp_sensor and cfg.weather_entity:
            ids.add(cfg.weather_entity)
        if ids:
            self._entity_unsubs.append(
                async_track_state_change_event(self.hass, sorted(ids), self._on_entity_change)
            )

    @callback
    def _on_entity_change(self, event: Event) -> None:
        self.request("entity")

    @callback
    def _on_watchdog(self, _now: datetime) -> None:
        self.request("watchdog")

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._timer_unsub = None
        self.request("timer")

    @callback
    def _on_action(self, event: Event) -> None:
        if event.data.get("action") == f"{self.cfg.n_prefix}_master_on":
            self.hass.async_create_task(self.async_set_master(True))

    # ------------------------------------------------------------------
    # Requests from entities, services, the card
    # ------------------------------------------------------------------
    @callback
    def request(self, reason: str, req: dict[str, Any] | None = None) -> None:
        self._reasons.add(reason)
        if req:
            self._requests.append(req)
        self._idle.clear()
        self._wake.set()

    async def async_set_master(self, on: bool) -> None:
        if self.state.master == on:
            return
        self.state.master = on
        await self._save()
        self._notify_listeners()
        self.request("master_on" if on else "master_off")

    async def async_set_cycle(self, index: int, on: bool) -> None:
        if not 0 <= index < len(self.state.cycle_on) or self.state.cycle_on[index] == on:
            return
        self.state.cycle_on[index] = on
        await self._save()
        self._notify_listeners()
        self.request(f"cycle_on_{index}" if on else f"cycle_off_{index}")

    async def async_set_seasonal(self, value: float) -> None:
        self.state.seasonal = max(0.0, min(200.0, float(value)))
        await self._save()
        self._notify_listeners()

    async def async_apply_options(self, options: dict[str, Any]) -> None:
        """Options changed without a count change: apply them live (no reload)."""
        new = normalize(options)
        old_cfg = self.cfg
        if self.state.mode in (E.WATERING, E.MANUAL) and 0 <= self.state.tap < len(old_cfg.taps):
            old_valve = old_cfg.taps[self.state.tap].valve
            new_valve = new["taps"][self.state.tap]["valve"] if self.state.tap < len(new["taps"]) else ""
            if old_valve and old_valve != new_valve:
                _LOGGER.warning("Valve of the running tap changed; closing %s", old_valve)
                await self._service(old_valve, False)
        self.options = new
        self.cfg = parse(new)
        self.config_rev = next(_REVS)
        E.fit(self.state, self.cfg)
        self._subscribe_entities()
        self._notify_listeners()
        self.request("options")

    async def async_wait_idle(self, timeout: float = 30) -> None:
        """Wait until queued requests have been processed (services, the card, tests).

        Bounded: during start-up the worker may wait up to 2 min for the valves.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        except TimeoutError:
            _LOGGER.debug("Still busy after %s s; the request stays queued", timeout)

    # ------------------------------------------------------------------
    # Entity listeners
    # ------------------------------------------------------------------
    @callback
    def add_listener(self, fn: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(fn)

        @callback
        def remove() -> None:
            if fn in self._listeners:
                self._listeners.remove(fn)

        return remove

    @callback
    def _notify_listeners(self) -> None:
        for fn in list(self._listeners):
            fn()

    # ------------------------------------------------------------------
    # The worker
    # ------------------------------------------------------------------
    async def _worker(self, first_reason: str) -> None:
        await self._running.wait()
        await self._wait_valves_online()
        self.ready = True
        self.request(first_reason)
        while True:
            await self._wake.wait()
            self._wake.clear()
            reasons, self._reasons = self._reasons, set()
            requests, self._requests = self._requests, []
            try:
                await self._reconcile(reasons, requests)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("Irrigation Scheduler: reconcile failed (reasons %s)", reasons)
                self._schedule(time.time() + 30)
            if not self._wake.is_set():
                self._idle.set()

    async def _wait_valves_online(self) -> None:
        """After a restart, give the valves up to 2 min to come online (event driven, no polling)."""
        valves = [t.valve for t in self.cfg.taps if t.valve]

        def offline() -> list[str]:
            return [
                v for v in valves
                if (s := self.hass.states.get(v)) is None or s.state in ("unavailable", "unknown")
            ]

        if not offline():
            return
        online = asyncio.Event()

        @callback
        def _changed(_event: Event) -> None:
            if not offline():
                online.set()

        unsub = async_track_state_change_event(self.hass, valves, _changed)
        try:
            async with asyncio.timeout(STARTUP_VALVE_WAIT_S):
                await online.wait()
        except TimeoutError:
            _LOGGER.warning("Valves still offline after %s s: %s; continuing", STARTUP_VALVE_WAIT_S, offline())
        finally:
            unsub()

    async def _reconcile(self, reasons: set[str], requests: list[dict[str, Any]]) -> None:
        tz = dt_util.get_default_time_zone()
        now = time.time()
        forecast = await self._maybe_forecast(now, tz)
        snap = self._build_snapshot(now, tz, reasons, requests, forecast)
        plan = E.reconcile(self.state, self.cfg, snap)
        msgs = self._startup_msgs + list(plan.msgs)
        self._startup_msgs = []
        await self._save()  # the record first, so valve events see the new state
        if plan.close or plan.open is not None:
            self._notify_listeners()

        failed: list[int] = []
        for i in plan.close:
            if not await self._close_confirm(self.cfg.taps[i].valve):
                failed.append(i)
        if failed:
            msgs += E.on_close_failed(self.state, self.cfg, failed[0], time.time(), tz)
        elif plan.close:
            E.after_close_confirmed(self.state, self.cfg, time.time())
        if plan.open is not None and not failed:
            valve = self.cfg.taps[plan.open].valve
            if not await self._open_confirm(valve):
                await self._service(valve, False)
                msgs += E.on_open_failed(self.state, self.cfg, plan.open, time.time(), tz)
        await self._save()

        self.snapshot = self._build_snapshot(time.time(), tz, set(), [], forecast)
        self._schedule(E.next_wake(self.state, self.cfg, self.snapshot))
        for kind, n, text in msgs:
            self.messages.append({"time": dt_util.utcnow().isoformat(), "kind": kind, "number": n, "message": text})
        if msgs or plan.clear:
            self.last_outbox = await async_send(
                self.hass, self.cfg, msgs, plan.clear, self.state.master, self.entry.entry_id
            )
        self._notify_listeners()

    def _schedule(self, wake: float | None) -> None:
        if self._timer_unsub:
            self._timer_unsub()
            self._timer_unsub = None
        self.next_wake = wake
        if wake is None:
            return
        when = dt_util.utc_from_timestamp(max(wake, time.time() + 0.2))
        self._timer_unsub = async_track_point_in_utc_time(self.hass, self._on_timer, when)

    async def _save(self, force: bool = False) -> None:
        data = self.state.to_dict()
        if force or data != self._saved:
            await self.store.async_save(data)
            self._saved = data

    # ------------------------------------------------------------------
    # Valves
    # ------------------------------------------------------------------
    async def _service(self, entity_id: str, on: bool) -> None:
        domain = entity_id.split(".", 1)[0]
        if domain == "valve":
            service = "open_valve" if on else "close_valve"
        elif domain in ("switch", "input_boolean", "light", "fan"):
            service = "turn_on" if on else "turn_off"
        else:
            domain, service = "homeassistant", ("turn_on" if on else "turn_off")
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entity_id}, blocking=True
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s.%s on %s failed: %s", domain, service, entity_id, err)

    def _is(self, entity_id: str, on: bool) -> bool:
        s = self.hass.states.get(entity_id)
        return s is not None and (s.state in ON_STATES if on else s.state in OFF_STATES)

    async def _wait_for(self, entity_id: str, on: bool, timeout: float) -> bool:
        if self._is(entity_id, on):
            return True
        fut: asyncio.Future[bool] = self.hass.loop.create_future()

        @callback
        def _changed(_event: Event) -> None:
            if self._is(entity_id, on) and not fut.done():
                fut.set_result(True)

        unsub = async_track_state_change_event(self.hass, [entity_id], _changed)
        try:
            async with asyncio.timeout(timeout):
                await fut
            return True
        except TimeoutError:
            return self._is(entity_id, on)
        finally:
            unsub()

    async def _close_confirm(self, entity_id: str) -> bool:
        """Close a valve and confirm it reports off (3 tries)."""
        if not entity_id:
            return True
        for _ in range(CLOSE_TRIES):
            if self._is(entity_id, False):
                return True
            s = self.hass.states.get(entity_id)
            if s is None or s.state in ("unavailable", "unknown"):
                return False  # offline: cannot be confirmed off
            await self._service(entity_id, False)
            if await self._wait_for(entity_id, False, self.cfg.confirm_s):
                return True
        return False

    async def _open_confirm(self, entity_id: str) -> bool:
        await self._service(entity_id, True)
        return await self._wait_for(entity_id, True, self.cfg.confirm_s)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def _state(self, entity_id: str):
        return self.hass.states.get(entity_id) if entity_id else None

    def _num(self, entity_id: str) -> float | None:
        s = self._state(entity_id)
        return _float(s.state) if s is not None else None

    def _temp_c(self) -> float | None:
        cfg = self.cfg
        if cfg.temp_sensor:
            s = self._state(cfg.temp_sensor)
            v = _float(s.state) if s else None
            if v is not None and s.attributes.get("unit_of_measurement") == UnitOfTemperature.FAHRENHEIT:
                v = TemperatureConverter.convert(v, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS)
            return v
        if cfg.weather_entity and (s := self._state(cfg.weather_entity)) is not None:
            v = _float(s.attributes.get("temperature"))
            unit = s.attributes.get("temperature_unit", UnitOfTemperature.CELSIUS)
            if v is not None and unit == UnitOfTemperature.FAHRENHEIT:
                v = TemperatureConverter.convert(v, unit, UnitOfTemperature.CELSIUS)
            return v
        return None

    def _wind_kmh(self) -> float | None:
        cfg = self.cfg

        def conv(v: float | None, unit: str | None) -> float | None:
            if v is None or not unit or unit == UnitOfSpeed.KILOMETERS_PER_HOUR:
                return v
            try:
                return SpeedConverter.convert(v, unit, UnitOfSpeed.KILOMETERS_PER_HOUR)
            except HomeAssistantError:
                return v

        if cfg.wind_sensor:
            s = self._state(cfg.wind_sensor)
            return conv(_float(s.state), s.attributes.get("unit_of_measurement")) if s else None
        if cfg.weather_entity and (s := self._state(cfg.weather_entity)) is not None:
            return conv(_float(s.attributes.get("wind_speed")), s.attributes.get("wind_speed_unit"))
        return None

    def _build_snapshot(
        self, now: float, tz, reasons: set[str], requests: list[dict[str, Any]], forecast: dict[str, Any] | None
    ) -> E.Snapshot:
        cfg = self.cfg
        clear_s = max(cfg.people_clear_min * 60, 0)
        valves, changed = [], []
        people, clear_at, who = [], [], []
        bypass, bypass_until = [], []
        soil, soil_bad = [], []
        for t in cfg.taps:
            s = self._state(t.valve)
            if not t.valve:
                valves.append(E.V_NONE)
            elif s is None:
                valves.append(E.V_OFFLINE)
            elif s.state in ON_STATES:
                valves.append(E.V_ON)
            elif s.state in OFF_STATES:
                valves.append(E.V_OFF)
            else:
                valves.append(E.V_OFFLINE)
            changed.append(s.last_changed.timestamp() if s is not None else 0.0)

            # people guard: 1 = someone there, 2 = left less than 'clear for' ago
            busy, last, name = False, 0.0, ""
            if cfg.people_on:
                for pid in t.people:
                    ps = self._state(pid)
                    if ps is None:
                        continue
                    last = max(last, ps.last_changed.timestamp())
                    if ps.state == "on" or (_float(ps.state) or 0) > 0:
                        busy = True
                        name = name or ps.attributes.get("friendly_name", pid)
            if busy:
                people.append(1)
                clear_at.append(0.0)
            elif t.people and cfg.people_on and now - last < clear_s:
                people.append(2)
                clear_at.append(last + clear_s)
            else:
                people.append(0)
                clear_at.append(0.0)
            who.append(name)

            # bypass (e.g. pressure washer): plus a short grace after it turns off
            on, until = False, 0.0
            if t.bypass and (bs := self._state(t.bypass)) is not None:
                if bs.state == "on":
                    on = True
                elif valves[-1] == E.V_ON and now - bs.last_changed.timestamp() < E.BYPASS_GRACE_S:
                    on, until = True, bs.last_changed.timestamp() + E.BYPASS_GRACE_S
            bypass.append(on)
            bypass_until.append(until)

            if t.soil:
                v = self._num(t.soil)
                soil.append(v)
                soil_bad.append(v is None)
            else:
                soil.append(None)
                soil_bad.append(False)

        seasonal_ext = self._num(cfg.seasonal_entity) if cfg.seasonal_entity else None
        rain = self._state(cfg.rain_sensor)
        weather = self._state(cfg.weather_entity)
        return E.Snapshot(
            now=now, tz=tz, valves=valves, valve_changed=changed,
            people=people, people_clear_at=clear_at, people_who=who,
            bypass=bypass, bypass_until=bypass_until, soil=soil, soil_bad=soil_bad,
            seasonal_ext=seasonal_ext, seasonal_ext_bad=bool(cfg.seasonal_entity) and seasonal_ext is None,
            rain_state=rain.state if rain else None, rain_value=_float(rain.state) if rain else None,
            weather_state=weather.state if weather else None, forecast=forecast,
            wind=self._wind_kmh(), temp=self._temp_c(), reasons=set(reasons), requests=list(requests),
        )

    async def _maybe_forecast(self, now: float, tz) -> dict[str, Any] | None:
        """Fetch today's forecast only when a cycle may be about to start."""
        cfg, st = self.cfg, self.state
        if not (cfg.wx_on and cfg.weather_entity and (cfg.fc_prob > 0 or cfg.fc_mm > 0)):
            return None
        if st.mode != E.IDLE or st.cycle >= 0:
            return None
        due = bool(st.pending) or any(
            int(E.last_occurrence(c, now, tz) // 60) > (st.handled[i] if i < len(st.handled) else 0)
            for i, c in enumerate(cfg.cycles)
        )
        if not due:
            return None
        fetched, cached = self._forecast
        if cached is not None and now - fetched < FORECAST_CACHE_S:
            return cached
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": cfg.weather_entity, "type": "daily"},
                blocking=True, return_response=True,
            )
            fc = (resp or {}).get(cfg.weather_entity, {}).get("forecast") or []
            today = fc[0] if fc else None
        except (HomeAssistantError, ValueError, KeyError, TypeError) as err:
            _LOGGER.warning("Could not get the forecast from %s: %s", cfg.weather_entity, err)
            today = None
        self._forecast = (now, today)
        return today

    # ------------------------------------------------------------------
    # Views for entities / card
    # ------------------------------------------------------------------
    def status(self) -> str:
        return E.status(self.state, self.cfg, self.snapshot)

    def next_cycle(self) -> tuple[float, int] | None:
        now = time.time()
        tz = dt_util.get_default_time_zone()
        best: tuple[float, int] | None = None
        for i, c in enumerate(self.cfg.cycles):
            if not self.state.cycle_on[i] or not E.cycle_has_water(self.cfg, i):
                continue
            occ = E.next_occurrence(c, now, tz)
            if occ and (best is None or occ < best[0]):
                best = (occ, i)
        return best

    def next_cycle_reason(self) -> str | None:
        """Why no cycle is scheduled (None when one is), in plain words for the card and the sensor."""
        if self.next_cycle() is not None:
            return None
        cfg, st = self.cfg, self.state
        with_water = [i for i in range(len(cfg.cycles)) if E.cycle_has_water(cfg, i)]
        if not with_water:
            return "No cycle has minutes yet"
        if not any(i < len(st.cycle_on) and st.cycle_on[i] for i in with_water):
            return "The cycles with minutes are switched off"
        return "No days are selected for the cycles with minutes"

    def next_tap(self) -> int | None:
        """The tap (0-based) the running cycle opens next, or None.

        Same choice as the engine: lowest run order among taps that still have
        time and are ready (not soaking) when the current step ends; if none is
        ready then, the one whose soak ends first.
        """
        st, cfg = self.state, self.cfg
        if st.cycle < 0 or st.cycle >= len(cfg.cycles):
            return None
        c = cfg.cycles[st.cycle]
        current = st.tap if st.mode == E.WATERING else -1
        cands = [
            t for t in range(len(cfg.taps))
            if t != current and cfg.taps[t].valve and t < len(st.remaining) and st.remaining[t] > 0
        ]
        if not cands:
            return None
        at = max(st.end, time.time())
        ready = [t for t in cands if st.ready[t] <= at]
        if not ready:
            first = min(st.ready[t] for t in cands)
            ready = [t for t in cands if st.ready[t] <= first]
        return min(ready, key=lambda t: (c.order[t], t))

    def status_attributes(self) -> dict[str, Any]:
        st, cfg = self.state, self.cfg
        tz = dt_util.get_default_time_zone()
        nxt = self.next_tap()

        def iso(ts: float) -> str | None:
            return datetime.fromtimestamp(ts, tz).isoformat() if ts else None

        attrs: dict[str, Any] = {
            "cycle": st.cycle + 1 if st.cycle >= 0 else None,
            "tap": st.tap + 1 if st.tap >= 0 else None,
            "tap_name": cfg.label(st.tap) if st.tap >= 0 else None,
            "next_tap": nxt + 1 if nxt is not None else None,
            "next_tap_name": cfg.label(nxt) if nxt is not None else None,
            "ends_at": iso(st.end) if st.mode in (E.WATERING, E.MANUAL) else None,
            "next_step": iso(self.next_wake) if self.next_wake else None,
            "remaining_min": {cfg.label(i): round(s / 60, 1) for i, s in enumerate(st.remaining) if s > 0},
            "pending": [
                {"cycle": p["cycle"] + 1, "due": iso(p["due"] * 60), "run_now": p["forced"]} for p in st.pending
            ],
            "master": st.master,
            "seasonal": st.seasonal,
            "config_rev": self.config_rev,
            "last_message": self.messages[-1]["message"] if self.messages else None,
        }
        return attrs
