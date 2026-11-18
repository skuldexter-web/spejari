"""Application controller: wires GPS, scan engine, session, exporter and UI together."""

from __future__ import annotations

import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live

from .config import Config, mode_label
from .adsb import AdsbEngine, AdsbJournal
from .bluetooth import BleEngine
from .exporter import Journal, export_filename, normalize_csv_path, write_wigle_csv
from .gps import GpsMonitor
from .models import FeedLog
from .wifi import make_engine
from .session import Session
from .ui import DashboardView, KeyReader, render_dashboard

IDLE, RUNNING, STOPPING = "IDLE", "RUNNING", "STOPPING"


class App:
    def __init__(self, cfg: Config, auto_start: bool = False) -> None:
        self.cfg = cfg
        self.auto_start = auto_start
        self.console = Console()
        self.feed = FeedLog(cfg.ui.feed_history)
        self.gps = GpsMonitor(cfg.gps)
        self.engine = None  # Wi-Fi scan engine (None when the drive mode has no Wi-Fi)
        self.ble: Optional[BleEngine] = None
        self.adsb: Optional[AdsbEngine] = None
        self.adsb_session = None
        self.adsb_journal: Optional[AdsbJournal] = None
        if cfg.wifi_on:
            self.engine = make_engine(cfg, self.gps, self.feed)
        if cfg.ble_on:
            self.ble = BleEngine(cfg, self.gps, self.feed, simulate=cfg.simulate)
        if cfg.adsb_on:
            self.adsb = AdsbEngine(cfg, self.gps, self.feed, simulate=cfg.simulate)
        self.uploading = False
        self.last_upload = ""
        self.state = IDLE
        self.session: Optional[Session] = None
        self.journal: Optional[Journal] = None
        self.last_export = ""
        self.last_export_path: Optional[Path] = None
        self.last_stats = None  # SessionStats of the finished session (kept on screen)
        self.last_km = 0.0
        self.exported_paths = []
        self._started = False
        self._quit_requested = False
        self._lock = threading.Lock()

    # -- session control ---------------------------------------------------------
    def start_session(self) -> None:
        with self._lock:
            if self.state != IDLE:
                return
            if self.engine is None and self.ble is None and self.adsb is None:
                self.feed.system("Cannot start: pick a drive mode first (Wi-Fi, BLE and / or ADS-B)", warn=True)
                return
            if self.engine is not None and self.engine.adapter_count() == 0 and self.ble is None and self.adsb is None:
                self.feed.system("Cannot start: no wireless adapter detected", warn=True)
                return
            started = datetime.now()
            self.journal = Journal(self.cfg.output_path, started)
            session = Session(self.cfg, self.feed, self.journal)
            session.started_local = started
            session.distance_baseline_m = self.gps.snapshot().distance_m
            self.session = session
            self.last_stats = None
            self.last_km = 0.0
            self.state = RUNNING
        if self.engine is not None:
            self.engine.begin_session(session)
        if self.ble is not None:
            self.ble.begin_session(session)
        if self.adsb is not None:
            self.adsb_journal = AdsbJournal(self.cfg.output_path, started)
            self.adsb_session = self.adsb.begin_session(started, self.adsb_journal)
        self.feed.system(f"Session started - {mode_label(self.cfg.drive.mode)} - logging")
        if not self.gps.snapshot().valid:
            self.feed.system("Waiting for GPS fix: networks are shown but only logged once there is a fix", warn=True)

    def stop_session(self, then_quit: bool = False) -> None:
        with self._lock:
            if self.state != RUNNING:
                return
            self.state = STOPPING
        threading.Thread(target=self._finish, args=(then_quit,), name="spejari-export", daemon=True).start()

    def _finish(self, then_quit: bool) -> None:
        notes = []
        try:
            if self.ble is not None:
                self.ble.end_session()
            adsb_session = None
            if self.adsb is not None:
                adsb_session = self.adsb.end_session()
            if self.engine is not None:
                self.engine.end_session()
            session = self.session
            km = max(0.0, self.gps.snapshot().distance_m - (session.distance_baseline_m if session else 0.0)) / 1000.0
            if session is not None:
                self.last_stats, self.last_km = session.stats(), km
            new_files = []
            if session is None or not session.observations:
                if self.engine is not None or self.ble is not None:
                    notes.append("nothing exported (no networks with GPS fix)")
                    self.feed.system("Session stopped: no Wi-Fi/BLE observations with a GPS fix, no CSV written",
                                     warn=True)
                if self.journal:
                    self.journal.discard()
            else:
                path = normalize_csv_path(self.cfg.output_path / export_filename(session.started_local))
                rows = write_wigle_csv(session.observations, path)
                st = session.stats()
                notes.append(f"{path.name} ({rows} rows)")
                self.exported_paths.append(path)
                new_files.append(path)
                self.last_export_path = path
                extra = f" / {st.ble_logged} BLE rows" if st.ble_devices else ""
                self.feed.system(f"Exported {rows} rows / {st.unique_bssids} networks{extra} / {km:.2f} km -> {path}")
                if self.journal:
                    self.journal.discard()
            if adsb_session is not None:
                result = adsb_session.export(self.cfg.output_path)
                if result is None:
                    if self.adsb_journal:
                        self.adsb_journal.discard()
                    self.feed.system("ADS-B: no aircraft positions heard, no ADS-B file written", warn=True)
                else:
                    csv_path, geo_path, n = result
                    notes.append(f"{csv_path.name} ({n} positions)")
                    self.exported_paths.extend([csv_path, geo_path])
                    new_files.append(csv_path)
                    self.feed.system(f"Exported {n} ADS-B positions -> {csv_path} (+ {geo_path.name})")
            self.last_export = " · ".join(notes)
            if new_files and self.cfg.upload.auto != "off" and not self.cfg.simulate:
                self.upload(new_files, self.cfg.upload.auto, wait=then_quit)
        except Exception as exc:  # the journals on disk still hold the data
            self.feed.system(f"Export failed: {exc} (use --recover to rebuild from journal)", warn=True)
        finally:
            self.journal = None
            self.adsb_journal = None
            self.adsb_session = None
            self.session = None
            self.state = IDLE
            if then_quit:
                self._quit_requested = True

    def request_quit(self, *_args) -> None:
        self._quit_requested = True

    # -- lifecycle shared by the terminal dashboard and the GUI --------------------
    def startup(self) -> None:
        if self._started:
            return
        self._started = True
        self.gps.start()
        if self.engine is not None:
            self.engine.start()
        if self.ble is not None:
            self.ble.start()
        if self.adsb is not None:
            self.adsb.start()

    def shutdown(self) -> None:
        if self.ble is not None:
            self.ble.shutdown()
        if self.adsb is not None:
            self.adsb.shutdown()
        if self.engine is not None:
            self.engine.shutdown()
        self.gps.stop()
        if self.journal:  # only reachable on an unexpected exit
            self.journal.close()

    def restart_gps(self) -> None:
        """Apply new GPS settings (source / device). Only call while idle."""
        old = self.gps
        old.stop()

        def swap() -> None:
            if old.is_alive():
                old.join(timeout=5.0)
            new = GpsMonitor(self.cfg.gps)
            self.gps = new
            if self.engine is not None:
                self.engine.gps = new
            for extra in (self.ble, self.adsb):
                if extra is not None:
                    extra.gps = new
            new.start()

        threading.Thread(target=swap, name="spejari-gps-restart", daemon=True).start()

    # -- drive mode (the GUI switches radios on and off while idle) -------------------------
    def set_mode(self, mode: str) -> None:
        """Apply a drive mode such as 'wifi', 'ble', 'adsb', 'wifi+ble' or 'all'. Only while idle."""
        if self.state != IDLE:
            return
        from .config import mode_parts, parse_mode

        mode = parse_mode(mode)
        parts = mode_parts(mode)
        self.set_wifi("wifi" in parts)
        self.set_bluetooth("ble" in parts)
        self.set_adsb("adsb" in parts)
        self.cfg.drive.mode = mode

    def set_wifi(self, enabled: bool) -> None:
        if self.state != IDLE:
            return
        if enabled and self.engine is None:
            self.cfg.set_part("wifi", True)
            self.engine = make_engine(self.cfg, self.gps, self.feed)
            if self._started:
                self.engine.start()
        elif not enabled and self.engine is not None:
            self.cfg.set_part("wifi", False)
            self.engine.shutdown()
            self.engine = None

    def set_bluetooth(self, enabled: bool) -> None:
        if self.state != IDLE:
            return
        self.cfg.set_part("ble", enabled)
        if enabled and self.ble is None:
            self.ble = BleEngine(self.cfg, self.gps, self.feed, simulate=self.cfg.simulate)
            if self._started:
                self.ble.start()
        elif not enabled and self.ble is not None:
            self.ble.shutdown()
            self.ble = None

    def set_adsb(self, enabled: bool) -> None:
        if self.state != IDLE:
            return
        self.cfg.set_part("adsb", enabled)
        if enabled and self.adsb is None:
            self.adsb = AdsbEngine(self.cfg, self.gps, self.feed, simulate=self.cfg.simulate)
            if self._started:
                self.adsb.start()
        elif not enabled and self.adsb is not None:
            self.adsb.shutdown()
            self.adsb = None

    # -- uploads -----------------------------------------------------------------
    def upload(self, files=None, target: str = "both", wait: bool = False) -> bool:
        """Upload files (default: every export not uploaded yet) in the background. False if one is running."""
        from . import upload as up

        if self.uploading:
            self.feed.system("An upload is already running", warn=True)
            return False
        keys = up.load_keys()
        targets = [t for t in up.targets_of(target) if keys.has(t)]
        missing = [t for t in up.targets_of(target) if not keys.has(t)]
        for t in missing:
            self.feed.system(f"Upload to {'WiGLE' if t == 'wigle' else 'WDGWars'} skipped: no API key "
                             "(GUI: API keys button, terminal: spejari --keys)", warn=True)
        if not targets:
            return False
        self.uploading = True

        def log(msg: str, warn: bool = False) -> None:
            self.feed.system(msg, warn=warn)

        def work() -> None:
            done = failed = 0
            try:
                for t in targets:
                    todo = list(files) if files else up.pending(self.cfg.output_path, t, self.cfg.upload)
                    name = "WiGLE" if t == "wigle" else "WDGWars"
                    if not todo:
                        log(f"{name}: nothing new to upload")
                        continue
                    log(f"Uploading {len(todo)} file(s) to {name} ...")
                    for r in up.upload_files(todo, t, self.cfg.upload, keys, self.cfg.output_path, log):
                        if r.details.get("skipped"):
                            continue
                        done, failed = (done + 1, failed) if r.ok else (done, failed + 1)
            except Exception as exc:  # never kill the app over a network problem
                failed += 1
                log(f"Upload error: {exc}", True)
            finally:
                self.last_upload = f"{done} uploaded" + (f", {failed} failed" if failed else "")
                self.uploading = False

        th = threading.Thread(target=work, name="spejari-upload", daemon=True)
        th.start()
        if wait:
            th.join()
        return True

    # -- view --------------------------------------------------------------------
    def view(self) -> DashboardView:
        gps = self.gps.snapshot()
        session = self.session
        v = DashboardView(
            state=self.state,
            gps=gps,
            adapters=self.engine.adapter_views() if self.engine is not None else [],
            max_adapters=self.cfg.scan.max_adapters,
            wifi_enabled=self.engine is not None,
            drive_mode=self.cfg.drive.mode,
            privacy=self.cfg.ui.privacy,
            uploading=self.uploading,
            last_upload=self.last_upload,
            feed=self.feed.tail(200),
            last_export=self.last_export,
            utc_now=self.gps.utc_now(),
        )
        if self.ble is not None:
            v.ble_enabled = True
            v.ble_adapters = self.ble.adapter_views()
            v.ble_note = self.ble.note
        if self.adsb is not None:
            v.adsb_enabled = True
            (v.aircraft, v.adsb_dongles, v.adsb_total, v.adsb_max_range_km, v.adsb_note) = self.adsb.views()
        if session is not None:
            st = session.stats()
            v.ble_devices, v.ble_logged = st.ble_devices, st.ble_logged
            v.elapsed_s = session.elapsed
            v.unique_ssids, v.unique_bssids, v.hidden = st.unique_ssids, st.unique_bssids, st.hidden
            v.logged, v.seen_no_fix = st.logged, st.seen_no_fix
            v.session_km = max(0.0, gps.distance_m - session.distance_baseline_m) / 1000.0
        elif self.last_stats is not None:  # show the finished session's totals until the next start
            st = self.last_stats
            v.unique_ssids, v.unique_bssids, v.hidden = st.unique_ssids, st.unique_bssids, st.hidden
            v.logged, v.seen_no_fix, v.session_km = st.logged, st.seen_no_fix, self.last_km
            v.ble_devices, v.ble_logged = st.ble_devices, st.ble_logged
        return v

    # -- main loop ---------------------------------------------------------------
    def _on_key(self, key: str) -> None:
        if key == "s":
            if self.state == IDLE:
                self.start_session()
            elif self.state == RUNNING:
                self.stop_session()
        elif key == "p":
            self.cfg.ui.privacy = not self.cfg.ui.privacy
            self.feed.system(f"Privacy mode {'ON: names hidden in the feed' if self.cfg.ui.privacy else 'OFF'}")
        elif key == "u":
            if self.state == RUNNING:
                self.feed.system("Stop the drive first, then upload", warn=True)
            else:
                self.upload(None, self.cfg.upload.auto if self.cfg.upload.auto != "off" else "both")
        elif key == "q":
            self.request_quit()

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_quit)
        signal.signal(signal.SIGTERM, self.request_quit)
        self.startup()
        if self.auto_start or not KeyReader().enabled:
            deadline = time.monotonic() + 8
            while self.engine is not None and self.engine.adapter_count() == 0 and time.monotonic() < deadline:
                time.sleep(0.2)
            self.start_session()
        interval = 1.0 / self.cfg.ui.refresh_hz
        try:
            with KeyReader() as keys, Live(
                render_dashboard(self.view()), console=self.console, screen=True, auto_refresh=False
            ) as live:
                while True:
                    for key in keys.read(interval):
                        self._on_key(key)
                    if self._quit_requested:
                        if self.state == RUNNING:
                            self.stop_session(then_quit=True)
                        elif self.state == IDLE and not self.uploading:
                            break
                    live.update(render_dashboard(self.view()), refresh=True)
        finally:
            self.shutdown()
        for path in self.exported_paths:
            self.console.print(f"[bold green]Exported:[/] {path}")
        return 0
