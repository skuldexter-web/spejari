"""ADS-B engine: one decoder process (readsb / dump1090) per RTL-SDR dongle, SBS clients, session logging.

Spejari does not demodulate 1090 MHz itself. It starts the well-tested decoder of your choice once per dongle,
reads its SBS (BaseStation) text feed over a local TCP port and keeps the aircraft table, the live panel and the
GPS-stamped coverage log. In ``external`` mode it connects to a decoder that is already running (for example the
one from PiAware / tar1090, or a decoder you started yourself).
"""

from __future__ import annotations

import collections
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Deque, List, Optional, Tuple

from ..config import PROJECT_ROOT, AdsbConfig, Config
from ..gps import GpsMonitor
from ..models import AdsbDongleView, AircraftView, FeedEntry, FeedLog
from .export import AdsbJournal, adsb_filename, write_adsb_csv, write_adsb_geojson
from .sbs import SbsUpdate, parse_sbs
from .tracker import AircraftTable, PositionRow

DECODER_ORDER = ["readsb", "dump1090-fa", "dump1090-mutability", "dump1090"]
_RETRY_S = 10.0


# ------------------------------------------------------------------------------- discovery
def find_decoder(name: str = "auto") -> Optional[Tuple[str, str]]:
    """(kind, path) of the decoder to use, or None. Looks on PATH and in ./tools/**."""
    candidates = DECODER_ORDER if name in ("", "auto") else [name]
    for cand in candidates:
        if os.path.sep in cand:
            if Path(cand).is_file():
                return _kind_of(cand), cand
            continue
        exe = shutil.which(cand)
        if exe:
            return _kind_of(cand), exe
        tools = PROJECT_ROOT / "tools"
        if tools.is_dir():
            for p in sorted(tools.rglob(cand)):
                if p.is_file() and os.access(p, os.X_OK):
                    return _kind_of(cand), str(p)
    return None


def _kind_of(name: str) -> str:
    base = os.path.basename(name).lower()
    for kind in DECODER_ORDER:
        if base.startswith(kind):
            return kind
    return "dump1090"


def build_command(kind: str, exe: str, device: str, port: int, cfg: AdsbConfig) -> List[str]:
    """Command line that makes the decoder write SBS on *port* and nothing else on the network.

    Note: ``--net-only`` must NOT be used - in the dump1090 family it means "no RTL-SDR device, network input only".
    Checked against the real readsb 3.16 and dump1090-mutability 1.15 binaries; dump1090-fa and classic dump1090 follow
    their documented options.
    """
    auto = str(cfg.gain).lower() == "auto"
    gain = "-10" if auto else str(cfg.gain)  # dump1090 family: -10 = auto gain
    off = ["--net-ro-port", "0", "--net-ri-port", "0", "--net-bo-port", "0", "--net-bi-port", "0"]
    if kind == "readsb":
        # readsb: auto gain is its default, so only a fixed gain is passed
        cmd = [exe, "--device-type", "rtlsdr", "--device", device, *([] if auto else ["--gain", gain]),
               "--ppm", str(cfg.ppm), "--net", "--net-sbs-port", str(port), "--net-api-port", "0", *off, "--quiet"]
    elif kind == "dump1090-fa":
        cmd = [exe, "--device-index", device, "--gain", gain, "--ppm", str(cfg.ppm), "--net",
               "--net-sbs-port", str(port), "--net-http-port", "0", *off, "--quiet"]
    elif kind == "dump1090-mutability":  # has no built-in web server option
        cmd = [exe, "--device-index", device, "--gain", gain, "--ppm", str(cfg.ppm), "--net",
               "--net-sbs-port", str(port), *off, "--quiet"]
    else:  # classic dump1090 (antirez / MalcolmRobb forks)
        cmd = [exe, "--device-index", device, "--gain", gain, "--net", "--net-sbs-port", str(port),
               "--net-http-port", "0", *off, "--quiet"]
    return cmd + list(cfg.extra_args)


