"""Translation files: strings.json and en.json agree, every error is translated."""

from __future__ import annotations

import json
from pathlib import Path
import re

PACKAGE = Path(__file__).parents[1] / "custom_components" / "rltech_fttr"
STRINGS = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
EN = json.loads((PACKAGE / "translations" / "en.json").read_text(encoding="utf-8"))
ICONS = json.loads((PACKAGE / "icons.json").read_text(encoding="utf-8"))
USER_VISIBLE = (
    "HomeAssistantError",
    "ServiceValidationError",
    "ConfigEntryNotReady",
    "ConfigEntryAuthFailed",
    "UpdateFailed",
)
_RAISE_RE = re.compile(r"raise (%s)\b" % "|".join(USER_VISIBLE))


def test_en_json_is_strings_json() -> None:
    assert EN == STRINGS


def _call_args(text: str, start: int) -> str:
    """Return the text between the parenthesis at ``start`` and its match."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    raise AssertionError("unbalanced call")


def test_every_user_visible_exception_is_translated() -> None:
    raised = 0
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _RAISE_RE.finditer(text):
            raised += 1
            where = f"{path.name}: {text[match.start() : match.start() + 60]!r}"
            assert text[match.end()] == "(", f"bare exception in {where}"
            args = _call_args(text, match.end())
            assert "translation_domain=DOMAIN" in args, where
            assert "translation_key=" in args, where
    assert raised >= 10


def test_every_exception_key_exists() -> None:
    keys: set[str] = set()
    for name in ("button.py", "coordinator.py"):
        text = (PACKAGE / name).read_text(encoding="utf-8")
        keys.update(re.findall(r'translation_key=\s*"(\w+)"', text))
        keys.update(re.findall(r'^\s+else "(\w+)"', text, re.MULTILINE))
    button = (PACKAGE / "button.py").read_text(encoding="utf-8")
    rejected = button[button.index("_REJECTED_KEYS") : button.index("}", button.index("_REJECTED_KEYS"))]
    keys.update(re.findall(r':\s*"(\w+)"', rejected))
    keys.update(re.findall(r'key = "(\w+)"', button))
    # UpdateFailed keys chosen by sources.decide_web_failure.
    keys.update({"web_ui_busy", "web_ui_unreachable", "web_ui_error"})
    keys.discard("ap_reboot")
    keys.discard("ac_reboot")
    missing = keys - set(STRINGS["exceptions"])
    assert not missing, missing


def test_every_entity_translation_has_an_icon_and_no_icon_is_hard_coded() -> None:
    for platform, keys in STRINGS["entity"].items():
        assert set(ICONS["entity"][platform]) == set(keys), platform
        for key, icons in ICONS["entity"][platform].items():
            assert icons["default"].startswith("mdi:"), key
            if platform == "sensor" and "state" in icons:
                # State icons only for real states of the sensor.
                assert set(icons["state"]) <= set(keys[key].get("state", {})) | {
                    "online",
                    "offline",
                }, key
    # Link/online status sensors show their state in the icon (stage 7).
    sensor_icons = ICONS["entity"]["sensor"]
    for key in ("pon_online_status", "lanpon_port_link"):
        assert set(sensor_icons[key]["state"]) == set(
            STRINGS["entity"]["sensor"][key]["state"]
        ), key
    assert set(sensor_icons["ap_online"]["state"]) == {"online", "offline"}
    for name in ("sensor.py", "button.py", "switch.py"):
        text = (PACKAGE / name).read_text(encoding="utf-8")
        assert "icon=" not in text and "_attr_icon" not in text, name


def test_entity_names_use_placeholders_only_for_lanpon() -> None:
    names = {
        key: value["name"] for key, value in STRINGS["entity"]["sensor"].items()
    }
    for key, name in names.items():
        if key.startswith("lanpon_port_"):
            assert name.startswith("LANPON{port} "), key
        else:
            assert "{" not in name, key
    sensor = (PACKAGE / "sensor.py").read_text(encoding="utf-8")
    assert "_attr_name" not in sensor
