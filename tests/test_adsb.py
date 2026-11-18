import os
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from spejari.adsb import (
    AdsbEngine, AdsbJournal, AircraftTable, PositionRow, adsb_filename, build_command, find_decoder, parse_sbs,
    read_adsb_journal, recover_adsb_journals, write_adsb_csv, write_adsb_geojson,
)
from spejari.config import AdsbConfig, Config
from spejari.models import FeedLog
from spejari.gps import GpsMonitor

HERE = Path(__file__).resolve().parent


def test_parse_sbs_position_velocity_ident():
    pos = parse_sbs("MSG,3,1,1,4840d6,1,2026/01/01,00:00:00.000,2026/01/01,00:00:00.000,,38000,,,52.1,4.2,,,0,0,0,0")
    assert pos.icao == "4840D6" and pos.alt_ft == 38000 and (pos.lat, pos.lon) == (52.1, 4.2)
    vel = parse_sbs("MSG,4,1,1,4840D6,1,d,t,d,t,,,451.5,87.2,,,-64,,0,0,0,0")
    assert vel.speed_kt == 451.5 and vel.track == 87.2 and vel.vrate == -64 and vel.lat is None
    ident = parse_sbs("MSG,1,1,1,4840D6,1,d,t,d,t,KLM1234 ,,,,,,,,0,0,0,0")
    assert ident.callsign == "KLM1234"


@pytest.mark.parametrize("line", ["", "garbage", "STA,1,2", "MSG,3,1,1,XYZ123,1", "MSG,x,1,1,4840D6,1",
                                  "MSG,3,1,1,4840D6,1,d,t,d,t,,100,,,0,0"])
def test_parse_sbs_rejects_bad_lines(line):
    out = parse_sbs(line)
    assert out is None or out.lat is None  # (0,0) positions are dropped, not logged


def test_tracker_range_and_log_throttle():
    t = AircraftTable(log_interval_s=5.0)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    upd = parse_sbs("MSG,3,1,1,AAAAAA,1,d,t,d,t,,30000,,,52.1,21.0,,,0,0,0,0")
    new, row = t.update(upd, "rtl0", (52.0, 21.0), 100.0, now)
    assert new and row is not None and 10 < row.range_km < 12  # ~11.1 km per 0.1 degree of latitude
    new, row = t.update(upd, "rtl0", (52.0, 21.0), 102.0, now)
    assert not new and row is None  # throttled
    _, row = t.update(upd, "rtl0", (52.0, 21.0), 106.0, now)
    assert row is not None
    assert t.max_range_km == pytest.approx(11.1, abs=0.3)
    assert t.views(107.0)[0].age_s == pytest.approx(1.0)
    assert t.views(500.0) == []  # aged out of the live list ...
    assert t.total_seen() == 1  # ... but still counted


def _row(i, icao="ABCDEF"):
    return PositionRow(datetime(2026, 1, 1, 12, 0, i, tzinfo=timezone.utc), icao, "TST1", 52 + i / 100, 21 + i / 100,
                       30000, 400.0, 90.0, "1000", 52.0, 21.0, 5.5 + i, "rtl0")


def test_exports_and_journal_recovery(tmp_path):
    started = datetime(2026, 1, 1, 12, 0, 0)
    assert adsb_filename(started) == "Spejari_ADSB_2026-01-01_12-00-00.csv"
    j = AdsbJournal(tmp_path, started)
    for i in range(3):
        j.write_row(_row(i))
    j.write_rx(started, 52.0, 21.0)
    j.write_rx(started + timedelta(seconds=5), 52.001, 21.001)
    j.close()
    with open(j.path, "a") as fh:
        fh.write('{"row": {"time_utc"')  # torn last line after a power cut
    rows, rx = read_adsb_journal(j.path)
    assert len(rows) == 3 and len(rx) == 2
    out = recover_adsb_journals(tmp_path)
    assert len(out) == 1 and out[0].suffix == ".csv"
    assert out[0].with_suffix(".geojson").exists() and not j.path.exists()
    header = out[0].read_text().splitlines()[0]
    assert header.startswith("TimeUTC,ICAO,Callsign")
    n = write_adsb_csv(rows, tmp_path / "x.CSV")
    assert n == 3 and (tmp_path / "x.csv").exists() and not (tmp_path / "x.CSV").exists() or sys.platform == "win32"
    geo = write_adsb_geojson(rows, rx, tmp_path / "x.csv")
    assert geo.suffix == ".geojson"