def list_rtlsdr() -> List[str]:
    """Human-readable lines for the RTL-SDR dongles that can be seen."""
    lines: List[str] = []
    exe = shutil.which("rtl_test") or shutil.which("rtl_test.exe")
    if exe:
        try:
            out = subprocess.run([exe, "-t"], capture_output=True, text=True, timeout=10)
            for line in (out.stdout + out.stderr).splitlines():
                if line.strip()[:1].isdigit() and ":" in line:
                    lines.append(line.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    if not lines and sys.platform.startswith("linux"):
        base = Path("/sys/bus/usb/devices")
        idx = 0
        for d in sorted(base.glob("*")) if base.is_dir() else []:
            try:
                vid, pid = (d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip()
            except OSError:
                continue
            if vid == "0bda" and pid in ("2832", "2838"):
                try:
                    prod = (d / "product").read_text().strip()
                except OSError:
                    prod = "RTL2832U"
                lines.append(f"{idx}: {prod} (USB {vid}:{pid})")
                idx += 1
    return lines


def describe_environment(cfg: AdsbConfig) -> List[str]:
    """Lines for `spejari --list-sdr`."""
    out = ["[bold]RTL-SDR dongles[/]"]
    dongles = list_rtlsdr()
    if dongles:
        out += [f"  {d}" for d in dongles]
    else:
        hint = "none found. Plug one in; if it is listed by lsusb but busy, blacklist dvb_usb_rtl28xxu (see README)."
        out.append(f"  [yellow]{hint}[/]")
    out.append("")
    out.append("[bold]ADS-B decoder[/]")
    found = find_decoder(cfg.decoder)
    if found:
        out.append(f"  [green]{found[0]}[/]  {found[1]}")
    else:
        out.append("  [yellow]no decoder found. Install readsb or dump1090-fa (Linux: ./setup.sh --adsb), "
                   "or use --adsb-external HOST:PORT.[/]")
    out.append("")
    if cfg.mode == "external":
        out.append(f"Mode: external   SBS feed {cfg.external_host}:{cfg.external_port}")
    else:
        used = ", ".join(f"#{d}" for d in cfg.devices)
        out.append(f"Mode: managed   dongles Spejari will use: {used} (pick others with --adsb-device N)   "
                   f"SBS ports from {cfg.base_port}")
    return out


# ---------------------------------------------------------------------------------- session
class AdsbSession:
    """Everything one start/stop cycle collects: position log, receiver track, aircraft table."""

    def __init__(self, cfg: Config, started: datetime, journal: Optional[AdsbJournal]) -> None:
        self.cfg = cfg
        self.started_local = started
        self.journal = journal
        self.table = AircraftTable(cfg.adsb.log_interval_s, cfg.adsb.max_age_s)
        self.rows: List[PositionRow] = []
        self.receiver_track: List[Tuple[datetime, float, float]] = []
        self._last_rx = 0.0
        self._lock = threading.Lock()

    def add_row(self, row: PositionRow) -> None:
        with self._lock:
            self.rows.append(row)
        if self.journal:
            self.journal.write_row(row)

    def note_receiver(self, now_mono: float, when: datetime, lat: float, lon: float) -> None:
        if now_mono - self._last_rx < 5.0:
            return
        self._last_rx = now_mono
        self.receiver_track.append((when, lat, lon))
        if self.journal:
            self.journal.write_rx(when, lat, lon)

    def export(self, directory: Path) -> Optional[Tuple[Path, Path, int]]:
        """Write CSV + GeoJSON. Returns None when nothing with a position was heard."""
        with self._lock:
            rows = list(self.rows)
        if not rows:
            return None
        csv_path = directory / adsb_filename(self.started_local)
        n = write_adsb_csv(rows, csv_path)
        geo = write_adsb_geojson(rows, self.receiver_track, csv_path)
        if self.journal:
            self.journal.discard()
        return csv_path, geo, n


# ----------------------------------------------------------------------------------- workers
class DongleWorker(threading.Thread):
    """Runs one decoder (managed mode) and reads its SBS feed; or just reads an external feed."""

    def __init__(self, engine: "AdsbEngine", name: str, device: Optional[str], host: str, port: int) -> None:
        super().__init__(name=f"spejari-adsb-{name}", daemon=True)
        self.engine = engine
        self.dongle = name
        self.device = device  # None = external feed
        self.host, self.port = host, port
        self.state = "starting"
        self.msgs = 0
        self._stop_evt = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._stderr: Deque[str] = collections.deque(maxlen=6)
        self._seen: set = set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._kill()

    def aircraft_count(self) -> int:
        return len(self._seen)

    def _active(self) -> bool:
        return not self._stop_evt.is_set() and self.engine.scanning.is_set()

    # -- decoder process
    def _spawn(self) -> bool:
        found = find_decoder(self.engine.cfg.adsb.decoder)
        if not found:
            self.state = "error"
            self.engine.note = ("No ADS-B decoder found. Install readsb or dump1090-fa "
                                "(./setup.sh --adsb), or use external mode.")
            return False
        kind, exe = found
        cmd = build_command(kind, exe, str(self.device), self.port, self.engine.cfg.adsb)
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            self.state = "error"
            self.engine.note = f"{self.dongle}: cannot start {exe}: {exc}"
            return False
        threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()
        self.engine.feed.system(f"ADS-B {self.dongle}: started {kind} on RTL-SDR #{self.device} (SBS port {self.port})")
        return True

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                line = line.strip()
                if line:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass

    def _decoder_died(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    # -- SBS client
    def _connect(self) -> Optional[socket.socket]:
        deadline = time.monotonic() + 20
        while self._active() and time.monotonic() < deadline:
            if self.device is not None and self._decoder_died():
                return None
            try:
                sock = socket.create_connection((self.host, self.port), timeout=2.0)
                sock.settimeout(1.0)
                return sock
            except OSError:
                self.state = "connecting"
                self._stop_evt.wait(0.5)
        return None

    def _read(self, sock: socket.socket) -> None:
        buf = b""
        self.state = "listening"
        last_data = time.monotonic()
        while self._active():
            if self.device is not None and self._decoder_died():
                return
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                if time.monotonic() - last_data > 30:
                    self.state = "no data"
                continue
            except OSError:
                return
            if not chunk:
                return
            last_data = time.monotonic()
            self.state = "receiving"
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                upd = parse_sbs(raw.decode("ascii", "ignore"))
                if upd is not None:
                    self.msgs += 1
                    self._seen.add(upd.icao)
                    self.engine.on_update(upd, self.dongle)
            if len(buf) > 65536:
                buf = b""

    def run(self) -> None:
        while not self._stop_evt.is_set():
            if not self.engine.scanning.is_set():
                self.state = "idle"
                self._stop_evt.wait(0.3)
                continue
            if self.device is not None and not self._spawn():
                self._stop_evt.wait(_RETRY_S)
                continue
            sock = self._connect()
            if sock is not None:
                try:
                    self._read(sock)
                finally:
                    sock.close()
            if self.device is not None and self._decoder_died():
                tail = "; ".join(list(self._stderr)[-2:]) or "no output"
                self.state = "decoder exited"
                self.engine.note = f"{self.dongle}: decoder exited ({tail[:110]})"
                self.engine.feed.system(f"ADS-B {self.dongle}: decoder exited - {tail[:110]}", warn=True)
                self._kill()
                self._stop_evt.wait(_RETRY_S)
            elif sock is None and self._active():
                self.state = "no connection"
                self._kill()
                self._stop_evt.wait(2.0)
        self._kill()


class SimWorker(threading.Thread):
    """Fake aircraft flying over the simulated drive (no dongle, no decoder)."""

    def __init__(self, engine: "AdsbEngine", name: str, seed: int) -> None:
        super().__init__(name=f"spejari-adsb-{name}", daemon=True)
        import random

        self.engine = engine
        self.dongle = name
        self.state = "idle"
        self.msgs = 0
        self._stop_evt = threading.Event()
        rng = random.Random(seed)
        self._planes = []
        for i in range(7):
            self._planes.append({
                "icao": f"{rng.randrange(0x400000, 0x7FFFFF):06X}",
                "call": f"{rng.choice(['LOT', 'DLH', 'RYR', 'WZZ', 'KLM', 'BAW'])}{rng.randrange(10, 9999)}",
                "lat": 52.2297 + rng.uniform(-1.2, 1.2), "lon": 21.0122 + rng.uniform(-1.8, 1.8),
                "alt": rng.choice([8000, 12000, 24000, 33000, 36000, 38000]),
                "kt": rng.uniform(250, 480), "trk": rng.uniform(0, 360),
            })
        self._seen: set = set()

    def stop(self) -> None:
        self._stop_evt.set()

    def aircraft_count(self) -> int:
        return len(self._seen)

    def run(self) -> None:
        import math

        last = time.monotonic()
        while not self._stop_evt.is_set():
            if not self.engine.scanning.is_set():
                self.state = "idle"
                self._stop_evt.wait(0.3)
                continue
            self.state = "receiving"
            now = time.monotonic()
            dt, last = now - last, now
            for p in self._planes:
                dist_nm = p["kt"] * dt / 3600.0 * 12.0  # 12x time lapse so aircraft visibly move
                p["lat"] += dist_nm / 60.0 * math.cos(math.radians(p["trk"]))
                p["lon"] += dist_nm / 60.0 * math.sin(math.radians(p["trk"])) / max(0.2, math.cos(math.radians(p["lat"])))
                if abs(p["lat"] - 52.2297) > 2.5 or abs(p["lon"] - 21.0122) > 4.0:
                    p["trk"] = (p["trk"] + 180) % 360
                self._seen.add(p["icao"])
                for kind in (1, 3, 4):
                    upd = SbsUpdate(icao=p["icao"], msg_type=kind)
                    if kind == 1:
                        upd.callsign = p["call"]
                    elif kind == 3:
                        upd.lat, upd.lon, upd.alt_ft = round(p["lat"], 5), round(p["lon"], 5), p["alt"]
                    else:
                        upd.speed_kt, upd.track = round(p["kt"]), round(p["trk"])
                    self.msgs += 1
                    self.engine.on_update(upd, self.dongle)
            self._stop_evt.wait(0.5)


# ------------------------------------------------------------------------------------ engine
class AdsbEngine:
    """Runs only while a session is active (decoders are started and stopped with it)."""

    def __init__(self, cfg: Config, gps: GpsMonitor, feed: FeedLog, simulate: bool = False) -> None:
        self.cfg = cfg
        self.gps = gps
        self.feed = feed
        self.simulate = simulate
        self.scanning = threading.Event()
        self.session: Optional[AdsbSession] = None
        self.last_session: Optional[AdsbSession] = None  # kept for the panel after a stop
        self.note = ""
        self._workers: List[threading.Thread] = []
        self._lock = threading.RLock()

    def start(self) -> None:  # decoders start with the session
        pass

    def shutdown(self) -> None:
        self.scanning.clear()
        self._stop_workers()

    def _stop_workers(self) -> None:
        with self._lock:
            workers, self._workers = self._workers, []
        for w in workers:
            w.stop()  # type: ignore[attr-defined]
        for w in workers:
            w.join(timeout=5.0)

    def begin_session(self, started: datetime, journal: Optional[AdsbJournal]) -> AdsbSession:
        session = AdsbSession(self.cfg, started, journal)
        with self._lock:
            self.session = session
            self.last_session = None
            self.note = ""
            a = self.cfg.adsb
            if self.simulate:
                self._workers = [SimWorker(self, "sim-rtl0", 7)]
            elif a.mode == "external":
                self._workers = [DongleWorker(self, "external", None, a.external_host, a.external_port)]
            else:
                self._workers = [
                    DongleWorker(self, f"rtl{dev}", dev, "127.0.0.1", a.base_port + 10 * n)
                    for n, dev in enumerate(a.devices)
                ]
        self.scanning.set()
        for w in list(self._workers):
            w.start()
        return session

    def end_session(self) -> Optional[AdsbSession]:
        self.scanning.clear()
        self._stop_workers()
        with self._lock:
            session, self.session = self.session, None
            self.last_session = session
        return session

    # -- data flow
    def receiver(self) -> Optional[Tuple[float, float]]:
        fix = self.gps.snapshot()
        if fix.valid and fix.lat is not None and fix.lon is not None:
            return fix.lat, fix.lon
        return None

    def on_update(self, upd: SbsUpdate, dongle: str) -> None:
        session = self.session
        if session is None or not self.scanning.is_set():
            return
        rx = self.receiver()
        now_utc = self.gps.utc_now()
        is_new, row = session.table.update(upd, dongle, rx, time.monotonic(), now_utc)
        if rx is not None:
            session.note_receiver(time.monotonic(), now_utc, rx[0], rx[1])
        if row is not None:
            session.add_row(row)
        if is_new:
            self.feed.add(FeedEntry(ts=time.time(), kind="NEW", ssid=upd.callsign or "", bssid=upd.icao, band="ADSB",
                                    adapter=dongle, security="aircraft", message="aircraft"))

    # -- views for the UI
    def views(self) -> Tuple[List[AircraftView], List[AdsbDongleView], int, float, str]:
        session = self.session or self.last_session
        with self._lock:
            workers = list(self._workers)
        dongles = [
            AdsbDongleView(name=w.dongle, state=w.state, msgs=w.msgs, aircraft=w.aircraft_count())  # type: ignore[attr-defined]
            for w in workers
        ]
        if session is None:
            return [], dongles, 0, 0.0, self.note
        aircraft = session.table.views(time.monotonic(), self.receiver()) if self.session else []
        return aircraft, dongles, session.table.total_seen(), session.table.max_range_km, self.note
