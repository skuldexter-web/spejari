"""Bluetooth Low Energy war-driving.

Uses the ``bleak`` library on top of BlueZ, with up to 5 adapters at once (hci0 ... hci4).

Every advertising device is deduplicated by address and logged with the GPS position, exactly like Wi-Fi
networks, as WiGLE type ``BLE`` rows in the same session CSV.
"""

from __future__ import annotations

from .engine import (
    HARD_MAX_BLE,
    BleEngine,
    BleSource,
    SourceError,
    bluez_running,
    classify_address,
    ensure_bluez,
    list_ble_adapters,
    sighting_from_bleak,
)

__all__ = ["HARD_MAX_BLE", "BleEngine", "BleSource", "SourceError", "bluez_running", "classify_address",
           "ensure_bluez", "list_ble_adapters", "sighting_from_bleak"]
