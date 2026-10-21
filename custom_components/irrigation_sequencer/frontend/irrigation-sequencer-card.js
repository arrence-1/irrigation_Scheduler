/*
 * Irrigation Scheduler card
 * A taps x cycles grid: each cell = minutes / run order / soak, "Off" cells greyed.
 * Tap a cell to edit it, tap a cycle header to edit its time and days.
 * Live status, master switch, seasonal %, run / stop buttons.
 * Served by the irrigation_sequencer integration (no build step, no dependencies).
 */
const CARD_VERSION = "1.5.0";
const DOMAIN = "irrigation_sequencer";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_SHORT = ["M", "T", "W", "T", "F", "S", "S"];
const STATUS_TEXT = {
  idle: "Idle", watering: "Watering", gap: "Between taps", soaking: "Soaking",
  waiting: "Waiting for people", offline: "Waiting: valve offline", held: "Held", manual: "Manual run", stuck: "Stuck",
};
const QUICK = [0, 1, 2, 3, 5, 10, 15, 20, 30];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Times are shown in Home Assistant's own time zone: the cycle start times are server-local.
let TZ;
const hhmm = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: TZ });
};
const weekday = (iso) => new Date(iso).toLocaleDateString([], { weekday: "short", timeZone: TZ });
const ymd = (d) => d.toLocaleDateString("en-CA", { timeZone: TZ }); // YYYY-MM-DD in HA's time zone
const dayWord = (iso) => { // "today" / "tomorrow" / "Mon"
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const now = new Date();
  if (ymd(d) === ymd(now)) return "today";
  if (ymd(d) === ymd(new Date(now.getTime() + 86400000))) return "tomorrow";
  return weekday(iso);
};
const dateText = (iso) => { // "Mon 26 Oct 10:00"
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const part = (o) => d.toLocaleDateString([], { ...o, timeZone: TZ });
  return `${weekday(iso)} ${part({ day: "numeric" })} ${part({ month: "short" })} ${hhmm(iso)}`; // day before month, any locale
};
const inText = (iso) => { // "in 45 min" / "in 2 h 5 min" / "in 3 days"
  const min = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (isNaN(min)) return "";
  if (min < 1) return "now";
  if (min < 60) return `in ${min} min`;
  if (min < 1440) return `in ${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ""}`;
  const days = Math.round(min / 1440);
  return `in ${days} day${days === 1 ? "" : "s"}`;
};
const duration = (min) => (min < 60 ? `${min} min` : `${Math.floor(min / 60)} h${min % 60 ? ` ${min % 60} min` : ""}`);
// time left shown in the status pill: the manual run, or the running cycle (its estimated end of watering)
const timeLeft = (state, a) => {
  const target = state === "manual" ? a.ends_at : a.cycle && state !== "stuck" ? a.cycle_ends_at || a.ends_at : null;
  const ms = target ? new Date(target).getTime() - Date.now() : NaN;
  return isNaN(ms) ? "" : `${duration(Math.max(1, Math.ceil(ms / 60000)))} left`;
};
const daysText = (days) => {
  if (!days || days.length === 0) return "no days";
  if (days.length === 7) return "every day";
  if (days.join() === "mon,tue,wed,thu,fri") return "weekdays";
  if (days.join() === "sat,sun") return "weekends";
  return days.map((d) => d.slice(0, 2)).map((d) => d[0].toUpperCase() + d[1]).join(" ");
};

class IrrigationSchedulerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cfg = null; // integration config from the websocket
    this._edit = null; // {kind: "cell", c, t, minutes, order, soak} | {kind: "cycle", c, time, days}
    this._busy = false;
    this._error = "";
    this._sig = "";
    this._fetchedFor = null;
    this._waiting = new Set(); // edits waiting to save while another save is under way
  }

  setConfig(config) {
    this._config = { title: "Irrigation", ...config };
    this._sig = "";
    if (this._hass) this._render();
  }

  static getStubConfig() { return {}; }
  static getConfigElement() { return document.createElement("irrigation-sequencer-card-editor"); }
  getCardSize() { return 2 + (this._cfg ? this._cfg.taps.length : 6); }
  getGridOptions() { return { columns: 12, min_columns: 6, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    TZ = (hass.config && hass.config.time_zone) || undefined;
    if (this._settings && this._settings.isConnected) this._settings.hass = hass;
    if (!this._cfg && !this._loading) this._load();
    const sig = this._signature();
    if (sig !== this._sig) {
      this._sig = sig;
      const focused = this.shadowRoot.activeElement;
      if (focused && (focused.tagName === "INPUT" || focused.tagName === "SELECT")) {
        // don't redraw under the user's fingers; redraw when they leave the field
        focused.addEventListener("blur", () => this._render(), { once: true });
        return;
      }
      this._render();
    }
  }

  async _load() {
    this._loading = true;
    try {
      const msg = { type: `${DOMAIN}/config` };
      if (this._config && this._config.entry_id) msg.entry_id = this._config.entry_id;
      const res = await this._hass.callWS(msg);
      this._cfg = res.entries[0] || null;
      this._error = this._cfg ? "" : "Irrigation Scheduler is not set up (or not loaded).";
    } catch (e) {
      this._error = `Could not load: ${e.message || e}`;
    }
    this._loading = false;
    if (this._stale) { // changed again while fetching: fetch once more
      this._stale = false;
      this._load();
      return;
    }
    this._sig = "";
    this._render();
  }

  _st(id) { return id && this._hass ? this._hass.states[id] : undefined; }

  _signature() {
    if (!this._hass) return "";
    const ids = [];
    if (this._cfg) {
      const e = this._cfg.entities;
      ids.push(e.master, e.status, e.next_cycle, e.seasonal, this._cfg.main_valve);
      this._cfg.cycles.forEach((c) => ids.push(c.switch));
      this._cfg.taps.forEach((t) => ids.push(t.valve, t.blocked_entity));
    }
    const status = this._cfg && this._st(this._cfg.entities.status);
    const rev = status && status.attributes.config_rev;
    // settings changed elsewhere (another phone, the settings screen): fetch the grid again,
    // at most once per settings version; a change during a fetch fetches once more
    if (this._cfg && rev !== undefined && this._cfg.config_rev !== undefined
        && rev !== this._cfg.config_rev && rev !== this._fetchedFor) {
      this._fetchedFor = rev;
      this._stale = true;
    }
    if (this._stale && !this._loading) {
      this._stale = false;
      this._load();
    }
    return ids.map((id) => {
      const s = this._st(id);
      return s ? `${s.state}|${s.last_updated}` : "-";
    }).join(";") + `|${this._hass.user && this._hass.user.is_admin}`;
  }

  _canEdit() { return !!(this._hass && this._hass.user && this._hass.user.is_admin); }

  async _call(domain, service, data) {
    try {
      await this._hass.callService(domain, service, data);
    } catch (e) {
      this._error = e.message || String(e);
      this._render();
    }
  }

  // ------------------------------------------------------------------ editing: every change saves itself
  _editData(e) {
    return e.kind === "cell"
      ? { cycle: e.c + 1, tap: e.t + 1, minutes: e.minutes, order: e.order, soak: e.soak }
      : { cycle: e.c + 1, time: e.time, days: [...e.days] };
  }

  _openEdit(e) {
    if (this._saveTimer && this._edit) this._saveEdit(this._edit); // the one being left saves first
    if (e && e.kind !== "tap") e.orig = e.saved = JSON.stringify(this._editData(e));
    this._edit = e;
    this._editMsg = "";
    this._error = "";
    this._render();
  }

  _autoSave(ms) { // a short pause so a few quick taps (+ + +, several days) make one save
    clearTimeout(this._saveTimer);
    const e = this._edit;
    this._saveTimer = setTimeout(() => this._saveEdit(e), ms);
  }

  async _saveEdit(e, close = false) {
    if (!e || e.kind === "tap") return;
    if (close) e.closeAfter = true;
    if (e === this._edit) {
      clearTimeout(this._saveTimer);
      this._saveTimer = null;
    }
    if (this._busy) { // one save at a time: this one follows
      this._waiting.add(e);
      return;
    }
    const data = this._editData(e);
    const sig = JSON.stringify(data);
    if (sig !== e.saved) {
      const cy = this._cfg.cycles[e.c];
      const was = e.kind === "cell" ? cy.order[e.t] : 0;
      const swapped = e.kind === "cell" && e.order !== was ? cy.order.indexOf(e.order) : -1;
      this._busy = true;
      this._editMsg = "Saving…";
      this._render();
      try {
        this._cfg = await this._hass.callWS({ type: `${DOMAIN}/update`, entry_id: this._cfg.entry_id, ...data });
        e.saved = sig;
        this._error = "";
        this._editMsg = swapped >= 0 ? `Saved · ${this._cfg.taps[swapped].label} moved to #${was}` : "Saved";
      } catch (err) {
        this._error = err.message || String(err);
        this._editMsg = "";
      }
      this._busy = false;
    }
    if (e.closeAfter && this._edit === e && !this._error) this._edit = null;
    this._sig = "";
    this._render();
    const next = this._waiting.values().next().value;
    if (next) {
      this._waiting.delete(next);
      this._saveEdit(next);
    }
  }

  // ------------------------------------------------------------------ render
  _frame() {
    // a fixed frame: the grid part is redrawn on every change, the settings slot is kept
    // (so a settings form being filled in is never wiped by a status update)
    if (!this._main) {
      this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card><div id="main"></div><div id="slot"></div></ha-card>`;
      this._main = this.shadowRoot.getElementById("main");
      this._slot = this.shadowRoot.getElementById("slot");
    }
    return this._main;
  }

  _mountSettings() {
    if (this._showSettings && this._cfg) {
      if (!this._settings) {
        this._settings = document.createElement("irrigation-sequencer-settings");
        this._settings.pageLink = true;
      }
      if (!this._settings.isConnected) this._slot.appendChild(this._settings);
      this._settings.hass = this._hass;
      this._settings.entryId = this._cfg.entry_id;
    } else if (this._settings && this._settings.isConnected) {
      this._settings.remove();
    }
  }

  _render() {
    if (!this._config) return;
    const root = this._frame();
    if (!this._cfg) {
      root.innerHTML = `<div class="msg">${esc(this._error || "Loading…")}</div>`;
      return;
    }
    const cfg = this._cfg;
    const ent = cfg.entities;
    const status = this._st(ent.status);
    const master = this._st(ent.master);
    const seasonal = this._st(ent.seasonal);
    const next = this._st(ent.next_cycle);
    const a = status ? status.attributes : {};
    const state = status ? status.state : "unavailable";
    const runC = a.cycle || null;
    const runT = a.tap || null;
    const edit = this._canEdit();

    let line = "";
    if (state === "watering" || state === "manual") {
      line = `${esc(a.tap_name || "")}${runC && state === "watering" ? ` · Cycle ${runC}` : ""} · until ${hhmm(a.ends_at)}`;
    } else if (state === "stuck") {
      line = `${esc(a.tap_name || "")} cannot be confirmed off`;
    } else if (runC) {
      line = `Cycle ${runC}` + (a.next_step ? ` · next step ${hhmm(a.next_step)}` : "");
    } else if (next && next.state && next.state !== "unknown" && next.state !== "unavailable") {
      line = `Next: Cycle ${next.attributes.cycle} · ${dayWord(next.state)} ${hhmm(next.state)} · ${inText(next.state)}`;
      if (master && master.state === "off") line += " · master switch off, so it won't run";
    } else {
      const why = next && next.attributes.reason;
      line = `No cycle scheduled${why ? `: ${esc(why.charAt(0).toLowerCase() + why.slice(1))}` : ""}`;
      if (why && why.startsWith("No cycle has minutes")) line += " (tap a cell to add some)";
    }
    if (runC && master && master.state === "off") line += " · master switch off";
    const pending = (a.pending || []).map((p) => `C${p.cycle}`).join(", ");
    const mainRow = this._mainRow(state, a) + this._offlineRow(state, a) + this._batteryRow(a);

    // Colours: the cycle in progress (blue) and what is about to run (amber).
    const remain = a.remaining_min || {};
    const nextTap = a.next_tap || null;
    const pendingC = (a.pending || []).map((p) => p.cycle);
    const upNext = new Set(pendingC); // waiting cycles start as soon as they can
    if (!runC && !upNext.size && master && master.state === "on" && next && next.attributes
        && next.attributes.cycle && !["unknown", "unavailable"].includes(next.state)) {
      upNext.add(next.attributes.cycle); // the next scheduled start (only when the master switch is on)
    }
    const firstC = runC ? null : (pendingC.length ? pendingC[0] : [...upNext][0] || null);
    const firstTap = (c) => { // the tap a cycle opens first: lowest run order with minutes
      let best = -1;
      c.minutes.forEach((m, ti) => {
        if (m > 0 && cfg.taps[ti].valve && (best < 0 || c.order[ti] < c.order[best])) best = ti;
      });
      return best;
    };
    const hasNext = (runC && nextTap) || upNext.size > 0;

    const head = cfg.cycles.map((c, ci) => {
      const sw = this._st(c.switch);
      const on = sw ? sw.state === "on" : true;
      const total = c.minutes.reduce((s, m, ti) => s + (cfg.taps[ti].valve ? m : 0), 0);
      const isRun = runC === ci + 1;
      const isNext = !isRun && upNext.has(ci + 1);
      const tag = isRun ? `<span class="tag run">${state === "manual" ? "Paused" : "Running"}</span>`
        : isNext ? `<span class="tag next">Next</span>` : "";
      return `<th class="cyc ${on ? "" : "dim"} ${isRun ? "run" : ""} ${isNext ? "next" : ""}">
        <button class="hbtn" data-act="cycle" data-c="${ci}" ${edit ? "" : "disabled"} title="Edit time and days">
          <span class="cn">C${ci + 1}</span><span class="ct">${esc(c.time)}</span>
          <span class="cd">${esc(daysText(c.days))}</span>
          ${on && total && sw && sw.attributes.next_start && !isRun ? `<span class="cnx" title="Next start">next ${esc(dayWord(sw.attributes.next_start))}</span>` : ""}${tag}</button></th>`;
    }).join("");
    // the switches and ▶ in their own header row, so they line up whatever each header above shows
    const switches = cfg.cycles.map((c, ci) => {
      const sw = this._st(c.switch);
      const on = sw ? sw.state === "on" : true;
      const total = c.minutes.reduce((s, m, ti) => s + (cfg.taps[ti].valve ? m : 0), 0);
      return `<th class="cycsw"><div class="hrow">
          <button class="sw ${on ? "on" : ""}" data-act="cycsw" data-id="${esc(c.switch || "")}" title="Cycle switch">${on ? "On" : "Off"}</button>
          <button class="ico" data-act="run" data-c="${ci}" title="Run now" ${total ? "" : "disabled"}>▶</button>
        </div></th>`;
    }).join("");

    const rows = cfg.taps.map((t, ti) => {
      const v = this._st(t.valve);
      const vOn = v && ["on", "open", "opening"].includes(v.state);
      const vBad = v && ["unavailable", "unknown"].includes(v.state);
      const blocked = this._st(t.blocked_entity);
      const people = blocked && blocked.state === "on";
      const offNote = (a.offline_skipped || []).includes(t.n) ? " · offline, skipped" : (a.offline || []).includes(t.n) ? " · offline" : "";
      const batNote = (a.battery_low || []).includes(t.n) && a.battery && a.battery[t.n] != null ? ` · battery ${a.battery[t.n]}%` : "";
      const cells = cfg.cycles.map((c, ci) => {
        const m = c.minutes[ti];
        const off = !m || !t.valve;
        const sel = this._edit && this._edit.kind === "cell" && this._edit.c === ci && this._edit.t === ti;
        const inRun = runC === ci + 1 && !off;
        const live = inRun && runT === ti + 1 && state === "watering";
        const done = inRun && !live && !(remain[t.label] > 0); // nothing left for this tap in the running cycle
        const isNext = !off && !live && ((inRun && nextTap === ti + 1) || (firstC === ci + 1 && firstTap(c) === ti));
        const nextCol = !off && !inRun && upNext.has(ci + 1);
        const cls = [off && "off", sel && "sel", live && "live", inRun && !live && "runcol", done && "done",
          isNext && "nexttap", nextCol && "nextcol"].filter(Boolean).join(" ");
        const tip = live ? "Watering now" : isNext ? "Up next" : done ? "Done in this cycle" : "";
        return `<td><button class="cell ${cls}" ${tip ? `title="${tip}"` : ""}
            data-act="cell" data-c="${ci}" data-t="${ti}" ${edit && t.valve ? "" : "disabled"}>
          ${off ? `<span class="m">Off</span>${t.valve ? `<span class="o faint" title="Run order #${c.order[ti]} (kept for when it has minutes)">#${c.order[ti]}</span>` : ""}`
            : `<span class="m">${m}<small>min</small></span>
            <span class="o">${done ? "✓ " : ""}#${c.order[ti]}${c.soak[ti] ? ` <span title="cycle-and-soak">≈</span>` : ""}</span>
            ${isNext ? `<span class="nx">next</span>` : ""}`}
        </button></td>`;
      }).join("");
      return `<tr class="${t.valve ? "" : "novalve"}">
        <th class="tap"><button class="tapbtn ${this._edit && this._edit.kind === "tap" && this._edit.t === ti ? "sel" : ""}"
            data-act="tap" data-t="${ti}" title="${esc(t.label)}: run now, valve, settings">
          <span class="dot ${vOn ? "on" : ""} ${vBad ? "bad" : ""}" title="${esc(v ? v.state : "no valve")}"></span>
          <span class="tl"><span>${esc(t.name || `Tap ${t.n}`)}</span><small>Tap ${t.n}${people ? " · person" : ""}${t.valve ? "" : " · no valve"}${offNote}${batNote}</small></span></button></th>
        ${cells}</tr>`;
    }).join("");

    const totals = cfg.cycles.map((c) => {
      const tot = c.minutes.reduce((s, m, ti) => s + (cfg.taps[ti].valve ? m : 0), 0);
      return `<td class="tot">${tot ? `${tot} min` : "–"}</td>`;
    }).join("");

    const left = timeLeft(state, a);
    const sv = seasonal ? Number(seasonal.state) : null;
    const gear = edit && !this._config.in_panel
      ? `<button class="ico gear ${this._showSettings ? "on" : ""}" data-act="settings" title="Settings"><ha-icon icon="mdi:cog"></ha-icon></button>`
      : "";
    root.innerHTML = `
        <div class="top">
          <div class="title">${esc(this._config.title)}</div>
          <span class="pill s-${esc(state)} ${left ? "has-left" : ""}" title="${esc(STATUS_TEXT[state] || state)}${left ? ` · ${left}` : ""}"><span
            class="st">${esc(STATUS_TEXT[state] || state)}</span>${left ? `<span class="sep"> · </span><span class="left">${left}</span>` : ""}</span>
          <button class="sw big ${master && master.state === "on" ? "on" : ""}" data-act="master" title="Master switch">
            ${master && master.state === "on" ? "Master on" : "Master off"}</button>
          ${gear}
        </div>
        <div class="line">${line}${pending ? ` · waiting: ${pending}` : ""}</div>
        ${mainRow}
        ${a.last_message ? `<div class="last" title="Last message">${esc(a.last_message)}</div>` : ""}
        <div class="bar">
          <span class="lab">Seasonal</span>
          <button class="ico" data-act="seas" data-d="-5" ${sv === null ? "disabled" : ""}>−</button>
          <span class="val">${sv === null ? "–" : `${Math.round(sv)}%`}</span>
          <button class="ico" data-act="seas" data-d="5" ${sv === null ? "disabled" : ""}>+</button>
          <span class="grow"></span>
          ${state !== "idle" || pending ? `<button class="act" data-act="skip" ${runT && state === "watering" ? "" : "disabled"}>Skip tap</button>
          <button class="act stop" data-act="stop">Stop</button>` : ""}
        </div>
        <div class="wrap"><table>
          <thead><tr><th class="corner" rowspan="2">Tap</th>${head}</tr><tr>${switches}</tr></thead>
          <tbody>${rows}</tbody>
          <tfoot><tr><th class="tap"></th>${totals}</tr></tfoot>
        </table></div>
        ${runC || hasNext ? `<div class="legend">
          ${runC ? `<span><i class="sq run"></i>${state === "manual" ? "Cycle paused (manual run)" : "Running"}</span>` : ""}
          ${hasNext ? `<span><i class="sq next"></i>Up next</span>` : ""}</div>` : ""}
        ${this._editor()}
        ${this._error ? `<div class="err">${esc(this._error)}</div>` : ""}`;
    root.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", (ev) => this._click(ev, el)));
    root.querySelectorAll("[data-in]").forEach((el) => el.addEventListener("change", (ev) => this._input(ev, el)));
    this._tick(!!left);
  }

  // the time left counts down between status updates (only the pill text changes)
  _tick(on) {
    if (on && !this._ticker) {
      this._ticker = setInterval(() => {
        const st = this._cfg && this._st(this._cfg.entities.status);
        const el = this._main && this._main.querySelector(".pill .left");
        if (st && el) el.textContent = timeLeft(st.state, st.attributes);
      }, 15000);
    } else if (!on && this._ticker) {
      clearInterval(this._ticker);
      this._ticker = null;
    }
  }

  connectedCallback() { if (this._main && this._cfg) this._render(); }

  disconnectedCallback() {
    this._tick(false);
    if (this._saveTimer) this._saveEdit(this._edit); // leaving the dashboard: save what was changed
  }

  // main water: shown when a tap is stuck (offer to close it) or while it is off (offer to open it)
  _mainRow(state, a) {
    const id = this._cfg.main_valve;
    if (!id) return "";
    const s = this._st(id);
    const on = s && ["on", "open", "opening"].includes(s.state);
    const off = s && ["off", "closed"].includes(s.state);
    const stuck = state === "stuck";
    if (!stuck && !off) return "";
    const want = off ? "open" : "close";
    const armed = this._mainArmed === want;
    const label = want === "close"
      ? (armed ? "Tap again to close" : "Close main water")
      : (armed ? "Tap again to open" : "Open main water");
    const snoozed = off && a.main_snoozed_until ? ` Reminders snoozed until ${dayWord(a.main_snoozed_until)} ${hhmm(a.main_snoozed_until)}.` : "";
    const text = (stuck
      ? `${esc(a.tap_name || "A tap")} can't be confirmed off.${off ? " The main water is off." : on ? "" : " Main water valve: " + esc(s ? s.state : "not found") + "."}`
      : "The main water is off: cycles wait until it is on again.") + snoozed;
    // snooze the "main water is off" reminders (they start again when the time is up)
    const snooze = !off ? "" : a.main_snoozed_until
      ? `<button class="chip" data-act="snooze" data-v="0" title="Remind me again now">Resume reminders</button>`
      : `<span class="lab">Snooze</span>${[1, 4, 12].map((h) => `<button class="chip" data-act="snooze" data-v="${h}"
          title="No 'main water is off' reminders for ${h} h">${h} h</button>`).join("")}`;
    const btn = (stuck && off) || (!on && !off) ? "" // stuck with the water already off: fix the tap first
      : `<button class="act ${want === "close" ? "stop" : ""} ${armed ? "armed" : ""}" data-act="main" data-v="${want}"
          title="${want === "close" ? "Closes the main water valve" : "Opens the main water valve again"}">${label}</button>`;
    return `<div class="mainrow ${stuck ? "alert" : ""}"><ha-icon icon="${off ? "mdi:water-off" : "mdi:alert"}"></ha-icon>
      <span class="grow">${text}</span>${snooze ? `<span class="snz">${snooze}</span>` : ""}${btn}</div>`;
  }

  // an offline valve: a waiting cycle (skip it, or Stop below), or one that cycles will wait for
  _offlineRow(state, a) {
    const cfg = this._cfg;
    const label = (n) => (cfg.taps[n - 1] ? cfg.taps[n - 1].label : `Tap ${n}`);
    const skipped = a.offline_skipped || [];
    const waiting = a.offline_waiting || [];
    const off = (waiting.length ? waiting : (a.offline || []).filter((n) => !skipped.includes(n)))
      .filter((n) => !(state === "stuck" && a.tap === n)); // a stuck tap has its own row
    if (!off.length) return "";
    const can = off.filter((n) => (a.offline_can_skip || []).includes(n));
    const names = off.map(label).join(", ");
    const one = off.length === 1;
    const text = waiting.length
      ? `${esc(names)} ${one ? "is" : "are"} offline: cycle ${a.cycle || ""} is waiting${a.offline_until ? ` until ${hhmm(a.offline_until)}` : ""}.`
      : `${esc(names)} ${one ? "is" : "are"} offline: cycles wait for ${one ? "it" : "them"} (you'll be asked).`;
    const v = can.join(",");
    const armed = this._offArmed === v;
    const btn = can.length && this._canEdit()
      ? `<button class="act ${armed ? "armed" : ""}" data-act="offskip" data-v="${v}" title="Water the other taps; included again when back online">${armed ? "Tap again to skip" : "Skip while offline"}</button>`
      : can.length < off.length ? `<span class="hint">Last seen open: can't be skipped.</span>` : "";
    return `<div class="mainrow ${waiting.length ? "alert" : ""}"><ha-icon icon="mdi:lan-disconnect"></ha-icon>
      <span class="grow">${text}</span>${btn}</div>`;
  }

  // low valve batteries: snooze the warnings (a valve that gets low later still warns)
  _batteryRow(a) {
    const low = a.battery_low || [];
    if (!low.length) return "";
    const cfg = this._cfg;
    const parts = low.map((n) => `${esc(cfg.taps[n - 1] ? cfg.taps[n - 1].label : `Tap ${n}`)} ${a.battery && a.battery[n] != null ? `${a.battery[n]}%` : ""}`);
    const until = a.battery_snoozed_until;
    const snooze = !this._canEdit() ? "" : until
      ? `<span class="lab">Snoozed until ${esc(dayWord(until))} ${hhmm(until)}</span><button class="chip" data-act="batsnooze" data-v="0" title="Warn me again now">Resume</button>`
      : `<span class="lab">Snooze</span>${[[12, "12 h"], [24, "1 day"], [72, "3 days"]].map(([h, t]) => `<button class="chip" data-act="batsnooze" data-v="${h}"
          title="No low-battery warnings for ${t}">${t}</button>`).join("")}`;
    return `<div class="mainrow"><ha-icon icon="mdi:battery-alert-variant-outline"></ha-icon>
      <span class="grow">Low battery: ${parts.join(", ")}.</span>${snooze ? `<span class="snz">${snooze}</span>` : ""}</div>`;
  }

  _editor() {
    const e = this._edit;
    if (!e || !this._cfg) return "";
    const cfg = this._cfg;
    if (e.kind === "tap") return this._tapPanel(e.t);
    if (e.kind === "cell") {
      const t = cfg.taps[e.t];
      const T = cfg.taps.length;
      const cy = cfg.cycles[e.c];
      const holder = (n) => cy.order.indexOf(n); // which tap has run order n now
      const opts = Array.from({ length: T }, (_, i) => {
        const h = holder(i + 1);
        const who = h === e.t ? "this tap" : h >= 0 ? (cfg.taps[h].name || `Tap ${h + 1}`) : "";
        return `<option value="${i + 1}" ${e.order === i + 1 ? "selected" : ""}>${i + 1}${who ? ` · ${esc(who)}` : ""}</option>`;
      }).join("");
      const was = cy.order[e.t];
      const swapWith = e.order !== was ? holder(e.order) : -1;
      const quick = QUICK.map((q) => `<button class="chip ${e.minutes === q ? "on" : ""}" data-act="q" data-v="${q}">${q === 0 ? "Off" : q}</button>`).join("");
      const over = e.minutes > cfg.max_run_min && !e.soak;
      return `<div class="editor">
        <div class="eh">Cycle ${e.c + 1} · ${esc(t.label)}</div>
        <div class="er"><span class="lab">Minutes</span>
          <button class="ico" data-act="mm" data-d="-1">−</button>
          <input class="num" type="number" min="0" max="240" step="1" value="${e.minutes}" data-in="minutes">
          <button class="ico" data-act="mm" data-d="1">+</button>
          <span class="hint">${e.minutes === 0 ? "Off: not in this cycle" : ""}</span></div>
        <div class="chips">${quick}</div>
        <div class="er"><span class="lab">Run order</span><select data-in="order">${opts}</select>
          <span class="lab sp">Cycle-and-soak</span>
          <button class="sw ${e.soak ? "on" : ""}" data-act="soak">${e.soak ? "On" : "Off"}</button></div>
        ${swapWith >= 0 ? `<div class="hint">Swaps with ${esc(cfg.taps[swapWith].label)}: it moves to #${was}.</div>` : ""}
        ${over ? `<div class="hint warn">Over the ${cfg.max_run_min} min safety limit: it runs in parts. Consider cycle-and-soak.</div>` : ""}
        ${this._editFoot(e)}
      </div>`;
    }
    const days = DAYS.map((d, i) => `<button class="day ${e.days.includes(d) ? "on" : ""}" data-act="day" data-d="${d}" title="${d}">${DAY_SHORT[i]}</button>`).join("");
    return `<div class="editor">
      <div class="eh">Cycle ${e.c + 1}</div>
      <div class="er"><span class="lab">Start time</span><input type="time" value="${esc(e.time)}" data-in="time">
        <span class="hint">24 h · avoid 02:00–03:00 (daylight saving)</span></div>
      <div class="er"><span class="lab">Days</span><span class="days">${days}</span></div>
      ${this._editFoot(e)}
    </div>`;
  }

  _editFoot(e) { // no Save button: changes save by themselves; Undo puts back what it was when opened
    const changed = JSON.stringify(this._editData(e)) !== e.orig;
    return `<div class="er end"><span class="hint grow">${esc(this._editMsg || "Changes save automatically")}</span>
      ${changed ? `<button class="act" data-act="undo" title="Back to how it was when you opened it">Undo</button>` : ""}
      <button class="act primary" data-act="done">Done</button></div>`;
  }

  _tapPanel(ti) {
    // one tap: run it now, stop / skip it, the valve, its settings
    const cfg = this._cfg;
    const t = cfg.taps[ti];
    if (!t) return "";
    const v = this._st(t.valve);
    const vOn = v && ["on", "open", "opening"].includes(v.state);
    const status = this._st(cfg.entities.status);
    const state = status ? status.state : "";
    const mine = status && status.attributes.tap === ti + 1;
    const max = cfg.max_run_min;
    const longest = Math.max(0, ...cfg.cycles.map((c) => c.minutes[ti] || 0));
    const choices = [...new Set([1, 2, 5, 10, 15, 20, 30, Math.min(longest, max)])]
      .filter((m) => m > 0 && m <= max).sort((x, y) => x - y);
    const a = status ? status.attributes : {};
    const lvl = a.battery ? a.battery[ti + 1] : null;
    const offline = (a.offline || []).includes(ti + 1);
    const skipped = (a.offline_skipped || []).includes(ti + 1);
    const canSkip = (a.offline_can_skip || []).includes(ti + 1);
    const used = cfg.cycles.map((c, ci) => (c.minutes[ti] > 0
      ? `C${ci + 1}: ${c.minutes[ti]} min, #${c.order[ti]}${c.soak[ti] ? " ≈" : ""}` : "")).filter(Boolean);
    return `<div class="editor tappanel">
      <div class="eh">${esc(t.label)}</div>
      <div class="er"><span class="lab">Valve</span>
        ${t.valve ? `<span class="vid">${esc(t.valve)}</span><span class="vst ${vOn ? "on" : ""}">${esc(v ? v.state : "not found")}</span>`
          + (lvl != null ? `<span class="vst ${(a.battery_low || []).includes(ti + 1) ? "low" : ""}" title="Battery${t.battery ? ` (${esc(t.battery)})` : ""}">battery ${lvl}%</span>` : "")
          : `<span class="hint">No valve: pick one under Tap settings.</span>`}</div>
      ${skipped ? `<div class="er"><span class="hint grow">Offline: skipped in every cycle until it is back online.</span>
          <button class="act" data-act="offinclude">Include again</button></div>`
        : offline ? `<div class="er"><span class="hint grow">${canSkip ? "Offline: cycles wait for it (you'll be asked). Skipping it lets the other taps water."
            : "Offline and last seen open: it may still be running, so it can't be skipped."}</span>
          ${canSkip ? `<button class="act" data-act="offskip" data-v="${ti + 1}">Skip while offline</button>` : ""}</div>` : ""}
      <div class="er"><span class="lab">In cycles</span><span class="hint">${used.length ? esc(used.join(" · ")) : "none yet: tap a cell to add minutes"}</span></div>
      ${t.valve && !offline ? `<div class="er"><span class="lab">Run now</span><span class="chips">${choices.map((m) =>
        `<button class="chip" data-act="runtap" data-v="${m}" title="Open ${esc(t.label)} for ${m} min">${m} min</button>`).join("")}</span></div>
        <div class="hint">Opens this tap now as a manual run: any other open tap closes and a running cycle pauses until it is done.</div>` : ""}
      ${mine && state === "manual" ? `<div class="er"><button class="act stop" data-act="tapoff">Stop this manual run</button></div>` : ""}
      ${mine && state === "watering" ? `<div class="er"><button class="act" data-act="skip">Skip this tap in the cycle</button></div>` : ""}
      <div class="er end">
        ${t.valve ? `<button class="act" data-act="valveinfo">Valve details</button>` : ""}
        ${this._canEdit() ? `<button class="act ${this._tapSettingsOpen(ti) ? "on" : ""}" data-act="tapsettings">${this._tapSettingsOpen(ti) ? "Hide tap settings" : "Tap settings"}</button>` : ""}
        <button class="act primary" data-act="cancel">Close</button></div>
    </div>`;
  }

  // a dashboard card's settings are showing this tap (pressing "Tap settings" again hides them)
  _tapSettingsOpen(tap) {
    return !this._config.in_panel && !!this._showSettings && !!this._settings && this._settings.isConnected
      && this._settings._tab === "taps" && this._settings._tap === tap;
  }

  _openSettings(tab, tap) {
    // the Irrigation page has its own settings below the grid: ask it; on a dashboard open the card's own
    if (this._config.in_panel) {
      this.dispatchEvent(new CustomEvent("irrigation-open-settings", { detail: { tab, tap }, bubbles: true, composed: true }));
      return;
    }
    if (tab === "taps" && this._tapSettingsOpen(tap)) { // second press: hide them again
      this._showSettings = false;
      this._mountSettings();
      this._render();
      return;
    }
    this._showSettings = true;
    this._mountSettings();
    if (this._settings) this._settings.open(tab, tap);
    this._render(); // after open(): the button then reads "Hide tap settings"
  }

  _input(ev, el) {
    const e = this._edit;
    if (!e) return;
    const k = el.dataset.in;
    if (k === "minutes") e.minutes = Math.max(0, Math.min(240, parseInt(el.value || "0", 10) || 0));
    if (k === "order") e.order = parseInt(el.value, 10);
    if (k === "time") {
      if (!el.value) return; // not a whole time yet
      e.time = el.value;
    }
    this._render();
    this._autoSave(300);
  }

  _click(ev, el) {
    ev.stopPropagation();
    const act = el.dataset.act;
    const cfg = this._cfg;
    const e = this._edit;
    switch (act) {
      case "settings":
        this._showSettings = !this._showSettings;
        this._mountSettings();
        this._render();
        break;
      case "master": {
        const m = this._st(cfg.entities.master);
        this._call("switch", m && m.state === "on" ? "turn_off" : "turn_on", { entity_id: cfg.entities.master });
        break;
      }
      case "cycsw": {
        const s = this._st(el.dataset.id);
        if (el.dataset.id) this._call("switch", s && s.state === "on" ? "turn_off" : "turn_on", { entity_id: el.dataset.id });
        break;
      }
      case "run":
        this._call(DOMAIN, "run_cycle", { cycle: Number(el.dataset.c) + 1, config_entry_id: cfg.entry_id });
        break;
      case "stop":
        this._call(DOMAIN, "stop", { config_entry_id: cfg.entry_id });
        break;
      case "snooze":
        this._call(DOMAIN, "snooze_main_water_reminders", { hours: Number(el.dataset.v), config_entry_id: cfg.entry_id });
        break;
      case "batsnooze":
        this._call(DOMAIN, "snooze_battery_warnings", { hours: Number(el.dataset.v), config_entry_id: cfg.entry_id });
        break;
      case "offskip": { // two taps (it lets the other taps run while this valve can't be confirmed closed)
        const taps = String(el.dataset.v).split(",").map(Number);
        clearTimeout(this._offTimer);
        if (this._offArmed !== el.dataset.v) {
          this._offArmed = el.dataset.v;
          this._offTimer = setTimeout(() => { this._offArmed = null; this._render(); }, 5000);
        } else {
          this._offArmed = null;
          taps.forEach((n) => this._call(DOMAIN, "skip_offline_tap", { tap: n, skip: true, config_entry_id: cfg.entry_id }));
        }
        this._render();
        break;
      }
      case "offinclude":
        this._call(DOMAIN, "skip_offline_tap", { tap: e.t + 1, skip: false, config_entry_id: cfg.entry_id });
        break;
      case "main": { // two taps: the first arms the button for 5 s, the second acts
        const want = el.dataset.v;
        clearTimeout(this._mainTimer);
        if (this._mainArmed !== want) {
          this._mainArmed = want;
          this._mainTimer = setTimeout(() => { this._mainArmed = null; this._render(); }, 5000);
        } else {
          this._mainArmed = null;
          this._call(DOMAIN, want === "close" ? "close_main_water" : "open_main_water", { config_entry_id: cfg.entry_id });
        }
        this._render();
        break;
      }
      case "skip": {
        const st = this._st(cfg.entities.status);
        if (st && st.attributes.tap) this._call(DOMAIN, "skip_tap", { tap: st.attributes.tap, config_entry_id: cfg.entry_id });
        break;
      }
      case "seas": {
        const s = this._st(cfg.entities.seasonal);
        if (!s) break;
        const v = Math.max(0, Math.min(200, Number(s.state) + Number(el.dataset.d)));
        this._call("number", "set_value", { entity_id: cfg.entities.seasonal, value: v });
        break;
      }
      case "tap": {
        const t = Number(el.dataset.t);
        this._openEdit(e && e.kind === "tap" && e.t === t ? null : { kind: "tap", t });
        break;
      }
      case "runtap":
        this._call(DOMAIN, "run_tap", { tap: e.t + 1, minutes: Number(el.dataset.v), config_entry_id: cfg.entry_id });
        break;
      case "tapoff": {
        const valve = cfg.taps[e.t].valve;
        const [domain] = valve.split(".");
        if (domain === "valve") this._call("valve", "close_valve", { entity_id: valve });
        else this._call("homeassistant", "turn_off", { entity_id: valve });
        break;
      }
      case "valveinfo":
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: cfg.taps[e.t].valve }, bubbles: true, composed: true }));
        break;
      case "tapsettings":
        this._openSettings("taps", e.t);
        break;
      case "cell": {
        const c = Number(el.dataset.c), t = Number(el.dataset.t);
        const cy = cfg.cycles[c];
        this._openEdit({ kind: "cell", c, t, minutes: cy.minutes[t], order: cy.order[t], soak: cy.soak[t] });
        break;
      }
      case "cycle": {
        const c = Number(el.dataset.c);
        this._openEdit({ kind: "cycle", c, time: cfg.cycles[c].time, days: [...cfg.cycles[c].days] });
        break;
      }
      case "q": e.minutes = Number(el.dataset.v); this._render(); this._autoSave(300); break;
      case "mm": e.minutes = Math.max(0, Math.min(240, e.minutes + Number(el.dataset.d))); this._render(); this._autoSave(800); break;
      case "soak": e.soak = !e.soak; this._render(); this._autoSave(300); break;
      case "day": {
        const d = el.dataset.d;
        e.days = e.days.includes(d) ? e.days.filter((x) => x !== d) : DAYS.filter((x) => x === d || e.days.includes(x));
        this._render();
        this._autoSave(800);
        break;
      }
      case "cancel": this._openEdit(null); break; // the tap panel's Close
      case "done": this._saveEdit(e, true); break;
      case "undo": {
        const o = JSON.parse(e.orig);
        if (e.kind === "cell") Object.assign(e, { minutes: o.minutes, order: o.order, soak: o.soak });
        else Object.assign(e, { time: o.time, days: o.days });
        this._render();
        this._saveEdit(e);
        break;
      }
      default:
    }
  }
}

