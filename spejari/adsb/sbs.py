"""Parser for the SBS-1 / BaseStation text feed (port 30003) produced by readsb and every dump1090 flavour."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_HEX = set("0123456789ABCDEF")


@dataclass
class SbsUpdate:
    icao: str
    msg_type: int
    callsign: Optional[str] = None
    alt_ft: Optional[int] = None
    speed_kt: Optional[float] = None
    track: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    vrate: Optional[int] = None
    squawk: Optional[str] = None
    on_ground: Optional[bool] = None


def _f(parts, i) -> Optional[float]:
    try:
        raw = parts[i].strip()
        return float(raw) if raw else None
    except (IndexError, ValueError):
        return None


def _i(parts, i) -> Optional[int]:
    v = _f(parts, i)
    return int(round(v)) if v is not None else None


def parse_sbs(line: str) -> Optional[SbsUpdate]:
    """Parse one ``MSG,...`` line. Returns None for anything that is not a usable aircraft message."""
    line = line.strip()
    if not line.startswith("MSG,"):
        return None
    parts = line.split(",")
    if len(parts) < 5:
        return None
    icao = parts[4].strip().upper()
    if len(icao) != 6 or not set(icao) <= _HEX:
        return None
    try:
        mtype = int(parts[1])
    except ValueError:
        return None
    upd = SbsUpdate(icao=icao, msg_type=mtype)
    callsign = parts[10].strip() if len(parts) > 10 else ""
    upd.callsign = callsign or None
    upd.alt_ft = _i(parts, 11)
    upd.speed_kt = _f(parts, 12)
    upd.track = _f(parts, 13)
    lat, lon = _f(parts, 14), _f(parts, 15)
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
        upd.lat, upd.lon = lat, lon
    upd.vrate = _i(parts, 16)
    squawk = parts[17].strip() if len(parts) > 17 else ""
    upd.squawk = squawk or None
    if len(parts) > 21 and parts[21].strip() in ("0", "-1", "1"):
        upd.on_ground = parts[21].strip() in ("-1", "1")
    return upd
