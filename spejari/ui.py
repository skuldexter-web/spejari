"""Rich-based live dashboard and keyboard handling."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from . import themes
from .channels import describe_channels, freq_to_channel
from .config import mode_label
from .models import AdapterView, AdsbDongleView, AircraftView, BleAdapterView, FeedEntry, GpsFix

TAGLINE_FMT = "{glyph}  {tagline}  {glyph}"
PRIVATE = "‹private›"


def feed_name(entry: FeedEntry, privacy: bool) -> str:
    """The SSID / device name to show for a feed row. Privacy mode hides every name (the CSV keeps them)."""
    if privacy:
        return PRIVATE
    if entry.ssid == "":
        return "<hidden>"
    return entry.ssid


def print_banner(console: Console) -> None:
    t = themes.active()
    console.print(Text(t.banner, style=f"bold {t.accent}"))
    console.print(Text(f"{TAGLINE_FMT.format(glyph=t.cli_glyph, tagline=t.tagline)}   v{__version__}", style=t.accent2))
    console.print(Text(f"{themes.CREDIT_GITHUB}  ·  IG {themes.CREDIT_INSTAGRAM}", style=t.dim))
    console.print()


@dataclass
class DashboardView:
    state: str = "IDLE"  # IDLE | RUNNING | STOPPING
    elapsed_s: float = 0.0
    unique_ssids: int = 0
    unique_bssids: int = 0
    hidden: int = 0
    logged: int = 0
    seen_no_fix: int = 0
    gps: GpsFix = field(default_factory=GpsFix)
    session_km: float = 0.0
    adapters: List[AdapterView] = field(default_factory=list)
    max_adapters: int = 10
    wifi_enabled: bool = True
    drive_mode: str = "wifi"
    privacy: bool = False
    uploading: bool = False
    last_upload: str = ""
    ble_enabled: bool = False
    ble_adapters: List[BleAdapterView] = field(default_factory=list)
    ble_devices: int = 0
    ble_logged: int = 0
    ble_note: str = ""
    adsb_enabled: bool = False
    adsb_dongles: List[AdsbDongleView] = field(default_factory=list)
    aircraft: List[AircraftView] = field(default_factory=list)
    adsb_total: int = 0
    adsb_max_range_km: float = 0.0
    adsb_note: str = ""
    feed: List[FeedEntry] = field(default_factory=list)
    last_export: str = ""
    utc_now: Optional[datetime] = None


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def safe_text(value: str, style: str = "") -> Text:
    """SSIDs are attacker-controlled: never feed them through Rich markup."""
    cleaned = "".join(ch if ch.isprintable() else "·" for ch in value)
    return Text(cleaned, style=style, no_wrap=True, overflow="ellipsis")


def rssi_style(rssi: int) -> str:
    return themes.active().rssi_color(rssi)


# ------------------------------------------------------------------------- panels
def _kv_table() -> Table:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style=f"bold {themes.active().accent2}", no_wrap=True, width=10)
    t.add_column(no_wrap=True, overflow="ellipsis")
    return t


def _gps_style(gps: GpsFix) -> str:
    th = themes.active()
    if gps.valid:
        return f"bold {th.ok}"
    return th.warn if gps.connected else th.bad


def _panel(body, title: str) -> Panel:
    th = themes.active()
    return Panel(body, title=f"[bold]{title}[/]", border_style=th.border, box=box.ROUNDED)


def session_panel(v: DashboardView) -> Panel:
    th = themes.active()
    t = _kv_table()
    t.add_row("Mode", Text(mode_label(v.drive_mode) + ("  · PRIVACY" if v.privacy else ""),
                           style=f"bold {th.accent2}"))
    if v.state == "RUNNING":
        state = Text(f"● SCANNING  {fmt_duration(v.elapsed_s)}", style=f"bold {th.ok}")
    elif v.state == "STOPPING":
        state = Text("◌ STOPPING - exporting CSV...", style=f"bold {th.warn}")
    else:
        state = Text("○ IDLE  (press S to start)", style="bold")
    t.add_row("Session", state)
    if v.wifi_enabled:
        t.add_row("SSIDs", Text(f"{v.unique_ssids}", style=f"bold {th.accent}"))
        t.add_row("BSSIDs", Text(f"{v.unique_bssids}  (hidden {v.hidden})"))
    if v.ble_enabled:
        t.add_row("BLE", Text(f"{v.ble_devices} devices", style=f"bold {th.accent}"))
    if v.adsb_enabled:
        t.add_row("Aircraft", Text(f"{v.adsb_total} seen · {len(v.aircraft)} in view", style=f"bold {th.accent}"))
    logged = f"{v.logged} logged"
    if v.seen_no_fix:
        logged += f" · {v.seen_no_fix} hits w/o fix"
    t.add_row("Logged", logged)

    g = v.gps
    t.add_row("GPS", Text(g.status, style=_gps_style(g)))
    if g.valid and g.lat is not None and g.lon is not None:
        t.add_row("Position", f"{g.lat:+.6f}, {g.lon:+.6f}")
        alt = f"{g.alt:.0f} m" if g.alt is not None else "-"
        spd = f"{g.speed_mps * 3.6:.0f} km/h" if g.speed_mps is not None else "-"
        t.add_row("Alt/Speed", f"{alt} · {spd}")
    else:
        t.add_row("Position", Text("-", style="dim"))
        t.add_row("Alt/Speed", Text("-", style="dim"))
    src = g.device or g.source
    hdop = f" · HDOP {g.hdop:.1f}" if g.hdop is not None else ""
    t.add_row("Source", Text(f"{src}{hdop}" if g.connected else "-", style="dim"))
    t.add_row("Distance", Text(f"{v.session_km:.3f} km", style=f"bold {th.accent}"))
    t.add_row("Export", Text(v.last_export or "-", style="dim"))
    if v.uploading or v.last_upload:
        t.add_row("Upload", Text("uploading..." if v.uploading else v.last_upload,
                                 style=th.warn if v.uploading else "dim"))
    return _panel(t, "Status & Metrics")


def adapters_panel(v: DashboardView) -> Panel:
    th = themes.active()
    title = f"Wi-Fi adapters ({len(v.adapters)}/{v.max_adapters})"
    if not v.adapters:
        text = "No scan-capable wireless adapter detected.\nPlug one in - Spejari hot-plugs."
        return _panel(Text(text, style=th.warn), title)
    t = Table(box=None, expand=True, show_header=True, header_style=f"bold {th.accent2}", pad_edge=False, padding=(0, 1))

    def state_style(a: AdapterView) -> str:
        return th.ok if a.state == "scanning" else ("dim" if a.state in ("idle", "waiting") else th.warn)

    t.add_column("Iface", no_wrap=True)
    t.add_column("Driver", no_wrap=True, max_width=10)
    t.add_column("Ch", justify="right", no_wrap=True)
    t.add_column("Assigned channels", ratio=1, no_wrap=True, overflow="ellipsis")
    t.add_column("Now", no_wrap=True, justify="right")
    t.add_column("Hits", justify="right", no_wrap=True)
    t.add_column("State", no_wrap=True)
    for a in v.adapters:
        now = ",".join(str(freq_to_channel(f)) for f in a.current) or "-"
        t.add_row(safe_text(a.tag, th.accent), safe_text(a.driver), str(len(a.channels)),
                  safe_text(describe_channels(a.channels)), now, str(a.hits),
                  Text(a.state, style=state_style(a)))
    total = len({f for a in v.adapters for f in a.channels})
    footer = Text(
        f"Cluster: {len(v.adapters)} adapter(s) share {total} channels "
        f"({' / '.join(str(len(a.channels)) for a in v.adapters)})",
        style="dim",
    )
    return _panel(Group(t, footer), title)


def bluetooth_panel(v: DashboardView) -> Panel:
    th = themes.active()
    t = Table(box=None, expand=True, show_header=True, header_style=f"bold {th.accent2}", pad_edge=False, padding=(0, 1))
    t.add_column("Adapter", no_wrap=True)
    t.add_column("Address", no_wrap=True)
    t.add_column("Devices", justify="right", no_wrap=True)
    t.add_column("Hits", justify="right", no_wrap=True)
    t.add_column("State", no_wrap=True)
    for a in v.ble_adapters:
        style = th.ok if a.state == "scanning" else th.warn
        t.add_row(safe_text(a.name, th.accent), safe_text(a.address, "dim"), str(a.devices), str(a.hits),
                  Text(a.state, style=style))
    if not v.ble_adapters:
        t.add_row(Text("-", style="dim"), "", "", "", Text(v.ble_note or "no Bluetooth adapter", style=th.warn))
    footer = Text(f"{v.ble_devices} unique devices · {v.ble_logged} logged as BLE", style="dim")
    return _panel(Group(t, footer), f"Bluetooth LE ({len(v.ble_adapters)})")


def adsb_panel(v: DashboardView) -> Panel:
    th = themes.active()
    t = Table(box=None, expand=True, show_header=True, header_style=f"bold {th.accent2}", pad_edge=False, padding=(0, 1))
    t.add_column("ICAO", no_wrap=True)
    t.add_column("Callsign", no_wrap=True)
    t.add_column("Alt ft", justify="right", no_wrap=True)
    t.add_column("Kt", justify="right", no_wrap=True)
    t.add_column("Trk", justify="right", no_wrap=True)
    t.add_column("Km", justify="right", no_wrap=True)
    t.add_column("Age", justify="right", no_wrap=True)
    for ac in v.aircraft[:12]:
        t.add_row(
            safe_text(ac.icao, th.accent),
            safe_text(ac.callsign),
            "-" if ac.alt_ft is None else f"{ac.alt_ft:,}",
            "-" if ac.speed_kt is None else f"{ac.speed_kt:.0f}",
            "-" if ac.track is None else f"{ac.track:.0f}",
            "-" if ac.range_km is None else f"{ac.range_km:.0f}",
            f"{ac.age_s:.0f}s",
        )
    if not v.aircraft:
        t.add_row(Text(v.adsb_note or "listening for aircraft...", style=th.warn), "", "", "", "", "", "")
    dongles = " · ".join(f"{d.name}:{d.state} {d.msgs}msg" for d in v.adsb_dongles) or "no dongle"
    footer = Text(
        f"{len(v.aircraft)} in view · {v.adsb_total} seen · max range {v.adsb_max_range_km:.0f} km · {dongles}",
        style="dim",
        no_wrap=True,
        overflow="ellipsis",
    )
    n = len(v.adsb_dongles)
    plural = "" if n == 1 else "s"
    return _panel(Group(t, footer), f"ADS-B aircraft ({n} dongle{plural})")


class FeedRenderable:
    """Renders as many of the newest feed rows as the region allows."""

    def __init__(self, entries: List[FeedEntry], privacy: bool = False) -> None:
        self.entries = entries
        self.privacy = privacy

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        th = themes.active()
        height = options.height or 15
        rows = max(1, height - 1)  # one line for the header
        t = Table(box=None, expand=True, show_header=True, header_style=f"bold {th.accent2}", pad_edge=False, padding=(0, 1))
        t.add_column("Time", no_wrap=True, width=8)
        t.add_column("Evt", no_wrap=True, width=4)
        t.add_column("SSID / name", ratio=1, no_wrap=True, overflow="ellipsis", min_width=10)
        t.add_column("BSSID / address", no_wrap=True, width=17)
        t.add_column("Ch", justify="right", no_wrap=True, width=3)
        t.add_column("Band", no_wrap=True, width=4, overflow="crop")
        t.add_column("RSSI", justify="right", no_wrap=True, width=4)
        t.add_column("Security", no_wrap=True, width=9)
        t.add_column("Source", no_wrap=True, width=12, overflow="ellipsis")
        for e in self.entries[-rows:]:
            ts = datetime.fromtimestamp(e.ts).strftime("%H:%M:%S")
            if e.kind in ("SYS", "WARN"):
                style = th.accent2 if e.kind == "SYS" else f"bold {th.warn}"
                t.add_row(Text(ts, style="dim"), Text(e.kind, style=style), safe_text(e.message, style), "", "", "", "", "", "")
                continue
            new = e.kind == "NEW"
            hidden = e.ssid == "" or self.privacy
            ssid = safe_text(feed_name(e, self.privacy), "italic dim" if hidden else ("bold" if new else ""))
            ble = e.band in ("BLE", "ADSB")
            t.add_row(
                Text(ts, style="dim"),
                Text(e.kind, style=f"bold {th.ok}" if new else "dim"),
                ssid,
                Text(e.bssid, style="dim"),
                "" if ble else str(e.channel),
                ("BLE" if e.band == "BLE" else "ADSB") if ble else f"{e.band}G",
                Text(str(e.rssi), style=rssi_style(e.rssi)),
                safe_text(e.security, th.bad if e.security in ("Open", "WEP") else ""),
                safe_text(e.adapter, "dim"),
            )
        yield t


def render_dashboard(v: DashboardView) -> Layout:
    th = themes.active()
    layout = Layout()
    # the top row holds the status panel plus the main radio of this mode; a second radio gets its own row
    radios = []
    if v.wifi_enabled:
        radios.append(("wifi", max(12, len(v.adapters) + 4)))
    if v.ble_enabled:
        radios.append(("ble", max(7, len(v.ble_adapters) + 5)))
    if v.adsb_enabled:
        radios.append(("adsb", 16))
    top_kind, top_size = radios[0] if radios else ("none", 12)
    rest = radios[1:]
    # status panel: 9 fixed rows + one per radio counter + upload line, plus the two border lines
    status_rows = 9 + (2 if v.wifi_enabled else 0) + int(v.ble_enabled) + int(v.adsb_enabled)
    status_rows += int(bool(v.uploading or v.last_upload))
    parts = [Layout(name="header", size=1), Layout(name="top", size=max(status_rows + 2, top_size))]
    if rest:
        parts.append(Layout(name="extra", size=max(size for _, size in rest)))
    parts += [Layout(name="feed"), Layout(name="footer", size=1)]
    layout.split_column(*parts)
    clock = v.utc_now.strftime("%H:%M:%S UTC") if v.utc_now else ""
    header = Text(justify="left", no_wrap=True, overflow="ellipsis")
    header.append(f" {th.cli_glyph} SPEJARI ", style=f"bold {th.bg} on {th.accent}")
    header.append(f" v{__version__}  {mode_label(v.drive_mode)} · {clock}", style=th.accent2)
    if v.privacy:
        header.append("  PRIVACY ", style=f"bold {th.bg} on {th.warn}")
    header.append(f"   {themes.CREDIT_GITHUB} · IG {themes.CREDIT_INSTAGRAM}", style=th.dim)
    layout["header"].update(header)
    panels = {"wifi": adapters_panel, "ble": bluetooth_panel, "adsb": adsb_panel}
    if radios:
        layout["top"].split_row(Layout(session_panel(v), ratio=5), Layout(panels[top_kind](v), ratio=7))
    else:
        layout["top"].update(session_panel(v))
    if rest:
        layout["extra"].split_row(*[Layout(panels[k](v), ratio=7 if k == "adsb" else 4) for k, _ in rest])
    layout["feed"].update(_panel(FeedRenderable(v.feed, v.privacy), "Live Feed"))
    priv = "[P] privacy " + ("off" if v.privacy else "on")
    if v.state == "RUNNING":
        keys = f"[S] stop & export   {priv}   [Q] quit (exports first)"
    elif v.state == "STOPPING":
        keys = "exporting - please wait..."
    else:
        keys = f"[S] start   {priv}   [U] upload new drives   [Q] quit"
    footer = Text(f" {keys}", style="dim", no_wrap=True, overflow="ellipsis")
    layout["footer"].update(footer)
    return layout


# ---------------------------------------------------------------------- keyboard
class KeyReader:
    """Non-blocking single-key reader; inert when stdin is not an interactive terminal."""

    def __init__(self) -> None:
        self.enabled = bool(sys.stdin) and sys.stdin.isatty()
        self._old = None

    def __enter__(self) -> "KeyReader":
        if self.enabled:
            import termios
            import tty

            self._old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc) -> None:
        if self.enabled and self._old is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)

    def read(self, timeout: float) -> List[str]:
        if not self.enabled:
            time.sleep(timeout)
            return []
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return []
        data = os.read(sys.stdin.fileno(), 32)
        return list(data.decode("utf-8", errors="ignore").lower())
