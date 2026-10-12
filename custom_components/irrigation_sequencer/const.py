"""Constants for Irrigation Scheduler."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "irrigation_sequencer"
NAME = "Irrigation Scheduler"
VERSION = "1.3.3"

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.NUMBER, Platform.SENSOR, Platform.BINARY_SENSOR]

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.state"

URL_BASE = f"/{DOMAIN}_static"
CARD_FILE = "irrigation-sequencer-card.js"
CARD_URL = f"{URL_BASE}/{CARD_FILE}"

# the "Irrigation" page in the sidebar (same JS module as the card)
PANEL_URL = "irrigation-sequencer"
PANEL_ELEMENT = "irrigation-sequencer-panel"
PANEL_TITLE = "Irrigation"
PANEL_ICON = "mdi:sprinkler-variant"

# valve states
ON_STATES = ("on", "open", "opening", "closing")
OFF_STATES = ("off", "closed")
CLOSE_TRIES = 3

STARTUP_VALVE_WAIT_S = 120
WATCHDOG_S = 60
FORECAST_CACHE_S = 600

EVENT_MESSAGE = f"{DOMAIN}_message"

STATUS_OPTIONS = ["idle", "watering", "gap", "soaking", "waiting", "held", "manual", "stuck"]

SERVICE_RUN_CYCLE = "run_cycle"
SERVICE_STOP = "stop"
SERVICE_SKIP_TAP = "skip_tap"
SERVICE_RUN_TAP = "run_tap"
