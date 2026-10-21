// Card checks without a browser (jsdom): the main water row, its two-tap buttons, the Main water settings tab.
// Run: npm install --no-save jsdom@26 && node tests/card/test_card.js custom_components/irrigation_sequencer/frontend/irrigation-sequencer-card.js
const fs = require("fs");
const { JSDOM } = require("jsdom");
const src = fs.readFileSync(process.argv[2], "utf8");
const dom = new JSDOM(`<!doctype html><body></body>`, { runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
w.eval(`customElements.define("ha-form", class extends HTMLElement {});`);
w.loadCardHelpers = async () => ({ createCardElement: () => null });
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
  console.log("CARD TESTS OK");
  process.exit(0);
})().catch((e) => fail(e.stack));
