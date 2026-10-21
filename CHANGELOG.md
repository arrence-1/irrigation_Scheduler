# Changelog

## 1.5.0 (21 Oct 2026) — offline valves and valve batteries

* **Offline valves** (new *Offline valves* tab and Configure screen). Before, any offline valve stopped a cycle. Now
  the cycle **waits** and you get a time-sensitive notification: press and hold for **Skip while offline** (needs
  Face ID / unlock) or **Stop the cycle**. A skipped tap sits every cycle out until it is back online, then it is in
  again by itself (if it comes back reporting open it is closed at once). With no answer after 30 min (*Wait for an
  answer*) the cycle stops, or, with *With no answer: Skip*, it skips the tap and carries on. Also asked 20 min before
  a cycle (*Ask 20 min before a cycle*). A valve last seen open is never skipped (that stays the stuck-tap / main
  water alert). The status sensor shows *offline* (Waiting: valve offline).
* **Valve batteries** (new *Batteries* tab). Each tap's battery sensor is found on its valve's device (or pick one
  under the tap). **Low battery warning** at or below 30 %, repeated every 2 h until replaced (fine again 5 % above),
  held back during the **quiet time** (21:00-06:00); press and hold to **Snooze 1 day** or **Snooze…** (type 12, 2d,
  1 week). A valve that gets low later still warns. **Battery report**: every valve's level every 14 days at 10:00
  (days, time and a first date are settings), plus *Send report now*. It reads: the lowest valve first, then one
  tap per line, then *Next report: Wed 4 Nov 10:00*. All levels, times and repeats can be changed.
* New actions `irrigation_sequencer.skip_offline_tap`, `snooze_battery_warnings` and `send_battery_report`; status
  attributes `offline`, `offline_skipped`, `offline_waiting`, `offline_until`, `battery`, `battery_low`,
  `battery_snoozed_until`, `battery_report_next`.
* **Card:** Off cells keep their run order (faint "#2"), so the positions stay visible. *Tap settings* now hides the
  settings when pressed again. Rows for an offline valve (*Skip while offline*, tap twice) and low batteries (*Snooze
  12 h / 1 day / 3 days*); the tap panel shows the battery % and *Skip while offline* / *Include again*.

## 1.4.0 (21 Oct 2026) — main water valve

* **Main water** (new settings tab and Configure screen): pick the valve that supplies every tap (a switch, valve,
  toggle helper or light: some relays show as a light). On = water flowing.
* **A tap that won't close:** the app still retries it every 5 min and nothing else opens. Once it has been stuck
  for the set time (default 10 min) you get a **critical alert** (sounds even on silent) with a **Close main water**
  button: press and hold the notification. With *Close the main water automatically* on, the app closes it itself
  and tells you.
* **While the main water is off** (closed by the app or by hand) cycles wait, and a running tap stops and keeps its
  time; they carry on, late, when it is on again. The status shows *Held*.
* **Reminders while the main water is off:** every 30 min (*While it is off, remind me every*; 0 = never) until it
  is on again, with **Open main water**, **Snooze 1 h** and **Snooze…** (type the hours: 3, 1.5, 90m) when you
  press and hold. When the snooze ends the reminders start again. The card has *Snooze 1 h / 4 h / 12 h* and
  *Resume reminders*; the action `irrigation_sequencer.snooze_main_water_reminders` does the same.
* **Before a cycle:** 20 and 10 min before a cycle that would run while the main water is off, a reminder with an
  **Open main water** button (*Remind me before a cycle*; also while snoozed).
* **The app never opens the main water by itself.** Once the stuck tap reports off you get a notification with an
  **Open main water** button (needs the phone unlocked), and the card offers the same.
* New actions `irrigation_sequencer.close_main_water`, `open_main_water` and `snooze_main_water_reminders`; the status
  sensor has `main_water` (on / off / offline) and `main_snoozed_until` attributes.
* **Card:** a row with the Close / Open main water button when a tap is stuck or the main water is off (tap twice:
  the first tap arms it for 5 s). The status pill says *Held* for both the bypass and the main water.
* The card's address carries a fingerprint of the card file as well as the version (`?v=1.4.0-…`), so a changed card
  always loads fresh after a restart, also while testing a build that keeps its version number.