const CSS = `
  :host { display: block; }
  ha-card { padding: 12px 12px 10px; overflow: hidden; }
  .msg { padding: 8px; color: var(--secondary-text-color); }
  .gear { display: inline-flex; align-items: center; justify-content: center; --mdc-icon-size: 18px; }
  .gear.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  #slot:not(:empty) { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--divider-color); }
  .top { display: flex; align-items: center; gap: 8px; }
  .title { font-size: 1.15em; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill { font-size: .78em; padding: 3px 9px; border-radius: 999px; background: var(--secondary-background-color); color: var(--primary-text-color); white-space: nowrap; }
  .pill.s-watering, .pill.s-manual { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .pill.s-soaking, .pill.s-gap { background: color-mix(in srgb, var(--primary-color) 22%, transparent); }
  .pill.s-held, .pill.s-waiting, .pill.s-offline { background: color-mix(in srgb, var(--warning-color, #ffa600) 30%, transparent); }
  .pill.s-stuck { background: var(--error-color, #db4437); color: #fff; }
  .pill .left { font-weight: 600; font-variant-numeric: tabular-nums; }
  #main { container-type: inline-size; }
  @container (max-width: 430px) { /* narrow (phone): only the time left */
    .pill.has-left .st, .pill.has-left .sep { display: none; }
  }
  .line { margin: 6px 2px 2px; color: var(--secondary-text-color); font-size: .9em; }
  .mainrow { display: flex; align-items: center; gap: 8px; margin: 6px 0 2px; padding: 6px 10px; border-radius: 10px;
    font-size: .85em; background: color-mix(in srgb, var(--warning-color, #ffa600) 16%, transparent); --mdc-icon-size: 18px; }
  .mainrow.alert { background: color-mix(in srgb, var(--error-color, #db4437) 16%, transparent); }
  .mainrow { flex-wrap: wrap; }
  .mainrow .act { flex: none; background: var(--card-background-color, #fff); }
  .mainrow .snz { display: inline-flex; align-items: center; gap: 4px; }
  .mainrow .chip { background: var(--card-background-color, #fff); }
  .act.armed { background: var(--error-color, #db4437); border-color: var(--error-color, #db4437); color: #fff; }
  .last { margin: 2px 2px 0; color: var(--secondary-text-color); font-size: .78em; opacity: .8; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .bar { display: flex; align-items: center; gap: 6px; margin: 8px 0 6px; flex-wrap: wrap; }
  .lab { font-size: .85em; color: var(--secondary-text-color); }
  .lab.sp { margin-left: 12px; }
  .val { min-width: 3.2em; text-align: center; font-variant-numeric: tabular-nums; }
  .grow { flex: 1; }
  button { font: inherit; color: inherit; cursor: pointer; border: none; background: none; }
  button:disabled { cursor: default; opacity: .45; }
  .ico { width: 28px; height: 28px; border-radius: 50%; background: var(--secondary-background-color); line-height: 28px; padding: 0; }
  .sw { font-size: .75em; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .sw.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .sw.big { font-size: .8em; padding: 4px 10px; }
  .act { padding: 5px 12px; border-radius: 8px; border: 1px solid var(--divider-color); font-size: .85em; }
  .act.stop { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
  .act.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .wrap { overflow-x: auto; margin: 0 -12px; padding: 0 12px; }
  table { border-collapse: separate; border-spacing: 4px; width: 100%; }
  th, td { padding: 0; }
  th.corner { text-align: left; font-weight: 400; font-size: .75em; color: var(--secondary-text-color); vertical-align: bottom; padding: 0 4px 4px; }
  th.cyc { min-width: 60px; vertical-align: top; font-weight: 400; }
  th.cyc.dim { opacity: .5; }
  th.cyc.run .hbtn { background: color-mix(in srgb, var(--primary-color) 16%, transparent); box-shadow: inset 0 -3px 0 var(--primary-color); }
  th.cyc.next .hbtn { background: color-mix(in srgb, var(--warning-color, #ffa600) 16%, transparent); box-shadow: inset 0 -3px 0 var(--warning-color, #ffa600); }
  .tag { margin-top: 3px; font-size: .6em; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 1px 6px; border-radius: 999px; }
  .tag.run { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .tag.next { background: var(--warning-color, #ffa600); color: #1c1c1c; }
  .hbtn { display: flex; flex-direction: column; align-items: center; width: 100%; padding: 4px 2px; border-radius: 8px; }
  .hbtn:not(:disabled):hover { background: var(--secondary-background-color); }
  .cn { font-weight: 600; font-size: .9em; }
  .ct { font-size: .95em; font-variant-numeric: tabular-nums; }
  .cd { font-size: .68em; color: var(--secondary-text-color); white-space: nowrap; }
  .hrow { display: flex; justify-content: center; align-items: center; gap: 4px; margin: 1px 0 2px; }
  th.cycsw { font-weight: 400; }
  .hrow .ico { width: 24px; height: 24px; line-height: 24px; font-size: .7em; }
  th.tap { text-align: left; font-weight: 400; padding-right: 4px; position: sticky; left: 0; background: var(--ha-card-background, var(--card-background-color, #fff)); z-index: 1; }
  .tapbtn { display: flex; align-items: center; width: 100%; max-width: 9.5em; text-align: left; padding: 4px 6px 4px 4px; border-radius: 8px; }
  .tapbtn:hover { background: var(--secondary-background-color); }
  .tapbtn.sel { box-shadow: inset 0 0 0 1px var(--primary-color); }
  .cnx { font-size: .64em; color: var(--secondary-text-color); white-space: nowrap; margin-top: 1px; }
  .vid { font-size: .8em; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .vst { font-size: .75em; padding: 1px 8px; border-radius: 999px; background: var(--card-background-color, #fff); border: 1px solid var(--divider-color); }
  .vst.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .vst.low { background: color-mix(in srgb, var(--warning-color, #ffa600) 30%, transparent); }
  .act.on { border-color: var(--primary-color); color: var(--primary-color); }
  .tappanel .er .lab { min-width: 5.5em; }
  .tl { display: inline-flex; flex-direction: column; line-height: 1.15; min-width: 0; font-size: .92em; }
  .tl > span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
  .tl small { font-size: .7em; color: var(--secondary-text-color); }
  .dot { flex: none; display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; background: var(--disabled-text-color, #bdbdbd); }
  .dot.on { background: var(--primary-color); box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary-color) 30%, transparent); }
  .dot.bad { background: var(--error-color, #db4437); }
  tr.novalve th.tap { opacity: .55; }
  .cell { width: 100%; min-height: 46px; border-radius: 10px; display: flex; flex-direction: column; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--primary-color) 14%, var(--ha-card-background, var(--card-background-color, #fff))); border: 1px solid transparent; }
  .cell .m { font-weight: 600; font-variant-numeric: tabular-nums; }
  .cell .m small { font-weight: 400; font-size: .68em; margin-left: 2px; color: var(--secondary-text-color); }
  .cell .o { font-size: .7em; color: var(--secondary-text-color); }
  .cell.off { background: var(--secondary-background-color); color: var(--disabled-text-color, #9e9e9e); }
  .cell.off .m { font-weight: 400; font-size: .85em; }
  .cell .o.faint { opacity: .5; }
  .cell.runcol { background: color-mix(in srgb, var(--primary-color) 30%, var(--ha-card-background, var(--card-background-color, #fff)));
    box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--primary-color) 75%, transparent); }
  .cell.nextcol { background: color-mix(in srgb, var(--warning-color, #ffa600) 14%, var(--ha-card-background, var(--card-background-color, #fff))); }
  .cell.done { opacity: .5; }
  .cell.nexttap { box-shadow: inset 0 0 0 2px var(--warning-color, #ffa600); }
  .cell .nx { font-size: .6em; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: var(--warning-color, #ffa600); line-height: 1.1; }
  .cell.sel { border-color: var(--primary-color); }
  .cell.live { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .cell.live .m small, .cell.live .o { color: inherit; opacity: .85; }
  .cell:not(:disabled):hover { border-color: var(--divider-color); }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 2px 0; font-size: .75em; color: var(--secondary-text-color); }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .sq { display: inline-block; width: 10px; height: 10px; border-radius: 3px; }
  .sq.run { background: var(--primary-color); }
  .sq.next { background: var(--warning-color, #ffa600); }
  td.tot { text-align: center; font-size: .72em; color: var(--secondary-text-color); padding-top: 2px; }
  .editor { margin-top: 8px; padding: 10px; border-radius: 12px; background: var(--secondary-background-color); }
  .editor .ico { background: var(--card-background-color, #fff); }
  .eh { font-weight: 500; margin-bottom: 6px; }
  .er { display: flex; align-items: center; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
  .er.end { justify-content: flex-end; }
  .num { width: 64px; text-align: center; }
  select { max-width: 15em; text-overflow: ellipsis; }
  input, select { font: inherit; padding: 4px 6px; border-radius: 6px; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .chip { padding: 3px 9px; border-radius: 999px; border: 1px solid var(--divider-color); font-size: .8em; }
  .chip.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .days { display: inline-flex; gap: 4px; }
  .day { width: 30px; height: 30px; border-radius: 50%; border: 1px solid var(--divider-color); font-size: .8em; }
  .day.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .hint { font-size: .78em; color: var(--secondary-text-color); }
  .hint.warn, .err { color: var(--warning-color, #ffa600); }
  .err { margin-top: 8px; font-size: .85em; color: var(--error-color, #db4437); }
`;

