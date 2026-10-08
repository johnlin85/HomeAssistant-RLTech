"""Tests for RLTech FTTR API parsing and transaction behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from importlib import util
import json
from pathlib import Path
import sys

PKG_ROOT = Path(__file__).parents[1] / "custom_components" / "rltech_fttr"
# SYNTHETIC 8080 pages built from SDK source, see fixtures/generate_synthetic.py.
FIXTURES = Path(__file__).parent / "fixtures"
AC_TZ = timezone(timedelta(hours=8))


def load_module(name: str):
    # Reuse an already imported module: under Home Assistant, tests/ha imports
    # the real package first, and re-executing a module here would create a
    # second set of classes (isinstance checks across tests would break).
    existing = sys.modules.get(f"custom_components.rltech_fttr.{name}")
    if existing is not None:
        return existing
    spec = util.spec_from_file_location(
        f"custom_components.rltech_fttr.{name}", PKG_ROOT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


models = load_module("models")
settings = load_module("settings")
ap_device_registry = load_module("ap_device_registry")
sources = load_module("sources")
api = load_module("api")
identifiers = load_module("identifiers")
ap_inventory = load_module("ap_inventory")
dhcp_enrichment = load_module("dhcp_enrichment")
hostname_enrichment = load_module("hostname_enrichment")
oui_enrichment = load_module("oui_enrichment")
station_inventory = load_module("station_inventory")
station_freshness = load_module("station_freshness")
mqtt = load_module("mqtt")


def payload(rows, total=None, code=0):
    return {
        "respCode": code,
        "total": len(rows) if total is None else total,
        "data": {"list": rows},
    }


def test_station_trackers_are_not_a_default_platform() -> None:
    const_text = (PKG_ROOT / "const.py").read_text(encoding="utf-8")
    assert "Platform.DEVICE_TRACKER" not in const_text


def test_ap_device_registry_updates_sync_integration_owned_metadata() -> None:
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:D0",
        ip="198.18.11.15",
        version="V0.0.49",
        model="RH802GW-AX3",
        sn="RLGM5FB8DCD0",
        alias="House53_Living",
    )
    device = DummyDevice(
        sw_version="V0.0.41",
        configuration_url="http://198.18.11.99",
        via_device_id="old-controller",
        area_id=None,
        name="RLTech AP old alias",
        name_by_user="User name",
        model="Existing model",
        serial_number="Existing serial",
        manufacturer="Existing manufacturer",
        connections={("mac", "02:00:5f:b8:dc:d0")},
    )

    updates = ap_device_registry.ap_device_registry_updates(
        ap,
        device,
        controller_device_id="controller-1",
        area_id="network",
    )

    assert updates == {
        "name": "RLTech AP House53_Living",
        "model": "RH802GW-AX3",
        "sw_version": "V0.0.49",
        "serial_number": "RLGM5FB8DCD0",
        "configuration_url": "http://198.18.11.15",
        "via_device_id": "controller-1",
        "area_id": "network",
    }
    # A user rename is never touched, nor the manufacturer.
    assert "name_by_user" not in updates
    assert "manufacturer" not in updates


def test_ap_device_registry_updates_do_not_clear_missing_values() -> None:
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:D0",
        ip=None,
        version=None,
        sn="RLGM5FB8DCD0",
    )
    device = DummyDevice(
        name="RLTech AP RLGM5FB8DCD0",
        model="RH802GW-AX3",
        sw_version="V0.0.49",
        serial_number="RLGM5FB8DCD0",
        configuration_url="http://198.18.11.15",
        via_device_id="controller-1",
        area_id="network",
        connections={("mac", "02:00:5f:b8:dc:d0")},
    )

    assert ap_device_registry.ap_device_registry_updates(
        ap,
        device,
        controller_device_id="controller-1",
        area_id="network",
    ) == {}


def test_eboo_value_vector() -> None:
    fields = [
        ("pageidx_rows", "1,100"),
        ("filterkey_value", "Mac,"),
        ("search_item", "0"),
        ("search_condition", ""),
        ("auto_refresh", "0"),
        ("txtMaxRows", "100"),
        ("txtCurPageIndex", "1"),
        ("rebootToChangeMode", "Yes"),
    ]
    assert api.eboo_value(fields) == "6cbf7420"


def test_extract_embedded_json_html_unescapes() -> None:
    embedded = html_payload("STA_manage", {"respCode": 0, "data": {"list": [{"HostName": "a&b"}]}})
    result = api.extract_embedded_json(embedded, "STA_manage")
    assert result["data"]["list"][0]["HostName"] == "a&b"


def test_parse_ap_online_detail_embedded_json() -> None:
    detail = api.parse_ap_detail(
        """
        <SCRIPT>
        var list_ap ='{ "respCode":0, "data":{ "Model":"RH802GW-AX3", "IP":"198.18.11.27", "Version":"V0.0.49", "Mac":"02005FB8DCE0", "Status":"1", "Profile":"Default", "ChannelG24":"1", "ChannelG5":"40", "Assoc":"1", "Alias":"House11_Office", "SN":"RLGM5FB8DCE0", "Uplink":"2", "UplinkPort":"5", "BssidG24":"02005FB8DCE4", "BssidG5":"02005FB8DCE5", "SysDuration":"77425", "RamSize":"536870912", "FlashSize":"268435456", "DevName":"FTTRSub_B8DCE0", "CPUUsage":"5", "CPUTemp":"66", "MEMUsage":"30", "FlashUsage":"86", "DevSN":"RLFAKE090300054", "UpgradeFlag":"1", "ProfileIdx":"1" } } '
        let list_pon = '{ "result": 0, "data": "{ \\"pon_id\\": 1, \\"onu_id\\": 2, \\"pon_sn\\": \\"RLGM5FB8DCE0\\", \\"opt_temperature\\": \\" 51.45\\", \\"opt_current\\": \\" 12.23\\", \\"opt_voltage\\": \\"  3.26\\", \\"opt_tx_power\\": \\" -1.79\\", \\"opt_rx_power\\": \\"-15.34\\", \\"active\\": 1, \\"onu_status\\": \\"online\\", \\"ont_distance\\": 443, \\"opt_err_status\\": 0, \\"last_up_time\\": \\"2026-08-19 17:43:11\\", \\"last_down_time\\": \\"2026-08-19 17:42:35\\", \\"last_dying_gasptime\\": \\"2026-08-17 15:26:17\\", \\"last_down_cause\\": \\"FiberBroken\\", \\"dnopt_rx_power\\": \\"-19.86\\", \\"identify_vendor\\": \\"RLGM\\", \\"equipment_id\\": \\"RH802GW-AX3\\", \\"sn_address\\": \\"RLGM-5FB8DCE0\\", \\"hardware_version\\": \\"N/A\\", \\"software_version\\": \\"V0.0.49\\", \\"fireware_version\\": \\"N/A\\" }" } ';
        </SCRIPT>
        """
    )

    assert detail.mac == "02:00:5F:B8:DC:E0"
    assert detail.ip == "198.18.11.27"
    assert detail.online is True
    assert detail.alias == "House11_Office"
    assert detail.sn == "RLGM5FB8DCE0"
    assert detail.hostname == "FTTRSub_B8DCE0"
    assert detail.sys_duration == 77425
    assert detail.ram_size == 536870912
    assert detail.flash_size == 268435456
    assert detail.cpu_usage == 5
    assert detail.cpu_temperature == 66
    assert detail.memory_usage == 30
    assert detail.flash_usage == 86
    assert detail.pon_id == 1
    assert detail.onu_id == 2
    assert detail.pon_sn == "RLGM5FB8DCE0"
    assert detail.onu_status == "online"
    assert detail.ont_distance == 443
    assert detail.optical_temperature == 51.45
    assert detail.optical_current == 12.23
    assert detail.optical_voltage == 3.26
    assert detail.optical_tx_power == -1.79
    assert detail.optical_rx_power == -15.34
    assert detail.downstream_optical_rx_power == -19.86
    assert detail.active is True
    assert detail.last_down_cause == "FiberBroken"
    assert detail.last_up_time == "2026-08-19 17:43:11"
    assert detail.last_down_time == "2026-08-19 17:42:35"
    assert detail.last_dying_gasp_time == "2026-08-17 15:26:17"
    assert detail.hardware_version is None
    assert detail.software_version == "V0.0.49"


def test_respcode_3_normalizes_to_empty() -> None:
    data = api.normalize_snapshot([payload([], code=3)], [payload([], code=3)], olt_html=None)
    assert data.aps == {}
    assert data.stations == {}


def test_normalization_join_and_channel_rules() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    data = api.normalize_snapshot(
        [
            payload(
                [
                    {
                        "Mac": "02005FB8DCD0",
                        "Alias": "Hall AP",
                        "Status": "1",
                        "BssidG24": "02:00:5f:b8:dc:d1",
                        "ChannelG24": "6",
                        "ChannelG5": "36",
                        "Assoc": "2",
                    }
                ]
            )
        ],
        [
            payload(
                [
                    {
                        "Mac": "02007C4C1759",
                        "APMac": "02:00:5F:B8:DC:D0",
                        "Status": "1",
                        "Channel": "0",
                        "Bandwidth": "4",
                        "RSSI": "-61",
                    }
                ]
            )
        ],
        olt_html=None,
        now=now,
    )
    station = data.stations["02:00:7C:4C:17:59"]
    assert station.ap_alias == "Hall AP"
    assert station.band is None
    assert station.bandwidth == "20/40/80 MHz"
    assert station.first_seen == now
    assert station.last_seen == now


def test_station_retention_keeps_then_expires_station() -> None:
    first = api.normalize_snapshot(
        [payload([])],
        [payload([{"Mac": "02007C4C1759", "Status": "1"}])],
        olt_html=None,
        now=datetime(2026, 8, 20, 1, 0, tzinfo=UTC),
    )
    kept = api.normalize_snapshot(
        [payload([])],
        [payload([])],
        olt_html=None,
        previous=first,
        now=datetime(2026, 8, 20, 1, 1, tzinfo=UTC),
        station_retention=180,
    )
    expired = api.normalize_snapshot(
        [payload([])],
        [payload([])],
        olt_html=None,
        previous=first,
        now=datetime(2026, 8, 20, 1, 5, tzinfo=UTC),
        station_retention=180,
    )
    assert kept.stations["02:00:7C:4C:17:59"].reported_online is True
    assert "02:00:7C:4C:17:59" not in expired.stations


def test_station_freshness_marks_stale_then_removes_station() -> None:
    last_seen = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    station = models.RltechStation(
        mac="02:00:7C:4C:17:59",
        reported_online=True,
        home=True,
        last_seen=last_seen,
        ip="198.18.1.10",
        hostname="phone",
        vendor="Example Vendor",
        ssid="main",
        rssi=-55,
    )
    data = models.RltechData(stations={station.mac: station})

    stale = station_freshness.age_station_data(
        data,
        now=last_seen + timedelta(seconds=900),
        stale_after=900,
        retention=3600,
    )
    expired = station_freshness.age_station_data(
        data,
        now=last_seen + timedelta(seconds=3600),
        stale_after=900,
        retention=3600,
    )

    stale_station = stale.stations[station.mac]
    assert stale_station.reported_online is False
    assert stale_station.home is False
    assert stale_station.hostname == "phone"
    assert stale_station.vendor == "Example Vendor"
    assert stale_station.rssi == -55
    assert station.mac not in expired.stations


def test_station_inventory_rows_are_serialized_without_entities() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    data = api.normalize_snapshot(
        [payload([])],
        [
            payload(
                [
                    {
                        "Mac": "02007C4C1759",
                        "IP": "198.18.1.10",
                        "HostName": "phone",
                        "SSID": "main",
                        "APMac": "02005FB8DCD0",
                        "Status": "1",
                        "RSSI": "-55",
                        "Channel": "36",
                        "Vlan": "40",
                        "UpTime": "123",
                    }
                ]
            )
        ],
        olt_html=None,
        now=now,
    )

    rows = station_inventory.station_rows(data)

    assert rows == [
        {
            "mac": "02:00:7C:4C:17:59",
            "ip": "198.18.1.10",
            "hostname": "phone",
            "vendor": None,
            "ssid": "main",
            "ap_mac": "02:00:5F:B8:DC:D0",
            "ap_alias": None,
            "rssi": -55,
            "band": "5 GHz",
            "channel": 36,
            "vlan": 40,
            "uptime": 123,
            "reported_online": True,
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "home": True,
            "rx_rate": None,
            "tx_rate": None,
            "rx_nego_rate": None,
            "tx_nego_rate": None,
            "bandwidth": None,
            "total_count": None,
        }
    ]


def test_dhcp_enrichment_fills_missing_or_junk_station_hostnames() -> None:
    data = api.normalize_snapshot(
        [payload([])],
        [
            payload(
                [
                    {
                        "Mac": "02007C4C1759",
                        "IP": "198.18.1.10",
                        "HostName": "N/A",
                        "Status": "1",
                    },
                    {
                        "Mac": "02007C4C1760",
                        "IP": "198.18.1.11",
                        "HostName": "",
                        "Status": "1",
                    },
                ]
            )
        ],
        olt_html=None,
    )

    enriched = dhcp_enrichment.enrich_station_hostnames(
        data,
        {"02007c4c1759": "phone-from-mac"},
        {"198.18.1.11": "tablet-from-ip"},
    )

    assert enriched.stations["02:00:7C:4C:17:59"].hostname == "phone-from-mac"
    assert enriched.stations["02:00:7C:4C:17:60"].hostname == "tablet-from-ip"


def test_dhcp_enrichment_does_not_replace_useful_fttr_hostname() -> None:
    data = api.normalize_snapshot(
        [payload([])],
        [
            payload(
                [
                    {
                        "Mac": "02007C4C1759",
                        "IP": "198.18.1.10",
                        "HostName": "fttr-phone",
                        "Status": "1",
                    }
                ]
            )
        ],
        olt_html=None,
    )

    enriched = dhcp_enrichment.enrich_station_hostnames(
        data,
        {"02007c4c1759": "dhcp-phone"},
        {"198.18.1.10": "dhcp-phone-ip"},
    )

    assert enriched.stations["02:00:7C:4C:17:59"].hostname == "fttr-phone"


def test_oui_enrichment_fills_missing_station_vendor_only() -> None:
    data = models.RltechData(
        stations={
            "02:00:7C:4C:17:59": models.RltechStation(
                mac="02:00:7C:4C:17:59",
                reported_online=True,
                home=True,
                last_seen=None,
            ),
            "AA:BB:CC:00:00:01": models.RltechStation(
                mac="AA:BB:CC:00:00:01",
                reported_online=True,
                home=True,
                last_seen=None,
                vendor="Existing Vendor",
            ),
        }
    )

    enriched = oui_enrichment.enrich_station_vendors(
        data,
        lambda mac: {
            "02:00:7C:4C:17:59": "Lookup Vendor",
            "AA:BB:CC:00:00:01": "Wrong Vendor",
        }.get(mac),
    )

    assert enriched.stations["02:00:7C:4C:17:59"].vendor == "Lookup Vendor"
    assert enriched.stations["AA:BB:CC:00:00:01"].vendor == "Existing Vendor"


def test_mqtt_station_update_merges_into_station_inventory() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    data = models.RltechData(
        aps={
            "02:00:5F:B8:DC:D0": models.RltechAp(
                mac="02:00:5F:B8:DC:D0",
                alias="House53_Living",
                sn="RLGM5FB8DCD0",
            )
        }
    )
    cmd, update = mqtt.parse_mqtt_payload(
        json.dumps(
            {
                "Cmd": "XReport_StaList",
                "Data": {
                    "List": [
                        {
                            "Mac": "02007C4C1759",
                            "APMac": "02005FB8DCD0",
                            "IP": "198.18.43.223",
                            "SSID": "TEST_SSID",
                            "RSSI": "-65",
                            "Bandwidth": "4",
                            "Channel": "40",
                            "Vlan": "40",
                            "RxRate": "1",
                            "TxRate": "2",
                            "RxNegoRate": "585",
                            "TxNegoRate": "864",
                            "UpTime": "5457",
                            "Status": "1",
                        }
                    ]
                },
            }
        )
    )

    assert cmd == "XReport_StaList"
    merged = mqtt.merge_station_updates(data, update, now=now)
    station = merged.stations["02:00:7C:4C:17:59"]
    assert station.ap_alias == "House53_Living"
    assert station.band == "5 GHz"
    assert station.bandwidth == "20/40/80 MHz"
    assert station.last_seen == now
    assert station.reported_online is True


def test_mqtt_station_update_preserves_existing_hostname() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    previous = models.RltechStation(
        mac="02:00:7C:4C:17:59",
        reported_online=True,
        home=True,
        last_seen=now,
        hostname="dhcp-phone",
    )
    data = models.RltechData(stations={previous.mac: previous})
    update = [
        mqtt.MqttStationUpdate(
            mac=previous.mac,
            ip="198.18.43.223",
            hostname=None,
            reported_online=True,
        )
    ]

    merged = mqtt.merge_station_updates(data, update, now=now + timedelta(seconds=5))

    assert merged.stations[previous.mac].hostname == "dhcp-phone"


def test_station_first_seen_is_preserved_across_http_and_mqtt_updates() -> None:
    first_seen = datetime(2026, 8, 21, 11, 0, tzinfo=UTC)
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    previous_station = models.RltechStation(
        mac="02:00:7C:4C:17:59",
        reported_online=True,
        home=True,
        first_seen=first_seen,
        last_seen=first_seen,
        hostname="phone",
    )
    previous = models.RltechData(stations={previous_station.mac: previous_station})

    http_data = api.normalize_snapshot(
        [payload([])],
        [payload([{"Mac": "02007C4C1759", "Status": "1"}])],
        olt_html=None,
        previous=previous,
        now=now,
    )
    mqtt_data = mqtt.merge_station_updates(
        http_data,
        [mqtt.MqttStationUpdate(mac=previous_station.mac, reported_online=True)],
        now=now + timedelta(seconds=5),
    )

    assert http_data.stations[previous_station.mac].first_seen == first_seen
    assert mqtt_data.stations[previous_station.mac].first_seen == first_seen
    assert mqtt_data.stations[previous_station.mac].last_seen == now + timedelta(
        seconds=5
    )


def test_mqtt_station_update_preserves_unreported_fields() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    previous = models.RltechStation(
        mac="02:00:7C:4C:17:59",
        reported_online=False,
        home=False,
        last_seen=now - timedelta(minutes=10),
        ip="198.18.43.223",
        hostname="dhcp-phone",
        vendor="Example Vendor",
        ssid="TEST_SSID",
        rssi=-65,
        rx_rate=1,
        tx_rate=2,
        rx_nego_rate=585,
        tx_nego_rate=864,
        uptime=600,
        channel=40,
        band="5 GHz",
        bandwidth="20/40/80 MHz",
        vlan=40,
    )
    data = models.RltechData(stations={previous.mac: previous})
    update = [mqtt.MqttStationUpdate(mac=previous.mac, reported_online=True)]

    merged = mqtt.merge_station_updates(data, update, now=now)

    station = merged.stations[previous.mac]
    assert station.reported_online is True
    assert station.home is True
    assert station.last_seen == now
    assert station.hostname == "dhcp-phone"
    assert station.vendor == "Example Vendor"
    assert station.rssi == -65
    assert station.tx_nego_rate == 864
    assert station.bandwidth == "20/40/80 MHz"


def test_mqtt_stats_exposes_last_station_message() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    stats = mqtt.RltechMqttStats(last_message=now, last_station_message=now)

    result = stats.as_dict()

    assert result["last_message"] == now.isoformat()
    assert result["last_station_message"] == now.isoformat()


def test_mqtt_ap_health_updates_known_ap_only_and_stabilizes_boot_time() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:D0",
        sn="RLGM5FB8DCD0",
        assoc_count=1,
    )
    previous_boot = now - timedelta(seconds=100)
    data = models.RltechData(
        aps={ap.mac: ap},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac,
                last_boot=previous_boot,
                cpu_usage=5,
            )
        },
    )
    cmd, update = mqtt.parse_mqtt_payload(
        json.dumps(
            {
                "Cmd": "XReport_ExtendInfo",
                "Send": "02005FB8DCD0",
                "Data": {
                    "PONSN": "RLGM5FB8DCD0",
                    "Assoc": "12",
                    "CPUUsage": "8",
                    "CPUTemp": "66",
                    "MEMUsage": "30",
                    "FlashUsage": "86",
                    "SysDuration": "101",
                },
            }
        )
    )

    assert cmd == "XReport_ExtendInfo"
    merged = mqtt.merge_ap_health_update(data, update, now=now)
    detail = merged.ap_details[ap.mac]
    assert merged.aps[ap.mac].assoc_count == 12
    assert detail.cpu_usage == 8
    assert detail.cpu_temperature == 66
    assert detail.memory_usage == 30
    assert detail.flash_usage == 86
    assert detail.last_boot == previous_boot

    unknown = mqtt.MqttApHealthUpdate(mac="02:00:5F:B8:FF:FF", assoc_count=99)
    assert mqtt.merge_ap_health_update(data, unknown, now=now) is data


def test_mqtt_ap_lifecycle_updates_known_ap_only() -> None:
    mac = "02:00:5F:B8:DC:D0"
    data = models.RltechData(
        aps={
            mac: models.RltechAp(
                mac=mac,
                sn="RLGM5FB8DCD0",
                online=True,
            )
        }
    )
    cmd, update = mqtt.parse_mqtt_payload(
        json.dumps({"Mac": "02005FB8DCD0"}), mqtt.TOPIC_AP_OFFLINE
    )

    assert cmd == "APOffline"
    assert isinstance(update, mqtt.MqttApStatusUpdate)
    assert update.mac == mac
    assert update.online is False

    merged = mqtt.merge_ap_status_update(data, update)
    assert merged.aps[mac].online is False
    assert mqtt.merge_ap_status_update(merged, update) is merged

    unknown = mqtt.MqttApStatusUpdate(
        online=True, mac="02:00:5F:B8:FF:FF", sn=None
    )
    assert mqtt.merge_ap_status_update(data, unknown) is data


def test_mqtt_ap_lifecycle_can_match_by_sn() -> None:
    mac = "02:00:5F:B8:DC:D0"
    data = models.RltechData(
        aps={
            mac: models.RltechAp(
                mac=mac,
                sn="RLGM5FB8DCD0",
                online=False,
            )
        }
    )
    cmd, update = mqtt.parse_mqtt_payload(
        json.dumps({"Data": {"PONSN": "RLGM-5FB8DCD0"}}), mqtt.TOPIC_AP_ONLINE
    )

    assert cmd == "APOnline"
    assert isinstance(update, mqtt.MqttApStatusUpdate)
    assert update.sn == "RLGM5FB8DCD0"
    assert update.online is True

    merged = mqtt.merge_ap_status_update(data, update)
    assert merged.aps[mac].online is True


def test_mqtt_live_overlay_preserves_newer_station_update() -> None:
    fresh_time = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    current_time = fresh_time + timedelta(seconds=10)
    mac = "02:00:7C:4C:17:59"
    fresh = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=fresh_time,
                ip="198.18.43.10",
                hostname=None,
                rssi=-80,
            )
        }
    )
    current = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=current_time,
                ip="198.18.43.10",
                hostname="dhcp-phone",
                rssi=-55,
            )
        }
    )

    merged = mqtt.preserve_live_overlay(current, fresh)

    assert merged.stations[mac].last_seen == current_time
    assert merged.stations[mac].hostname == "dhcp-phone"
    assert merged.stations[mac].rssi == -55


def test_mqtt_live_overlay_keeps_http_station_when_newer() -> None:
    fresh_time = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    current_time = fresh_time - timedelta(seconds=10)
    mac = "02:00:7C:4C:17:59"
    fresh = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=fresh_time,
                hostname=None,
                rssi=-55,
            )
        }
    )
    current = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=current_time,
                hostname="dhcp-phone",
                rssi=-80,
            )
        }
    )

    merged = mqtt.preserve_live_overlay(current, fresh)

    assert merged.stations[mac].last_seen == fresh_time
    assert merged.stations[mac].hostname == "dhcp-phone"
    assert merged.stations[mac].rssi == -55


def test_mqtt_live_overlay_does_not_resurrect_unchanged_expired_station() -> None:
    seen_time = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    mac = "02:00:7C:4C:17:59"
    previous = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=seen_time,
                hostname="stale-client",
            )
        }
    )
    current = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=seen_time,
                hostname="stale-client",
            )
        }
    )
    fresh = models.RltechData(stations={})

    merged = mqtt.preserve_live_overlay(current, fresh, previous)

    assert mac not in merged.stations


def test_mqtt_live_overlay_keeps_station_changed_during_poll() -> None:
    seen_time = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    mqtt_time = seen_time + timedelta(seconds=30)
    mac = "02:00:7C:4C:17:59"
    previous = models.RltechData(stations={})
    current = models.RltechData(
        stations={
            mac: models.RltechStation(
                mac=mac,
                reported_online=True,
                home=True,
                last_seen=mqtt_time,
                hostname="mqtt-client",
            )
        }
    )
    fresh = models.RltechData(stations={})

    merged = mqtt.preserve_live_overlay(current, fresh, previous)

    assert merged.stations[mac].last_seen == mqtt_time


def test_mqtt_live_overlay_preserves_ap_health_without_overwriting_optics() -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    mac = "02:00:5F:B8:DC:D0"
    ap = models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=1)
    fresh = models.RltechData(
        aps={mac: ap},
        ap_details={
            mac: models.RltechApDetail(
                mac=mac,
                optical_tx_power=-1.79,
                optical_rx_power=-15.46,
                last_update=now,
            )
        },
    )
    current = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=12)},
        ap_details={
            mac: models.RltechApDetail(
                mac=mac,
                cpu_usage=8,
                cpu_temperature=66,
                memory_usage=30,
                flash_usage=86,
                sys_duration=3600,
                last_boot=now - timedelta(hours=1),
                last_update=now - timedelta(seconds=5),
            )
        },
    )

    merged = mqtt.preserve_live_overlay(current, fresh)
    detail = merged.ap_details[mac]

    assert merged.aps[mac].assoc_count == 12
    assert detail.optical_tx_power == -1.79
    assert detail.optical_rx_power == -15.46
    assert detail.cpu_usage == 8
    assert detail.cpu_temperature == 66
    assert detail.memory_usage == 30
    assert detail.flash_usage == 86
    assert detail.sys_duration == 3600


def test_mqtt_live_overlay_only_preserves_ap_assoc_changed_during_poll() -> None:
    mac = "02:00:5F:B8:DC:D0"
    previous = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=12)}
    )
    current_unchanged = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=12)}
    )
    current_changed = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=13)}
    )
    fresh = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", assoc_count=9)}
    )

    stale_merged = mqtt.preserve_live_overlay(current_unchanged, fresh, previous)
    changed_merged = mqtt.preserve_live_overlay(current_changed, fresh, previous)

    assert stale_merged.aps[mac].assoc_count == 9
    assert changed_merged.aps[mac].assoc_count == 13


def test_mqtt_live_overlay_preserves_ap_online_changed_during_poll() -> None:
    mac = "02:00:5F:B8:DC:D0"
    previous = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", online=True)}
    )
    current_unchanged = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", online=True)}
    )
    current_changed = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", online=False)}
    )
    fresh = models.RltechData(
        aps={mac: models.RltechAp(mac=mac, sn="RLGM5FB8DCD0", online=True)}
    )

    stale_merged = mqtt.preserve_live_overlay(current_unchanged, fresh, previous)
    changed_merged = mqtt.preserve_live_overlay(current_changed, fresh, previous)

    assert stale_merged.aps[mac].online is True
    assert changed_merged.aps[mac].online is False


def test_mqtt_packet_reader_handles_publish_ping_and_disconnect() -> None:
    async def run() -> None:
        client = mqtt.AsyncPskMqttClient(
            "olt",
            8883,
            "admin",
            "123456",
            psk_identity="admin",
            psk_hex="61646D696E21402324",
            client_id="test",
        )
        reader = asyncio.StreamReader()
        reader.feed_data(
            mqtt._packet(0x30, mqtt._pack_string("topic") + b'{"ok":true}')
            + mqtt._packet(0xD0, b"")
            + mqtt._packet(0xE0, b"")
        )
        client.reader = reader

        assert await client.read_message() == ("topic", '{"ok":true}')
        assert await client.read_message() is None
        try:
            await client.read_message()
        except mqtt.RltechMqttError:
            return
        raise AssertionError("expected broker disconnected error")

    asyncio.run(run())


def test_mqtt_subscribe_accepts_suback() -> None:
    class FakeWriter:
        def __init__(self) -> None:
            self.written = b""

        def write(self, data: bytes) -> None:
            self.written += data

        async def drain(self) -> None:
            return None

    async def run() -> None:
        client = mqtt.AsyncPskMqttClient(
            "olt",
            8883,
            "admin",
            "123456",
            psk_identity="admin",
            psk_hex="61646D696E21402324",
            client_id="test",
        )
        reader = asyncio.StreamReader()
        reader.feed_data(mqtt._packet(0x90, b"\x00\x01\x00"))
        client.reader = reader
        writer = FakeWriter()
        client.writer = writer

        await client.subscribe(["topic"])

        assert writer.written.startswith(b"\x82")

    asyncio.run(run())


def test_mqtt_connect_packet_uses_longer_keepalive(monkeypatch) -> None:
    class FakeWriter:
        def __init__(self) -> None:
            self.written = b""

        def write(self, data: bytes) -> None:
            self.written += data

        async def drain(self) -> None:
            return None

        def get_extra_info(self, _name):
            return None

    async def fake_open_connection(*_args, **_kwargs):
        reader = asyncio.StreamReader()
        reader.feed_data(mqtt._packet(0x20, b"\x00\x00"))
        return reader, writer

    async def run() -> None:
        monkeypatch.setattr(mqtt.asyncio, "open_connection", fake_open_connection)
        monkeypatch.setattr(mqtt, "build_psk_context", lambda *_args: None)
        client = mqtt.AsyncPskMqttClient(
            "olt",
            8883,
            "admin",
            "123456",
            psk_identity="admin",
            psk_hex="61646D696E21402324",
            client_id="test",
        )

        await client.connect()

        assert writer.written.startswith(b"\x10")
        assert b"\x00<" in writer.written

    writer = FakeWriter()
    asyncio.run(run())


def test_dhcp_match_summary_counts_fillable_missing_hostnames() -> None:
    data = api.normalize_snapshot(
        [payload([])],
        [
            payload(
                [
                    {
                        "Mac": "02000000C0C9",
                        "IP": "198.18.43.207",
                        "HostName": "",
                        "Status": "1",
                    },
                    {
                        "Mac": "02007C4C1760",
                        "IP": "198.18.1.11",
                        "HostName": "",
                        "Status": "1",
                    },
                ]
            )
        ],
        olt_html=None,
    )

    summary = hostname_enrichment.dhcp_match_summary(
        object(),
        data.stations.values(),
        lookup_fn=lambda _hass: (
            {"02000000c0c9": "client-tablet-01"},
            {"198.18.43.207": "client-tablet-01"},
        ),
    )

    assert summary["dhcp_mac_count"] == 1
    assert summary["dhcp_ip_count"] == 1
    assert summary["station_mac_match_count"] == 1
    assert summary["station_ip_match_count"] == 1
    assert summary["station_missing_hostname_fillable_count"] == 1


def test_ap_inventory_rows_are_serialized_for_table() -> None:
    data = api.normalize_snapshot(
        [
            payload(
                [
                    {
                        "Mac": "02005FB8DCD0",
                        "Alias": "Hall AP",
                        "IP": "198.18.1.20",
                        "Model": "RH802GW-AX3",
                        "Version": "V0.0.49",
                        "Status": "1",
                        "Profile": "Default",
                        "ProfileIdx": "1",
                        "BssidG24": "02005FB8DCD4",
                        "BssidG5": "02005FB8DCD5",
                        "ChannelG24": "6",
                        "ChannelG5": "40",
                        "Assoc": "3",
                        "Uplink": "2",
                        "UplinkPort": "6",
                        "SN": "RLGM5FB8DCD0",
                        "DevSN": "RLFAKE090300001",
                        "UpgradeFlag": "1",
                        "OptTxPower": "2.15",
                        "OptRxPower": "-18.42",
                    }
                ]
            )
        ],
        [payload([])],
        olt_html=None,
    )

    rows = ap_inventory.ap_rows(data)

    assert rows == [
        {
            "device_id": None,
            "hardware_id": "RLGM5FB8DCD0",
            "mac": "02:00:5F:B8:DC:D0",
            "alias": "Hall AP",
            "ip": "198.18.1.20",
            "online": True,
            "model": "RH802GW-AX3",
            "version": "V0.0.49",
            "profile": "Default",
            "profile_idx": "1",
            "channel_24": 6,
            "channel_5": 40,
            "bssid_24": "02:00:5F:B8:DC:D4",
            "bssid_5": "02:00:5F:B8:DC:D5",
            "assoc_count": 3,
            "uplink": 2,
            "uplink_port": 6,
            "sn": "RLGM5FB8DCD0",
            "dev_sn": "RLFAKE090300001",
            "upgrade_flag": "1",
            "entities": {},
            "disabled_entities": [],
            "optical_rx_power": -18.42,
            "optical_tx_power": 2.15,
            "station_count_reported": 0,
        }
    ]


def test_ap_sensor_unique_id_prefers_serial() -> None:
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:D0",
        sn="RLGM5FB8DCD0",
        dev_sn="RLFAKE090300001",
    )

    assert (
        identifiers.ap_sensor_unique_id("entry", ap, "assoc_count")
        == "entry_ap_RLGM5FB8DCD0_assoc_count"
    )
    assert identifiers.AP_SENSOR_KEYS[:4] == ("online", "assoc_count", "profile", "alias")
    assert "optical_rx_power" in identifiers.AP_SENSOR_KEYS
    assert "source_host" in identifiers.AP_SENSOR_KEYS


def test_stable_sensor_object_ids_use_hardware_identity() -> None:
    ap = models.RltechAp(
        mac="02:00:5F:B8:E0:70",
        sn="RLGM5FB8E070",
        alias="House66",
    )

    assert (
        identifiers.ap_sensor_object_id(ap, "profile")
        == "rltech_ap_rlgm5fb8e070_profile"
    )
    assert (
        identifiers.controller_sensor_object_id("198.18.11.1", "last_boot")
        == "rltech_olt_198_18_11_1_last_boot"
    )
    assert (
        identifiers.lan_port_sensor_object_id(
            "198.18.11.1",
            "LANPON2",
            "rx_power",
        )
        == "rltech_olt_198_18_11_1_lanpon2_rx_power"
    )


def test_retired_sensor_unique_ids_match_exactly() -> None:
    entry = "01JENTRY"
    retired = [
        f"{entry}_lan_port_1_status",
        f"{entry}_lan_port_4_rate",
        f"{entry}_lan_port_5_status",  # LANPON1 link
        f"{entry}_lan_port_6_mode",
        f"{entry}_lanpon_port_1_status",  # entity_id ..._lanpon1_status_2
        f"{entry}_legacy_olt_198.18.11.2_lan_port_2_status",
        f"{entry}_legacy_olt_198.18.11.2_lanpon_port_2_status",
        f"{entry}_uplink_pon_optics",  # dual-group decision D
        f"{entry}_uplink_pon_link_state",  # stage 6b
        f"{entry}_uplink_pon_registration_state",  # stage 6b
    ]
    kept = [
        f"{entry}_lanpon_port_1_tx_power",
        f"{entry}_lanpon_port_2_rx_power",
        f"{entry}_lanpon_port_2_temperature",
        f"{entry}_lanpon_port_2_voltage",
        f"{entry}_lanpon_port_2_current",
        f"{entry}_legacy_olt_198.18.11.2_lanpon_port_1_tx_power",
        f"{entry}_legacy_olt_198.18.11.2_last_boot",
        f"{entry}_olt_cpu_temperature",
        f"{entry}_ap_count",
        f"{entry}_ap_RLGMFEB0B000_online",
        f"{entry}_lan_port_1_status_extra",
        f"{entry}_uplink_pon_tx_power",
        f"{entry}_uplink_pon_rx_power",
        f"{entry}_uplink_pon_link_type",
        f"{entry}_uplink_pon_online_status",
        f"{entry}_uplink_pon_link_state_2",
        "OTHER_uplink_pon_link_state",
        f"{entry}_uplink_pon_optics_2",
        "OTHER_uplink_pon_optics",
        f"{entry}_lan_port_x_status",
        "OTHER_lan_port_1_status",
        f"{entry}lan_port_1_status",
        None,
    ]
    assert all(identifiers.is_retired_sensor_unique_id(entry, uid) for uid in retired)
    assert not any(identifiers.is_retired_sensor_unique_id(entry, uid) for uid in kept)


def test_ap_hardware_id_does_not_use_dev_sn() -> None:
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:D0",
        dev_sn="RLFAKE090300001",
    )

    assert identifiers.ap_hardware_id(ap) is None
    assert identifiers.ap_sensor_unique_id("entry", ap, "assoc_count") is None


def test_normalize_snapshot_preserves_ap_details() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    detail = models.RltechApDetail(
        mac="02:00:5F:B8:DC:E0",
        optical_rx_power=-15.34,
        last_update=now,
    )

    data = api.normalize_snapshot(
        [
            payload(
                [
                    {
                        "Mac": "02005FB8DCE0",
                        "SN": "RLGM5FB8DCE0",
                    }
                ]
            )
        ],
        [payload([])],
        olt_html=None,
        now=now,
        ap_details={"02:00:5F:B8:DC:E0": detail},
    )

    assert data.ap_details["02:00:5F:B8:DC:E0"].optical_rx_power == -15.34


def test_parse_legacy_port80_pages() -> None:
    now = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)
    status = api.parse_legacy_runinfo(
        """
        function X(ProName,VerInfo,SnmpOid,BuatRate,ManuInfo,SupRFC,CopyInfo,MACAddr,RunTime,CPURatio,MemRatio,Sn)
        X('RH8002GR','V5.0.1-51675','','','','','','02:00:5F:B9:D3:F0','19 Days 18 Hour 44 Min 43 Sec','8','20','RLFAKE092600019');
        """,
        now=now,
    )
    lan_ports = api.parse_legacy_lan_ports(
        """
        <script>showPortInfo('LAN-1','1','Full','1000M','10228557175','9786317129');showPortInfo('LANPON1','1','Full','2500M','18446744072608318560','420643402');</script>
        """
    )
    lanpon_ports = api.parse_legacy_lanpon_ports(
        """
        <script>showLANPonInfo('LANPON1','disable','enable','enable','up','2.73 dBm','-20.48','49.83 ℃','3.08 mA','38.56 V');</script>
        """
    )
    onu_rows = api.parse_legacy_onu_mgmt(
        """
        R( "0/1/2","RLGM-5FB8DCE0","Fri Aug 21 06:12:26 2026"," -1.79/-15.46","online","17","","0");
        """,
        source_host="198.18.11.1",
    )

    assert status.gateway_type == "RH8002GR"
    assert status.cpu_usage == 8
    assert status.memory_usage == 20
    assert status.serial_number == "RLFAKE092600019"
    assert status.last_boot == datetime(2026, 8, 1, 15, 15, 17, tzinfo=UTC)
    assert lan_ports[1].status == "connected"
    assert lan_ports[5].label == "LANPON1"
    assert lan_ports[5].rate == "2500M"
    assert lanpon_ports[1].status == "up"
    assert lanpon_ports[1].tx_power == 2.73
    assert lanpon_ports[1].rx_power == -20.48
    assert lanpon_ports[1].temperature == 49.83
    assert lanpon_ports[1].voltage == 3.08
    assert lanpon_ports[1].current == 38.56
    assert onu_rows["RLGM5FB8DCE0"].optical_tx_power == -1.79
    assert onu_rows["RLGM5FB8DCE0"].optical_rx_power == -15.46
    assert onu_rows["RLGM5FB8DCE0"].reg_off_time == datetime(
        2026, 8, 21, 6, 12, 26, tzinfo=UTC
    )
    assert onu_rows["RLGM5FB8DCE0"].last_down_cause == "0"
    assert onu_rows["RLGM5FB8DCE0"].source_host == "198.18.11.1"


def test_legacy_onu_details_join_to_aps_by_serial() -> None:
    now = datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC)
    last_boot = datetime(2026, 8, 21, 8, 0, 0, tzinfo=UTC)
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:E0",
        sn="RLGM5FB8DCE0",
        alias="House11_Office",
        online=True,
    )
    details = {
        "RLGM5FB8DCE0": models.RltechApDetail(
            sn="RLGM5FB8DCE0",
            optical_tx_power=-1.79,
            optical_rx_power=-15.46,
            reg_off_time=datetime(2026, 8, 21, 6, 12, 26, tzinfo=UTC),
        )
    }
    previous = models.RltechData(
        aps={ap.mac: ap},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac,
                sys_duration=7200,
                last_boot=last_boot,
                cpu_usage=8,
                cpu_temperature=66,
                memory_usage=30,
                flash_usage=86,
            )
        },
    )

    joined = api._join_legacy_ap_details({ap.mac: ap}, details, previous, now=now)

    assert joined[ap.mac].sn == "RLGM5FB8DCE0"
    assert joined[ap.mac].mac == ap.mac
    assert joined[ap.mac].alias == "House11_Office"
    assert joined[ap.mac].optical_rx_power == -15.46
    assert joined[ap.mac].last_update == now
    assert joined[ap.mac].sys_duration == 7200
    assert joined[ap.mac].last_boot == last_boot
    assert joined[ap.mac].cpu_usage == 8
    assert joined[ap.mac].cpu_temperature == 66
    assert joined[ap.mac].memory_usage == 30
    assert joined[ap.mac].flash_usage == 86


def test_normalize_snapshot_preserves_ap_details_when_no_details_polled() -> None:
    previous = models.RltechData(
        aps={
            "02:00:5F:B8:DC:E0": models.RltechAp(
                mac="02:00:5F:B8:DC:E0",
                sn="RLGM5FB8DCE0",
            )
        },
        ap_details={
            "02:00:5F:B8:DC:E0": models.RltechApDetail(
                mac="02:00:5F:B8:DC:E0",
                cpu_usage=8,
            )
        },
    )

    data = api.normalize_snapshot(
        [
            payload(
                [
                    {
                        "Mac": "02005FB8DCE0",
                        "SN": "RLGM5FB8DCE0",
                    }
                ]
            )
        ],
        [],
        olt_html=None,
        previous=previous,
        ap_details=None,
    )

    assert data.ap_details["02:00:5F:B8:DC:E0"].cpu_usage == 8


def test_legacy_sources_are_preserved_per_olt() -> None:
    data = models.RltechData(
        legacy_sources={
            "198.18.11.1": models.RltechLegacyOltSource(
                host="198.18.11.1",
                base_url="http://198.18.11.1",
                olt_status=models.RltechOltStatus(cpu_usage=8),
                lan_ports={1: models.RltechLanPort(port=1, status="connected")},
                lanpon_ports={1: models.RltechLanPonPort(ponid=1, status="up")},
            ),
            "198.18.11.2": models.RltechLegacyOltSource(
                host="198.18.11.2",
                base_url="http://198.18.11.2",
                olt_status=models.RltechOltStatus(cpu_usage=6),
                lan_ports={1: models.RltechLanPort(port=1, status="connected")},
                lanpon_ports={2: models.RltechLanPonPort(ponid=2, status="up")},
            ),
        }
    )

    assert data.legacy_sources["198.18.11.1"].olt_status.cpu_usage == 8
    assert data.legacy_sources["198.18.11.2"].olt_status.cpu_usage == 6
    assert data.legacy_sources["198.18.11.2"].lanpon_ports[2].status == "up"


def test_legacy_source_boot_time_is_stabilized_per_host() -> None:
    async def run() -> None:
        now = datetime.now(UTC)
        previous_boot = now - timedelta(days=19, hours=18, minutes=44, seconds=42)
        previous = models.RltechData(
            legacy_sources={
                "slave": models.RltechLegacyOltSource(
                    host="slave",
                    base_url="http://slave",
                    olt_status=models.RltechOltStatus(last_boot=previous_boot),
                )
            }
        )
        client = api.RltechClient(
            "http://olt:8080",
            "u",
            "p",
            legacy_base_urls=["http://slave"],
        )

        async def fetch_source(*_args, **_kwargs):
            return (
                models.RltechOltStatus(last_boot=previous_boot + timedelta(seconds=1)),
                {},
                {},
                {},
            )

        client._fetch_legacy_source = fetch_source
        _, _, _, _, sources = await client._fetch_legacy_snapshot(
            object(), {}, previous, now=now
        )
        assert sources["slave"].olt_status.last_boot == previous_boot

        reboot_time = previous_boot + timedelta(minutes=10)

        async def fetch_rebooted_source(*_args, **_kwargs):
            return (
                models.RltechOltStatus(last_boot=reboot_time),
                {},
                {},
                {},
            )

        client._fetch_legacy_source = fetch_rebooted_source
        _, _, _, _, rebooted_sources = await client._fetch_legacy_snapshot(
            object(), {}, previous, now=now
        )
        assert rebooted_sources["slave"].olt_status.last_boot == reboot_time

    asyncio.run(run())


def test_ap_detail_due_respects_interval() -> None:
    client = api.RltechClient("http://example.invalid", "u", "p")
    now = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    ap = models.RltechAp(
        mac="02:00:5F:B8:DC:E0",
        sn="RLGM5FB8DCE0",
    )
    fresh = models.RltechData(
        aps={ap.mac: ap},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac,
                last_update=now,
                web_detail_update=now - timedelta(seconds=599),
            )
        },
    )
    stale = models.RltechData(
        aps={ap.mac: ap},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac,
                last_update=now,
                web_detail_update=now - timedelta(seconds=600),
            )
        },
    )
    legacy_only = models.RltechData(
        aps={ap.mac: ap},
        ap_details={ap.mac: models.RltechApDetail(mac=ap.mac, last_update=now)},
    )

    assert client._ap_detail_due(
        {ap.mac: ap},
        fresh,
        now=now,
        scan_interval=60,
        detail_interval=600,
    ) == []
    assert client._ap_detail_due(
        {ap.mac: ap},
        stale,
        now=now,
        scan_interval=60,
        detail_interval=600,
    ) == [ap]
    # A legacy/MQTT refresh of last_update must not starve 8080 details.
    assert client._ap_detail_due(
        {ap.mac: ap},
        legacy_only,
        now=now,
        scan_interval=60,
        detail_interval=600,
    ) == [ap]


def test_ap_detail_due_covers_missing_details_without_starvation() -> None:
    client = api.RltechClient("http://example.invalid", "u", "p")
    start = datetime(2026, 8, 20, 1, 0, tzinfo=UTC)
    aps = {
        f"02:00:5F:B8:E0:{index:02X}": models.RltechAp(
            mac=f"02:00:5F:B8:E0:{index:02X}",
            sn=f"RLGM5FB8E0{index:02X}",
        )
        for index in range(29)
    }
    seen: set[str] = set()

    for offset in range(0, 600, 60):
        due = client._ap_detail_due(
            aps,
            None,
            now=start + timedelta(seconds=offset),
            scan_interval=60,
            detail_interval=600,
        )
        assert len(due) <= 3
        seen.update(ap.sn for ap in due if ap.sn)

    assert seen == {ap.sn for ap in aps.values()}


def test_parse_olt_status_optional_fields() -> None:
    status = api.parse_olt_status(
        """
        var phy_status = 'gpon_phy_up';
        var pon_mode = '1';
        this.LinkSta = '1';
        this.trafficstate = 'up';
        this.fecState = '1';
        this.PonSendPkt = '12';
        this.PonRecvPkt = '34';
        var ponuptime = "2026-08-20 01:00:00";
        var curtime = '2026-08-20 01:05:00';
        CpuUsage = '13';
        MemoryUsage = '44';
        FlashUsage = '55';
        CpuTemp = '48.5';
        CustomerSWVersion = 'V0.0.29';
        """
    )
    assert status.fec_state is True
    assert status.pon_tx_frames == 12
    assert status.pon_rx_frames == 34
    assert status.cpu_usage == 13
    assert status.software_version == "V0.0.29"


def test_parse_olt_status_table_rendered_browser_values() -> None:
    now = datetime(2026, 8, 20, 15, 0, 0, tzinfo=UTC)
    status = api.parse_olt_status(
        """
        var phy_status = 'down';
        var pon_mode = '3';
        this.LinkSta = 'N/A';
        this.trafficstate = 'down';
        this.fecState = 'N/A';
        this.PonSendPkt = '1063260816';
        this.PonRecvPkt = '1753625037';
        <TR><TD class="table_title">Manufacturer:</TD><TD>
        <SCRIPT>document.write('RLTech');</SCRIPT>&nbsp;</TD></TR>
        <TR><TD class="table_title">Device Type:</TD><TD>10GE&nbsp;</TD></TR>
        <TR><TD class="table_title">Gateway Type:</TD><TD>RH8002GR&nbsp;</TD></TR>
        <TR><TD class="table_title">Serial Number:</TD><TD>
        02005F-RLFAKE092600019&nbsp;</TD></TR>
        <TR><TD class="table_title">Hardware Version:</TD><TD>V0.1.0&nbsp;</TD></TR>
        <TR><TD class="table_title">Software Version:</TD><TD>V0.0.29&nbsp;</TD></TR>
        <TR><TD class="table_title">Run Time:</TD><TD>
        18 Days 20 Hour 37 Min 46 Sec&nbsp;</TD></TR>
        <TR><TD class="table_title">CPU Temperature:</TD><TD>63 ℃&nbsp;</TD></TR>
        <TR><TD class="table_title">WAN Link Up Time:</TD><TD>
        18 Days 20 Hour 37 Min 14 Sec&nbsp;</TD></TR>
        """,
        now=now,
    )
    assert status.manufacturer == "RLTech"
    assert status.device_type == "10GE"
    assert status.gateway_type == "RH8002GR"
    assert status.serial_number == "RLFAKE092600019"
    assert status.device_identifier == "02005F-RLFAKE092600019"
    assert status.hardware_version == "V0.1.0"
    assert status.software_version == "V0.0.29"
    assert status.system_uptime == "18 Days 20 Hour 37 Min 46 Sec"
    assert status.wan_link_uptime == "18 Days 20 Hour 37 Min 14 Sec"
    assert status.last_boot == datetime(2026, 8, 1, 18, 22, 14, tzinfo=UTC)
    assert status.wan_link_up_since == datetime(2026, 8, 1, 18, 22, 46, tzinfo=UTC)
    assert status.cpu_temperature == 63
    assert status.current_time is None


def test_parse_olt_status_script_rendered_wan_link_uptime() -> None:
    now = datetime(2026, 8, 20, 15, 0, 0, tzinfo=UTC)
    status = api.parse_olt_status(
        """
        <TR>
          <TD class="table_title" width="20%">WAN Link Up Time:</TD>
          <TD>
            <script language=JavaScript type=text/javascript>
              function wanUpTime() {
                var curTime = '1708324';
                var WanUpTime = '32';
                var IsWanUp = '1';
                document.write('calculated by browser');
              }
              wanUpTime();
            </script>&nbsp;
          </TD>
        </TR>
        """,
        now=now,
    )

    assert status.wan_link_uptime == "19 Days 18 Hour 31 Min 32 Sec"
    assert status.wan_link_up_since == datetime(2026, 7, 31, 20, 28, 28, tzinfo=UTC)


def test_script_rendered_wan_link_timestamp_is_stabilized() -> None:
    previous = models.RltechData(
        olt_status=models.RltechOltStatus(
            wan_link_up_since=datetime(2026, 7, 31, 20, 28, 0, tzinfo=UTC),
        )
    )

    data = api.normalize_snapshot(
        [],
        [],
        olt_html="""
        <TR>
          <TD class="table_title" width="20%">WAN Link Up Time:</TD>
          <TD>
            <script language=JavaScript type=text/javascript>
              function wanUpTime() {
                var curTime = '1708324';
                var WanUpTime = '32';
                var IsWanUp = '1';
              }
              wanUpTime();
            </script>&nbsp;
          </TD>
        </TR>
        """,
        previous=previous,
        now=datetime(2026, 8, 20, 15, 0, 0, tzinfo=UTC),
    )

    assert data.olt_status.wan_link_up_since == datetime(
        2026, 7, 31, 20, 28, 0, tzinfo=UTC
    )


def test_olt_boot_timestamps_are_stabilized_across_small_uptime_drift() -> None:
    last_boot = datetime(2026, 8, 1, 14, 50, 0, tzinfo=UTC)
    wan_up_since = datetime(2026, 8, 1, 14, 51, 0, tzinfo=UTC)
    previous = models.RltechData(
        olt_status=models.RltechOltStatus(
            last_boot=last_boot,
            wan_link_up_since=wan_up_since,
        )
    )

    data = api.normalize_snapshot(
        [],
        [],
        olt_html="""
        <TR><TD class="table_title">Run Time:</TD><TD>10 Min 45 Sec&nbsp;</TD></TR>
        <TR><TD class="table_title">WAN Link Up Time:</TD><TD>9 Min 45 Sec&nbsp;</TD></TR>
        """,
        previous=previous,
        now=datetime(2026, 8, 1, 15, 0, 45, tzinfo=UTC),
    )

    assert data.olt_status.last_boot == last_boot
    assert data.olt_status.wan_link_up_since == wan_up_since

    rebooted = api.normalize_snapshot(
        [],
        [],
        olt_html="""
        <TR><TD class="table_title">Run Time:</TD><TD>3 Min&nbsp;</TD></TR>
        <TR><TD class="table_title">WAN Link Up Time:</TD><TD>2 Min&nbsp;</TD></TR>
        """,
        previous=previous,
        now=datetime(2026, 8, 1, 15, 10, 0, tzinfo=UTC),
    )

    assert rebooted.olt_status.last_boot == datetime(2026, 8, 1, 15, 7, 0, tzinfo=UTC)
    assert rebooted.olt_status.wan_link_up_since == datetime(2026, 8, 1, 15, 8, 0, tzinfo=UTC)


def test_parse_lan_and_lanpon_ports() -> None:
    html = """
    var lancntvalue = '{"data":[{"Port":"1", "LanState":"1", "TxBytes":"9643532541", "RxBytes":"9726199278", "Negoration":"1000M", "Mode":"Full"},{"Port":"2", "LanState":"0", "TxBytes":"0", "RxBytes":"0", "Negoration":"Down", "Mode":""},{"Port":"5", "LanState":"1", "TxBytes":"2095094151", "RxBytes":"18446744073115276764", "Negoration":"2500M", "Mode":"Full"}]}';
    Ethernet = [["0","Disabled","560097","4123","0","0","3096680","5897","0","0"],
    ["InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.2","Up","560097","4123","0","0","3096680","5897","0","0"],
    ["InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.3","Disabled","560362","4124","0","0","3096680","5897","0","0"],
    ["InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.4","Disabled","560362","4124","0","0","3096680","5897","0","0"],null]
    var ponport_info = '{ "list": [ { "ponid": 1, "fec": "disable", "active": "enable", "autoregister": "enable", "status": "up", "tx_power": "2.73", "rx_power": "-20.56", "temp": "53.59", "voltage": "3.08", "current": "44.39" }, { "ponid": 2, "fec": "disable", "active": "enable", "autoregister": "enable", "status": "up", "tx_power": "3.00", "rx_power": "-21.69", "temp": "52.26", "voltage": "3.08", "current": "40.92" } ] }';
    """

    lan_ports = api.parse_lan_ports(html)
    lanpon_ports = api.parse_lanpon_ports(html)

    assert lan_ports[1].status == "connected"
    assert lan_ports[1].connected is True
    assert lan_ports[1].rate == "1000M"
    assert lan_ports[1].mode == "Full-Duplex"
    assert lan_ports[2].status == "disconnected"
    assert lan_ports[5].label == "LANPON1"
    assert lan_ports[5].rate == "2500M"
    assert lanpon_ports[1].status == "up"
    assert lanpon_ports[1].rx_power == -20.56
    assert lanpon_ports[2].temperature == 52.26


def test_parse_lan_ports_falls_back_to_ethernet_array() -> None:
    lan_ports = api.parse_lan_ports(
        """
        Ethernet = [["InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.2","Up","1","2","3","4","5","6","7","8"],null]
        """
    )

    assert lan_ports[2].label == "LAN-2"
    assert lan_ports[2].status == "Up"
    assert lan_ports[2].connected is True
    assert lan_ports[2].tx_bytes == 1
    assert lan_ports[2].rx_packets == 6


def test_normalize_snapshot_includes_optional_lan_ports() -> None:
    data = api.normalize_snapshot(
        [payload([])],
        [payload([])],
        olt_html=None,
        user_html="""
        var lancntvalue = '{"data":[{"Port":"5", "LanState":"1", "TxBytes":"1", "RxBytes":"2", "Negoration":"2500M", "Mode":"Full"}]}';
        var ponport_info = '{ "list": [ { "ponid": 1, "status": "up" } ] }';
        """,
    )

    assert data.lan_ports[5].status == "connected"
    assert data.lan_ports[5].label == "LANPON1"
    assert data.lanpon_ports[1].status == "up"


def test_diagnostics_redaction_helper() -> None:
    diagnostics = load_module("diagnostics")
    result = diagnostics._redact(
        {
            "password": "secret",
            "username": "admin",
            "mqtt_password": "mqtt-secret",
            "mqtt_psk": "abcdef",
            "mqtt_psk_identity": "admin",
            "mqtt_host": "198.18.11.1",
            "base_url": "http://192.168.1.1",
            "nested": {"token": "abc", "ok": True, "mac": "aa:bb"},
        }
    )
    assert result["password"] == diagnostics.REDACTED
    assert result["username"] == diagnostics.REDACTED
    assert result["mqtt_password"] == diagnostics.REDACTED
    assert result["mqtt_psk"] == diagnostics.REDACTED
    assert result["mqtt_psk_identity"] == diagnostics.REDACTED
    assert result["mqtt_host"] == diagnostics.REDACTED
    assert result["base_url"] == diagnostics.REDACTED
    assert result["nested"]["token"] == diagnostics.REDACTED
    assert result["nested"]["mac"] == diagnostics.REDACTED
    assert result["nested"]["ok"] is True


def test_authentication_error_mapping() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession([FakeResponse(200, json.dumps({"Privilege": "0"}))])
        try:
            await client.login(session)
        except api.AuthenticationError:
            return
        raise AssertionError("expected AuthenticationError")

    asyncio.run(run())


def test_account_busy_mapping() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession([FakeResponse(200, json.dumps({"Logged": "1"}))])
        try:
            await client.login(session)
        except api.AccountBusyError:
            return
        raise AssertionError("expected AccountBusyError")

    asyncio.run(run())


def test_logout_after_success_and_failure_drops_token() -> None:
    async def run_success() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "0", "Privilege": "1", "Active": "1", "ecntToken": "tok"})),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                FakeResponse(200, "var phy_status = 'down';"),
                FakeResponse(200, ""),
                FakeResponse(200, ""),
            ]
        )
        await client.fetch_snapshot(session)
        assert client.token is None
        assert session.calls[-1][0] == "GET"
        assert session.calls[-1][1].endswith("/logout.cgi")

    async def run_failed_logout() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "0", "Privilege": "1", "Active": "1", "ecntToken": "tok"})),
                FakeResponse(500, ""),
            ]
        )
        await client.login(session)
        try:
            await client.logout(session)
        except api.UnexpectedResponse:
            # Tokens are single-use: never retried after a failed logout.
            assert client.token is None
            return
        raise AssertionError("expected logout failure")

    asyncio.run(run_success())
    asyncio.run(run_failed_logout())


def test_fetch_snapshot_stale_token_cleanup_does_not_block_refresh() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        client.token = "stale"
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    json.dumps(
                        {
                            "Logged": "0",
                            "Privilege": "1",
                            "Active": "1",
                            "ecntToken": "fresh",
                        }
                    ),
                ),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(
            session, include_hardware_status=False, status_interval=0
        )

        assert data.aps == {}
        assert data.stations == {}
        # The stale token is discarded without sending it to logout.cgi.
        assert session.calls[0][0] == "POST"
        assert session.calls[0][1].endswith("/check_auth.json")
        assert all("stale" not in str(call[2].get("headers")) for call in session.calls)
        assert session.calls[-1][1].endswith("/logout.cgi")
        assert client.token is None

    asyncio.run(run())


def test_fetch_snapshot_final_logout_failure_returns_successful_data() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    json.dumps(
                        {
                            "Logged": "0",
                            "Privilege": "1",
                            "Active": "1",
                            "ecntToken": "tok",
                        }
                    ),
                ),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                FakeResponse(500, ""),
            ]
        )

        data = await client.fetch_snapshot(
            session, include_hardware_status=False, status_interval=0
        )

        assert data.aps == {}
        assert data.stations == {}
        assert session.calls[-1][0] == "GET"
        assert session.calls[-1][1].endswith("/logout.cgi")
        assert client.token is None

        # The next poll must not reuse the old token, not even for logout.
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                FakeResponse(200, ""),
            ]
        )
        await client.fetch_snapshot(
            session, include_hardware_status=False, status_interval=0
        )
        assert session.calls[0][0] == "POST"
        assert session.calls[0][1].endswith("/check_auth.json")
        assert [call[1] for call in session.calls].count(
            "http://olt/cgi-bin/logout.cgi"
        ) == 1

    asyncio.run(run())


def test_fetch_snapshot_uses_legacy_status_when_8080_is_busy() -> None:
    async def run() -> None:
        previous = models.RltechData(
            aps={
                "02:00:5F:B8:DC:E0": models.RltechAp(
                    mac="02:00:5F:B8:DC:E0",
                    sn="RLGM5FB8DCE0",
                    alias="House11_Office",
                    online=True,
                )
            },
            stations={
                "02:00:7C:4C:18:40": models.RltechStation(
                    mac="02:00:7C:4C:18:40",
                    reported_online=True,
                    home=True,
                    last_seen=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
                )
            },
            last_success=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
            last_success_8080=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
            last_success_80=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
        )
        client = api.RltechClient(
            "http://olt:8080",
            "u",
            "p",
            legacy_base_urls=["http://olt", "http://slave"],
            legacy_username="admin",
            legacy_password="admin",
        )
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "1"})),
                FakeResponse(200, ""),
                FakeResponse(
                    200,
                    "X('RH8002GR','V5.0.1-51675','','','','','','02:00:5F:B9:D3:F0','19 Days 18 Hour 44 Min 43 Sec','8','20','RLFAKE092600019');",
                ),
                FakeResponse(200, "showPortInfo('LAN-1','1','Full','1000M','1','2');"),
                FakeResponse(200, "showLANPonInfo('LANPON1','disable','enable','enable','up','2.73 dBm','-20.48','49.83 ℃','3.08 mA','38.56 V');"),
                FakeResponse(200, ""),
                FakeResponse(200, ""),
                FakeResponse(200, ""),
                FakeResponse(
                    200,
                    "X('RH8002GR','V5.0.1-51675','','','','','','02:00:5F:B9:D3:F1','19 Days 18 Hour 44 Min 43 Sec','6','21','RLFAKE092600020');",
                ),
                FakeResponse(200, "showPortInfo('LAN-1','1','Full','1000M','3','4');"),
                FakeResponse(200, "showLANPonInfo('LANPON1','disable','enable','enable','up','2.90 dBm','-21.01','48.10 ℃','3.08 mA','37.00 V');"),
                FakeResponse(200, ""),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(session, previous=previous)

        assert data.aps == previous.aps
        assert data.stations == previous.stations
        assert data.last_success == previous.last_success
        assert data.last_success_8080 == previous.last_success_8080
        assert data.last_success_80 is not None
        assert data.last_success_80 > previous.last_success_80
        assert data.olt_status.cpu_usage == 8
        assert data.legacy_sources["olt"].olt_status.cpu_usage == 8
        assert data.legacy_sources["olt"].last_success == data.last_success_80
        assert data.legacy_sources["slave"].olt_status.cpu_usage == 6
        assert data.legacy_sources["slave"].last_success == data.last_success_80
        assert data.lanpon_ports[1].temperature == 49.83
        assert session.calls[0][1].endswith("/check_auth.json")
        assert any("/runinfo.asp" in call[1] for call in session.calls)

    asyncio.run(run())


def test_fetch_snapshot_raises_when_8080_and_legacy_both_fail() -> None:
    async def run() -> None:
        previous = models.RltechData(
            aps={
                "02:00:5F:B8:DC:E0": models.RltechAp(
                    mac="02:00:5F:B8:DC:E0",
                    sn="RLGM5FB8DCE0",
                    alias="House11_Office",
                    online=True,
                )
            },
            last_success=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
        )
        client = api.RltechClient(
            "http://olt:8080",
            "u",
            "p",
            legacy_base_urls=["http://olt"],
            legacy_username="admin",
            legacy_password="admin",
        )
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "1"})),
                FakeResponse(500, ""),
            ]
        )

        try:
            await client.fetch_snapshot(session, previous=previous)
        except api.AccountBusyError:
            return
        raise AssertionError("expected original 8080 error")

    asyncio.run(run())


def test_fetch_snapshot_can_skip_ap_and_station_pages() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "0", "Privilege": "1", "Active": "1", "ecntToken": "tok"})),
                FakeResponse(200, "CpuTemp = '40';"),
                FakeResponse(200, ""),
            ]
        )
        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            include_ap_inventory=False,
            include_station_inventory=False,
            include_hardware_status=False,
        )

        assert data.aps == {}
        assert data.stations == {}
        assert all("ap_online_list" not in call[1] for call in session.calls)
        assert all("ap_wlan_ac_client_list" not in call[1] for call in session.calls)

    asyncio.run(run())


def test_fetch_snapshot_preserves_stations_when_station_polling_is_skipped() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        station = models.RltechStation(
            mac="02:00:7C:4C:17:59",
            reported_online=True,
            home=True,
            last_seen=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        )
        previous = models.RltechData(stations={station.mac: station})
        session = FakeSession([])

        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            previous=previous,
            include_ap_inventory=False,
            include_station_inventory=False,
            include_hardware_status=False,
        )

        assert data.stations == previous.stations
        assert session.calls == []

    asyncio.run(run())


def test_fetch_snapshot_can_poll_aps_while_skipping_station_pages() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt", "u", "p")
        station = models.RltechStation(
            mac="02:00:7C:4C:17:59",
            reported_online=True,
            home=True,
            last_seen=datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        )
        previous = models.RltechData(stations={station.mac: station})
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    json.dumps(
                        {
                            "Logged": "0",
                            "Privilege": "1",
                            "Active": "1",
                            "ecntToken": "tok",
                        }
                    ),
                ),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            previous=previous,
            include_ap_inventory=True,
            include_station_inventory=False,
            include_hardware_status=False,
        )

        assert data.aps == {}
        assert data.stations == previous.stations
        assert any("ap_online_list" in call[1] for call in session.calls)
        assert all("ap_wlan_ac_client_list" not in call[1] for call in session.calls)

    asyncio.run(run())


AP_B_MAC = "E0:21:FE:B0:B0:00"


def _real_ap_b() -> "models.RltechAp":
    page = api.extract_embedded_json(
        (FIXTURES / "real_ap_online_list.html").read_text(encoding="utf-8"),
        "AP_manage",
    )
    return api._normalize_ap_payloads([page])[AP_B_MAC]


def _reboot_session(*pages: str, login: str | None = None) -> FakeSession:
    responses = [FakeResponse(200, login or LOGIN_OK)]
    responses += [FakeResponse(200, page) for page in pages]
    responses.append(FakeResponse(200, ""))  # logout
    return FakeSession(responses)


def test_reboot_form_is_the_list_form_with_two_fields_changed() -> None:
    client = api.RltechClient("http://olt:8080", "u", "p")
    list_fields = client._ap_list_fields()
    reboot_fields = client._ap_list_fields(
        upgrade_mac="E021FEB0B000", upgrade_action=api.AP_REBOOT_ACTION
    )
    # Same field names in the same order (the live ConfigForm, appendix A.5).
    assert [name for name, _ in reboot_fields] == [name for name, _ in list_fields]
    changed = {
        name
        for (name, value), (_, old) in zip(reboot_fields, list_fields)
        if value != old
    }
    assert changed == {"upgrade_mac", "upgrade_action"}
    assert dict(reboot_fields)["delete"] == "0"
    assert dict(list_fields)["delete"] == "0"
    assert api.AP_LIST_DELETE_NONE == "0"
    assert dict(reboot_fields)["upgrade_action"] == "2"
    # Byte-identical to the one reboot submitted in phase 0.
    timeline = json.loads(
        (FIXTURES / "real_ap_reboot_timeline.json").read_text(encoding="utf-8")
    )
    assert reboot_fields == [tuple(item) for item in timeline["form"]]


def test_reboot_via_ac_submits_once_and_logs_out() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        ap = replace(_real_ap_b(), mac="e0:21:fe:b0:b0:00")  # any MAC format
        session = _reboot_session(
            fixture("real_ap_online_list.html"),
            fixture("real_ap_online_list_reboot_submitted.html"),
        )

        record = await client.reboot_ap_via_ac(session, ap)

        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "ap_online_list.asp",
            "logout.cgi",
        ]
        posts = [call for call in session.calls if call[0] == "POST"][1:]
        assert len(posts) == 2
        for _method, _url, kwargs in posts:
            assert dict(kwargs["data"])["delete"] == "0"
            assert kwargs["headers"]["Cookie"] == (
                "ecntToken=tok; EBOOVALUE=" + api.eboo_value(kwargs["data"])
            )
        check, submit = (dict(call[2]["data"]) for call in posts)
        assert check["upgrade_mac"] == "0" and check["upgrade_action"] == ""
        assert submit["upgrade_mac"] == "E021FEB0B000"
        assert submit["upgrade_action"] == "2"
        assert record.result == "submitted"
        assert record.reboot_status == "0"
        assert client.reboot_records[AP_B_MAC] is record
        assert client.last_reboot is record
        assert client.token is None

    asyncio.run(run())


def test_reboot_via_ac_rejects_offline_ap_without_submitting() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        # +31 s page: the AP is offline (Status 0).
        session = _reboot_session(fixture("real_ap_online_list_rebooting.html"))
        try:
            await client.reboot_ap_via_ac(session, _real_ap_b())
        except api.ApRebootRejected as err:
            assert err.reason == "offline"
        else:
            raise AssertionError("expected ApRebootRejected")
        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "logout.cgi",
        ]
        assert client.last_reboot.result == "rejected_offline"

    asyncio.run(run())


def test_reboot_via_ac_rejects_unfinished_task_and_unknown_ap() -> None:
    async def run(page: str, ap) -> str:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = _reboot_session(page)
        try:
            await client.reboot_ap_via_ac(session, ap)
        except api.ApRebootRejected as err:
            assert _call_kinds(session)[-1] == "logout.cgi"
            assert len([c for c in session.calls if c[0] == "POST"]) == 2
            return err.reason
        raise AssertionError("expected ApRebootRejected")

    # Submitted page: the AP is online but its reboot task is still queued.
    submitted = fixture("real_ap_online_list_reboot_submitted.html")
    assert asyncio.run(run(submitted, _real_ap_b())) == "task_pending"
    unknown = replace(_real_ap_b(), mac="02:00:00:00:00:99")
    assert asyncio.run(run(fixture("real_ap_online_list.html"), unknown)) == (
        "not_listed"
    )


def test_reboot_via_ac_busy_and_unconfirmed() -> None:
    async def busy() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, json.dumps({"Logged": "1"}))])
        try:
            await client.reboot_ap_via_ac(session, _real_ap_b())
        except api.AccountBusyError:
            pass
        else:
            raise AssertionError("expected AccountBusyError")
        # No token, so no logout; nothing was posted to the AP list.
        assert _call_kinds(session) == ["check_auth.json"]
        assert client.last_reboot.result == "busy"

    async def unconfirmed() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        # The answer to the submit has no bare "1" line.
        session = _reboot_session(
            fixture("real_ap_online_list.html"), fixture("real_ap_online_list.html")
        )
        try:
            await client.reboot_ap_via_ac(session, _real_ap_b())
        except api.UnexpectedResponse:
            pass
        else:
            raise AssertionError("expected UnexpectedResponse")
        assert _call_kinds(session)[-1] == "logout.cgi"
        assert client.last_reboot.result == "unconfirmed"

    asyncio.run(busy())
    asyncio.run(unconfirmed())


def test_reboot_via_ac_caps_timeouts_and_skips_a_late_submit() -> None:
    async def run(list_seconds: float) -> tuple[list[str], FakeSession, object]:
        client = api.RltechClient("http://olt:8080", "u", "p")
        ticks = iter([100.0, 100.0 + list_seconds])
        client._clock = lambda: next(ticks)
        session = _reboot_session(
            fixture("real_ap_online_list.html"),
            fixture("real_ap_online_list_reboot_submitted.html"),
        )
        try:
            await client.reboot_ap_via_ac(session, _real_ap_b())
        except api.ApRebootRejected as err:
            return [err.reason], session, client
        return ["submitted"], session, client

    # A list answer within the guard: submitted, both requests capped at 8 s.
    result, session, _client = asyncio.run(run(2.0))
    assert result == ["submitted"]
    posts = [call for call in session.calls if "ap_online_list" in call[1]]
    assert len(posts) == 2
    if api.aiohttp is not None:
        assert all(call[2]["timeout"].total == 8 for call in posts)

    # A slow list answer (boa near its 10 s idle expiry): no submit, logout.
    result, session, client = asyncio.run(run(api.AP_REBOOT_SUBMIT_GUARD + 0.5))
    assert result == ["slow"]
    assert _call_kinds(session) == [
        "check_auth.json",
        "ap_online_list.asp",
        "logout.cgi",
    ]
    assert client.last_reboot.result == "rejected_slow"
    assert client.token is None


def test_reboot_via_ac_list_timeout_or_login_page_never_submits() -> None:
    async def run(list_response) -> tuple[type, FakeSession, object]:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [FakeResponse(200, LOGIN_OK), list_response, FakeResponse(200, "")]
        )
        try:
            await client.reboot_ap_via_ac(session, _real_ap_b())
        except Exception as err:  # noqa: BLE001 - asserted below
            return type(err), session, client
        raise AssertionError("expected an error")

    expected_calls = ["check_auth.json", "ap_online_list.asp", "logout.cgi"]
    kind, session, client = asyncio.run(run(RaisingResponse(TimeoutError())))
    assert issubclass(kind, TimeoutError)
    assert _call_kinds(session) == expected_calls
    assert client.last_reboot.result == "unreachable"

    login_page = (
        "<html><script>$.post('/cgi-bin/check_auth.json', {username: u})"
        "</script></html>"
    )
    for response in (
        FakeResponse(200, login_page),
        FakeResponse(302, "", {"Location": "/cgi-bin/login.asp"}),
    ):
        kind, session, client = asyncio.run(run(response))
        assert issubclass(kind, api.SessionExpired)
        assert _call_kinds(session) == expected_calls
        assert client.last_reboot.result == "error"
        assert client.token is None


def test_reboot_waits_for_a_running_poll_burst() -> None:
    class GatedResponse(FakeResponse):
        def __init__(self, body: str, gate: asyncio.Event) -> None:
            super().__init__(200, body)
            self.gate = gate

        async def __aenter__(self):
            await self.gate.wait()
            return self

    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        gate = asyncio.Event()
        session = FakeSession(
            [
                # Poll burst: login, AP list (held), clients, logout.
                FakeResponse(200, LOGIN_OK),
                GatedResponse(fixture("real_ap_online_list.html"), gate),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(200, ""),
                # Reboot burst: login, list, submit, logout.
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("real_ap_online_list.html")),
                FakeResponse(200, fixture("real_ap_online_list_reboot_submitted.html")),
                FakeResponse(200, ""),
            ]
        )
        poll = asyncio.create_task(
            client.fetch_snapshot(
                session,
                include_hardware_status=False,
                detail_interval=0,
                status_interval=0,
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        reboot = asyncio.create_task(client.reboot_ap_via_ac(session, _real_ap_b()))
        for _ in range(5):
            await asyncio.sleep(0)
        # The reboot waits for the session lock: no request of its own yet.
        assert _call_kinds(session) == ["check_auth.json", "ap_online_list.asp"]
        gate.set()
        await poll
        record = await reboot
        # Two whole bursts, never interleaved.
        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "ap_wlan_ac_client_list.asp",
            "logout.cgi",
            "check_auth.json",
            "ap_online_list.asp",
            "ap_online_list.asp",
            "logout.cgi",
        ]
        assert record.result == "submitted"

    asyncio.run(run())


def test_xadd_task_confirmation_and_task_list_on_real_pages() -> None:
    submitted = fixture("real_ap_online_list_reboot_submitted.html")
    rebooting = fixture("real_ap_online_list_rebooting.html")
    done = fixture("real_ap_online_list_reboot_done.html")
    plain = fixture("real_ap_online_list.html")

    assert api.xadd_task_accepted(submitted) is True
    for page in (plain, rebooting, done):
        assert api.xadd_task_accepted(page) is False

    assert api.parse_ap_task_list(plain) == {}
    states = [
        api.parse_ap_task_list(page)[AP_B_MAC]
        for page in (submitted, rebooting, done)
    ]
    assert [task.reboot_status for task in states] == ["0", "3", "1"]
    assert [task.reboot_state for task in states] == [
        "queued",
        "rebooting",
        "completed",
    ]
    assert [api.ap_task_blocks_reboot(task) for task in states] == [
        True,
        True,
        False,
    ]


def test_ap_task_blocks_reboot_follows_the_web_ui_rules() -> None:
    task = models.RltechApTask
    mac = AP_B_MAC
    assert api.ap_task_blocks_reboot(None) is False
    assert api.ap_task_blocks_reboot(task(mac, action="2", reboot_status="2")) is False
    assert api.ap_task_blocks_reboot(task(mac, action="2", reboot_status="3")) is True
    assert api.ap_task_blocks_reboot(task(mac, action="1", upgrade_status="4")) is False
    assert api.ap_task_blocks_reboot(task(mac, action="1", upgrade_status="2")) is True
    assert api.ap_task_blocks_reboot(task(mac, action="3", restore_status="0")) is True
    assert api.ap_task_blocks_reboot(task(mac, action="3", restore_status="1")) is False


def test_fetch_snapshot_keeps_the_ap_task_list() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")

        def session_for(list_page: str) -> FakeSession:
            return FakeSession(
                [
                    FakeResponse(200, LOGIN_OK),
                    FakeResponse(200, fixture(list_page)),
                    FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                    FakeResponse(200, ""),
                ]
            )

        data = await client.fetch_snapshot(
            session_for("real_ap_online_list_rebooting.html"),
            include_hardware_status=False,
            detail_interval=0,
            status_interval=0,
        )
        assert data.ap_tasks[AP_B_MAC].reboot_state == "rebooting"
        data = await client.fetch_snapshot(
            session_for("real_ap_online_list_reboot_done.html"),
            previous=data,
            include_hardware_status=False,
            detail_interval=0,
            status_interval=0,
        )
        assert data.ap_tasks[AP_B_MAC].reboot_state == "completed"

    asyncio.run(run())


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


LOGIN_OK = json.dumps(
    {"Logged": "0", "Privilege": "1", "Active": "1", "ecntToken": "tok"}
)
SYN_AP1_MAC = "02:00:5E:10:00:01"
SYN_AP2_MAC = "02:00:5E:10:00:02"


def test_normalize_ap_parses_list_optical_power() -> None:
    page = api.extract_embedded_json(
        fixture("synthetic_ap_online_list.html"), "AP_manage"
    )
    aps = api._normalize_ap_payloads([page])

    assert aps[SYN_AP1_MAC].optical_tx_power == 2.15
    assert aps[SYN_AP1_MAC].optical_rx_power == -18.42
    # SQL NULL ("NULL") and "" both mean unknown.
    assert aps[SYN_AP2_MAC].optical_tx_power is None
    assert aps[SYN_AP2_MAC].optical_rx_power is None
    # Lenient about an optional unit suffix and padding.
    ap = api.normalize_ap(
        {"Mac": "02005E100009", "OptTxPower": " 1.5 dBm", "OptRxPower": "-20"}
    )
    assert ap.optical_tx_power == 1.5
    assert ap.optical_rx_power == -20.0


def test_parse_ap_detail_synthetic_full_prefers_pon_values() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    detail = api.parse_ap_detail(
        fixture("synthetic_ap_online_detail_full.html"),
        now=now,
        local_timezone=AC_TZ,
    )

    assert detail.mac == SYN_AP1_MAC
    assert detail.sn == "RLGM5E100001"
    assert detail.cpu_usage == 7
    assert detail.cpu_temperature == 63
    assert detail.memory_usage == 41
    assert detail.flash_usage == 86
    assert detail.sys_duration == 86461
    assert detail.last_boot == now - timedelta(seconds=86461)
    # registerinfo optics win over the list_ap copies (2.15 / -18.42).
    assert detail.optical_tx_power == 2.21
    assert detail.optical_rx_power == -18.37
    assert detail.optical_temperature == 48.2
    assert detail.last_down_cause == "Reboot"
    assert detail.last_down_time == "2026-09-28 18:24:10"
    assert detail.last_down_at == datetime(2026, 9, 28, 18, 24, 10, tzinfo=AC_TZ)
    assert detail.reg_off_time == detail.last_down_at
    assert detail.hardware_version is None
    assert detail.detail_source == "8080"
    assert detail.detail_error is None


def test_parse_ap_detail_falls_back_to_list_ap_without_pon() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    no_pon = api.parse_ap_detail(
        fixture("synthetic_ap_online_detail_no_pon.html"), now=now
    )

    assert no_pon.mac == SYN_AP2_MAC
    assert no_pon.optical_tx_power == 1.98
    assert no_pon.optical_rx_power == -20.05
    assert no_pon.last_down_cause == "Offline"
    assert no_pon.last_down_at is None
    assert no_pon.reg_off_time is None
    assert no_pon.pon_id is None
    assert no_pon.last_boot == now - timedelta(seconds=3605)
    assert no_pon.detail_error is None

    empty_pon = api.parse_ap_detail(
        fixture("synthetic_ap_online_detail_empty_pon.html"), now=now
    )
    assert empty_pon.cpu_usage == 7
    assert empty_pon.optical_tx_power == 2.15
    assert empty_pon.optical_rx_power == -18.42
    assert empty_pon.last_down_cause is None
    assert empty_pon.detail_error is None


def test_parse_ap_detail_salvages_truncated_list_ap() -> None:
    text = fixture("synthetic_ap_online_detail_truncated.html")
    try:
        api._extract_json_from_js_string(text, "list_ap")
    except api.UnexpectedResponse:
        pass
    else:  # pragma: no cover - guards the fixture itself
        raise AssertionError("fixture list_ap should be truncated")

    detail = api.parse_ap_detail(text, now=datetime(2026, 9, 29, tzinfo=UTC))

    assert detail.detail_error == "list_ap_truncated"
    assert detail.mac == SYN_AP1_MAC
    assert detail.cpu_usage == 7
    assert detail.flash_usage == 86
    assert detail.sys_duration == 86461
    assert detail.optical_tx_power == 2.21
    assert detail.optical_rx_power == -18.37


def test_parse_ap_detail_rejects_unusable_list_ap() -> None:
    for body in (
        "var list_ap ='{ \"respCode\":3, \"failreason\":\"x\" } '",
        "var list_ap ='{ \"respCode\":0, \"data\":{ \"Model\":\"RL8'",
        "<html>login</html>",
    ):
        try:
            api.parse_ap_detail(body)
        except (api.UnexpectedResponse, api.SessionExpired):
            continue
        raise AssertionError(f"expected parse failure for {body!r}")


def test_parse_ap_detail_offline_ap_and_never_down_sentinel() -> None:
    page = (
        "var list_ap ='"
        + json.dumps(
            {
                "respCode": 0,
                "data": {
                    "Mac": "02005E100001",
                    "Status": "0",
                    "SysDuration": "500",
                    "LastOfflineReason": "NULL",
                },
            }
        )
        + "'\nlet list_pon = '"
        + json.dumps(
            {
                "result": 0,
                "data": json.dumps(
                    {
                        "last_down_time": "1970-01-01 08:00:00",
                        "last_down_cause": "",
                    }
                ),
            }
        )
        + "';"
    )

    detail = api.parse_ap_detail(page, local_timezone=AC_TZ)

    assert detail.online is False
    assert detail.last_boot is None
    assert detail.last_down_at is None
    assert detail.last_down_cause is None
    assert api._parse_ac_datetime("2026/09/28 18:24:10", local_timezone=AC_TZ) == (
        datetime(2026, 9, 28, 18, 24, 10, tzinfo=AC_TZ)
    )
    assert api._parse_ac_datetime("Fri Aug 21 06:12:26 2026") == datetime(
        2026, 8, 21, 6, 12, 26, tzinfo=UTC
    )
    assert api._parse_ac_datetime("N/A") is None


ROUTE_DETAIL_BY_AP = "__route_detail_by_ap__"
DETAIL_FIXTURE_BY_MAC_KEY = {
    "02005E100001": "synthetic_ap_online_detail_full.html",
    "02005E100002": "synthetic_ap_online_detail_no_pon.html",
}


def test_parse_olt_status_real_chinese_sta_device() -> None:
    now = datetime(2026, 9, 29, 11, 4, 0, tzinfo=UTC)
    status = api.parse_olt_status(fixture("real_sta_device.html"), now=now)

    assert status.serial_number == "RL0000000000003"
    assert status.device_identifier == "E021FE-RL0000000000003"
    assert status.hardware_version == "V0.1.0"
    assert status.software_version == "V0.0.30"
    assert status.system_uptime == "14 Days 30 Min 42 Sec"
    assert status.cpu_temperature == 52
    assert status.manufacturer == "ODM"
    # curTime (AC uptime seconds) is the boot reference: 1211442 s.
    assert status.last_boot == now - timedelta(seconds=1211442)
    # System uptime sensor (stage 6c): the same curTime seconds.
    assert status.uptime_seconds == 1211442
    assert status.wan_link_uptime == "8 Days 8 Hour 29 Min 59 Sec"
    # The stDeviceInfo placeholder and top.ModelName must not leak in.
    assert status.gateway_type is None
    assert status.device_type is None
    assert status.pon_up_since is None
    assert status.cpu_usage is None
    assert status.memory_usage is None


def test_parse_sta_user_real_lan_ports_identity_and_pon() -> None:
    text = fixture("real_sta_user.html")

    lan_ports = api.parse_lan_ports(text)
    assert sorted(lan_ports) == [1, 2, 3, 4, 5, 6]
    assert lan_ports[3].tx_bytes == 33186232
    assert lan_ports[3].rx_bytes == 8049172
    assert lan_ports[6].label == "LANPON2"
    assert lan_ports[6].connected is True
    assert lan_ports[6].tx_bytes == 959789560
    # 18446744073147154563 is a u64 wrap-around, not a real counter.
    assert lan_ports[6].rx_bytes is None

    assert api.parse_ac_lan_identity(text) == ("192.0.2.1", "E0:21:FE:C0:C0:00")

    lanpon = api.parse_lanpon_ports(text)
    assert lanpon[2].status == "up"
    assert lanpon[2].rx_power == -15.52


def test_parse_ap_detail_real_samples() -> None:
    now = datetime(2026, 9, 29, 11, 5, tzinfo=UTC)
    with_pon = api.parse_ap_detail(
        fixture("real_ap_online_detail_fs8820a.html"), now=now, local_timezone=AC_TZ
    )
    assert with_pon.mac == "E0:21:FE:A0:A0:00"
    assert with_pon.cpu_usage == 5
    assert with_pon.sys_duration == 5878566
    assert with_pon.optical_tx_power == 2.43
    assert with_pon.optical_rx_power == -15.07
    # last_down_time "-" means never: no timestamp and no stale cause.
    assert with_pon.last_down_time == "-"
    assert with_pon.last_down_at is None
    assert with_pon.last_down_cause is None
    assert with_pon.detail_error is None

    no_pon = api.parse_ap_detail(
        fixture("real_ap_online_detail_rl820gw.html"), now=now
    )
    assert no_pon.alias == "820"
    assert no_pon.optical_tx_power == -2.5
    assert no_pon.optical_rx_power == -14.4
    assert no_pon.pon_id is None
    assert no_pon.detail_error is None

    try:
        api.parse_ap_detail(fixture("real_ap_online_detail_bad_mac.html"))
    except api.UnexpectedResponse:
        pass
    else:
        raise AssertionError("respCode 2 must be rejected")

    assert api._parse_ac_datetime("-") is None


def test_fetch_snapshot_reads_controller_status_pages_every_interval() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")

        def session_for(*extra: FakeResponse) -> FakeSession:
            return FakeSession(
                [
                    FakeResponse(200, LOGIN_OK),
                    FakeResponse(200, fixture("real_ap_online_list.html")),
                    FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                    *extra,
                    FakeResponse(200, ""),
                ]
            )

        session = session_for(
            FakeResponse(200, fixture("real_sta_device.html")),
            FakeResponse(200, fixture("real_sta_user.html")),
            FakeResponse(200, fixture("sta_network_link_down.html")),
        )
        data = await client.fetch_snapshot(
            session, include_hardware_status=False, detail_interval=0
        )

        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "ap_wlan_ac_client_list.asp",
            "logout.cgi",
        ]
        assert [call[1].rsplit("/", 1)[1] for call in session.calls][3:7] == [
            "sta-device.asp",
            "sta-user.asp",
            "sta-network.asp",
            "logout.cgi",
        ]
        assert all(
            call[0] == "GET" for call in session.calls if "sta-network" in call[1]
        )
        assert data.uplink_pon.link_state == "down"
        assert data.uplink_pon.rx_power is None
        assert data.olt_status.serial_number == "RL0000000000003"
        assert data.olt_status.lan_mac == "E0:21:FE:C0:C0:00"
        assert data.olt_status.lan_ip == "192.0.2.1"
        assert data.olt_status.last_boot is not None
        assert data.lan_ports[6].label == "LANPON2"
        assert data.lanpon_ports[2].status == "up"
        assert data.web_status_update is not None
        assert len(data.aps) == 2
        assert len(data.stations) == 5

        # Within the 5-minute interval the pages are skipped, values kept.
        session = session_for()
        again = await client.fetch_snapshot(
            session, previous=data, include_hardware_status=False, detail_interval=0
        )
        assert all("sta-" not in call[1] for call in session.calls)
        assert again.olt_status.serial_number == "RL0000000000003"
        assert again.lan_ports == data.lan_ports
        assert again.web_status_update == data.web_status_update
        assert again.uplink_pon == data.uplink_pon

    asyncio.run(run())


def test_parse_uplink_pon_without_fibre_gives_no_values() -> None:
    # Field fragment (2026-09-30): LinkSta 'N/A', TxPower/RxPower '1',
    # SupplyVoltage/TxBiasCurrent/Temperature '0', PonState forced to 'up'.
    pon = api.parse_uplink_pon(fixture("sta_network_link_down.html"))
    assert pon is not None
    assert pon.link_state == "down"
    assert pon.registration_state == "unregistered"
    assert pon.loid_status == "init"
    assert pon.optics_state == "no_readings"
    # Never 0 V / 0 mA / 0 C, never the -40 dBm the page JS computes.
    for key in ("tx_power", "rx_power", "temperature", "voltage", "bias_current"):
        assert getattr(pon, key) is None, key
    assert pon.pon_type is None  # 'N/A' (the page would fake 'GPON')


def test_parse_uplink_pon_link_up_uses_the_page_formulas() -> None:
    pon = api.parse_uplink_pon(fixture("sta_network_link_up.html"))
    assert pon.link_state == "up"
    assert pon.registration_state == "authenticated"
    assert pon.loid_status == "up"
    assert pon.pon_type == "GPON"
    assert pon.tx_power == 3.0  # 10*log10(19953/10000)
    assert pon.rx_power == -14.0  # 10*log10(398/10000)
    assert pon.temperature == 47.0  # 12032/256
    assert pon.voltage == 3.3  # 33000/10 mV
    assert pon.bias_current == 12.0  # 6000*2/1000 mA
    assert pon.optics_state == "ok"


def test_parse_uplink_pon_edge_cases() -> None:
    up = fixture("sta_network_link_up.html")
    # Negative temperatures are 16-bit two's complement in 1/256 C.
    cold = api.parse_uplink_pon(up.replace("Temperature = '12032'", "Temperature = '64256'"))
    assert cold.temperature == -5.0
    # LinkSta 'N/A' but TxPower '1': the page shows -40 dBm; we report None.
    na = up.replace("'gpon_phy_up'", "'down'").replace("= 'up';\n", "= 'down';\n", 1)
    na = na.replace("this.LinkSta					= '1'", "this.LinkSta = 'N/A'")
    na = na.replace("TxPower = '19953'", "TxPower = '1'")
    pon = api.parse_uplink_pon(na)
    assert pon.link_state == "down"
    assert pon.tx_power is None and pon.rx_power is None
    # Module readings stay when the module reports a supply voltage.
    assert pon.voltage == 3.3 and pon.temperature == 47.0
    assert pon.optics_state == "no_signal"
    # Physical link up but no traffic: registered, not authenticated.
    unauth = up.replace("this.trafficstate			= 'up'", "this.trafficstate = 'down'")
    pon = api.parse_uplink_pon(unauth)
    assert pon.link_state == "up"
    assert pon.registration_state == "unauthenticated"
    # PonState is never used: 'up' alone does not make a link.
    assert api._pon_link_state(None, None, None) is None
    # Page rule (review S2): only an up phy makes the link up / registered.
    stale = up.replace("'gpon_phy_up'", "'gpon_phy_down'")
    pon = api.parse_uplink_pon(stale)  # fibre just pulled, traffic still up
    assert pon.link_state == "down"
    assert pon.registration_state == "unregistered"
    assert pon.tx_power is None and pon.rx_power is None
    assert api._pon_link_state("1", "up", None) is None
    assert api._pon_link_state("0", None, None) == "down"
    assert api._pon_link_state("N/A", "down", None) == "down"
    assert api.parse_uplink_pon("<html>no pon section</html>") is None


def test_uplink_link_type_and_online_status_follow_the_page() -> None:
    # Field reconstruction: ActiveEtherWan + SFP choose, PonMode 3, GE module
    # (get_pontype ASP values inferred), PonState overwritten with
    # EthernetState 'up' after the if/else -> the page shows online / GE.
    down = api.parse_uplink_pon(fixture("sta_network_link_down.html"))
    assert down.pon_state == "up"
    assert down.online_status == "online"
    assert down.page_link_type == "GE"
    assert down.link_type == "ge"
    assert (down.uplink_sfp_choose, down.active_ether_wan, down.ae_wan_speed) == (
        "Yes",
        "Yes",
        "1000M",
    )
    # Online, but still no optical values (phy rule unchanged, stage 6).
    for key in ("tx_power", "rx_power", "temperature", "voltage", "bias_current"):
        assert getattr(down, key) is None, key
    up = api.parse_uplink_pon(fixture("sta_network_link_up.html"))
    assert (up.online_status, up.link_type) == ("online", "gpon")
    gpon = api.parse_uplink_pon(fixture("sta_network_gpon_no_fibre.html"))
    assert (gpon.pon_state, gpon.online_status, gpon.link_type) == (
        "down",
        "offline",
        "gpon",
    )
    assert gpon.tx_power is None and gpon.voltage is None


def test_page_pon_type_matches_get_pontype() -> None:
    f = api.page_pon_type
    assert f("gpon_phy_up", "1", "No", "No", "N/A") == "GPON"
    assert f("epon_phy_up", "2", None, None, None) == "EPON"
    assert f("gpon_phy_up", "6", None, None, None) == "XG-PON"
    assert f("gpon_phy_up", "7", None, None, None) == "XGS-PON"
    assert f("epon_phy_up", "5", None, None, None) == "EPON"
    assert f("epon_phy_up", "3", None, None, None) == "10G-EPON"
    assert f("down", "1", None, None, None) == "GPON"
    assert f("down", "4", None, None, None) == "10G-EPON"
    # JS: undefined/empty PonMode is "not 0/1/2" -> 10G-EPON.
    assert f("gpon_phy_up", None, None, None, None) == "10G-EPON"
    assert f("gpon_phy_up", "", None, None, None) == "10G-EPON"
    assert f("down", "3", "Yes", "Yes", "1000M") == "GE"
    assert f("down", "3", "Yes", "Yes", "10000M") == "10GE"
    assert f("down", "3", "Yes", "Yes", "N/A") == "GE/10GE"
    assert f("down", "3", "Yes", "No", "1000M") == "10G-EPON"
    assert set(api.PON_LINK_TYPES) == {
        "GPON", "EPON", "XG-PON", "XGS-PON", "10G-EPON", "GE", "10GE", "GE/10GE",
    }


def _uplink_variants() -> dict[str, str]:
    down = fixture("sta_network_link_down.html")
    up = fixture("sta_network_link_up.html")
    gpon = fixture("sta_network_gpon_no_fibre.html")
    return {
        "field": down,
        "gpon_up": up,
        "gpon_no_fibre": gpon,
        # ActiveEtherWan with the Ethernet uplink down.
        "aewan_down": down.replace(
            "    this.PonState = 'up';\n    if( '0' != this.LinkSta)",
            "    this.PonState = 'down';\n    if( '0' != this.LinkSta)",
        ),
        "aewan_connecting": down.replace(
            "    this.PonState = 'up';\n    if( '0' != this.LinkSta)",
            "    this.PonState = 'connecting';\n    if( '0' != this.LinkSta)",
        ),
        "10ge": down.replace("ae_wan_speed = '1000M'", "ae_wan_speed = '10000M'"),
        "ge_or_10ge": down.replace("ae_wan_speed = '1000M'", "ae_wan_speed = ''"),
        "no_sfp_choice": down.replace(
            "is_up_link_sfp_choose = 'Yes'", "is_up_link_sfp_choose = 'No'"
        ),
        "xgs_pon": up.replace("PonMode					=	'1'", "PonMode					=	'7'"),
        "epon_down": gpon.replace("PonMode					=	'1'", "PonMode					=	'2'"),
        # Traffic up without the ActiveEtherWan override: the if-branch wins.
        "gpon_traffic_only": gpon.replace(
            "this.trafficstate			= 'down'", "this.trafficstate			= 'up'"
        ),
        # The page compares case-sensitively: 'UP' is not up (review N4).
        "gpon_traffic_upper": gpon.replace(
            "this.trafficstate			= 'down'", "this.trafficstate			= 'UP'"
        ),
    }


def test_uplink_parse_equals_the_page_javascript() -> None:
    """Run the fixture's own page script in Node and compare (when available)."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name, page in _uplink_variants().items():
        start = page.index("function transTemperature")
        end = page.index("</SCRIPT>", start)
        script = page[start:end]
        program = (
            "const document = {write() {}};\n"
            + script
            + "\nprocess.stdout.write(JSON.stringify("
            "{state: PonInfo.PonState, type: get_pontype()}));"
        )
        result = subprocess.run(
            [node, "-e", program], capture_output=True, text=True, check=True
        )
        expected = json.loads(result.stdout)
        pon = api.parse_uplink_pon(page)
        assert pon.pon_state == expected["state"], name
        assert pon.page_link_type == expected["type"], name
        assert pon.link_type == api.PON_LINK_TYPES[expected["type"]], name


