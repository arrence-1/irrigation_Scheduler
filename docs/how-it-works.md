# How it works

Irrigation Scheduler is a **custom integration**: Python code that Home Assistant loads like any built-in
integration, plus one JavaScript file for the card and the Irrigation page. It is set up and changed in the
Home Assistant UI. There is no YAML to write (it cannot be set up in `configuration.yaml`), and no automations or
helpers of its own: it creates its own entities.

Its technical name is still `irrigation_sequencer` (from when it was called Irrigation Sequencer): the folder,
the actions (`irrigation_sequencer.run_cycle`, …), the event `irrigation_sequencer_message`, the card type
`custom:irrigation-sequencer-card` and the page address `/irrigation-sequencer`. Home Assistant ties settings,
entities and dashboards to that name, so it stays: renaming it would make it a different integration.

## The pieces

```
custom_components/irrigation_sequencer/
  engine.py          every watering rule, in one function (reconcile). Pure Python, no Home Assistant code,
                     so the rules are tested with a fake clock and fake valves (tests/test_engine.py)
  estimate.py        when the running cycle should finish (the "23 min left" on the card)
  model.py           the settings: their shape, defaults and checks (run order swaps, limits)
  coordinator.py     the Home Assistant side: listens to valves and sensors, runs the engine, opens and closes
                     valves (and checks they did), saves its state, sends notifications
  switch.py number.py sensor.py binary_sensor.py
                     the entities (master switch, cycle switches, seasonal %, status, next cycle, people guard)
  services.py        the actions run_cycle, stop, skip_tap, run_tap
  config_flow.py     the set-up screen and Settings > Devices & services > Irrigation Scheduler > Configure
  websocket.py       what the card and the Irrigation page talk to (read the grid and the settings, save them)
  notify.py          phone notifications (tags, iOS thread, time-sensitive, the "Turn master switch on" button)
  diagnostics.py     the "Download diagnostics" file
  __init__.py        start-up: entities, actions, the card as a dashboard resource, the Irrigation page
  frontend/irrigation-sequencer-card.js
                     the card, its ⚙ settings and the Irrigation page (plain JavaScript, no build step)
  translations/en.json, icons.json, services.yaml, manifest.json, brand/icon.png
```

What happens when: every change Home Assistant reports (a valve, a sensor, a switch, a setting) and a
1-minute watchdog ask the engine "what now?". One request at a time; the state is saved before any valve is
touched, so a restart in the middle never loses track. Nothing polls the valves.

## Where everything is kept

All of it is inside Home Assistant's `/config` folder, so every Home Assistant backup has it.

| What | Where | Changed by |
|---|---|---|
| The code | `/config/custom_components/irrigation_sequencer/` | HACS (updates), or by hand |
| Settings (taps, valves, cycles, minutes, run order, notifications, …) | `/config/.storage/core.config_entries` (the entry's *options*) | the Irrigation page, the card, *Configure* |
| Running state (what is open, time left, waiting cycles, master / cycle switches, seasonal %) | `/config/.storage/irrigation_sequencer.<entry id>` | the integration itself |
| The card as a dashboard resource | `/config/.storage/lovelace_resources` | the integration, at start-up |
| Your dashboards with the card | `/config/.storage/lovelace.<dashboard>` | you |
| Entity names and IDs | `/config/.storage/core.entity_registry` | you (entity settings) |

Don't edit the `.storage` files by hand: Home Assistant rewrites them while it runs.

## Every way to change it

**Settings** (all write to the same place, so they always agree):
1. **Irrigation page** in the sidebar (admins): the grid on top, every setting below. Saves by itself.
2. **The card** on any dashboard: tap a cell or a cycle header for minutes, run order, soak, time and days; ⚙ for
   every other setting.
3. **Settings > Devices & services > Irrigation Scheduler > Configure**: Home Assistant's own screens for the
   same settings. These still work if the card ever fails to load.
4. **Ask Claude**: it can read and change the settings through your Home Assistant connection.

**Day to day** (no settings change): the master switch, each cycle's switch and the seasonal % slider are normal
entities, so automations, scripts, voice assistants and other dashboards can use them. The actions
`irrigation_sequencer.run_cycle`, `stop`, `skip_tap` and `run_tap` do the same from automations and scripts. Every
message is also the event `irrigation_sequencer_message`, for your own automations.

**The code**: change it in the GitHub repository and release a new version ([Updating](updating.md)); HACS
brings it to Home Assistant. A change made in Home Assistant with the File editor or Studio Code Server works,
but the next HACS update overwrites it.

**The card's own options** (title, which Irrigation Scheduler when you have more than one) are YAML in the
dashboard, like any card: `type: custom:irrigation-sequencer-card`, `title:`, `entry_id:`.