class IrrigationSchedulerCardEditor extends HTMLElement {
  setConfig(config) { this._config = config; this._render(); }
  set hass(hass) { this._hass = hass; this._render(); }
  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => ({ title: "Title", entry_id: "Irrigation Scheduler (only if you have more than one)" }[s.name]);
      this._form.addEventListener("value-changed", (ev) => {
        const cfg = { ...this._config, ...ev.detail.value };
        Object.keys(cfg).forEach((k) => (cfg[k] === "" || cfg[k] === undefined) && delete cfg[k]);
        this._config = cfg;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: cfg }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = [
      { name: "title", selector: { text: {} } },
      { name: "entry_id", selector: { config_entry: { integration: DOMAIN } } },
    ];
  }
}

// ------------------------------------------------------------------ settings
// Every setting of the Configure screens, on the card (⚙) and on the Irrigation page in the sidebar.
// Labels and help texts come from the integration's own translations, so both places read the same.
const PANEL_PATH = "/irrigation-sequencer";
const PAGE_CHOICE = `${DOMAIN}_page_choice`; // the Irrigation Scheduler last picked on the page (this browser)
const S = {
  num: (min, max, step = 1, unit) => ({ number: { min, max, step, mode: "box", ...(unit ? { unit_of_measurement: unit } : {}) } }),
  ent: (domain, multiple = false) => ({ entity: { filter: { domain }, ...(multiple ? { multiple: true } : {}) } }),
  bool: { boolean: {} },
  text: { text: {} },
  time: { time: {} },
  date: { date: {} },
};
const GROUPS = {
  taps: { label: "Taps", group: "tap", step: "tap", schema: [
    { name: "valve", selector: S.ent(["switch", "valve", "input_boolean"]) },
    { name: "name", selector: S.text },
    { name: "people", selector: S.ent(["binary_sensor", "sensor"], true) },
    { name: "soil", selector: S.ent(["sensor"]) },
    { name: "soil_pct", selector: S.num(0, 100, 1, "%") },
    { name: "bypass", selector: S.ent(["input_boolean", "switch", "binary_sensor"]) },
    { name: "battery", selector: S.ent(["sensor"]) },
  ] },
  general: { label: "General", group: "general", step: "general", key: "general", schema: [
    { name: "gap_s", selector: S.num(0, 60, 1, "s") },
    { name: "confirm_s", selector: S.num(5, 120, 1, "s") },
    { name: "max_run_min", selector: S.num(1, 240, 1, "min") },
    { name: "late_min", selector: S.num(1, 720, 1, "min") },
    { name: "seasonal_entity", selector: S.ent(["input_number", "number", "sensor"]) },
  ] },
  notify: { label: "Notifications", group: "notify", step: "notifications", key: "notify", schema: [
    { name: "devices", selector: { device: { filter: { integration: "mobile_app" }, multiple: true } } },
    { name: "prefix", selector: S.text },
    { name: "start_finish", selector: S.bool },
    { name: "skips", selector: S.bool },
    { name: "manual", selector: S.bool },
    { name: "people", selector: S.bool },
    { name: "reminders", selector: S.bool },
    { name: "rain", selector: S.bool },
    { name: "hot_c", selector: S.num(0, 50, 1, "°C") },
  ] },
  soak: { label: "Cycle-and-soak", group: "soak", step: "soak", key: "soak", schema: [
    { name: "burst_min", selector: S.num(1, 60, 1, "min") },
    { name: "soak_min", selector: S.num(0, 120, 1, "min") },
  ] },
  people: { label: "People guard", group: "people", step: "people", key: "people", schema: [
    { name: "on", selector: S.bool },
    { name: "clear_min", selector: S.num(0, 30, 0.5, "min") },
    { name: "max_wait_min", selector: S.num(0, 240, 5, "min") },
    { name: "manual", selector: S.bool },
  ] },
  weather: { label: "Rain & weather", group: "weather", step: "weather", key: "weather", schema: [
    { name: "on", selector: S.bool },
    { name: "rain_sensor", selector: S.ent(["sensor", "binary_sensor"]) },
    { name: "rain_mm", selector: S.num(0.5, 100, 0.5, "mm") },
    { name: "weather_entity", selector: S.ent(["weather"]) },
    { name: "fc_prob", selector: S.num(0, 100, 5, "%") },
    { name: "fc_mm", selector: S.num(0, 50, 0.5, "mm") },
    { name: "wind_sensor", selector: S.ent(["sensor"]) },
    { name: "wind_kmh", selector: S.num(0, 100, 1, "km/h") },
    { name: "temp_sensor", selector: S.ent(["sensor"]) },
    { name: "cold_on", selector: S.bool },
    { name: "cold_c", selector: S.num(-10, 15, 1, "°C") },
    { name: "boost_pct", selector: S.num(100, 200, 5, "%") },
  ] },
  main: { label: "Main water", group: "main", step: "main", key: "main", schema: [
    { name: "valve", selector: S.ent(["switch", "valve", "input_boolean", "light"]) },
    { name: "wait_min", selector: S.num(1, 240, 1, "min") },
    { name: "auto_close", selector: S.bool },
    { name: "remind_min", selector: S.num(0, 720, 5, "min") },
    { name: "remind_cycle", selector: S.bool },
  ] },
  offline: { label: "Offline valves", group: "offline", step: "offline", key: "offline", schema: [
    { name: "wait_min", selector: S.num(1, 240, 5, "min") },
    { name: "no_answer", selector: { select: { mode: "dropdown", options: [
      { value: "stop", label: "Stop the cycle" }, { value: "skip", label: "Skip the offline tap and carry on" }] } } },
    { name: "ask_ahead", selector: S.bool },
  ] },
  battery: { label: "Batteries", group: "battery", step: "battery", key: "battery", schema: [
    { name: "pct", selector: S.num(0, 100, 5, "%") },
    { name: "remind_h", selector: S.num(0, 48, 0.5, "h") },
    { name: "quiet_on", selector: S.bool },
    { name: "quiet_from", selector: S.time },
    { name: "quiet_to", selector: S.time },
    { name: "report_on", selector: S.bool },
    { name: "report_days", selector: S.num(1, 90, 1, "days") },
    { name: "report_time", selector: S.time },
    { name: "report_start", selector: S.date },
  ] },
  counts: { label: "Taps & cycles", group: "counts", step: "counts", schema: [
    { name: "tap_count", selector: S.num(1, 16) },
    { name: "cycle_count", selector: S.num(1, 12) },
  ] },
};
// value sent for a field that was cleared (the form drops cleared fields)
const EMPTY = (sel) => (sel.boolean ? false
  : (sel.entity && sel.entity.multiple) || (sel.device && sel.device.multiple) ? []
    : sel.number ? sel.number.min : "");

