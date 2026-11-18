import re
import sys

import pytest

from spejari import settings, themes
from spejari.cli import apply_overrides, build_parser, main
from spejari.config import load_config
from spejari.ui import DashboardView, render_dashboard, print_banner
from spejari.models import AdapterView, BleAdapterView, AircraftView, AdsbDongleView, FeedEntry

HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_three_named_themes_exist():
    assert [t.label for t in themes.THEMES.values()] == ["Sk7ld", "V1RU5", "cvlt of bahp0mau5"]
    assert list(themes.THEMES) == ["sk7ld", "v1ru5", "cvlt"]


@pytest.mark.parametrize("key", list(themes.THEMES))
def test_every_theme_colour_is_valid_hex(key):
    t = themes.THEMES[key]
    for name, value in t.__dict__.items():
        if name in ("key", "label", "tagline", "glyph", "cli_glyph", "banner"):
            continue
        assert HEX.match(value), (key, name, value)


def test_theme_identities():
    def hue(h):
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        import colorsys
        return colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360

    assert 170 < hue(themes.SK7LD.accent) < 200  # cyan
    assert 250 < hue(themes.SK7LD.accent2) < 280  # purple
    assert 70 < hue(themes.V1RU5.accent) < 100  # lime
    assert 250 < hue(themes.V1RU5.accent2) < 290  # purple
    assert hue(themes.CVLT.accent) < 10 or hue(themes.CVLT.accent) > 350  # blood red


def test_resolve_by_key_label_and_fallback():
    assert themes.resolve("V1RU5").key == "v1ru5"
    assert themes.resolve("cvlt of bahp0mau5").key == "cvlt"
    assert themes.resolve("CVLT").key == "cvlt"
    assert themes.resolve("nope").key == "sk7ld"
    assert themes.set_active("cvlt").key == "cvlt"
    assert themes.active().key == "cvlt"
    themes.set_active("sk7ld")


def test_saved_theme_roundtrip(tmp_path, monkeypatch):
    path = tmp_path / "u.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)
    assert settings.load_saved_theme() is None
    assert settings.save_theme("v1ru5")
    assert settings.load_saved_theme() == "v1ru5"
    path.write_text("not json")
    assert settings.load_saved_theme() is None


def _view(privacy):
    return DashboardView(
        state="RUNNING", elapsed_s=61, unique_ssids=3, privacy=privacy, drive_mode="all",
        adapters=[AdapterView(tag="wlan0", name="wlan0", driver="rtl", channels=[2412, 2437], state="scanning")],
        ble_enabled=True, ble_adapters=[BleAdapterView("hci0", "AA:BB:CC:DD:EE:FF", "scanning", 5, 40)],
        adsb_enabled=True, adsb_dongles=[AdsbDongleView("rtl0", "receiving", 100, 2)],
        aircraft=[AircraftView("ABCDEF", "KLM1", 52.0, 21.0, 30000, 400, 90, "", 5, 1.0, 12.3, "rtl0")],
        feed=[FeedEntry(ts=0, kind="NEW", ssid="SecretHomeNet", bssid="aa:bb", channel=6, band="2.4", rssi=-50,
                        security="WPA2", adapter="wlan0"),
              FeedEntry(ts=0, kind="NEW", ssid="x", bssid="b", band="BLE", rssi=-60, security="random", adapter="hci0"),
              FeedEntry(ts=0, kind="NEW", ssid="KLM1", bssid="ABCDEF", band="ADSB", adapter="rtl0", security="aircraft")],
    )


@pytest.mark.parametrize("key", list(themes.THEMES))
@pytest.mark.parametrize("privacy", [False, True])
def test_dashboard_renders_in_every_theme(key, privacy):
    from rich.console import Console
    themes.set_active(key)
    c = Console(width=200, height=60, record=True, force_terminal=True, color_system="truecolor")
    c.print(render_dashboard(_view(privacy)))
    text = c.export_text()
    assert "SPEJARI" in text and "Bluetooth LE" in text and "ADS-B aircraft" in text
    assert themes.CREDIT_GITHUB in text and themes.CREDIT_INSTAGRAM in text  # the credits are in every theme
    if privacy:
        assert "SecretHomeNet" not in text and "‹private›" in text and "PRIVACY" in text
    else:
        assert "SecretHomeNet" in text
    c2 = Console(width=120, record=True, force_terminal=True)
    print_banner(c2)
    banner = c2.export_text()
    assert themes.CREDIT_GITHUB in banner and themes.CREDIT_INSTAGRAM in banner
    themes.set_active("sk7ld")


