// Card checks without a browser (jsdom): the main water row, its two-tap buttons, the Main water settings tab;
// Off cells keep their run order, offline valves (skip / include), low batteries (snooze), Tap settings toggle.
// Run: npm install --no-save jsdom@26 && node tests/card/test_card.js custom_components/irrigation_sequencer/frontend/irrigation-sequencer-card.js
const fs = require("fs");
const { JSDOM } = require("jsdom");
const src = fs.readFileSync(process.argv[2], "utf8");
const dom = new JSDOM(`<!doctype html><body></body>`, { runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
w.eval(`customElements.define("ha-form", class extends HTMLElement {});`);
w.loadCardHelpers = async () => ({ createCardElement: () => null });
w.Element.prototype.scrollIntoView = function () {}; // not in jsdom
w.eval(src);
const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));
const fail = (m) => { console.error("FAIL:", m); process.exit(1); };
const calls = [];
const cfg = {
  entry_id: "E1", title: "Irrigation", config_rev: 1, max_run_min: 20, main_valve: "light.main_water",
  taps: [{ n: 1, label: "Tap 1 (BL)", name: "BL", valve: "switch.t1", blocked_entity: null }],
  cycles: [{ n: 1, time: "06:00", days: ["mon"], minutes: [5], order: [1], soak: [false], switch: "switch.c1" }],
  entities: { master: "switch.m", status: "sensor.s", next_cycle: "sensor.n", seasonal: "number.p" },
};
const opts = { tap_count: 1, cycle_count: 1, general: {}, notify: {}, soak: {}, people: {}, weather: {},
  main: { valve: "light.main_water", wait_min: 10, auto_close: false }, taps: [{ name: "BL" }], cycles: [] };
