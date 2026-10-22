# Test report — Irrigation Scheduler (1.0.0 on 10 Oct 2026; later versions at the end)

**Result: 85 / 85 automated tests pass on Home Assistant 2026.9.4 (your version) and on 2026.10.0** (the
latest release), plus a live smoke test in a real Home Assistant 2026.9.4 with its frontend. Nothing was
installed on your Home Assistant; it was only read (version, entity names) to match your setup.

## 1. Engine scenarios (60 tests, simulator)
`tests/test_engine.py` drives the engine with a fake clock and fake valves (`tests/sim.py`) and checks after
**every step that no two valves are open** (only a person opening a tap by hand can briefly make two; the app
closes the other at once).

| Spec checklist item | Test |
|---|---|
| Cycle runs taps in run order, one at a time, 5 s gap | `test_cycle_runs_in_run_order_with_gap` |
| Two cycles follow their own order | `test_two_cycles_follow_their_own_order` |
| Manual tap before a cycle: its timer, then the cycle starts "late" | `test_manual_before_cycle_runs_its_timer_then_cycle_starts_late` |
| Manual tap mid-cycle: pause, resume with remaining time | `test_manual_mid_cycle_pauses_and_resumes_with_remaining` |
| Second manual tap closes the first | `test_second_manual_tap_closes_the_first` |
| Scheduled tap turned off by hand: skipped | `test_scheduled_tap_turned_off_by_hand_is_skipped` |
| Restart mid-tap (end ahead): stays on, no blip, "Recovered" | `test_restart_mid_tap_end_still_ahead` |
| Restart past a tap's end: closed, cycle continues | `test_restart_past_the_end_closes_and_continues` |
| HA down over one / two start times: run once, back to back | `test_down_over_one_and_two_start_times_runs_missed_once` |
| Master off mid-cycle, reminders 20 / 10 min, cleared | `test_master_off_mid_cycle_cancels_and_reminds`, `test_reminder_cleared_when_cycle_time_passes` |
| Cycle-and-soak fills soak time; total = set minutes | `test_cycle_and_soak_fills_soak_time_with_other_taps`, `test_two_soak_taps_interleave` |
| Tap over the safety limit runs in parts | `test_tap_over_safety_limit_runs_in_parts` |
| Seasonal 120 % / 0 % | `test_minimum_60_seconds_and_seasonal_120`, `test_seasonal_zero_skips_and_notifies` |
| Weather skip with each check, and with the switch off | `test_weather_skip_each_check` (7 cases), `test_weather_skip_lists_every_reason_and_switch_off_waters` |
| Soil: wet tap sits out, lawn waters, unavailable waters + warning | `test_soil_per_tap`, `test_soil_every_tap_wet_skips_cycle` |
| People guard: held back, paused + resumed, skipped after give-up | `test_people_guard_holds_back_pauses_resumes_and_gives_up`, `test_people_guard_off_and_manual_option` |
| Valve that won't close: nothing else opens, retries, resumes | `test_valve_that_wont_close_blocks_everything_and_resumes` |
| Never two valves open | checked in every scenario; `test_long_random_soak_with_invariant` (6 taps x 4 cycles, manual runs, a restart, 25 h) |

Also: days filter, 60 s minimum, equal run order warning, manual timer cap, missed > 24 h ignored, first start
and time edits cause no surprise runs, corrupt state, no doubling, cycle switch off, manual runs with master off,
valve offline while open, other valve offline (cycle stops), valve won't report on, unconfigured tap, bypass
(hold, mid-cycle pause, tap still on after bypass), services, hot-day boost, too-hot alert, rain alert debounce,
daylight-saving change days (Sydney, both directions), status values.

## 2. Home Assistant tests (25 tests, real HA core, Toggle helpers as valves)
`tests/test_ha.py`, `tests/test_ha_more.py` with `pytest-homeassistant-custom-component` (0.13.367 = HA 2026.9.4,
0.13.371 = HA 2026.10.0) and time travel:
install flow and every settings screen (incl. clearing optional fields, changing counts → reload); full cycle
with the exact notification payloads (title, tag, group, iOS thread-id, interruption level); status attributes;
manual run timer + pause / resume; reload mid-tap; HA down over two start times; master off → reminders with
the *Turn master switch on* button → the phone action turns it on → `clear_notification`; all four actions;
stuck valve (time-sensitive alert, nothing else opens, recovers); bypass hold with `input_boolean`; forecast skip
via `weather.get_forecasts`; °F sensor converted; card websocket edits applied live; entity IDs; state file
removed with the entry; two entries; `valve.*` entities (open / closed); **HA start-up** waiting for an offline
valve, then "Recovered after restart"; people / soil / external seasonal % read from live states; rain alert from
a flickering sensor; switches + slider survive a reload; corrupt saved state; diagnostics; card resource
registration. No deprecation warnings logged on either version; `ruff` clean.

