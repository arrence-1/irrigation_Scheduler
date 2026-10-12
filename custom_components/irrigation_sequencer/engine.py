"""Irrigation Scheduler engine.

One routine, :func:`reconcile`, makes every decision. The coordinator builds
a :class:`Snapshot` of the world (clock, valve states, sensors), calls
``reconcile`` with the saved :class:`State`, saves the state, then carries
out the returned :class:`Plan` (close valves and confirm, open a valve and
confirm, notify, set the next wake-up). Failures while carrying it out are
fed back through :func:`on_close_failed` / :func:`on_open_failed`.

Pure Python, no Home Assistant imports: the simulator tests drive it with a
fake clock and fake valves.

Ported from the Irrigation Sequencer blueprint v1.5.1 planner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, tzinfo
from typing import Any

from .model import DAYS, Cfg, CycleCfg

STATE_VERSION = 1

IDLE = "idle"
GAP = "gap"  # between taps, soak wait, people wait, bypass hold
WATERING = "watering"
MANUAL = "manual"
STUCK = "stuck"
MODES = (IDLE, GAP, WATERING, MANUAL, STUCK)

# valve states as reported in the snapshot
V_ON = "on"
V_OFF = "off"
V_OFFLINE = "offline"
V_NONE = "none"  # no valve configured for this tap

STUCK_RETRY_S = 300
PEOPLE_POLL_S = 30
BYPASS_GRACE_S = 30
MISSED_WINDOW_S = 24 * 3600
LATE_NOTE_S = 90
HOT_FOR_S = 600
HOT_REARM_C = 1.0
RAIN_REARM_S = 1800
WET_WEATHER = ("rainy", "pouring", "lightning-rainy")


# --------------------------------------------------------------------------
# Saved state
# --------------------------------------------------------------------------
@dataclass
class State:
    """Everything the engine must remember across restarts (stored as JSON)."""

    version: int = STATE_VERSION
    # run record
    mode: str = IDLE
    tap: int = -1  # active tap (0-based) for watering / manual / stuck
    end: float = 0.0  # end of the current step (watering, manual, gap, stuck retry)
    cycle: int = -1  # running or paused cycle (0-based)
    forced: bool = False  # running cycle was started by run_cycle (ignores switches)
    t0: float = 0.0  # cycle start
    remaining: list[int] = field(default_factory=list)  # seconds left per tap
    ready: list[float] = field(default_factory=list)  # epoch when a tap may run again (soak / people)
    people_wait: float = 0.0  # when the cycle started waiting for people-blocked taps
    held_note: bool = False  # "held by bypass" was announced for this hold
    # schedule memory
    fingerprints: list[str] = field(default_factory=list)
    handled: list[int] = field(default_factory=list)  # epoch minute of last handled occurrence
    pending: list[dict[str, Any]] = field(default_factory=list)  # {"cycle", "due" (epoch min), "forced"}
    # switches owned by the app
    master: bool = False
    cycle_on: list[bool] = field(default_factory=list)
    seasonal: float = 100.0
    # alert memory
    reminders: dict[str, list[float]] = field(default_factory=dict)  # cycle -> [occurrence ts, level]
    hot_since: float = 0.0
    hot_alerted: bool = False
    rain_seen: bool = False
    rain_dry_since: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> tuple[State, bool]:
        """Load a stored dict. Returns (state, ok). Not ok = reset to idle."""
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return cls(), False
        st = cls()
        try:
            for k in st.to_dict():
                if k in data:
                    setattr(st, k, data[k])
            if st.mode not in MODES:
                raise ValueError("bad mode")
            st.tap, st.cycle = int(st.tap), int(st.cycle)
            st.end, st.t0 = float(st.end), float(st.t0)
            st.remaining = [int(x) for x in st.remaining]
            st.ready = [float(x) for x in st.ready]
            st.handled = [int(x) for x in st.handled]
            st.fingerprints = [str(x) for x in st.fingerprints]
            st.cycle_on = [bool(x) for x in st.cycle_on]
            st.pending = [
                {"cycle": int(p["cycle"]), "due": int(p["due"]), "forced": bool(p.get("forced", False))}
                for p in st.pending
            ]
            st.seasonal = float(st.seasonal)
            st.reminders = {str(k): [float(v[0]), float(v[1])] for k, v in dict(st.reminders).items()}
        except (TypeError, ValueError, KeyError, IndexError):
            fresh = cls()
            # keep the user's switches if they are readable
            for k in ("master", "cycle_on", "seasonal", "fingerprints", "handled"):
                try:
                    setattr(fresh, k, data[k])
                except KeyError:
                    pass
            return fresh, False
        return st, True


# --------------------------------------------------------------------------
# Snapshot of the world and the plan
# --------------------------------------------------------------------------
@dataclass
class Snapshot:
    now: float
    tz: tzinfo
    valves: list[str]  # per tap: on / off / offline / none
    valve_changed: list[float]  # last_changed epoch per tap
    people: list[int] = field(default_factory=list)  # 0 clear, 1 someone there, 2 left < clear time ago
    people_clear_at: list[float] = field(default_factory=list)
    people_who: list[str] = field(default_factory=list)
    bypass: list[bool] = field(default_factory=list)  # effective (incl. grace after it turns off)
    bypass_until: list[float] = field(default_factory=list)  # end of the grace period (0 = none)
    soil: list[float | None] = field(default_factory=list)  # None = no sensor or unavailable
    soil_bad: list[bool] = field(default_factory=list)  # sensor configured but unavailable
    seasonal_ext: float | None = None  # external seasonal entity value (None = not set / unavailable)
    seasonal_ext_bad: bool = False
    rain_state: str | None = None
    rain_value: float | None = None
    weather_state: str | None = None
    forecast: dict[str, Any] | None = None  # today's daily forecast
    wind: float | None = None  # km/h
    temp: float | None = None  # °C
    reasons: set[str] = field(default_factory=set)
    requests: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Plan:
    close: list[int] = field(default_factory=list)
    open: int | None = None
    msgs: list[tuple[str, int, str]] = field(default_factory=list)  # (kind, number, text)
    clear: list[str] = field(default_factory=list)  # tag suffixes, e.g. "reminder_1"
    next_wake: float | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _hm(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _dhm(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M")


def _g(x: float) -> str:
    """Compact number: 2.0 -> '2', 2.5 -> '2.5'."""
    return f"{x:g}"


def _parse_hm(s: str) -> tuple[int, int]:
    try:
        h, m = s.split(":")[:2]
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except (ValueError, AttributeError):
        return 6, 0


def fingerprint(c: CycleCfg) -> str:
    h, m = _parse_hm(c.time)
    mask = sum(1 << i for i, d in enumerate(DAYS) if d in c.days)
    return f"{h:02d}{m:02d}:{mask}"


def last_occurrence(c: CycleCfg, now: float, tz: tzinfo) -> float:
    """Most recent scheduled start at or before now, within 24 h (0 = none)."""
    h, m = _parse_hm(c.time)
    nd = datetime.fromtimestamp(now, tz)
    today = datetime.combine(nd.date(), dtime(h, m), tzinfo=tz)
    if today.timestamp() <= now:
        return today.timestamp() if DAYS[today.weekday()] in c.days else 0.0
    yday = datetime.combine(nd.date() - timedelta(days=1), dtime(h, m), tzinfo=tz)
    return yday.timestamp() if DAYS[yday.weekday()] in c.days else 0.0


def next_occurrence(c: CycleCfg, now: float, tz: tzinfo) -> float:
    """Next scheduled start after now (0 = no days selected)."""
    h, m = _parse_hm(c.time)
    nd = datetime.fromtimestamp(now, tz)
    for k in range(0, 8):
        dt = datetime.combine(nd.date() + timedelta(days=k), dtime(h, m), tzinfo=tz)
        if dt.timestamp() > now and DAYS[dt.weekday()] in c.days:
            return dt.timestamp()
    return 0.0


def cycle_has_water(cfg: Cfg, ci: int) -> bool:
    c = cfg.cycles[ci]
    return any(cfg.taps[t].valve and c.minutes[t] > 0 for t in range(len(cfg.taps)))


def manual_seconds(cfg: Cfg, tap: int) -> int:
    """Manual timer: the tap's longest cycle minutes, capped by the safety limit."""
    maxrt = max(cfg.max_run_min * 60, 60)
    longest = max((c.minutes[tap] for c in cfg.cycles), default=0) * 60
    return maxrt if longest <= 0 else min(longest, maxrt)


