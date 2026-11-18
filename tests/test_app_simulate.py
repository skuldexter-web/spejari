"""End-to-end: a simulated drive (Wi-Fi + BLE + ADS-B) exports the three files."""

import csv
import json
import time

from spejari.app import IDLE, App
from spejari.config import Config
from spejari.exporter import recover_journals


def _cfg(tmp_path):
    cfg = Config()
    cfg.simulate = True
    cfg.simulate_adapters = 3
    cfg.gps.source = "sim"
    cfg.drive.mode = "all"
    cfg.adsb.log_interval_s = 1.0
    cfg.log.output_dir = str(tmp_path)
    cfg.normalise()
    return cfg


def test_simulated_session_exports_wifi_ble_and_adsb(tmp_path):
    app = App(_cfg(tmp_path), auto_start=False)
    app.startup()
    try:
        deadline = time.monotonic() + 20
        while app.engine.adapter_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.2)
        assert app.engine.adapter_count() == 3
        while not app.gps.snapshot().valid and time.monotonic() < deadline:
            time.sleep(0.2)
        app.start_session()
        time.sleep(9)
        v = app.view()
        assert v.ble_enabled and v.adsb_enabled
        assert v.unique_bssids > 0 and v.ble_devices > 0 and len(v.aircraft) > 0
        app.stop_session()
        deadline = time.monotonic() + 40
        while app.state != IDLE and time.monotonic() < deadline:
            time.sleep(0.2)
        assert app.state == IDLE
    finally:
        app.shutdown()

    csvs = sorted(p for p in tmp_path.glob("*.csv") if "_ADSB_" not in p.name)
    adsb = sorted(tmp_path.glob("Spejari_ADSB_*.csv"))
    geo = sorted(tmp_path.glob("Spejari_ADSB_*.geojson"))
    assert len(csvs) == 1 and csvs[0].suffix == ".csv" and csvs[0].name.startswith("Spejari_")
    assert len(adsb) == 1 and len(geo) == 1
    assert not list(tmp_path.glob(".spejari_*.journal"))  # journals are removed after a clean export

    lines = csvs[0].read_text().splitlines()
    assert lines[0].startswith("WigleWifi-1.6")
    rows = list(csv.DictReader(lines[1:]))
    kinds = {r["Type"] for r in rows}
    assert {"WIFI", "BLE"} <= kinds
    assert all(r["AuthMode"] == "Misc [LE]" and r["Channel"] == "0" for r in rows if r["Type"] == "BLE")
    assert all(int(r["Frequency"]) > 2400 for r in rows if r["Type"] == "WIFI")

    arows = list(csv.DictReader(adsb[0].read_text().splitlines()))
    assert arows and {"ICAO", "Latitude", "RangeKm"} <= set(arows[0])
    gj = json.loads(geo[0].read_text())
    assert gj["type"] == "FeatureCollection" and gj["features"]


def test_random_ble_addresses_are_not_logged_by_default(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.drive.mode = "wifi+ble"
    cfg.normalise()
    app = App(cfg)
    app.startup()
    try:
        deadline = time.monotonic() + 20
        while (app.engine.adapter_count() == 0 or not app.gps.snapshot().valid) and time.monotonic() < deadline:
            time.sleep(0.2)
        app.start_session()
        time.sleep(7)
        session = app.session
        random_addrs = {a for a, n in session._ble.items() if int(a.split(":")[0], 16) >> 6 in (0, 1)}
        logged = {o.bssid for o in session.observations if o.kind == "BLE"}
        assert random_addrs, "the simulator should produce some rotating addresses"
        assert not (random_addrs & logged)
        assert logged
        app.stop_session()
        deadline = time.monotonic() + 40
        while app.state != IDLE and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        app.shutdown()


def _run_mode(tmp_path, mode):
    cfg = _cfg(tmp_path)
    cfg.drive.mode = mode
    cfg.normalise()
    app = App(cfg)
    app.startup()
    try:
        deadline = time.monotonic() + 20
        while not app.gps.snapshot().valid and time.monotonic() < deadline:
            time.sleep(0.2)
        app.start_session()
        time.sleep(6)
        v = app.view()
        app.stop_session()
        while app.state != IDLE and time.monotonic() < deadline + 30:
            time.sleep(0.2)
    finally:
        app.shutdown()
    return app, v


def test_ble_only_mode_has_no_wifi(tmp_path):
    app, v = _run_mode(tmp_path, "ble")
    assert app.engine is None and not v.wifi_enabled and v.ble_enabled and not v.adsb_enabled
    rows = list(csv.DictReader(sorted(tmp_path.glob("Spejari_2*.csv"))[0].read_text().splitlines()[1:]))
    assert rows and {r["Type"] for r in rows} == {"BLE"}
    assert not list(tmp_path.glob("Spejari_ADSB_*"))


def test_adsb_only_mode(tmp_path):
    app, v = _run_mode(tmp_path, "adsb")
    assert app.engine is None and app.ble is None and v.adsb_enabled
    assert list(tmp_path.glob("Spejari_ADSB_*.csv"))
    assert not list(tmp_path.glob("Spejari_2*.csv"))  # no Wi-Fi/BLE file in ADS-B-only mode


def test_switching_modes_while_idle(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.drive.mode = "wifi"
    cfg.normalise()
    app = App(cfg)
    app.startup()
    try:
        app.set_mode("ble+adsb")
        assert app.engine is None and app.ble is not None and app.adsb is not None
        assert cfg.drive.mode == "ble+adsb" and not cfg.wifi_on
        app.set_mode("wifi")
        assert app.engine is not None and app.ble is None and app.adsb is None
    finally:
        app.shutdown()
