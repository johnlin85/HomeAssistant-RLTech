"""Config flow for RLTech FTTR (plan stage 4, section 3)."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import UNDEFINED

from .api import (
    AccountBusyError,
    AccountLockedError,
    AuthenticationError,
    RltechClient,
    SessionExpired,
    UnexpectedResponse,
)
from .config_logic import (
    DEFAULT_OPTIONS,
    MIN_SCAN_INTERVAL,
    MIN_STATION_STALE_AFTER,
    MQTT_PORT,
    RETIRED_OPTIONS,
    SECRET_OPTIONS,
    InvalidHost,
    auto_title,
    default_options,
    flatten_sections,
    host_to_base_url,
    merge_options,
    normalize_host,
    normalize_mqtt_psk,
)
from .const import (
    CONF_BASE_URL,
    CONF_ENABLE_MQTT,
    CONF_MQTT_HOST,
    CONF_MQTT_PSK,
    CONF_MQTT_USE_FACTORY_DEFAULTS,
    CONF_OBJECT_ID_HOST,
    DEFAULT_MQTT_PASSWORD,
    DEFAULT_MQTT_PSK_IDENTITY,
    DEFAULT_MQTT_USERNAME,
    DEFAULT_USERNAME,
    DOMAIN,
    FACTORY_MQTT_PSK,
)
from .migration import (
    CURRENT_MINOR_VERSION,
    CURRENT_VERSION,
    ac_unique_id,
    is_legacy_unique_id,
)
from .probe import (
    AcIdentity,
    async_mqtt_handshake,
    async_read_ac_identity,
    async_tcp_probe,
    probe_targets,
)
from .runtime import loaded_coordinator
from .settings import entry_host, entry_value, utc_offset_choices

_LOGGER = logging.getLogger(__name__)

_PASSWORD = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_TEXT = selector.TextSelector()
_BOOL = selector.BooleanSelector()

MQTT_ERRORS = {
    "unreachable": "mqtt_unreachable",
    "timeout": "mqtt_unreachable",
    "connection_refused": "mqtt_unreachable",
    "disconnected": "mqtt_unreachable",
    "tls_handshake": "mqtt_tls_failed",
    "connack_refused": "mqtt_auth_failed",
}


def _login_schema(*, password_required: bool) -> vol.Schema:
    password_key = (
        vol.Required(CONF_PASSWORD)
        if password_required
        else vol.Optional(CONF_PASSWORD)
    )
    return vol.Schema(
        {
            vol.Required(CONF_BASE_URL): _TEXT,
            vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): _TEXT,
            password_key: _PASSWORD,
        }
    )


def identity_error(err: BaseException) -> str:
    """Map an identity/login exception to a config flow error key."""
    if isinstance(err, AccountLockedError):
        return "account_locked"
    if isinstance(err, AuthenticationError):
        return "invalid_auth"
    if isinstance(err, AccountBusyError):
        return "account_busy"
    if isinstance(err, (aiohttp.ClientError, TimeoutError, OSError)):
        return "cannot_connect"
    if isinstance(err, (UnexpectedResponse, SessionExpired)):
        return "not_rltech"
    return "unknown"


def _loaded_client(hass, entry: ConfigEntry | None) -> RltechClient | None:
    """Return the running client of a loaded entry, if any."""
    if entry is None:
        return None
    return getattr(loaded_coordinator(entry), "client", None)


class RltechConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the RLTech FTTR config flow."""

    VERSION = CURRENT_VERSION
    MINOR_VERSION = CURRENT_MINOR_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RltechOptionsFlow:
        """Return the options flow (reads self.config_entry itself)."""
        return RltechOptionsFlow()

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._host: str | None = None
        self._identity = AcIdentity()
        self._ports: dict[tuple[str, int], bool] = {}

    async def _async_identify(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        entry: ConfigEntry | None = None,
    ) -> tuple[AcIdentity | None, str | None]:
        """Log in once and read the AC identity; return (identity, error)."""
        client = RltechClient(base_url, username, password)
        running = _loaded_client(self.hass, entry)
        # Serialize with this entry's own polling burst so the flow never
        # competes with it for the single Web UI login (plan 3.5 step 3).
        guard = running.exclusive() if running is not None else contextlib.nullcontext()
        try:
            async with guard:
                identity = await async_read_ac_identity(
                    async_get_clientsession(self.hass), client
                )
        except Exception as err:  # noqa: BLE001 - mapped to form errors
            error = identity_error(err)
            if error == "unknown":
                _LOGGER.exception("Unexpected error validating RLTech AC")
            return None, error
        return identity, None

    def _mac_of_loaded_entries(self, lan_mac: str | None) -> bool:
        """Return whether a loaded entry already reports this AC LAN MAC.

        Covers entries whose unique_id is still the legacy URL (plan 4.5).
        """
        if not lan_mac:
            return False
        wanted = ac_unique_id(lan_mac, None)
        for entry in self._async_current_entries(include_ignore=False):
            data = getattr(loaded_coordinator(entry), "data", None)
            status = getattr(data, "olt_status", None)
            if status is not None and ac_unique_id(status.lan_mac, None) == wanted:
                return True
        return False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the AC address and Web UI login only."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                host = normalize_host(user_input[CONF_BASE_URL])
            except InvalidHost:
                errors[CONF_BASE_URL] = "invalid_host"
            else:
                base_url = host_to_base_url(host)
                self._async_abort_entries_match({CONF_BASE_URL: base_url})
                identity, error = await self._async_identify(
                    base_url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                if error is not None:
                    errors["base"] = error
                else:
                    assert identity is not None
                    uid = ac_unique_id(identity.lan_mac, identity.serial_number)
                    if uid is not None:
                        await self.async_set_unique_id(uid)
                        self._abort_if_unique_id_configured()
                    if self._mac_of_loaded_entries(identity.lan_mac):
                        return self.async_abort(reason="already_configured")
                    self._host = host
                    self._identity = identity
                    self._data = {
                        CONF_BASE_URL: base_url,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_OBJECT_ID_HOST: host,
                    }
                    # Only decides whether to offer MQTT; never blocks setup.
                    self._ports = await async_tcp_probe(
                        probe_targets(host, identity.lan_ip)
                    )
                    if any(
                        reachable
                        for (_host, port), reachable in self._ports.items()
                        if port == MQTT_PORT
                    ):
                        return await self.async_step_mqtt()
                    return self._async_create(default_options())

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _login_schema(password_required=True), user_input or {}
            ),
            errors=errors,
        )

    def _async_create(self, options: dict[str, Any]) -> ConfigFlowResult:
        assert self._host is not None
        return self.async_create_entry(
            title=auto_title(self._host), data=self._data, options=options
        )

    async def async_step_mqtt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer MQTT live data when port 8883 answered (Q5)."""
        errors: dict[str, str] = {}
        lan_ip = self._identity.lan_ip or self._host or ""
        if user_input is not None:
            options = default_options()
            if not user_input.get(CONF_ENABLE_MQTT):
                options[CONF_ENABLE_MQTT] = False
                return self._async_create(options)
            psk = normalize_mqtt_psk(user_input.get(CONF_MQTT_PSK))
            factory = bool(user_input.get(CONF_MQTT_USE_FACTORY_DEFAULTS))
            explicit = str(user_input.get(CONF_MQTT_HOST) or "").strip()
            try:
                mqtt_host = (
                    normalize_host(explicit, allowed_port=MQTT_PORT) if explicit else ""
                )
            except InvalidHost:
                errors[CONF_MQTT_HOST] = "invalid_host"
            else:
                if not psk and not factory:
                    errors["base"] = "mqtt_credentials_required"
                else:
                    kind = await async_mqtt_handshake(
                        mqtt_host or lan_ip,
                        MQTT_PORT,
                        DEFAULT_MQTT_USERNAME,
                        DEFAULT_MQTT_PASSWORD,
                        DEFAULT_MQTT_PSK_IDENTITY,
                        psk or FACTORY_MQTT_PSK,
                    )
                    if kind is None:
                        options.update(
                            {
                                CONF_ENABLE_MQTT: True,
                                CONF_MQTT_HOST: mqtt_host,
                                CONF_MQTT_PSK: psk,
                                CONF_MQTT_USE_FACTORY_DEFAULTS: factory,
                            }
                        )
                        return self._async_create(options)
                    errors["base"] = MQTT_ERRORS.get(kind, "mqtt_unknown")

        reachable = "; ".join(
            f"{host}:{port} {'reachable' if ok else 'unreachable'}"
            for (host, port), ok in self._ports.items()
            if port == MQTT_PORT
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_ENABLE_MQTT, default=True): _BOOL,
                vol.Optional(CONF_MQTT_HOST): _TEXT,
                vol.Optional(CONF_MQTT_PSK): _PASSWORD,
                vol.Optional(CONF_MQTT_USE_FACTORY_DEFAULTS, default=False): _BOOL,
            }
        )
        return self.async_show_form(
            step_id="mqtt",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    key: value
                    for key, value in (user_input or {}).items()
                    if key != CONF_MQTT_PSK
                },
            ),
            errors=errors,
            description_placeholders={"lan_ip": lan_ip, "reachable": reachable},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the AC rejected the login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the Web UI username and password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            identity, error = await self._async_identify(
                entry.data[CONF_BASE_URL],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                entry=entry,
            )
            if error is not None:
                errors["base"] = error
            else:
                assert identity is not None
                uid = ac_unique_id(identity.lan_mac, identity.serial_number)
                # Only verify when both sides are hardware ids; upgrading a
                # legacy unique_id is the runtime's job (plan 4.5).
                if uid is not None and not is_legacy_unique_id(entry.unique_id):
                    await self.async_set_unique_id(uid)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
                ): _TEXT,
                vol.Required(CONF_PASSWORD): _PASSWORD,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"host": entry_host(entry)},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the AC address or login of an existing entry (plan 3.5)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                host = normalize_host(user_input[CONF_BASE_URL])
            except InvalidHost:
                errors[CONF_BASE_URL] = "invalid_host"
            else:
                base_url = host_to_base_url(host)
                if any(
                    other.entry_id != entry.entry_id
                    and other.data.get(CONF_BASE_URL) == base_url
                    for other in self._async_current_entries(include_ignore=False)
                ):
                    errors[CONF_BASE_URL] = "host_in_use"
                else:
                    result = await self._async_reconfigure_to(
                        entry, host, base_url, user_input, errors
                    )
                    if result is not None:
                        return result

        suggested = {
            CONF_BASE_URL: entry_host(entry),
            CONF_USERNAME: entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
            **{
                key: value
                for key, value in (user_input or {}).items()
                if key != CONF_PASSWORD
            },
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _login_schema(password_required=False), suggested
            ),
            errors=errors,
        )

    async def _async_reconfigure_to(
        self,
        entry: ConfigEntry,
        host: str,
        base_url: str,
        user_input: dict[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
        identity, error = await self._async_identify(
            base_url, user_input[CONF_USERNAME], password, entry=entry
        )
        if error is not None:
            errors["base"] = error
            return None
        assert identity is not None
        uid = ac_unique_id(identity.lan_mac, identity.serial_number)
        new_unique_id: Any = UNDEFINED
        if not is_legacy_unique_id(entry.unique_id):
            if uid is None:
                # Cannot prove it is the same AC: do not move the entry.
                errors["base"] = "cannot_identify"
                return None
            await self.async_set_unique_id(uid)
            self._abort_if_unique_id_mismatch(reason="wrong_device")
        elif uid is not None:
            if any(
                other.entry_id != entry.entry_id and other.unique_id == uid
                for other in self._async_current_entries(include_ignore=False)
            ):
                return self.async_abort(reason="already_configured")
            new_unique_id = uid  # upgrade the legacy URL id on the way
        title: Any = UNDEFINED
        if entry.title == auto_title(entry_host(entry)):
            title = auto_title(host)
        data_updates: dict[str, Any] = {
            CONF_BASE_URL: base_url,
            CONF_USERNAME: user_input[CONF_USERNAME],
        }
        if user_input.get(CONF_PASSWORD):
            data_updates[CONF_PASSWORD] = user_input[CONF_PASSWORD]
        # object_id_host is deliberately kept: entity_ids and the controller
        # device name do not follow the new address (plan 4.4, Q9).
        return self.async_update_reload_and_abort(
            entry,
            unique_id=new_unique_id,
            title=title,
            data_updates=data_updates,
        )


def _number(minimum: float, maximum: float, unit: str | None = None):
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


# Options form layout: key -> section ("" = top level). Kept flat in storage.
OPTION_SECTIONS: dict[str, tuple[str, ...]] = {
    "": (
        "scan_interval",
        "web_busy_grace_polls",
        "stale_device_days",
        "ac_utc_offset",
        "ap_area_id",
    ),
    "clients": (
        "station_retention",
        "station_stale_after",
        "enable_ap_polling",
        "enable_station_polling",
    ),
    "port80": (
        "enable_hardware_status",
        "legacy_username",
        "legacy_password",
        "legacy_hosts",
    ),
    "mqtt": (
        "enable_mqtt",
        "mqtt_host",
        "mqtt_username",
        "mqtt_password",
        "mqtt_psk_identity",
        "mqtt_psk",
        "mqtt_use_factory_defaults",
    ),
}


def _option_selectors() -> dict[str, Any]:
    return {
        "scan_interval": _number(MIN_SCAN_INTERVAL, 3600, "s"),
        "web_busy_grace_polls": _number(0, 20),
        "stale_device_days": _number(0, 365, "d"),
        "ac_utc_offset": selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=utc_offset_choices(),
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key="ac_utc_offset",
            )
        ),
        "ap_area_id": selector.AreaSelector(),
        "station_retention": _number(MIN_STATION_STALE_AFTER, 7 * 86400, "s"),
        "station_stale_after": _number(MIN_STATION_STALE_AFTER, 7 * 86400, "s"),
        "enable_ap_polling": _BOOL,
        "enable_station_polling": _BOOL,
        "enable_hardware_status": _BOOL,
        "legacy_username": _TEXT,
        "legacy_password": _PASSWORD,
        "legacy_hosts": selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)
        ),
        "enable_mqtt": _BOOL,
        "mqtt_host": _TEXT,
        "mqtt_username": _TEXT,
        "mqtt_password": _PASSWORD,
        "mqtt_psk_identity": _TEXT,
        "mqtt_psk": _PASSWORD,
        "mqtt_use_factory_defaults": _BOOL,
    }


def options_schema() -> vol.Schema:
    """Return the options form schema (sections collapsed except basic)."""
    selectors = _option_selectors()
    optional = {"ap_area_id", "mqtt_host", "legacy_hosts", *SECRET_OPTIONS}
    schema: dict[Any, Any] = {}
    for section_name, keys in OPTION_SECTIONS.items():
        fields = {
            (vol.Optional(key) if key in optional else vol.Required(key)): selectors[key]
            for key in keys
        }
        if not section_name:
            schema.update(fields)
        else:
            schema[vol.Required(section_name)] = section(
                vol.Schema(fields), {"collapsed": True}
            )
    return vol.Schema(schema)


def options_suggested_values(entry: ConfigEntry) -> dict[str, Any]:
    """Return current effective values nested like the form (no secrets)."""
    values: dict[str, Any] = {}
    for section_name, keys in OPTION_SECTIONS.items():
        current = {
            key: entry_value(entry, key, DEFAULT_OPTIONS.get(key))
            for key in keys
            if key not in SECRET_OPTIONS
        }
        current = {key: value for key, value in current.items() if value is not None}
        if section_name:
            values[section_name] = current
        else:
            values.update(current)
    return values


class RltechOptionsFlow(OptionsFlowWithReload):
    """Options: polling, busy grace, stale devices, AC time zone, extras."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        current = {
            key: entry_value(entry, key, default)
            for key, default in DEFAULT_OPTIONS.items()
        }
        if (area := entry_value(entry, "ap_area_id")) is not None:
            current["ap_area_id"] = area
        # Retired settings have no field any more; keep stored values as is.
        for key in RETIRED_OPTIONS:
            if key in entry.options:
                current[key] = entry.options[key]
        if user_input is not None:
            flat = flatten_sections(user_input)
            # An optional field left empty is omitted by the frontend: treat
            # it as cleared (for example back to the automatic MQTT host).
            flat.setdefault("mqtt_host", "")
            flat.setdefault("legacy_hosts", "")
            flat.setdefault("ap_area_id", None)
            try:
                merged = merge_options(current, flat)
            except InvalidHost:
                errors["base"] = "invalid_host"
            except (TypeError, ValueError):
                errors["base"] = "invalid_option"
            else:
                error = await self._async_check_mqtt(current, merged)
                if error is None:
                    return self.async_create_entry(data=merged)
                errors["base"] = error

        coordinator = loaded_coordinator(entry)
        data = getattr(coordinator, "data", None)
        status = getattr(data, "olt_status", None)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema(), options_suggested_values(entry)
            ),
            errors=errors,
            description_placeholders={
                "detected_lan_ip": (status.lan_ip if status else None) or "unknown",
                "effective_mqtt_host": getattr(coordinator, "mqtt_host_in_use", None)
                or "-",
            },
        )

    async def _async_check_mqtt(
        self, current: dict[str, Any], merged: dict[str, Any]
    ) -> str | None:
        """Handshake once for an explicit, changed MQTT address (plan 3.6)."""
        if not merged.get("enable_mqtt") or not merged.get("mqtt_host"):
            return None
        keys = (
            "mqtt_host",
            "mqtt_username",
            "mqtt_password",
            "mqtt_psk_identity",
            "mqtt_psk",
            "mqtt_use_factory_defaults",
        )
        if all(current.get(key) == merged.get(key) for key in keys) and current.get(
            "enable_mqtt"
        ):
            return None
        psk = merged.get("mqtt_psk") or (
            FACTORY_MQTT_PSK if merged.get("mqtt_use_factory_defaults") else ""
        )
        if not psk:
            return "mqtt_credentials_required"
        kind = await async_mqtt_handshake(
            merged["mqtt_host"],
            MQTT_PORT,
            merged["mqtt_username"],
            merged["mqtt_password"],
            merged["mqtt_psk_identity"],
            psk,
        )
        if kind is None:
            return None
        return MQTT_ERRORS.get(kind, "mqtt_unknown")