* Tests: 13 new engine scenarios, 3 new Home Assistant tests, and card checks without a browser (jsdom) in CI.

## 1.3.3 (13 Oct 2026) — renamed Irrigation Scheduler

* **Called Irrigation Scheduler everywhere you see it:** Settings > Devices & services, the set-up and Configure
  screens, the device (maker and model), the card picker, the card's and the page's messages, error messages,
  the README and the notes. (HACS already showed it since 1.3.2.)
* **Nothing to redo:** the technical name stays `irrigation_sequencer`, so your settings, entities, dashboards
  (`custom:irrigation-sequencer-card`), automations using the actions and the Irrigation page keep working.

## 1.3.2 (13 Oct 2026) — called Irrigation Scheduler in HACS

* HACS lists it as **Irrigation Scheduler** (`hacs.json`). Nothing else changes: the integration, its entities,
  settings and card are the same, and Home Assistant still calls it Irrigation Sequencer
  (domain `irrigation_sequencer`).

## 1.3.1 (12 Oct 2026) — on GitHub, installed and updated through HACS

* **No change to how it waters, the card or the settings.** This release packages it for GitHub and HACS.
* `manifest.json` links to the GitHub repository (documentation, issue tracker, code owner); HACS needs them.
* Tested on Home Assistant 2026.10.1 (96 tests pass).
* The tests now also fail when Home Assistant warns that something this integration uses is going away
  ("Detected that custom integration … will stop working in …"). Home Assistant gives these warnings months
  ahead, so the weekly test against the newest Home Assistant flags a coming break long before it happens.
* GitHub Actions: Home Assistant's *hassfest* and the HACS checks; the tests on every change and every Monday
  against the newest Home Assistant; releases published with *Actions > Release > Run workflow* (or a version
  tag), with a zip for manual installs.
* New tools: `tools/bump_version.py` (set the version everywhere), `tools/release_check.py` (versions match,
  release notes from this file), `tools/set_repo.py` (point the links at your repository).
* New notes in `docs/`: how it works and where everything is kept, install (also on another machine),
  backup, and updating (including what to do if a Home Assistant update breaks it).

## 1.3.0 (11 Oct 2026) — no more Save buttons, time left

* **Changes save by themselves.** Card editors (cell: minutes / run order / soak; cycle: time / days) save each
  change after a short pause, so a few quick taps make one save; **Undo** goes back to how it was when opened,
  **Done** closes. Saved run-order swaps say which tap moved ("Saved · Tap 1 moved to #2").
* **Settings (page and ⚙) save by themselves** too: switches and pickers at once, typed boxes when you pause or
  leave them; the bottom line says "Saving…" / "All changes saved" (or what went wrong, with *Try again* / *Undo*).
  *Taps & cycles* still has Save / Revert because it reloads the app.
* **Time left in the status pill** while a cycle runs ("Watering · 23 min left", only "23 min left" on a phone)
  or during a manual run ("Manual run · 4 min left"); it counts down between updates.
* New status attribute `cycle_ends_at`: when the running cycle should finish watering (estimate from run order,
  gaps, safety limit, cycle-and-soak; people waits and bypass holds can only make it later).
* Fix: disabling or reloading an entry after Home Assistant had started logged "Unable to remove unknown job
  listener" (harmless, but an error in the log).

## 1.2.1 (11 Oct 2026) — which Irrigation Sequencer you are looking at

* **Irrigation page:** with more than one Irrigation Sequencer (e.g. *Irrigation* and *Irrigation Test*), each has
  its own button at the top of the page, and the one shown is highlighted (was a small drop-down in the title bar).
  The page opens on the first by name unless you picked another, and only a pick you make is remembered.
* **Settings heading names the Irrigation Sequencer** ("Settings · Irrigation Test"), on the page and on the card.
* Fix: after changing the number of taps or cycles, a card or settings screen that was already open (another tab,
  another phone) kept showing the old taps until the page was reloaded. The settings version now never repeats
  after a reload, so they refresh by themselves.

## 1.2.0 (11 Oct 2026) — tap panel and next start