def test_uplink_link_type_is_unknown_without_its_page_inputs() -> None:
    down = fixture("sta_network_link_down.html")
    # Non-JOYME4 build: PonMode is not rendered. The JS would print 10G-EPON.
    no_mode = down.replace("    this.PonMode = '3';\n", "")
    pon = api.parse_uplink_pon(no_mode)
    assert pon.page_link_type == "10G-EPON"
    assert pon.link_type is None
    # Rendered but empty or N/A PonMode: the same artefact (review N4).
    for empty in ("''", "'N/A'"):
        pon = api.parse_uplink_pon(down.replace("this.PonMode = '3';", f"this.PonMode = {empty};"))
        assert pon.page_link_type == "10G-EPON", empty
        assert pon.link_type is None, empty
    # No rendered get_pontype body (page variant).
    start = down.index("function get_pontype()\n{")
    no_func = down[:start] + down[down.index("return pon_type", start) + 20 :]
    assert api.parse_uplink_pon(no_func).link_type is None
    # trafficstate 'UP' is not 'up' for the page (case-sensitive).
    gpon = fixture("sta_network_gpon_no_fibre.html").replace(
        "this.trafficstate			= 'down'", "this.trafficstate			= 'UP'"
    )
    assert api.parse_uplink_pon(gpon).online_status == "offline"
    # Online status needs PonState; without any assignment it is unknown.
    no_state = down.replace("this.PonState", "this.PonStateX")
    assert api.parse_uplink_pon(no_state).online_status is None


