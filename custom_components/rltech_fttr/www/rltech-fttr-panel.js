// RLTech FTTR sidebar panel (stage 5, plan 3.3.1).
// Reuses the two table cards; no build step, plain custom elements.

const RLTECH_PANEL_ZH = {
  "Access points": "接入点",
  "Clients": "客户端",
  "Integration": "集成",
  "No loaded RLTech FTTR integration found.": "没有已加载的 RLTech FTTR 集成。",
  "Unable to load the table cards: {error}": "无法加载表格卡片：{error}",
  "Clients of {label}": "{label} 下的客户端",
  "Show all clients": "显示全部客户端",
  "8080 Web UI": "8080 网页",
  "OK": "正常",
  "busy": "被占用",
  "unreachable": "无法连接",
  "error": "异常",
  "locked": "登录被锁定",
  "auth_failed": "认证失败",
  "paused": "已暂停",
  "unknown": "未知",
  "data frozen at {time}": "数据停在 {time}",
  "until {time}": "至 {time}",
  "MQTT": "MQTT",
  "live": "在线",
  "offline": "离线",
  "off": "未启用",
  "Clients via {source}": "客户端数据来自 {source}",
};

const RLTECH_PANEL_POLL_MS = 30000;

function rltechPanelTime(value) {
  if (!value) {
    return "";
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "";
  }
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function rltechPanelChips(sources, t) {
  const chips = [];
  const web = sources.web || {};
  const state = web.state || "unknown";
  if (state === "ok") {
    chips.push({ level: "ok", text: `${t("8080 Web UI")}: ${t("OK")}` });
  } else if (state === "paused") {
    const until = rltechPanelTime(web.paused_until);
    chips.push({ level: "warn", text: `${t("8080 Web UI")}: ${t("paused")}${until ? ` ${t("until {time}", { time: until })}` : ""}` });
  } else {
    const frozen = rltechPanelTime(web.last_success);
    const level = state === "busy" ? "warn" : "error";
    chips.push({ level, text: `${t("8080 Web UI")}: ${t(state)}${frozen ? ` · ${t("data frozen at {time}", { time: frozen })}` : ""}` });
  }
  const mqtt = sources.mqtt || {};
  if (!mqtt.enabled) {
    chips.push({ level: "muted", text: `${t("MQTT")}: ${t("off")}` });
  } else {
    chips.push({ level: mqtt.connected ? "ok" : "warn", text: `${t("MQTT")}: ${t(mqtt.connected ? "live" : "offline")}` });
  }
  if (sources.stations_source) {
    chips.push({ level: "muted", text: t("Clients via {source}", { source: sources.stations_source === "mqtt" ? "MQTT" : "HTTP" }) });
  }
  return chips;
}

class RltechFttrPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._entries = [];
    this._entryId = "";
    this._tab = "aps";
    this._cards = {};
    this._apFilter = null;
    this._pollTimer = null;
    this._started = false;
    this._rendered = false;
    this._langCode = "en";
    this._error = "";
    this._narrow = false;
  }

  set hass(hass) {
    this._hass = hass;
    const lang = this._lang();
    if (lang !== this._langCode) {
      this._langCode = lang;
      this._rendered = false;
    }
    this._render();
    for (const card of Object.values(this._cards)) {
      card.ap.hass = hass;
      card.stations.hass = hass;
    }
    if (this._menuButton) {
      this._menuButton.hass = hass;
    }
    if (!this._started) {
      this._started = true;
      this._start();
    }
  }

  set narrow(value) {
    this._narrow = value;
    if (this._menuButton) {
      this._menuButton.narrow = value;
    }
  }

  set panel(value) {
    this._panel = value;
  }

  connectedCallback() {
    if (this._started && !this._pollTimer) {
      this._schedulePoll();
    }
  }

  disconnectedCallback() {
    if (this._pollTimer) {
      window.clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }
  }

  _lang() {
    const hass = this._hass;
    const lang = (hass && ((hass.locale && hass.locale.language) || hass.language)) || "en";
    return String(lang).toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  _t(text, vars) {
    let out = (this._langCode === "zh" && RLTECH_PANEL_ZH[text]) || text;
    if (vars) {
      for (const [key, value] of Object.entries(vars)) {
        out = out.split(`{${key}}`).join(String(value));
      }
    }
    return out;
  }

  _escape(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async _start() {
    // The cards are Lovelace resources; a custom panel does not load those,
    // so import the same versioned URLs the integration registered.
    const urls = (this._panel && this._panel.config && this._panel.config.card_urls) || [];
    for (const url of urls) {
      try {
        await import(url);
      } catch (err) {
        this._error = this._t("Unable to load the table cards: {error}", { error: err.message || String(err) });
      }
    }
    await this._refreshEntries();
    this._schedulePoll();
  }

  _schedulePoll() {
    if (this._pollTimer) {
      window.clearTimeout(this._pollTimer);
    }
    this._pollTimer = window.setTimeout(async () => {
      this._pollTimer = null;
      await this._refreshEntries();
      if (this.isConnected) {
        this._schedulePoll();
      }
    }, RLTECH_PANEL_POLL_MS);
  }

  async _refreshEntries() {
    if (!this._hass) {
      return;
    }
    try {
      const result = await this._hass.callWS({ type: "rltech_fttr/get_entries" });
      const entries = result.entries || [];
      const signature = entries.map((entry) => `${entry.entry_id}:${entry.title}`).join("|");
      if (signature !== this._entriesSignature) {
        // Only rebuild the shell (entry picker) when the entry list changed;
        // re-parenting the cards would reset their timers.
        this._entriesSignature = signature;
        this._rendered = false;
      }
      this._entries = entries;
      if (!this._entries.some((entry) => entry.entry_id === this._entryId)) {
        this._entryId = this._entries.length ? this._entries[0].entry_id : "";
        this._apFilter = null;
      }
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  _currentEntry() {
    return this._entries.find((entry) => entry.entry_id === this._entryId) || null;
  }

  _ensureCards(entryId) {
    if (!entryId || this._cards[entryId]) {
      return this._cards[entryId] || null;
    }
    if (!customElements.get("rltech-fttr-ap-table-card") || !customElements.get("rltech-fttr-station-table-card")) {
      return null;
    }
    const ap = document.createElement("rltech-fttr-ap-table-card");
    ap.setConfig({
      type: "custom:rltech-fttr-ap-table-card",
      entry_id: entryId,
      show_sources: false,
      show_clients_on_click: true,
      storage_key: `panel.${entryId}`,
    });
    const stations = document.createElement("rltech-fttr-station-table-card");
    stations.setConfig({
      type: "custom:rltech-fttr-station-table-card",
      entry_id: entryId,
      show_sources: false,
      storage_key: `panel.${entryId}`,
    });
    ap.addEventListener("rltech-fttr-show-clients", (event) => {
      this._apFilter = { mac: event.detail.ap_mac, label: event.detail.label };
      stations.setApFilter(event.detail.ap_mac);
      this._tab = "stations";
      this._render();
    });
    if (this._hass) {
      ap.hass = this._hass;
      stations.hass = this._hass;
    }
    this._cards[entryId] = { ap, stations };
    return this._cards[entryId];
  }

  _render() {
    if (!this.shadowRoot || !this._hass) {
      return;
    }
    if (!this._rendered) {
      this._renderShell();
      this._rendered = true;
    }
    const entry = this._currentEntry();
    const bar = this.shadowRoot.getElementById("sources");
    bar.innerHTML = entry && entry.sources
      ? rltechPanelChips(entry.sources, (text, vars) => this._t(text, vars))
        .map((chip) => `<span class="chip ${chip.level}">${this._escape(chip.text)}</span>`)
        .join("")
      : "";
    const message = this.shadowRoot.getElementById("message");
    message.textContent = this._error || (this._entries.length ? "" : this._t("No loaded RLTech FTTR integration found."));
    message.hidden = !message.textContent;

    for (const button of this.shadowRoot.querySelectorAll("[data-tab]")) {
      const active = button.dataset.tab === this._tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    }
    const filter = this.shadowRoot.getElementById("ap-filter");
    filter.hidden = !(this._tab === "stations" && this._apFilter);
    if (this._apFilter) {
      this.shadowRoot.getElementById("ap-filter-label").textContent = this._t("Clients of {label}", { label: this._apFilter.label });
    }

    const cards = this._ensureCards(this._entryId);
    const content = this.shadowRoot.getElementById("content");
    const wanted = cards ? [cards.ap, cards.stations] : [];
    for (const child of Array.from(content.children)) {
      if (!wanted.includes(child)) {
        content.removeChild(child);
      }
    }
    if (cards) {
      for (const element of wanted) {
        if (element.parentNode !== content) {
          content.appendChild(element);
        }
      }
      cards.ap.hidden = this._tab !== "aps";
      cards.stations.hidden = this._tab !== "stations";
    }
  }

  _renderShell() {
    const entries = this._entries;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          background: var(--primary-background-color);
          color: var(--primary-text-color);
          display: block;
          height: 100%;
          overflow-x: hidden;
        }
        .header {
          align-items: center;
          background: var(--app-header-background-color, var(--primary-color));
          box-sizing: border-box;
          color: var(--app-header-text-color, var(--text-primary-color, #fff));
          display: flex;
          gap: 8px;
          min-height: 56px;
          padding: 0 12px;
        }
        .title { flex: 1 1 auto; font-size: 20px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .header select { background: transparent; border: 1px solid currentColor; border-radius: 4px; color: inherit; max-width: 45vw; padding: 4px; }
        .header select option { color: var(--primary-text-color); background: var(--card-background-color, #fff); }
        .body { box-sizing: border-box; margin: 0 auto; max-width: 1600px; padding: 12px; width: 100%; }
        .tabs { border-bottom: 1px solid var(--divider-color); display: flex; gap: 4px; margin-bottom: 8px; }
        .tabs button {
          background: none; border: none; border-bottom: 2px solid transparent; color: var(--secondary-text-color);
          cursor: pointer; font: inherit; padding: 8px 12px;
        }
        .tabs button.active { border-bottom-color: var(--primary-color); color: var(--primary-color); }
        .sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
        .chip { border: 1px solid var(--divider-color); border-radius: 12px; font-size: 12px; line-height: 18px; padding: 1px 8px; }
        .chip.ok { border-color: var(--success-color, #43a047); }
        .chip.warn { border-color: var(--warning-color, #ffa600); background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent); }
        .chip.error { border-color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); }
        .chip.muted { color: var(--secondary-text-color); }
        .ap-filter { align-items: center; display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 8px; }
        .ap-filter[hidden], .message[hidden] { display: none; }
        .ap-filter button { background: none; border: 1px solid var(--divider-color); border-radius: 12px; color: var(--primary-color); cursor: pointer; font: inherit; font-size: 12px; padding: 2px 10px; }
        .message { color: var(--secondary-text-color); padding: 16px 0; }
        #content { min-width: 0; }
        #content > [hidden] { display: none; }
      </style>
      <div class="header">
        <span id="menu"></span>
        <div class="title">RLTech FTTR</div>
        ${entries.length > 1 ? `<select id="entry" aria-label="${this._escape(this._t("Integration"))}">${entries.map((entry) => `<option value="${this._escape(entry.entry_id)}"${entry.entry_id === this._entryId ? " selected" : ""}>${this._escape(entry.title)}</option>`).join("")}</select>` : ""}
      </div>
      <div class="body">
        <div class="tabs" role="tablist">
          <button type="button" role="tab" data-tab="aps">${this._escape(this._t("Access points"))}</button>
          <button type="button" role="tab" data-tab="stations">${this._escape(this._t("Clients"))}</button>
        </div>
        <div id="sources" class="sources"></div>
        <div id="ap-filter" class="ap-filter" hidden>
          <span id="ap-filter-label"></span>
          <button id="ap-filter-clear" type="button">${this._escape(this._t("Show all clients"))}</button>
        </div>
        <div id="message" class="message" hidden></div>
        <div id="content"></div>
      </div>
    `;
    this._menuButton = document.createElement("ha-menu-button");
    this._menuButton.hass = this._hass;
    this._menuButton.narrow = this._narrow;
    this.shadowRoot.getElementById("menu").appendChild(this._menuButton);
    for (const button of this.shadowRoot.querySelectorAll("[data-tab]")) {
      button.addEventListener("click", () => {
        this._tab = button.dataset.tab;
        this._render();
      });
    }
    const select = this.shadowRoot.getElementById("entry");
    if (select) {
      select.addEventListener("change", () => {
        this._entryId = select.value;
        this._apFilter = null;
        this._render();
      });
    }
    this.shadowRoot.getElementById("ap-filter-clear").addEventListener("click", () => {
      const cards = this._cards[this._entryId];
      this._apFilter = null;
      if (cards) {
        cards.stations.setApFilter("");
      }
      this._render();
    });
  }
}

if (!customElements.get("rltech-fttr-panel")) {
  customElements.define("rltech-fttr-panel", RltechFttrPanel);
}
