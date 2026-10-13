# Updating

## How a new version gets to Home Assistant

Test on Home Assistant first, publish after:

```
1. change   Claude changes the code on the `dev` branch and runs the tests (dev is pushed to GitHub as a
            backup; HACS never looks at it)
2. test     Claude copies the integration from `dev` straight into Home Assistant
            (/config/custom_components/irrigation_sequencer/) and restarts it when nothing is watering.
            You try it. Not right? Back to 1.
3. publish  When you're happy: Claude moves it to `main`, you press Actions > Release > Run workflow
4. HACS     offers that version under Settings > Updates. It is the code you already tested; install it
            (and restart once) so HACS knows which version you have.
```

`main` only ever holds versions you have tested, and HACS installs releases from `main`, never `dev`.

The card's address carries the version and a fingerprint of the card file (`…card.js?v=1.3.3-a2fc7506`), so a
changed card loads fresh after the restart, even between two tests of the same version. If a phone still shows the
old card: Companion app > Settings > Debugging > *Reset frontend cache*.

HACS looks at GitHub for a custom repository when Home Assistant starts and then every 48 hours. To look now:
HACS > Irrigation Scheduler > ⋮ > *Update information*.

Restart when nothing is watering (the status says *Idle*). A restart mid-run is handled (the tap stays on and
closes on time, "Recovered after restart"), but idle is tidier.

**Going back a version:** HACS > Irrigation Scheduler > ⋮ > *Redownload* > *Need a different version?* > pick the
older one > restart. The settings are not touched.

## Making a release

**Asking Claude** (with GitHub connected to Claude): describe the change. Claude changes the code on `dev`, runs
the tests, sets the version, writes the CHANGELOG entry and copies it into Home Assistant for you to test. Once you
say it's good, Claude merges it into `main`, and **you** publish it: on GitHub, *Actions > Release > Run workflow >
Run workflow* (leave the box empty). Claude's session can push code but can't create tags, so no release happens
until you press that button.

**By hand on github.com:**
1. Change the files (the pencil icon on a file, then *Commit changes*). Change the version in
   `custom_components/irrigation_sequencer/manifest.json`, `const.py` (`VERSION`) and
   `frontend/irrigation-sequencer-card.js` (`CARD_VERSION`): all three the same. Add a `## 1.3.2 (date)` entry at
   the top of `CHANGELOG.md`.
2. *Actions > Release > Run workflow*. (Or *Releases > Draft a new release*, tag `v1.3.2`, *Publish release*: the
   release workflow then adds the zip.)

**On a computer with git:**
```bash
python tools/bump_version.py 1.3.2      # sets all three version strings, starts the CHANGELOG entry
# write the CHANGELOG entry, then:
python tools/release_check.py 1.3.2     # versions match, entry not empty
pytest -q && ruff check .
git commit -am "Release 1.3.2" && git tag v1.3.2 && git push && git push --tags   # the tag starts the release
```

A release workflow that fails (versions differ, empty CHANGELOG entry, a failing test) publishes nothing, so HACS
never offers a broken build.

## When Home Assistant updates

Home Assistant has a big release on the first Wednesday of each month and small fixes (.1, .2, …) in the weeks
after. Each can change things custom integrations use.

**What the integration relies on, and what happens if it breaks:**

| Part | Uses | If a Home Assistant update breaks it |
|---|---|---|
| Watering (engine, coordinator, entities, actions) | the standard, documented integration parts: entries, entities, events, timers, storage | Home Assistant warns **months ahead** in its log ("custom integration 'irrigation_sequencer' … will stop working in …"). The scheduled tests fail on that warning and open a GitHub issue, so it gets fixed before the update that removes it. |
| The card, its settings and the Irrigation page | Home Assistant's frontend: its form fields, dashboard resources, sidebar pages | Frontend changes come with less warning (2026.8 changed how cards load; fixed in 1.0.0). **Watering carries on**; only the screen is affected. Use Settings > Devices & services > Irrigation Scheduler > *Configure* meanwhile. |
| Hassfest / HACS rules | manifest, translations, services | The weekly *Validate* check fails and opens an issue. Nothing changes in your garden. |

**What protects the garden if the integration doesn't load at all:** nothing schedules and nothing closes a tap
that is already open. So: set each valve's own **auto-off timer** (hardware backstop), keep the old irrigation
automations **disabled, not deleted** (re-enable them to take over), and check the Irrigation page after a Home
Assistant update.

**Before updating Home Assistant:**
1. Look at the repository: a green *Tests* badge and no open "Scheduled test failed" issue mean it passed against
   the newest Home Assistant (the tests run every Monday and Thursday).
2. Prefer the .1 release over the .0 of a new month: most breakage is found and fixed in the first days.
3. Let Home Assistant make a backup before the update.

**After updating:** Settings > System > Logs, search for *irrigation*; open the Irrigation page (on a phone, if the
card looks old or blank: Companion app > Settings > Debugging > *Reset frontend cache*).

**If it is broken:**
1. Master switch off, and make sure no valve is open (turn them off by hand if needed).
2. Go back: restore the backup from before the update, or update to a fixed version of the integration once there
   is one.
3. If watering can't wait, re-enable the old irrigation automations.
4. Ask Claude with the log lines (or open an issue in the repository): fix, test against the new Home Assistant,
   release; HACS brings it in.

## Keeping the tests current

`requirements_test.txt` pins the Home Assistant version the current release was tested on
(`pytest-homeassistant-custom-component` 0.13.372 = Home Assistant 2026.10.1). When a scheduled run against the
newest Home Assistant passes, bump that pin with the next release.
