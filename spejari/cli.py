"""Command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import List, Optional

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from . import __version__, settings, themes, wifi
from .config import ConfigError, load_config, mode_label, parse_mode
from .exporter import recover_journals
from .gps import list_serial_ports
from .ui import print_banner


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spejari",
        description="Spejari - Wi-Fi (2.4 / 5 GHz), Bluetooth LE and ADS-B war-driving scout for Debian, Kali and "
        "Raspberry Pi OS, with GPS, WiGLE CSV export and uploads to WiGLE / WDGWars.",
    )
    ui = p.add_mutually_exclusive_group()
    ui.add_argument("--cli", action="store_true", help="terminal dashboard (works over SSH)")
    ui.add_argument("--gui", action="store_true", help="desktop window (CustomTkinter)")
    p.add_argument("-m", "--mode", metavar="MODE",
                   help="what to war-drive: wifi | ble | adsb | wifi+ble | all  (any + combination, e.g. ble+adsb)")
    p.add_argument("--privacy", action="store_true", help="privacy mode: hide SSIDs and device names in the live feed")
    p.add_argument("--no-privacy", action="store_true", help="switch a remembered privacy mode off for this run")
    p.add_argument("-c", "--config", help="path to a TOML config file (default: config/spejari.toml)")
    wf = p.add_argument_group("Wi-Fi")
    wf.add_argument("-i", "--interface", action="append", default=[], metavar="IFACE",
                    help="use only this Wi-Fi adapter, e.g. wlan1 (repeatable)")
    wf.add_argument("-x", "--exclude", action="append", default=[], metavar="IFACE",
                    help="never use this Wi-Fi adapter, e.g. the one carrying your SSH / internet link (repeatable)")
    wf.add_argument("-n", "--max-adapters", type=int, metavar="N", help="adapter limit (1-10)")
    wf.add_argument("-b", "--band", choices=("both", "2.4", "5"), help="restrict scanned bands")
    wf.add_argument("--skip-dfs", action="store_true", help="skip 5 GHz DFS/radar channels (52-144)")
    wf.add_argument("--passive", action="store_true", help="passive scans only (never send probe requests)")
    gp = p.add_argument_group("GPS")
    gp.add_argument("--gps", choices=("auto", "gpsd", "serial", "none"), help="GPS source")
    gp.add_argument("--gps-device", metavar="PORT", help="serial GPS device, e.g. /dev/ttyACM0 (implies --gps serial)")
    gp.add_argument("--gpsd", metavar="HOST:PORT", help="gpsd address (default 127.0.0.1:2947)")
    bt = p.add_argument_group("Bluetooth LE (up to 5 adapters)")
    bt.add_argument("--bluetooth", action="store_true", help="add Bluetooth LE to the mode (same as --mode ...+ble)")
    bt.add_argument("--ble-adapter", action="append", default=[], metavar="HCI",
                    help="use only this Bluetooth adapter, e.g. hci1 (repeatable)")
    bt.add_argument("--ble-exclude", action="append", default=[], metavar="HCI",
                    help="never use this Bluetooth adapter, e.g. hci0 (repeatable)")
    bt.add_argument("--ble-max", type=int, metavar="N", help="Bluetooth adapter limit (1-5, default 5)")
    bt.add_argument("--ble-log-random", action="store_true",
                    help="also log random private addresses (they rotate, so this makes a big file)")
    ad = p.add_argument_group("ADS-B (RTL-SDR)")
    ad.add_argument("--adsb", action="store_true", help="add ADS-B to the mode (same as --mode ...+adsb)")
    ad.add_argument("--adsb-device", action="append", default=[], metavar="INDEX",
                    help="RTL-SDR device index to use (repeatable, default 0)")
    ad.add_argument("--adsb-decoder", metavar="NAME|PATH", help="readsb, dump1090-fa, dump1090-mutability, dump1090 or a path")
    ad.add_argument("--adsb-external", metavar="HOST:PORT",
                    help="read SBS (BaseStation) from a decoder that is already running, e.g. 127.0.0.1:30003")
    upg = p.add_argument_group("Upload to WiGLE / WDGWars")
    upg.add_argument("--keys", action="store_true", help="enter your WiGLE / WDGWars API keys (asked, never echoed)")
    upg.add_argument("--test-keys", action="store_true", help="check the stored API keys with both sites, then exit")
    upg.add_argument("--upload", choices=("wigle", "wdgwars", "both"), metavar="TARGET",
                     help="upload drives to wigle | wdgwars | both, then exit (default: every export not uploaded yet)")
    upg.add_argument("--file", action="append", default=[], metavar="CSV",
                     help="with --upload: upload this file instead (repeatable)")
    upg.add_argument("--force", action="store_true", help="with --upload: send files again even if already uploaded")
    upg.add_argument("--auto-upload", choices=("off", "wigle", "wdgwars", "both"), metavar="TARGET",
                     help="upload each drive automatically when it is stopped")
    p.add_argument("--theme", choices=themes.theme_keys(), metavar="THEME",
                   help="colour theme: " + " | ".join(f"{t.key} ({t.label})" for t in themes.THEMES.values()))
    p.add_argument("-o", "--output-dir", metavar="DIR", help="where CSV exports are written")
    p.add_argument("--auto-start", action="store_true", help="start scanning immediately")
    p.add_argument("--simulate", nargs="?", const=3, type=int, metavar="N",
                   help="no hardware or root needed: N simulated Wi-Fi adapters (default 3), GPS drive, BLE devices "
                   "and aircraft (the mode defaults to all)")
    p.add_argument("--list-adapters", action="store_true", help="show detected Wi-Fi adapters and GPS ports, then exit")
    p.add_argument("--list-gps", action="store_true", help="show serial ports that could be a GPS receiver, then exit")
    p.add_argument("--list-bluetooth", action="store_true", help="show Bluetooth adapters and BlueZ status, then exit")
    p.add_argument("--list-sdr", action="store_true", help="show RTL-SDR dongles and ADS-B decoders, then exit")
    p.add_argument("--recover", action="store_true", help="convert journals of crashed sessions into CSV files, then exit")
    p.add_argument("-V", "--version", action="store_true", help="print version and exit")
    return p


def apply_overrides(cfg, args) -> Optional[str]:
    """Apply command-line flags. Returns an error text for a bad --mode."""
    if args.mode:
        try:
            cfg.drive.mode = parse_mode(args.mode)
        except ValueError as exc:
            return f"--mode: {exc}"
    elif args.simulate is not None and not (args.bluetooth or args.adsb):
        cfg.drive.mode = "all"
    if args.bluetooth:
        cfg.set_part("ble", True)
    if args.adsb or args.adsb_device or args.adsb_external:
        cfg.set_part("adsb", True)
    if args.privacy:
        cfg.ui.privacy = True
    if args.no_privacy:
        cfg.ui.privacy = False
    if args.interface:
        cfg.scan.include_interfaces = list(args.interface)
    if args.exclude:
        cfg.scan.exclude_interfaces = list(cfg.scan.exclude_interfaces) + list(args.exclude)
    if args.max_adapters:
        cfg.scan.max_adapters = args.max_adapters
    if args.band:
        cfg.scan.band_2g = args.band in ("both", "2.4")
        cfg.scan.band_5g = args.band in ("both", "5")
    if args.skip_dfs:
        cfg.scan.skip_dfs = True
    if args.passive:
        cfg.scan.passive = True
    if args.gps:
        cfg.gps.source = args.gps
    if args.gps_device:
        cfg.gps.serial_port = args.gps_device
        if not args.gps:
            cfg.gps.source = "serial"
    if args.gpsd:
        host, _, port = args.gpsd.partition(":")
        cfg.gps.gpsd_host = host or cfg.gps.gpsd_host
        if port.isdigit():
            cfg.gps.gpsd_port = int(port)
    if args.output_dir:
        cfg.log.output_dir = args.output_dir
    if args.theme:
        cfg.ui.theme = args.theme
    if args.ble_adapter:
        cfg.bluetooth.include_adapters = list(args.ble_adapter)
    if args.ble_exclude:
        cfg.bluetooth.exclude_adapters = list(cfg.bluetooth.exclude_adapters) + list(args.ble_exclude)
    if args.ble_max:
        cfg.bluetooth.max_adapters = args.ble_max
    if args.ble_log_random:
        cfg.bluetooth.log_random = True
    if args.adsb_device:
        cfg.adsb.devices = list(args.adsb_device)
    if args.adsb_decoder:
        cfg.adsb.decoder = args.adsb_decoder
    if args.adsb_external:
        host, _, port = args.adsb_external.partition(":")
        cfg.adsb.mode = "external"
        cfg.adsb.external_host = host or cfg.adsb.external_host
        if port.isdigit():
            cfg.adsb.external_port = int(port)
    if args.auto_upload:
        cfg.upload.auto = args.auto_upload
    if args.simulate is not None:
        cfg.simulate = True
        cfg.simulate_adapters = max(1, min(10, args.simulate))
        cfg.gps.source = "sim"
    cfg.normalise()
    return None


def apply_saved(cfg, args) -> None:
    """Choices remembered by the GUI, unless a flag sets them for this run."""
    if not args.theme:
        saved = settings.load_saved_theme()
        if saved:
            cfg.ui.theme = saved
    if not (args.mode or args.bluetooth or args.adsb or args.adsb_device or args.adsb_external
            or args.simulate is not None):
        saved = settings.load("mode")
        if saved:
            try:
                cfg.drive.mode = parse_mode(saved)
            except ValueError:
                pass
    if not (args.privacy or args.no_privacy) and settings.load("privacy") is not None:
        cfg.ui.privacy = bool(settings.load("privacy"))
    if not args.auto_upload and settings.load("upload_auto") in ("off", "wigle", "wdgwars", "both"):
        cfg.upload.auto = settings.load("upload_auto")


def list_adapters(cfg, console: Console) -> int:
    from .channels import describe_channels, distribute, freq_to_band, is_dfs
    from .wifi.adapters import AdapterDetector, AdapterError

    print_banner(console)
    sim = None
    if cfg.simulate_adapters:
        from .wifi.sim import SimWifi

        sim = SimWifi(cfg.simulate_adapters)
    try:
        adapters = AdapterDetector(cfg.scan, sim).detect()
    except AdapterError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    if not adapters:
        console.print("[yellow]No scan-capable (managed-mode) wireless adapters found.[/]")
    else:
        t = Table(title="Wireless adapters", header_style="bold")
        for col in ("Iface", "PHY", "Driver", "MAC", "2.4 GHz", "5 GHz", "of which DFS"):
            t.add_column(col)
        for a in adapters:
            g2 = sum(1 for f in a.channels if freq_to_band(f) == "2.4")
            g5 = sum(1 for f in a.channels if freq_to_band(f) == "5")
            dfs = sum(1 for f in a.channels if is_dfs(f))
            t.add_row(a.ifname, a.phy, a.driver, a.mac, str(g2), str(g5), str(dfs))
        console.print(t)
        plan = distribute({a.ifname: a.channels for a in adapters})
        console.print("\n[bold]Channel split with the adapters currently plugged in:[/]")
        for name, freqs in plan.items():
            console.print(f"  [cyan]{name}[/]  {len(freqs):>2} ch  {describe_channels(freqs)}", soft_wrap=True)
    list_gps(console)
    return 0


def list_gps(console: Console) -> int:
    ports = list_serial_ports()
    console.print("\n[bold]Serial ports (possible GPS):[/]")
    if not ports:
        console.print("  none found (/dev/ttyACM*, /dev/ttyUSB*) - gpsd may still provide GPS")
        return 0
    t = Table(header_style="bold")
    for col in ("Port", "USB id", "Description", "u-blox?"):
        t.add_column(col)
    for p in ports:
        usb = f"{int(p['vid']):04X}:{int(p['pid']):04X}" if p["vid"] else "-"
        t.add_row(str(p["port"]), usb, str(p["description"]), "yes" if p["ublox"] else "")
    console.print(t)
    return 0


def list_bluetooth(cfg, console: Console) -> int:
    from .bluetooth import HARD_MAX_BLE, bluez_running, list_ble_adapters

    adapters, note = list_ble_adapters()
    t = Table(title=f"Bluetooth adapters (Spejari uses up to {HARD_MAX_BLE})", header_style="bold")
    for col in ("Name", "Address", "Powered"):
        t.add_column(col)
    for a in adapters:
        powered = a.get("powered", "")
        colour = "green" if powered == "yes" else "yellow"
        t.add_row(a["name"], a.get("address", ""), f"[{colour}]{powered}[/]")
    if adapters:
        console.print(t)
    if note:
        console.print(f"[yellow]{note}[/]")
    if bluez_running():
        console.print("[green]BlueZ (bluetooth service): running[/]")
    else:
        console.print("[yellow]BlueZ (bluetooth service): NOT running - Bluetooth scans will fail.[/]\n"
                      "  Fix:  sudo apt install bluez  &&  sudo systemctl enable --now bluetooth\n"
                      "  (Spejari also tries to start it by itself when it runs as root.)")
    if any(a.get("powered") == "no" for a in adapters):
        console.print("[yellow]Adapters that are down are switched on automatically when Spejari runs as root "
                      "(or: sudo btmgmt --index N power on).[/]")
    return 0


def list_sdr(cfg, console: Console) -> int:
    from .adsb import describe_environment

    for line in describe_environment(cfg.adsb):
        console.print(line)
    return 0


def recover(cfg, console: Console) -> int:
    from .adsb import recover_adsb_journals

    paths = recover_journals(cfg.output_path) + recover_adsb_journals(cfg.output_path)
    if not paths:
        console.print("No unfinished session journals found.")
    for p in paths:
        console.print(f"[green]Recovered:[/] {p}")
    return 0


def setup_keys(console: Console) -> int:
    from . import upload as up

    keys = up.load_keys()
    if not (sys.stdin and sys.stdin.isatty()):
        console.print("[red]--keys needs an interactive terminal (or set WIGLE_API_NAME / WIGLE_API_TOKEN / "
                      "WDGWARS_API_KEY).[/]")
        return 1
    console.print("[bold]API keys[/] - press Enter to keep the current value, '-' to delete it.")
    console.print("  WiGLE:   wigle.net → Account → API Token (API Name + API Token)")
    console.print("  WDGWars: wdgwars.pl → your profile → create an API key\n")

    def ask(label: str, current: str, secret: bool) -> str:
        shown = up.mask(current) if secret else current
        prompt = f"{label} [{shown or 'empty'}]: "
        try:
            value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(1)
        if value == "-":
            return ""
        return value or current

    keys.wigle_name = ask("WiGLE API Name", keys.wigle_name, False)
    keys.wigle_token = ask("WiGLE API Token", keys.wigle_token, True)
    if keys.wigle_name and keys.wigle_token:
        keys.wigle_encoded = ""
    keys.wdgwars_key = ask("WDGWars API key", keys.wdgwars_key, True)
    path = up.save_keys(keys)
    console.print(f"[green]Saved to {path} (permissions 600).[/] Check them with:  ./run.sh --test-keys")
    return 0


def test_keys(console: Console) -> int:
    from . import upload as up

    keys = up.load_keys()
    rc = 0
    for target, check in (("wigle", up.check_wigle), ("wdgwars", up.check_wdgwars)):
        name = "WiGLE" if target == "wigle" else "WDGWars"
        if not keys.has(target):
            console.print(f"[yellow]{name}: no key (run --keys)[/]")
            continue
        ok, msg = check(keys)
        console.print(f"[{'green' if ok else 'red'}]{msg}[/]")
        rc = rc or (0 if ok else 1)
    return rc


def upload_cmd(cfg, args, console: Console) -> int:
    from . import upload as up

    keys = up.load_keys()
    rc = 0

    def log(msg: str, warn: bool = False) -> None:
        console.print(f"[{'yellow' if warn else 'cyan'}]{msg}[/]")

    for target in up.targets_of(args.upload):
        name = "WiGLE" if target == "wigle" else "WDGWars"
        if not keys.has(target):
            console.print(f"[red]{name}: no API key. Run:  ./run.sh --keys[/]")
            rc = 1
            continue
        files = [Path(f) for f in args.file] if args.file else up.pending(cfg.output_path, target, cfg.upload)
        missing = [f for f in files if not f.is_file()]
        for f in missing:
            console.print(f"[red]{f}: file not found[/]")
            rc = 1
        files = [f for f in files if f.is_file()]
        if not files:
            console.print(f"{name}: nothing new to upload in {cfg.output_path}")
            continue
        console.print(f"[bold]{name}:[/] uploading {len(files)} file(s)")
        for r in up.upload_files(files, target, cfg.upload, keys, None if args.file else cfg.output_path, log,
                                 force=args.force):
            if not r.ok:
                rc = 1
    return rc


def choose_mode(args, cfg, console: Console) -> str:
    """CLI or GUI: explicit flag > config default > interactive chooser."""
    if args.gui:
        return "gui"
    if args.cli:
        return "cli"
    if cfg.ui.default_mode in ("cli", "gui"):
        return cfg.ui.default_mode
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not (sys.stdin and sys.stdin.isatty()):  # e.g. double-clicked / desktop launcher: the TUI cannot work
        return "gui" if has_display else "cli"
    console.print("[bold]Choose an interface:[/]")
    console.print("  [cyan]1[/]  CLI   terminal dashboard (works over SSH)")
    console.print("  [cyan]2[/]  GUI   desktop window (CustomTkinter)")
    default = "2" if has_display else "1"
    answer = Prompt.ask("Select", choices=["1", "2", "cli", "gui"], default=default, console=console)
    return "gui" if answer in ("2", "gui") else "cli"


def _use_utf8() -> None:
    """Rich draws box characters and runes: make sure the console encoding can print them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _use_utf8()
    args = build_parser().parse_args(argv)
    console = Console()
    if args.version:
        print(f"Spejari {__version__}")
        return 0
    if not sys.platform.startswith("linux"):
        console.print("[red]Spejari runs on Linux: Debian, Kali, Ubuntu and Raspberry Pi OS.[/]")
        return 1
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    apply_saved(cfg, args)
    problem = apply_overrides(cfg, args)
    if problem:
        console.print(f"[red]{problem}[/]")
        return 2
    themes.set_active(cfg.ui.theme)

    if args.list_gps:
        return list_gps(console)
    if args.list_bluetooth:
        return list_bluetooth(cfg, console)
    if args.list_sdr:
        return list_sdr(cfg, console)
    if args.list_adapters:
        return list_adapters(cfg, console)
    if args.recover:
        return recover(cfg, console)
    if args.keys:
        return setup_keys(console)
    if args.test_keys:
        return test_keys(console)
    if args.upload:
        return upload_cmd(cfg, args, console)

    print_banner(console)
    for warning in cfg.warnings:
        console.print(f"[yellow]config: {warning}[/]")
    if cfg.wifi_on:
        problem = wifi.preflight(cfg)
        if problem:
            console.print(f"[red]{problem}[/]")
            console.print("[dim]No Wi-Fi today? Pick another mode, e.g.  --mode ble  or  --mode adsb[/]")
            return 1
    console.print(f"[bold]Mode:[/] {mode_label(cfg.drive.mode)}" + ("   [bold]privacy on[/]" if cfg.ui.privacy else ""))

    mode = choose_mode(args, cfg, console)
    from .app import App  # imported late so --help/--list work quickly

    try:
        app = App(cfg, auto_start=args.auto_start)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 1
    if mode == "gui":
        try:
            from .gui import run_gui
        except ImportError as exc:
            console.print(f"[red]The GUI needs CustomTkinter and Tk ({exc}).[/]\n"
                          "Install with:  sudo apt install python3-tk   and   .venv/bin/pip install customtkinter "
                          "(or run ./setup.sh), or use the terminal version with --cli.")
            return 1
        return run_gui(app)
    return app.run()
