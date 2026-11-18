"""Aircraft table: merges SBS updates, computes range from the receiver and produces the position log."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..geo import haversine_m
from ..models import AircraftView
from .sbs import SbsUpdate


@dataclass
class Aircraft:
    icao: str
    callsign: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_ft: Optional[int] = None
    speed_kt: Optional[float] = None
    track: Optional[float] = None
    squawk: str = ""
    msgs: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_mono: float = 0.0
    source: str = ""
    max_range_km: float = 0.0
    last_row_mono: float = -1e9
    positions: int = 0


@dataclass
class PositionRow:
    """One line of the ADS-B coverage log."""

    time_utc: datetime
    icao: str
    callsign: str
    lat: float
    lon: float
    alt_ft: Optional[int]
    speed_kt: Optional[float]
    track: Optional[float]
    squawk: str
    rx_lat: Optional[float]
    rx_lon: Optional[float]
    range_km: Optional[float]
    source: str

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["time_utc"] = self.time_utc.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "PositionRow":
        d = dict(d)
        d["time_utc"] = datetime.fromisoformat(d["time_utc"])
        return cls(**d)


class AircraftTable:
    def __init__(self, log_interval_s: float = 5.0, max_age_s: float = 60.0) -> None:
        self.log_interval_s = log_interval_s
        self.max_age_s = max_age_s
        self._ac: Dict[str, Aircraft] = {}
        self._lock = threading.Lock()
        self.total_msgs = 0
        self.max_range_km = 0.0

    def update(
        self,
        upd: SbsUpdate,
        source: str,
        rx: Optional[Tuple[float, float]],
        now_mono: float,
        now_utc: datetime,
    ) -> Tuple[bool, Optional[PositionRow]]:
        """Merge one update. Returns (is_new_aircraft, position row to log or None)."""
        with self._lock:
            ac = self._ac.get(upd.icao)
            is_new = ac is None
            if ac is None:
                ac = Aircraft(icao=upd.icao, first_seen=now_utc)
                self._ac[upd.icao] = ac
            ac.msgs += 1
            self.total_msgs += 1
            ac.last_seen, ac.last_mono, ac.source = now_utc, now_mono, source
            if upd.callsign:
                ac.callsign = upd.callsign
            if upd.alt_ft is not None:
                ac.alt_ft = upd.alt_ft
            if upd.speed_kt is not None:
                ac.speed_kt = upd.speed_kt
            if upd.track is not None:
                ac.track = upd.track
            if upd.squawk:
                ac.squawk = upd.squawk
            row = None
            if upd.lat is not None and upd.lon is not None:
                ac.lat, ac.lon = upd.lat, upd.lon
                ac.positions += 1
                rng = None
                if rx is not None:
                    rng = haversine_m(rx[0], rx[1], upd.lat, upd.lon) / 1000.0
                    if rng > ac.max_range_km:
                        ac.max_range_km = rng
                    if rng > self.max_range_km:
                        self.max_range_km = rng
                if now_mono - ac.last_row_mono >= self.log_interval_s:
                    ac.last_row_mono = now_mono
                    row = PositionRow(
                        time_utc=now_utc, icao=ac.icao, callsign=ac.callsign, lat=upd.lat, lon=upd.lon,
                        alt_ft=ac.alt_ft, speed_kt=ac.speed_kt, track=ac.track, squawk=ac.squawk,
                        rx_lat=rx[0] if rx else None, rx_lon=rx[1] if rx else None,
                        range_km=round(rng, 2) if rng is not None else None, source=source,
                    )
            return is_new, row

    def views(self, now_mono: float, rx: Optional[Tuple[float, float]] = None) -> List[AircraftView]:
        out: List[AircraftView] = []
        with self._lock:
            for ac in self._ac.values():
                age = now_mono - ac.last_mono
                if age > self.max_age_s:
                    continue
                rng = None
                if rx is not None and ac.lat is not None and ac.lon is not None:
                    rng = haversine_m(rx[0], rx[1], ac.lat, ac.lon) / 1000.0
                out.append(
                    AircraftView(
                        icao=ac.icao, callsign=ac.callsign, lat=ac.lat, lon=ac.lon, alt_ft=ac.alt_ft,
                        speed_kt=ac.speed_kt, track=ac.track, squawk=ac.squawk, msgs=ac.msgs, age_s=age,
                        range_km=rng, source=ac.source,
                    )
                )
        out.sort(key=lambda v: (v.range_km is None, v.range_km or 0.0, v.age_s))
        return out

    def total_seen(self) -> int:
        with self._lock:
            return len(self._ac)

    def all_aircraft(self) -> List[Aircraft]:
        with self._lock:
            return list(self._ac.values())