def test_uplink_pon_is_dropped_after_two_status_cycles_without_a_parse() -> None:
    async def run(age_seconds: int):
        client = api.RltechClient("http://olt:8080", "u", "p")
        now = datetime.now(UTC)
        previous = models.RltechData(
            uplink_pon=api.parse_uplink_pon(fixture("sta_network_link_up.html")),
            uplink_pon_update=now - timedelta(seconds=age_seconds),
        )
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("real_ap_online_list.html")),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(200, fixture("real_sta_device.html")),
                FakeResponse(200, fixture("real_sta_user.html")),
                # Page changed: no PonInfoClass any more.
                FakeResponse(200, "<html><body>new layout</body></html>"),
                FakeResponse(200, ""),
            ]
        )
        return await client.fetch_snapshot(
            session,
            previous=previous,
            include_hardware_status=False,
            detail_interval=0,
            status_interval=300,
        )

    kept = asyncio.run(run(300))  # one missed cycle: last values kept
    assert kept.uplink_pon is not None and kept.uplink_pon.rx_power == -14.0
    dropped = asyncio.run(run(601))  # over 2 x 300 s: dropped
    assert dropped.uplink_pon is None
    assert api.UPLINK_PON_MAX_STATUS_CYCLES == 2
    # A successful parse refreshes the timestamp.
    pon = api.parse_uplink_pon(fixture("sta_network_link_down.html"))
    now = datetime.now(UTC)
    assert api._age_uplink_pon(pon, now, now=now, status_interval=300) == (pon, now)