// The HA form (entity pickers etc.) is only loaded once some editor needs it: load it the way card editors do.
let haFormReady;
const loadHaForm = () => haFormReady || (haFormReady = (async () => {
  if (customElements.get("ha-form")) return;
  try {
    const helpers = await window.loadCardHelpers();
    for (const type of ["entities", "tile"]) {
      if (customElements.get("ha-form")) break;
      const card = await helpers.createCardElement({ type, entities: [], entity: "sun.sun" });
      if (card && card.constructor.getConfigElement) await card.constructor.getConfigElement();
    }
  } catch (e) { /* fall through: wait below */ }
  await Promise.race([customElements.whenDefined("ha-form"), new Promise((r) => setTimeout(r, 10000))]);
})());

const navigate = (path) => {
  history.pushState(null, "", path);
  window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
};

class IrrigationSchedulerSettings extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "taps";
    this._tap = 0;
    this._s = null; // {entry_id, title, options}
    this._drafts = {}; // edits not saved yet, per group ("general", "tap:0", ...)
    this._pending = new Set(); // groups waiting to save (every tab but "Taps & cycles" saves by itself)
    this._msg = "";
    this._status = ""; // "", "saving", "saved", "error"
    this.pageLink = false;
  }

  disconnectedCallback() { if (this._pending.size) this._flush(true); } // closed or left: save now

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._form) this._form.hass = hass;
    if (first) this._init();
    // settings changed elsewhere (another phone, the Configure screen): fetch again so the
    // form never shows, or saves back, old values; unsaved edits are kept
    const st = this._s && this._s.status_entity ? hass.states[this._s.status_entity] : undefined;
    const rev = st ? st.attributes.config_rev : undefined;
    if (rev !== undefined && rev !== this._s.config_rev && rev !== this._revFetched && !this._busy) {
      this._revFetched = rev;
      this._fetch();
    }
  }

  get hass() { return this._hass; }

  set entryId(id) {
    if (!id || id === this._entryId) return;
    this._entryId = id;
    this._s = null;
    this._drafts = {};
    this._fetch();
  }

  async _init() {
    this._frame();
    this._render();
    await loadHaForm();
    try {
      this._localize = await this._hass.loadBackendTranslation("options", DOMAIN);
    } catch (e) {
      this._localize = null;
    }
    this._ready = true;
    this._fetch();
  }

  _t(path) { // text from the integration's Configure screens ("" if there is none)
    const key = `component.${DOMAIN}.options.step.${path}`;
    const fn = this._localize || (this._hass && this._hass.localize);
    const v = fn ? fn(key) : "";
    return v && v !== key ? v : "";
  }

  async _fetch(retry = 0) {
    if (!this._ready || !this._entryId || !this._hass) { this._render(); return; }
    try {
      this._s = await this._hass.callWS({ type: `${DOMAIN}/settings`, entry_id: this._entryId });
      this._err = "";
    } catch (e) {
      if (retry < 10) { // e.g. reloading after the number of taps changed
        setTimeout(() => this._fetch(retry + 1), 1500);
        return;
      }
      this._err = `Could not load the settings: ${e.message || e}`;
    }
    this._render();
  }

  open(tab, tap) { // jump to a tab (and tap), e.g. from a tap's "Tap settings"
    if (this._pending.size) this._flush(true);
    if (GROUPS[tab]) this._tab = tab;
    if (tap !== undefined && tap !== null) this._tap = tap;
    this._msg = "";
    this._render();
    this.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  _key() { return this._tab === "taps" ? `tap:${this._tap}` : this._tab; }

  _saved() {
    const o = this._s.options;
    if (this._tab === "taps") return o.taps[this._tap] || {};
    if (this._tab === "counts") return { tap_count: o.tap_count, cycle_count: o.cycle_count };
    return o[GROUPS[this._tab].key] || {};
  }

  _data() { return this._drafts[this._key()] || { ...this._saved() }; }

  _frame() {
    if (this._body) return;
    this.shadowRoot.innerHTML = `<style>${SETTINGS_CSS}</style>
      <div class="head"></div><div class="tabs" role="tablist"></div><div class="sub"></div>
      <div class="note"></div><div class="form"></div><div class="foot"></div>`;
    const q = (c) => this.shadowRoot.querySelector(c);
    [this._head, this._tabs, this._sub, this._note, this._body, this._foot] =
      [".head", ".tabs", ".sub", ".note", ".form", ".foot"].map(q);
    this.shadowRoot.addEventListener("click", (ev) => {
      const el = ev.target.closest("[data-a]");
      if (!el) return;
      ev.preventDefault();
      this._click(el);
    });
  }

  _render() {
    this._frame();
    const s = this._s;
    // which Irrigation Scheduler these settings belong to (there can be several, e.g. a test one)
    this._head.innerHTML = `<span class="h">Settings${s && s.title ? ` <span class="who">· ${esc(s.title)}</span>` : ""}</span>`
      + (this.pageLink ? `<a href="${PANEL_PATH}" data-a="page">Open the Irrigation page</a>` : "");
    if (!s || !this._ready) {
      this._tabs.innerHTML = this._sub.innerHTML = this._foot.innerHTML = "";
      this._note.textContent = this._err || "Loading…";
      this._body.hidden = true;
      return;
    }
    if (this._tap >= s.options.taps.length) this._tap = 0;
    const draftIn = (id) => Object.keys(this._drafts).some((k) => k === id || (id === "taps" && k.startsWith("tap:")));
    this._tabs.innerHTML = Object.entries(GROUPS).map(([id, g]) => `<button role="tab" class="tab ${id === this._tab ? "on" : ""}"
      data-a="tab" data-v="${id}" aria-selected="${id === this._tab}">${g.label}${draftIn(id) ? ` <i title="Not saved">•</i>` : ""}</button>`).join("");
    this._sub.innerHTML = this._tab !== "taps" ? "" : s.options.taps.map((t, i) => `<button class="chip ${i === this._tap ? "on" : ""}"
      data-a="tap" data-v="${i}">${i + 1}${t.name ? ` · ${esc(t.name)}` : ""}${this._drafts[`tap:${i}`] ? " •" : ""}</button>`).join("");
    const g = GROUPS[this._tab];
    const desc = this._t(`${g.step}.description`).replace(/\{[a-z_]+\}/g, "").trim();
    this._note.innerHTML = (this._tab === "taps"
      ? "Minutes, run order and soak per cycle, and each cycle's start time and days: tap a cell or a cycle header on the grid. "
      : "") + esc(desc);
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (ev) => this._changed(ev.detail.value));
      // leaving a box you typed in saves it straight away
      this._body.addEventListener("focusout", () => {
        if (this._pending.size) setTimeout(() => this._flush(), 150);
      });
      this._body.appendChild(this._form);
    }
    this._body.hidden = false;
    this._form.hass = this._hass;
    this._form.computeLabel = (f) => this._t(`${g.step}.data.${f.name}`) || f.name.replace(/_/g, " ");
    this._form.computeHelper = (f) => this._t(`${g.step}.data_description.${f.name}`);
    this._form.schema = g.schema;
    this._form.data = this._data();
    const dirty = !!this._drafts[this._key()];
    if (this._tab === "counts") { // a reload: saved only with the button
      this._foot.innerHTML = `<span class="msg ${this._msgErr ? "err" : ""}">${esc(this._msg
        || "Changing the number of taps or cycles reloads the app; press Save when ready.")}</span>
        <button class="act" data-a="revert" ${dirty && !this._busy ? "" : "disabled"}>Revert</button>
        <button class="act primary" data-a="save" ${dirty && !this._busy ? "" : "disabled"}>${this._busy ? "Saving…" : "Save"}</button>`;
      return;
    }
    const err = this._status === "error";
    const txt = this._pending.size || this._status === "saving" ? "Saving…"
      : err ? `Not saved: ${this._msg}`
        : this._status === "saved" ? "All changes saved" : "Changes save automatically";
    const st = s.status_entity && this._hass.states[s.status_entity];
    const next = st && st.attributes.battery_report_next;
    const report = this._tab !== "battery" ? "" : `<span class="msg">${next ? `Next battery report ${esc(dateText(next))}.` : "The battery report is off."}${this._reportMsg ? ` ${esc(this._reportMsg)}` : ""}</span>
      <button class="act" data-a="report" title="Send the battery report to your phone now">Send report now</button>`;
    this._foot.innerHTML = `${report}<span class="msg ${err ? "err" : ""}">${esc(txt)}</span>
      ${err && dirty ? `<button class="act" data-a="revert">Undo</button><button class="act primary" data-a="retry">Try again</button>` : ""}`;
  }

  async _report() {
    try {
      await this._hass.callService(DOMAIN, "send_battery_report", { config_entry_id: this._entryId });
      this._reportMsg = "Sent.";
    } catch (e) {
      this._reportMsg = `Not sent: ${e.message || e}`;
    }
    this._render();
  }

  _changed(value) {
    const key = this._key();
    const g = GROUPS[this._tab];
    const before = this._data();
    this._drafts[key] = value;
    this._msg = "";
    if (this._tab !== "counts") {
      const same = (a, b, f) => JSON.stringify(a[f.name] ?? EMPTY(f.selector)) === JSON.stringify(b[f.name] ?? EMPTY(f.selector));
      if (g.schema.every((f) => same(value, this._saved(), f))) {
        delete this._drafts[key]; // back to what is saved (or the form only re-sent it): nothing to do
        this._pending.delete(key);
      } else {
        // a box being typed in (number, text) saves when you pause or leave it; a switch or picker at once
        this._typing = g.schema.some((f) => (f.selector.text || f.selector.number) && !same(value, before, f));
        this._pending.add(key);
        this._waited = false;
        clearTimeout(this._timer);
        this._timer = setTimeout(() => this._flush(), this._typing ? 1200 : 300);
      }
    }
    this._render();
  }

  _typingNow() { // is the cursor in a text / number box of this form?
    let el = this.shadowRoot.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    return !!el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && !["checkbox", "radio"].includes(el.type)));
  }

  async _flush(now = false) {
    clearTimeout(this._timer);
    this._timer = null;
    if (!this._pending.size) return;
    if (!now && this._typing && !this._waited && this._typingNow()) {
      // the cursor is still in the box: give it a little longer (leaving the box saves at once)
      this._waited = true;
      this._timer = setTimeout(() => this._flush(), 1500);
      return;
    }
    if (this._busy) { this._flushAgain = true; return; }
    this._busy = true;
    while (this._pending.size) {
      const key = this._pending.values().next().value;
      this._pending.delete(key);
      await this._saveKey(key);
    }
    this._busy = false;
    if (this._flushAgain) {
      this._flushAgain = false;
      if (this._pending.size) this._flush();
    }
    this._render();
  }

  async _saveKey(key) { // save one group ("general", "tap:2", ...) as it is in its draft now
    const draft = this._drafts[key];
    if (!draft) return;
    const tab = key.startsWith("tap:") ? "taps" : key;
    const g = GROUPS[tab];
    const data = {};
    for (const f of g.schema) {
      const v = draft[f.name];
      data[f.name] = v === undefined || v === null ? EMPTY(f.selector) : v;
    }
    const msg = { type: `${DOMAIN}/save_settings`, entry_id: this._entryId, group: g.group, data };
    if (tab === "taps") msg.tap = Number(key.slice(4)) + 1;
    this._status = "saving";
    this._render();
    try {
      const res = await this._hass.callWS(msg);
      if (this._drafts[key] === draft) delete this._drafts[key]; // not changed again meanwhile
      this._s = res;
      this._status = "saved";
    } catch (e) {
      this._status = "error";
      this._msg = e.message || String(e);
    }
    this._render();
  }

  _click(el) {
    const a = el.dataset.a;
    if (a === "page") navigate(PANEL_PATH);
    else if (a === "tab") { this._flush(true); this._tab = el.dataset.v; this._msg = ""; this._render(); }
    else if (a === "tap") { this._flush(true); this._tap = Number(el.dataset.v); this._msg = ""; this._render(); }
    else if (a === "revert") {
      delete this._drafts[this._key()];
      this._pending.delete(this._key());
      this._msg = "";
      this._status = "";
      this._render();
    } else if (a === "retry") { this._pending.add(this._key()); this._flush(true); }
    else if (a === "save") this._save();
    else if (a === "report") this._report();
  }

  async _save() {
    const g = GROUPS[this._tab];
    const d = this._data();
    const data = {};
    for (const f of g.schema) {
      const v = d[f.name];
      data[f.name] = v === undefined || v === null ? EMPTY(f.selector) : v;
    }
    const msg = { type: `${DOMAIN}/save_settings`, entry_id: this._entryId, group: g.group, data };
    if (this._tab === "taps") msg.tap = this._tap + 1;
    const key = this._key();
    this._busy = true;
    this._render();
    try {
      const res = await this._hass.callWS(msg);
      delete this._drafts[key];
      this._s = res;
      this._msg = res.reloading ? "Saved. Reloading with the new number of taps / cycles…" : "Saved.";
      this._msgErr = false;
      if (res.reloading) {
        this._drafts = {};
        setTimeout(() => this._fetch(), 2500);
      }
    } catch (e) {
      this._msg = e.message || String(e);
      this._msgErr = true;
    }
    this._busy = false;
    this._render();
  }
}

