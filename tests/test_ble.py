import types

from spejari.bluetooth import classify_address, sighting_from_bleak, list_ble_adapters
from spejari.bluetooth.sim import SimBleSource


def test_classify_address_kinds():
    assert classify_address("C0:11:22:33:44:55", "random") is False  # static random: stable, fine to log
    assert classify_address("5A:11:22:33:44:55", "random") is True  # resolvable private (01)
    assert classify_address("1A:11:22:33:44:55", "random") is True  # non-resolvable private (00)
    assert classify_address("5A:11:22:33:44:55", "public") is False  # public addresses may look like anything
    assert classify_address("5A:11:22:33:44:55", None) is False  # unknown -> logged
    assert classify_address("garbage", "random") is False


def _dev(address, name=None, details=None):
    return types.SimpleNamespace(address=address, name=name, details=details)


def _adv(rssi=-60, local_name=None, manu=None, uuids=None, platform_data=None):
    return types.SimpleNamespace(rssi=rssi, local_name=local_name, manufacturer_data=manu or {},
                                 service_uuids=uuids or [], platform_data=platform_data)


def test_sighting_from_bleak_bluez_random():
    d = _dev("5a:11:22:33:44:55", None, {"props": {"AddressType": "random"}})
    s = sighting_from_bleak(d, _adv(-71, "Tile", {0x067C: b"\x01"}, ["fd84"]))
    assert s.address == "5A:11:22:33:44:55" and s.name == "Tile" and s.rssi == -71
    assert s.random is True and s.company == "0x067C" and s.uuids == 1


def test_sighting_from_bleak_bluez_platform_data():
    """bleak 3.x: the advertisement carries (dbus path, props) with AddressType."""
    adv = _adv(-60, "x", platform_data=("/org/bluez/hci0/dev_x", {"AddressType": "random"}))
    assert sighting_from_bleak(_dev("1A:11:22:33:44:55"), adv).random is True
    adv = _adv(-60, "x", platform_data=("/org/bluez/hci0/dev_x", {"AddressType": "public"}))
    assert sighting_from_bleak(_dev("1A:11:22:33:44:55"), adv).random is False


def test_adapter_kwargs_new_and_old_bleak():
    from spejari.bluetooth.engine import _adapter_kwargs

    out = _adapter_kwargs("hci1")
    assert out in ({"bluez": {"adapter": "hci1"}}, {"adapter": "hci1"})


def test_sighting_from_bleak_winrt_style_address_type():
    args = types.SimpleNamespace(bluetooth_address_type=1)  # WinRT: 1 = random
    s = sighting_from_bleak(_dev("1A:00:00:00:00:01", "x"), _adv(-50, platform_data=(None, args)))
    assert s.random is True
    args = types.SimpleNamespace(bluetooth_address_type=0)  # public
    s = sighting_from_bleak(_dev("1A:00:00:00:00:01", "x"), _adv(-50, platform_data=(None, args)))
    assert s.random is False


def test_sighting_from_bleak_never_raises_on_odd_objects():
    s = sighting_from_bleak(object(), object())
    assert s.address == "" and s.rssi == -100


def test_simulated_source_produces_public_and_random():
    src = SimBleSource(seed=3, count=12)
    got = []
    state = {"n": 0}

    def active():
        state["n"] += 1
        return state["n"] < 4

    src.run(got.append, active)
    assert got and {s.random for s in src.devices} == {True, False}


def test_list_adapters_returns_note_or_adapters():
    adapters, note = list_ble_adapters()
    assert isinstance(adapters, list) and isinstance(note, str)


def test_parse_hciconfig_powered_state():
    from spejari.bluetooth.engine import _parse_hciconfig

    out = (
        "hci1:\tType: Primary  Bus: USB\n\tBD Address: 00:E0:4C:2B:11:43  ACL MTU: 1021:8  SCO MTU: 255:12\n"
        "\tDOWN \n\tRX bytes:1 acl:0 sco:0 events:1 errors:0\n"
        "hci0:\tType: Primary  Bus: USB\n\tBD Address: 14:4F:8A:20:99:4F  ACL MTU: 1021:4  SCO MTU: 96:6\n"
        "\tUP RUNNING \n\tRX bytes:99 acl:0 sco:0 events:9 errors:0\n"
    )
    info = _parse_hciconfig(out)
    assert info["hci0"] == {"address": "14:4F:8A:20:99:4F", "powered": "yes"}
    assert info["hci1"] == {"address": "00:E0:4C:2B:11:43", "powered": "no"}


def test_bluez_errors_get_a_clear_fix():
    from spejari.bluetooth.engine import BLUEZ_FIX, friendly_error

    seen = "[org.freedesktop.systemd1.NoSuchUnit] Unit dbus-org.bluez.service not found."
    assert friendly_error(seen) == BLUEZ_FIX
    assert "systemctl enable --now bluetooth" in BLUEZ_FIX
    assert "switched off" in friendly_error("[org.bluez.Error.NotReady] Resource Not Ready")
    assert "rfkill" in friendly_error("Operation blocked by rfkill")


def test_up_to_five_adapters_and_one_error_line(monkeypatch):
    from spejari.bluetooth import engine as be
    from spejari.config import Config
    from spejari.gps import GpsMonitor
    from spejari.models import FeedLog

    fake = [{"name": f"hci{i}", "address": f"00:00:00:00:00:0{i}", "powered": "yes"} for i in range(7)]
    monkeypatch.setattr(be, "list_ble_adapters", lambda: (fake, ""))
    cfg = Config()
    cfg.bluetooth.max_adapters = 99
    cfg.normalise()
    assert cfg.bluetooth.max_adapters == 5
    feed = FeedLog(50)
    eng = be.BleEngine(cfg, GpsMonitor(cfg.gps), feed)
    wanted, _ = eng._wanted()
    assert [a["name"] for a in wanted] == ["hci0", "hci1", "hci2", "hci3", "hci4"]
    eng.simulate = True  # do not try to start a real BlueZ from the test
    eng.report_error("hci0", "boom")
    eng.report_error("hci1", "boom")  # same problem on a second adapter within a minute: no second line
    assert sum("boom" in e.message for e in feed.tail(50)) == 1