def test_network_page_timeout_ends_the_burst_with_logout() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("real_ap_online_list.html")),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(200, fixture("real_sta_device.html")),
                FakeResponse(200, fixture("real_sta_user.html")),
                RaisingResponse(TimeoutError()),
                FakeResponse(200, ""),
            ]
        )
        previous_pon = api.parse_uplink_pon(fixture("sta_network_link_up.html"))
        data = await client.fetch_snapshot(
            session,
            previous=models.RltechData(uplink_pon=previous_pon),
            include_hardware_status=False,
            detail_interval=0,
        )
        assert _call_kinds(session)[-1] == "logout.cgi"
        assert client.token is None
        assert data.uplink_pon == previous_pon  # last good values kept
        assert data.olt_status.serial_number == "RL0000000000003"

    asyncio.run(run())


def test_fetch_snapshot_status_page_failure_keeps_inventory() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("real_ap_online_list.html")),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(500, ""),
                FakeResponse(200, fixture("real_sta_user.html")),
                FakeResponse(200, fixture("sta_network_link_up.html")),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(
            session, include_hardware_status=False, detail_interval=0
        )

        assert len(data.aps) == 2
        assert data.olt_status.serial_number is None
        assert data.olt_status.lan_mac == "E0:21:FE:C0:C0:00"
        assert data.uplink_pon.rx_power == -14.0
        assert _call_kinds(session)[-1] == "logout.cgi"
        assert client.token is None

    asyncio.run(run())