const SETTINGS_CSS = `
  :host { display: block; }
  .head { display: flex; align-items: baseline; gap: 12px; margin: 0 2px 8px; }
  .head .h { flex: 1; font-weight: 500; font-size: 1.05em; }
  .head .who { font-weight: 400; color: var(--secondary-text-color); }
  .head a { font-size: .85em; color: var(--primary-color); text-decoration: none; }
  .tabs { display: flex; flex-wrap: wrap; gap: 6px; }
  .tab, .chip { flex: none; font: inherit; cursor: pointer; white-space: nowrap; border-radius: 999px;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .tab { font-size: .85em; padding: 6px 12px; }
  .chip { font-size: .8em; padding: 4px 10px; }
  .tab.on, .chip.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .tab i { font-style: normal; color: var(--warning-color, #ffa600); }
  .tab.on i { color: inherit; }
  .sub { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .sub:empty, .note:empty { display: none; }
  .note { margin: 10px 2px 4px; font-size: .82em; line-height: 1.4; color: var(--secondary-text-color); }
  .form { margin-top: 8px; }
  .foot { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 12px; }
  .foot .msg { flex: 1; font-size: .85em; color: var(--secondary-text-color); }
  .foot .msg.err { color: var(--error-color, #db4437); }
  .act { font: inherit; font-size: .85em; padding: 6px 14px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .act.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .act:disabled { opacity: .45; cursor: default; }
`;

