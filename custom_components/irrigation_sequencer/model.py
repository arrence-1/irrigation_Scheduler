"""Configuration model for Irrigation Scheduler.

Plain Python (no Home Assistant imports) so the engine and its simulator
tests can use it directly. The config entry's ``options`` dict is the
stored form; :func:`normalize` fills defaults and fits every list to the
tap / cycle counts, and :func:`parse` turns it into typed dataclasses.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_TIMES = ["06:00", "18:00", "07:00", "19:00", "08:00", "20:00", "09:00", "21:00",
                 "10:00", "22:00", "11:00", "23:00"]

MAX_TAPS = 16
MAX_CYCLES = 12
MAX_MINUTES = 240

# Defaults are the blueprint's (v1.5.1) values.
DEFAULT_GENERAL = {
    "gap_s": 5,  # gap between taps, 0-60 s
    "confirm_s": 30,  # wait for a valve to report on/off, 5-120 s
    "max_run_min": 20,  # safety limit per run, 1-240 min
    "late_min": 30,  # running-late threshold, 1-720 min
    "seasonal_entity": "",  # optional external % source (else the app's slider)
}
DEFAULT_NOTIFY = {
    "devices": [],  # mobile_app device ids
    "prefix": "irrigation",
    "start_finish": True,
    "skips": True,
    "manual": True,
    "people": True,
    "reminders": True,
    "rain": False,
    "hot_c": 30,  # 0 = off
}
DEFAULT_SOAK = {"burst_min": 3, "soak_min": 5}
DEFAULT_PEOPLE = {"on": True, "clear_min": 2, "max_wait_min": 30, "manual": False}
DEFAULT_WEATHER = {
    "on": True,
    "rain_sensor": "",
    "rain_mm": 5.0,
    "weather_entity": "",
    "fc_prob": 0,
    "fc_mm": 0.0,
    "wind_sensor": "",
    "wind_kmh": 0,
    "temp_sensor": "",
    "cold_on": False,
    "cold_c": 2,
    "boost_pct": 100,
}
# battery: an optional battery sensor (%) for the tap's valve; empty = found on the valve's device
DEFAULT_TAP = {"valve": "", "name": "", "people": [], "soil": "", "soil_pct": 0, "bypass": "", "battery": ""}
# Offline valves: no tap opens while another valve is offline (it can't be confirmed closed). The cycle waits and
# asks (skip that tap while it is offline, or stop the cycle); with no answer after wait_min it does no_answer.
# ask_ahead: ask 20 min before a cycle too, so it can be settled before the start.
NO_ANSWER = ("stop", "skip")
DEFAULT_OFFLINE = {"wait_min": 30, "no_answer": "stop", "ask_ahead": True}
# Valve batteries: a warning at or below pct (0 = off), repeated every remind_h hours (0 = once) and held back during
# the quiet time; plus a report of every level every report_days days at report_time (counted from report_start,
# a date, when it is set).
DEFAULT_BATTERY = {
    "pct": 30,
    "remind_h": 2,
    "quiet_on": True,
    "quiet_from": "21:00",
    "quiet_to": "06:00",
    "report_on": True,
    "report_days": 14,
    "report_time": "10:00",
    "report_start": "",
}
# Main water: the supply valve for every tap. A tap that can't be confirmed off for wait_min minutes raises a
# critical alert with a "Close main water" button, or (auto_close) closes it straight away. While it is off you are
# reminded every remind_min minutes (snooze from the notification), and 20 / 10 min before a cycle (remind_cycle).
DEFAULT_MAIN = {"valve": "", "wait_min": 10, "auto_close": False, "remind_min": 30, "remind_cycle": True}


def _int(v: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        r = int(round(float(v)))
    except (TypeError, ValueError):
        r = default
    if lo is not None:
        r = max(lo, r)
    if hi is not None:
        r = min(hi, r)
    return r


def _float(v: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        r = float(v)
    except (TypeError, ValueError):
        r = default
    if r != r:  # NaN
        r = default
    if lo is not None:
        r = max(lo, r)
    if hi is not None:
        r = min(hi, r)
    return r


def _str(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _list_str(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]
    return []


def normalize_time(v: Any, default: str = "06:00") -> str:
    """'6:5', '06:05:00' -> '06:05'. Invalid -> default."""
    if not isinstance(v, str):
        return default
    parts = v.strip().split(":")
    try:
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return default
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return default
    return f"{h:02d}:{m:02d}"


def normalize_date(v: Any) -> str:
    """'2026-11-01' (or '2026-11-01T…') -> '2026-11-01'. Invalid / empty -> ''."""
    if not isinstance(v, str) or not v.strip():
        return ""
    try:
        return date.fromisoformat(v.strip()[:10]).isoformat()
    except ValueError:
        return ""


def fix_order(order: list[int]) -> list[int]:
    """Make a cycle's run order 1..T with every number used once.

    The running sequence is kept: equal numbers (from older versions) go in
    tap-number order, e.g. [2, 1, 1] -> [3, 1, 2].
    """
    ranked = sorted(range(len(order)), key=lambda i: (order[i], i))
    out = [0] * len(order)
    for pos, i in enumerate(ranked, 1):
        out[i] = pos
    return out


def swap_order(order: list[int], tap: int, new: int) -> list[int]:
    """Give ``tap`` (0-based) run order ``new``; the tap that had it takes the old number."""
    out = list(order)
    new = max(1, min(len(out), int(new)))
    old = out[tap]
    if new == old:
        return out
    for j, v in enumerate(out):
        if j != tap and v == new:
            out[j] = old
    out[tap] = new
    return out


def resolve_order(old: list[int], new: list[int]) -> list[int] | None:
    """Run orders from the settings form, where every tap's number is edited at once.

    A complete 1..T set is used as it is; a single changed number swaps with
    the tap that had it. Anything else is ambiguous: None (the form asks again).
    """
    T = len(old)
    new = [max(1, min(T, int(v))) for v in new]
    if sorted(new) == list(range(1, T + 1)):
        return new
    changed = [i for i in range(T) if new[i] != old[i]]
    if len(changed) == 1:
        return swap_order(old, changed[0], new[changed[0]])
    return None


def default_cycle(index: int, taps: int) -> dict[str, Any]:
    return {
        "time": DEFAULT_TIMES[index % len(DEFAULT_TIMES)],
        "days": list(DAYS),
        "minutes": [0] * taps,
        "order": list(range(1, taps + 1)),
        "soak": [False] * taps,
    }


def default_options(taps: int = 6, cycles: int = 4) -> dict[str, Any]:
    return normalize({"tap_count": taps, "cycle_count": cycles})


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a complete, clamped copy of the options dict."""
    raw = copy.deepcopy(dict(raw or {}))
    T = _int(raw.get("tap_count", len(raw.get("taps") or []) or 6), 6, 1, MAX_TAPS)
    C = _int(raw.get("cycle_count", len(raw.get("cycles") or []) or 4), 4, 1, MAX_CYCLES)

    out: dict[str, Any] = {"tap_count": T, "cycle_count": C}

    g = {**DEFAULT_GENERAL, **(raw.get("general") or {})}
    out["general"] = {
        "gap_s": _int(g["gap_s"], 5, 0, 60),
        "confirm_s": _int(g["confirm_s"], 30, 5, 120),
        "max_run_min": _int(g["max_run_min"], 20, 1, 240),
        "late_min": _int(g["late_min"], 30, 1, 720),
        "seasonal_entity": _str(g["seasonal_entity"]),
    }
    n = {**DEFAULT_NOTIFY, **(raw.get("notify") or {})}
    out["notify"] = {
        "devices": _list_str(n["devices"]),
        "prefix": (_str(n["prefix"]) or "irrigation").lower().replace(" ", "_"),
        "start_finish": bool(n["start_finish"]),
        "skips": bool(n["skips"]),
        "manual": bool(n["manual"]),
        "people": bool(n["people"]),
        "reminders": bool(n["reminders"]),
        "rain": bool(n["rain"]),
        "hot_c": _float(n["hot_c"], 30, 0, 50),
    }
    s = {**DEFAULT_SOAK, **(raw.get("soak") or {})}
    out["soak"] = {"burst_min": _int(s["burst_min"], 3, 1, 60), "soak_min": _int(s["soak_min"], 5, 0, 120)}
    p = {**DEFAULT_PEOPLE, **(raw.get("people") or {})}
    out["people"] = {
        "on": bool(p["on"]),
        "clear_min": _float(p["clear_min"], 2, 0, 30),
        "max_wait_min": _int(p["max_wait_min"], 30, 0, 240),
        "manual": bool(p["manual"]),
    }
    w = {**DEFAULT_WEATHER, **(raw.get("weather") or {})}
    out["weather"] = {
        "on": bool(w["on"]),
        "rain_sensor": _str(w["rain_sensor"]),
        "rain_mm": _float(w["rain_mm"], 5, 0.5, 100),
        "weather_entity": _str(w["weather_entity"]),
        "fc_prob": _int(w["fc_prob"], 0, 0, 100),
        "fc_mm": _float(w["fc_mm"], 0, 0, 50),
        "wind_sensor": _str(w["wind_sensor"]),
        "wind_kmh": _float(w["wind_kmh"], 0, 0, 100),
        "temp_sensor": _str(w["temp_sensor"]),
        "cold_on": bool(w["cold_on"]),
        "cold_c": _float(w["cold_c"], 2, -10, 15),
        "boost_pct": _int(w["boost_pct"], 100, 100, 200),
    }

    m = {**DEFAULT_MAIN, **(raw.get("main") or {})}
    out["main"] = {
        "valve": _str(m["valve"]),
        "wait_min": _int(m["wait_min"], 10, 1, 240),
        "auto_close": bool(m["auto_close"]),
        "remind_min": _int(m["remind_min"], 30, 0, 720),
        "remind_cycle": bool(m["remind_cycle"]),
    }
    f = {**DEFAULT_OFFLINE, **(raw.get("offline") or {})}
    out["offline"] = {
        "wait_min": _int(f["wait_min"], 30, 1, 240),
        "no_answer": f["no_answer"] if f["no_answer"] in NO_ANSWER else "stop",
        "ask_ahead": bool(f["ask_ahead"]),
    }
    b = {**DEFAULT_BATTERY, **(raw.get("battery") or {})}
    out["battery"] = {
        "pct": _int(b["pct"], 30, 0, 100),
        "remind_h": _float(b["remind_h"], 2, 0, 48),
        "quiet_on": bool(b["quiet_on"]),
        "quiet_from": normalize_time(b["quiet_from"], "21:00"),
        "quiet_to": normalize_time(b["quiet_to"], "06:00"),
        "report_on": bool(b["report_on"]),
        "report_days": _int(b["report_days"], 14, 1, 90),
        "report_time": normalize_time(b["report_time"], "10:00"),
        "report_start": normalize_date(b["report_start"]),
    }

    taps_in = raw.get("taps") or []
    taps = []
    for i in range(T):
        t = {**DEFAULT_TAP, **(taps_in[i] if i < len(taps_in) and isinstance(taps_in[i], dict) else {})}
        taps.append({
            "valve": _str(t["valve"]),
            "name": _str(t["name"]),
            "people": _list_str(t["people"]),
            "soil": _str(t["soil"]),
            "soil_pct": _float(t["soil_pct"], 0, 0, 100),
            "bypass": _str(t["bypass"]),
            "battery": _str(t["battery"]),
        })
    out["taps"] = taps

    cyc_in = raw.get("cycles") or []
    cycles = []
    for c in range(C):
        d = default_cycle(c, T)
        src = cyc_in[c] if c < len(cyc_in) and isinstance(cyc_in[c], dict) else {}
        days = [x for x in DAYS if x in _list_str(src.get("days", d["days"]))]
        mins = list(src.get("minutes") or [])
        order = list(src.get("order") or [])
        soak = list(src.get("soak") or [])
        cycles.append({
            "time": normalize_time(src.get("time"), d["time"]),
            "days": days,
            "minutes": [_int(mins[i], 0, 0, MAX_MINUTES) if i < len(mins) else 0 for i in range(T)],
            "order": fix_order([_int(order[i], i + 1, 1, T) if i < len(order) else i + 1 for i in range(T)]),
            "soak": [bool(soak[i]) if i < len(soak) else False for i in range(T)],
        })
    out["cycles"] = cycles
    return out


