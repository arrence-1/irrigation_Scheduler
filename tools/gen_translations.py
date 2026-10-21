"""Generate translations/en.json (labels for every screen, entity and action).

Run from the repo root:  python tools/gen_translations.py
"""

import json
from pathlib import Path

MAX_TAPS, MAX_CYCLES = 16, 12
OUT = Path(__file__).resolve().parents[1] / "custom_components" / "irrigation_sequencer" / "translations" / "en.json"

ENTITY_PICK = "Leave empty to switch this off."

cfg = {
    "step": {
        "user": {
            "title": "Irrigation Scheduler",
            "description": "Waters your garden one tap at a time over scheduled cycles. Choose how many taps (valves) "
                           "and cycles you need; both can be changed later.",
            "data": {"name": "Name", "tap_count": "Number of taps", "cycle_count": "Number of cycles"},
            "data_description": {
                "name": "Used for the device and entity names (Irrigation -> switch.irrigation_master).",
                "tap_count": "Unused taps can be left empty.",
                "cycle_count": "Each cycle has its own start time, days, and per tap minutes / run order / soak.",
            },
        },
        "valves": {
            "title": "Pick the valves",
            "description": "One valve (switch, valve or toggle helper) per tap. Toggle helpers are good for a first "
                           "test. Everything else (names, minutes, sensors) is set afterwards under Configure or on "
                           "the dashboard card.\n\nThe master switch starts OFF: nothing is scheduled until you turn "
                           "it on. Manual runs (a tap switched on by hand) are timed from the start.",
            "data": {f"tap_{i}": f"Tap {i}" for i in range(1, MAX_TAPS + 1)},
        },
    },
    "abort": {},
}

sections = {}
for i in range(1, MAX_TAPS + 1):
    sections[f"tap_{i}"] = {
        "name": "{tap_%d}" % i,
        "data": {"minutes": "Minutes (0 = Off)", "order": "Run order", "soak": "Cycle-and-soak"},
        "data_description": {
            "minutes": "0 = Off, not in this cycle.",
            "order": "1 runs first. Every tap has its own number: pick one another tap has and the two swap.",
            "soak": "Water in short bursts with soak breaks (timing under Cycle-and-soak).",
        },
    }

