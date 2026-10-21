"""A small world simulator for the engine: fake clock, fake valves, fake sensors.

It mimics what the coordinator does: build a snapshot, reconcile, save,
close + confirm (3 tries), open + confirm, feed failures back, and run
reconcile again on every valve change, at the next wake-up and on the
1-minute watchdog. After every step it checks the core invariant:
never two (non-bypassed) valves open.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.irrigation_sequencer import engine as E
from custom_components.irrigation_sequencer.model import normalize, parse

TZ = ZoneInfo("Australia/Sydney")


def ts(s: str) -> float:
    """'2026-10-12 06:00' or '2026-10-12 06:00:30' local -> epoch."""
    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp()


def hms(t: float) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")


@dataclass
class Valve:
    state: str = "off"  # on / off / offline
    changed: float = 0.0
    responds_off: bool = True  # False = won't close (stuck)
    responds_on: bool = True  # False = won't report on


@dataclass
class World:
    options: dict
    now: float
    state: E.State = field(default_factory=E.State)
    valves: list[Valve] = field(default_factory=list)
    people: list[int] = field(default_factory=list)
    people_clear_at: list[float] = field(default_factory=list)
    bypass: list[bool] = field(default_factory=list)
    soil: list[float | None] = field(default_factory=list)
    soil_bad: list[bool] = field(default_factory=list)
    rain_state: str | None = None
    rain_value: float | None = None
    weather_state: str | None = None
    forecast: dict | None = None
    wind: float | None = None
    temp: float | None = None
    seasonal_ext: float | None = None
    log: list[tuple[float, str]] = field(default_factory=list)  # valve on/off timeline
    msgs: list[tuple[float, str, int, str]] = field(default_factory=list)
    clears: list[tuple[float, str]] = field(default_factory=list)
    wake: float | None = None
    max_open: int = 0
    down: bool = False  # Home Assistant is "down": nothing runs
    events: list[tuple[float, callable]] = field(default_factory=list)
    main: Valve | None = None  # the main water valve (made "on" when the options set one)

    def __post_init__(self) -> None:
        self.options = normalize(self.options)
        self.cfg = parse(self.options)
        T = len(self.cfg.taps)
        if not self.valves:
            self.valves = [Valve() for _ in range(T)]
        self.people = self.people or [0] * T
        self.people_clear_at = self.people_clear_at or [0.0] * T
        self.bypass = self.bypass or [False] * T
        self.soil = self.soil or [None] * T
        self.soil_bad = self.soil_bad or [False] * T
        if self.main is None and self.cfg.main_valve:
            self.main = Valve(state="on")
        self.state.master = True

    # ------------------------------------------------------------------
    def set_options(self, options: dict) -> None:
        self.options = normalize(options)
        self.cfg = parse(self.options)

    def snapshot(self, reasons: set[str], requests: list | None = None) -> E.Snapshot:
        T = len(self.cfg.taps)
        valves = []
        for i in range(T):
            if not self.cfg.taps[i].valve:
                valves.append(E.V_NONE)
            else:
                valves.append({"on": E.V_ON, "off": E.V_OFF}.get(self.valves[i].state, E.V_OFFLINE))
        return E.Snapshot(
            now=self.now, tz=TZ, valves=valves, valve_changed=[v.changed for v in self.valves],
            people=list(self.people), people_clear_at=list(self.people_clear_at),
            people_who=["Camera"] * T, bypass=list(self.bypass), bypass_until=[0.0] * T,
            soil=list(self.soil), soil_bad=list(self.soil_bad), seasonal_ext=self.seasonal_ext,
            rain_state=self.rain_state, rain_value=self.rain_value, weather_state=self.weather_state,
            forecast=self.forecast, wind=self.wind, temp=self.temp, reasons=set(reasons),
            requests=list(requests or []),
            main=(E.V_NONE if not self.cfg.main_valve or self.main is None
                  else {"on": E.V_ON, "off": E.V_OFF}.get(self.main.state, E.V_OFFLINE)),
        )

    def _set_valve(self, i: int, st: str) -> bool:
        v = self.valves[i]
        if v.state != st:
            v.state, v.changed = st, self.now
            self.log.append((self.now, f"T{i + 1} {st}"))
            return True
        return False

    def check(self) -> None:
        open_now = sum(1 for i, v in enumerate(self.valves) if v.state == "on" and not self.bypass[i])
        self.max_open = max(self.max_open, open_now)

    # ------------------------------------------------------------------
    def reconcile(self, reasons: set[str], requests: list | None = None) -> None:
        """One coordinator pass, then follow-up passes for the changes it caused."""
        if self.down:
            return
        for _ in range(10):
            plan = E.reconcile(self.state, self.cfg, self.snapshot(reasons, requests))
            requests = None
            changed = self.execute(plan)
            reasons = {"valve"} if changed else set()
            if not changed:
                break
        self.check()

    def execute(self, plan: E.Plan) -> bool:
        changed = False
        msgs = list(plan.msgs)
        failed = []
        for i in plan.close:
            v = self.valves[i]
            if v.state == "offline":
                failed.append(i)
                continue
            if v.responds_off:
                changed |= self._set_valve(i, "off")
            else:
                failed.append(i)
        if failed:
            msgs += E.on_close_failed(self.state, self.cfg, failed[0], self.now, TZ)
        elif plan.close:
            E.after_close_confirmed(self.state, self.cfg, self.now)
        if plan.open is not None and not failed:
            i = plan.open
            v = self.valves[i]
            if v.responds_on and v.state != "offline":
                changed |= self._set_valve(i, "on")
            else:
                if v.state != "offline":
                    self._set_valve(i, "off")
                msgs += E.on_open_failed(self.state, self.cfg, i, self.now, TZ)
        if plan.main and self.cfg.main_valve and self.main is not None:
            want = "off" if plan.main == "close" else "on"
            responds = self.main.responds_off if want == "off" else self.main.responds_on
            ok = responds and self.main.state != "offline"
            if ok and self.main.state != want:
                self.main.state, self.main.changed = want, self.now
                self.log.append((self.now, f"MAIN {want}"))
                changed = True
            msgs += E.on_main_done(self.state, self.cfg, plan.main, ok, self.now, TZ)
        for k, n, t in msgs:
            self.msgs.append((self.now, k, n, t))
        for c in plan.clear:
            self.clears.append((self.now, c))
        self.check()
        snap = self.snapshot(set())
        self.wake = E.next_wake(self.state, self.cfg, snap)
        # round-trip the state through JSON form, as the store does
        self.state, ok = E.State.from_dict(copy.deepcopy(self.state.to_dict()))
        assert ok
        return changed

    # ------------------------------------------------------------------
    def at(self, when: str | float, fn) -> None:
        t = ts(when) if isinstance(when, str) else when
        self.events.append((t, fn))
        self.events.sort(key=lambda e: e[0])

    def run_until(self, until: str | float) -> None:
        end = ts(until) if isinstance(until, str) else until
        watchdog = (int(self.now // 60) + 1) * 60
        while self.now < end:
            cands = [end, watchdog]
            if self.wake is not None and self.wake > self.now:
                cands.append(self.wake)
            elif self.wake is not None:
                cands.append(self.now + 0.5)
            if self.events:
                cands.append(self.events[0][0])
            t = max(min(cands), self.now)
            self.now = t
            if self.events and self.events[0][0] <= self.now:
                _, fn = self.events.pop(0)
                r = fn(self)
                self.reconcile(r if isinstance(r, set) else {"event"})
                continue
            if t >= watchdog:
                watchdog += 60
                self.reconcile({"watchdog"})
            elif self.wake is not None and t >= self.wake:
                self.reconcile({"timer"})
            if t >= end:
                break

    # helpers for scenarios --------------------------------------------
    def user_on(self, i: int):
        def fn(w: World):
            w._set_valve(i, "on")
            return {"valve"}
        return fn

    def user_off(self, i: int):
        def fn(w: World):
            w._set_valve(i, "off")
            return {"valve"}
        return fn

    def request(self, req: dict):
        """A service call / notification button: one pass with the request."""
        def fn(w: World):
            w.reconcile({"service"}, [req])
            return set()
        return fn

    def main_to(self, state: str):
        """The main water valve changes by hand (on / off / offline)."""
        def fn(w: World):
            w.main.state, w.main.changed = state, w.now
            w.log.append((w.now, f"MAIN {state}"))
            return {"main"}
        return fn

    def restart(self, down_for_s: float = 0):
        """HA stops now and starts down_for_s later (valves keep their state)."""
        def stop(w: World):
            w.down = True
            w.wake = None
            return set()

        def start(w: World):
            w.down = False
            st, ok = E.State.from_dict(copy.deepcopy(w.state.to_dict()))
            assert ok
            w.state = st
            return {"startup"}
        return stop, start

    def on_periods(self, i: int) -> list[tuple[float, float]]:
        """[(on, off), ...] for tap i (0-based)."""
        out, start = [], None
        for t, ev in self.log:
            if ev == f"T{i + 1} on":
                start = t
            elif ev == f"T{i + 1} off" and start is not None:
                out.append((start, t))
                start = None
        if start is not None:
            out.append((start, float("inf")))
        return out

    def watered(self, i: int) -> float:
        return sum(b - a for a, b in self.on_periods(i) if b != float("inf"))

    def order(self) -> list[str]:
        return [ev.split()[0] for _t, ev in self.log if ev.endswith(" on") and ev.startswith("T")]

    def texts(self, kind: str | None = None) -> list[str]:
        return [t for _ts, k, _n, t in self.msgs if kind is None or k == kind]

    def dump(self) -> str:
        lines = [f"{hms(t)} {ev}" for t, ev in self.log]
        lines += [f"{hms(t)} [{k} {n}] {txt}" for t, k, n, txt in self.msgs]
        return "\n".join(sorted(lines))


def opts(taps: int = 3, cycles: int = 2, **kw) -> dict:
    """Options with toggle-helper valves input_boolean.tap_N."""
    o = normalize({"tap_count": taps, "cycle_count": cycles})
    for i in range(taps):
        o["taps"][i]["valve"] = f"input_boolean.tap_{i + 1}"
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(o.get(k), dict):
            o[k].update(v)
        else:
            o[k] = v
    return o


def world(options: dict, start: str, **kw) -> World:
    w = World(options=options, now=ts(start), **kw)
    w.reconcile({"startup"})  # first ever start: the past counts as handled
    return w