def test_diagnostics_skip_placeholder_details_and_redact_ac_identity() -> None:
    diagnostics = load_module("diagnostics")
    ap = models.RltechAp(mac=SYN_AP1_MAC, sn="RLGM5E100001", online=True)
    other = models.RltechAp(mac=SYN_AP2_MAC, sn="RLGM5E100002", online=True)
    data = models.RltechData(
        aps={ap.mac: ap, other.mac: other},
        ap_details={
            ap.mac: models.RltechApDetail(mac=ap.mac, cpu_usage=5),
            other.mac: models.RltechApDetail(
                mac=other.mac,
                detail_source="8080",
                detail_error="timeout",
                web_detail_update=datetime(2026, 9, 29, tzinfo=UTC),
            ),
        },
        olt_status=models.RltechOltStatus(
            serial_number="RL0000000000003", lan_mac="E0:21:FE:C0:C0:00"
        ),
        legacy_sources={
            "198.51.100.29": models.RltechLegacyOltSource(
                host="198.51.100.29", base_url="http://198.51.100.29"
            )
        },
    )

    class Holder:
        pass

    coordinator = Holder()
    coordinator.data = data
    coordinator.client = Holder()
    coordinator.client.ap_detail_stats = {"success": 1}
    hass = Holder()
    entry = Holder()
    entry.entry_id = "entry"
    entry.data = {}
    entry.runtime_data = coordinator

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, entry)
    )
    summary = result["summary"]

    assert summary["ap_detail_count"] == 1
    assert summary["ap_missing_detail_count"] == 1
    assert summary["ap_detail_error_count"] == 1
    identity = summary["ac_identity"]
    assert identity["serial_number"] == diagnostics.REDACTED
    assert identity["lan_mac"] == diagnostics.REDACTED
    assert identity["serial_number_present"] is True
    assert identity["lan_mac_present"] is True
    assert list(summary["legacy_sources"]) == ["source_0"]
    assert "198.51.100.29" not in json.dumps(result)


def test_merge_olt_status_prefers_8080_and_fills_from_legacy() -> None:
    web = models.RltechOltStatus(serial_number="RL0000000000003", cpu_temperature=52)
    legacy = models.RltechOltStatus(
        serial_number="RLFAKE092600019", cpu_usage=8, memory_usage=20
    )

    merged = api._merge_olt_status(web, legacy)

    assert merged.serial_number == "RL0000000000003"
    assert merged.cpu_usage == 8
    assert merged.memory_usage == 20
    assert merged.cpu_temperature == 52
    assert api._merge_olt_status(None, legacy) is legacy
    assert api._merge_olt_status(web, None) is web


def _snapshot_session(detail_body: str) -> FakeSession:
    return DetailRoutingSession(
        [
            FakeResponse(200, LOGIN_OK),
            FakeResponse(200, fixture("synthetic_ap_online_list.html")),
            FakeResponse(
                200,
                html_payload(
                    "STA_manage",
                    payload([{"Mac": "02007C4C1840", "APMac": "02005E100001"}]),
                ),
            ),
            FakeResponse(200, detail_body),
            FakeResponse(200, ""),
        ]
    )


def _call_kinds(session: FakeSession) -> list[str]:
    kinds = []
    for _method, url, _kwargs in session.calls:
        for name in (
            "check_auth.json",
            "ap_online_list.asp",
            "ap_wlan_ac_client_list.asp",
            "ap_online_detail.asp",
            "mag-reset.asp",
            "logout.cgi",
        ):
            if name in url:
                kinds.append(name)
    return kinds


def test_fetch_snapshot_fetches_due_ap_detail_inside_8080_session() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = _snapshot_session(ROUTE_DETAIL_BY_AP)

        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            include_hardware_status=False,
            scan_interval=60,
            detail_interval=300,
        )

        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "ap_wlan_ac_client_list.asp",
            "ap_online_detail.asp",
            "logout.cgi",
        ]
        detail_url = session.calls[3][1]
        # 2 APs, 5 polls per detail interval: one detail per poll.
        assert len(data.ap_details) == 1
        polled_mac = next(iter(data.ap_details))
        polled_ap = data.aps[polled_mac]
        mac_key = polled_mac.replace(":", "")
        assert f"param1={mac_key}_{polled_ap.sn}&param2={polled_ap.sn}" in detail_url
        detail = data.ap_details[polled_mac]
        assert detail.cpu_usage == 7
        assert detail.web_detail_update == detail.last_update
        assert detail.web_detail_update is not None
        assert client.ap_detail_stats["success"] == 1
        assert client.token is None

        # Next poll picks up the AP that is still missing its detail.
        session = _snapshot_session(ROUTE_DETAIL_BY_AP)
        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            previous=data,
            include_hardware_status=False,
            scan_interval=60,
            detail_interval=300,
        )
        assert set(data.ap_details) == {SYN_AP1_MAC, SYN_AP2_MAC}
        assert "ap_online_detail.asp" in session.calls[3][1]
        assert mac_key not in session.calls[3][1]
        assert data.ap_details[polled_mac].cpu_usage == 7

    asyncio.run(run())


def test_fetch_snapshot_detail_parse_failure_keeps_list_and_stations() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = _snapshot_session("<html><body>no detail here</body></html>")

        data = await client.fetch_snapshot(
            session, include_hardware_status=False, status_interval=0
        )

        assert set(data.aps) == {SYN_AP1_MAC, SYN_AP2_MAC}
        assert "02:00:7C:4C:18:40" in data.stations
        assert _call_kinds(session)[-1] == "logout.cgi"
        (detail,) = data.ap_details.values()
        assert detail.detail_error == "parse_error"
        assert detail.cpu_usage is None
        assert detail.last_update is None
        assert detail.web_detail_update is not None
        assert client.ap_detail_stats["parse_errors"] == 1
        assert client.token is None

    asyncio.run(run())


def test_fetch_snapshot_skips_details_when_burst_budget_is_spent() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = _snapshot_session("unused")
        session.responses.pop(3)

        data = await client.fetch_snapshot(
            session, include_hardware_status=False, burst_budget=0, status_interval=0
        )

        assert "ap_online_detail.asp" not in _call_kinds(session)
        assert _call_kinds(session)[-1] == "logout.cgi"
        assert data.ap_details == {}
        assert client.ap_detail_stats["budget_skipped"] == 1
        assert set(data.aps) == {SYN_AP1_MAC, SYN_AP2_MAC}

    asyncio.run(run())


def test_fetch_due_ap_details_stabilizes_last_boot() -> None:
    async def run() -> None:
        now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
        client = api.RltechClient("http://olt:8080", "u", "p")
        client.token = "tok"
        ap = models.RltechAp(mac=SYN_AP1_MAC, sn="RLGM5E100001", online=True)
        previous_boot = now - timedelta(seconds=86461 + 45)
        previous = models.RltechData(
            aps={ap.mac: ap},
            ap_details={
                ap.mac: models.RltechApDetail(
                    mac=ap.mac,
                    last_boot=previous_boot,
                    web_detail_update=now - timedelta(seconds=300),
                )
            },
        )
        session = FakeSession(
            [FakeResponse(200, fixture("synthetic_ap_online_detail_full.html"))]
        )

        details = await client._fetch_due_ap_details(
            session,
            {ap.mac: ap},
            previous,
            now=now,
            scan_interval=60,
            detail_interval=300,
        )

        assert details[ap.mac].last_boot == previous_boot

    asyncio.run(run())


def test_merge_ap_details_prefers_fresh_web_then_legacy_then_previous() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    earlier = now - timedelta(minutes=5)
    ap = models.RltechAp(mac=SYN_AP1_MAC, sn="RLGM5E100001")
    other = models.RltechAp(mac=SYN_AP2_MAC, sn="RLGM5E100002")
    previous = models.RltechData(
        aps={ap.mac: ap, other.mac: other},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac,
                cpu_usage=5,
                optical_rx_power=-19.0,
                source_host="olt",
                last_update=earlier,
                web_detail_update=earlier,
            ),
            other.mac: models.RltechApDetail(
                mac=other.mac,
                cpu_usage=9,
                last_update=earlier,
                web_detail_update=earlier,
            ),
        },
    )
    legacy = {
        ap.mac: models.RltechApDetail(
            mac=ap.mac,
            optical_rx_power=-18.9,
            reg_off_time=earlier,
            last_update=now,
        )
    }
    web = {
        ap.mac: models.RltechApDetail(
            mac=ap.mac,
            cpu_usage=7,
            optical_rx_power=-18.37,
            last_update=now,
            web_detail_update=now,
            detail_source="8080",
        ),
        other.mac: models.RltechApDetail(
            mac=other.mac,
            detail_source="8080",
            detail_error="parse_error",
            web_detail_update=now,
        ),
    }

    merged = api._merge_ap_details(
        {ap.mac: ap, other.mac: other}, previous=previous, legacy=legacy, web=web
    )

    assert merged[ap.mac].cpu_usage == 7
    assert merged[ap.mac].optical_rx_power == -18.37
    assert merged[ap.mac].reg_off_time == earlier
    assert merged[ap.mac].source_host == "olt"
    assert merged[ap.mac].detail_source == "8080"
    assert merged[ap.mac].detail_error is None
    # A failed attempt keeps last known values but records the error.
    assert merged[other.mac].cpu_usage == 9
    assert merged[other.mac].last_update == earlier
    assert merged[other.mac].detail_error == "parse_error"
    assert merged[other.mac].web_detail_update == now

    ok_again = api._merge_ap_details(
        {other.mac: other},
        previous=models.RltechData(ap_details=merged),
        legacy={},
        web={
            other.mac: models.RltechApDetail(
                mac=other.mac, cpu_usage=3, detail_source="8080", web_detail_update=now
            )
        },
    )
    assert ok_again[other.mac].detail_error is None
    assert ok_again[other.mac].cpu_usage == 3

    legacy_only = api._merge_ap_details(
        {ap.mac: ap}, previous=previous, legacy=legacy, web={}
    )
    assert legacy_only[ap.mac].optical_rx_power == -18.9
    assert legacy_only[ap.mac].detail_source == "legacy"
    assert legacy_only[ap.mac].cpu_usage == 5