opt = {
    "error": {
        "order_clash": "Two taps can't share a run order ({clash}). Change one run order at a time to swap "
                       "two taps, or give every tap its own number.",
    },
    "step": {
        "init": {
            "title": "Irrigation Scheduler settings",
            "description": "Pick what to change. Each screen saves when you press Submit. Minutes, run order and "
                           "soak can also be edited on the dashboard card.",
            "menu_options": {
                "cycle_pick": "Cycles: time, days, minutes, run order, soak",
                "tap_pick": "Taps: valve, name, people sensors, soil, bypass",
                "general": "General: gap, safety limit, seasonal source",
                "notifications": "Notifications",
                "soak": "Cycle-and-soak timing",
                "people": "People guard (cameras)",
                "weather": "Rain & weather skip",
                "main": "Main water: shut-off valve, tap-won't-close alert",
                "offline": "Offline valves: wait, ask, skip while offline",
                "battery": "Valve batteries: low warning, quiet time, battery report",
                "counts": "Number of taps and cycles",
            },
        },
        "cycle_pick": {"title": "Which cycle?", "data": {"cycle": "Cycle"}},
        "cycle": {
            "title": "Cycle {cycle}",
            "description": "Minutes 0 = Off (the tap is not in this cycle). Run order 1 runs first; every tap has its "
                           "own number, so changing one swaps it with the tap that had it. This cycle's on/off switch "
                           "is its own entity (Cycle {cycle}).",
            "data": {"time": "Start time", "days": "Days"},
            "data_description": {
                "time": "Avoid 02:00-03:00 (the daylight-saving change hour). If something is running, the start "
                        "shifts later; it is never dropped.",
            },
            "sections": sections,
        },
        "tap_pick": {"title": "Which tap?", "data": {"tap": "Tap"}},
        "tap": {
            "title": "{tap}",
            "data": {
                "valve": "Valve",
                "name": "Name",
                "people": "People sensors (cameras / zones)",
                "soil": "Soil moisture sensor",
                "soil_pct": "Wet enough at",
                "bypass": "Bypass switch",
                "battery": "Battery sensor",
            },
            "data_description": {
                "valve": "switch, valve or toggle helper. Leave empty if unused.",
                "name": "Shown in notifications and on the card, e.g. Backyard Left.",
                "people": "Optional. Person sensors covering the area this tap waters (Frigate person occupancy, "
                          "person count). See People guard.",
                "soil": "Optional. A soil moisture sensor (%) in this tap's area. Leave empty for e.g. the lawn.",
                "soil_pct": "This tap sits a cycle out at or above this. 0 = off. Tip: note the reading a day after "
                            "a good soak and set a little below it. Part of Rain & weather skip.",
                "bypass": "Optional. While this is on (e.g. input_boolean.pressure_washer_bypass) the tap is left "
                          "alone (no manual timer) and cycles wait until it is off.",
                "battery": "Optional. Leave empty: the battery sensor (%) on the valve's own device is used. Pick one "
                           "here only if that is wrong or missing. Used by Valve batteries.",
            },
        },
        "general": {
            "title": "General",
            "data": {
                "gap_s": "Gap between taps",
                "confirm_s": "Confirm timeout",
                "max_run_min": "Safety limit per run",
                "late_min": "Running-late threshold",
                "seasonal_entity": "Seasonal % from another entity",
            },
            "data_description": {
                "gap_s": "Pause after a tap reports off, before the next one opens.",
                "confirm_s": "How long to wait for a valve to report on / off (3 tries when closing).",
                "max_run_min": "No tap stays open longer than this in one go; longer taps run in parts. Also caps "
                               "manual runs.",
                "late_min": "Cycles always run; starting later than this is flagged 'Running late'.",
                "seasonal_entity": "Optional. Use this input_number / sensor (%) instead of the app's Seasonal "
                                   "adjustment slider. " + ENTITY_PICK,
            },
        },
        "notifications": {
            "title": "Notifications",
            "description": "Messages go to the phones you pick, in one group / thread; a newer message of the same "
                           "type replaces the older one. Tap faults are always sent (time-sensitive on iOS).",
            "data": {
                "devices": "Phones (Companion app)",
                "prefix": "Group / tag prefix",
                "start_finish": "Cycle started and finished",
                "skips": "Cycle or tap skipped",
                "manual": "Manual runs",
                "people": "People guard",
                "reminders": "Master switch off reminders",
                "rain": "Started raining",
                "hot_c": "Too hot at",
            },
            "data_description": {
                "prefix": "Groups all messages and starts every tag, e.g. irrigation -> irrigation_cycle_1.",
                "reminders": "20 and 10 min before a cycle while the master switch is off, with a "
                             "'Turn master switch on' button.",
                "rain": "When the rain sensor turns wet while the master switch is on (re-armed after 30 min dry).",
                "hot_c": "An alert after 10 min at or above this; also the hot-day boost trigger. 0 = off.",
            },
        },
        "soak": {
            "title": "Cycle-and-soak timing",
            "description": "For taps with cycle-and-soak on: water a little, let it soak in, water again. Other taps "
                           "run while one soaks.",
            "data": {"burst_min": "Burst (water on)", "soak_min": "Soak (rest)"},
        },
        "people": {
            "title": "People guard (cameras)",
            "description": "Uses each tap's people sensors. A tap does not start while someone is in its area and "
                           "pauses if someone walks in; other taps run meanwhile.",
            "data": {
                "on": "People guard on",
                "clear_min": "Clear for",
                "max_wait_min": "Give up after",
                "manual": "Also stop manual runs",
            },
            "data_description": {
                "clear_min": "How long the area must be empty before the tap starts or carries on.",
                "max_wait_min": "When only people-blocked taps are left, wait this long, then skip them. 0 = wait as "
                                "long as it takes.",
            },
        },
        "weather": {
            "title": "Rain & weather skip",
            "description": "A cycle is skipped when ANY of these applies at its start. Soil moisture is per tap. "
                           "Leave a sensor empty or a limit at 0 to switch that check off.",
            "data": {
                "on": "Skip checks on (includes soil)",
                "rain_sensor": "Rain sensor",
                "rain_mm": "Rain amount",
                "weather_entity": "Weather entity",
                "fc_prob": "Forecast rain chance",
                "fc_mm": "Forecast rain amount",
                "wind_sensor": "Wind speed sensor",
                "wind_kmh": "Too windy at",
                "temp_sensor": "Outdoor temperature sensor",
                "cold_on": "Cold skip",
                "cold_c": "Cold skip at",
                "boost_pct": "Hot-day boost",
            },
            "data_description": {
                "rain_sensor": "Skip when it is 'on' or at/above the rain amount (a rain-today or 24 h total works "
                               "well).",
                "weather_entity": "Skip when it is rainy / pouring now; also the forecast source, and the fallback for "
                                  "wind and temperature.",
                "fc_prob": "Today's forecast chance. 0 = off.",
                "fc_mm": "Today's forecast amount. 0 = off.",
                "wind_kmh": "0 = off.",
                "temp_sensor": "Used by the cold skip, the too-hot alert and the hot-day boost.",
                "boost_pct": "Durations x this when it is at/above 'Too hot at' as a cycle starts (100 = off; max "
                             "250 % together with the seasonal %). Works even with the skip checks off.",
            },
        },
        "main": {
            "title": "Main water",
            "description": "The valve that supplies water to every tap. If a tap can't be confirmed off, the app keeps "
                           "retrying every 5 min; once it has been stuck for the wait below you get a critical alert "
                           "with a Close main water button (press and hold the notification). While the main water "
                           "is off, cycles wait and run late once it is on again. The app never opens the main "
                           "water by itself: you do, with the Open main water button.",
            "data": {
                "valve": "Main water valve",
                "wait_min": "Alert after a tap is stuck for",
                "auto_close": "Close the main water automatically",
                "remind_min": "While it is off, remind me every",
                "remind_cycle": "Remind me before a cycle",
            },
            "data_description": {
                "valve": "Switch, valve, toggle helper or light (some relays show as a light). On = water flowing. "
                         "Leave empty to switch this off.",
                "wait_min": "Counted from the moment a tap could not be confirmed off.",
                "auto_close": "Off: you are asked (critical alert with a Close main water button). On: the app "
                              "closes it after the wait and tells you.",
                "remind_min": "Repeats until the main water is on again. Snooze from the notification (press and "
                              "hold: Snooze 1 h, or Snooze… and type the hours); the reminders start again when the "
                              "snooze ends. 0 = no reminders.",
                "remind_cycle": "20 and 10 min before a cycle that would run while the main water is off (also "
                                "while snoozed).",
            },
        },
        "offline": {
            "title": "Offline valves",
            "description": "No tap opens while another tap's valve is offline (e.g. a flat battery), because it can't "
                           "be confirmed closed. The cycle waits and you get a notification: press and hold to Skip "
                           "while offline (the tap sits every cycle out until it is back online; needs Face ID / "
                           "unlock) or Stop the cycle. A valve last seen open is never skipped: that is the stuck-tap "
                           "and main water alert.",
            "data": {
                "wait_min": "Wait for an answer",
                "no_answer": "With no answer",
                "ask_ahead": "Ask 20 min before a cycle",
            },
            "data_description": {
                "wait_min": "How long a cycle waits for your answer (or for the valve to come back).",
                "no_answer": "Stop the cycle (safest), or skip the offline tap and water the rest. A valve last seen "
                             "open always stops the cycle.",
                "ask_ahead": "If a valve is offline 20 min before a cycle would start, ask then, so the start isn't "
                             "held up.",
            },
        },
        "battery": {
            "title": "Valve batteries",
            "description": "Uses each tap valve's battery sensor (found on the valve's device, or set under the tap). "
                           "A warning comes when a battery is at or below the level and repeats until it is replaced "
                           "(fine again 5 % above the level). Snooze it from the notification (press and hold: "
                           "Snooze 1 day, or Snooze… and type hours or days, e.g. 12 or 3d). Separately, a battery "
                           "report lists every valve's level on the days you choose.",
            "data": {
                "pct": "Warn at or below",
                "remind_h": "Repeat the warning every",
                "quiet_on": "Quiet time",
                "quiet_from": "Quiet from",
                "quiet_to": "Quiet until",
                "report_on": "Battery report",
                "report_days": "Report every",
                "report_time": "Report time",
                "report_start": "First report on",
            },
            "data_description": {
                "pct": "0 = no low-battery warnings.",
                "remind_h": "0 = warn once (again only when another valve gets low).",
                "quiet_on": "No low-battery warnings between these times: one held back is sent when the quiet time "
                            "ends. The battery report and the offline questions are not held.",
                "report_days": "14 = every 2 weeks, 7 = weekly, 1 = daily.",
                "report_time": "The report is sent at this time.",
                "report_start": "Optional. The date of a report day (the next ones follow every 'Report every' days). "
                                "Empty: from the day the report was switched on.",
            },
        },
        "counts": {
            "title": "Number of taps and cycles",
            "description": "Changing these reloads the integration (the run in progress carries on).",
            "data": {"tap_count": "Number of taps", "cycle_count": "Number of cycles"},
        },
    },
}

