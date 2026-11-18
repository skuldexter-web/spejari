"""The simulated adapters drive the real Linux engine: iw text -> parser -> clustering -> hot-plug."""

import time

from spejari.config import Config
from spejari.gps import GpsMonitor
from spejari.models import FeedLog
from spejari.parser import parse_iw_scan
from spejari.wifi import make_engine
from spejari.wifi.adapters import AdapterDetector
from spejari.wifi.sim import SimWifi


def test_sim_output_parses_with_the_real_parser():
    world = SimWifi(adapters=2)
    freqs = sorted({ap["freq"] for ap in world.aps})
    aps = parse_iw_scan(world.scan_text("simwlan0", freqs))
    assert len(aps) > 20
    assert {"WPA2", "WPA3", "Open"} & {a.security for a in aps}
    assert all(a.freq in freqs for a in aps)


def test_sim_adapters_are_clustered_and_hot_unplug():
    cfg = Config()
    cfg.simulate_adapters = 3
    cfg.normalise()
    world = SimWifi(3)
    det = AdapterDetector(cfg.scan, world)
    names = [a.ifname for a in det.detect()]
    assert names == ["simwlan0", "simwlan1", "simwlan2"]
    ex = det.detect()[2].executor
    world.set_adapter_count(2)
    rc, _out, err = ex.run(["iw", "dev", "simwlan2", "scan", "flush", "freq", "2412"])
    assert rc != 0 and "No such device" in err


def test_engine_scans_with_simulated_adapters_without_root():
    cfg = Config()
    cfg.simulate_adapters = 2
    cfg.gps.source = "none"
    cfg.normalise()
    eng = make_engine(cfg, GpsMonitor(cfg.gps), FeedLog(100))
    eng.start()
    try:
        deadline = time.monotonic() + 10
        while eng.adapter_count() < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        views = eng.adapter_views()
        assert len(views) == 2
        all_ch = set(views[0].channels) | set(views[1].channels)
        assert not set(views[0].channels) & set(views[1].channels)  # channels are split, not duplicated
        assert len(all_ch) > 20
    finally:
        eng.shutdown()