// ------------------------------------------------------------------ the Irrigation page (sidebar)
class IrrigationSchedulerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set narrow(v) {
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
  }

  set panel(v) { this._panel = v; }

  set route(v) { this._route = v; }

  set hass(hass) {
    this._hass = hass;
    if (!this._menu) this._build();
    this._menu.hass = hass;
    if (this._card) this._card.hass = hass;
    if (this._settings) this._settings.hass = hass;
    if (!this._entries && !this._loadingEntries) this._loadEntries();
  }

  _build() {
    this.shadowRoot.innerHTML = `<style>${PANEL_CSS}</style>
      <div class="toolbar"><ha-menu-button></ha-menu-button><div class="title">Irrigation</div></div>
      <div class="content"><div class="entries" role="tablist" aria-label="Irrigation Scheduler"></div>
        <div class="grid"></div><ha-card class="box" hidden></ha-card></div>`;
    this._menu = this.shadowRoot.querySelector("ha-menu-button");
    this._menu.narrow = this._narrow;
    // several Irrigation Schedulers (e.g. a test one): one button each, the one shown is highlighted
    this._pick = this.shadowRoot.querySelector(".entries");
    this._pick.addEventListener("click", (ev) => {
      const b = ev.target.closest("button[data-id]");
      if (b && b.dataset.id !== this._shown) this._select(b.dataset.id, true);
    });
    // a tap's "Tap settings" on the grid opens that tap in the settings below
    this.shadowRoot.addEventListener("irrigation-open-settings", (ev) => {
      if (this._settings) this._settings.open(ev.detail.tab, ev.detail.tap);
    });
  }

  async _loadEntries() {
    this._loadingEntries = true;
    try {
      this._entries = (await this._hass.callWS({ type: `${DOMAIN}/config` })).entries
        .sort((a, b) => a.title.localeCompare(b.title));
    } catch (e) {
      this._entries = [];
    }
    this._loadingEntries = false;
    let id = null;
    try { id = localStorage.getItem(PAGE_CHOICE); } catch (e) { /* private mode */ }
    if (!this._entries.some((e) => e.entry_id === id)) id = this._entries.length ? this._entries[0].entry_id : null;
    this._select(id);
  }

  _select(id, chosen = false) {
    const grid = this.shadowRoot.querySelector(".grid");
    const box = this.shadowRoot.querySelector(".box");
    this._shown = id;
    this._pick.innerHTML = this._entries.length > 1 ? this._entries.map((e) => `<button role="tab"
      data-id="${e.entry_id}" class="${e.entry_id === id ? "on" : ""}" aria-selected="${e.entry_id === id}">${esc(e.title)}</button>`).join("") : "";
    if (!id) {
      grid.innerHTML = `<ha-card><div class="empty">Irrigation Scheduler is not set up (or not loaded). Add it under Settings → Devices &amp; services.</div></ha-card>`;
      box.hidden = true;
      return;
    }
    if (chosen) { // remember only a choice the user made
      try { localStorage.setItem(PAGE_CHOICE, id); } catch (e) { /* private mode */ }
    }
    const entry = this._entries.find((e) => e.entry_id === id);
    this._card = document.createElement("irrigation-sequencer-card");
    this._card.setConfig({ title: entry ? entry.title : "Irrigation", entry_id: id, in_panel: true });
    this._card.hass = this._hass;
    grid.replaceChildren(this._card);
    if (!this._settings) {
      this._settings = document.createElement("irrigation-sequencer-settings");
      box.appendChild(this._settings);
    }
    box.hidden = false;
    this._settings.hass = this._hass;
    this._settings.entryId = id;
  }
}

