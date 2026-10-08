const RLTECH_AP_ZH = {
  "More": "更多",
  "MAC": "MAC",
  "IP": "IP",
  "State": "状态",
  "All": "全部",
  "Clear": "清除",
  "Close": "关闭",
  "Columns": "列",
  "View": "视图",
  "Reset view": "重置视图",
  "Rows per page": "每页行数",
  "Sort field": "排序字段",
  "Sort direction": "排序方向",
  "Table options": "表格选项",
  "Prev": "上一页",
  "Next": "下一页",
  "{n} rows": "{n} 行",
  "{matched} matched of {total}": "匹配 {matched} / 共 {total}",
  "All {n}": "全部 {n}",
  "{start}-{end} of {total}": "{start}-{end} / 共 {total}",
  "Multiple RLTech FTTR integrations found. Set entry_id to one of: {list}": "找到多个 RLTech FTTR 集成，请在 entry_id 中指定其一：{list}",
  "No loaded RLTech FTTR integration found.": "没有已加载的 RLTech FTTR 集成。",
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
  "Alias": "别名",
  "Online": "在线",
  "Offline": "离线",
  "Model": "型号",
  "Version": "版本",
  "Profile": "配置",
  "Profile ID": "配置 ID",
  "Assoc": "关联数",
  "Clients": "客户端",
  "2.4 Ch": "2.4G 信道",
  "5 Ch": "5G 信道",
  "2.4 BSSID": "2.4G BSSID",
  "5 BSSID": "5G BSSID",
  "Uplink": "上联",
  "Uplink raw": "上联原始值",
  "Uplink port": "上联端口",
  "SN": "SN",
  "Dev SN": "设备序列号",
  "Upgrade": "升级",
  "Rx dBm": "接收 dBm",
  "Tx dBm": "发送 dBm",
  "CPU %": "CPU %",
  "CPU temp": "CPU 温度",
  "Memory %": "内存 %",
  "Flash %": "Flash %",
  "Last boot": "上次启动",
  "Reg/Off Time": "注册/下线时间",
  "Off reason": "下线原因",
  "ONU status": "ONU 状态",
  "ONU interface": "ONU 接口",
  "ONU source": "ONU 来源",
  "ONU update": "ONU 更新",
  "Search access points": "搜索 AP",
  "No matching access points": "没有匹配的 AP",
  "AP details": "AP 详情",
  "Access point": "AP",
  "Open device": "打开设备",
  "Open history": "查看历史",
  "Entity disabled in Home Assistant": "该实体在 Home Assistant 中已禁用",
  "Show clients of this AP": "查看这台 AP 下的客户端"
};