def fit(st: State, cfg: Cfg) -> None:
    """Fit the per-tap and per-cycle lists to the current counts."""
    T, C = len(cfg.taps), len(cfg.cycles)

    def _fit(lst: list, n: int, fill: Any) -> list:
        return (list(lst) + [fill] * n)[:n]

    st.remaining = _fit(st.remaining, T, 0)
    st.ready = _fit(st.ready, T, 0.0)
    st.cycle_on = _fit(st.cycle_on, C, True)
    if st.tap >= T:
        st.tap = -1
        if st.mode in (WATERING, MANUAL, STUCK):
            st.mode = GAP if st.cycle >= 0 else IDLE
            st.end = 0.0
    if st.cycle >= C:
        _reset_cycle(st, T)
        if st.mode in (WATERING, GAP):
            st.mode, st.tap, st.end = IDLE, -1, 0.0
    st.pending = [p for p in st.pending if 0 <= p["cycle"] < C]
    st.reminders = {k: v for k, v in st.reminders.items() if k.isdigit() and int(k) < C}


def _reset_cycle(st: State, T: int) -> None:
    st.cycle = -1
    st.forced = False
    st.t0 = 0.0
    st.remaining = [0] * T
    st.ready = [0.0] * T
    st.people_wait = 0.0
    st.held_note = False


def _people_on(cfg: Cfg, sn: Snapshot, i: int) -> int:
    if not cfg.people_on or i >= len(sn.people):
        return 0
    return sn.people[i]


def _bypassed(sn: Snapshot, i: int) -> bool:
    return i < len(sn.bypass) and sn.bypass[i]