def test_mqtt_ap_health_carries_optics_and_offline_reason() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    ap = models.RltechAp(mac=SYN_AP1_MAC, sn="RLGM5E100001")
    data = models.RltechData(
        aps={ap.mac: ap},
        ap_details={
            ap.mac: models.RltechApDetail(
                mac=ap.mac, optical_tx_power=2.0, last_down_cause="Reboot"
            )
        },
    )
    _cmd, update = mqtt.parse_mqtt_payload(
        json.dumps(
            {
                "Cmd": "XReport_ExtendInfo",
                "Send": "02005E100001",
                "Data": {
                    "OptTxPower": "",
                    "OptRxPower": "-18.40",
                    "LastOfflineReason": "Offline",
                },
            }
        )
    )

    assert update.optical_tx_power is None
    assert update.optical_rx_power == -18.4
    merged = mqtt.merge_ap_health_update(data, update, now=now).ap_details[ap.mac]
    assert merged.optical_tx_power == 2.0
    assert merged.optical_rx_power == -18.4
    assert merged.last_down_cause == "Offline"
    assert merged.detail_source == "mqtt"


def test_fetch_snapshot_stops_details_after_timeout_and_logs_out() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = _snapshot_session("unused")
        session.responses[3] = RaisingResponse(TimeoutError())

        data = await client.fetch_snapshot(
            session,
            status_interval=0,
            include_hardware_status=False,
            scan_interval=60,
            detail_interval=60,  # both APs due in this poll
        )

        kinds = _call_kinds(session)
        assert kinds.count("ap_online_detail.asp") == 1
        assert kinds[-1] == "logout.cgi"
        assert client.token is None
        assert client.ap_detail_stats["fetch_errors"] == 1
        assert client.ap_detail_stats["aborted"] == 1
        (detail,) = data.ap_details.values()
        assert detail.detail_error == "timeout"
        assert set(data.aps) == {SYN_AP1_MAC, SYN_AP2_MAC}
        # min(DEFAULT_TIMEOUT, 8) per detail request.
        assert session.calls[3][2]["timeout"].total == 8

    asyncio.run(run())


def test_fetch_due_ap_details_discards_mismatched_mac() -> None:
    async def run() -> None:
        now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
        client = api.RltechClient("http://olt:8080", "u", "p")
        client.token = "tok"
        ap = models.RltechAp(mac=SYN_AP2_MAC, sn="RLGM5E100002", online=True)
        # The page returned belongs to AP1.
        session = FakeSession(
            [FakeResponse(200, fixture("synthetic_ap_online_detail_full.html"))]
        )

        details = await client._fetch_due_ap_details(
            session,
            {ap.mac: ap},
            None,
            now=now,
            scan_interval=60,
            detail_interval=300,
        )

        assert details[ap.mac].detail_error == "mac_mismatch"
        assert details[ap.mac].cpu_usage is None
        assert details[ap.mac].sn is None
        assert client.ap_detail_stats["parse_errors"] == 1

    asyncio.run(run())


def test_mqtt_live_overlay_preserves_newer_mqtt_optics() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    mac = SYN_AP1_MAC
    ap = models.RltechAp(mac=mac, sn="RLGM5E100001")
    fresh = models.RltechData(
        aps={mac: ap},
        ap_details={
            mac: models.RltechApDetail(
                mac=mac,
                optical_rx_power=-18.0,
                optical_tx_power=2.0,
                last_down_cause="Reboot",
                last_update=now,
            )
        },
    )
    current = models.RltechData(
        aps={mac: ap},
        ap_details={
            mac: models.RltechApDetail(
                mac=mac,
                optical_rx_power=-25.0,
                last_down_cause="Offline",
                cpu_usage=50.0,
                last_update=now + timedelta(seconds=3),
            )
        },
    )

    detail = mqtt.preserve_live_overlay(current, fresh).ap_details[mac]

    assert detail.optical_rx_power == -25.0
    assert detail.optical_tx_power == 2.0
    assert detail.last_down_cause == "Offline"
    assert detail.cpu_usage == 50.0


def test_mqtt_live_overlay_does_not_revert_fresh_8080_detail() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    mac = SYN_AP1_MAC
    ap = models.RltechAp(mac=mac, sn="RLGM5E100001")
    stale = models.RltechApDetail(
        mac=mac, cpu_usage=5, memory_usage=40, last_update=now - timedelta(minutes=5)
    )
    fresh = models.RltechData(
        aps={mac: ap},
        ap_details={
            mac: models.RltechApDetail(
                mac=mac, cpu_usage=7, memory_usage=None, last_update=now
            )
        },
    )
    # Only a station changed via MQTT during the poll: AP detail is stale.
    current = models.RltechData(aps={mac: ap}, ap_details={mac: stale})

    detail = mqtt.preserve_live_overlay(current, fresh).ap_details[mac]

    assert detail.cpu_usage == 7
    assert detail.memory_usage == 40


def html_payload(name: str, body: dict) -> str:
    return f"var {name} = '{json.dumps(body).replace('&', '&amp;')}';"


class FakeResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def text(self, **kwargs):
        return self.body

    async def read(self):
        return self.body.encode()


class RaisingResponse:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


class DetailRoutingSession(FakeSession):
    """Serve the synthetic detail page matching the requested AP."""

    def get(self, url, **kwargs):
        response = super().get(url, **kwargs)
        if getattr(response, "body", None) == ROUTE_DETAIL_BY_AP:
            mac_key = url.split("param1=", 1)[1].split("_", 1)[0]
            response.body = fixture(DETAIL_FIXTURE_BY_MAC_KEY[mac_key])
        return response


class DummyDevice:
    def __init__(self, **values) -> None:
        self.__dict__.update(values)


def test_source_state_tracks_streaks_and_kind_changes() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    state = sources.SourceState("web")

    assert state.record_failure("busy", t0) is True
    assert state.record_failure("busy", t0 + timedelta(minutes=1)) is False
    assert state.consecutive_failures == 2
    assert state.failing_since == t0
    # A different problem restarts the streak.
    assert state.record_failure("unreachable", t0 + timedelta(minutes=2)) is True
    assert state.consecutive_failures == 1
    assert state.failing_since == t0 + timedelta(minutes=2)
    assert state.record_success(t0 + timedelta(minutes=3)) is True
    assert state.state == "ok"
    assert state.failing is False
    assert state.record_success(t0 + timedelta(minutes=4)) is False


def test_circuit_breaker_opens_after_three_failures_and_half_opens() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    breaker = sources.CircuitBreaker()

    assert breaker.record_failure(t0) is False
    assert breaker.record_failure(t0) is False
    assert breaker.record_failure(t0) is True
    assert breaker.allow(t0 + timedelta(minutes=29)) is False
    assert breaker.allow(t0 + timedelta(minutes=30)) is True
    # The single trial after the cooldown fails: open again at once.
    assert breaker.record_failure(t0 + timedelta(minutes=30)) is True
    assert breaker.allow(t0 + timedelta(minutes=31)) is False
    breaker.record_success()
    assert breaker.allow(t0 + timedelta(minutes=31)) is True


def test_network_retry_after_backs_off_to_five_minutes() -> None:
    assert [sources.network_retry_after(n, 60) for n in range(1, 6)] == [
        60,
        120,
        240,
        300,
        300,
    ]


def test_busy_grace_and_issue_decisions() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    state = sources.SourceState("web")
    for minute in range(3):
        state.record_failure("busy", t0 + timedelta(minutes=minute))
        assert sources.busy_within_grace(state, 3) is True
    state.record_failure("busy", t0 + timedelta(minutes=3))
    assert sources.busy_within_grace(state, 3) is False

    assert sources.desired_web_issue(state, t0 + timedelta(minutes=9)) is None
    assert (
        sources.desired_web_issue(state, t0 + timedelta(minutes=10))
        == sources.ISSUE_WEB_UI_BUSY
    )
    state.record_failure("unreachable", t0 + timedelta(minutes=10))
    assert sources.desired_web_issue(state, t0 + timedelta(minutes=39)) is None
    assert (
        sources.desired_web_issue(state, t0 + timedelta(minutes=40))
        == sources.ISSUE_WEB_UI_UNREACHABLE
    )
    state.record_success(t0 + timedelta(minutes=41))
    assert sources.desired_web_issue(state, t0 + timedelta(minutes=41)) is None


def test_legacy_breaker_stops_requests_without_warnings(caplog) -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
        )
        calls = 0

        async def failing_source(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError()

        client._fetch_legacy_source = failing_source
        now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
        for minute in range(10):
            await client._fetch_legacy_snapshot(
                object(), {}, None, now=now + timedelta(minutes=minute)
            )
        assert calls == 3
        assert client.legacy_states["olt"].last_error_kind == "unreachable"

        await client._fetch_legacy_snapshot(
            object(), {}, None, now=now + timedelta(minutes=33)
        )
        assert calls == 4

    with caplog.at_level("DEBUG"):
        asyncio.run(run())
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]


def test_legacy_requests_use_short_timeout_and_run_outside_web_lock() -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
        )
        lock_held: list[bool] = []

        async def legacy_login(*_args, **_kwargs):
            lock_held.append(client._lock.locked())
            raise TimeoutError()

        client.legacy_login = legacy_login
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, html_payload("AP_manage", payload([]))),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(session, status_interval=0)

        assert lock_held == [False]
        assert data.aps == {}
        assert client.last_web_error is None
        assert client.last_burst_ms is not None
        assert client.legacy_timeout == 5

    asyncio.run(run())


def test_legacy_http_requests_use_legacy_timeout() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, "")])
        await client.legacy_fetch_html(session, "http://olt", 1, "/runinfo.asp")
        assert session.calls[0][2]["timeout"].total == 5

    asyncio.run(run())


def test_fetch_snapshot_records_web_error_for_coordinator() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, json.dumps({"Logged": "1"}))])
        try:
            await client.fetch_snapshot(session, include_hardware_status=False)
        except api.AccountBusyError:
            pass
        assert isinstance(client.last_web_error, api.AccountBusyError)
        assert api.error_kind(client.last_web_error) == "busy"
        assert api.error_kind(TimeoutError()) == "unreachable"
        assert api.error_kind(api.UnexpectedResponse("x")) == "error"

    asyncio.run(run())


def test_mqtt_error_kinds_and_backoff() -> None:
    assert mqtt.mqtt_error_kind(TimeoutError()) == "timeout"
    assert mqtt.mqtt_error_kind(ConnectionRefusedError()) == "connection_refused"
    assert mqtt.mqtt_error_kind(OSError(113, "No route")) == "unreachable"
    assert (
        mqtt.mqtt_error_kind(mqtt.RltechMqttError("CONNACK refused: 5"))
        == "connack_refused"
    )
    delays = [5]
    for _ in range(8):
        delays.append(mqtt.mqtt_next_backoff(delays[-1]))
    assert delays[:4] == [5, 10, 20, 40]
    assert delays[-1] == 300
    assert "last_error_kind" in mqtt.RltechMqttStats().as_dict()


def test_web_failure_decision_busy_grace_then_fail() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    state = sources.SourceState("web")
    decisions = []
    for minute in range(4):
        state.record_failure("busy", t0 + timedelta(minutes=minute))
        decisions.append(
            sources.decide_web_failure(state, grace_polls=3, scan_interval=60)
        )

    assert [d.serve_previous for d in decisions] == [True, True, True, False]
    assert all(d.translation_key == "web_ui_busy" for d in decisions)
    # Busy is retried every poll: no back-off.
    assert all(d.retry_after is None for d in decisions)
    # Recovery resets the streak, so a new busy spell gets a fresh grace.
    state.record_success(t0 + timedelta(minutes=5))
    state.record_failure("busy", t0 + timedelta(minutes=6))
    assert sources.decide_web_failure(
        state, grace_polls=3, scan_interval=60
    ).serve_previous


def test_web_failure_decision_network_errors_back_off() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    state = sources.SourceState("web")
    retries = []
    for minute in range(5):
        state.record_failure("unreachable", t0 + timedelta(minutes=minute))
        decision = sources.decide_web_failure(state, grace_polls=3, scan_interval=60)
        assert decision.serve_previous is False
        assert decision.translation_key == "web_ui_unreachable"
        retries.append(decision.retry_after)
    assert retries == [60, 120, 240, 300, 300]
    state.record_failure("error", t0 + timedelta(minutes=6))
    assert (
        sources.decide_web_failure(state, grace_polls=3, scan_interval=60)
        .translation_key
        == "web_ui_error"
    )


def test_entity_availability_matrix() -> None:
    now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    max_age = timedelta(minutes=15)
    online = models.RltechAp(mac=SYN_AP1_MAC, online=True, optical_rx_power=-15.0)
    offline = models.RltechAp(mac=SYN_AP1_MAC, online=False)
    fresh = models.RltechApDetail(mac=SYN_AP1_MAC, last_update=now)
    stale = models.RltechApDetail(
        mac=SYN_AP1_MAC, last_update=now - timedelta(minutes=16)
    )

    # AP row entities.
    assert sources.ap_entity_available(online, requires_online=True)
    assert sources.ap_entity_available(offline, requires_online=False)
    assert not sources.ap_entity_available(offline, requires_online=True)
    assert not sources.ap_entity_available(None, requires_online=False)

    # AP detail entities: present, online, fresh (or list fallback).
    detail = sources.ap_detail_entity_available
    assert detail(online, fresh, now=now, max_age=max_age)
    assert not detail(online, stale, now=now, max_age=max_age)
    assert not detail(online, None, now=now, max_age=max_age)
    assert detail(online, stale, now=now, max_age=max_age, fallback_value=-15.0)
    assert not detail(offline, fresh, now=now, max_age=max_age)
    assert not detail(None, fresh, now=now, max_age=max_age)

    # Optional port-80 entities follow their source state.
    assert sources.source_available("ok")
    for state in ("busy", "unreachable", "error", "disabled", "unknown", None):
        assert not sources.source_available(state)


def test_async_close_waits_for_the_web_burst_lock() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, "")])
        order: list[str] = []

        async def burst() -> None:
            async with client._lock:
                client.token = "tok"
                await asyncio.sleep(0.01)
                order.append("burst_done")
                # fetch_snapshot always logs out in finally.
                client.token = None

        task = asyncio.create_task(burst())
        await asyncio.sleep(0)
        await client.async_close(session)
        order.append("closed")
        await task

        assert order == ["burst_done", "closed"]
        # The burst already logged out, so close sends nothing.
        assert session.calls == []

        client.token = "tok2"
        await client.async_close(session)
        assert session.calls[0][1].endswith("/logout.cgi")
        assert client.token is None

    asyncio.run(run())


def test_diagnostics_include_source_states() -> None:
    diagnostics = load_module("diagnostics")
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)

    class Holder:
        pass

    coordinator = Holder()
    coordinator.data = None
    coordinator.web_state = sources.SourceState("web")
    coordinator.web_state.record_failure("busy", t0)
    coordinator.web_issue = None
    coordinator.web_polling_paused_until = None
    coordinator.client = api.RltechClient(
        "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
    )
    coordinator.client.last_burst_ms = 210
    coordinator.client.legacy_states["olt"] = sources.SourceState("legacy:olt")
    coordinator.client.legacy_breakers["olt"] = sources.CircuitBreaker()
    hass = Holder()
    entry = Holder()
    entry.entry_id = "entry"
    entry.data = {}
    entry.runtime_data = coordinator

    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    src = result["sources"]

    assert src["web"]["state"] == "busy"
    assert src["busy_since"] == t0.isoformat()
    assert src["web_burst_ms"] == 210
    assert src["detail_parse_errors"] == 0
    assert src["legacy"][0]["breaker"]["trips"] == 0
    assert "olt" not in json.dumps(src["legacy"])


def test_translations_cover_issues_exceptions_and_switch() -> None:
    strings = json.loads((PKG_ROOT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (PKG_ROOT / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert strings == english
    for key in (sources.ISSUE_WEB_UI_BUSY, sources.ISSUE_WEB_UI_UNREACHABLE):
        description = strings["issues"][key]["description"]
        assert "{host}" in description
        assert "Web UI polling" in description
    # Only the unreachable issue points at Reconfigure (a new address).
    assert "reconfigure" not in strings["issues"]["web_ui_busy"]["description"].lower()
    assert "Reconfigure" in strings["issues"]["web_ui_unreachable"]["description"]
    for key in ("web_ui_busy", "web_ui_unreachable", "web_ui_error"):
        assert strings["exceptions"][key]["message"]
    assert strings["entity"]["switch"]["web_polling"]["name"] == "Web UI polling"
    const_text = (PKG_ROOT / "const.py").read_text(encoding="utf-8")
    assert "Platform.SWITCH" in const_text


def test_detail_timeout_or_expiry_skips_status_pages_and_logs_out() -> None:
    async def run(detail: object) -> list[str]:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("synthetic_ap_online_list.html")),
                FakeResponse(200, html_payload("STA_manage", payload([]))),
                detail,
                FakeResponse(200, ""),
            ]
        )
        data = await client.fetch_snapshot(
            session,
            include_hardware_status=False,
            detail_interval=300,
            status_interval=300,
        )
        assert client.token is None
        # Status pages stay due for the next poll.
        assert data.web_status_update is None
        return [call[1].rsplit("/", 1)[1].split("?", 1)[0] for call in session.calls]

    expected = [
        "check_auth.json",
        "ap_online_list.asp",
        "ap_wlan_ac_client_list.asp",
        "ap_online_detail.asp",
        "logout.cgi",
    ]
    assert asyncio.run(run(RaisingResponse(TimeoutError()))) == expected
    redirect = FakeResponse(302, "", {"Location": "/cgi-bin/login.asp"})
    assert asyncio.run(run(redirect)) == expected


def test_wan_master_ports_follow_8080_when_port_80_never_works() -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
        )

        async def failing_source(*_args, **_kwargs):
            raise OSError(113, "No route to host")

        client._fetch_legacy_source = failing_source
        user_1 = fixture("real_sta_user.html")
        user_2 = user_1.replace(
            '{"Port":"1", "LanState":"0"', '{"Port":"1", "LanState":"1"'
        ).replace(
            '"ponid": 1, "fec": "disable", "active": "enable", '
            '"autoregister": "enable", "status": "down"',
            '"ponid": 1, "fec": "disable", "active": "enable", '
            '"autoregister": "enable", "status": "up"',
        )

        def session_for(user_html: str) -> FakeSession:
            return FakeSession(
                [
                    FakeResponse(200, LOGIN_OK),
                    FakeResponse(200, fixture("real_ap_online_list.html")),
                    FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                    FakeResponse(200, fixture("real_sta_device.html")),
                    FakeResponse(200, user_html),
                    FakeResponse(200, fixture("sta_network_link_down.html")),
                    FakeResponse(200, ""),
                ]
            )

        first = await client.fetch_snapshot(
            session_for(user_1), detail_interval=0, status_interval=300
        )
        assert first.lan_ports[1].connected is False
        assert first.lanpon_ports[1].status == "down"

        # Make the status pages due again and poll with a changed sta-user.
        due = replace(
            first, web_status_update=first.web_status_update - timedelta(seconds=301)
        )
        second = await client.fetch_snapshot(
            session_for(user_2),
            previous=due,
            detail_interval=0,
            status_interval=300,
        )
        assert second.lan_ports[1].connected is True
        assert second.lanpon_ports[1].status == "up"
        assert second.web_lan_ports[1].connected is True

    asyncio.run(run())


def test_legacy_fallback_uses_only_its_own_previous_values() -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
        )

        async def failing_source(*_args, **_kwargs):
            raise OSError(113, "No route to host")

        client._fetch_legacy_source = failing_source
        merged_only = models.RltechData(
            lan_ports={1: models.RltechLanPort(port=1, status="connected")},
            olt_status=models.RltechOltStatus(serial_number="from-8080"),
        )
        status, lan, lanpon, _details, _sources = await client._fetch_legacy_snapshot(
            object(), {}, merged_only, now=datetime(2026, 9, 29, tzinfo=UTC)
        )
        # Port 80 never succeeded: nothing of the merged 8080 data leaks back.
        assert (status, lan, lanpon) == (None, {}, {})

        own = models.RltechData(
            lan_ports={1: models.RltechLanPort(port=1, status="connected")},
            legacy_sources={
                "olt": models.RltechLegacyOltSource(
                    host="olt",
                    base_url="http://olt",
                    olt_status=models.RltechOltStatus(cpu_usage=8),
                    lan_ports={2: models.RltechLanPort(port=2, status="up")},
                )
            },
        )
        status, lan, _lanpon, _details, _sources = await client._fetch_legacy_snapshot(
            object(), {}, own, now=datetime(2026, 9, 29, tzinfo=UTC)
        )
        assert status.cpu_usage == 8
        assert list(lan) == [2]

    asyncio.run(run())


def test_busy_grace_is_not_renewed_by_interleaved_errors() -> None:
    t0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    state = sources.SourceState("web")
    serve = []
    kinds = ["busy", "busy", "busy", "busy", "unreachable", "busy", "error", "busy"]
    for minute, kind in enumerate(kinds):
        state.record_failure(kind, t0 + timedelta(minutes=minute))
        serve.append(
            sources.decide_web_failure(state, grace_polls=3, scan_interval=60)
            .serve_previous
        )

    # After the grace is used up, busy never serves stale data again until a
    # success, even with other errors in between.
    assert serve == [True, True, True, False, False, False, False, False]
    assert state.busy_polls == 6
    assert state.busy_since == t0
    # The busy issue timer runs from the first busy poll and survives an
    # interleaved unexpected response.
    assert (
        sources.desired_web_issue(state, t0 + timedelta(minutes=10))
        == sources.ISSUE_WEB_UI_BUSY
    )
    state.record_failure("error", t0 + timedelta(minutes=10))
    assert (
        sources.desired_web_issue(state, t0 + timedelta(minutes=10))
        == sources.ISSUE_WEB_UI_BUSY
    )
    state.record_success(t0 + timedelta(minutes=11))
    state.record_failure("busy", t0 + timedelta(minutes=12))
    assert sources.decide_web_failure(
        state, grace_polls=3, scan_interval=60
    ).serve_previous


# --- Stage 3: device model (plan 3.4.2), pure decisions in ap_device_registry --

ENTRY = "entry"
T0 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)


def _device(device_id: str, **values):
    return DummyDevice(id=device_id, **values)


