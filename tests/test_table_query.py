"""Tests for table_query.py (station AP filters, options) and AP counts."""

from __future__ import annotations

from datetime import UTC, datetime

from test_settings import load_module

models = load_module("models")
table_query = load_module("table_query")
ap_inventory = load_module("ap_inventory")
station_inventory = load_module("station_inventory")

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
AP_A = models.RltechAp(mac="E0:21:FE:A0:A0:00", sn="RLGMFEA0A000", alias=None)
AP_B = models.RltechAp(mac="E0:21:FE:B0:B0:00", sn="RLGMFEB0B000", alias="820")


def station(mac, ap_mac, online=True, alias=None):
    return models.RltechStation(
        mac=mac,
        reported_online=online,
        home=online,
        last_seen=NOW,
        ap_mac=ap_mac,
        ap_alias=alias,
    )


DATA = models.RltechData(
    aps={AP_A.mac: AP_A, AP_B.mac: AP_B},
    stations={
        "02:00:00:00:00:01": station("02:00:00:00:00:01", AP_B.mac, alias="820"),
        "02:00:00:00:00:02": station("02:00:00:00:00:02", AP_B.mac, alias="820"),
        "02:00:00:00:00:03": station("02:00:00:00:00:03", AP_A.mac),
        "02:00:00:00:00:04": station("02:00:00:00:00:04", AP_B.mac, online=False),
        "02:00:00:00:00:05": station("02:00:00:00:00:05", "02:00:00:00:00:AA"),
    },
)


def query(**filters):
    return table_query.station_query(
        station_inventory.station_rows(DATA),
        table_query.ap_filter_options(DATA),
        search="",
        filters=filters,
        sort_key="mac",
        sort_dir=1,
        page=0,
        page_size=0,
    )


def macs(result):
    return [row["mac"] for row in result["rows"]]


def test_ap_mac_filter_is_exact_and_format_tolerant() -> None:
    assert macs(query(ap_mac=AP_B.mac)) == [
        "02:00:00:00:00:01",
        "02:00:00:00:00:02",
        "02:00:00:00:00:04",
    ]
    assert macs(query(ap_mac="e021fea0a000")) == ["02:00:00:00:00:03"]
    assert macs(query(ap_mac=AP_B.mac, online="active")) == [
        "02:00:00:00:00:01",
        "02:00:00:00:00:02",
    ]


def test_old_ap_key_still_works() -> None:
    # Old cards sent the alias (or the MAC when there was no alias).
    assert macs(query(ap="820")) == ["02:00:00:00:00:01", "02:00:00:00:00:02"]
    assert macs(query(ap=AP_A.mac)) == ["02:00:00:00:00:03"]


def test_ap_filter_options_are_value_label_pairs() -> None:
    options = query()["filter_options"]["ap"]
    assert options == [
        {"value": AP_B.mac, "label": "820"},
        {"value": AP_A.mac, "label": "RLGMFEA0A000"},
        # A client on an AP the AC no longer lists still gets an option.
        {"value": "02:00:00:00:00:AA", "label": "02:00:00:00:00:AA"},
    ]
    assert query()["filter_options"]["ssid"] == []


def test_ap_rows_count_reported_clients() -> None:
    rows = {row["mac"]: row for row in ap_inventory.ap_rows(DATA)}
    assert rows[AP_B.mac]["station_count_reported"] == 2
    assert rows[AP_A.mac]["station_count_reported"] == 1
