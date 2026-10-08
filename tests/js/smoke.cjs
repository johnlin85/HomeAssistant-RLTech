// DOM smoke test for the panel and the two table cards (stage 5).
// Needs Node and jsdom (NODE_PATH or a local node_modules); run through
// tests/test_frontend.py, which skips when either is missing.
"use strict";

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const WWW = path.join(__dirname, "..", "..", "custom_components", "rltech_fttr", "www");

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  runScripts: "outside-only",
  url: "http://ha.local/",
});
const { window } = dom;
window.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
for (const file of [
  "rltech-fttr-ap-table-card.js",
  "rltech-fttr-station-table-card.js",
  "rltech-fttr-panel.js",
]) {
  window.eval(fs.readFileSync(path.join(WWW, file), "utf8"));
}
// Loading twice (Lovelace resource + panel import) must not throw.
window.eval(fs.readFileSync(path.join(WWW, "rltech-fttr-ap-table-card.js"), "utf8"));

const AP_B = "E0:21:FE:B0:B0:00";
const SOURCES_BUSY = {
  web: { state: "busy", last_success: "2026-09-30T01:02:00+00:00", busy_since: "2026-09-30T01:03:00+00:00", paused_until: null, available: true },
  mqtt: { enabled: true, connected: true, last_message: null, error_kind: null },
  stations_source: "mqtt",
  stations_reason: "disabled_by_mqtt",
};

function fakeHass(language, calls) {
  return {
    language,
    locale: { language },
    callWS: async (msg) => {
      calls.push(msg);
      if (msg.type === "rltech_fttr/get_entries") {
        return { entries: [{ entry_id: "e1", title: "RLTech FTTR 198.51.100.29", sources: SOURCES_BUSY }] };
      }
      if (msg.type === "rltech_fttr/get_access_points") {
        return {
          access_points: [
            {
              mac: AP_B, alias: "820", sn: "RLGMFEB0B000", online: true, station_count_reported: 4,
              cpu_usage: 5, memory_usage: 40,
              entities: { cpu_usage: "sensor.rltech_ap_rlgmfeb0b000_cpu_usage" },
              disabled_entities: ["memory_usage"],
            },
          ],
          total: 1, filtered: 1, page: 0, page_count: 1, filter_options: {}, sources: SOURCES_BUSY,
        };
      }
      if (msg.type === "rltech_fttr/get_stations") {
        return {
          stations: [{ mac: "02:00:00:00:00:01", ap_mac: AP_B, ap_alias: "820", reported_online: true }],
          total: 1, filtered: 1, page: 0, page_count: 1,
          filter_options: { ap: [{ value: AP_B, label: "820" }], ssid: [], vlan: [], band: [] },
          sources: SOURCES_BUSY,
        };
      }
      throw new Error(`unexpected ${msg.type}`);
    },
    connection: { subscribeMessage: async () => () => {} },
  };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 30));