def test_build_command_isolated_ports_and_gain():
    cfg = AdsbConfig(gain="auto", ppm=3, extra_args=["--lat", "52"])
    cmd = build_command("readsb", "/usr/bin/readsb", "1", 30113, cfg)
    assert cmd[:3] == ["/usr/bin/readsb", "--device-type", "rtlsdr"]
    assert cmd[cmd.index("--device") + 1] == "1" and cmd[cmd.index("--net-sbs-port") + 1] == "30113"
    assert "--net-only" not in cmd  # in the dump1090 family this would disable the dongle
    assert "--gain" not in cmd  # readsb defaults to auto gain
    assert cmd[cmd.index("--ppm") + 1] == "3"
    fixed = build_command("readsb", "readsb", "0", 30103, AdsbConfig(gain="40.2"))
    assert fixed[fixed.index("--gain") + 1] == "40.2"
    for flag in ("--net-ro-port", "--net-ri-port", "--net-bo-port", "--net-bi-port"):
        assert cmd[cmd.index(flag) + 1] == "0"  # nothing else listens: several dongles never collide
    assert cmd[-2:] == ["--lat", "52"]
    fa = build_command("dump1090-fa", "dump1090-fa", "0", 30103, AdsbConfig(gain="40.2"))
    assert fa[fa.index("--device-index") + 1] == "0" and fa[fa.index("--gain") + 1] == "40.2"
    mut = build_command("dump1090-mutability", "dump1090-mutability", "2", 30123, AdsbConfig())
    assert mut[mut.index("--device-index") + 1] == "2" and "--net-only" not in mut and "--net-http-port" not in mut
    fa_auto = build_command("dump1090-fa", "dump1090-fa", "0", 30103, AdsbConfig())
    assert fa_auto[fa_auto.index("--gain") + 1] == "-10"


def test_find_decoder_by_path(tmp_path):
    exe = tmp_path / "dump1090-fa"
    exe.write_text("#!/bin/sh\n")
    assert find_decoder(str(exe)) == ("dump1090-fa", str(exe))
    assert find_decoder("definitely-not-installed-xyz") is None


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shebang script as the fake decoder")
def test_managed_mode_runs_one_decoder_per_dongle(tmp_path, monkeypatch):
    """Two fake 'readsb' processes, two dongles, two SBS ports: both feeds reach the aircraft table."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "readsb"
    exe.write_text(f"#!{sys.executable}\n" + (HERE / "fake_decoder.py").read_text().split("\n", 1)[1])
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    cfg = Config()
    cfg.drive.mode = "adsb"
    cfg.adsb.devices = ["0", "1"]
    cfg.adsb.base_port = 31103
    cfg.adsb.log_interval_s = 1.0
    cfg.normalise()
    gps = GpsMonitor(cfg.gps)
    feed = FeedLog(100)
    eng = AdsbEngine(cfg, gps, feed)
    session = eng.begin_session(datetime.now(), None)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            aircraft, dongles, total, _rng, _note = eng.views()
            if total >= 2 and all(d.state == "receiving" for d in dongles):
                break
            time.sleep(0.2)
        assert total == 2, (dongles, eng.note)
        assert {a.icao for a in aircraft} == {"A1B2C0", "A1B2C1"}
        assert {a.callsign for a in aircraft} == {"TST0", "TST1"}
        assert [d.name for d in dongles] == ["rtl0", "rtl1"]
    finally:
        eng.end_session()
    assert len(session.rows) >= 2  # positions were logged (no GPS fix needed for aircraft)


def test_missing_decoder_gives_clear_note(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    cfg = Config()
    cfg.drive.mode = "adsb"
    cfg.adsb.decoder = "readsb"
    cfg.normalise()
    eng = AdsbEngine(cfg, GpsMonitor(cfg.gps), FeedLog(10))
    eng.begin_session(datetime.now(), None)
    try:
        time.sleep(1.0)
        assert "No ADS-B decoder found" in eng.note
        assert eng.views()[1][0].state == "error"
    finally:
        eng.end_session()
