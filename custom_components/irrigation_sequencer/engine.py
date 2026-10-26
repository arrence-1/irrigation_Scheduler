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
from datetime import date, datetime, time as dtime, timedelta, tzinfo
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
AHEAD_S = 1200  # the offline question goes out 20 min before a cycle
BAT_REARM = 5  # a low battery counts as fine again 5 % above the warning level (no flip-flopping)
OFFLINE_ASK = ("offline", "offline_open", "offline_ahead")  # offline messages with buttons


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
    # main water (supply valve)
    stuck_since: float = 0.0  # when the current stuck episode began (0 = not stuck)
    main_alerted: bool = False  # the "tap won't close" main-water alert went out for this episode
    main_ask: bool = False  # the "Close main water?" alert is showing
    main_prompt: bool = False  # the "Open main water?" prompt is showing
    main_note: bool = False  # "held: main water off" was announced
    main_off_since: float = 0.0  # when the main water was first seen off (0 = on / not set)
    main_remind_at: float = 0.0  # next "the main water is off" reminder
    main_snooze_until: float = 0.0  # reminders snoozed until then
    main_reminded: bool = False  # a reminder is showing
    main_cycle_rem: dict[str, list[float]] = field(default_factory=dict)  # cycle -> [occurrence ts, level]
    # offline valves
    off_skip: list[bool] = field(default_factory=list)  # skipped while offline (until it is back online)
    off_seen: list[str] = field(default_factory=list)  # last state seen while online: "on" / "off" / "" (unknown)
    off_since: list[float] = field(default_factory=list)  # when it went offline (0 = online)
    off_wait: float = 0.0  # the running cycle waits for an answer since then (0 = not waiting)
    off_asked: list[int] = field(default_factory=list)  # taps in the offline question that is showing
    off_shown: bool = False  # an offline question (with buttons) is showing
    off_ahead: dict[str, float] = field(default_factory=dict)  # cycle -> occurrence asked about 20 min ahead
    # valve batteries
    bat_level: list[float] = field(default_factory=list)  # last known % per tap (-1 = unknown)
    bat_low: list[bool] = field(default_factory=list)
    bat_next: float = 0.0  # next low-battery warning (0 = none planned)
    bat_snooze_until: float = 0.0
    bat_snoozed: list[int] = field(default_factory=list)  # taps the snooze covers
    bat_last: list[int] = field(default_factory=list)  # taps in the last warning
    bat_shown: bool = False  # a low-battery warning is showing
    rep_anchor: str = ""  # date the battery report counts from when no start date is set
    rep_fp: str = ""  # report settings the schedule was worked out for
    rep_last: int = 0  # epoch minute of the last report occurrence handled
    # skip watering (you chose to skip: the next cycle, or everything until a time)
    skip_next: int = 0  # scheduled cycles still to skip
    skip_until: float = 0.0  # skip every scheduled cycle due before this (0 = off)
    fc_last: dict[str, Any] = field(default_factory=dict)  # last forecast check (for the status attribute)

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
            st.stuck_since = float(st.stuck_since)
            for k in ("main_alerted", "main_ask", "main_prompt", "main_note", "main_reminded"):
                setattr(st, k, bool(getattr(st, k)))
            for k in ("main_off_since", "main_remind_at", "main_snooze_until"):
                setattr(st, k, float(getattr(st, k)))
            st.main_cycle_rem = {str(k): [float(v[0]), float(v[1])] for k, v in dict(st.main_cycle_rem).items()}
            st.off_skip = [bool(x) for x in st.off_skip]
            st.off_seen = [str(x) if x in (V_ON, V_OFF) else "" for x in st.off_seen]
            st.off_since = [float(x) for x in st.off_since]
            st.off_wait = float(st.off_wait)
            st.off_asked = [int(x) for x in st.off_asked]
            st.off_shown = bool(st.off_shown)
            st.off_ahead = {str(k): float(v) for k, v in dict(st.off_ahead).items()}
            st.bat_level = [float(x) for x in st.bat_level]
            st.bat_low = [bool(x) for x in st.bat_low]
            st.bat_next, st.bat_snooze_until = float(st.bat_next), float(st.bat_snooze_until)
            st.bat_snoozed = [int(x) for x in st.bat_snoozed]
            st.bat_last = [int(x) for x in st.bat_last]
            st.bat_shown = bool(st.bat_shown)
            st.rep_anchor, st.rep_fp, st.rep_last = str(st.rep_anchor), str(st.rep_fp), int(st.rep_last)
            st.skip_next, st.skip_until = max(int(st.skip_next), 0), float(st.skip_until)
            st.fc_last = dict(st.fc_last)
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
    main: str = V_NONE  # main water valve: on / off / offline / none
    battery: list[float | None] = field(default_factory=list)  # % per tap (None = no sensor / unavailable)


