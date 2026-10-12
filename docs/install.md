# Install

You need Home Assistant **2026.9 or newer**, an admin user, and (for the easy way) **HACS 2**.

## With HACS (recommended)

Once per Home Assistant:
1. **HACS** > ⋮ (top right) > **Custom repositories**.
2. Repository: `https://github.com/arrence-1/irrigation_Scheduler`, Type: **Integration** > **Add**.

Then:
3. Find **Irrigation Scheduler** in HACS (that is its name there) > **Download** > the newest version.
4. **Restart** Home Assistant (Settings > System > ⋮ > Restart Home Assistant, or the repair HACS shows).

From then on, a new version appears under **Settings > Updates** like any other update ([Updating](updating.md)).

If you installed it by hand before, do the same: HACS downloads into the same folder and keeps every setting
(settings are not in that folder; see [How it works](how-it-works.md)).

## By hand

1. Download `irrigation_sequencer.zip` from the newest
   [release](https://github.com/arrence-1/irrigation_Scheduler/releases).
2. Unzip it into `/config/custom_components/` with the File editor, Studio Code Server or Samba share app, so
   that `/config/custom_components/irrigation_sequencer/manifest.json` exists.
3. Restart Home Assistant.

HACS does not know about a copy made by hand, so it won't offer updates for it until you download it once
through HACS.

## Add it

**Settings > Devices & services > Add integration > Irrigation Scheduler**
1. A name (`Irrigation` gives `switch.irrigation_master`, `sensor.irrigation_status`, …), the number of taps and
   cycles (you can change both later).
2. A valve for each tap: any `switch`, `valve` or `input_boolean`. For a first test use Toggle helpers (pretend
   valves).
3. Set minutes on the **Irrigation** page in the sidebar or on the card, then turn the **master switch** on.

The card: *Edit dashboard > Add card >* **Irrigation Scheduler**. The integration adds the card to the dashboard
resources by itself.

**Only one thing may drive the valves.** Before you give it the real valves, disable any other automation or
blueprint that switches them (disable, don't delete: they are your fallback). Set each valve's own auto-off
timer as a hardware backstop.

## On another machine

**Moving your whole Home Assistant to new hardware** (the usual case): make a backup on the old machine, then
choose **Restore from backup** when you set up the new one. Everything comes back: the integration's code (and
HACS), its settings and state, the card, your dashboards. Afterwards check that the valves have the same entity
IDs (they do when the Zigbee / Wi-Fi devices came across with the backup) and look at the Irrigation page once.

**A second or fresh Home Assistant** (a test box, a holiday house):
1. Install it with HACS (above), restart, *Add integration*.
2. Pick that machine's valves and set the cycles. Settings don't travel on their own: copy them from the other
   machine's Irrigation page, or from its **Download diagnostics** file (Settings > Devices & services >
   Irrigation Scheduler > ⋮ > Download diagnostics, the `options` part), or ask Claude to copy them across.

## Remove it

1. Turn the master switch off and make sure no valve is open.
2. Settings > Devices & services > Irrigation Scheduler > delete each entry. The last one also removes the card
   resource and the Irrigation page.
3. HACS > Irrigation Scheduler > ⋮ > **Remove**, then restart. Remove the card from your dashboards.