function hass(status, attrs, main) {
  const st = (s, a = {}) => ({ state: s, attributes: a, last_updated: String(Math.random()) });
  return {
    config: { time_zone: "Australia/Sydney" }, user: { is_admin: true },
    states: { "sensor.s": st(status, { config_rev: 1, ...attrs }), "switch.m": st("on"), "light.main_water": st(main),
              "switch.t1": st("on"), "switch.c1": st("on"), "number.p": st("100"), "sensor.n": st("unknown") },
    callWS: async (m) => m.type.endsWith("/config") ? { entries: [cfg] }
      : { entry_id: "E1", title: "Irrigation", options: opts, config_rev: 1, status_entity: "sensor.s" },
    callService: async (d, s, data) => { calls.push([d, s, data]); },
    loadBackendTranslation: async () => (k) => "", localize: () => "",
  };
}
(async () => {
  const card = w.document.createElement("irrigation-sequencer-card");
  card.setConfig({});
  w.document.body.appendChild(card);
  card.hass = hass("stuck", { tap: 1, tap_name: "Tap 1 (BL)" }, "on");
  await tick();
  card.hass = hass("stuck", { tap: 1, tap_name: "Tap 1 (BL)" }, "on");
  const root = card.shadowRoot;
  let row = root.querySelector(".mainrow");
  if (!row) fail("no main row while stuck");
  console.log("stuck row:", row.textContent.replace(/\s+/g, " ").trim());
  if (!/Tap 1 \(BL\) can't be confirmed off/.test(row.textContent)) fail("stuck text");
  let btn = row.querySelector("button");
  if (btn.textContent.trim() !== "Close main water") fail("close button: " + btn.textContent);
  btn.click(); await tick();
  btn = root.querySelector(".mainrow button");
  console.log("after 1st tap:", btn.textContent.trim(), btn.className.trim());
  if (btn.textContent.trim() !== "Tap again to close" || calls.length) fail("two-step arm");
  btn.click(); await tick();
  console.log("service:", JSON.stringify(calls));
  if (JSON.stringify(calls[0]) !== JSON.stringify(["irrigation_sequencer", "close_main_water", { config_entry_id: "E1" }])) fail("close call");
  // main off, cycle waiting
  card.hass = hass("held", { pending: [{ cycle: 1 }] }, "off"); await tick();
  row = root.querySelector(".mainrow");
  console.log("off row:", row.textContent.replace(/\s+/g, " ").trim());
  console.log("pill:", root.querySelector(".pill").textContent.trim());
  if (!/main water is off/.test(row.textContent) || row.querySelector('button[data-act="main"]').textContent.trim() !== "Open main water") fail("open row");
  if (root.querySelector(".pill").textContent.trim() !== "Held") fail("pill");
  const chips = [...row.querySelectorAll('button[data-act="snooze"]')].map((b) => b.textContent.trim());
  console.log("snooze chips:", chips.join(" | "));
  if (chips.join() !== "1 h,4 h,12 h") fail("snooze chips");
  row.querySelector('button[data-act="snooze"][data-v="4"]').click(); await tick();
  if (JSON.stringify(calls[1]) !== JSON.stringify(["irrigation_sequencer", "snooze_main_water_reminders", { hours: 4, config_entry_id: "E1" }])) fail("snooze call " + JSON.stringify(calls[1]));
  calls.splice(1);
  root.querySelector('.mainrow button[data-act="main"]').click(); await tick(); root.querySelector('.mainrow button[data-act="main"]').click(); await tick();
  if (calls[1][1] !== "open_main_water") fail("open call");
  card.hass = hass("held", { pending: [{ cycle: 1 }], main_snoozed_until: "2026-10-21T15:00:00+11:00" }, "off"); await tick();
  row = root.querySelector(".mainrow");
  console.log("snoozed row:", row.textContent.replace(/\s+/g, " ").trim());
  if (!/Reminders snoozed until/.test(row.textContent) || !row.querySelector('button[data-v="0"]')) fail("snoozed row");
  // stuck with main already off: no button
  card.hass = hass("stuck", { tap: 1, tap_name: "Tap 1 (BL)" }, "off"); await tick();
  row = root.querySelector(".mainrow");
  console.log("stuck+off row:", row.textContent.replace(/\s+/g, " ").trim(), "| open/close:", row.querySelectorAll('button[data-act="main"]').length);
  if (row.querySelectorAll('button[data-act="main"]').length) fail("no open/close button when stuck and off (snooze only)");
  // main on and idle: no row
  card.hass = hass("idle", {}, "on"); await tick();
  if (root.querySelector(".mainrow")) fail("row when all fine");
  // settings: the Main water tab
  const set = w.document.createElement("irrigation-sequencer-settings");
  w.document.body.appendChild(set);
  set.hass = hass("idle", {}, "on"); set.entryId = "E1";
  await tick(100);
  const tabs = [...set.shadowRoot.querySelectorAll(".tab")].map((b) => b.textContent.trim());
  console.log("tabs:", tabs.join(" | "));
  if (!tabs.includes("Main water")) fail("tab");
  set.shadowRoot.querySelector('.tab[data-v="main"]').click(); await tick();
  const form = set.shadowRoot.querySelector("ha-form");
  console.log("main form fields:", form.schema.map((f) => f.name).join(","), JSON.stringify(form.data));
  if (form.schema.map((f) => f.name).join(",") !== "valve,wait_min,auto_close,remind_min,remind_cycle") fail("schema");
  if (!form.schema[0].selector.entity.filter.domain.includes("light")) fail("light domain");

  // ---------------- two taps: Off cell run order, offline valve, low battery, Tap settings toggle
  const cfg2 = {
    ...cfg, main_valve: "", offline_wait_min: 30, battery_pct: 30,
    taps: [{ n: 1, label: "Tap 1 (BL)", name: "BL", valve: "switch.t1", battery: "sensor.b1", blocked_entity: null },
      { n: 2, label: "Tap 2 (BC)", name: "BC", valve: "switch.t2", battery: "sensor.b2", blocked_entity: null }],
    cycles: [{ n: 1, time: "06:00", days: ["mon"], minutes: [5, 0], order: [1, 2], soak: [false, false], switch: "switch.c1" }],
  };
  const opts2 = { ...opts, tap_count: 2, taps: [{ name: "BL" }, { name: "BC" }] };
  const hass2 = (status, attrs) => {
    const h = hass(status, attrs, "on");
    h.states["switch.t1"] = { state: "off", attributes: {}, last_updated: "1" };
    h.states["switch.t2"] = { state: status === "offline" || attrs.offline ? "unavailable" : "off", attributes: {}, last_updated: "1" };
    h.callWS = async (m) => m.type.endsWith("/config") ? { entries: [cfg2] }
      : { entry_id: "E1", title: "Irrigation", options: opts2, config_rev: 1, status_entity: "sensor.s" };
    return h;
  };
  calls.length = 0;
  const c2 = w.document.createElement("irrigation-sequencer-card");
  c2.setConfig({});
  w.document.body.appendChild(c2);
  const r2 = c2.shadowRoot;
  const waitAttrs = { cycle: 1, offline: [2], offline_waiting: [2], offline_can_skip: [2],
    offline_until: "2026-10-12T06:30:00+11:00", battery: { 1: 61, 2: 22 }, battery_low: [2] };
  c2.hass = hass2("offline", waitAttrs); await tick();
  c2.hass = hass2("offline", waitAttrs); await tick();
  const offCell = r2.querySelector('button.cell.off[data-t="1"]');
  console.log("off cell:", offCell.textContent.replace(/\s+/g, " ").trim(), "| faint:", !!offCell.querySelector(".o.faint"));
  if (offCell.textContent.replace(/\s+/g, "") !== "Off#2" || !offCell.querySelector(".o.faint")) fail("Off cell keeps #2");
  console.log("pill:", r2.querySelector(".pill").textContent.trim());
  if (r2.querySelector(".pill").textContent.trim() !== "Waiting: valve offline") fail("offline pill");
  const rows2 = [...r2.querySelectorAll(".mainrow")].map((r) => r.textContent.replace(/\s+/g, " ").trim());
  console.log("rows:", rows2.join(" || "));
  if (!/^Tap 2 \(BC\) is offline: cycle 1 is waiting until 06:30\.\s*Skip while offline$/.test(rows2[0])) fail("offline row");
  if (!/^Low battery: Tap 2 \(BC\) 22%\.\s*Snooze\s*12 h\s*1 day\s*3 days$/.test(rows2[1])) fail("battery row");
  if (!/offline · battery 22%/.test(r2.querySelector('button.tapbtn[data-t="1"]').textContent.replace(/\s+/g, " "))) fail("tap subtitle");
  r2.querySelector('.mainrow button[data-act="offskip"]').click(); await tick();
  if (r2.querySelector('.mainrow button[data-act="offskip"]').textContent.trim() !== "Tap again to skip" || calls.length) fail("skip arm");
  r2.querySelector('.mainrow button[data-act="offskip"]').click(); await tick();
  r2.querySelector('.mainrow button[data-act="batsnooze"][data-v="24"]').click(); await tick();
  console.log("calls:", JSON.stringify(calls));
  if (JSON.stringify(calls) !== JSON.stringify([["irrigation_sequencer", "skip_offline_tap", { tap: 2, skip: true, config_entry_id: "E1" }],
    ["irrigation_sequencer", "snooze_battery_warnings", { hours: 24, config_entry_id: "E1" }]])) fail("skip / snooze calls");
  // skipped while offline, battery snoozed: no offline row; the tap panel offers Include again
  calls.length = 0;
  const skipAttrs = { offline: [2], offline_skipped: [2], offline_can_skip: [2], battery: { 1: 61, 2: 22 }, battery_low: [2],
    battery_snoozed_until: "2026-10-13T06:00:00+11:00" };
  c2.hass = hass2("idle", skipAttrs); await tick();
  const rows3 = [...r2.querySelectorAll(".mainrow")].map((r) => r.textContent.replace(/\s+/g, " ").trim());
  console.log("rows:", rows3.join(" || "));
  if (rows3.length !== 1 || !/Snoozed until .*Resume$/.test(rows3[0])) fail("snoozed battery row only");
  if (!/offline, skipped/.test(r2.querySelector('button.tapbtn[data-t="1"]').textContent)) fail("skipped subtitle");
  r2.querySelector('button.tapbtn[data-t="1"]').click(); await tick();
  const panel = r2.querySelector(".tappanel");
  console.log("tap panel:", panel.textContent.replace(/\s+/g, " ").trim());
  if (!/battery 22%/.test(panel.textContent) || !panel.querySelector('button[data-act="offinclude"]')) fail("panel battery / include");
  panel.querySelector('button[data-act="offinclude"]').click(); await tick();
  if (JSON.stringify(calls[0]) !== JSON.stringify(["irrigation_sequencer", "skip_offline_tap", { tap: 2, skip: false, config_entry_id: "E1" }])) fail("include call");
  // Tap settings: opens this tap's settings, pressing it again hides them
  let ts = r2.querySelector('.tappanel button[data-act="tapsettings"]');
  if (ts.textContent.trim() !== "Tap settings") fail("tap settings label");
  ts.click(); await tick(100);
  ts = r2.querySelector('.tappanel button[data-act="tapsettings"]');
  const shown = () => !!r2.querySelector("#slot irrigation-sequencer-settings");
  console.log("after 1st press:", ts.textContent.trim(), "| settings shown:", shown());
  if (ts.textContent.trim() !== "Hide tap settings" || !shown()) fail("tap settings open");
  ts.click(); await tick();
  ts = r2.querySelector('.tappanel button[data-act="tapsettings"]');
  console.log("after 2nd press:", ts.textContent.trim(), "| settings shown:", shown());
  if (ts.textContent.trim() !== "Tap settings" || shown()) fail("tap settings toggle closes");
  // settings: the Offline valves and Batteries tabs, the battery report button
  const set2 = w.document.createElement("irrigation-sequencer-settings");
  w.document.body.appendChild(set2);
  set2.hass = hass2("idle", { battery_report_next: "2026-10-26T10:00:00+11:00" }); set2.entryId = "E1";
  await tick(100);
  const tabs2 = [...set2.shadowRoot.querySelectorAll(".tab")].map((b) => b.textContent.trim());
  console.log("tabs:", tabs2.join(" | "));
  if (!tabs2.includes("Offline valves") || !tabs2.includes("Batteries")) fail("new tabs");
  set2.shadowRoot.querySelector('.tab[data-v="taps"]').click(); await tick();
  if (!set2.shadowRoot.querySelector("ha-form").schema.some((f) => f.name === "battery")) fail("tap battery field");
  set2.shadowRoot.querySelector('.tab[data-v="offline"]').click(); await tick();
  let f2 = set2.shadowRoot.querySelector("ha-form");
  console.log("offline fields:", f2.schema.map((f) => f.name).join(","));
  if (f2.schema.map((f) => f.name).join(",") !== "wait_min,no_answer,ask_ahead") fail("offline schema");
  set2.shadowRoot.querySelector('.tab[data-v="battery"]').click(); await tick();
  f2 = set2.shadowRoot.querySelector("ha-form");
  console.log("battery fields:", f2.schema.map((f) => f.name).join(","));
  if (f2.schema.map((f) => f.name).join(",") !== "pct,remind_h,quiet_on,quiet_from,quiet_to,report_on,report_days,report_time,report_start") fail("battery schema");
  const foot = set2.shadowRoot.querySelector(".foot");
  console.log("battery foot:", foot.textContent.replace(/\s+/g, " ").trim());
  if (!/Next battery report Mon 26 Oct 10:00\./.test(foot.textContent)) fail("next report");
  calls.length = 0;
  foot.querySelector('button[data-a="report"]').click(); await tick();
  if (JSON.stringify(calls[0]) !== JSON.stringify(["irrigation_sequencer", "send_battery_report", { config_entry_id: "E1" }])) fail("report call");
  if (!/Sent\./.test(set2.shadowRoot.querySelector(".foot").textContent)) fail("report sent message");
  console.log("CARD TESTS OK");
  process.exit(0);
})().catch((e) => fail(e.stack));
