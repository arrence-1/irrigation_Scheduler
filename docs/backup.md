# Backup

## What there is to keep

| What | Kept in | Backed up by |
|---|---|---|
| The code, every version | this GitHub repository (and a zip on every release) | GitHub |
| The settings (taps, valves, cycles, notifications, …) | Home Assistant's `/config/.storage` | Home Assistant backups |
| The running state, the master and cycle switches, seasonal % | Home Assistant's `/config/.storage` | Home Assistant backups |
| The installed code, the card resource, your dashboards | Home Assistant's `/config` | Home Assistant backups |

So **a normal Home Assistant backup already contains all of it**: the "Home Assistant settings" part of a backup
is the whole `/config` folder, which holds `custom_components/irrigation_sequencer/` and `.storage/`. Nothing
extra to set up for this integration.

## Make sure your Home Assistant backups are good

**Settings > System > Backups**:
* **Automatic backups on**, daily, keeping a few (3–7).
* **Back up before updates** on (Home Assistant asks when you update; set it as the default).
* **At least one copy off the machine.** A backup that only lives on the Home Assistant box dies with its disk.
  Add a location under *Backup locations*: a NAS (Settings > System > Storage > *Add network storage*, usage
  *Backup*), Google Drive or OneDrive (their integrations), Cloudflare R2, or Home Assistant Cloud (keeps only the
  newest backup).
* **Keep the encryption key** (download the emergency kit) somewhere that is not the Home Assistant box, e.g. your
  password manager. A backup can't be restored without it.

## Getting it back

* **Whole machine lost or replaced:** set up Home Assistant, choose *Restore from backup*.
* **Something went wrong after a change:** restore a backup from before it (Settings > System > Backups > the
  backup > *Restore*). A restore puts back the whole `/config` folder, not just this integration.
* **Only the integration's code** (an update misbehaves): no backup needed. HACS > Irrigation Scheduler > ⋮ >
  *Redownload* > *Need a different version?* > the previous one > restart. Settings stay as they are.
* **Only the settings:** they can be read from a backup or from a *Download diagnostics* file
  (Settings > Devices & services > Irrigation Sequencer > ⋮), and entered again on the Irrigation page.

Don't copy files out of `.storage` while Home Assistant is running, and don't put them back by hand: use a
backup restore.
