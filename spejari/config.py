"""Configuration loading for Spejari (TOML file + CLI overrides)."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python < 3.11 needs the "tomli" backport
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "spejari.toml"
HARD_MAX_ADAPTERS = 10
HARD_MAX_BLE_ADAPTERS = 5

# Drive modes: what Spejari war-drives. Any "+"-combination of wifi / ble / adsb works; these are the named ones.
MODES = {
    "wifi": ("wifi",),
    "ble": ("ble",),
    "adsb": ("adsb",),
    "wifi+ble": ("wifi", "ble"),
    "all": ("wifi", "ble", "adsb"),
}
MODE_LABELS = {
    "wifi": "Wi-Fi only",
    "ble": "Bluetooth LE only",
    "adsb": "ADS-B (RTL-SDR) only",
    "wifi+ble": "Wi-Fi + Bluetooth LE",
    "all": "Everything (Wi-Fi + BLE + ADS-B)",
}
_PARTS = ("wifi", "ble", "adsb")
_ALIASES = {"bt": "ble", "bluetooth": "ble", "wi-fi": "wifi", "sdr": "adsb", "rtl-sdr": "adsb", "ads-b": "adsb"}


def parse_mode(text: str) -> str:
    """'ble+wifi', 'wifi,adsb', 'all' ... -> canonical mode string ('wifi+ble', 'wifi+adsb', 'all').

    Raises ValueError for unknown parts."""
    raw = str(text or "").strip().lower()
    if raw in ("all", "everything"):
        return "all"
    parts = set()
    for token in raw.replace(",", "+").replace(" ", "+").split("+"):
        if not token:
            continue
        token = _ALIASES.get(token, token)
        if token not in _PARTS:
            raise ValueError(f"unknown mode '{token}' (use wifi, ble, adsb, all or a combination like wifi+ble)")
        parts.add(token)
    if not parts:
        raise ValueError("empty mode")
    if len(parts) == 3:
        return "all"
    return "+".join(p for p in _PARTS if p in parts)


def mode_parts(mode: str):
    return set(MODES.get(mode) or mode.split("+"))


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode) or " + ".join({"wifi": "Wi-Fi", "ble": "BLE", "adsb": "ADS-B"}[p]
                                               for p in _PARTS if p in mode_parts(mode))


class ConfigError(Exception):
    """Raised for unreadable or invalid configuration files."""


@dataclass
class ScanConfig:
    max_adapters: int = HARD_MAX_ADAPTERS  # hard limit is 10
    include_interfaces: List[str] = field(default_factory=list)  # interface names, e.g. ["wlan1", "wlan2"]
    exclude_interfaces: List[str] = field(default_factory=list)
    band_2g: bool = True
    band_5g: bool = True
    adapter_poll_interval: float = 2.0  # hot-plug detection interval
    skip_dfs: bool = False
    exclude_channels: List[int] = field(default_factory=list)
    passive: bool = False
    channels_per_scan: int = 1
    scan_timeout: float = 20.0


@dataclass
class LogConfig:
    output_dir: str = "exports"
    min_distance_m: float = 25.0
    rssi_improvement_db: int = 8
    feed_update_interval: float = 5.0


@dataclass
class GpsConfig:
    source: str = "auto"  # auto | gpsd | serial | none   (sim is set by --simulate)
    gpsd_host: str = "127.0.0.1"
    gpsd_port: int = 2947
    serial_port: str = ""  # /dev/ttyACM0; empty = auto-detect (u-blox first)
    baud: int = 0  # 0 = auto-detect
    probe_other_ports: bool = False  # also probe non-u-blox serial devices
    min_fix_mode: int = 2  # 2 = accept 2D fixes, 3 = require 3D
    min_step_m: float = 5.0  # ignore GPS jitter below this step
    max_speed_mps: float = 70.0  # reject position jumps implying > 250 km/h
    stale_after_s: float = 5.0


@dataclass
class UiConfig:
    default_mode: str = "ask"  # ask | cli | gui
    refresh_hz: float = 4.0
    feed_history: int = 500
    theme: str = "sk7ld"  # sk7ld | v1ru5 | cvlt
    privacy: bool = False  # hide SSIDs and Bluetooth device names in the live feed (the CSV keeps them)


@dataclass
class DriveConfig:
    mode: str = "wifi"  # wifi | ble | adsb | wifi+ble | all  (any + combination works)


@dataclass
class BluetoothConfig:
    """Bluetooth Low Energy war-driving (needs the 'bleak' package and the BlueZ service)."""

    max_adapters: int = HARD_MAX_BLE_ADAPTERS  # up to 5 HCI adapters at once (hci0 ... hci4)
    include_adapters: List[str] = field(default_factory=list)  # e.g. ["hci1"]; empty = all
    exclude_adapters: List[str] = field(default_factory=list)  # e.g. ["hci0"] (keep the built-in one free)
    log_random: bool = False  # random private addresses change constantly: show them, but do not log them
    min_rssi: int = -100  # ignore anything weaker than this


@dataclass
class AdsbConfig:
    """ADS-B aircraft tracking with RTL-SDR dongles through an external decoder (readsb / dump1090)."""

    mode: str = "managed"  # managed = Spejari starts one decoder per dongle | external = connect to a running decoder
    decoder: str = "auto"  # auto | readsb | dump1090-fa | dump1090-mutability | dump1090 (or a full path)
    devices: List[str] = field(default_factory=lambda: ["0"])  # RTL-SDR device indexes (managed mode)
    gain: str = "auto"  # "auto" or a number in dB
    ppm: int = 0  # frequency correction
    base_port: int = 30103  # SBS output port of dongle 0; dongle N uses base_port + 10*N
    extra_args: List[str] = field(default_factory=list)  # appended to every decoder command line
    external_host: str = "127.0.0.1"  # external mode: SBS (BaseStation) feed, usually port 30003
    external_port: int = 30003
    max_age_s: float = 60.0  # forget an aircraft that has been silent this long (it stays in the export)
    log_interval_s: float = 5.0  # at most one logged position per aircraft per this many seconds


@dataclass
class UploadConfig:
    """Uploading finished drives to WiGLE and WDGWars. The API keys live in config/.spejari_keys.json."""

    auto: str = "off"  # off | wigle | wdgwars | both  - upload every drive as soon as it is stopped
    wigle_donate: bool = False  # WiGLE "donate" flag: allow commercial use of the uploaded data
    wdgwars_adsb: bool = True  # also send ADS-B aircraft to WDGWars (it scores aircraft too)
    chunk_rows: int = 10000  # WDGWars: rows per request (keeps big drives under the server timeout)
    timeout_s: float = 180.0


@dataclass
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    log: LogConfig = field(default_factory=LogConfig)
    gps: GpsConfig = field(default_factory=GpsConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    drive: DriveConfig = field(default_factory=DriveConfig)
    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    adsb: AdsbConfig = field(default_factory=AdsbConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    simulate_adapters: int = 0  # >0 = simulated Wi-Fi adapters (set by --simulate)
    simulate: bool = False  # simulated GPS, Bluetooth and ADS-B as well
    warnings: List[str] = field(default_factory=list)

    # -- what this drive collects
    @property
    def wifi_on(self) -> bool:
        return "wifi" in mode_parts(self.drive.mode)

    @property
    def ble_on(self) -> bool:
        return "ble" in mode_parts(self.drive.mode)

    @property
    def adsb_on(self) -> bool:
        return "adsb" in mode_parts(self.drive.mode)

    def set_part(self, part: str, on: bool) -> None:
        parts = mode_parts(self.drive.mode)
        (parts.add if on else parts.discard)(part)
        self.drive.mode = parse_mode("+".join(parts)) if parts else self.drive.mode

    @property
    def output_path(self) -> Path:
        p = Path(self.log.output_dir).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    def normalise(self) -> None:
        s = self.scan
        s.max_adapters = max(1, min(HARD_MAX_ADAPTERS, int(s.max_adapters)))
        s.channels_per_scan = max(1, min(8, int(s.channels_per_scan)))
        s.adapter_poll_interval = max(0.5, float(s.adapter_poll_interval))
        s.exclude_channels = [int(c) for c in s.exclude_channels]
        self.gps.source = str(self.gps.source).lower()
        if self.gps.source not in ("auto", "gpsd", "serial", "none", "sim"):
            self.warnings.append(f"Unknown gps.source '{self.gps.source}', using 'auto'")
            self.gps.source = "auto"
        self.gps.min_fix_mode = 3 if int(self.gps.min_fix_mode) >= 3 else 2
        self.ui.refresh_hz = max(1.0, min(20.0, float(self.ui.refresh_hz)))
        try:
            self.drive.mode = parse_mode(self.drive.mode)
        except ValueError as exc:
            self.warnings.append(f"drive.mode: {exc}; using 'wifi'")
            self.drive.mode = "wifi"
        b = self.bluetooth
        b.max_adapters = max(1, min(HARD_MAX_BLE_ADAPTERS, int(b.max_adapters)))
        b.min_rssi = max(-127, min(0, int(b.min_rssi)))
        a = self.adsb
        a.mode = str(a.mode).lower()
        if a.mode not in ("managed", "external"):
            self.warnings.append(f"Unknown adsb.mode '{a.mode}', using 'managed'")
            a.mode = "managed"
        a.devices = [str(d) for d in (a.devices or ["0"])][:8]
        a.base_port = max(1024, min(65000, int(a.base_port)))
        a.max_age_s = max(10.0, float(a.max_age_s))
        a.log_interval_s = max(1.0, float(a.log_interval_s))
        u = self.upload
        u.auto = str(u.auto).lower()
        if u.auto not in ("off", "wigle", "wdgwars", "both"):
            self.warnings.append(f"Unknown upload.auto '{u.auto}', using 'off'")
            u.auto = "off"
        u.chunk_rows = max(500, min(50000, int(u.chunk_rows)))
        u.timeout_s = max(20.0, float(u.timeout_s))
        self.ui.privacy = bool(self.ui.privacy)
        self.ui.theme = str(self.ui.theme).lower()
        self.ui.default_mode = str(self.ui.default_mode).lower()
        if self.ui.default_mode not in ("ask", "cli", "gui"):
            self.warnings.append(f"Unknown ui.default_mode '{self.ui.default_mode}', using 'ask'")
            self.ui.default_mode = "ask"


def _apply(section: Any, data: Dict[str, Any], name: str, warnings: List[str]) -> None:
    valid = {f.name for f in fields(section)}
    for key, value in data.items():
        if key in valid:
            setattr(section, key, value)
        else:
            warnings.append(f"Unknown option [{name}] {key}")


def load_config(path: Optional[str] = None) -> Config:
    """Load configuration. An explicit *path* must exist; the default may be absent."""
    cfg = Config()
    target = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if target.is_file():
        try:
            with open(target, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read config {target}: {exc}") from exc
        sections = ("drive", "scan", "log", "gps", "ui", "bluetooth", "adsb", "upload")
        legacy = []  # Spejari 2 switched radios on with [bluetooth] / [adsb] enabled = true
        for name, part in (("bluetooth", "ble"), ("adsb", "adsb")):
            sec = data.get(name)
            if isinstance(sec, dict) and "enabled" in sec:
                if sec.pop("enabled"):
                    legacy.append(part)
        for name in sections:
            section = data.get(name)
            if isinstance(section, dict):
                _apply(getattr(cfg, name), section, name, cfg.warnings)
        for key in data:
            if key == "pineapple":
                cfg.warnings.append("WiFi Pineapple support was removed in Spejari 3: delete the [[pineapple]] blocks")
            elif key not in sections:
                cfg.warnings.append(f"Unknown config section [{key}]")
        if legacy and not (isinstance(data.get("drive"), dict) and "mode" in data["drive"]):
            cfg.drive.mode = "+".join(["wifi"] + legacy)
    elif path:
        raise ConfigError(f"Config file not found: {target}")
    cfg.normalise()
    return cfg