* **Tap names on the card are clickable.** A tap panel opens under the grid: the valve and its state, which
  cycles use the tap (minutes, run order, soak), **Run now** for 1–30 min (capped by the safety limit; a manual
  run, so any other open tap closes and a running cycle pauses), **Stop this manual run** / **Skip this tap**
  while it runs, **Valve details** (Home Assistant's own dialog) and **Tap settings** (admins: opens that tap in
  the settings, on the card or on the Irrigation page).
* **Next start everywhere on the card:** each cycle header says when it runs next (today / tomorrow / weekday);
  the status line says "Next: Cycle 1 · tomorrow 06:00 · in 8 h 15 min" and, with the master switch off,
  "so it won't run". When nothing is scheduled it says why.
* The next-cycle sensor has a new `reason` attribute: "No cycle has minutes yet", "The cycles with minutes are
  switched off" or "No days are selected for the cycles with minutes" (empty when a cycle is scheduled).
* The cycle switches and ▶ buttons sit in their own header row, so they line up whatever each header shows.

## 1.1.0 (11 Oct 2026) — settings where you can find them, clearer card

* **Irrigation page in the sidebar** (admins): the grid on top and every setting below, in tabs — Taps, General,
  Notifications, Cycle-and-soak, People guard, Rain & weather, Number of taps & cycles. Same labels, help texts
  and limits as the Configure screens (which still work). Several Irrigation Sequencers: pick one at the top.
* **⚙ on the card** opens the same settings right on the dashboard, with a link to the page. A status update
  never wipes a form you are filling in; unsaved edits are kept per tab (marked •) until Save or Revert; an open
  settings screen refreshes itself when settings are changed elsewhere.
* **Run order: every tap has its own number.** Picking a number another tap has swaps the two (card and
  Configure screen); the card's run-order list shows which tap has each number. On the Configure screen a full
  new order is accepted as typed; two or more changed numbers that clash are sent back with a message instead
  of being saved. Settings saved by 1.0.0 with shared numbers are renumbered on load, keeping the same sequence.
* **Card colours:** the cycle in progress is blue (RUNNING / PAUSED tag, tinted cells, the watering tap solid,
  finished taps dimmed with ✓); what is about to run is amber (NEXT tag on the waiting or next scheduled cycle,
  an outline and "next" on the next tap). A legend explains them. The status sensor has new `next_tap` /
  `next_tap_name` attributes (the engine's own choice, soak breaks included).
* Fix: the card could keep showing an old grid when several settings changes arrived close together.
* Fix: a failure in the YAML-mode card fallback can no longer stop the integration from loading.

## 1.0.0 (10 Oct 2026) — first version as a custom integration

Ported from the *Irrigation Sequencer* blueprint v1.5.1. Every blueprint rule is kept; what changed:

* **An integration, not an automation**: config flow (name, tap and cycle counts, valves) and a settings screen
  per group; each tap gets its own box on the cycle screen.
* **Own entities instead of helpers**: master switch, a switch per cycle, seasonal % slider, status and
  next-cycle sensors, a people-guard sensor per tap. No text / date helpers to create; the run record and
  schedule memory are saved as versioned JSON in `.storage`.
* **Editable dashboard card**: taps x cycles grid, Off cells greyed, tap a cell / cycle header to edit; live status,
  run / stop / skip, seasonal −/+. Registered as a dashboard resource automatically.
* **Bypass switch per tap** (pressure washer): the tap is left alone and cycles are held until it is off.
* **Actions**: `run_cycle`, `stop`, `skip_tap`, `run_tap`.
* Limits raised to 16 taps and 12 cycles.
* One event-driven worker replaces the every-minute planner template (a 1-minute watchdog remains as backstop);
  the start-up valve wait is event driven.
* Every message is also fired as the event `irrigation_sequencer_message`.
* Unit handling: °F temperature and m/s / mph wind sensors are converted.
* Started-raining alert is debounced (re-armed after 30 min dry); too-hot alert re-arms 1 °C below the limit.

Tested: 85 automated tests (60 engine scenarios in a simulator, 25 against real Home Assistant core with Toggle
helpers as valves), passing on HA 2026.9.4 and 2026.10.0, plus a live smoke test (install flow, settings screens
and the card in a real 2026.9.4 with its frontend). See TESTING.md. Not yet run against real valves.