function rltechFormatTime(value) {
  if (!value) {
    return "";
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "";
  }
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function rltechSourceChips(sources, t) {
  const chips = [];
  const web = sources.web || {};
  const state = web.state || "unknown";
  if (state === "ok") {
    chips.push({ level: "ok", text: `${t("8080 Web UI")}: ${t("OK")}` });
  } else if (state === "paused") {
    const until = rltechFormatTime(web.paused_until);
    chips.push({ level: "warn", text: `${t("8080 Web UI")}: ${t("paused")}${until ? ` ${t("until {time}", { time: until })}` : ""}` });
  } else {
    const frozen = rltechFormatTime(web.last_success);
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

class RltechFttrApTableCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rows = [];
    this._search = "";
    this._filters = { state: "", profile: "", model: "", uplink: "" };
    this._sortKey = "online";
    this._sortDir = 1;
    this._page = 0;
    this._pageSize = 25;
    this._mobilePageSize = 10;
    this._totalRows = 0;
    this._filteredCount = 0;
    this._pageCount = 1;
    this._filterOptions = {};
    this._columns = [];
    this._mobileColumns = [];
    this._columnDefsCache = null;
    this._error = "";
    this._lastFetch = 0;
    this._fetchTimer = null;
    this._fetchInFlight = false;
    this._fetchQueued = false;
    this._refreshTimer = null;
    this._shellRendered = false;
    this._isMobile = false;
    this._mediaQuery = null;
    this._entryResolving = null;
    this._resolvedEntryId = "";
    this._langCode = "en";
    this._sources = null;
    this._preferencesLoadedKey = "";
    this._activeDialog = null;
  }

  // --- i18n (stage 5): English by default, Chinese when HA runs in zh ---
  _lang() {
    const hass = this._hass;
    const lang = (hass && ((hass.locale && hass.locale.language) || hass.language)) || "en";
    return String(lang).toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  _t(text, vars) {
    const table = this._langCode === "zh" ? RLTECH_AP_ZH : null;
    let out = (table && table[text]) || text;
    if (vars) {
      for (const [key, value] of Object.entries(vars)) {
        out = out.split(`{${key}}`).join(String(value));
      }
    }
    return out;
  }

  _relocalize() {
    const lang = this._lang();
    if (lang === this._langCode) {
      return;
    }
    this._langCode = lang;
    this._columnDefsCache = null;
    if (!this._shellRendered) {
      return;
    }
    this._shellRendered = false;
    this._renderShell();
    const search = this.shadowRoot.getElementById("search");
    if (search) {
      search.value = this._search;
    }
    this._renderHeaders();
    this._refreshFilterOptions();
    this._refreshPageSize();
    this._refreshMobileSortControls();
    this._renderSources();
    this._refreshTable();
  }

  // --- data source status bar (stage 5) ---
  _renderSources(sources) {
    if (sources !== undefined) {
      this._sources = sources;
    }
    const bar = this.shadowRoot && this.shadowRoot.getElementById("sources");
    if (!bar) {
      return;
    }
    const data = this._sources;
    if (!data || this._config.show_sources === false) {
      bar.hidden = true;
      bar.innerHTML = "";
      return;
    }
    bar.hidden = false;
    bar.innerHTML = rltechSourceChips(data, (text, vars) => this._t(text, vars))
      .map((chip) => `<span class="chip ${chip.level}">${this._escape(chip.text)}</span>`)
      .join("");
  }

  setConfig(config) {
    this._config = {
      page_size: 25,
      mobile_page_size: 10,
      page_size_options: [25, 50, 100],
      default_sort_key: "online",
      default_sort_dir: 1,
      remember_preferences: true,
      refresh_interval_ms: 60000,
      search_debounce_ms: 150,
      columns: this._defaultColumns(),
      mobile_columns: this._defaultMobileColumns(),
      ...config,
    };
    this._pageSize = Number(this._config.page_size) || 25;
    this._mobilePageSize = Number(this._config.mobile_page_size) || 10;
    this._sortKey = this._config.default_sort_key || "online";
    this._sortDir = this._config.default_sort_dir === -1 ? -1 : 1;
    this._columns = this._validColumns(this._config.columns, this._defaultColumns());
    this._mobileColumns = this._validColumns(this._config.mobile_columns, this._defaultMobileColumns());
    this._setupMediaQuery();
    this._loadPreferences(true);
    this._renderShell();
    this._refreshTable();
  }

  set hass(hass) {
    const firstUpdate = !this._hass;
    this._hass = hass;
    this._relocalize();
    if (firstUpdate || Date.now() - this._lastFetch >= this._autoRefreshMs()) {
      this._scheduleFetch(firstUpdate);
    }
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return {
      columns: "full",
      min_columns: 4,
    };
  }

  disconnectedCallback() {
    this._closeDialog();
    if (this._fetchTimer) {
      window.clearTimeout(this._fetchTimer);
      this._fetchTimer = null;
    }
    if (this._refreshTimer) {
      window.clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  static getStubConfig() {
    return {
      type: "custom:rltech-fttr-ap-table-card",
    };
  }

  _columnDefs() {
    if (this._columnDefsCache) {
      return this._columnDefsCache;
    }
    this._columnDefsCache = [
      ["details", "More", (_row, index) => this._detailsButton(index), () => ""],
      ["alias", "Alias", (row) => this._escape(row.alias || row.mac), (row) => row.alias || row.mac],
      ["mac", "MAC"],
      ["ip", "IP"],
      ["online", "State", (row) => this._stateCell(row), (row) => (row.online ? "online" : "offline"), "online"],
      ["model", "Model"],
      ["version", "Version"],
      ["profile", "Profile", null, null, "profile"],
      ["profile_idx", "Profile ID"],
      ["assoc_count", "Assoc", null, null, "assoc_count"],
      ["station_count_reported", "Clients"],
      ["channel_24", "2.4 Ch"],
      ["channel_5", "5 Ch"],
      ["bssid_24", "2.4 BSSID"],
      ["bssid_5", "5 BSSID"],
      ["uplink_label", "Uplink", (row) => this._escape(this._uplinkLabel(row)), (row) => this._uplinkLabel(row)],
      ["uplink", "Uplink raw"],
      ["uplink_port", "Uplink port"],
      ["sn", "SN"],
      ["dev_sn", "Dev SN"],
      ["upgrade_flag", "Upgrade"],
      ["optical_rx_power", "Rx dBm", null, null, "optical_rx_power"],
      ["optical_tx_power", "Tx dBm", null, null, "optical_tx_power"],
      ["cpu_usage", "CPU %", null, null, "cpu_usage"],
      ["cpu_temperature", "CPU temp", null, null, "cpu_temperature"],
      ["memory_usage", "Memory %", null, null, "memory_usage"],
      ["flash_usage", "Flash %", null, null, "flash_usage"],
      [
        "last_boot",
        "Last boot",
        (row) => this._escape(this._formatDateTime(row.last_boot)),
        (row) => row.last_boot,
        "last_boot",
      ],
      [
        "reg_off_time",
        "Reg/Off Time",
        (row) => this._escape(this._formatDateTime(row.reg_off_time)),
        (row) => row.reg_off_time,
        "reg_off_time",
      ],
      [
        "last_down_cause",
        "Off reason",
        (row) => this._escape(this._formatOffReason(row.last_down_cause)),
        (row) => this._formatOffReason(row.last_down_cause),
        "last_down_cause",
      ],
      ["onu_status", "ONU status"],
      ["interface", "ONU interface"],
      ["source_host", "ONU source", null, null, "source_host"],
      [
        "detail_last_update",
        "ONU update",
        (row) => this._escape(this._formatDateTime(row.detail_last_update)),
        (row) => row.detail_last_update,
      ],
    ].map(([key, label, render, sort, entityKey]) => ({
      key,
      label: this._t(label),
      render: render || ((row) => this._escape(row[key])),
      sort: sort || ((row) => row[key]),
      entityKey,
    }));
    return this._columnDefsCache;
  }

  _defaultColumns() {
    return ["alias", "ip", "online", "assoc_count", "profile", "details"];
  }

  _defaultMobileColumns() {
    return ["alias", "ip", "online", "assoc_count", "details"];
  }

  _activeColumns() {
    return this._isMobile ? this._mobileColumns : this._columns;
  }

  _setActiveColumns(columns) {
    if (this._isMobile) {
      this._mobileColumns = columns;
    } else {
      this._columns = columns;
    }
  }

  _activeDefaultColumns() {
    return this._isMobile ? this._defaultMobileColumns() : this._defaultColumns();
  }

  _activePageSize() {
    return this._isMobile ? this._mobilePageSize : this._pageSize;
  }

  _setActivePageSize(value) {
    if (this._isMobile) {
      this._mobilePageSize = value;
    } else {
      this._pageSize = value;
    }
  }

  _setupMediaQuery() {
    if (this._mediaQuery || !window.matchMedia) {
      this._isMobile = window.innerWidth <= 760;
      return;
    }
    this._mediaQuery = window.matchMedia("(max-width: 760px)");
    this._isMobile = this._mediaQuery.matches;
    const listener = (event) => {
      this._isMobile = event.matches;
      this._page = 0;
      if (this._shellRendered) {
        this._refreshColumnPicker();
        this._renderHeaders();
        this._refreshPageSize();
        this._refreshMobileSortControls();
        this._scheduleFetch(true);
      }
    };
    if (this._mediaQuery.addEventListener) {
      this._mediaQuery.addEventListener("change", listener);
    } else {
      this._mediaQuery.addListener(listener);
    }
  }

  _storageKey() {
    const key = this._config.storage_key || this._config.entry_id || this._resolvedEntryId || "auto";
    return `rltech_fttr.ap_table.${key}`;
  }

  _legacyStorageKeys() {
    if (this._config.storage_key || this._config.entry_id || !this._resolvedEntryId) {
      return [];
    }
    return ["rltech_fttr.ap_table.auto"];
  }

  _loadPreferences(force = false) {
    const storageKey = this._storageKey();
    if (
      !this._config.remember_preferences ||
      (!force && this._preferencesLoadedKey === storageKey)
    ) {
      return;
    }
    this._preferencesLoadedKey = storageKey;
    try {
      let raw = window.localStorage.getItem(storageKey);
      let sourceKey = storageKey;
      if (!raw) {
        for (const key of this._legacyStorageKeys()) {
          raw = window.localStorage.getItem(key);
          if (raw) {
            sourceKey = key;
            break;
          }
        }
      }
      if (!raw) {
        return;
      }
      const prefs = JSON.parse(raw);
      if (Array.isArray(prefs.columns)) {
        this._columns = this._validColumns(prefs.columns, this._defaultColumns());
      }
      if (Array.isArray(prefs.mobile_columns)) {
        this._mobileColumns = this._validColumns(prefs.mobile_columns, this._defaultMobileColumns());
      }
      if (Number.isFinite(Number(prefs.page_size))) {
        this._pageSize = Number(prefs.page_size);
      }
      if (Number.isFinite(Number(prefs.mobile_page_size))) {
        this._mobilePageSize = Number(prefs.mobile_page_size);
      }
      if (typeof prefs.sort_key === "string") {
        this._sortKey = prefs.sort_key;
      }
      if (prefs.sort_dir === 1 || prefs.sort_dir === -1) {
        this._sortDir = prefs.sort_dir;
      }
      if (prefs.filters && typeof prefs.filters === "object") {
        this._filters = { ...this._filters, ...prefs.filters };
      }
      if (sourceKey !== storageKey) {
        this._savePreferences();
      }
    } catch (_) {
      window.localStorage.removeItem(storageKey);
    }
  }

  _savePreferences() {
    if (!this._config.remember_preferences) {
      return;
    }
    try {
      window.localStorage.setItem(
        this._storageKey(),
        JSON.stringify({
          columns: this._columns,
          mobile_columns: this._mobileColumns,
          page_size: this._pageSize,
          mobile_page_size: this._mobilePageSize,
          sort_key: this._sortKey,
          sort_dir: this._sortDir,
          filters: this._filters,
        })
      );
    } catch (_) {
      // Browser storage can be unavailable in restricted web views.
    }
  }

  _resetPreferences() {
    try {
      window.localStorage.removeItem(this._storageKey());
      for (const key of this._legacyStorageKeys()) {
        window.localStorage.removeItem(key);
      }
    } catch (_) {
      // Browser storage can be unavailable in restricted web views.
    }
    this._columns = this._validColumns(this._config.columns, this._defaultColumns());
    this._mobileColumns = this._validColumns(this._config.mobile_columns, this._defaultMobileColumns());
    this._pageSize = Number(this._config.page_size) || 25;
    this._mobilePageSize = Number(this._config.mobile_page_size) || 10;
    this._sortKey = this._config.default_sort_key || "online";
    this._sortDir = this._config.default_sort_dir === -1 ? -1 : 1;
    this._filters = { state: "", profile: "", model: "", uplink: "" };
    this._search = "";
    this._page = 0;
    this.shadowRoot.getElementById("search").value = "";
    this._closeOptions();
    this._refreshFilterOptions();
    this._refreshColumnPicker();
    this._refreshPageSize();
    this._renderHeaders();
    this._refreshMobileSortControls();
    this._scheduleFetch(true);
  }

  _validColumns(columns, fallback) {
    const valid = new Set(this._columnDefs().map((col) => col.key));
    const selected = (Array.isArray(columns) ? columns : fallback).filter((key) => valid.has(key));
    const normalized = selected.filter((key) => key !== "details");
    if (selected.includes("details")) {
      normalized.push("details");
    }
    return normalized.length ? normalized : fallback;
  }

  _scheduleFetch(immediate = false) {
    if (!this._hass) {
      return;
    }
    if (this._fetchInFlight) {
      if (immediate) {
        this._fetchQueued = true;
      }
      return;
    }
    const wait = immediate ? 0 : Math.max(0, 5000 - (Date.now() - this._lastFetch));
    if (this._fetchTimer) {
      if (immediate) {
        window.clearTimeout(this._fetchTimer);
        this._fetchTimer = null;
      } else {
        return;
      }
    }
    this._fetchTimer = window.setTimeout(() => {
      this._fetchTimer = null;
      this._fetch();
    }, wait);
  }

  async _fetch() {
    if (!this._hass) {
      return;
    }
    if (this._fetchInFlight) {
      this._fetchQueued = true;
      return;
    }
    this._fetchInFlight = true;
    try {
      const entryId = await this._entryId();
      if (!entryId) {
        return;
      }
      const result = await this._hass.callWS({
        type: "rltech_fttr/get_access_points",
        entry_id: entryId,
        page: this._page,
        page_size: this._activePageSize(),
        search: this._search,
        sort_key: this._sortKey,
        sort_dir: this._sortDir,
        filters: this._filters,
      });
      this._rows = result.access_points || [];
      this._renderSources(result.sources || null);
      this._page = Number(result.page || 0);
      this._totalRows = Number(result.total || 0);
      this._filteredCount = Number(result.filtered || 0);
      this._pageCount = Number(result.page_count || 1);
      this._filterOptions = result.filter_options || {};
      this._error = "";
      this._refreshFilterOptions();
      this._refreshPageSize();
    } catch (err) {
      this._error = err.message || String(err);
    } finally {
      this._lastFetch = Date.now();
      this._fetchInFlight = false;
      this._refreshTable();
      if (this._fetchQueued) {
        this._fetchQueued = false;
        this._scheduleFetch(true);
      }
    }
  }

  _autoRefreshMs() {
    const configured = Number(this._config?.refresh_interval_ms);
    return Number.isFinite(configured) && configured >= 10000 ? configured : 60000;
  }

  async _entryId() {
    if (this._config.entry_id) {
      this._loadPreferences();
      if (this._shellRendered) {
        this._refreshPageSize();
      }
      return this._config.entry_id;
    }
    if (!this._entryResolving) {
      this._entryResolving = this._hass.callWS({ type: "rltech_fttr/get_entries" });
    }
    try {
      const result = await this._entryResolving;
      const entries = result.entries || [];
      if (entries.length === 1) {
        this._resolvedEntryId = entries[0].entry_id;
        this._loadPreferences(true);
        this._refreshColumnPicker();
        this._renderHeaders();
        this._refreshPageSize();
        return this._resolvedEntryId;
      }
      this._error = entries.length
        ? this._t("Multiple RLTech FTTR integrations found. Set entry_id to one of: {list}", { list: entries.map((entry) => `${entry.title} (${entry.entry_id})`).join(", ") })
        : this._t("No loaded RLTech FTTR integration found.");
      return "";
    } catch (err) {
      this._error = err.message || String(err);
      return "";
    }
  }

  _filteredRows() {
    const search = this._search.trim().toLowerCase();
    const defs = new Map(this._columnDefs().map((col) => [col.key, col]));
    return this._rows
      .filter((row) => {
        const haystack = Object.values(row).filter((value) => value !== null && value !== undefined).join(" ").toLowerCase();
        if (search && !haystack.includes(search)) {
          return false;
        }
        if (this._filters.state) {
          const state = row.online ? "online" : "offline";
          if (state !== this._filters.state) {
            return false;
          }
        }
        if (this._filters.profile && row.profile !== this._filters.profile) {
          return false;
        }
        if (this._filters.model && row.model !== this._filters.model) {
          return false;
        }
        if (this._filters.uplink && this._uplinkLabel(row) !== this._filters.uplink) {
          return false;
        }
        return true;
      })
      .sort((left, right) => {
        const col = defs.get(this._sortKey);
        const a = col ? col.sort(left) : left[this._sortKey];
        const b = col ? col.sort(right) : right[this._sortKey];
        return this._compare(a, b, this._sortDir);
      });
  }

  _pageRows(rows) {
    const pageSize = this._activePageSize();
    if (pageSize === 0) {
      return rows;
    }
    const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    this._page = Math.min(this._page, pageCount - 1);
    const start = this._page * pageSize;
    return rows.slice(start, start + pageSize);
  }

  _renderShell() {
    if (!this.shadowRoot || this._shellRendered) {
      return;
    }
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="wrap">
          <div class="toolbar">
            <input id="search" type="search" placeholder="${this._t("Search access points")}" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
            <select id="state">
              <option value="">${this._t("State")}</option>
              <option value="online">${this._t("Online")}</option>
              <option value="offline">${this._t("Offline")}</option>
            </select>
            <select id="profile"></select>
            <select id="model"></select>
            <select id="uplink"></select>
            <button id="clear-filters" type="button" hidden>Clear</button>
            <select id="page-size" title="${this._t("Rows per page")}"></select>
            <select id="mobile-sort" class="mobile-sort" title="${this._t("Sort field")}" aria-label="${this._t("Sort field")}"></select>
            <button id="mobile-sort-dir" class="mobile-sort mobile-sort-dir" type="button" title="${this._t("Sort direction")}" aria-label="${this._t("Sort direction")}"></button>
            <div class="options">
              <button id="options" class="options-button" type="button" title="${this._t("Table options")}" aria-label="${this._t("Table options")}">
                <span></span><span></span><span></span>
              </button>
            </div>
          </div>
          <div id="sources" class="sources" hidden></div>
          <div id="meta" class="meta"></div>
          <div class="table-wrap">
            <table>
              <thead><tr id="headers"></tr></thead>
              <tbody id="rows"></tbody>
            </table>
          </div>
          <div class="mobile-list" id="mobile-rows"></div>
          <div class="pager">
            <button id="prev" type="button">${this._t("Prev")}</button>
            <span id="page-info"></span>
            <button id="next" type="button">${this._t("Next")}</button>
          </div>
        </div>
      </ha-card>
      <style>
        .sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
        .sources[hidden] { display: none; }
        .sources .chip { border: 1px solid var(--divider-color, #ddd); border-radius: 12px; color: var(--primary-text-color); font-size: 12px; line-height: 18px; padding: 1px 8px; }
        .sources .chip.ok { border-color: var(--success-color, #43a047); }
        .sources .chip.warn { border-color: var(--warning-color, #ffa600); background: color-mix(in srgb, var(--warning-color, #ffa600) 12%, transparent); }
        .sources .chip.error { border-color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); }
        .sources .chip.muted { color: var(--secondary-text-color); }
        .ap-filter { align-items: center; display: inline-flex; gap: 4px; }
        .ap-filter[hidden] { display: none; }
        tr.clickable, .mobile-row.clickable { cursor: pointer; }
        tr.clickable:hover td { background: var(--secondary-background-color, rgba(127, 127, 127, 0.08)); }
        :host {
          display: block;
          min-width: 0;
          width: 100%;
        }
        ha-card {
          display: block;
          max-width: 100%;
          overflow: hidden;
          width: 100%;
        }
        ha-card, .wrap, table, th, td, button, .mobile-list, .details, .entity-cell {
          -webkit-user-select: text;
          user-select: text;
        }
        .wrap {
          box-sizing: border-box;
          min-width: 0;
          padding: 12px;
          width: 100%;
        }
        .toolbar {
          align-items: center;
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-bottom: 8px;
          position: relative;
        }
        .toolbar input {
          flex: 1 1 220px;
          min-width: 160px;
        }
        .toolbar select {
          flex: 0 1 140px;
          min-width: 96px;
        }
        .mobile-sort { display: none; }
        .mobile-sort-dir {
          flex: 0 0 42px;
          min-width: 42px;
          padding: 0;
        }
        input:not([type="checkbox"]), select, button {
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          color: var(--primary-text-color);
          min-height: 32px;
          padding: 0 8px;
        }
        button { cursor: pointer; }
        .meta, #page-info {
          color: var(--secondary-text-color);
          font-size: 12px;
        }
        .options { position: relative; }
        .options-button {
          align-items: center;
          display: inline-flex;
          flex-direction: column;
          gap: 3px;
          justify-content: center;
          min-width: 34px;
          padding: 0;
        }
        .options-button span {
          background: var(--primary-text-color);
          border-radius: 999px;
          display: block;
          height: 2px;
          width: 16px;
        }
        .table-wrap {
          overflow-x: auto;
          overflow-y: visible;
          width: 100%;
        }
        table {
          border-collapse: collapse;
          min-width: max-content;
          width: 100%;
        }
        th, td {
          border-bottom: 1px solid var(--divider-color);
          font-size: 13px;
          padding: 6px 8px;
          text-align: left;
          white-space: nowrap;
        }
        th {
          background: var(--card-background-color);
          position: sticky;
          top: 0;
          z-index: 1;
        }
        th button, .icon-button {
          background: transparent;
          border: 0;
          min-height: 0;
          padding: 0;
        }
        .entity-cell {
          background: transparent;
          border: 0;
          color: var(--primary-color, #1f6feb);
          font: inherit;
          min-height: 0;
          padding: 0;
          text-align: left;
        }
        .state {
          border-radius: 999px;
          display: inline-block;
          font-size: 12px;
          line-height: 1;
          padding: 4px 8px;
        }
        .online {
          background: rgba(36, 161, 72, 0.14);
          color: #1a7f37;
        }
        .offline {
          background: rgba(207, 34, 46, 0.12);
          color: #cf222e;
        }
        .empty {
          color: var(--secondary-text-color);
          padding: 18px 8px;
          text-align: center;
        }
        .mobile-list { display: none; }
        .mobile-row {
          border-bottom: 1px solid var(--divider-color);
          display: grid;
          gap: 5px;
          padding: 10px 0;
        }
        .mobile-main {
          align-items: center;
          display: flex;
          gap: 8px;
          justify-content: space-between;
        }
        .mobile-fields {
          display: grid;
          gap: 4px 10px;
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .mobile-field { min-width: 0; }
        .mobile-label {
          color: var(--secondary-text-color);
          display: block;
          font-size: 11px;
        }
        .mobile-value {
          display: block;
          font-size: 13px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .pager {
          align-items: center;
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin-top: 8px;
        }
        @media (max-width: 760px) {
          .wrap { padding: 10px; }
          .toolbar input,
          .toolbar select {
            flex: 1 1 calc(50% - 8px);
            min-width: 0;
          }
          .mobile-sort { display: block; }
          .mobile-sort-dir {
            align-items: center;
            display: inline-flex;
            justify-content: center;
          }
          .options { position: static; }
          .table-wrap { display: none; }
          .mobile-list { display: block; }
        }
      </style>
    `;

    this.shadowRoot.getElementById("search").addEventListener("input", (event) => {
      this._search = event.target.value;
      this._page = 0;
      this._debouncedFetch();
    });
    for (const key of ["state", "profile", "model", "uplink"]) {
      this.shadowRoot.getElementById(key).addEventListener("change", (event) => {
        this._filters[key] = event.target.value;
        this._page = 0;
        this._savePreferences();
        this._scheduleFetch(true);
      });
    }
    this.shadowRoot.getElementById("clear-filters").addEventListener("click", () => this._clearFilters());
    this.shadowRoot.getElementById("options").addEventListener("click", (event) => {
      event.stopPropagation();
      this._toggleOptions();
    });
    this.shadowRoot.getElementById("page-size").addEventListener("change", (event) => {
      this._setActivePageSize(Number(event.target.value));
      this._page = 0;
      this._savePreferences();
      this._scheduleFetch(true);
    });
    this.shadowRoot.getElementById("mobile-sort").addEventListener("change", (event) => {
      this._sortKey = event.target.value;
      this._page = 0;
      this._savePreferences();
      this._renderHeaders();
      this._refreshMobileSortControls();
      this._scheduleFetch(true);
    });
    this.shadowRoot.getElementById("mobile-sort-dir").addEventListener("click", () => {
      this._sortDir *= -1;
      this._page = 0;
      this._savePreferences();
      this._renderHeaders();
      this._refreshMobileSortControls();
      this._scheduleFetch(true);
    });
    this.shadowRoot.getElementById("prev").addEventListener("click", () => {
      this._page = Math.max(0, this._page - 1);
      this._scheduleFetch(true);
    });
    this.shadowRoot.getElementById("next").addEventListener("click", () => {
      this._page += 1;
      this._scheduleFetch(true);
    });
    this._refreshPageSize();
    this._renderHeaders();
    this._refreshMobileSortControls();
    this._refreshColumnPicker();
    this._refreshFilterOptions();
    this._shellRendered = true;
  }

  _renderHeaders() {
    const defs = new Map(this._columnDefs().map((col) => [col.key, col]));
    this.shadowRoot.getElementById("headers").innerHTML = this._activeColumns()
      .map((key) => defs.get(key))
      .filter(Boolean)
      .map((col) => this._header(col.key, col.label))
      .join("");
    for (const button of this.shadowRoot.querySelectorAll("th button[data-key]")) {
      button.addEventListener("click", () => this._sort(button.dataset.key));
    }
  }

  _refreshMobileSortControls() {
    const select = this.shadowRoot.getElementById("mobile-sort");
    const button = this.shadowRoot.getElementById("mobile-sort-dir");
    if (!select || !button) {
      return;
    }
    const columns = this._columnDefs().filter((col) => col.key !== "details");
    select.innerHTML = columns
      .map((col) => `<option value="${this._escape(col.key)}">${this._escape(col.label)}</option>`)
      .join("");
    if (!columns.some((col) => col.key === this._sortKey)) {
      this._sortKey = columns[0]?.key || "";
    }
    select.value = this._sortKey;
    button.textContent = this._sortDir === 1 ? "^" : "v";
  }

  _refreshColumnPicker() {
    const panel = this._activeDialog?.querySelector("[data-column-panel]");
    if (!panel) {
      return;
    }
    const selected = this._activeColumns();
    panel.innerHTML = this._columnDefs()
      .map((col) => `
        <label>
          <input type="checkbox" value="${this._escape(col.key)}" ${selected.includes(col.key) ? "checked" : ""}>
          ${this._escape(col.label)}
        </label>`)
      .join("");
    for (const input of panel.querySelectorAll("input")) {
      input.addEventListener("change", () => {
        const checked = Array.from(panel.querySelectorAll("input:checked")).map((item) => item.value);
        this._setActiveColumns(this._validColumns(checked, this._activeDefaultColumns()));
        this._savePreferences();
        this._refreshColumnPicker();
        this._renderHeaders();
        this._refreshTable();
      });
    }
  }

  _refreshPageSize() {
    const select = this.shadowRoot.getElementById("page-size");
    const pageSize = this._activePageSize();
    const options = [...new Set([...(this._config.page_size_options || []), pageSize, 0])];
    select.innerHTML = options
      .filter((value) => Number.isFinite(Number(value)))
      .map((value) => `<option value="${Number(value)}">${Number(value) === 0 ? this._t("All") : this._t("{n} rows", { n: Number(value) })}</option>`)
      .join("");
    select.value = String(pageSize);
  }

  _refreshTable() {
    this._renderShell();
    const rows = this._rows;
    const pageSize = this._activePageSize();
    const start = this._filteredCount === 0 ? 0 : pageSize === 0 ? 1 : this._page * pageSize + 1;
    const end = pageSize === 0 ? this._filteredCount : Math.min(this._filteredCount, start + rows.length - 1);
    this.shadowRoot.getElementById("meta").textContent =
      `${this._t("{matched} matched of {total}", { matched: this._filteredCount, total: this._totalRows })}${this._error ? ` - ${this._error}` : ""}`;
    this.shadowRoot.getElementById("page-info").textContent =
      pageSize === 0 ? this._t("All {n}", { n: this._filteredCount }) : this._t("{start}-{end} of {total}", { start, end, total: this._filteredCount });
    this.shadowRoot.getElementById("prev").disabled = this._page <= 0 || pageSize === 0;
    this.shadowRoot.getElementById("next").disabled = pageSize === 0 || this._page >= this._pageCount - 1;
    this.shadowRoot.getElementById("clear-filters").hidden = !this._hasActiveFilters();
    const defs = new Map(this._columnDefs().map((col) => [col.key, col]));
    const columns = this._activeColumns();
    const desktopRows = this.shadowRoot.getElementById("rows");
    const mobileRows = this.shadowRoot.getElementById("mobile-rows");
    if (this._isMobile) {
      desktopRows.innerHTML = "";
      mobileRows.innerHTML = rows.length
        ? rows.map((row, index) => this._mobileRow(row, index, defs, columns)).join("")
        : `<div class="empty">${this._t("No matching access points")}</div>`;
    } else {
      mobileRows.innerHTML = "";
      desktopRows.innerHTML = rows.length
        ? rows.map((row, index) => `<tr${this._clientsClick() ? ` class="clickable" data-row="${index}" title="${this._escape(this._t("Show clients of this AP"))}"` : ""}>${columns.map((key) => `<td>${this._renderCell(row, defs.get(key), index)}</td>`).join("")}</tr>`).join("")
        : `<tr><td class="empty" colspan="${columns.length || 1}">${this._t("No matching access points")}</td></tr>`;
    }
    for (const button of this.shadowRoot.querySelectorAll("button[data-details]")) {
      button.addEventListener("click", () => this._showDetails(rows[Number(button.dataset.details)]));
    }
    if (this._clientsClick()) {
      for (const element of this.shadowRoot.querySelectorAll("[data-row]")) {
        element.addEventListener("click", (event) => {
          if (event.target.closest("button")) {
            return;
          }
          const row = rows[Number(element.dataset.row)];
          if (row) {
            this.dispatchEvent(new CustomEvent("rltech-fttr-show-clients", {
              bubbles: true,
              composed: true,
              detail: { ap_mac: row.mac, label: row.alias || row.sn || row.mac },
            }));
          }
        });
      }
    }
    for (const button of this.shadowRoot.querySelectorAll("button[data-entity]")) {
      button.addEventListener("click", () => this._openMoreInfo(button.dataset.entity));
    }
    for (const button of this.shadowRoot.querySelectorAll("button[data-device]")) {
      button.addEventListener("click", () => this._openDevice(button.dataset.device));
    }
  }

  _mobileRow(row, index, defs, columns) {
    const main = row.alias || row.ip || row.mac || this._t("Access point");
    const fields = columns.filter((key) => key !== "details").slice(0, 6);
    return `
      <div class="mobile-row${this._clientsClick() ? " clickable" : ""}" data-row="${index}">
        <div class="mobile-main">
          <strong>${this._escape(main)}</strong>
          ${this._detailsButton(index)}
        </div>
        <div class="mobile-fields">
          ${fields.map((key) => `<div class="mobile-field"><span class="mobile-label">${this._escape(defs.get(key).label)}</span><span class="mobile-value">${this._renderCell(row, defs.get(key), index)}</span></div>`).join("")}
        </div>
      </div>`;
  }

  _renderCell(row, col, index) {
    if (col.key === "details") {
      return col.render(row, index);
    }
    const value = col.render(row, index);
    if (col.key === "alias" && row.device_id) {
      return `<button class="entity-cell" type="button" data-device="${this._escape(row.device_id)}" title="${this._t("Open device")}">${value}</button>`;
    }
    const entityId = col.entityKey && row.entities ? row.entities[col.entityKey] : null;
    if (!entityId) {
      if (col.entityKey && (row.disabled_entities || []).includes(col.entityKey)) {
        // Disabled in Home Assistant (stage 7 default): no history to open.
        return `<span class="entity-disabled" data-disabled-entity="${this._escape(col.entityKey)}" title="${this._t("Entity disabled in Home Assistant")}">${value}</span>`;
      }
      return value;
    }
    return `<button class="entity-cell" type="button" data-entity="${this._escape(entityId)}" title="${this._t("Open history")}">${value}</button>`;
  }

  _refreshFilterOptions() {
    if (!this.shadowRoot || !this._shellRendered) {
      return;
    }
    this._setOptions("profile", this._t("Profile"));
    this._setOptions("model", this._t("Model"));
    this._setOptions("uplink", this._t("Uplink"));
    this.shadowRoot.getElementById("state").value = this._filters.state;
  }

  _setOptions(id, label) {
    const element = this.shadowRoot.getElementById(id);
    const current = this._filters[id];
    const values = (this._filterOptions[id] || []).map((value) => String(value));
    element.innerHTML = [`<option value="">${label}</option>`, ...values.map((value) => `<option value="${this._escape(value)}">${this._escape(value)}</option>`)].join("");
    element.value = values.includes(current) ? current : "";
    this._filters[id] = element.value;
  }

  _sort(key) {
    if (key === "details") {
      return;
    }
    if (this._sortKey === key) {
      this._sortDir *= -1;
    } else {
      this._sortKey = key;
      this._sortDir = 1;
    }
    this._page = 0;
    this._savePreferences();
    this._renderHeaders();
    this._refreshMobileSortControls();
    this._scheduleFetch(true);
  }

  _showDetails(row) {
    if (!row) {
      return;
    }
    const body = this._columnDefs()
      .filter((col) => col.key !== "details")
      .map((col) => `<div>${this._escape(col.label)}</div><div>${col.render(row)}</div>`)
      .join("");
    this._showDialog(row.alias || row.mac || this._t("AP details"), `<div class="details">${body}</div>`);
  }

  _openMoreInfo(entityId) {
    if (!entityId) {
      return;
    }
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      bubbles: true,
      composed: true,
      detail: { entityId },
    }));
  }

  _openDevice(deviceId) {
    if (!deviceId) {
      return;
    }
    window.history.pushState(null, "", `/config/devices/device/${encodeURIComponent(deviceId)}`);
    window.dispatchEvent(new Event("location-changed"));
  }

  _clearFilters() {
    this._filters = { state: "", profile: "", model: "", uplink: "" };
    this._search = "";
    this._page = 0;
    this.shadowRoot.getElementById("search").value = "";
    this._refreshFilterOptions();
    this._savePreferences();
    this._scheduleFetch(true);
  }

  _hasActiveFilters() {
    return Boolean(this._search.trim() || Object.values(this._filters).some((value) => value));
  }

  _toggleOptions() {
    this._showDialog(
      this._t("Table options"),
      `
        <div class="menu-title">${this._t("Columns")}</div>
        <div data-column-panel class="column-panel"></div>
        <div class="menu-title">${this._t("View")}</div>
        <div class="dialog-actions">
          <button data-reset class="menu-button" type="button">${this._t("Reset view")}</button>
        </div>`,
      { kind: "options", maxWidth: 420 },
    );
    this._activeDialog.querySelector("[data-reset]").addEventListener("click", () => this._resetPreferences());
    this._refreshColumnPicker();
  }

  _closeOptions() {
    if (this._activeDialog?.dataset.kind === "options") {
      this._closeDialog();
    }
  }

  _showDialog(title, body, options = {}) {
    this._closeDialog();
    const overlay = document.createElement("div");
    overlay.className = "rltech-fttr-dialog";
    overlay.dataset.kind = options.kind || "details";
    overlay.innerHTML = `
      <style>
        .rltech-fttr-dialog {
          align-items: flex-start;
          background: rgba(0, 0, 0, 0.35);
          box-sizing: border-box;
          display: flex;
          inset: 0;
          justify-content: center;
          padding: 8vh 10px 16px;
          position: fixed;
          z-index: 2147483647;
        }
        .rltech-fttr-dialog-card {
          background: var(--card-background-color, #fff);
          border-radius: 8px;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
          box-sizing: border-box;
          color: var(--primary-text-color, #111);
          max-height: 80vh;
          max-width: ${Number(options.maxWidth) || 720}px;
          overflow: auto;
          padding: 16px;
          width: min(100%, ${Number(options.maxWidth) || 720}px);
        }
        .dialog-head {
          align-items: center;
          display: flex;
          gap: 12px;
          justify-content: space-between;
          margin-bottom: 12px;
        }
        .details {
          display: grid;
          gap: 6px 14px;
          grid-template-columns: minmax(120px, max-content) 1fr;
        }
        .details div:nth-child(odd),
        .menu-title {
          color: var(--secondary-text-color, #666);
        }
        .menu-title {
          font-size: 12px;
          font-weight: 650;
          margin: 4px 0 6px;
          text-transform: uppercase;
        }
        .column-panel {
          display: grid;
          gap: 6px;
        }
        .column-panel label {
          align-items: center;
          display: flex;
          font-size: 13px;
          gap: 8px;
          line-height: 1.3;
          min-height: 28px;
          white-space: nowrap;
        }
        .column-panel input[type="checkbox"] {
          flex: 0 0 auto;
          height: 16px;
          margin: 0;
          width: 16px;
        }
        .dialog-actions {
          margin-top: 10px;
        }
        .menu-button {
          width: 100%;
        }
        @media (max-width: 760px) {
          .rltech-fttr-dialog {
            padding-top: 4vh;
          }
          .rltech-fttr-dialog-card {
            max-height: 88vh;
            max-width: none;
            width: calc(100vw - 20px);
          }
          .details {
            grid-template-columns: 1fr;
          }
          .column-panel {
            gap: 10px;
          }
          .column-panel label {
            font-size: 15px;
          }
          .column-panel input[type="checkbox"] {
            height: 20px;
            width: 20px;
          }
        }
      </style>
      <div class="rltech-fttr-dialog-card" role="dialog" aria-modal="true">
        <div class="dialog-head">
          <strong>${this._escape(title)}</strong>
          <button data-close type="button">${this._t("Close")}</button>
        </div>
        ${body}
      </div>`;
    overlay.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      const entityButton = target?.closest("[data-entity]");
      if (entityButton) {
        this._closeDialog();
        this._openMoreInfo(entityButton.dataset.entity);
        return;
      }
      const deviceButton = target?.closest("[data-device]");
      if (deviceButton) {
        this._closeDialog();
        this._openDevice(deviceButton.dataset.device);
        return;
      }
      if (event.target === overlay) {
        this._closeDialog();
      }
    });
    overlay.querySelector("[data-close]").addEventListener("click", () => this._closeDialog());
    document.body.appendChild(overlay);
    this._activeDialog = overlay;
  }

  _closeDialog() {
    if (this._activeDialog) {
      this._activeDialog.remove();
      this._activeDialog = null;
    }
  }

  _debouncedFetch() {
    if (this._refreshTimer) {
      window.clearTimeout(this._refreshTimer);
    }
    this._refreshTimer = window.setTimeout(() => {
      this._refreshTimer = null;
      this._scheduleFetch(true);
    }, this._config.search_debounce_ms);
  }

  _detailsButton(index) {
    return `<button class="icon-button" type="button" data-details="${index}" title="More">More</button>`;
  }

  _clientsClick() {
    return this._config && this._config.show_clients_on_click === true;
  }

  _stateCell(row) {
    const online = row.online === true;
    const label = this._t(online ? "Online" : "Offline");
    const cls = online ? "online" : "offline";
    return `<span class="state ${cls}">${label}</span>`;
  }

  _uplinkLabel(row) {
    if (row.uplink === null || row.uplink === undefined) {
      return "";
    }
    const port = row.uplink_port === null || row.uplink_port === undefined ? "" : ` ${row.uplink_port}`;
    if (row.uplink === 0) {
      return `LAN${port}`;
    }
    if (row.uplink === 2) {
      return `LAN-PON${port}`;
    }
    return `Uplink ${row.uplink}${port}`;
  }

  _compare(a, b, direction = 1) {
    if (a === b) {
      return 0;
    }
    if (a === null || a === undefined || a === "") {
      return 1;
    }
    if (b === null || b === undefined || b === "") {
      return -1;
    }
    const na = Number(a);
    const nb = Number(b);
    if (Number.isFinite(na) && Number.isFinite(nb)) {
      return (na - nb) * direction;
    }
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" }) * direction;
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  _header(key, label) {
    const suffix = this._sortKey === key ? (this._sortDir === 1 ? " ^" : " v") : "";
    return `<th><button data-key="${key}">${label}${suffix}</button></th>`;
  }

  _formatDateTime(value) {
    if (!value) {
      return "";
    }
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) {
      return value;
    }
    return new Date(timestamp).toLocaleString();
  }

  _formatOffReason(value) {
    const text = String(value ?? "").trim();
    if (!text) {
      return "";
    }
    const map = {
      0: "none",
      1: "wire down",
      2: "pon los",
      3: "dying gasp",
      4: "reboot",
      5: "deactive",
      6: "manual reg",
      7: "black list",
      8: "admin down",
    };
    return map[text] || text;
  }
}

if (!customElements.get("rltech-fttr-ap-table-card")) customElements.define("rltech-fttr-ap-table-card", RltechFttrApTableCard);

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "rltech-fttr-ap-table-card")) window.customCards.push({
  type: "rltech-fttr-ap-table-card",
  name: "RLTech FTTR AP Table",
  description: "Searchable RLTech FTTR access point inventory",
});