# --------------------------------------------------------------------------
# Typed view used by the engine
# --------------------------------------------------------------------------
@dataclass
class TapCfg:
    valve: str = ""
    name: str = ""
    people: list[str] = field(default_factory=list)
    soil: str = ""
    soil_pct: float = 0.0
    bypass: str = ""
    battery: str = ""


@dataclass
class CycleCfg:
    time: str = "06:00"
    days: list[str] = field(default_factory=lambda: list(DAYS))
    minutes: list[int] = field(default_factory=list)
    order: list[int] = field(default_factory=list)
    soak: list[bool] = field(default_factory=list)


@dataclass
class Cfg:
    taps: list[TapCfg]
    cycles: list[CycleCfg]
    gap_s: int = 5
    confirm_s: int = 30
    max_run_min: int = 20
    late_min: int = 30
    seasonal_entity: str = ""
    burst_min: int = 3
    soak_min: int = 5
    people_on: bool = True
    people_clear_min: float = 2
    people_wait_min: int = 30
    people_manual: bool = False
    wx_on: bool = True
    rain_sensor: str = ""
    rain_mm: float = 5
    weather_entity: str = ""
    fc_prob: int = 0
    fc_mm: float = 0
    wind_sensor: str = ""
    wind_kmh: float = 0
    temp_sensor: str = ""
    cold_on: bool = False
    cold_c: float = 2
    boost_pct: int = 100
    n_devices: list[str] = field(default_factory=list)
    n_prefix: str = "irrigation"
    n_start_finish: bool = True
    n_skips: bool = True
    n_manual: bool = True
    n_people: bool = True
    n_reminders: bool = True
    n_rain: bool = False
    hot_c: float = 30
    main_valve: str = ""
    main_wait_min: int = 10
    main_auto_close: bool = False
    main_remind_min: int = 30
    main_remind_cycle: bool = True
    off_wait_min: int = 30
    off_no_answer: str = "stop"
    off_ask_ahead: bool = True
    bat_pct: int = 30
    bat_remind_h: float = 2
    quiet_on: bool = True
    quiet_from: str = "21:00"
    quiet_to: str = "06:00"
    rep_on: bool = True
    rep_days: int = 14
    rep_time: str = "10:00"
    rep_start: str = ""

    def label(self, i: int) -> str:
        """'Tap 1 (Backyard Left)' or 'Tap 1'."""
        name = self.taps[i].name if 0 <= i < len(self.taps) else ""
        return f"Tap {i + 1} ({name})" if name else f"Tap {i + 1}"