entity = {
    "switch": {"master": {"name": "Master"}, "cycle": {"name": "Cycle {n}"}},
    "number": {"seasonal_adjustment": {"name": "Seasonal adjustment"}},
    "sensor": {
        "status": {
            "name": "Status",
            "state": {
                "idle": "Idle",
                "watering": "Watering",
                "gap": "Between taps",
                "soaking": "Soaking",
                "waiting": "Waiting for people",
                "offline": "Waiting: valve offline",
                "held": "Held (bypass on or main water off)",
                "manual": "Manual run",
                "stuck": "Stuck (valve not off)",
            },
        },
        "next_cycle": {"name": "Next cycle"},
    },
    "binary_sensor": {"tap_blocked": {"name": "Tap {n} blocked"}},
}

entry_field = {"name": "Integration entry", "description": "Only needed when you have more than one."}
services = {
    "run_cycle": {
        "name": "Run cycle now",
        "description": "Runs a cycle now (or after what is running), ignoring the master and cycle switches and the "
                       "weather and soil checks.",
        "fields": {"cycle": {"name": "Cycle", "description": "Cycle number (1 = first)."},
                   "config_entry_id": entry_field},
    },
    "stop": {
        "name": "Stop",
        "description": "Closes the running tap (scheduled or manual) and cancels the running and waiting cycles.",
        "fields": {"config_entry_id": entry_field},
    },
    "skip_tap": {
        "name": "Skip tap",
        "description": "Skips a tap for the rest of the running cycle.",
        "fields": {"tap": {"name": "Tap", "description": "Tap number (1 = first)."}, "config_entry_id": entry_field},
    },
    "close_main_water": {
        "name": "Close main water",
        "description": "Closes the main water valve (set under Main water). Cycles wait until it is open again.",
        "fields": {"config_entry_id": entry_field},
    },
    "open_main_water": {
        "name": "Open main water",
        "description": "Opens the main water valve again. Waiting cycles then run (late).",
        "fields": {"config_entry_id": entry_field},
    },
    "snooze_main_water_reminders": {
        "name": "Snooze main water reminders",
        "description": "Pauses the 'main water is off' reminders. They start again when the time is up.",
        "fields": {"hours": {"name": "Hours", "description": "How long. 0 = remind again now."},
                   "config_entry_id": entry_field},
    },
    "skip_offline_tap": {
        "name": "Skip offline tap",
        "description": "Skips a tap in every cycle while its valve is offline (it is included again by itself when "
                       "back online), or includes it again. Not for a valve last seen open.",
        "fields": {
            "tap": {"name": "Tap", "description": "Tap number (1 = first)."},
            "skip": {"name": "Skip", "description": "On = skip while offline, off = include it again."},
            "config_entry_id": entry_field,
        },
    },
    "snooze_battery_warnings": {
        "name": "Snooze battery warnings",
        "description": "Pauses the low-battery warnings for the valves that are low now; a valve that gets low later "
                       "still warns. They start again when the time is up.",
        "fields": {"hours": {"name": "Hours", "description": "How long (24 = a day). 0 = warn again now."},
                   "config_entry_id": entry_field},
    },
    "send_battery_report": {
        "name": "Send battery report",
        "description": "Sends the battery report (every tap valve's battery level) now.",
        "fields": {"config_entry_id": entry_field},
    },
    "run_tap": {
        "name": "Run tap",
        "description": "Opens one tap as a manual run (any other open tap closes; a running cycle pauses).",
        "fields": {
            "tap": {"name": "Tap", "description": "Tap number (1 = first)."},
            "minutes": {"name": "Minutes", "description": "How long. Default: the tap's longest cycle minutes. "
                                                          "Capped by the safety limit."},
            "config_entry_id": entry_field,
        },
    },
}

selector = {"no_answer": {"options": {"stop": "Stop the cycle", "skip": "Skip the offline tap and carry on"}}}

data = {"title": "Irrigation Scheduler", "config": cfg, "options": opt, "entity": entity, "services": services,
        "selector": selector}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {OUT}")