async function main() {
  // Station card in Chinese, with a fixed AP from the card config.
  const calls = [];
  const station = window.document.createElement("rltech-fttr-station-table-card");
  station.setConfig({ ap_mac: AP_B, remember_preferences: false });
  window.document.body.appendChild(station);
  station.hass = fakeHass("zh-Hans", calls);
  await tick();
  await tick();
  const stationRoot = station.shadowRoot;
  const sent = calls.find((msg) => msg.type === "rltech_fttr/get_stations");
  assert.strictEqual(sent.filters.ap_mac, AP_B, "ap_mac filter is sent");
  assert.ok(!("ap" in sent.filters), "old ap key is not sent");
  assert.ok(stationRoot.getElementById("search").placeholder.includes("搜索"), "zh placeholder");
  assert.ok(stationRoot.getElementById("ap-fixed").textContent.includes("820"), "fixed AP label");
  const chips = stationRoot.getElementById("sources").textContent;
  assert.ok(chips.includes("被占用") && chips.includes("数据停在"), `zh busy chip: ${chips}`);
  assert.ok(chips.includes("MQTT"), "mqtt chip");

  // AP card in English emits show-clients when asked to.
  const apCalls = [];
  const ap = window.document.createElement("rltech-fttr-ap-table-card");
  ap.setConfig({
    show_clients_on_click: true,
    remember_preferences: false,
    columns: ["alias", "online", "cpu_usage", "memory_usage"],
  });
  window.document.body.appendChild(ap);
  ap.hass = fakeHass("en", apCalls);
  await tick();
  await tick();
  const apRoot = ap.shadowRoot;
  assert.ok(apRoot.getElementById("sources").textContent.includes("data frozen at"), "en busy chip");
  let selected = null;
  ap.addEventListener("rltech-fttr-show-clients", (event) => { selected = event.detail; });
  const row = apRoot.querySelector("[data-row]");
  assert.ok(row, "clickable AP row");
  row.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  assert.strictEqual(JSON.stringify(selected), JSON.stringify({ ap_mac: AP_B, label: "820" }));
  // Disabled entities (stage 7 defaults) are not clickable, with a hint.
  const disabledCell = apRoot.querySelector('[data-disabled-entity="memory_usage"]');
  assert.ok(disabledCell, "memory cell marked disabled");
  assert.strictEqual(disabledCell.tagName, "SPAN", "disabled cell is not a button");
  assert.strictEqual(disabledCell.title, "Entity disabled in Home Assistant");
  assert.ok(!apRoot.querySelector('[data-entity*="memory_usage"]'), "no link to a disabled entity");
  assert.ok(apRoot.querySelector('[data-entity="sensor.rltech_ap_rlgmfeb0b000_cpu_usage"]'), "enabled cell links");
  assert.strictEqual(typeof ap.getGridOptions, "function");
  assert.strictEqual(typeof station.getGridOptions, "function");

  // Panel: tabs, status bar, AP click narrows the client list.
  const panelCalls = [];
  const panel = window.document.createElement("rltech-fttr-panel");
  panel.panel = { config: { card_urls: [] } };
  window.document.body.appendChild(panel);
  panel.hass = fakeHass("zh", panelCalls);
  await tick();
  await tick();
  const panelRoot = panel.shadowRoot;
  const tabs = Array.from(panelRoot.querySelectorAll("[data-tab]")).map((b) => b.textContent);
  assert.strictEqual(tabs.join("|"), "接入点|客户端");
  assert.ok(panelRoot.getElementById("sources").textContent.includes("数据停在"), "panel status bar");
  const cards = panelRoot.getElementById("content").children;
  assert.strictEqual(cards.length, 2, "both cards mounted");
  const panelAp = cards[0];
  const panelStations = cards[1];
  assert.ok(panelStations.hidden && !panelAp.hidden, "AP tab first");
  await tick();
  panelAp.shadowRoot.querySelector("[data-row]").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await tick();
  assert.ok(panelAp.hidden && !panelStations.hidden, "switched to clients");
  assert.ok(!panelRoot.getElementById("ap-filter").hidden, "AP filter chip shown");
  await tick();
  const narrowed = panelCalls.filter((msg) => msg.type === "rltech_fttr/get_stations").pop();
  assert.strictEqual(narrowed.filters.ap_mac, AP_B, "panel narrowed clients to the AP");
  panelRoot.getElementById("ap-filter-clear").click();
  await tick();
  assert.ok(panelRoot.getElementById("ap-filter").hidden, "filter cleared");

  // Client data is admin-only (stage 7): a non-admin sees a hint, not an
  // error, and the card does not even ask for the client list.
  const userCalls = [];
  const denied = window.document.createElement("rltech-fttr-station-table-card");
  denied.setConfig({ remember_preferences: false });
  window.document.body.appendChild(denied);
  denied.hass = { ...fakeHass("zh-Hans", userCalls), user: { is_admin: false } };
  await tick();
  await tick();
  assert.ok(!userCalls.some((msg) => msg.type === "rltech_fttr/get_stations"), "no client request");
  const deniedText = denied.shadowRoot.querySelector("[data-empty]").textContent;
  assert.ok(deniedText.includes("管理员权限"), `zh admin hint: ${deniedText}`);

  // Without user info the server's "unauthorized" answer gives the same hint.
  const refusedCalls = [];
  const refusingHass = fakeHass("en", refusedCalls);
  const allowed = refusingHass.callWS;
  refusingHass.callWS = async (msg) => {
    if (msg.type === "rltech_fttr/get_stations") {
      refusedCalls.push(msg);
      throw { code: "unauthorized", message: "Unauthorized" };
    }
    return allowed(msg);
  };
  refusingHass.connection = {
    subscribeMessage: async () => { throw { code: "unauthorized", message: "Unauthorized" }; },
  };
  const refused = window.document.createElement("rltech-fttr-station-table-card");
  refused.setConfig({ remember_preferences: false });
  window.document.body.appendChild(refused);
  refused.hass = refusingHass;
  await tick();
  await tick();
  const refusedText = refused.shadowRoot.querySelector("[data-empty]").textContent;
  assert.strictEqual(refusedText, "Administrator access is required to view clients.");
  assert.ok(!refused.shadowRoot.getElementById("meta").textContent.includes("Unauthorized"), "no raw error");
  const asked = refusedCalls.length;
  refused._scheduleFetch(true);
  await tick();
  assert.strictEqual(refusedCalls.length, asked, "not asked again after unauthorized");

  console.log("frontend smoke ok");
  window.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