def parse(options: dict[str, Any]) -> Cfg:
    o = normalize(options)
    g, n, s, p, w, m = o["general"], o["notify"], o["soak"], o["people"], o["weather"], o["main"]
    f, b = o["offline"], o["battery"]
    return Cfg(
        taps=[TapCfg(**t) for t in o["taps"]],
        cycles=[CycleCfg(**c) for c in o["cycles"]],
        gap_s=g["gap_s"], confirm_s=g["confirm_s"], max_run_min=g["max_run_min"],
        late_min=g["late_min"], seasonal_entity=g["seasonal_entity"],
        burst_min=s["burst_min"], soak_min=s["soak_min"],
        people_on=p["on"], people_clear_min=p["clear_min"], people_wait_min=p["max_wait_min"],
        people_manual=p["manual"],
        wx_on=w["on"], rain_sensor=w["rain_sensor"], rain_mm=w["rain_mm"],
        weather_entity=w["weather_entity"], fc_prob=w["fc_prob"], fc_mm=w["fc_mm"],
        wind_sensor=w["wind_sensor"], wind_kmh=w["wind_kmh"], temp_sensor=w["temp_sensor"],
        cold_on=w["cold_on"], cold_c=w["cold_c"], boost_pct=w["boost_pct"],
        n_devices=n["devices"], n_prefix=n["prefix"], n_start_finish=n["start_finish"],
        n_skips=n["skips"], n_manual=n["manual"], n_people=n["people"],
        n_reminders=n["reminders"], n_rain=n["rain"], hot_c=n["hot_c"],
        main_valve=m["valve"], main_wait_min=m["wait_min"], main_auto_close=m["auto_close"],
        main_remind_min=m["remind_min"], main_remind_cycle=m["remind_cycle"],
        off_wait_min=f["wait_min"], off_no_answer=f["no_answer"], off_ask_ahead=f["ask_ahead"],
        bat_pct=b["pct"], bat_remind_h=b["remind_h"], quiet_on=b["quiet_on"], quiet_from=b["quiet_from"],
        quiet_to=b["quiet_to"], rep_on=b["report_on"], rep_days=b["report_days"], rep_time=b["report_time"],
        rep_start=b["report_start"],
    )