def test_device_identifiers_and_names() -> None:
    reg = ap_device_registry
    ap = models.RltechAp(mac="E0:21:FE:B0:B0:00", sn="RLGMFEB0B000", alias="820")

    assert reg.controller_identifier(ENTRY) == ("rltech_fttr", "entry")
    assert reg.ap_identifier(ENTRY, "RLGMFEB0B000") == (
        "rltech_fttr",
        "entry_ap_RLGMFEB0B000",
    )
    assert reg.ap_mac_connection(ap) == ("mac", "e0:21:fe:b0:b0:00")
    assert reg.ap_device_name(ap) == "RLTech AP 820"
    # Alias cleared on the AC: fall back to the SN.
    assert reg.ap_device_name(replace(ap, alias=None)) == "RLTech AP RLGMFEB0B000"
    # Event payloads carry identifiers as lists.
    assert (
        reg.sn_from_ap_identifiers(ENTRY, [["rltech_fttr", "entry_ap_RLGMFEB0B000"]])
        == "RLGMFEB0B000"
    )
    assert reg.sn_from_ap_identifiers(ENTRY, [["rltech_fttr", "entry"]]) is None
    assert reg.sn_from_ap_identifiers(ENTRY, [["other", "entry_ap_X"]]) is None


def test_ap_rename_updates_device_name_only() -> None:
    ap = models.RltechAp(mac="E0:21:FE:B0:B0:00", sn="SN1", alias="Kitchen")
    device = DummyDevice(
        name="RLTech AP 820",
        name_by_user="My custom name",
        connections={("mac", "e0:21:fe:b0:b0:00")},
        serial_number="SN1",
    )

    updates = ap_device_registry.ap_device_registry_updates(
        ap, device, controller_device_id=None
    )

    assert updates == {"name": "RLTech AP Kitchen"}


def test_same_sn_new_mac_replaces_only_the_mac_connection() -> None:
    ap = models.RltechAp(mac="E0:21:FE:B0:B0:99", sn="SN1", alias="820")
    device = DummyDevice(
        name="RLTech AP 820",
        serial_number="SN1",
        connections={("mac", "e0:21:fe:b0:b0:00"), ("upnp", "uuid:1")},
    )

    updates = ap_device_registry.ap_device_registry_updates(
        ap, device, controller_device_id=None
    )

    assert updates == {
        "new_connections": {("mac", "e0:21:fe:b0:b0:99"), ("upnp", "uuid:1")}
    }


def test_same_mac_new_sn_strips_mac_from_the_old_device() -> None:
    reg = ap_device_registry
    ap = models.RltechAp(mac="E0:21:FE:B0:B0:00", sn="NEWSN")
    old_device = _device(
        "old", connections={("mac", "e0:21:fe:b0:b0:00"), ("upnp", "uuid:1")}
    )

    # New SN has no device yet; the old SN's device still owns the MAC. If it
    # kept it, registering the new SN would be merged into the old device.
    strip = reg.mac_connection_conflict(
        ap, own_device=None, connection_holder=old_device
    )
    assert strip == reg.ConnectionStrip(
        device_id="old", new_connections={("upnp", "uuid:1")}
    )
    # Nothing to do when the MAC already belongs to this AP's own device.
    own = _device("own", connections={("mac", "e0:21:fe:b0:b0:00")})
    assert reg.mac_connection_conflict(ap, own_device=own, connection_holder=own) is None
    assert reg.mac_connection_conflict(ap, own_device=own, connection_holder=None) is None


def test_missing_timer_offline_ap_is_not_missing() -> None:
    # Offline APs stay in the AC list (Status=0): no timer.
    missing = ap_device_registry.update_missing_since(
        {},
        known_sns={"A", "B"},
        listed_sns={"A", "B"},
        now=T0,
        list_fresh=True,
        ac_last_boot=T0 - timedelta(days=3),
    )
    assert missing == {}


def test_missing_timer_starts_and_ap_is_removed_after_n_days() -> None:
    reg = ap_device_registry
    missing = reg.update_missing_since(
        {},
        known_sns={"A", "B"},
        listed_sns={"A"},
        now=T0,
        list_fresh=True,
        ac_last_boot=T0 - timedelta(days=3),
    )
    assert missing == {"B": T0}
    # The start time is kept on later polls.
    later = reg.update_missing_since(
        missing,
        known_sns={"A", "B"},
        listed_sns={"A"},
        now=T0 + timedelta(days=6),
        list_fresh=True,
        ac_last_boot=T0 - timedelta(days=3),
    )
    assert later == {"B": T0}
    assert reg.stale_sns(later, now=T0 + timedelta(days=6, hours=23), days=7) == []
    assert reg.stale_sns(later, now=T0 + timedelta(days=7), days=7) == ["B"]
    # 0 disables automatic removal.
    assert reg.stale_sns(later, now=T0 + timedelta(days=70), days=0) == []


def test_missing_timer_ignores_ac_reboot_and_stale_lists() -> None:
    reg = ap_device_registry
    started = {"B": T0}
    base = dict(known_sns={"A", "B", "C"}, now=T0 + timedelta(hours=1))

    # AC rebooted: the AP database is empty until the APs register again.
    assert reg.update_missing_since(
        started, listed_sns=set(), list_fresh=True, ac_last_boot=None, **base
    ) == {"B": T0}
    # AC up for less than 30 minutes: no new timers (C), existing kept.
    assert reg.update_missing_since(
        started,
        listed_sns={"A"},
        list_fresh=True,
        ac_last_boot=T0 + timedelta(minutes=40),
        **base,
    ) == {"B": T0}
    # Busy/unreachable poll (stale list): nothing starts.
    assert reg.update_missing_since(
        {}, listed_sns={"A"}, list_fresh=False, ac_last_boot=None, **base
    ) == {}


def test_missing_timer_clears_when_a_deleted_ap_comes_back() -> None:
    assert ap_device_registry.update_missing_since(
        {"B": T0},
        known_sns={"A", "B"},
        listed_sns={"A", "B"},
        now=T0 + timedelta(days=1),
        list_fresh=True,
        ac_last_boot=None,
    ) == {}


def test_device_removal_rules() -> None:
    allowed = ap_device_registry.device_removal_allowed
    kw = dict(listed_sns={"LISTED"}, legacy_hosts={"198.51.100.2"})

    assert not allowed(ENTRY, [("rltech_fttr", "entry")], **kw)
    assert not allowed(ENTRY, [("rltech_fttr", "entry_ap_LISTED")], **kw)
    assert allowed(ENTRY, [("rltech_fttr", "entry_ap_GONE")], **kw)
    assert not allowed(ENTRY, [("rltech_fttr", "entry_legacy_olt_198.51.100.2")], **kw)
    assert allowed(ENTRY, [("rltech_fttr", "entry_legacy_olt_198.51.100.3")], **kw)
    assert not allowed(ENTRY, [("other", "entry_ap_GONE")], **kw)


def test_legacy_only_entity_cleanup_is_strict() -> None:
    reg = ap_device_registry

    def ent(entity_id, unique_id, platform="rltech_fttr", config_entry_id=ENTRY):
        return DummyDevice(
            entity_id=entity_id,
            unique_id=unique_id,
            platform=platform,
            config_entry_id=config_entry_id,
        )

    entries = [
        ent("sensor.olt_cpu", "entry_olt_cpu_usage"),
        ent("sensor.olt_mem", "entry_olt_memory_usage"),
        ent("sensor.ap_src", "entry_ap_SN1_source_host"),
        ent("sensor.olt_temp", "entry_olt_cpu_temperature"),
        ent("sensor.ap_cpu", "entry_ap_SN1_cpu_usage"),
        ent("sensor.other_entry", "other_olt_cpu_usage", config_entry_id="other"),
        ent("sensor.other_platform", "entry_olt_cpu_usage", platform="demo"),
        ent("sensor.slave_cpu", "entry_legacy_olt_198.51.100.2_cpu_usage"),
    ]
    remove = reg.legacy_only_entities_to_remove

    expected = ["sensor.ap_src", "sensor.olt_cpu", "sensor.olt_mem"]
    # WAN: port 80 never answered and its breaker tripped.
    assert (
        remove(
            entries,
            entry_id=ENTRY,
            hardware_status_enabled=True,
            legacy_ever_succeeded=False,
            legacy_confirmed_down=True,
            wan_deployment_confirmed=True,
        )
        == expected
    )
    # Same, but the WAN deployment is not confirmed: keep (stage 3 S5).
    assert (
        remove(
            entries,
            entry_id=ENTRY,
            hardware_status_enabled=True,
            legacy_ever_succeeded=False,
            legacy_confirmed_down=True,
            wan_deployment_confirmed=False,
        )
        == []
    )
    # Hardware status turned off: the entities are never created.
    assert (
        remove(
            entries,
            entry_id=ENTRY,
            hardware_status_enabled=False,
            legacy_ever_succeeded=True,
            legacy_confirmed_down=False,
            wan_deployment_confirmed=False,
        )
        == expected
    )
    # Port 80 worked once, or is not confirmed down yet: keep everything.
    for ever_ok, down in ((True, True), (False, False)):
        assert (
            remove(
                entries,
                entry_id=ENTRY,
                hardware_status_enabled=True,
                legacy_ever_succeeded=ever_ok,
                legacy_confirmed_down=down,
                wan_deployment_confirmed=True,
            )
            == []
        )


def test_rltech_data_indexes_aps_by_sn() -> None:
    data = models.RltechData(
        aps={
            "A": models.RltechAp(mac="A", sn="SN-A"),
            "B": models.RltechAp(mac="B"),
        }
    )
    assert list(data.aps_by_sn) == ["SN-A"]
    assert data.aps_by_sn["SN-A"].mac == "A"


def test_down_lanpon_port_reports_no_rx_power() -> None:
    lanpon = api.parse_lanpon_ports(fixture("real_sta_user.html"))
    # Real AC: LANPON1 down reports rx_power "0.00"; that is not 0 dBm.
    assert lanpon[1].status == "down"
    assert lanpon[1].rx_power is None
    # Module readings stay: TX, temperature, voltage, bias current.
    assert lanpon[1].tx_power == 2.92
    assert lanpon[1].temperature == 49.61
    assert lanpon[1].voltage == 3.28
    assert lanpon[1].current == 39.31
    assert lanpon[2].rx_power == -15.52

    legacy = api.parse_legacy_lanpon_ports(
        "showLANPonInfo('LANPON1','disable','enable','enable','down','2.73 dBm',"
        "'0.00','49.83 ℃','3.08 mA','38.56 V');"
    )
    assert legacy[1].rx_power is None
    assert legacy[1].tx_power == 2.73


def test_master_ports_ignore_stale_master_when_only_a_slave_answers() -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080",
            "u",
            "p",
            legacy_base_urls=["http://olt", "http://slave"],
        )

        async def source(_session, base_url, **_kwargs):
            if "slave" in base_url:
                return models.RltechOltStatus(cpu_usage=6), {}, {}, {}
            raise OSError(113, "No route to host")

        client._fetch_legacy_source = source
        previous = models.RltechData(
            legacy_sources={
                "olt": models.RltechLegacyOltSource(
                    host="olt",
                    base_url="http://olt",
                    lan_ports={1: models.RltechLanPort(port=1, connected=False)},
                )
            },
            web_status_update=datetime(2020, 1, 1, tzinfo=UTC),
        )
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, fixture("real_ap_online_list.html")),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(200, fixture("real_sta_device.html")),
                FakeResponse(
                    200,
                    fixture("real_sta_user.html").replace(
                        '{"Port":"1", "LanState":"0"', '{"Port":"1", "LanState":"1"'
                    ),
                ),
                FakeResponse(200, ""),
            ]
        )

        data = await client.fetch_snapshot(
            session, previous=previous, detail_interval=0, status_interval=300
        )

        assert data.legacy_sources["slave"].olt_status.cpu_usage == 6
        # The master's stale port-80 fallback (LAN-1 down) must not win.
        assert data.lan_ports[1].connected is True

    asyncio.run(run())


def test_endpoint_candidates_and_choice() -> None:
    assert sources.mqtt_host_candidates("", "192.0.2.1", "198.51.100.29") == [
        "192.0.2.1",
        "198.51.100.29",
    ]
    assert sources.mqtt_host_candidates("", None, "198.51.100.29") == ["198.51.100.29"]
    assert sources.mqtt_host_candidates("192.0.2.9", "192.0.2.1", "h") == [
        "192.0.2.9"
    ]
    assert sources.legacy_master_candidates("198.51.100.29", "192.0.2.1") == [
        "198.51.100.29",
        "192.0.2.1",
    ]
    assert sources.legacy_master_candidates("192.0.2.1", "192.0.2.1") == [
        "192.0.2.1"
    ]
    reachable = {("192.0.2.1", 8883): True, ("198.51.100.29", 8883): False}
    assert (
        sources.choose_endpoint(["198.51.100.29", "192.0.2.1"], reachable, 8883)
        == "192.0.2.1"
    )
    assert sources.choose_endpoint(["198.51.100.29"], reachable, 8883) is None


def test_mqtt_manager_uses_the_injected_task_factory() -> None:
    created = []

    def factory(coro, name):
        created.append(name)
        coro.close()
        return None

    manager = mqtt.RltechMqttManager(
        host="192.0.2.1",
        port=8883,
        username="admin",
        password="x",
        psk_identity="admin",
        psk_hex="00",
        client_id="c",
        apply_update=lambda *args: None,
        stats=mqtt.RltechMqttStats(),
        task_factory=factory,
    )
    manager.start()
    assert created == ["rltech-fttr-mqtt-192.0.2.1"]


def test_remaining_pause() -> None:
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    assert sources.remaining_pause(None, now) is None
    assert sources.remaining_pause(now - timedelta(seconds=1), now) is None
    assert sources.remaining_pause(now, now) is None
    assert sources.remaining_pause(now + timedelta(minutes=5), now) == timedelta(
        minutes=5
    )


def test_missing_timer_does_not_count_on_placeholder_data() -> None:
    # A restored pause serves an empty placeholder (list_fresh is False).
    assert ap_device_registry.update_missing_since(
        {},
        known_sns={"A", "B"},
        listed_sns=set(),
        now=datetime(2026, 9, 30, tzinfo=UTC),
        list_fresh=False,
        ac_last_boot=None,
    ) == {}


def test_wan_deployment_confirmation() -> None:
    confirmed = ap_device_registry.wan_deployment_confirmed
    closed = {"198.51.100.29:80": False, "192.0.2.1:80": False}
    # WAN confirmed: LAN IP known, differs, port 80 closed on both.
    assert confirmed(host="198.51.100.29", lan_ip="192.0.2.1", reachable=closed)
    # LAN deployment: host is the LAN IP.
    assert not confirmed(host="192.0.2.1", lan_ip="192.0.2.1", reachable=closed)
    # LAN IP not read yet.
    assert not confirmed(host="198.51.100.29", lan_ip=None, reachable=closed)
    # Port 80 answers on the LAN IP.
    assert not confirmed(
        host="198.51.100.29",
        lan_ip="192.0.2.1",
        reachable={**closed, "192.0.2.1:80": True},
    )
    # No probe yet, or the probe did not cover both.
    assert not confirmed(host="198.51.100.29", lan_ip="192.0.2.1", reachable=None)
    assert not confirmed(
        host="198.51.100.29", lan_ip="192.0.2.1", reachable={"198.51.100.29:80": False}
    )


def test_should_warn_once_per_step_and_error_type() -> None:
    seen: set[tuple[str, str]] = set()
    warn = ap_device_registry.should_warn
    assert warn("aps", "KeyError", seen)
    assert not warn("aps", "KeyError", seen)
    assert warn("aps", "ValueError", seen)
    assert warn("controller", "KeyError", seen)


def test_ap_detail_due_force_all_after_a_pause() -> None:
    client = api.RltechClient("http://example.invalid", "u", "p")
    now = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    aps = {
        f"02:00:5F:B8:E0:{index:02X}": models.RltechAp(
            mac=f"02:00:5F:B8:E0:{index:02X}", sn=f"SN{index:02d}"
        )
        for index in range(12)
    }
    aps["02:00:5F:B8:E0:FF"] = models.RltechAp(mac="02:00:5F:B8:E0:FF")
    normal = client._ap_detail_due(
        aps, None, now=now, scan_interval=60, detail_interval=300
    )
    assert len(normal) == 3
    forced = client._ap_detail_due(
        aps, None, now=now, scan_interval=60, detail_interval=300, force_all=True
    )
    assert [ap.sn for ap in forced] == [f"SN{index:02d}" for index in range(12)]


# --- Stage 6b: AC reboot through mag-reset.asp -----------------------------

# Fields of the other mag-reset.asp forms: factory reset (defaultflag /
# restoreflag2, defaultflag2 / restoreflag22), USB backup, SN, Restartflag,
# timed reboot, and the login_init reset of Account_Entry0.
AC_RESET_RED_LINE_FIELDS = {
    "defaultflag",
    "defaultflag2",
    "restoreflag2",
    "restoreflag22",
    "backupflg",
    "setsnflag",
    "Restartflag",
    "timereboot",
    "login_init",
}
MAG_RESET_PAGE = "<html><form name='ResetForm'></form></html>"


def _assert_plain_reboot_body(fields) -> None:
    names = [name for name, _ in fields]
    assert names == ["rebootflag", "restoreFlag", "isCUCSupport"]
    assert dict(fields) == {"rebootflag": "1", "restoreFlag": "1", "isCUCSupport": "0"}
    for name in names:
        assert name not in AC_RESET_RED_LINE_FIELDS
        assert "default" not in name.lower()
        assert not name.lower().startswith("restoreflag2")


def _ac_posts(session) -> list:
    return [call for call in session.calls if "mag-reset.asp" in call[1]]


def test_ac_reboot_body_is_a_whitelist_that_rejects_everything_else() -> None:
    fields = api.ac_reboot_fields()
    _assert_plain_reboot_body(fields)
    assert api.AC_REBOOT_ALLOWED_FIELDS == {"rebootflag", "restoreFlag", "isCUCSupport"}
    bad_bodies = [
        fields + [(name, "1")] for name in sorted(AC_RESET_RED_LINE_FIELDS)
    ] + [
        [("rebootflag", "1"), ("restoreFlag", "2"), ("isCUCSupport", "0")],
        [("rebootflag", "1"), ("restoreFlag", "4"), ("isCUCSupport", "0")],
        [("rebootflag", "0"), ("restoreFlag", "1"), ("isCUCSupport", "0")],
        [("rebootflag", "1"), ("restoreFlag", "1")],
        fields + [("restoreFlag", "1")],
        [("rebootflag", "1"), ("restoreflag2", "1"), ("isCUCSupport", "0")],
        [],
    ]
    for body in bad_bodies:
        try:
            api.check_ac_reboot_fields(body)
        except AssertionError:
            continue
        raise AssertionError(f"accepted {body}")


def test_reboot_ac_posts_the_whitelist_once_and_logs_out() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, MAG_RESET_PAGE),
                FakeResponse(200, ""),
            ]
        )
        seen: list[list[str]] = []

        record = await client.reboot_ac(
            session, on_submitted=lambda: seen.append(_call_kinds(session))
        )

        assert _call_kinds(session) == ["check_auth.json", "mag-reset.asp", "logout.cgi"]
        # The window opens inside the lock, before the logout.
        assert seen == [["check_auth.json", "mag-reset.asp"]]
        (_method, url, kwargs), = _ac_posts(session)
        assert _method == "POST"
        assert url == "http://olt:8080/cgi-bin/mag-reset.asp"
        _assert_plain_reboot_body(kwargs["data"])
        assert kwargs["headers"]["Cookie"] == (
            "ecntToken=tok; EBOOVALUE=" + api.eboo_value(kwargs["data"])
        )
        assert kwargs["allow_redirects"] is False
        assert record.result == "submitted"
        assert client.last_ac_reboot is record
        assert client.token is None

    asyncio.run(run())


def test_reboot_ac_timeout_is_submitted_unconfirmed_and_never_retried() -> None:
    async def run(post_response, logout_response) -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, LOGIN_OK), post_response, logout_response])
        opened: list[bool] = []
        record = await client.reboot_ac(session, on_submitted=lambda: opened.append(True))
        assert record.result == "submitted_unconfirmed"
        assert opened == [True]
        # One POST only, then the (short) logout; no second attempt.
        assert _call_kinds(session) == ["check_auth.json", "mag-reset.asp", "logout.cgi"]
        _assert_plain_reboot_body(_ac_posts(session)[0][2]["data"])
        if api.aiohttp is not None:
            assert _ac_posts(session)[0][2]["timeout"].total == 8
            assert session.calls[-1][2]["timeout"].total == api.AC_REBOOT_LOGOUT_TIMEOUT
        assert client.token is None

    asyncio.run(run(RaisingResponse(TimeoutError()), FakeResponse(200, "")))
    # The AC drops the connection and the logout fails too: still no raise.
    asyncio.run(
        run(
            RaisingResponse(ConnectionResetError()),
            RaisingResponse(ConnectionRefusedError()),
        )
    )


def test_reboot_ac_failure_before_sending_raises_without_a_window() -> None:
    async def run(responses, expected_error, expected_calls, result) -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(responses)
        opened: list[bool] = []
        try:
            await client.reboot_ac(session, on_submitted=lambda: opened.append(True))
        except expected_error:
            pass
        else:
            raise AssertionError("expected an error")
        assert _call_kinds(session) == expected_calls
        assert opened == []
        assert client.last_ac_reboot.result == result
        for _method, _url, kwargs in _ac_posts(session):
            _assert_plain_reboot_body(kwargs["data"])

    # Busy login: no token, no POST, no logout.
    asyncio.run(
        run(
            [FakeResponse(200, json.dumps({"Logged": "1"}))],
            api.AccountBusyError,
            ["check_auth.json"],
            "busy",
        )
    )
    # Connection refused / connect timeout on the POST: nothing was sent
    # (review N2), so it is an error the user sees and no window opens.
    not_sent = [ConnectionRefusedError()]
    if api.aiohttp is not None:
        not_sent.append(api.aiohttp.ConnectionTimeoutError())
    for error in not_sent:
        asyncio.run(
            run(
                [FakeResponse(200, LOGIN_OK), RaisingResponse(error), FakeResponse(200, "")],
                OSError if isinstance(error, OSError) else Exception,
                ["check_auth.json", "mag-reset.asp", "logout.cgi"],
                "unreachable",
            )
        )


def test_reboot_ac_unclean_answers_are_submitted_unconfirmed() -> None:
    login_page = (
        "<html><script>$.post('/cgi-bin/check_auth.json', {username: u})"
        "</script></html>"
    )

    async def run(answer, problem: str, status: int) -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession([FakeResponse(200, LOGIN_OK), answer, FakeResponse(200, "")])
        opened: list[bool] = []
        record = await client.reboot_ac(session, on_submitted=lambda: opened.append(True))
        # The body was sent and mag-reset.asp commits before it renders:
        # open the window, never retry (review S1).
        assert record.result == "submitted_unconfirmed"
        assert record.error == problem
        assert record.http_status == status
        assert opened == [True]
        assert _call_kinds(session) == ["check_auth.json", "mag-reset.asp", "logout.cgi"]
        assert client.last_ac_reboot is record

    asyncio.run(run(FakeResponse(200, login_page), "login_page", 200))
    asyncio.run(
        run(FakeResponse(302, "", {"Location": "/cgi-bin/login.asp"}), "redirect_302", 302)
    )
    asyncio.run(run(FakeResponse(500, "oops"), "http_500", 500))