## 3. Live smoke test (real HA 2026.9.4 + frontend, throwaway instance in the workspace)
Six Toggle helpers named like your Sonoff valves. Install flow via the REST API, grid edited through the
card's websocket API, a dashboard with the card, Chromium (Playwright) screenshots in `docs/screenshots/`.

Found and fixed by this test (the unit tests could not see them):
1. **Card "Custom element doesn't exist" on HA 2026.8+**: the new frontend registry ignored a card injected
   early. Now the integration registers the card as a dashboard resource (as other integrations had to).
2. **"Translation error: UNCLOSED_TAG"** on the cycle screen (a `<name>` in the help text). Fixed; tap boxes
   now show the tap names ("Tap 1 · WS Backyard - Left (9388)").
3. Card times followed the browser's time zone; now always your HA's time zone. Long tap names pushed Cycle 4
   off-screen; names now wrap so all 4 cycles fit, also at phone width.

Seen working live: run order (Tap 2 #1 first), manual run on Tap 4 pausing the cycle with its 10 min timer,
cell editor save round trip, cycle editor, dark theme, phone width, settings menu, cycle and tap screens.

## Not tested yet
Real valves (Sonoff SWV via ZHA), real phone notifications, a real HA restart on your box. Do that next with
short durations, after turning the old irrigation automations off (see README).

## 1.1.0 (11 Oct 2026)
**Automated: 90 tests** (all of the above plus): run-order helpers (renumbering old shared numbers keeping the
sequence, swap, the Configure screen's full-order / single-change / clash cases); the card's swap over the
websocket (never two taps with one number); the Configure screen sending a clash back with what was typed and
saving nothing; `next_tap` following run order and soak breaks; the settings websocket for every group (unknown
fields dropped, cleared fields, values clamped, applied live, count change reloads with new entities, not found,
admin only while the grid stays readable); the sidebar page added once with the first entry (admins only) and
removed with the last. Linted with ruff (no new findings).

**Live (HA 2026.9.4 + frontend, Chromium):** card colours while a cycle runs with another waiting (light, dark,
phone), idle with the next scheduled cycle, the run-order list and swap hint, swap saved; the Irrigation page in
the sidebar (desktop, dark, phone 390 px): every tab renders its fields with the Configure screen's labels, a
change saved through the form is applied and the grid on the page follows; the card's ⚙: a draft survives
status updates, Revert, the link to the page.

Found and fixed by the live test:
1. The card kept an old grid when several settings changes arrived before its first update (now compared with a
   settings version sent with the grid).
2. An open settings screen kept old values after a change made elsewhere (now refreshes, keeping unsaved edits).
3. The page was wider than a phone screen (the grid now scrolls inside its card); tabs wrap instead of scrolling.

## 1.2.0 (11 Oct 2026)
**Automated: 91 tests** (plus the next-cycle `reason` for no minutes, cycles switched off, no days, scheduled).
**Live (HA 2026.9.4 + frontend, Chromium):** next-start labels in the cycle headers and the status line (master
on / off, cycles switched off); tap panel: Run 1 min started the tap as a manual run, Stop ended it, Valve details
opened HA's more-info dialog, Tap settings opened that tap in the card's settings and, on the Irrigation page, in
the page's settings; phone width; colours and run-order swap re-checked. Found and fixed by this test: the cycle
switch buttons no longer lined up once some headers had a "next" line (now their own header row).

## 1.2.1 (11 Oct 2026)
**Automated: 91 tests** (the settings test now also checks that a tap / cycle count change gives the reloaded
entry a new settings version and that the grid then has the new tap).
**Live (HA 2026.9.4 + frontend, Chromium):** with a second entry "Irrigation Test": both buttons on the page,
"Irrigation" shown first even with 1.2.0's stored automatic pick, picking "Irrigation Test" switches grid and
settings ("Settings · Irrigation Test", 2 tap chips) and is remembered after a reload. Tap count 6 → 7 saved on the
page: a dashboard card open in another tab showed the 7th row without a reload, and the page settings showed 7 tap
chips; back to 6 the same way. Phone width: buttons wrap, no sideways scroll. The second entry was removed again.

## 1.3.0 (11 Oct 2026)
**Automated: 96 tests** (plus: unload after HA started / before it finished starting leaves no listener error;
this test fails on 1.2.1). New: `cycle_end_estimate` compared with the real end of watering at many points of
three simulated cycles (plain with gaps, one tap with cycle-and-soak, two soaking taps) and with a manual run in the
middle; the status sensor's `cycle_ends_at` on a real HA run with soak (within 2 s of the last valve closing, and
empty when idle).
**Live (HA 2026.9.4 + frontend, Chromium):** cell editor: chip 5 saved without a button; + + + quickly = one save
of 8 min; run-order pick saved with "Saved · Tap 1 … moved to #2"; Undo restored minutes and both taps' numbers;
Done closed it. Cycle editor: two day taps = one save; time saved; Undo restored both. Pill: "Watering · 5 min
left" during a run (cycle_ends_at matched), "5 min left" at phone width, "Manual run · 2 min left", "Idle" after.
Irrigation page settings: a typed tap name saved after a pause and again on leaving the box, a number box saved,
a switch saved within a second (one save each), Taps & cycles not saved without Save. Settings put back after.

## 1.3.1 (12 Oct 2026)
No change to the integration's behaviour; this release puts it on GitHub for HACS.
* **96 / 96 tests pass on Home Assistant 2026.10.1** (`pytest-homeassistant-custom-component` 0.13.372, the newest
  on 12 Oct 2026; Python 3.14.6).
* New in the tests: a test fails when Home Assistant logs that something this integration uses is going away
  ("Detected that custom integration 'irrigation_sequencer' … will stop working …"), and a Python
  `DeprecationWarning` raised from the integration's own code is an error. Checked both ways with a throwaway test
  (trips on such a log line, stays quiet on a normal warning). None are raised today.
* **hassfest** (Home Assistant 2026.10.1's own checker, run locally): 1 integration, 0 invalid.
* HACS checks read from the HACS source: the local `brand/icon.png` satisfies the brand check; `hacs.json` keys
  are valid; manifest has documentation / issue_tracker / codeowners.
* GitHub workflows checked with `actionlint` 1.7.12. Release steps tried locally: `release_check.py v1.3.1` prints
  the notes, `release_check.py v1.3.2` refuses (versions and CHANGELOG don't match); the zip holds the 27 files of
  `irrigation_sequencer/`.
* `ruff check` (rules E4, E7, E9, F) clean; two findings in the tests fixed (an unused import, a lambda).
* A real install went from Home Assistant 2026.9.4 to 2026.10.1 on 11 Oct with 1.3.0 on it: the integration loaded
  with no errors in the log.

## 1.3.2 (13 Oct 2026)
Only `hacs.json` (name) and the version strings changed. 96 / 96 tests on Home Assistant 2026.10.1, `release_check`
OK, `hacs.json` checked against the HACS schema.

## 1.3.3 (13 Oct 2026)
Display name only ("Irrigation Sequencer" → "Irrigation Scheduler"; the technical name `irrigation_sequencer` is
unchanged). 96 / 96 tests on Home Assistant 2026.10.1, hassfest 0 invalid, `ruff` clean, the card parses
(`node --check`), `translations/en.json` regenerated (only the titles changed), `release_check` OK.
Card address fingerprint: `_card_tag()` gives `1.3.3-a2fc7506`; the resource and the Irrigation page both use
it (the two tests that check the address now expect it).

## 1.4.0 (21 Oct 2026) — main water valve
* **111 / 111 tests pass on Home Assistant 2026.10.1** (was 96). New engine scenarios: stuck tap → critical alert
  after the wait (once) → *Close main water* pressed → tap fixed → *Open main water* prompt, never re-opened by the
  app, cycle waits and carries on after you open it; automatic close; main water closed by hand answers the alert;
  main water off holds cycles (run late) and pauses a running tap (keeps its time); no main valve = no change;
  requests when already there; old saved state loads. Reminders: every 30 min while off (08:35, 09:05), snoozed
  2 h at 09:10 (none until 11:10, then again), cleared from the phone when the water is back on; 20 / 10 min before a
  cycle even while snoozed (cleared at the start time); reminders off / switched on later / "remind again now";
  while a tap is stuck the reminder has no Open button; opened by the app = no extra clear.
* New Home Assistant tests: the Main water Configure screen (set, then cleared), the settings websocket group
  (clamped like the screen), the alert's payload (critical sound, `irrigation_main` tag, *Close main water*
  destructive button), the phone's action closing a **light** entity, the *Open main water* prompt (needs unlock),
  status *held* while it waits, the prompt's Snooze 1 h / Snooze… (text input) buttons, a typed "2.5" snoozing and
  "soon" refused, all three actions (`close_main_water`, `open_main_water`, `snooze_main_water_reminders`, error
  without a valve), `main_snoozed_until` set and cleared; parsing typed hours (3, 1,5, 2h, 90m, 4 hours).
* Card (jsdom, also in CI): stuck → *Close main water* row, two-tap arming, service called with the entry; main off →
  *Open main water*, pill *Held*; stuck with the water already off → no button; all fine → no row; the settings'
  *Main water* tab with valve (light allowed) / wait / auto-close / remind every / remind before a cycle; snooze
  chips 1 h / 4 h / 12 h call the action, "Reminders snoozed until …" with *Resume reminders*.
* hassfest 2026.10.1: 0 invalid. `ruff` clean.

## 1.5.0 (21 Oct 2026) — offline valves and valve batteries
* **131 / 131 tests pass on Home Assistant 2026.10.1** (was 111). New engine scenarios: a valve offline 20 min before
  a cycle → asked ahead (once) → *Skip while offline* → the cycle waters the rest on time, "Skipped while offline"
  → back online = in the cycles again; offline at the start → the cycle waits (status *offline*), one question with
  the deadline → skip from the notification carries on at once, or the valve coming back carries on with it, or no
  answer → stops at 06:30 (or, with *no answer: skip*, skips and carries on); a valve last seen open can't be skipped
  and the cycle stops; a skipped valve coming back open is closed at once (not taken as a manual run); *Stop the
  cycle*; include again / nothing to skip; the question cleared when the valve is back; old saved state loads.
  Batteries: warning at 30 % repeats every 2 h, held in the quiet time (18:00, 20:00, then 06:00, 08:00); snooze
  1 day, a new low valve breaks through, 32 % stays low (fine again from 35 %), fine again stops it; warn once
  (repeat 0) and off (0 %); report every 14 days from the first day (10:00), on request, a start date + new time
  (no report for the past), report off; "no battery sensor" on request; the offline question shows the last level.
* New Home Assistant tests: the *Offline valves* and *Batteries* Configure screens (start date cleared), the tap's
  battery sensor field, the settings websocket groups (clamped, bad date → empty), the battery sensor found on the
  valve's device (ZHA first, not Battery Notes' copy, not the signal sensor) or picked by hand; the offline question
  payload (time-sensitive, *Skip while offline* with unlock, *Stop the cycle*), the phone's skip carrying on, back
  online; the stop button and `skip_offline_tap` (errors, include again, online tap); the low-battery warning
  (held in the quiet time until 06:00, *Snooze 1 day* / *Snooze…* buttons), typed "2d" and "?" replies,
  `snooze_battery_warnings` 0, `send_battery_report`, fine again with new batteries; typed days and weeks.
* Card (jsdom): Off cell shows "#2" faintly; pill *Waiting: valve offline*; offline row with two-tap *Skip while
  offline*; battery row with 12 h / 1 day / 3 days (and *Snoozed until … Resume*); tap subtitle "offline · battery
  22%"; tap panel battery % and *Include again*; *Tap settings* → "Hide tap settings" → hidden again; the *Offline
  valves* / *Batteries* tabs and *Send report now*. Also looked at in Chromium at phone width.
* hassfest 2026.10.1: 0 invalid. `ruff` clean. `release_check 1.5.0` OK.

## 1.6.0 (22 Oct 2026) — skip watering, forecast needs both
* Engine: forecast both (95 %/20 mm and 70 %/5 mm skip; 70 %/1 mm and 30 %/9 mm don't, with the note naming the
  limit that was met; 20 %/0 mm quiet), one limit set still skips alone, no forecast noted; skip next (only the next
  one), skip 24 h (both cycles, then runs again), skip stops the running cycle (counts as the skipped one), Run now
  still runs, cancel / nothing to cancel, skip kept across a restart. Home Assistant: phone buttons
  (`irrigation_skip_next`, typed "2d", "rain" refused, `irrigation_skip_24`), the actions, the attributes
  (`skip_next`, `skip_until`, `next_cycle_skipped`, `forecast_check`), the note's tag and title, the Rain & weather
  screen's *Forecast: need both*. Card (jsdom): skip row + *Run as scheduled*, "will be skipped" on the line, *Skip
  next cycle* button, the setting.