@dataclass
class Plan:
    close: list[int] = field(default_factory=list)
    open: int | None = None
    msgs: list[tuple[str, int, str]] = field(default_factory=list)  # (kind, number, text)
    clear: list[str] = field(default_factory=list)  # tag suffixes, e.g. "reminder_1"
    next_wake: float | None = None
    main: str | None = None  # "close" / "open" the main water valve


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _hm(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _dhm(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M")


def _dur(s: float) -> str:
    """'45 min', '2 h 5 min', '1 day 3 h'."""
    m = max(int(s // 60), 0)
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")
    d = m // 1440
    return f"{d} day{'s' if d > 1 else ''}" + (f" {(m % 1440) // 60} h" if (m % 1440) // 60 else "")


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
    st.off_skip = _fit(st.off_skip, T, False)
    st.off_seen = _fit(st.off_seen, T, "")
    st.off_since = _fit(st.off_since, T, 0.0)
    st.off_asked = [i for i in st.off_asked if 0 <= i < T]
    st.off_ahead = {k: v for k, v in st.off_ahead.items() if k.isdigit() and int(k) < C}
    st.bat_level = _fit(st.bat_level, T, -1.0)
    st.bat_low = _fit(st.bat_low, T, False)
    st.bat_snoozed = [i for i in st.bat_snoozed if 0 <= i < T]
    st.bat_last = [i for i in st.bat_last if 0 <= i < T]
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
    st.main_cycle_rem = {k: v for k, v in st.main_cycle_rem.items() if k.isdigit() and int(k) < C}


def _reset_cycle(st: State, T: int) -> None:
    st.cycle = -1
    st.forced = False
    st.t0 = 0.0
    st.remaining = [0] * T
    st.ready = [0.0] * T
    st.people_wait = 0.0
    st.held_note = False
    st.off_wait = 0.0


def _people_on(cfg: Cfg, sn: Snapshot, i: int) -> int:
    if not cfg.people_on or i >= len(sn.people):
        return 0
    return sn.people[i]


def _bypassed(sn: Snapshot, i: int) -> bool:
    return i < len(sn.bypass) and sn.bypass[i]


def main_is_off(cfg: Cfg, sn: Snapshot | None) -> bool:
    """The main water valve is set and reports off (offline / unknown does not count)."""
    return bool(cfg.main_valve) and sn is not None and sn.main == V_OFF


def _offline(sn: Snapshot, i: int) -> bool:
    return i < len(sn.valves) and sn.valves[i] == V_OFFLINE


def can_skip_offline(st: State, i: int) -> bool:
    """An offline valve may be skipped only when it was not last seen open (a flat-battery valve stays put)."""
    return 0 <= i < len(st.off_seen) and st.off_seen[i] != V_ON and not (st.mode == STUCK and st.tap == i)


def offline_blockers(st: State, cfg: Cfg, sn: Snapshot) -> list[int]:
    """Offline valves that keep every tap shut: not skipped, not bypassed and not the stuck tap (handled apart)."""
    return [
        i for i in range(len(cfg.taps))
        if cfg.taps[i].valve and _offline(sn, i) and not (i < len(st.off_skip) and st.off_skip[i])
        and not _bypassed(sn, i) and not (st.mode == STUCK and st.tap == i)
    ]


def _names(cfg: Cfg, taps: list[int]) -> str:
    return ", ".join(cfg.label(t) for t in taps)


def _off_detail(st: State, cfg: Cfg, t: int, now: float, tz: tzinfo) -> str:
    """'Tap 2 (Backyard) has been offline since 05:40 (last seen closed; battery 12% then)'."""
    since = st.off_since[t] if t < len(st.off_since) else 0.0
    head = (f"{cfg.label(t)} has been offline since {_hm(since, tz) if now - since < 20 * 3600 else _dhm(since, tz)}"
            if since else f"{cfg.label(t)} is offline")
    bits = []
    seen = st.off_seen[t] if t < len(st.off_seen) else ""
    if seen == V_OFF:
        bits.append("last seen closed")
    elif seen == V_ON:
        bits.append("last seen OPEN")
    lvl = st.bat_level[t] if t < len(st.bat_level) else -1.0
    if lvl >= 0:
        bits.append(f"battery {_g(round(lvl))}% then")
    return head + (f" ({'; '.join(bits)})" if bits else "")


def _mins(hhmm: str) -> int:
    h, m = _parse_hm(hhmm)
    return h * 60 + m


def in_quiet(cfg: Cfg, now: float, tz: tzinfo) -> bool:
    """Inside the battery warnings' quiet time (e.g. 21:00-06:00, over midnight)."""
    if not cfg.quiet_on:
        return False
    f, t = _mins(cfg.quiet_from), _mins(cfg.quiet_to)
    if f == t:
        return False
    d = datetime.fromtimestamp(now, tz)
    m = d.hour * 60 + d.minute
    return f <= m < t if f < t else (m >= f or m < t)


def _quiet_end(cfg: Cfg, now: float, tz: tzinfo) -> float:
    h, m = _parse_hm(cfg.quiet_to)
    nd = datetime.fromtimestamp(now, tz)
    for k in range(3):
        dt = datetime.combine(nd.date() + timedelta(days=k), dtime(h, m), tzinfo=tz)
        if dt.timestamp() > now:
            return dt.timestamp()
    return now + 86400


def _rep_anchor(st: State, cfg: Cfg) -> date | None:
    try:
        return date.fromisoformat(cfg.rep_start or st.rep_anchor)
    except ValueError:
        return None


def report_times(st: State, cfg: Cfg, now: float, tz: tzinfo) -> tuple[float, float]:
    """Battery report: (last time at or before now, next time after now); 0 = none."""
    a = _rep_anchor(st, cfg)
    if not cfg.rep_on or a is None:
        return 0.0, 0.0
    h, m = _parse_hm(cfg.rep_time)
    days = max(cfg.rep_days, 1)
    k = max((datetime.fromtimestamp(now, tz).date() - a).days // days, 0)
    last = nxt = 0.0
    for kk in (k - 1, k, k + 1):
        if kk < 0:
            continue
        o = datetime.combine(a + timedelta(days=kk * days), dtime(h, m), tzinfo=tz).timestamp()
        if o <= now:
            last = max(last, o)
        elif not nxt:
            nxt = o
    return last, nxt


def _ddhm(ts: float, tz: tzinfo) -> str:
    """'Wed 4 Nov 10:00'."""
    d = datetime.fromtimestamp(ts, tz)
    return f"{d:%a} {d.day} {d:%b %H:%M}"


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

    # ---------- 1. offline valves: the last state seen, back online ----------
    for i in range(T):
        v = sn.valves[i]
        if v == V_NONE:
            st.off_skip[i], st.off_seen[i], st.off_since[i] = False, "", 0.0
            continue
        if v == V_OFFLINE:
            if not st.off_since[i]:
                st.off_since[i] = now
            continue
        was_offline = st.off_since[i] > 0
        st.off_since[i] = 0.0
        st.off_seen[i] = v
        if st.off_skip[i]:
            st.off_skip[i] = False
            if v == V_ON and not _bypassed(sn, i):
                close(i)  # it was skipped as closed: it must not run unannounced
                msg(("offline_note", i + 1, f"{lbl(i)} is back online but reports open, so it was closed. "
                                            "It is in the cycles again."))
            else:
                msg(("offline_note", i + 1, f"{lbl(i)} is back online: it is in the cycles again."))
        elif was_offline and i in st.off_asked:
            msg(("offline_note", i + 1, f"{lbl(i)} is back online."))

    if st.skip_until and now >= st.skip_until - 1:
        st.skip_until = 0.0  # the skip time is over: cycles run as scheduled again

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
                if now < st.skip_until - 1 or st.skip_next > 0:
                    # you chose to skip (e.g. from a rain alert)
                    until = st.skip_until > now
                    if not until:
                        st.skip_next -= 1
                    msg(("skip", i + 1, f"Cycle {i + 1} (due {_hm(lm * 60, tz)}) skipped: you chose to skip "
                         + (f"watering until {_ddhm(st.skip_until, tz)}." if until else "this cycle.")))
                elif st.cycle == i or any(p["cycle"] == i for p in st.pending):
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
    report_now = False
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
        elif kind == "snooze_main" and cfg.main_valve:
            hours = max(float(req.get("hours", 0) or 0), 0.0)
            if sn.main != V_OFF:
                msg(("main", 0, "The main water is on: there is nothing to snooze."))
            elif hours <= 0:
                st.main_snooze_until = 0.0
                st.main_remind_at = now
                msg(("main", 0, "Main water reminders are back on."))
            else:
                st.main_snooze_until = now + hours * 3600
                st.main_remind_at = max(st.main_remind_at, st.main_snooze_until)
                st.main_reminded = False
                msg(("main", 0, f"Main water reminders snoozed for {_g(round(hours, 2))} h, until "
                                f"{_dhm(st.main_snooze_until, tz)}. The main water is still off."))
        elif kind in ("close_main", "open_main") and cfg.main_valve:
            want_off = kind == "close_main"
            if sn.main == (V_OFF if want_off else V_ON):
                msg(("main", 0, f"The main water is already {'off' if want_off else 'on'}."))
            else:
                plan.main = "close" if want_off else "open"
        elif kind == "skip_offline":
            _skip_offline(st, cfg, sn, plan, int(req.get("tap", -1)), bool(req.get("skip", True)))
        elif kind == "stop_cycle":
            if st.cycle >= 0:
                if st.mode == WATERING:
                    close(st.tap)
                    give_back(st.tap)
                msg(("offline_note" if st.off_wait else "cycle", st.cycle + 1,
                     f"Cycle {st.cycle + 1} stopped at {hm} (requested)."))
                _reset_cycle(st, T)
                if st.mode in (WATERING, GAP):
                    st.mode, st.tap, st.end = IDLE, -1, 0.0
            else:
                msg(("offline_note", 0, "No cycle is running: there is nothing to stop."))
        elif kind == "snooze_battery":
            _snooze_battery(st, cfg, sn, plan, max(float(req.get("hours", 0) or 0), 0.0))
        elif kind == "battery_report":
            report_now = True
        elif kind == "skip_watering":
            _skip_watering(st, cfg, sn, plan, req, occ_next, close)

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
                main_off = main_is_off(cfg, sn) or plan.main == "close"
                msg(("tap", rv + 1, f"{lbl(rv)} is now confirmed off."
                     + (f" Cycle {st.cycle + 1} resumes." if st.cycle >= 0 and not main_off else "")
                     + (f" Cycle {st.cycle + 1} waits for the main water." if st.cycle >= 0 and main_off else "")))
                if main_off:
                    st.main_ask, st.main_prompt = False, True
                    msg(("main_open", 0, f"{lbl(rv)} is confirmed off, but the main water is still off. Open it again "
                                         "when you're ready: press and hold this notification."))
                elif st.main_ask:
                    st.main_ask = False
                    plan.clear.append("main")  # the "Close main water?" alert is no longer needed
                st.stuck_since, st.main_alerted = 0.0, False
            to_gap_or_idle()
        elif now >= st.end - 1:
            # offline when it should stop: block everything, look again every 5 min
            if st.mode != STUCK:
                msg(("tap", rv + 1, f"{lbl(rv)} is offline and was due to stop at {_hm(st.end, tz)}. "
                                    "Cannot confirm it is off, so nothing else will open. Checking every 5 min."))
                st.stuck_since, st.main_alerted = now, False
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

    # main water: a tap that can't be confirmed off for the set time -> critical alert / close the main water
    if st.mode == STUCK and 0 <= st.tap < T:
        if not st.stuck_since:
            st.stuck_since = now  # stuck since before this version
        waited = now - st.stuck_since
        if cfg.main_valve and not st.main_alerted and waited >= cfg.main_wait_min * 60:
            st.main_alerted = True
            mins = max(int(round(waited / 60)), 1)
            what = f"{lbl(st.tap)} still can't be confirmed off after {mins} min"
            if main_is_off(cfg, sn) or plan.main == "close":
                msg(("main", 0, f"{what}. The main water is already off."))
            elif cfg.main_auto_close and sn.main == V_ON:
                plan.main = "close"
                msg(("main_auto", 0, f"{what}, so the main water is being closed (automatic)."))
            else:
                st.main_ask = True
                msg(("main_alert", 0, f"{what} and may still be running. Press and hold this notification to close "
                                      "the main water."
                     + (" The main water valve is offline." if sn.main == V_OFFLINE else "")))
    elif st.stuck_since:
        st.stuck_since, st.main_alerted = 0.0, False

    # main water off: hold cycles (a running tap closes and keeps its time) until it is on again
    main_off = main_is_off(cfg, sn) or plan.main == "close"
    if main_off and st.mode == WATERING:
        close(st.tap)
        give_back(st.tap)
        to_gap_or_idle()
    if main_off and not st.main_note and (st.cycle >= 0 or st.pending):
        st.main_note = True
        ci = st.cycle if st.cycle >= 0 else st.pending[0]["cycle"]
        msg(("cycle", ci + 1, (f"Cycle {ci + 1} paused at {hm}" if st.cycle >= 0 else f"Cycle {ci + 1} is waiting")
             + ": the main water is off. It carries on when the main water is on again."))
    if not main_off and sn.main == V_ON:
        if st.main_note:
            st.main_note = False
            if st.cycle >= 0 or st.pending:
                ci = st.cycle if st.cycle >= 0 else st.pending[0]["cycle"]
                msg(("cycle", ci + 1, f"Main water on at {hm}: cycle {ci + 1} carries on."))
        if st.main_prompt:
            st.main_prompt = False
            plan.clear.append("main")
        if st.main_ask and st.mode != STUCK:
            st.main_ask = False
            plan.clear.append("main")
    if main_off and st.main_ask and plan.main != "close":
        st.main_ask = False  # closed by hand (or elsewhere): the question is answered
        msg(("main", 0, f"The main water is off ({hm})."
             + (f" {lbl(st.tap)} still can't be confirmed off; it is checked every 5 min." if st.mode == STUCK else "")))

    # main water off: remind every remind_min minutes (unless snoozed); forget it all once it is on again
    remind_s = max(cfg.main_remind_min, 0) * 60
    if cfg.main_valve and sn.main == V_OFF:
        if not st.main_off_since:
            st.main_off_since = now
            st.main_remind_at = now + remind_s if remind_s else 0.0
        if remind_s and not st.main_remind_at:
            st.main_remind_at = now + remind_s  # reminders were just switched on
        if remind_s and now >= st.main_remind_at - 1 and now >= st.main_snooze_until - 1:
            st.main_reminded = True
            st.main_remind_at = now + remind_s
            waiting = ([st.cycle] if st.cycle >= 0 else []) + [p["cycle"] for p in st.pending]
            stuck_txt = f" {lbl(st.tap)} still can't be confirmed off." if st.mode == STUCK else ""
            msg(("main_remind_stuck" if st.mode == STUCK else "main_remind", 0,
                 f"The main water has been off since {_dhm(st.main_off_since, tz)} ({_dur(now - st.main_off_since)})."
                 + (f" Waiting: cycle {', '.join(str(c + 1) for c in waiting)}." if waiting else "")
                 + stuck_txt + " Press and hold to " + ("snooze" if st.mode == STUCK else "open it or snooze")
                 + " these reminders."))
    elif sn.main == V_ON and st.main_off_since:
        st.main_off_since = st.main_remind_at = st.main_snooze_until = 0.0
        if st.main_reminded:
            st.main_reminded = False
            if "main" not in plan.clear:
                plan.clear.append("main")
    elif not cfg.main_valve:
        st.main_off_since = st.main_remind_at = st.main_snooze_until = 0.0
        st.main_reminded = False

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
        if held or main_off:
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
    _main_cycle_reminders(st, cfg, sn, plan, occ_next)
    _offline_ahead(st, cfg, sn, plan, occ_next)
    _offline_tidy(st, cfg, sn, plan)
    _battery(st, cfg, sn, plan)
    _battery_report(st, cfg, sn, plan, report_now)
    _alerts(st, cfg, sn, plan, occ_next)
    if startup and (loaded[0] != IDLE or loaded[4] > 0):
        _recovery(st, cfg, sn, plan, loaded)

    plan.next_wake = next_wake(st, cfg, sn, occ_next)
    return plan


def _forecast_check(st: State, cfg: Cfg, sn: Snapshot, ci: int) -> tuple[list[str], str]:
    """Today's forecast against the limits. Returns (skip reasons, a note when only one limit was met).

    With both limits set and "both" on, a cycle is skipped only when the chance AND the amount are met;
    otherwise each limit skips on its own. The result is kept in st.fc_last (status attribute).
    """
    p_on, a_on = cfg.fc_prob > 0, cfg.fc_mm > 0
    if not (p_on or a_on) or not cfg.weather_entity:
        return [], ""
    f0 = sn.forecast or {}
    prob = f0.get("precipitation_probability")
    amt = f0.get("precipitation")
    prob = prob if isinstance(prob, (int, float)) else None
    amt = amt if isinstance(amt, (int, float)) else None
    p_hit = p_on and prob is not None and prob >= cfg.fc_prob
    a_hit = a_on and amt is not None and amt >= cfg.fc_mm
    both = cfg.fc_both and p_on and a_on
    shown = (f"{round(prob)}% chance" if prob is not None else "no chance given") + " of " \
        + (f"{_g(amt)} mm" if amt is not None else "an unknown amount")
    why: list[str] = []
    note = ""
    if not f0:
        result = "no forecast"
    elif both:
        if p_hit and a_hit:
            why.append(f"forecast {shown}")
        elif p_hit or a_hit:
            note = (f"Forecast {shown}: not skipped, both {cfg.fc_prob}% and {_g(cfg.fc_mm)} mm are needed "
                    f"(only the {'chance' if p_hit else 'amount'} was met).")
        result = "skipped" if why else "not skipped"
    else:
        if p_hit:
            why.append(f"forecast {round(prob)}% chance of rain")
        if a_hit:
            why.append(f"forecast {_g(amt)} mm of rain")
        result = "skipped" if why else "not skipped"
    st.fc_last = {"at": sn.now, "cycle": ci + 1, "chance": prob, "amount": amt, "result": result,
                  "rule": "both" if both else "either"}
    return why, note


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
    fc_note = ""
    if cfg.wx_on and not forced:
        if cfg.rain_sensor and (sn.rain_state == "on" or (sn.rain_value is not None and sn.rain_value >= cfg.rain_mm)):
            why.append("rain sensor " + ("reports rain" if sn.rain_state == "on" else f"at {_g(sn.rain_value or 0)} mm"))
        if cfg.weather_entity and sn.weather_state in WET_WEATHER:
            why.append("weather is " + sn.weather_state.replace("-", " "))
        fc_why, fc_note = _forecast_check(st, cfg, sn, ci)
        why += fc_why
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
    off_skipped: list[str] = []
    for t in range(T):
        d = c.minutes[t] if cfg.taps[t].valve else 0
        if d > 0 and st.off_skip[t] and _offline(sn, t):
            off_skipped.append(lbl(t))  # skipped while offline
            d = 0
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
    if total == 0 and off_skipped:
        msg(("skip", ci + 1, f"Cycle {ci + 1} (due {due_txt}) skipped: its taps are offline and skipped while "
                             f"offline ({', '.join(off_skipped)})."))
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
    if fc_note:  # the forecast met one limit but not both: say so, so the "both" rule can be checked
        msg(("cycle", ci + 1, fc_note))
    if off_skipped:
        msg(("skip", ci + 1, f"Skipped while offline: {', '.join(off_skipped)}."))
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

    # an offline valve can't be confirmed closed: nothing opens until it is back, skipped, or the wait is over
    waiting = offline_blockers(st, cfg, sn)
    if waiting:
        return _offline_hold(st, cfg, sn, plan, waiting)
    if st.off_wait:
        st.off_wait = 0.0
        msg(("offline_note", ci + 1, f"Cycle {ci + 1} carries on at {_hm(now, tz)}."))

    best, best_key = -1, None
    blocked: list[int] = []
    for t in range(T):
        if st.remaining[t] <= 0:
            continue
        if not cfg.taps[t].valve or sn.valves[t] == V_NONE:
            st.remaining[t] = 0
        elif sn.valves[t] == V_OFFLINE:  # only taps skipped while offline get here
            st.remaining[t] = 0
            msg(("skip", ci + 1, f"{lbl(t)} is offline: skipped in cycle {ci + 1} (skipped while offline)."))
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
               if t != best and cfg.taps[t].valve and sn.valves[t] not in (V_OFF, V_NONE) and not _bypassed(sn, t)
               and not (st.off_skip[t] and sn.valves[t] == V_OFFLINE)]
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


def _skip_watering(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, req: dict[str, Any], occ_next: list[float],
                   close: Any) -> None:
    """You chose to skip watering (e.g. a rain alert's button): the next cycle, for some hours, or run again.

    req: {"next": True} = skip the next cycle (the running or waiting one if there is one),
    {"hours": h} = skip every scheduled cycle due in the next h hours (and stop the running one),
    {"hours": 0} = cancel any skip. "Run now" cycles are never skipped.
    """
    msg = plan.msgs.append
    now, tz = sn.now, sn.tz
    T = len(cfg.taps)
    hm = _hm(now, tz)
    hours = max(float(req.get("hours", 0) or 0), 0.0)
    nxt = bool(req.get("next"))
    if not nxt and hours <= 0:  # run as scheduled again
        was = st.skip_next > 0 or st.skip_until > now
        st.skip_next, st.skip_until = 0, 0.0
        msg(("skip_note", 0, "Skipping cancelled: cycles run as scheduled again." if was
             else "Nothing is being skipped: cycles run as scheduled."))
        return
    stopped = []
    if st.cycle >= 0 and not st.forced:
        if st.mode == WATERING:
            close(st.tap)
        stopped.append(st.cycle)
        _reset_cycle(st, T)
        if st.mode in (WATERING, GAP):
            st.mode, st.tap, st.end = IDLE, -1, 0.0
    keep = []
    for p in st.pending:
        if p["forced"]:
            keep.append(p)
        else:
            stopped.append(p["cycle"])
    st.pending = keep
    done = f"Cycle {', '.join(str(c + 1) for c in stopped)} stopped at {hm}. " if stopped else ""
    if hours > 0:
        st.skip_until, st.skip_next = now + hours * 3600, 0
        msg(("skip_note", 0, f"{done}Watering skipped for {_dur(hours * 3600)}, until {_ddhm(st.skip_until, tz)}: "
                             "cycles due before then are skipped."
             + ("" if st.master else " (The master switch is off, so nothing runs anyway.)")))
        return
    if stopped:
        msg(("skip_note", 0, f"{done}It counts as the skipped cycle; the next ones run as scheduled."))
        return
    st.skip_next = 1
    cands = [(o, i) for i, o in enumerate(occ_next) if o > 0 and st.cycle_on[i] and cycle_has_water(cfg, i)]
    if cands:
        o, i = min(cands)
        what = f"Cycle {i + 1}, {_ddhm(o, tz)}"
    else:
        what = "none is scheduled yet"
    msg(("skip_note", 0, f"The next cycle ({what}) will be skipped."
         + ("" if st.master else " (The master switch is off, so nothing runs anyway.)")))


def _offline_hold(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, waiting: list[int]) -> bool:
    """A valve is offline: the cycle waits and asks; after the wait it stops or skips it (setting).

    Returns True when the cycle stopped (like _pick_next).
    """
    msg = plan.msgs.append
    now, tz = sn.now, sn.tz
    ci = st.cycle
    n = len(waiting)
    names = _names(cfg, waiting)
    can = [t for t in waiting if can_skip_offline(st, t)]
    first = not st.off_wait
    if first:
        st.off_wait = now
    deadline = st.off_wait + max(cfg.off_wait_min, 1) * 60
    if now >= deadline - 1:
        if cfg.off_no_answer == "skip" and len(can) == n:
            for t in waiting:
                st.off_skip[t] = True
                st.remaining[t] = 0
            st.off_wait = 0.0
            msg(("offline_note", ci + 1, f"No answer after {cfg.off_wait_min} min: {names} "
                                         f"{_isare(n)} skipped while offline. Cycle {ci + 1} carries on at {_hm(now, tz)}."
                                         + " Included again when back online."))
            st.end = now
            return _pick_next(st, cfg, sn, plan)
        msg(("offline_note", ci + 1, f"Cycle {ci + 1} stopped at {_hm(now, tz)}: {names} {_isare(n)} still offline "
                                     f"and there was no answer after {cfg.off_wait_min} min."
             + (" It was last seen open, so it is not skipped." if cfg.off_no_answer == "skip" and len(can) < n else "")))
        _reset_cycle(st, len(cfg.taps))
        st.mode, st.tap, st.end = IDLE, -1, 0.0
        return True
    if first or sorted(waiting) != sorted(st.off_asked) or not st.off_shown:
        st.off_asked = list(waiting)
        detail = "; ".join(_off_detail(st, cfg, t, now, tz) for t in waiting)
        then = ("stops" if cfg.off_no_answer == "stop" or len(can) < n
                else f"skips {'it' if n == 1 else 'them'} and carries on")
        if can:
            ask = (f" Press and hold: skip {'it' if n == 1 else 'them'} while offline (included again when back "
                   "online), or stop the cycle.")
        else:
            ask = (" It was last seen open and may still be running, so it can't be skipped: check it"
                   + (", or close the main water." if cfg.main_valve else "."))
        msg(("offline" if can else "offline_open", ci + 1,
             f"Cycle {ci + 1} is waiting: {detail}. No tap opens while a valve can't be confirmed closed.{ask}"
             f" With no answer by {_hm(deadline, tz)} the cycle {then}."))
    return False


def _isare(n: int) -> str:
    return "is" if n == 1 else "are"


def _skip_offline(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, tap: int, skip: bool) -> None:
    """Skip a tap while its valve is offline (button / card / action), or include it again."""
    msg = plan.msgs.append
    T = len(cfg.taps)
    lbl = cfg.label
    if 0 <= tap < T:
        targets = [tap]
    else:
        targets = [t for t in st.off_asked if _offline(sn, t)] or offline_blockers(st, cfg, sn)
    if not targets:
        msg(("offline_note", 0, "No valve is offline: there is nothing to skip."))
        return
    for t in targets:
        if not cfg.taps[t].valve:
            continue
        if not skip:
            st.off_skip[t] = False
            msg(("offline_note", t + 1, f"{lbl(t)} is in the cycles again."
                 + (" It is still offline, so cycles wait for it (you'll be asked)." if _offline(sn, t) else "")))
        elif not _offline(sn, t):
            msg(("offline_note", t + 1, f"{lbl(t)} is online: there is nothing to skip."))
        elif not can_skip_offline(st, t):
            msg(("offline_open", t + 1, f"{lbl(t)} was last seen open and may still be running, so it can't be "
                                        "skipped: check it" + (", or close the main water." if cfg.main_valve else ".")))
        elif not st.off_skip[t]:
            st.off_skip[t] = True
            if st.cycle >= 0:
                st.remaining[t] = 0
            msg(("offline_note", t + 1, f"{lbl(t)} is skipped in every cycle while it is offline; it is included "
                                        "again when it is back online."))


def _offline_ahead(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, occ_next: list[float]) -> None:
    """20 min before a cycle that would run: a valve is offline, ask now so the start isn't held up."""
    now = sn.now
    for k, occ in list(st.off_ahead.items()):
        if now >= occ:
            del st.off_ahead[k]
    if not cfg.off_ask_ahead or not st.master or st.mode == STUCK or st.off_wait:
        return
    waiting = offline_blockers(st, cfg, sn)
    if not waiting:
        return
    for i, occ in enumerate(occ_next):
        if occ <= 0 or not st.cycle_on[i] or not cycle_has_water(cfg, i):
            continue
        d = occ - now
        if not 0 < d <= AHEAD_S + 30 or st.off_ahead.get(str(i)) == occ:
            continue
        st.off_ahead[str(i)] = occ
        if st.off_shown and sorted(waiting) == sorted(st.off_asked):
            continue  # already asked about these valves
        st.off_asked = list(waiting)
        n = len(waiting)
        detail = "; ".join(_off_detail(st, cfg, t, now, sn.tz) for t in waiting)
        can = [t for t in waiting if can_skip_offline(st, t)]
        tail = (f" Press and hold to skip {'it' if n == 1 else 'them'} while offline; otherwise the cycle waits at the "
                "start and asks again." if can else
                " It was last seen open and may still be running: check it before the cycle starts.")
        plan.msgs.append(("offline_ahead" if can else "offline_open", i + 1,
                          f"Cycle {i + 1} starts at {_hm(occ, sn.tz)} (in {max(int(round(d / 60)), 1)} min) but "
                          f"{detail}.{tail}"))
        return  # one question at a time


def _offline_tidy(st: State, cfg: Cfg, sn: Snapshot, plan: Plan) -> None:
    """Keep track of the offline question on the phone; clear it once nothing is offline any more."""
    sent = {k for k, _n, _t in plan.msgs}
    waiting = offline_blockers(st, cfg, sn)
    if sent & set(OFFLINE_ASK):
        st.off_shown = True
    elif "offline_note" in sent:
        st.off_shown = False  # the note replaced the question
    elif st.off_shown and not st.off_wait and not waiting:
        st.off_shown = False
        plan.clear.append("offline")
    if not st.off_wait and not waiting:
        st.off_asked = []


def _battery(st: State, cfg: Cfg, sn: Snapshot, plan: Plan) -> None:
    """Valve batteries: warn at or below the level, repeat, respect the snooze and the quiet time."""
    msg = plan.msgs.append
    now, tz = sn.now, sn.tz
    T = len(cfg.taps)
    lbl = cfg.label
    pct = cfg.bat_pct
    for t in range(T):
        v = sn.battery[t] if t < len(sn.battery) else None
        if not cfg.taps[t].valve:
            st.bat_level[t], st.bat_low[t] = -1.0, False
        elif v is not None:
            st.bat_level[t] = max(0.0, min(100.0, float(v)))
    fine = []
    for t in range(T):
        lvl = st.bat_level[t]
        if pct <= 0:
            st.bat_low[t] = False
        elif lvl >= 0 and lvl <= pct:
            st.bat_low[t] = True
        elif st.bat_low[t] and lvl >= pct + BAT_REARM:
            st.bat_low[t] = False
            fine.append(t)
    if fine:
        msg(("battery_note", 0, "Battery fine again: "
             + ", ".join(f"{lbl(t)} {_g(round(st.bat_level[t]))}%" for t in fine) + "."))
    low = [t for t in range(T) if st.bat_low[t]]
    if not low:
        if st.bat_shown and not fine:
            plan.clear.append("battery")
        st.bat_next = st.bat_snooze_until = 0.0
        st.bat_snoozed, st.bat_last, st.bat_shown = [], [], False
        return
    snoozing = now < st.bat_snooze_until - 1
    if st.bat_snooze_until and not snoozing:
        st.bat_snooze_until, st.bat_snoozed = 0.0, []
    due_taps = [t for t in low if not (snoozing and t in st.bat_snoozed)]
    new = [t for t in due_taps if t not in st.bat_last]
    if not due_taps or not (new or (st.bat_next > 0 and now >= st.bat_next - 1)):
        return
    if in_quiet(cfg, now, tz):
        st.bat_next = max(st.bat_next, _quiet_end(cfg, now, tz))  # held until the quiet time ends
        return
    st.bat_last, st.bat_shown = list(low), True
    st.bat_next = now + cfg.bat_remind_h * 3600 if cfg.bat_remind_h > 0 else 0.0
    # short: one valve per line, lowest first (the title already says "Low battery")
    lines = [f"{lbl(t)} {_g(round(st.bat_level[t]))}%" + (" (offline)" if _offline(sn, t) else "")
             for t in sorted(low, key=lambda t: (st.bat_level[t], t))]
    msg(("battery", 0, "\n".join(lines) + "\n\nReplace the batteries soon. Press and hold to snooze."))


def _snooze_battery(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, hours: float) -> None:
    msg = plan.msgs.append
    now, tz = sn.now, sn.tz
    low = [t for t in range(len(cfg.taps)) if st.bat_low[t]]
    if not low:
        msg(("battery_note", 0, "No valve battery is low: there is nothing to snooze."))
    elif hours <= 0:
        st.bat_snooze_until, st.bat_snoozed = 0.0, []
        st.bat_next = now
        msg(("battery_note", 0, "Battery warnings are back on."))
    else:
        st.bat_snooze_until = now + hours * 3600
        st.bat_snoozed = list(low)
        st.bat_next = max(st.bat_next, st.bat_snooze_until)
        st.bat_shown = False  # the note replaces the warning
        msg(("battery_note", 0, f"Battery warnings snoozed for {_dur(hours * 3600)}, until "
                                f"{_ddhm(st.bat_snooze_until, tz)}. Still low: {_names(cfg, low)}."))


def _battery_report(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, asked: bool) -> None:
    """Every report_days days at report_time (and on request): every tap valve's battery level."""
    now, tz = sn.now, sn.tz
    due = False
    if not cfg.rep_on:
        st.rep_anchor = st.rep_fp = ""
    else:
        if not cfg.rep_start and not st.rep_anchor:
            st.rep_anchor = datetime.fromtimestamp(now, tz).date().isoformat()
        last, _nxt = report_times(st, cfg, now, tz)
        fp = f"{cfg.rep_start or st.rep_anchor}|{cfg.rep_days}|{cfg.rep_time}"
        if st.rep_fp != fp:  # new or changed schedule: the past counts as done
            st.rep_fp, st.rep_last = fp, int(last // 60)
        if last and int(last // 60) > st.rep_last:
            st.rep_last = int(last // 60)
            due = now - last < MISSED_WINDOW_S
    if not (due or asked):
        return
    lbl = cfg.label
    taps = [t for t in range(len(cfg.taps)) if cfg.taps[t].valve]
    known = [t for t in taps if st.bat_level[t] >= 0]
    if not known:
        if asked:
            plan.msgs.append(("battery_report", 0, "No battery levels to report: none of the tap valves has a "
                                                   "battery sensor (set one under the tap's settings)."))
        return
    def line(t: int) -> str:
        if st.bat_level[t] < 0:
            return f"{lbl(t)}: no battery reading"
        return (f"{lbl(t)} {_g(round(st.bat_level[t]))}%" + (" (offline)" if _offline(sn, t) else "")
                + (" (low)" if st.bat_low[t] else ""))

    # the lowest first, then one tap per line in tap order, then when the next one comes
    lowest = min(known, key=lambda t: (st.bat_level[t], t))
    _last, nxt = report_times(st, cfg, now, tz)
    plan.msgs.append(("battery_report", 0, f"Lowest: {line(lowest)}\n\n" + "\n".join(line(t) for t in taps)
                      + (f"\n\nNext report: {_ddhm(nxt, tz)}" if nxt else "")))


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


def _main_cycle_reminders(st: State, cfg: Cfg, sn: Snapshot, plan: Plan, occ_next: list[float]) -> None:
    """Main water off: remind 20 and 10 min before a cycle that would run (not affected by a snooze)."""
    now = sn.now
    for k, (occ, _lvl) in list(st.main_cycle_rem.items()):
        if now >= occ or sn.main != V_OFF:
            plan.clear.append(f"main_cycle_{int(k) + 1}")
            del st.main_cycle_rem[k]
    if not (cfg.main_valve and cfg.main_remind_cycle and sn.main == V_OFF and st.master):
        return
    for i, occ in enumerate(occ_next):
        if occ <= 0 or not st.cycle_on[i] or not cycle_has_water(cfg, i):
            continue
        d = occ - now
        sent = st.main_cycle_rem.get(str(i))
        lvl_sent = sent[1] if sent and sent[0] == occ else 0
        level = 10 if d <= 630 else (20 if d <= 1230 else 0)
        if level and (lvl_sent == 0 or level < lvl_sent):
            st.main_cycle_rem[str(i)] = [occ, float(level)]
            plan.msgs.append(("main_cycle", i + 1, f"Cycle {i + 1} starts at {_hm(occ, sn.tz)} "
                                                   f"(in {max(int(round(d / 60)), 1)} min) but the main water is off: "
                                                   "it will wait until the main water is on."))


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
    if st.mode == IDLE and st.pending and not any(sn.bypass) and not main_is_off(cfg, sn):
        cands.append(now + 1)
    if st.mode == STUCK and cfg.main_valve and not st.main_alerted and st.stuck_since:
        cands.append(st.stuck_since + cfg.main_wait_min * 60)
    if cfg.main_valve and sn.main == V_OFF:
        if st.main_remind_at:
            cands.append(max(st.main_remind_at, st.main_snooze_until))
        if cfg.main_remind_cycle and st.master:
            for i, o in enumerate(occ_next):
                if o > 0 and st.cycle_on[i]:
                    cands += [o - 1200, o - 600]
    cands += [o for o in occ_next if o > 0]
    if not st.master:
        for i, o in enumerate(occ_next):
            if o > 0 and st.cycle_on[i]:
                cands += [o - 1200, o - 600]
    if st.hot_since and not st.hot_alerted:
        cands.append(st.hot_since + HOT_FOR_S)
    if st.skip_until > now:
        cands.append(st.skip_until)
    if st.off_wait and st.cycle >= 0:
        cands.append(st.off_wait + max(cfg.off_wait_min, 1) * 60)
    if cfg.off_ask_ahead and st.master and offline_blockers(st, cfg, sn):
        cands += [o - AHEAD_S for i, o in enumerate(occ_next) if o > 0 and st.cycle_on[i]]
    if any(st.bat_low):
        cands += [t for t in (st.bat_next, st.bat_snooze_until) if t]
    _last, rep_next = report_times(st, cfg, now, sn.tz)
    if rep_next:
        cands.append(rep_next)
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
                     + " Nothing else will open; retrying every 5 min."
                     + (f" If it is still not off after {cfg.main_wait_min} min, you'll be asked to close the main water."
                        if cfg.main_valve and not cfg.main_auto_close else "")
                     + (f" If it is still not off after {cfg.main_wait_min} min, the main water closes automatically."
                        if cfg.main_valve and cfg.main_auto_close else "")))
        st.stuck_since, st.main_alerted = now, False
    st.mode, st.tap, st.end = STUCK, tap, now + STUCK_RETRY_S  # the cycle keeps its remaining time
    return msgs


def on_main_done(st: State, cfg: Cfg, action: str, ok: bool, now: float, tz: tzinfo) -> list[tuple[str, int, str]]:
    """The executor closed / opened the main water valve (ok = it reports the new state)."""
    hm = _hm(now, tz)
    stuck = f" {cfg.label(st.tap)} still can't be confirmed off; it is checked every 5 min." if st.mode == STUCK else ""
    if action == "close":
        if ok:
            st.main_ask = False
            return [("main", 0, f"Main water closed at {hm}.{stuck} Cycles wait until it is on again.")]
        st.main_ask = True
        return [("main_alert", 0, f"Could not confirm the main water is off ({hm}). Check it, or try again: press and "
                                  f"hold this notification.{stuck}")]
    if ok:
        st.main_prompt = st.main_reminded = False
        return [("main", 0, f"Main water open again at {hm}."
                 + (f" Note:{stuck}" if stuck else "") + (" Waiting cycles carry on." if st.pending or st.cycle >= 0 else ""))]
    return [("main_open", 0, f"Could not confirm the main water is on ({hm}). Check it, or try again: press and hold "
                             "this notification.")]


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
    held = bool(sn and (any(sn.bypass) or main_is_off(cfg, sn)))
    if st.mode == GAP and st.cycle >= 0:
        if held:
            return "held"
        if st.off_wait:
            return "offline"
        if st.people_wait:
            return "waiting"
        now = sn.now if sn else 0
        if st.end - now > max(cfg.gap_s, 0) + 1:
            return "soaking"
        return "gap"
    if st.pending and held:
        return "held"
    return "idle"