# --------------------------------------------------------------------------
# The reconcile routine
# --------------------------------------------------------------------------
def reconcile(st: State, cfg: Cfg, sn: Snapshot) -> Plan:  # noqa: C901 - one routine by design
    """Compare the saved state with the clock and the valves; decide the next step(s)."""
    plan = Plan()
    msg = plan.msgs.append
    tz = sn.tz
    now = sn.now
    T, C = len(cfg.taps), len(cfg.cycles)
    fit(st, cfg)
    hm = _hm(now, tz)
    gap = max(cfg.gap_s, 0)
    maxrt = max(cfg.max_run_min * 60, 60)
    clear_s = max(cfg.people_clear_min * 60, 0)
    master_on = st.master
    startup = bool({"startup", "reload"} & sn.reasons)
    loaded = (st.mode, st.tap, st.end, st.cycle, len(st.pending))
    lbl = cfg.label
    on = [sn.valves[i] == V_ON for i in range(T)]
    held_taps = [i for i in range(T) if _bypassed(sn, i)]
    held = bool(held_taps)

    def close(i: int) -> None:
        if 0 <= i < T and i not in plan.close and sn.valves[i] != V_NONE:
            plan.close.append(i)

    def to_gap_or_idle() -> None:
        st.tap = -1
        if st.cycle >= 0:
            st.mode, st.end = GAP, now + gap
        else:
            st.mode, st.end = IDLE, 0.0

    def give_back(i: int) -> None:
        """A scheduled tap stopped early keeps its unused time."""
        if 0 <= i < T:
            st.remaining[i] += max(int(round(st.end - now)), 0)

    # ---------- 2. due and missed cycles -> pending queue ----------
    occ_last = [last_occurrence(c, now, tz) for c in cfg.cycles]
    occ_next = [next_occurrence(c, now, tz) for c in cfg.cycles]
    fps = [fingerprint(c) for c in cfg.cycles]
    first_run = not st.fingerprints
    st.fingerprints = (list(st.fingerprints) + [""] * C)[:C]
    st.handled = (list(st.handled) + [0] * C)[:C]
    for i in range(C):
        if first_run or st.fingerprints[i] != fps[i]:
            # first start, a new cycle slot, or its time/days were edited: the past counts as handled
            st.handled[i] = int(occ_last[i] // 60)
            st.fingerprints[i] = fps[i]
    for i in range(C):
        lm = int(occ_last[i] // 60)
        if lm > 0 and lm > st.handled[i]:
            st.handled[i] = lm
            if master_on and st.cycle_on[i] and cycle_has_water(cfg, i):
                if st.cycle == i or any(p["cycle"] == i for p in st.pending):
                    msg(("cycle", i + 1, f"Cycle {i + 1} is due again while its previous run is still "
                                         "waiting or running; it will not run twice."))
                else:
                    st.pending.append({"cycle": i, "due": lm, "forced": False})
    st.pending.sort(key=lambda p: (not p["forced"], p["due"]))

    # ---------- 3. master / cycle switches, service requests ----------
    if st.cycle >= 0:
        ci = st.cycle
        why = ""
        if st.forced:
            if "master_off" in sn.reasons:
                why = "the master switch was turned off"
            elif f"cycle_off_{ci}" in sn.reasons:
                why = "its cycle switch was turned off"
        elif not master_on:
            why = "the master switch is off"
        elif not st.cycle_on[ci]:
            why = "its cycle switch is off"
        if why:
            if st.mode == WATERING:
                close(st.tap)
            msg(("cycle", ci + 1, f"Cycle {ci + 1} stopped at {hm}: {why}."))
            _reset_cycle(st, T)
            if st.mode in (WATERING, GAP):
                st.mode, st.tap, st.end = IDLE, -1, 0.0
    keep = []
    for p in st.pending:
        ci = p["cycle"]
        if p["forced"]:
            if "master_off" in sn.reasons or f"cycle_off_{ci}" in sn.reasons:
                msg(("cycle", ci + 1, f"Cycle {ci + 1} cancelled: "
                     + ("the master switch was turned off." if "master_off" in sn.reasons
                        else "its cycle switch was turned off.")))
                continue
            keep.append(p)
        elif master_on and st.cycle_on[ci]:
            keep.append(p)
        else:
            msg(("cycle", ci + 1, f"Cycle {ci + 1} cancelled: "
                 + ("the master switch is off." if not master_on else "its cycle switch is off.")))
    st.pending = keep

    run_tap_req: dict[str, Any] | None = None
    for req in sn.requests:
        kind = req.get("type")
        if kind == "stop":
            if st.mode in (WATERING, MANUAL):
                close(st.tap)
            if st.cycle >= 0:
                msg(("cycle", st.cycle + 1, f"Cycle {st.cycle + 1} stopped at {hm} (stop requested)."))
            for p in st.pending:
                msg(("cycle", p["cycle"] + 1, f"Cycle {p['cycle'] + 1} cancelled (stop requested)."))
            if st.mode == MANUAL:
                msg(("manual", 0, f"Manual run on {lbl(st.tap)} stopped at {hm} (stop requested)."))
            st.pending = []
            _reset_cycle(st, T)
            if st.mode != STUCK:
                st.mode, st.tap, st.end = IDLE, -1, 0.0
        elif kind == "skip_tap":
            i = int(req.get("tap", -1))
            if st.cycle >= 0 and 0 <= i < T:
                if st.mode == WATERING and st.tap == i:
                    close(i)
                    st.remaining[i] = 0
                    to_gap_or_idle()
                elif st.remaining[i] > 0:
                    st.remaining[i] = 0
                msg(("cycle", st.cycle + 1, f"{lbl(i)} skipped in cycle {st.cycle + 1} (requested)."))
        elif kind == "run_cycle":
            ci = int(req.get("cycle", -1))
            if 0 <= ci < C:
                if st.cycle == ci or any(p["cycle"] == ci for p in st.pending):
                    msg(("cycle", ci + 1, f"Cycle {ci + 1} is already running or waiting."))
                else:
                    st.pending.insert(0, {"cycle": ci, "due": int(now // 60), "forced": True})
        elif kind == "run_tap":
            run_tap_req = req

    # ---------- 4. what the valves are really doing ----------
    rv = st.tap if (st.mode in (WATERING, MANUAL, STUCK) and 0 <= st.tap < T) else -1
    newest, newest_ts = -1, -1.0
    others_on = []
    for i in range(T):
        if on[i] and i != rv and i not in plan.close and not _bypassed(sn, i):
            others_on.append(i)
            ts = sn.valve_changed[i] if i < len(sn.valve_changed) else 0.0
            if ts > newest_ts:
                newest, newest_ts = i, ts

    if run_tap_req is not None and st.mode != STUCK:
        i = int(run_tap_req.get("tap", -1))
        secs = int(run_tap_req.get("seconds", 0))
        if 0 <= i < T and cfg.taps[i].valve and secs > 0 and sn.valves[i] != V_OFFLINE:
            secs = min(secs, maxrt)
            for j in range(T):
                if j != i and on[j] and not _bypassed(sn, j):
                    close(j)
            if st.mode == WATERING and rv >= 0:
                give_back(rv)
            repl = f" {lbl(rv)} was closed." if st.mode == MANUAL and rv >= 0 and rv != i else ""
            msg(("manual", 0, f"Manual run: {lbl(i)} for {_g(secs / 60)} min, until {_hm(now + secs, tz)}."
                 + repl + (f" Cycle {st.cycle + 1} paused." if st.cycle >= 0 else "")))
            st.mode, st.tap, st.end = MANUAL, i, now + secs
            if not on[i]:
                plan.open = i
            rv = i
            others_on = []
        elif 0 <= i < T:
            msg(("tap", i + 1, f"{lbl(i)} could not be started: "
                 + ("it has no valve." if not cfg.taps[i].valve else "it is offline.")))

    if others_on and st.mode == STUCK:
        # a tap is stuck open: nothing else may run, not even a manual run
        for i in others_on:
            close(i)
        msg(("tap", st.tap + 1, f"{lbl(newest)} was closed again because {lbl(st.tap)} "
                                "cannot be confirmed off."))
    elif others_on:
        # manual run: the newest tap wins, every other open tap closes, a running cycle pauses
        for i in others_on:
            if i != newest:
                close(i)
        if st.mode in (WATERING, MANUAL) and rv >= 0:
            close(rv)
        if st.mode == WATERING:
            give_back(rv)
        mt = manual_seconds(cfg, newest)
        repl = f" {lbl(rv)} was closed." if st.mode == MANUAL and rv >= 0 else ""
        msg(("manual", 0, f"Manual run: {lbl(newest)} for {_g(mt / 60)} min, until {_hm(now + mt, tz)}."
             + repl + (f" Cycle {st.cycle + 1} paused." if st.cycle >= 0 else "")))
        st.mode, st.tap, st.end = MANUAL, newest, now + mt
    elif rv >= 0 and st.mode == MANUAL and _bypassed(sn, rv):
        # the tap was handed to its bypass (e.g. pressure washer): stop timing it, leave it alone
        msg(("manual", 0, f"Manual run on {lbl(rv)} handed over to its bypass at {hm}; it is left alone."))
        to_gap_or_idle()
    elif rv >= 0 and not on[rv] and plan.open != rv:
        vs = sn.valves[rv]
        if vs == V_OFF:
            if st.mode == WATERING:
                if startup:
                    # restart / reload: the run part ended while we were away (e.g. valve timer)
                    if cfg.cycles[st.cycle].soak[rv] and st.remaining[rv] > 0:
                        st.ready[rv] = now + cfg.soak_min * 60
                else:
                    st.remaining[rv] = 0
                    msg(("cycle", st.cycle + 1, f"{lbl(rv)} was turned off during cycle {st.cycle + 1}, "
                                                "so it is skipped."))
            elif st.mode == MANUAL:
                if not startup:
                    msg(("manual", 0, f"Manual run on {lbl(rv)} finished at {hm} (turned off)."
                         + (f" Cycle {st.cycle + 1} resumes." if st.cycle >= 0 else "")))
            elif st.mode == STUCK:
                msg(("tap", rv + 1, f"{lbl(rv)} is now confirmed off."
                     + (f" Cycle {st.cycle + 1} resumes." if st.cycle >= 0 else "")))
            to_gap_or_idle()
        elif now >= st.end - 1:
            # offline when it should stop: block everything, look again every 5 min
            if st.mode != STUCK:
                msg(("tap", rv + 1, f"{lbl(rv)} is offline and was due to stop at {_hm(st.end, tz)}. "
                                    "Cannot confirm it is off, so nothing else will open. Checking every 5 min."))
            st.mode, st.end = STUCK, now + STUCK_RETRY_S
    elif rv >= 0 and _people_on(cfg, sn, rv) == 1 and (
        st.mode == WATERING or (st.mode == MANUAL and cfg.people_manual)
    ):
        close(rv)
        who = sn.people_who[rv] if rv < len(sn.people_who) else ""
        if st.mode == WATERING:
            give_back(rv)
            st.ready[rv] = now + clear_s
            msg(("people", rv + 1, f"{lbl(rv)} paused at {hm}: person detected ({who}). It carries on when "
                                   f"the area has been clear for {_g(clear_s / 60)} min; other taps run meanwhile."))
        else:
            msg(("people", rv + 1, f"Manual run on {lbl(rv)} stopped at {hm}: person detected ({who})."))
        to_gap_or_idle()
    elif rv >= 0 and now >= st.end - 1:
        # end of a run / burst / manual timer, or time to retry a stuck valve
        close(rv)
        if st.mode == STUCK:
            st.end = now + STUCK_RETRY_S  # the executor confirms; on_close_failed keeps it stuck
        else:
            if st.mode == WATERING and cfg.cycles[st.cycle].soak[rv] and st.remaining[rv] > 0:
                st.ready[rv] = now + cfg.soak_min * 60
            if st.mode == MANUAL:
                msg(("manual", 0, f"Manual run on {lbl(rv)} finished at {hm} (timer)."
                     + (f" Cycle {st.cycle + 1} resumes." if st.cycle >= 0 else "")))
            to_gap_or_idle()
    elif rv >= 0 and st.mode in (WATERING, MANUAL) and st.end - now > maxrt + 60:
        st.end = now + maxrt  # the safety limit was lowered while running

    # bypass: hold the cycle while any tap's bypass is on (the washer gets full pressure)
    if held and st.mode == WATERING:
        close(st.tap)
        give_back(st.tap)
        to_gap_or_idle()
    if held and st.cycle >= 0 and not st.held_note:
        st.held_note = True
        msg(("cycle", st.cycle + 1, f"Cycle {st.cycle + 1} paused at {hm}: bypass is on for "
                                    f"{', '.join(lbl(i) for i in held_taps)}. It carries on when the bypass is off."))
    if held and st.cycle < 0 and st.pending and not st.held_note:
        st.held_note = True
        p = st.pending[0]
        msg(("cycle", p["cycle"] + 1, f"Cycle {p['cycle'] + 1} is waiting: bypass is on for "
                                      f"{', '.join(lbl(i) for i in held_taps)}."))
    if not held and st.held_note:
        st.held_note = False
        if st.cycle >= 0:
            msg(("cycle", st.cycle + 1, f"Bypass off at {hm}: cycle {st.cycle + 1} carries on."))

    # ---------- 5 + 6. start pending cycles, pick the next tap ----------
    for _ in range(C + 3):
        if held:
            break
        if st.mode == IDLE and st.cycle < 0 and st.pending:
            _start_next_pending(st, cfg, sn, plan)
            continue  # a skipped cycle lets the next pending one try
        if st.mode == GAP and st.cycle >= 0 and plan.close and st.end < now + gap:
            st.end = now + gap
        if st.mode == GAP and st.cycle >= 0 and now >= st.end and not plan.close and plan.open is None:
            if _pick_next(st, cfg, sn, plan):
                continue  # the cycle finished; another pending cycle may start
        break

    # ---------- 7. reminders, alerts, restart summary ----------
    _reminders(st, cfg, sn, plan, occ_next)
    _alerts(st, cfg, sn, plan, occ_next)
    if startup and (loaded[0] != IDLE or loaded[4] > 0):
        _recovery(st, cfg, sn, plan, loaded)

    plan.next_wake = next_wake(st, cfg, sn, occ_next)
    return plan


def _start_next_pending(st: State, cfg: Cfg, sn: Snapshot, plan: Plan) -> None:
    """Pop the first pending cycle: run the weather / seasonal / soil checks, start or skip it."""
    msg = plan.msgs.append
    tz, now = sn.tz, sn.now
    T = len(cfg.taps)
    p = st.pending.pop(0)
    ci, forced = p["cycle"], p["forced"]
    due = p["due"] * 60
    late = now - due
    c = cfg.cycles[ci]
    lbl = cfg.label
    maxrt = max(cfg.max_run_min * 60, 60)

    # rain & weather skip: collect every reason (an empty setting is off)
    why: list[str] = []
    if cfg.wx_on and not forced:
        if cfg.rain_sensor and (sn.rain_state == "on" or (sn.rain_value is not None and sn.rain_value >= cfg.rain_mm)):
            why.append("rain sensor " + ("reports rain" if sn.rain_state == "on" else f"at {_g(sn.rain_value or 0)} mm"))
        if cfg.weather_entity and sn.weather_state in WET_WEATHER:
            why.append("weather is " + sn.weather_state.replace("-", " "))
        f0 = sn.forecast or {}
        prob = f0.get("precipitation_probability")
        if cfg.fc_prob > 0 and isinstance(prob, (int, float)) and prob >= cfg.fc_prob:
            why.append(f"forecast {round(prob)}% chance of rain")
        amt = f0.get("precipitation")
        if cfg.fc_mm > 0 and isinstance(amt, (int, float)) and amt >= cfg.fc_mm:
            why.append(f"forecast {_g(amt)} mm of rain")
        if cfg.wind_kmh > 0 and sn.wind is not None and sn.wind >= cfg.wind_kmh:
            why.append(f"wind {_g(round(sn.wind, 1))} km/h")
        if cfg.cold_on and sn.temp is not None and sn.temp <= cfg.cold_c:
            why.append(f"only {_g(round(sn.temp, 1))} °C")

    if cfg.seasonal_entity:
        if sn.seasonal_ext is None:
            msg(("config", 0, "Seasonal adjustment is unavailable; using 100%."))
            pv = 100.0
        else:
            pv = sn.seasonal_ext
    else:
        pv = st.seasonal
    pct = min(max(pv, 0.0), 200.0)
    if forced and pct <= 0:
        pct = 100.0
    is_hot = sn.temp is not None and cfg.hot_c > 0 and sn.temp >= cfg.hot_c
    boosted = is_hot and cfg.boost_pct > 100 and pct > 0
    if boosted:
        pct = min(pct * cfg.boost_pct / 100, 250.0)

    rem: list[int] = []
    had = False
    soil_wet: list[str] = []
    soil_bad: list[str] = []
    over: list[str] = []
    for t in range(T):
        d = c.minutes[t] if cfg.taps[t].valve else 0
        s = int(round(d * 60 * pct / 100))
        if d > 0 and 0 < s < 60:
            s = 60
        if d > 0:
            had = True
        tc = cfg.taps[t]
        if s > 0 and cfg.wx_on and not forced and tc.soil and tc.soil_pct > 0:
            if t < len(sn.soil_bad) and sn.soil_bad[t]:
                soil_bad.append(lbl(t))
            elif t < len(sn.soil) and sn.soil[t] is not None and sn.soil[t] >= tc.soil_pct:
                soil_wet.append(f"{lbl(t)} (soil {_g(round(sn.soil[t], 1))}%, wet enough at {_g(tc.soil_pct)}%)")
                s = 0
        if s > maxrt and not c.soak[t]:
            over.append(lbl(t))
        rem.append(s)
    total = sum(rem)
    due_txt = _hm(due, tz)

    if soil_bad:
        msg(("config", 0, f"Cycle {ci + 1}: soil sensor unavailable for {', '.join(soil_bad)}; watering as normal."))
    if why and had:
        msg(("skip", ci + 1, f"Cycle {ci + 1} (due {due_txt}) skipped: {', '.join(why)}."))
        return
    if pct <= 0 and had:
        msg(("skip", ci + 1, f"Cycle {ci + 1} skipped: seasonal adjustment is 0%."))
        return
    if had and total == 0 and soil_wet:
        msg(("skip", ci + 1, f"Cycle {ci + 1} (due {due_txt}) skipped: soil wet enough for every tap: "
                             f"{', '.join(soil_wet)}."))
        return
    if total == 0:
        return  # no tap has minutes in this cycle: nothing to do, stay quiet

    st.cycle, st.forced, st.t0 = ci, forced, now
    st.remaining = rem
    st.ready = [0.0] * T
    st.people_wait = 0.0
    st.mode, st.tap = GAP, -1
    st.end = now + max(cfg.gap_s, 0) if plan.close else now
    info = (f" ({int(round(pct))}%"
            + (f", hot-day boost at {_g(round(sn.temp or 0, 1))} °C" if boosted else "")
            + f", {int(round(total / 60))} min of watering)")
    if late > LATE_NOTE_S:
        msg(("cycle", ci + 1, ("Running late: " if late > cfg.late_min * 60 else "")
             + f"Cycle {ci + 1} started at {_hm(now, tz)}, {int(late // 60)} min late (due {due_txt}){info}."))
    elif cfg.n_start_finish:
        msg(("cycle", ci + 1, f"Cycle {ci + 1} {'started (run now)' if forced else 'started'} at {_hm(now, tz)}{info}."))
    seen: dict[int, list[int]] = {}
    for t in range(T):
        if rem[t] > 0:
            seen.setdefault(c.order[t], []).append(t)
    dup = sorted(k for k, v in seen.items() if len(v) > 1)
    if dup:
        msg(("config", 0, f"Cycle {ci + 1}: more than one tap has run order {', '.join(str(d) for d in dup)}; "
                          "those taps run in tap-number order."))
    if soil_wet:
        msg(("skip", ci + 1, f"Skipped {', '.join(soil_wet)}."))
    if over:
        msg(("config", 0, f"Cycle {ci + 1}: over the {maxrt // 60} min safety limit, so it runs in parts: "
                          f"{', '.join(over)}. Consider cycle-and-soak or a lower %."))


def _pick_next(st: State, cfg: Cfg, sn: Snapshot, plan: Plan) -> bool:
    """Open the next tap, wait, or finish the cycle. Returns True when the cycle finished."""
    msg = plan.msgs.append
    tz, now = sn.tz, sn.now
    T = len(cfg.taps)
    ci = st.cycle
    c = cfg.cycles[ci]
    lbl = cfg.label
    maxrt = max(cfg.max_run_min * 60, 60)
    clear_s = max(cfg.people_clear_min * 60, 0)
    best, best_key = -1, None
    blocked: list[int] = []
    for t in range(T):
        if st.remaining[t] <= 0:
            continue
        if not cfg.taps[t].valve or sn.valves[t] == V_NONE:
            st.remaining[t] = 0
        elif sn.valves[t] == V_OFFLINE:
            st.remaining[t] = 0
            msg(("tap", t + 1, f"{lbl(t)} is offline; it is skipped in cycle {ci + 1}."))
        elif st.ready[t] <= now and _people_on(cfg, sn, t) > 0:
            blocked.append(t)
        elif st.ready[t] <= now:
            pos = c.order[t] if 1 <= c.order[t] <= 99 else 99
            key = (pos, t)
            if best_key is None or key < best_key:
                best, best_key = t, key

    if best >= 0:
        # before opening, every other tap must report off
        bad = [t for t in range(T)
               if t != best and cfg.taps[t].valve and sn.valves[t] not in (V_OFF, V_NONE) and not _bypassed(sn, t)]
        if bad:
            msg(("tap", bad[0] + 1, f"Cycle {ci + 1} stopped: cannot confirm {lbl(bad[0])} is off (offline?)."))
            _reset_cycle(st, T)
            st.mode, st.tap, st.end = IDLE, -1, 0.0
            return True
        run = min(st.remaining[best], cfg.burst_min * 60) if c.soak[best] else st.remaining[best]
        run = min(run, maxrt)
        st.remaining[best] -= run
        st.people_wait = 0.0
        st.mode, st.tap, st.end = WATERING, best, now + run
        plan.open = best
        return False

    if max(st.remaining, default=0) > 0:
        waits = [st.ready[t] for t in range(T) if st.remaining[t] > 0 and t not in blocked]
        if blocked and not waits:
            # only people-blocked taps are left: wait (look again every 30 s), give up after the max wait
            names = ", ".join(lbl(t) for t in blocked)
            if st.people_wait == 0:
                st.people_wait = now
                mw = cfg.people_wait_min
                msg(("people", blocked[0] + 1, f"Cycle {ci + 1} is waiting: people near {names}. It carries on "
                     f"when clear for {_g(clear_s / 60)} min" + (f", or skips them after {mw} min." if mw > 0 else ".")))
            if cfg.people_wait_min > 0 and now - st.people_wait >= cfg.people_wait_min * 60:
                for t in blocked:
                    st.remaining[t] = 0
                msg(("people", blocked[0] + 1, f"Cycle {ci + 1}: skipped {names} (people there for "
                                               f"{cfg.people_wait_min} min)."))
                st.people_wait = 0.0
                st.end = now
                return _pick_next(st, cfg, sn, plan)
            clear_at = [sn.people_clear_at[t] for t in blocked
                        if t < len(sn.people_clear_at) and sn.people_clear_at[t] > now]
            st.end = min([now + PEOPLE_POLL_S, *clear_at])
        else:
            st.end = max(min(waits), now + 1)
            if blocked:
                st.end = min(st.end, now + PEOPLE_POLL_S)
        return False

    if cfg.n_start_finish:
        msg(("cycle", ci + 1, f"Cycle {ci + 1} finished at {_hm(now, tz)}."))
    _reset_cycle(st, T)
    st.mode, st.tap, st.end = IDLE, -1, 0.0
    return True


def _reminders(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, occ_next: list[float]) -> None:
    """Master switch off: remind 20 and 10 min before a cycle that would run; clear them later."""
    now = sn.now
    if st.master:
        for k in list(st.reminders):
            plan.clear.append(f"reminder_{int(k) + 1}")
        st.reminders = {}
        return
    for k, (occ, _lvl) in list(st.reminders.items()):
        if now >= occ:
            plan.clear.append(f"reminder_{int(k) + 1}")
            del st.reminders[k]
    for i, occ in enumerate(occ_next):
        if occ <= 0 or not st.cycle_on[i] or not cycle_has_water(cfg, i):
            continue
        d = occ - now
        sent = st.reminders.get(str(i))
        lvl_sent = sent[1] if sent and sent[0] == occ else 0
        level = 10 if d <= 630 else (20 if d <= 1230 else 0)
        if level and (lvl_sent == 0 or level < lvl_sent):
            st.reminders[str(i)] = [occ, float(level)]
            plan.msgs.append(("reminder", i + 1, f"Cycle {i + 1} starts at {_hm(occ, sn.tz)} "
                                                 f"(in {max(int(round(d / 60)), 1)} min) but the master switch is off."))


def _alerts(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, occ_next: list[float]) -> None:
    now = sn.now
    # too hot for 10 min: one alert (re-armed when it drops 1 °C below the limit)
    if cfg.hot_c > 0 and sn.temp is not None:
        if sn.temp >= cfg.hot_c:
            if st.hot_since == 0:
                st.hot_since = now
            if not st.hot_alerted and now - st.hot_since >= HOT_FOR_S:
                st.hot_alerted = True
                nxt = [(occ, i) for i, occ in enumerate(occ_next)
                       if occ > 0 and st.cycle_on[i] and cycle_has_water(cfg, i)]
                if not st.master:
                    tail = "The master switch is OFF, so nothing will water."
                elif nxt:
                    occ, i = min(nxt)
                    tail = f"Next: Cycle {i + 1} at {_dhm(occ, sn.tz)}."
                    if cfg.boost_pct > 100:
                        tail += f" Durations get the {cfg.boost_pct}% hot-day boost while it stays this hot."
                else:
                    tail = "No cycle is scheduled."
                plan.msgs.append(("hot", 0, f"It is {_g(round(sn.temp, 1))} °C (too hot at {_g(cfg.hot_c)} °C). {tail}"))
        else:
            st.hot_since = 0.0
            if sn.temp < cfg.hot_c - HOT_REARM_C:
                st.hot_alerted = False
    # started raining (debounced: re-armed after 30 min dry, the piezo flag flickers in real rain)
    if cfg.rain_sensor:
        wet = sn.rain_state == "on" or (sn.rain_value is not None and sn.rain_value >= cfg.rain_mm)
        if wet:
            st.rain_dry_since = 0.0
            if not st.rain_seen:
                st.rain_seen = True
                if st.master and cfg.n_rain:
                    shown = "on" if sn.rain_state == "on" else f"{_g(sn.rain_value or 0)} mm"
                    plan.msgs.append(("rain", 0, f"It has started raining (rain sensor: {shown}). Cycles due while "
                                      "it is wet are skipped." + (f" Cycle {st.cycle + 1} is running now; turn the "
                                      "master switch off to stop it." if st.cycle >= 0 else "")))
        elif st.rain_seen and sn.rain_state not in (None, "unavailable", "unknown"):
            if st.rain_dry_since == 0:
                st.rain_dry_since = now
            elif now - st.rain_dry_since >= RAIN_REARM_S:
                st.rain_seen = False
                st.rain_dry_since = 0.0


def _recovery(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, loaded: tuple) -> None:
    tz = sn.tz
    lbl = cfg.label
    mode0, tap0, end0, _cyc0, _np = loaded
    parts = []
    if mode0 in (WATERING, MANUAL) and 0 <= tap0 < len(cfg.taps) and tap0 in plan.close:
        parts.append(f"{lbl(tap0)} closed (was due off at {_hm(end0, tz)})")
    if st.mode == MANUAL:
        parts.append(f"manual run on {lbl(st.tap)} continues until {_hm(st.end, tz)}")
    elif st.mode == STUCK:
        parts.append(f"{lbl(st.tap)} still cannot be confirmed off")
    elif st.cycle >= 0:
        parts.append(f"cycle {st.cycle + 1} continues"
                     + (f" with {lbl(st.tap)} until {_hm(st.end, tz)}" if st.mode == WATERING else ""))
    elif st.pending:
        parts.append("waiting cycles: " + ", ".join(str(p["cycle"] + 1) for p in st.pending))
    else:
        parts.append("nothing left to run")
    kind = "restart" if "startup" in sn.reasons else "reload"
    plan.msgs.insert(0, ("recovery", 0, f"Recovered after {kind} at {_hm(sn.now, tz)}: {'; '.join(parts)}."))


def next_wake(st: State, cfg: Cfg, sn: Snapshot, occ_next: list[float] | None = None) -> float | None:
    """The next time reconcile must run (the 1-minute watchdog is the backstop)."""
    now = sn.now
    if occ_next is None:
        occ_next = [next_occurrence(c, now, sn.tz) for c in cfg.cycles]
    cands: list[float] = []
    if st.mode in (WATERING, MANUAL, STUCK, GAP):
        cands.append(st.end)
    if st.mode == IDLE and st.pending and not any(sn.bypass):
        cands.append(now + 1)
    cands += [o for o in occ_next if o > 0]
    if not st.master:
        for i, o in enumerate(occ_next):
            if o > 0 and st.cycle_on[i]:
                cands += [o - 1200, o - 600]
    if st.hot_since and not st.hot_alerted:
        cands.append(st.hot_since + HOT_FOR_S)
    cands += [t for t in sn.bypass_until if t]
    if st.cycle >= 0:
        cands += [t for t in sn.people_clear_at if t]
    future = [c for c in cands if c > now - 0.001]
    if not future:
        return None
    return max(min(future), now + 0.5)


# --------------------------------------------------------------------------
# Feedback from the executor
# --------------------------------------------------------------------------
def on_close_failed(st: State, cfg: Cfg, tap: int, now: float, tz: tzinfo) -> list[tuple[str, int, str]]:
    """A valve did not report off after 3 tries: stuck. Nothing else opens; retry every 5 min."""
    msgs = []
    if st.mode != STUCK or st.tap != tap:
        msgs.append(("tap", tap + 1, f"Could not confirm {cfg.label(tap)} is off."
                     + (f" Cycle {st.cycle + 1} is paused until it is." if st.cycle >= 0 else "")
                     + " Nothing else will open; retrying every 5 min."))
    st.mode, st.tap, st.end = STUCK, tap, now + STUCK_RETRY_S  # the cycle keeps its remaining time
    return msgs


def on_open_failed(st: State, cfg: Cfg, tap: int, now: float, tz: tzinfo) -> list[tuple[str, int, str]]:
    """A valve did not report on: it was closed again and is skipped for this cycle."""
    msgs = [("tap", tap + 1, f"{cfg.label(tap)} did not report on; "
             + ("it is skipped this cycle." if st.cycle >= 0 and st.mode == WATERING else "the manual run was cancelled."))]
    if st.mode == WATERING and st.tap == tap and 0 <= tap < len(st.remaining):
        st.remaining[tap] = 0
    st.tap = -1
    if st.cycle >= 0:
        st.mode, st.end = GAP, now + max(cfg.gap_s, 0) + 2
    else:
        st.mode, st.end = IDLE, 0.0
    return msgs


def after_close_confirmed(st: State, cfg: Cfg, now: float) -> None:
    """The gap counts from the moment the last tap reported off."""
    if st.mode == GAP:
        st.end = max(st.end, now + max(cfg.gap_s, 0))


# --------------------------------------------------------------------------
# Read-only views for entities and the card
# --------------------------------------------------------------------------
def status(st: State, cfg: Cfg, sn: Snapshot | None) -> str:
    if st.mode == STUCK:
        return "stuck"
    if st.mode == MANUAL:
        return "manual"
    if st.mode == WATERING:
        return "watering"
    held = bool(sn and any(sn.bypass))
    if st.mode == GAP and st.cycle >= 0:
        if held:
            return "held"
        if st.people_wait:
            return "waiting"
        now = sn.now if sn else 0
        if st.end - now > max(cfg.gap_s, 0) + 1:
            return "soaking"
        return "gap"
    if st.pending and held:
        return "held"
    return "idle"
