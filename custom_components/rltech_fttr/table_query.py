"""Table filtering, sorting and paging for the websocket API (pure).

No Home Assistant imports, so the query logic is unit tested directly.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, Callable

_PAGE_SIZE_MAX = 1000


def table_result(
    rows: list[dict[str, Any]],
    *,
    search: str,
    filters: dict[str, Any],
    sort_key: str,
    sort_dir: int,
    page: int,
    page_size: int,
    filter_specs: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]],
    filter_predicates: dict[str, Callable[[dict[str, Any], str], bool]],
    sort_values: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
) -> dict[str, Any]:
    """Return filtered, sorted, paginated table rows and filter options."""
    sort_values = sort_values or {}
    search_text = search.strip().lower()
    active_filters = {
        key: str(value)
        for key, value in filters.items()
        if value is not None and str(value) != ""
    }

    filtered = [
        row
        for row in rows
        if _matches_search(row, search_text)
        and all(
            filter_predicates[key](row, value)
            for key, value in active_filters.items()
            if key in filter_predicates
        )
    ]
    sort_value = sort_values.get(sort_key, lambda item: item.get(sort_key))
    filtered.sort(
        key=cmp_to_key(
            lambda left, right: _compare_sort_values(
                sort_value(left),
                sort_value(right),
                sort_dir,
            )
        )
    )

    page_size = max(0, min(_PAGE_SIZE_MAX, page_size))
    page = max(0, page)
    if page_size:
        page_count = max(1, (len(filtered) + page_size - 1) // page_size)
        page = min(page, page_count - 1)
        start = page * page_size
        paged = filtered[start : start + page_size]
    else:
        page_count = 1
        start = 0
        paged = filtered

    return {
        "rows": paged,
        "total": len(rows),
        "filtered": len(filtered),
        "page": page,
        "page_size": page_size,
        "page_count": page_count,
        "filter_options": _filter_options(rows, filter_specs),
    }


def _matches_search(row: dict[str, Any], search: str) -> bool:
    """Return whether a row matches free-text search."""
    if not search:
        return True
    haystack = " ".join(
        str(value)
        for value in row.values()
        if value is not None and not isinstance(value, dict)
    ).lower()
    return search in haystack


def _filter_options(
    rows: list[dict[str, Any]],
    specs: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]],
) -> dict[str, list[Any]]:
    """Return distinct filter option values."""
    return {
        key: sorted(
            {
                value
                for row in rows
                if (value := value_fn(row)) is not None and value != ""
            },
            key=lambda value: str(value).lower(),
        )
        for key, (_label, value_fn) in specs.items()
    }


def _compare_sort_values(left: Any, right: Any, sort_dir: int) -> int:
    """Compare sort values while keeping empty values last."""
    left_empty = _is_empty_sort_value(left)
    right_empty = _is_empty_sort_value(right)
    if left_empty and right_empty:
        return 0
    if left_empty:
        return 1
    if right_empty:
        return -1

    left_key = _sort_key(left)
    right_key = _sort_key(right)
    if left_key == right_key:
        return 0
    result = -1 if left_key < right_key else 1
    return result * sort_dir


def _is_empty_sort_value(value: Any) -> bool:
    """Return whether a table sort value should always sort last."""
    return value is None or value == ""


def _sort_key(value: Any) -> tuple[int, Any]:
    """Normalize non-empty sort values."""
    if value is None or value == "":
        return (1, "")
    if isinstance(value, bool):
        return (0, int(value))
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value).lower())


def uplink_label(row: dict[str, Any]) -> str:
    """Return AP uplink label for filtering and sorting."""
    uplink = row.get("uplink")
    if uplink is None:
        return ""
    port = "" if row.get("uplink_port") is None else f" {row['uplink_port']}"
    if uplink == 0:
        return f"LAN{port}"
    if uplink == 2:
        return f"LAN-PON{port}"
    return f"Uplink {uplink}{port}"


def _mac_key(value: Any) -> str:
    """Normalize a MAC for comparisons (uppercase, colon separated)."""
    text = "".join(ch for ch in str(value or "") if ch.isalnum()).upper()
    if len(text) != 12:
        return str(value or "").strip().upper()
    return ":".join(text[i : i + 2] for i in range(0, 12, 2))


def ap_filter_options(data: Any) -> list[dict[str, str]]:
    """Return AP filter choices as value/label pairs (plan 3.3.3).

    value = AP MAC (stable), label = alias, else SN, else MAC.
    """
    if data is None:
        return []
    options = [
        {"value": ap.mac, "label": ap.alias or ap.sn or ap.mac}
        for ap in data.aps.values()
    ]
    return sorted(options, key=lambda item: (item["label"].lower(), item["value"]))


def _station_ap_matches(row: dict[str, Any], value: str) -> bool:
    """Old ``ap`` filter (kept one version): alias-or-MAC, or the MAC itself."""
    label = row.get("ap_alias") or row.get("ap_mac") or ""
    return label == value or _mac_key(row.get("ap_mac")) == _mac_key(value)


def station_query(
    rows: list[dict[str, Any]],
    ap_options: list[dict[str, str]],
    *,
    search: str,
    filters: dict[str, Any],
    sort_key: str,
    sort_dir: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Filter/sort/page station rows; ``ap_mac`` is the exact AP filter."""
    result = table_result(
        rows,
        search=search,
        filters=filters,
        sort_key=sort_key,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
        filter_specs={
            "ssid": ("SSID", lambda row: row.get("ssid")),
            "vlan": ("VLAN", lambda row: row.get("vlan")),
            "band": ("Band", lambda row: row.get("band")),
        },
        filter_predicates={
            "ssid": lambda row, value: row.get("ssid") == value,
            "ap_mac": lambda row, value: _mac_key(row.get("ap_mac"))
            == _mac_key(value),
            "ap": _station_ap_matches,
            "vlan": lambda row, value: str(
                row.get("vlan") if row.get("vlan") is not None else ""
            )
            == value,
            "band": lambda row, value: row.get("band") == value,
            "online": lambda row, value: (
                "active" if row.get("reported_online") else "inactive"
            )
            == value,
        },
        sort_values={
            "reported_online": lambda row: "active"
            if row.get("reported_online")
            else "inactive",
        },
    )
    listed = {option["value"] for option in ap_options}
    extra = sorted(
        {
            _mac_key(row.get("ap_mac"))
            for row in rows
            if row.get("ap_mac") and _mac_key(row.get("ap_mac")) not in listed
        }
    )
    result["filter_options"]["ap"] = [
        *ap_options,
        *({"value": mac, "label": mac} for mac in extra),
    ]
    return result