@pytest.mark.parametrize("mode", ["wifi", "ble", "adsb", "wifi+ble", "all", "ble+adsb"])
def test_dashboard_renders_every_mode(mode):
    from rich.console import Console
    from spejari.config import mode_parts
    parts = mode_parts(mode)
    v = _view(False)
    v.drive_mode = mode
    v.wifi_enabled, v.ble_enabled, v.adsb_enabled = "wifi" in parts, "ble" in parts, "adsb" in parts
    c = Console(width=160, height=50, record=True, force_terminal=True)
    c.print(render_dashboard(v))
    text = c.export_text()
    assert ("Wi-Fi adapters" in text) == ("wifi" in parts)
    assert ("Bluetooth LE (" in text) == ("ble" in parts)
    assert ("ADS-B aircraft" in text) == ("adsb" in parts)


def test_cli_overrides_for_new_features():
    args = build_parser().parse_args(["--bluetooth", "--ble-exclude", "hci0", "--adsb", "--adsb-device", "0",
                                      "--adsb-device", "1", "--adsb-decoder", "readsb", "--theme", "cvlt",
                                      "--ble-log-random"])
    cfg = load_config()
    apply_overrides(cfg, args)
    assert cfg.drive.mode == "all"
    assert cfg.ble_on and cfg.bluetooth.exclude_adapters == ["hci0"] and cfg.bluetooth.log_random
    assert cfg.adsb_on and cfg.adsb.devices == ["0", "1"] and cfg.adsb.decoder == "readsb"
    assert cfg.ui.theme == "cvlt"
    ext = build_parser().parse_args(["--adsb-external", "10.0.0.5:30003"])
    cfg = load_config()
    apply_overrides(cfg, ext)
    assert cfg.adsb_on and cfg.adsb.mode == "external" and cfg.adsb.external_host == "10.0.0.5"


@pytest.mark.parametrize("flag,expected", [("wifi", "wifi"), ("ble", "ble"), ("adsb", "adsb"), ("bt+wifi", "wifi+ble"),
                                           ("all", "all"), ("adsb,ble", "ble+adsb"), ("wifi+ble+adsb", "all")])
def test_mode_flag(flag, expected):
    cfg = load_config()
    assert apply_overrides(cfg, build_parser().parse_args(["--mode", flag])) is None
    assert cfg.drive.mode == expected


def test_bad_mode_flag_is_reported():
    cfg = load_config()
    assert "unknown mode" in apply_overrides(cfg, build_parser().parse_args(["--mode", "lora"]))


def test_privacy_and_ble_limit_flags():
    cfg = load_config()
    apply_overrides(cfg, build_parser().parse_args(["--privacy", "--ble-max", "9", "--mode", "ble"]))
    assert cfg.ui.privacy and cfg.bluetooth.max_adapters == 5


def test_old_config_enabled_flags_still_work(tmp_path):
    p = tmp_path / "old.toml"
    p.write_text("[bluetooth]\nenabled = true\n[adsb]\nenabled = true\n")
    cfg = load_config(str(p))
    assert cfg.drive.mode == "all" and cfg.warnings == []


def test_simulate_flag_turns_everything_on():
    cfg = load_config()
    apply_overrides(cfg, build_parser().parse_args(["--simulate", "4"]))
    assert cfg.simulate and cfg.simulate_adapters == 4 and cfg.gps.source == "sim"
    assert cfg.drive.mode == "all"
    cfg = load_config()
    apply_overrides(cfg, build_parser().parse_args(["--simulate", "--mode", "ble"]))
    assert cfg.drive.mode == "ble"


def test_version_and_list_commands(capsys):
    assert main(["--version"]) == 0
    assert "3." in capsys.readouterr().out
    assert main(["--list-gps"]) == 0
    assert main(["--list-sdr"]) == 0
    assert main(["--list-bluetooth"]) == 0


def test_bundled_config_loads_without_warnings():
    cfg = load_config()
    assert cfg.warnings == []
    assert cfg.ui.theme == "sk7ld" and cfg.adsb.base_port == 30103 and cfg.bluetooth.max_adapters == 5