const PANEL_CSS = `
  :host { display: block; height: 100%; overflow-y: auto; background: var(--primary-background-color); color: var(--primary-text-color); }
  .toolbar { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; box-sizing: border-box;
    height: var(--header-height, 56px); padding: 0 12px; font-size: 20px;
    background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, var(--text-primary-color, #fff));
    border-bottom: var(--app-header-border-bottom, none); }
  .title { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .entries { display: flex; flex-wrap: wrap; gap: 8px; }
  .entries:empty { display: none; }
  .entries button { font: inherit; font-size: 14px; padding: 6px 16px; border-radius: 999px; cursor: pointer;
    border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  .entries button.on { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .content { box-sizing: border-box; max-width: 1040px; margin: 0 auto; padding: 16px; display: grid; gap: 16px;
    grid-template-columns: minmax(0, 1fr); } /* lets the grid scroll inside its card on a phone */
  .content > * { min-width: 0; }
  .box { display: block; padding: 14px 16px 16px; }
  .box[hidden] { display: none; }
  .empty { padding: 16px; color: var(--secondary-text-color); }
  @media (max-width: 600px) { .content { padding: 8px; gap: 8px; } .box { padding: 12px; } }
`;

if (!customElements.get("irrigation-sequencer-card")) {
  customElements.define("irrigation-sequencer-card", IrrigationSchedulerCard);
  customElements.define("irrigation-sequencer-card-editor", IrrigationSchedulerCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "irrigation-sequencer-card",
    name: "Irrigation Scheduler",
    description: "Taps x cycles grid with live status; tap a cell to edit minutes, run order and soak.",
    preview: false,
    documentationURL: "https://github.com/arrence-1/irrigation_Scheduler",
    getEntitySuggestion: (hass, entityId) => {
      const ent = hass.entities && hass.entities[entityId];
      if (!ent || ent.platform !== DOMAIN || !entityId.startsWith("sensor.") || !entityId.endsWith("_status")) return null;
      return { config: { type: "custom:irrigation-sequencer-card" } };
    },
  });
  console.info(`%c IRRIGATION-SCHEDULER-CARD %c v${CARD_VERSION} `, "background:#039be5;color:#fff", "");
}
if (!customElements.get("irrigation-sequencer-settings")) {
  customElements.define("irrigation-sequencer-settings", IrrigationSchedulerSettings);
}
if (!customElements.get("irrigation-sequencer-panel")) {
  customElements.define("irrigation-sequencer-panel", IrrigationSchedulerPanel);
}
