"""ADS-B exports: coverage CSV, GeoJSON tracks and a crash-safe journal (separate from the WiGLE file)."""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .. import APP_NAME
from ..exporter import _chown_to_sudo_user, normalize_csv_path
from .tracker import PositionRow

ADSB_COLUMNS = [
    "TimeUTC", "ICAO", "Callsign", "Latitude", "Longitude", "AltitudeFt", "SpeedKt", "TrackDeg", "Squawk",
    "ReceiverLatitude", "ReceiverLongitude", "RangeKm", "Dongle",
]
JOURNAL_PREFIX = ".spejari_adsb_"
JOURNAL_SUFFIX = ".journal"


def adsb_filename(started: datetime, ext: str = "csv") -> str:
    """Spejari_ADSB_YYYY-MM-DD_HH-MM-SS.csv (extension always lowercase)"""
    return f"{APP_NAME}_ADSB_{started:%Y-%m-%d_%H-%M-%S}.{ext.lower()}"


def _fmt(v, nd: int) -> str:
    return "" if v is None else f"{v:.{nd}f}"


def write_adsb_csv(rows: Iterable[PositionRow], path: Path) -> int:
    path = normalize_csv_path(path)
    data = sorted(rows, key=lambda r: (r.time_utc, r.icao))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(ADSB_COLUMNS)
        for r in data:
            w.writerow([
                r.time_utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), r.icao,
                r.callsign.replace(",", " "), f"{r.lat:.6f}", f"{r.lon:.6f}",
                "" if r.alt_ft is None else r.alt_ft, _fmt(r.speed_kt, 0), _fmt(r.track, 0), r.squawk,
                _fmt(r.rx_lat, 6), _fmt(r.rx_lon, 6), _fmt(r.range_km, 2), r.source,
            ])
    os.replace(tmp, path)
    _chown_to_sudo_user(path)
    return len(data)


def write_adsb_geojson(rows: Iterable[PositionRow], receiver_track: Iterable[Tuple[datetime, float, float]], path: Path) -> Path:
    """One LineString per aircraft (its track) plus the receiver's own drive. Opens in geojson.io, QGIS, Google Earth."""
    path = Path(path).with_suffix(".geojson")
    by_icao: Dict[str, List[PositionRow]] = {}
    for r in sorted(rows, key=lambda r: r.time_utc):
        by_icao.setdefault(r.icao, []).append(r)
    features = []
    for icao, pts in by_icao.items():
        coords = [[r.lon, r.lat, round((r.alt_ft or 0) * 0.3048)] for r in pts]
        props = {
            "icao": icao,
            "callsign": next((r.callsign for r in reversed(pts) if r.callsign), ""),
            "first_seen": pts[0].time_utc.astimezone(timezone.utc).isoformat(),
            "last_seen": pts[-1].time_utc.astimezone(timezone.utc).isoformat(),
            "max_range_km": max((r.range_km or 0.0) for r in pts),
            "positions": len(pts),
        }
        geom = {"type": "LineString", "coordinates": coords} if len(coords) > 1 else {"type": "Point", "coordinates": coords[0]}
        features.append({"type": "Feature", "properties": props, "geometry": geom})
    rx = [[lon, lat] for _t, lat, lon in receiver_track]
    if len(rx) > 1:
        features.append({"type": "Feature", "properties": {"name": "receiver (Spejari drive)"},
                          "geometry": {"type": "LineString", "coordinates": rx}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".geojson.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh, separators=(",", ":"))
    os.replace(tmp, path)
    _chown_to_sudo_user(path)
    return path


class AdsbJournal:
    """Append-only JSON lines: one position row or one receiver point per line."""

    def __init__(self, directory: Path, started: datetime) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{JOURNAL_PREFIX}{started:%Y-%m-%d_%H-%M-%S}{JOURNAL_SUFFIX}"
        self._fh = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        _chown_to_sudo_user(self.path)

    def _write(self, obj: dict) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.write(json.dumps(obj, separators=(",", ":")) + "\n")
                self._fh.flush()

    def write_row(self, row: PositionRow) -> None:
        self._write({"row": row.to_json()})

    def write_rx(self, when: datetime, lat: float, lon: float) -> None:
        self._write({"rx": [when.isoformat(), lat, lon]})

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def discard(self) -> None:
        self.close()
        try:
            self.path.unlink()
        except OSError:
            pass


def read_adsb_journal(path: Path) -> Tuple[List[PositionRow], List[Tuple[datetime, float, float]]]:
    rows: List[PositionRow] = []
    rx: List[Tuple[datetime, float, float]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if "row" in obj:
                    rows.append(PositionRow.from_json(obj["row"]))
                elif "rx" in obj:
                    t, lat, lon = obj["rx"]
                    rx.append((datetime.fromisoformat(t), float(lat), float(lon)))
            except (ValueError, KeyError, TypeError):
                continue  # torn last line after a power cut
    return rows, rx


def recover_adsb_journals(directory: Path) -> List[Path]:
    results: List[Path] = []
    for jpath in sorted(directory.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}")):
        stamp = jpath.name[len(JOURNAL_PREFIX): -len(JOURNAL_SUFFIX)]
        rows, rx = read_adsb_journal(jpath)
        if not rows:
            jpath.unlink(missing_ok=True)
            continue
        target = directory / f"{APP_NAME}_ADSB_{stamp}.csv"
        if target.exists():
            target = directory / f"{APP_NAME}_ADSB_{stamp}_recovered.csv"
        write_adsb_csv(rows, target)
        write_adsb_geojson(rows, rx, target)
        jpath.unlink(missing_ok=True)
        results.append(target)
    return results
