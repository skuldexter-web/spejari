"""Shared data models."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional, Tuple


@dataclass(frozen=True)
class AccessPoint:
    """One Wi-Fi access point as seen in a single scan result."""

    bssid: str
    ssid: str
    freq: int
    channel: int
    band: str  # "2.4" or "5"
    rssi: int
    auth: str  # WiGLE-style capability string, e.g. [WPA2-PSK-CCMP][ESS]
    security: str  # short label, e.g. WPA2, WPA3, Open

    @property
    def hidden(self) -> bool:
        return self.ssid == ""


@dataclass
class GpsFix:
    """Snapshot of the GPS state."""

    source: str = "none"
    device: str = ""
    connected: bool = False
    valid: bool = False
    mode: int = 0  # 0/1 = no fix, 2 = 2D, 3 = 3D
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[float] = None
    speed_mps: Optional[float] = None
    track: Optional[float] = None
    hdop: Optional[float] = None
    accuracy_m: Optional[float] = None
    sats_used: int = 0
    sats_seen: int = 0
    distance_m: float = 0.0
    last_update: float = 0.0  # time.monotonic() of the last position report
    status: str = "Searching for GPS..."


@dataclass
class Observation:
    """A single logged sighting (one CSV row)."""

    bssid: str
    ssid: str
    auth: str
    seen_utc: datetime
    channel: int
    rssi: int
    lat: float
    lon: float
    alt: float
    accuracy: float
    kind: str = "WIFI"  # WIFI | BLE  (the WiGLE "Type" column)
    freq: int = 0  # MHz (WiGLE 1.6 "Frequency"); 0 for BLE
    mfgr: str = ""  # BLE manufacturer id, decimal (WiGLE 1.6 "MfgrId")

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "freq": self.freq,
            "mfgr": self.mfgr,
            "bssid": self.bssid,
            "ssid": self.ssid,
            "auth": self.auth,
            "seen": self.seen_utc.astimezone(timezone.utc).isoformat(),
            "ch": self.channel,
            "rssi": self.rssi,
            "lat": self.lat,
            "lon": self.lon,
            "alt": self.alt,
            "acc": self.accuracy,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Observation":
        return cls(
            bssid=d["bssid"],
            ssid=d["ssid"],
            auth=d["auth"],
            seen_utc=datetime.fromisoformat(d["seen"]),
            channel=int(d["ch"]),
            rssi=int(d["rssi"]),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            alt=float(d["alt"]),
            accuracy=float(d["acc"]),
            kind=str(d.get("kind", "WIFI")),
            freq=int(d.get("freq") or 0),
            mfgr=str(d.get("mfgr") or ""),
        )


@dataclass
class FeedEntry:
    ts: float
    kind: str  # NEW | UPD | SYS | WARN
    ssid: str = ""
    bssid: str = ""
    channel: int = 0
    band: str = ""
    rssi: int = 0
    security: str = ""
    adapter: str = ""
    message: str = ""


class FeedLog:
    """Thread-safe ring buffer feeding the live panel."""

    def __init__(self, maxlen: int = 500) -> None:
        self._items: Deque[FeedEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._total = 0  # entries ever added (lets a consumer poll incrementally)

    def add(self, entry: FeedEntry) -> None:
        with self._lock:
            self._items.append(entry)
            self._total += 1

    def system(self, message: str, warn: bool = False) -> None:
        self.add(FeedEntry(ts=time.time(), kind="WARN" if warn else "SYS", message=message))

    def since(self, last_total: int) -> Tuple[List[FeedEntry], int]:
        """Entries added after *last_total* (as returned earlier) and the new total."""
        with self._lock:
            new = self._total - last_total
            items = list(self._items)[-new:] if new > 0 else []
            return items, self._total

    def tail(self, n: int) -> List[FeedEntry]:
        with self._lock:
            items = list(self._items)
        return items[-n:] if n > 0 else []


@dataclass
class AdapterView:
    """Read-only view of a Wi-Fi adapter for the UI."""

    tag: str  # interface name (wlan1)
    name: str = ""  # interface name
    driver: str = ""
    phy: str = ""
    channels: List[int] = field(default_factory=list)  # Linux: assigned frequencies
    current: List[int] = field(default_factory=list)  # Linux: frequencies being scanned right now
    state: str = "starting"
    hits: int = 0

    @property
    def ifname(self) -> str:  # Linux naming kept for readability
        return self.tag


@dataclass
class BleAdapterView:
    name: str  # hci0, hci1, ...
    address: str = ""
    state: str = "starting"
    devices: int = 0
    hits: int = 0


@dataclass
class BleSighting:
    """One advertisement from a Bluetooth LE device."""

    address: str
    name: str
    rssi: int
    random: bool  # True = random private address (changes over time)
    company: str = ""  # manufacturer id, e.g. "0x004C"
    uuids: int = 0


@dataclass
class AircraftView:
    """One ADS-B aircraft for the UI."""

    icao: str
    callsign: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[int] = None
    speed_kt: Optional[float] = None
    track: Optional[float] = None
    squawk: str = ""
    msgs: int = 0
    age_s: float = 0.0
    range_km: Optional[float] = None  # distance from the receiver (needs GPS fix and aircraft position)
    source: str = ""  # which dongle heard it


@dataclass
class AdsbDongleView:
    name: str  # "rtl0", "rtl1", ... or "external"
    state: str = "starting"
    msgs: int = 0
    aircraft: int = 0