def test_reboot_ac_keeps_a_sanitised_answer_head() -> None:
    page = (
        "<html><head><title>Device reboot</title></head><body>"
        "<input type='hidden' name='name0' value=\"useradmin\">"
        "<script>var mac='E0:21:FE:C0:C0:00'; var ip='192.0.2.1';"
        "var sn='RLFAKE092600019'; var tok='a1b2c3d4e5f6a7b8';</script>"
        + "x" * 600
        + "</body></html>"
    )

    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        session = FakeSession(
            [FakeResponse(200, LOGIN_OK), FakeResponse(200, page), FakeResponse(200, "")]
        )
        record = await client.reboot_ac(session)
        assert record.result == "submitted"
        assert record.http_status == 200
        head = record.response_head
        assert head.startswith("<html><head><title>Device reboot</title>")
        assert len(head.encode()) <= api.AC_REBOOT_RESPONSE_HEAD_BYTES
        for secret in ("useradmin", "E0:21:FE", "192.0.2.1", "RLFAKE092600019", "a1b2c3d4e5f6a7b8"):
            assert secret not in head, secret

    asyncio.run(run())
    assert api.sanitize_response_head(None) is None
    # Multi-byte text is cut on a character boundary.
    assert api.sanitize_response_head("设" * 200).endswith("设")


def test_sanitize_response_head_redacts_before_the_cut() -> None:
    # An identifier split by the 300-byte cut must not leave a prefix (N-R2-1).
    for secret in ("RLFAKE092600019", "192.0.2.1", "E0:21:FE:C0:C0:00"):
        pad = "x " * ((api.AC_REBOOT_RESPONSE_HEAD_BYTES - 5) // 2)
        head = api.sanitize_response_head(pad + secret + " tail")
        assert secret[:4] not in head[len(pad):], secret
    text = (
        "token_1a2b3c4d5e6f7a8b ip=fe80::1c2d:3e4f:5a6b:7c8d "
        "mac=e021.fec0.c000 tok=abcdefghijklmnopqrst end"
    )
    head = api.sanitize_response_head(text)
    for secret in ("1a2b3c4d5e6f7a8b", "fe80::1c2d", "e021.fec0", "abcdefghijklmnopqrst"):
        assert secret not in head, secret
    assert head.endswith(" end")


def test_reboot_ac_waits_for_a_running_poll_burst() -> None:
    class GatedResponse(FakeResponse):
        def __init__(self, body: str, gate: asyncio.Event) -> None:
            super().__init__(200, body)
            self.gate = gate

        async def __aenter__(self):
            await self.gate.wait()
            return self

    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        gate = asyncio.Event()
        session = FakeSession(
            [
                FakeResponse(200, LOGIN_OK),
                GatedResponse(fixture("real_ap_online_list.html"), gate),
                FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
                FakeResponse(200, ""),
                FakeResponse(200, LOGIN_OK),
                FakeResponse(200, MAG_RESET_PAGE),
                FakeResponse(200, ""),
            ]
        )
        poll = asyncio.create_task(
            client.fetch_snapshot(
                session,
                include_hardware_status=False,
                detail_interval=0,
                status_interval=0,
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        reboot = asyncio.create_task(client.reboot_ac(session))
        for _ in range(5):
            await asyncio.sleep(0)
        assert _call_kinds(session) == ["check_auth.json", "ap_online_list.asp"]
        gate.set()
        await poll
        record = await reboot
        assert _call_kinds(session) == [
            "check_auth.json",
            "ap_online_list.asp",
            "ap_wlan_ac_client_list.asp",
            "logout.cgi",
            "check_auth.json",
            "mag-reset.asp",
            "logout.cgi",
        ]
        assert record.result == "submitted"

    asyncio.run(run())


def test_no_code_path_posts_a_factory_reset_field() -> None:
    source = (PKG_ROOT / "api.py").read_text(encoding="utf-8")
    # mag-reset.asp is only ever posted by _post_ac_reboot, and the only
    # red-line names in the code are in comments.
    assert source.count("cgi-bin/mag-reset.asp") == 1
    code_lines = [
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    ]
    for name in AC_RESET_RED_LINE_FIELDS - {"login_init"}:
        assert not any(f'"{name}"' in line for line in code_lines), name


# --- Stage 6b: unified PON/LANPON naming and the LAN-PON port count --------


def test_renamed_sensor_entity_ids() -> None:
    entry, host = "01JENTRY", "198.51.100.29"
    obj = "rltech_olt_198_51_100_29"
    rename = identifiers.renamed_sensor_entity_id
    for key in ("tx_power", "rx_power", "temperature", "voltage", "bias_current"):
        assert rename(entry, host, f"{entry}_uplink_pon_{key}") == (
            f"sensor.{obj}_uplink_pon_{key}",
            f"sensor.{obj}_pon_{key}",
        )
    assert rename(entry, host, f"{entry}_lanpon_port_2_current") == (
        f"sensor.{obj}_lanpon2_current",
        f"sensor.{obj}_lanpon2_bias_current",
    )
    assert rename(entry, host, f"{entry}_legacy_olt_198.18.11.2_lanpon_port_1_current") == (
        "sensor.rltech_olt_198_18_11_2_lanpon1_current",
        "sensor.rltech_olt_198_18_11_2_lanpon1_bias_current",
    )
    for unique_id in (
        f"{entry}_uplink_pon_link_type",
        f"{entry}_uplink_pon_online_status",
        f"{entry}_uplink_pon_link_state",
        f"{entry}_lanpon_port_1_tx_power",
        f"{entry}_lanpon_port_1_link",
        f"{entry}_lanpon_port_1_current_2",
        f"OTHER_uplink_pon_tx_power",
        f"{entry}_olt_cpu_temperature",
        None,
    ):
        assert rename(entry, host, unique_id) is None, unique_id


def test_lanpon_link_unique_id_is_not_retired_and_ports_parse() -> None:
    entry = "01JENTRY"
    for n in (1, 2, 3):
        assert not identifiers.is_retired_sensor_unique_id(
            entry, f"{entry}_lanpon_port_{n}_link"
        )
        assert identifiers.master_lanpon_port(entry, f"{entry}_lanpon_port_{n}_link") == n
    port = identifiers.master_lanpon_port
    assert port(entry, f"{entry}_lanpon_port_2_current") == 2
    assert port(entry, f"{entry}_lanpon_port_2_status") is None  # retired
    assert port(entry, f"{entry}_legacy_olt_h_lanpon_port_2_tx_power") is None
    assert port("OTHER", f"{entry}_lanpon_port_2_tx_power") is None


def one_port_sta_user() -> str:
    """real_sta_user.html as an RL8001GR would render it (LANPON1 only)."""
    page = fixture("real_sta_user.html")
    start = page.index(', { "ponid": 2')
    end = page.index("} ] }", start)
    return page[:start] + page[end + 1 :]


def test_one_port_ponport_info_parses_one_port() -> None:
    ports = api.parse_lanpon_ports(one_port_sta_user())
    assert list(ports) == [1]
    assert ports[1].status == "down"
    both = api.parse_lanpon_ports(fixture("real_sta_user.html"))
    assert [p.status for p in both.values()] == ["down", "up"]


def test_port80_never_adds_a_lanpon_port_the_ac_does_not_report() -> None:
    async def run(web_ports) -> dict:
        previous = models.RltechData(
            aps={},
            last_success=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
            last_success_8080=datetime(2026, 8, 30, 11, 1, tzinfo=UTC),
            web_lanpon_ports=web_ports,
            web_status_update=datetime.now(UTC),
        )
        client = api.RltechClient(
            "http://olt:8080",
            "u",
            "p",
            legacy_base_urls=["http://olt"],
            legacy_username="admin",
            legacy_password="admin",
        )
        two_rows = (
            "showLANPonInfo('LANPON1','disable','enable','enable','up','2.73 dBm','-20.48','49.83 ℃','3.08 mA','38.56 V');"
            "showLANPonInfo('LANPON2','disable','enable','enable','down','2.70 dBm','0.00','48.00 ℃','3.00 mA','38.00 V');"
        )
        session = FakeSession(
            [
                FakeResponse(200, json.dumps({"Logged": "1"})),
                FakeResponse(200, ""),
                FakeResponse(
                    200,
                    "X('RH8001GR','V5.0.1-51675','','','','','','02:00:5F:B9:D3:F0','19 Days 18 Hour 44 Min 43 Sec','8','20','RLFAKE092600019');",
                ),
                FakeResponse(200, "showPortInfo('LAN-1','1','Full','1000M','1','2');"),
                FakeResponse(200, two_rows),
                FakeResponse(200, ""),
                FakeResponse(200, ""),
            ]
        )
        data = await client.fetch_snapshot(session, previous=previous)
        return data.lanpon_ports

    one = {1: models.RltechLanPonPort(ponid=1, status="down")}
    assert list(asyncio.run(run(one))) == [1]
    # Before the AC list is known, port 80 decides (cleanup follows later).
    assert sorted(asyncio.run(run({}))) == [1, 2]


def test_ac_reboot_diagnostics_carry_the_answer_head() -> None:
    from types import SimpleNamespace

    diagnostics = load_module("diagnostics")
    record = models.RltechAcRebootRecord(
        requested_at=datetime(2026, 9, 30, 8, 0, tzinfo=UTC),
        result="submitted_unconfirmed",
        error="redirect_302",
        http_status=302,
        response_head="<html>***</html>",
    )
    coordinator = SimpleNamespace(
        client=SimpleNamespace(last_ac_reboot=record),
        expected_offline_until=None,
        ac_reboot_observed=True,
    )
    summary = diagnostics._ac_reboot_summary(coordinator)
    assert summary == {
        "requested_at": "2026-09-30T08:00:00+00:00",
        "result": "submitted_unconfirmed",
        "error": "redirect_302",
        "http_status": 302,
        "response_head": "<html>***</html>",
        "expected_offline_until": None,
        "reboot_observed": True,
    }
    # The redaction pass of the diagnostics download leaves it readable.
    assert diagnostics._redact({"ac_reboot": summary})["ac_reboot"] == summary


def test_system_uptime_is_unknown_rather_than_zero() -> None:
    page = fixture("real_sta_device.html")
    # No curTime and no uptime text: unknown.
    assert api.parse_olt_status("<html>nothing</html>").uptime_seconds is None
    # curTime 0 or garbage falls back to the uptime text of the page.
    zero = page.replace("var curTime = '1211442';", "var curTime = '0';")
    status = api.parse_olt_status(zero)
    assert status.uptime_seconds == 14 * 86400 + 30 * 60 + 42
    # Neither usable: never 0.
    for bad in ("'0'", "'N/A'"):
        text = "<script>function wanUpTime(){ var curTime = " + bad + "; }</script>"
        assert api.parse_olt_status(text).uptime_seconds is None, bad



def test_status_without_readings_keeps_only_the_identity() -> None:
    status = api.parse_olt_status(fixture("real_sta_device.html"))
    status = replace(status, lan_ip="192.0.2.1", lan_mac="E0:21:FE:C0:C0:00")
    assert status.uptime_seconds and status.cpu_temperature and status.last_boot
    cleared = api.status_without_readings(status)
    assert cleared.uptime_seconds is None
    assert cleared.cpu_temperature is None
    assert cleared.last_boot is None
    assert cleared.system_uptime is None
    assert cleared.serial_number == status.serial_number
    assert cleared.software_version == status.software_version
    assert (cleared.lan_ip, cleared.lan_mac) == ("192.0.2.1", "E0:21:FE:C0:C0:00")
    assert api.status_without_readings(cleared) == cleared
    assert api.status_without_readings(None) is None


def test_age_web_status_after_two_status_intervals() -> None:
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    status = models.RltechOltStatus(cpu_temperature=52, serial_number="SN")
    age = api._age_web_status
    assert age(status, now - timedelta(seconds=600), now=now, status_interval=300) == status
    aged = age(status, now - timedelta(seconds=601), now=now, status_interval=300)
    assert aged == models.RltechOltStatus(serial_number="SN")
    # No timestamp (older snapshot) or ageing disabled: kept.
    assert age(status, None, now=now, status_interval=300) == status
    assert age(status, now - timedelta(days=1), now=now, status_interval=0) == status


def _status_session(*status_pages: FakeResponse) -> FakeSession:
    return FakeSession(
        [
            FakeResponse(200, LOGIN_OK),
            FakeResponse(200, fixture("real_ap_online_list.html")),
            FakeResponse(200, fixture("real_ap_wlan_ac_client_list.html")),
            *status_pages,
            FakeResponse(200, ""),
        ]
    )


def _good_status_pages() -> list[FakeResponse]:
    return [
        FakeResponse(200, fixture("real_sta_device.html")),
        FakeResponse(200, fixture("real_sta_user.html")),
        FakeResponse(200, fixture("sta_network_link_down.html")),
    ]


def _failing_status_pages() -> list[FakeResponse]:
    return [FakeResponse(500, ""), FakeResponse(500, ""), FakeResponse(500, "")]


def test_fetch_snapshot_drops_status_readings_not_refreshed_for_600s() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        first = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            include_hardware_status=False,
            detail_interval=0,
        )
        assert first.web_status_parsed == first.web_status_update
        assert first.olt_status.uptime_seconds == 1211442
        assert first.olt_status.cpu_temperature is not None

        # The status pages are due but every read fails.
        due = replace(
            first,
            web_status_update=first.web_status_update - timedelta(seconds=301),
            web_status_parsed=first.web_status_parsed - timedelta(seconds=301),
        )
        kept = await client.fetch_snapshot(
            _status_session(*_failing_status_pages()),
            previous=due,
            include_hardware_status=False,
            detail_interval=0,
        )
        # Attempted (not due again for 5 minutes) but nothing parsed.
        assert kept.web_status_update > due.web_status_update
        assert kept.web_status_parsed == due.web_status_parsed
        assert kept.olt_status.uptime_seconds == 1211442

        # Two status intervals without a good read: readings unknown.
        stale = replace(
            kept,
            web_status_update=kept.web_status_update - timedelta(seconds=301),
            web_status_parsed=kept.web_status_parsed - timedelta(seconds=300),
        )
        dropped = await client.fetch_snapshot(
            _status_session(*_failing_status_pages()),
            previous=stale,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert dropped.olt_status.uptime_seconds is None
        assert dropped.olt_status.cpu_temperature is None
        assert dropped.olt_status.last_boot is None
        assert dropped.olt_status.serial_number == "RL0000000000003"
        assert dropped.olt_status.lan_mac == "E0:21:FE:C0:C0:00"

        # The next good read brings them back.
        stale = replace(
            dropped,
            web_status_update=dropped.web_status_update - timedelta(seconds=301),
        )
        back = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            previous=stale,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert back.olt_status.uptime_seconds == 1211442

    asyncio.run(run())


def test_status_readings_are_dropped_and_reread_after_an_ac_reboot() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        first = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            include_hardware_status=False,
            detail_interval=0,
        )
        read_at = first.web_status_update - timedelta(seconds=60)
        first = replace(first, web_status_update=read_at, web_status_parsed=read_at)
        # Not due for 5 minutes, but an AC reboot was sent after the read.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=read_at + timedelta(seconds=10),
            result="submitted",
        )
        after = await client.fetch_snapshot(
            _status_session(*_failing_status_pages()),
            previous=first,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert after.olt_status.uptime_seconds is None
        assert after.olt_status.last_boot is None
        assert after.olt_status.serial_number == "RL0000000000003"
        # Read (attempted) after the request: not forced again.
        assert after.web_status_update > client.last_ac_reboot.requested_at
        again = await client.fetch_snapshot(
            _status_session(),
            previous=after,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert again.olt_status.uptime_seconds is None

        # A reboot that was not sent (busy) changes nothing.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=read_at + timedelta(seconds=10),
            result="busy",
        )
        untouched = await client.fetch_snapshot(
            _status_session(),
            previous=first,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert untouched.olt_status.uptime_seconds == 1211442

    asyncio.run(run())


def test_clear_status_readings_makes_the_status_pages_due() -> None:
    data = models.RltechData(
        web_status=models.RltechOltStatus(uptime_seconds=5, lan_ip="192.0.2.1"),
        olt_status=models.RltechOltStatus(uptime_seconds=5, cpu_usage=3, lan_ip="192.0.2.1"),
        web_status_update=datetime(2026, 9, 30, tzinfo=UTC),
    )
    cleared = api.clear_status_readings(data)
    assert cleared.web_status == models.RltechOltStatus(lan_ip="192.0.2.1")
    assert cleared.olt_status == models.RltechOltStatus(lan_ip="192.0.2.1")
    assert cleared.web_status_update is None


def test_port80_fallback_values_age_and_are_cleared_by_an_ac_reboot() -> None:
    async def run() -> None:
        client = api.RltechClient(
            "http://olt:8080", "u", "p", legacy_base_urls=["http://olt"]
        )

        async def failing_source(*_args, **_kwargs):
            raise OSError(113, "No route to host")

        client._fetch_legacy_source = failing_source
        now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        booted = now - timedelta(days=3)

        def previous(last_success: datetime) -> models.RltechData:
            return models.RltechData(
                legacy_sources={
                    "olt": models.RltechLegacyOltSource(
                        host="olt",
                        base_url="http://olt",
                        olt_status=models.RltechOltStatus(
                            cpu_usage=8,
                            memory_usage=40,
                            last_boot=booted,
                            serial_number="SN",
                        ),
                        last_success=last_success,
                    )
                },
            )

        async def fallback(last_success: datetime):
            status, _lan, _lanpon, _details, sources_ = (
                await client._fetch_legacy_snapshot(
                    object(), {}, previous(last_success), now=now, status_max_age=600
                )
            )
            return status, sources_["olt"].olt_status

        # Within 2 status intervals: the last port-80 values are used.
        status, kept = await fallback(now - timedelta(seconds=600))
        assert status.cpu_usage == 8 and status.last_boot == booted
        assert kept == status
        # Older: readings unknown, identity kept, also in the stored source.
        status, kept = await fallback(now - timedelta(seconds=601))
        assert status == models.RltechOltStatus(serial_number="SN")
        assert kept == status

        # A sent AC reboot after the last port-80 read clears them at once.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=now - timedelta(seconds=30), result="submitted"
        )
        status, _ = await fallback(now - timedelta(seconds=60))
        assert status.last_boot is None and status.cpu_usage is None
        # ...and so does a read after the request that still shows the old
        # boot time (the AC had not gone down yet).
        status, _ = await fallback(now - timedelta(seconds=10))
        assert status.last_boot is None and status.memory_usage is None
        # A reboot that was not sent changes nothing.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=now - timedelta(seconds=30), result="busy"
        )
        status, _ = await fallback(now - timedelta(seconds=60))
        assert status.cpu_usage == 8

    asyncio.run(run())


def test_clear_status_readings_also_clears_port80_sources() -> None:
    data = models.RltechData(
        legacy_sources={
            "olt": models.RltechLegacyOltSource(
                host="olt",
                base_url="http://olt",
                olt_status=models.RltechOltStatus(cpu_usage=8, serial_number="SN"),
            )
        },
    )
    cleared = api.clear_status_readings(data)
    assert cleared.legacy_sources["olt"].olt_status == models.RltechOltStatus(
        serial_number="SN"
    )


def test_status_read_after_the_reboot_inside_the_window_is_accepted() -> None:
    """Review 7 round 2: only reads from before the AC went down are dropped."""

    def device_page(uptime: int) -> FakeResponse:
        page = fixture("real_sta_device.html")
        assert "var curTime = '1211442';" in page
        return FakeResponse(200, page.replace("var curTime = '1211442';", f"var curTime = '{uptime}';"))

    async def read(client, previous, uptime):
        return await client.fetch_snapshot(
            _status_session(
                device_page(uptime),
                FakeResponse(200, fixture("real_sta_user.html")),
                FakeResponse(200, fixture("sta_network_link_down.html")),
            ),
            previous=previous,
            include_hardware_status=False,
            detail_interval=0,
        )

    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        first = await read(client, None, 1211442)
        cleared = api.clear_status_readings(first)
        # Reboot sent 200 s ago (inside the 300 s window) in every case; the
        # AC reports 120 s of uptime, i.e. it booted 80 s after the request.
        for offset, accepted in (
            (-80, True),   # boot after the request: a real post-reboot read
            (30, True),    # boot 30 s before the request: within the 1-min slack
            (150, False),  # boot 150 s before the request: read before going down
        ):
            now = datetime.now(UTC)
            boot = now - timedelta(seconds=120)
            client.last_ac_reboot = models.RltechAcRebootRecord(
                requested_at=boot + timedelta(seconds=offset),
                result="submitted_unconfirmed",
            )
            assert (now - client.last_ac_reboot.requested_at).total_seconds() < 300
            data = await read(client, cleared, 120)
            if accepted:
                assert data.olt_status.uptime_seconds == 120, offset
                assert data.olt_status.last_boot is not None, offset
                assert data.web_status_update is not None, offset
                assert data.web_status_parsed is not None, offset
            else:
                assert data.olt_status.uptime_seconds is None, offset
                assert data.olt_status.last_boot is None, offset
                assert data.web_status_update is None, offset
            assert data.olt_status.serial_number == "RL0000000000003", offset

    asyncio.run(run())


def test_status_read_before_the_ac_goes_down_is_ignored() -> None:
    async def run() -> None:
        client = api.RltechClient("http://olt:8080", "u", "p")
        first = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            include_hardware_status=False,
            detail_interval=0,
        )
        assert first.olt_status.uptime_seconds == 1211442
        # Reboot sent just now; the AC still answers with its old uptime.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=datetime.now(UTC), result="submitted"
        )
        cleared = api.clear_status_readings(first)
        pre_down = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            previous=cleared,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert pre_down.olt_status.uptime_seconds is None
        assert pre_down.olt_status.last_boot is None
        assert pre_down.olt_status.serial_number == "RL0000000000003"
        # Still due: the next poll reads the status pages again.
        assert pre_down.web_status_update is None
        again = _status_session(*_good_status_pages())
        await client.fetch_snapshot(
            again, previous=pre_down, include_hardware_status=False, detail_interval=0
        )
        assert any("sta-device.asp" in call[1] for call in again.calls)

        # Outside the window (the AC never rebooted) the read counts again.
        client.last_ac_reboot = models.RltechAcRebootRecord(
            requested_at=datetime.now(UTC) - timedelta(seconds=301),
            result="submitted",
        )
        late = await client.fetch_snapshot(
            _status_session(*_good_status_pages()),
            previous=pre_down,
            include_hardware_status=False,
            detail_interval=0,
        )
        assert late.olt_status.uptime_seconds == 1211442
        assert late.web_status_update is not None

    asyncio.run(run())
