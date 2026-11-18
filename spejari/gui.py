"""CustomTkinter desktop GUI for Spejari (same engine as the terminal dashboard).

Shows Wi-Fi, Bluetooth LE and ADS-B, switches drive mode, privacy mode and colour theme
(Sk7ld, V1RU5, cvlt of bahp0mau5) while it runs, and uploads drives to WiGLE / WDGWars.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

import customtkinter as ctk

from . import __version__, settings, themes
from . import upload as up
from .app import IDLE, RUNNING, STOPPING, App
from .channels import describe_channels, freq_to_channel
from .config import HARD_MAX_ADAPTERS, MODE_LABELS, mode_label
from .gps import list_serial_ports
from .ui import PRIVATE, fmt_duration

BAND_CHOICES = ["Both: 2.4 + 5 GHz", "2.4 GHz only", "5 GHz only"]
SCAN_CHOICES = ["Active (probe requests)", "Passive (listen only)"]
MODE_CHOICES = list(MODE_LABELS.values())
MODE_BY_LABEL = {v: k for k, v in MODE_LABELS.items()}
AUTO_UPLOAD = {"Off": "off", "WiGLE": "wigle", "WDGWars": "wdgwars", "WiGLE + WDGWars": "both"}
GPS_SOURCES = ["auto", "gpsd", "serial", "none"]
FIX_CHOICES = ["2D or better", "3D fix only"]
AUTO_DEVICE = "auto-detect (u-blox first)"
ADSB_MODES = ["RTL-SDR dongle(s)", "External decoder (SBS feed)"]
ADSB_DECODERS = ["auto", "readsb", "dump1090-fa", "dump1090-mutability", "dump1090"]
FEED_LIMIT = 1000


def natural_key(text: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def _clean(text: str) -> str:
    """Replace control characters (SSIDs are attacker-controlled)."""
    return "".join(ch if ch.isprintable() else "·" for ch in text)


class SpejariGUI(ctk.CTk):
    def __init__(self, app: App) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        super().__init__()
        self.app = app
        self.cfg = app.cfg
        self.t = themes.set_active(self.cfg.ui.theme)
        self.title(f"Spejari {__version__}  -  Wi-Fi · Bluetooth LE · ADS-B war-driving scout")
        self.geometry("1360x900")
        self.minsize(1120, 720)
        self._include_converted = False
        self._gps_apply_job: Optional[str] = None
        self._port_map: Dict[str, str] = {AUTO_DEVICE: ""}
        self._was_state = IDLE
        self._was_uploading = False
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_all()
        self.after(250, self._tick)

    # ============================================================== (re)build
    def _build_all(self) -> None:
        """Build every widget from the active theme. Called at start and after a theme change."""
        self.t = themes.active()
        for child in self.winfo_children():
            child.destroy()
        self._feed_total = 0
        self._adapter_sig: Optional[tuple] = None
        self._ble_sig: Optional[tuple] = None
        self._air_sig: Optional[tuple] = None
        self._adapter_rows: Dict[str, Dict[str, ctk.CTkBaseClass]] = {}
        self._adapter_vars: Dict[str, tk.BooleanVar] = {}
        self.configure(fg_color=self.t.bg)
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self._build_statusbar()
        self._refresh_ports()
        self._load_options_into_widgets()

    # ------------------------------------------------------------------ sidebar
    def _section(self, parent, text: str) -> None:
        ctk.CTkLabel(parent, text=text.upper(), text_color=self.t.accent2, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(fill="x", padx=14, pady=(16, 4))

    def _menu(self, parent, label: str, values: List[str], command) -> ctk.CTkOptionMenu:
        t = self.t
        ctk.CTkLabel(parent, text=label, text_color=t.dim, anchor="w").pack(fill="x", padx=14, pady=(6, 0))
        menu = ctk.CTkOptionMenu(parent, values=values, command=command, fg_color=t.card2, text_color=t.text,
                                 button_color=t.btn, button_hover_color=t.btn_hover, dynamic_resizing=False,
                                 dropdown_fg_color=t.card2, dropdown_text_color=t.text,
                                 dropdown_hover_color=t.btn_hover)
        menu.pack(fill="x", padx=14, pady=(2, 0))
        return menu

    def _switch(self, parent, text: str, var: tk.BooleanVar, command, pady=(10, 0)) -> ctk.CTkSwitch:
        t = self.t
        sw = ctk.CTkSwitch(parent, text=text, variable=var, command=command, progress_color=t.accent2,
                           button_color=t.text, button_hover_color=t.accent, text_color=t.text)
        sw.pack(fill="x", padx=14, pady=pady)
        return sw

    def _button(self, parent, text: str, command, **kw) -> ctk.CTkButton:
        t = self.t
        opts = dict(height=30, fg_color=t.card2, hover_color=t.btn_hover, text_color=t.text, command=command)
        opts.update(kw)
        return ctk.CTkButton(parent, text=text, **opts)

    def _build_sidebar(self) -> None:
        t = self.t
        side = ctk.CTkScrollableFrame(self, width=290, fg_color=t.card, corner_radius=0,
                                      scrollbar_button_color=t.btn, scrollbar_button_hover_color=t.btn_hover)
        side.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self._sidebar = side

        ctk.CTkLabel(side, text=f"{t.glyph} SPEJARI", text_color=t.accent,
                     font=ctk.CTkFont(size=28, weight="bold")).pack(padx=14, pady=(14, 0), anchor="w")
        ctk.CTkLabel(side, text=f"{t.tagline}  ·  v{__version__}", text_color=t.dim, anchor="w",
                     justify="left", wraplength=260, font=ctk.CTkFont(size=11)).pack(padx=14, anchor="w")
        self._credits(side)

        self.btn_start = ctk.CTkButton(side, text="▶  Start session", height=46, fg_color=t.go,
                                       hover_color=t.go_hover, text_color=t.go_text,
                                       font=ctk.CTkFont(size=15, weight="bold"), command=self._toggle_session)
        self.btn_start.pack(fill="x", padx=14, pady=(18, 0))

        self._section(side, "War-drive mode")
        self.menu_mode = self._menu(side, "What to collect", MODE_CHOICES, self._on_mode)
        self.switch_privacy_var = tk.BooleanVar(value=False)
        self.switch_privacy = self._switch(side, "Privacy mode (hide names in feed)", self.switch_privacy_var,
                                           self._on_privacy, (10, 0))

        self._section(side, "Theme")
        self.menu_theme = self._menu(side, "Colour scheme", themes.theme_labels(), self._on_theme)
        self.menu_theme.set(t.label)

        self._section(side, "Wi-Fi options")
        self.menu_band = self._menu(side, "Band", BAND_CHOICES, self._on_band)
        self.menu_scan = self._menu(side, "Scan type", SCAN_CHOICES, self._on_scan_type)
        self.menu_max = self._menu(side, f"Max adapters (1-{HARD_MAX_ADAPTERS})",
                                   [str(i) for i in range(1, HARD_MAX_ADAPTERS + 1)], self._on_max)
        self.switch_dfs_var = tk.BooleanVar(value=False)
        self.switch_dfs = self._switch(side, "Skip 5 GHz DFS channels", self.switch_dfs_var, self._on_dfs, (12, 0))

        self._section(side, "Bluetooth LE (up to 5 adapters)")
        self.switch_ble_random_var = tk.BooleanVar(value=False)
        self.switch_ble_random = self._switch(side, "Also log random addresses", self.switch_ble_random_var,
                                              self._on_ble_random, (4, 0))
        ctk.CTkLabel(side, text="Random private addresses rotate every few minutes; leave this off to keep the "
                     "file small.", text_color=t.dim, anchor="w", justify="left", wraplength=250,
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(4, 0))

        self._section(side, "ADS-B (RTL-SDR)")
        self.menu_adsb_mode = self._menu(side, "Source", ADSB_MODES, self._on_adsb_opts)
        self.menu_adsb_decoder = self._menu(side, "Decoder", ADSB_DECODERS, self._on_adsb_opts)
        ctk.CTkLabel(side, text="Dongle indexes (comma separated)", text_color=t.dim, anchor="w").pack(
            fill="x", padx=14, pady=(6, 0))
        self.entry_adsb_devices = ctk.CTkEntry(side, fg_color=t.card2, text_color=t.text, border_color=t.border,
                                               placeholder_text="0")
        self.entry_adsb_devices.pack(fill="x", padx=14, pady=(2, 0))
        self.entry_adsb_devices.bind("<FocusOut>", lambda _e: self._on_adsb_opts())
        self.entry_adsb_devices.bind("<Return>", lambda _e: self._on_adsb_opts())

        self._section(side, "GPS")
        self.menu_gps = self._menu(side, "Source", GPS_SOURCES, self._on_gps_changed)
        self.menu_port = self._menu(side, "Serial device (u-blox)", [AUTO_DEVICE], self._on_gps_changed)
        self.btn_ports = self._button(side, "↻  Rescan serial ports", self._refresh_ports, height=28)
        self.btn_ports.pack(fill="x", padx=14, pady=(6, 0))
        self.menu_fix = self._menu(side, "Required fix quality", FIX_CHOICES, self._on_fix)

        self._section(side, "Upload (WiGLE / WDGWars)")
        self.lbl_keys = ctk.CTkLabel(side, text="", text_color=t.dim, anchor="w", justify="left", wraplength=260,
                                     font=ctk.CTkFont(size=11))
        self.lbl_keys.pack(fill="x", padx=14)
        self.btn_keys = self._button(side, "🔑  API keys…", self._open_keys, height=28)
        self.btn_keys.pack(fill="x", padx=14, pady=(6, 0))
        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        self.btn_up_wigle = self._button(row, "⇪ WiGLE", lambda: self._upload("wigle"))
        self.btn_up_wigle.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_up_wdg = self._button(row, "⇪ WDGWars", lambda: self._upload("wdgwars"))
        self.btn_up_wdg.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkLabel(side, text="Uploads every drive not uploaded yet (ADS-B files go to WDGWars only).",
                     text_color=t.dim, anchor="w", justify="left", wraplength=250,
                     font=ctk.CTkFont(size=11)).pack(fill="x", padx=14, pady=(4, 0))
        self.menu_auto = self._menu(side, "Auto-upload after each drive", list(AUTO_UPLOAD), self._on_auto_upload)
        self._refresh_keys_label()

        self._section(side, "Output (.csv)")
        self.lbl_outdir = ctk.CTkLabel(side, text="", text_color=t.text, anchor="w", justify="left", wraplength=250)
        self.lbl_outdir.pack(fill="x", padx=14)
        row = ctk.CTkFrame(side, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(6, 0))
        row.grid_columnconfigure((0, 1), weight=1)
        self.btn_browse = self._button(row, "Browse…", self._browse)
        self.btn_browse.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._button(row, "Open folder", self._open_folder).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ctk.CTkButton(side, text="Quit", height=34, fg_color="transparent", border_width=1, border_color=t.dim,
                      text_color=t.text, hover_color=t.card2, command=self._on_close).pack(
            fill="x", padx=14, pady=(26, 14))

    def _credits(self, parent) -> None:
        """The maker's GitHub and Instagram, in every theme."""
        t = self.t
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", padx=14, pady=(6, 0))
        for text, url in ((f"{t.cli_glyph}  {themes.CREDIT_GITHUB}", themes.CREDIT_GITHUB_URL),
                          (f"{t.cli_glyph}  IG {themes.CREDIT_INSTAGRAM}", themes.CREDIT_INSTAGRAM_URL)):
            lbl = ctk.CTkLabel(box, text=text, text_color=t.accent2, anchor="w", cursor="hand2",
                               font=ctk.CTkFont(size=12, weight="bold"))
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda _e, u=url: self._open_url(u))

    def _open_url(self, url: str) -> None:
        cmd = ["xdg-open", url]
        user = os.environ.get("SUDO_USER")
        if hasattr(os, "geteuid") and os.geteuid() == 0 and user:
            cmd = ["runuser", "-u", user, "--"] + cmd  # a browser should not run as root
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self._set_status(url, self.t.dim)

    # --------------------------------------------------------------------- main
    def _build_main(self) -> None:
        t = self.t
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=14, pady=(14, 6))
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)
        main.grid_rowconfigure(3, weight=2)

        cards = ctk.CTkFrame(main, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew")
        self._cards: Dict[str, ctk.CTkLabel] = {}
        spec = [("session", "SESSION"), ("ssids", "UNIQUE SSIDs"), ("bssids", "BSSIDs"), ("logged", "ROWS LOGGED"),
                ("ble", "BLE DEVICES"), ("air", "AIRCRAFT"), ("km", "DISTANCE (km)"), ("gps", "GPS")]
        for i, (key, caption) in enumerate(spec):
            cards.grid_columnconfigure(i, weight=1, uniform="card")
            card = ctk.CTkFrame(cards, fg_color=t.card, corner_radius=10, border_width=1, border_color=t.card2)
            card.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0 if i == len(spec) - 1 else 4))
            value = ctk.CTkLabel(card, text="-", text_color=t.accent if key in ("ssids", "km") else t.text,
                                 font=ctk.CTkFont(size=19, weight="bold"))
            value.pack(padx=6, pady=(10, 0))
            ctk.CTkLabel(card, text=caption, text_color=t.dim, font=ctk.CTkFont(size=9)).pack(pady=(0, 10))
            self._cards[key] = value

        self.lbl_gps = ctk.CTkLabel(main, text="", text_color=t.dim, anchor="w", font=ctk.CTkFont(size=12))
        self.lbl_gps.grid(row=1, column=0, sticky="ew", pady=(8, 4))

        # tabs: Wi-Fi adapters / Bluetooth LE / ADS-B aircraft
        self.tabs = ctk.CTkTabview(main, fg_color=t.card, corner_radius=10, height=250,
                                   segmented_button_fg_color=t.card2, segmented_button_selected_color=t.border,
                                   segmented_button_selected_hover_color=t.border,
                                   segmented_button_unselected_color=t.card2,
                                   segmented_button_unselected_hover_color=t.btn_hover, text_color=t.text)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        wifi_tab = self.tabs.add("Wi-Fi adapters")
        ble_tab = self.tabs.add("Bluetooth LE")
        air_tab = self.tabs.add("ADS-B aircraft")
        wifi_tab.grid_columnconfigure(0, weight=1)
        wifi_tab.grid_rowconfigure(1, weight=1)
        self.lbl_adapters_title = ctk.CTkLabel(wifi_tab, text="Adapters", text_color=t.accent2, anchor="w",
                                               font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_adapters_title.grid(row=0, column=0, sticky="ew")
        self.adapter_frame = ctk.CTkScrollableFrame(wifi_tab, fg_color=t.card, corner_radius=0,
                                                    scrollbar_button_color=t.btn)
        self.adapter_frame.grid(row=1, column=0, sticky="nsew")
        for col, weight in enumerate((0, 0, 0, 0, 1, 0, 0, 0)):
            self.adapter_frame.grid_columnconfigure(col, weight=weight)

        ble_tab.grid_columnconfigure(0, weight=1)
        ble_tab.grid_rowconfigure(1, weight=1)
        self.lbl_ble_title = ctk.CTkLabel(ble_tab, text="", text_color=t.accent2, anchor="w",
                                          font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_ble_title.grid(row=0, column=0, sticky="ew")
        self.ble_frame = ctk.CTkScrollableFrame(ble_tab, fg_color=t.card, corner_radius=0,
                                                scrollbar_button_color=t.btn)
        self.ble_frame.grid(row=1, column=0, sticky="nsew")

        air_tab.grid_columnconfigure(0, weight=1)
        air_tab.grid_rowconfigure(1, weight=1)
        self.lbl_air_title = ctk.CTkLabel(air_tab, text="", text_color=t.accent2, anchor="w",
                                          font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_air_title.grid(row=0, column=0, sticky="ew")
        self._style_tree()
        cols = ("icao", "call", "alt", "kt", "trk", "km", "age", "dongle")
        heads = {"icao": "ICAO", "call": "Callsign", "alt": "Alt ft", "kt": "Kt", "trk": "Trk", "km": "Range km",
                 "age": "Age", "dongle": "Dongle"}
        self.air_tree = ttk.Treeview(air_tab, columns=cols, show="headings", style="Spejari.Treeview",
                                     selectmode="browse")
        for c in cols:
            self.air_tree.heading(c, text=heads[c], anchor="w")
            self.air_tree.column(c, width=90, minwidth=40, stretch=(c == "call"), anchor="w")
        self.air_tree.grid(row=1, column=0, sticky="nsew")

        # live feed
        feed_box = ctk.CTkFrame(main, fg_color=t.card, corner_radius=10)
        feed_box.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        feed_box.grid_columnconfigure(0, weight=1)
        feed_box.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(feed_box, text="LIVE FEED", text_color=t.accent2, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        cols = ("time", "evt", "ssid", "bssid", "ch", "band", "rssi", "sec", "iface")
        self.tree = ttk.Treeview(feed_box, columns=cols, show="headings", style="Spejari.Treeview",
                                 selectmode="browse")
        widths = {"time": 76, "evt": 48, "ssid": 240, "bssid": 156, "ch": 40, "band": 50, "rssi": 55,
                  "sec": 120, "iface": 112}
        heads = {"time": "Time", "evt": "Evt", "ssid": "SSID / name", "bssid": "BSSID / address", "ch": "Ch",
                 "band": "Band", "rssi": "RSSI", "sec": "Security", "iface": "Source"}
        for c in cols:
            self.tree.heading(c, text=heads[c], anchor="w")
            self.tree.column(c, width=widths[c], minwidth=40, stretch=(c == "ssid"), anchor="w")
        self.tree.tag_configure("NEW", foreground=t.ok)
        self.tree.tag_configure("UPD", foreground=t.dim)
        self.tree.tag_configure("OPEN", foreground=t.bad)
        self.tree.tag_configure("SYS", foreground=t.accent2)
        self.tree.tag_configure("WARN", foreground=t.warn)
        bar = ctk.CTkScrollbar(feed_box, command=self.tree.yview, button_color=t.btn,
                               button_hover_color=t.btn_hover)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))
        bar.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=(0, 8))

    def _style_tree(self) -> None:
        t = self.t
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Spejari.Treeview", background=t.card, fieldbackground=t.card, foreground=t.text,
                        rowheight=22, borderwidth=0, bordercolor=t.card, lightcolor=t.card, darkcolor=t.card,
                        font="TkFixedFont")
        style.configure("Spejari.Treeview.Heading", background=t.card2, foreground=t.accent2, relief="flat",
                        font=("TkDefaultFont", 10, "bold"))
        style.map("Spejari.Treeview", background=[("selected", t.sel)], foreground=[("selected", t.text)])
        style.map("Spejari.Treeview.Heading", background=[("active", t.card2)])

    def _build_statusbar(self) -> None:
        t = self.t
        bar = ctk.CTkFrame(self, fg_color=t.card, corner_radius=0, height=28)
        bar.grid(row=1, column=1, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="Ready. Pick a mode and your options, then press Start session.",
                                   text_color=t.dim, anchor="w", fg_color=t.card, corner_radius=0, height=28)
        self.status.grid(row=0, column=0, sticky="ew")
        credit = ctk.CTkLabel(bar, text=t.credit_line(), text_color=t.accent2, fg_color=t.card, cursor="hand2",
                              font=ctk.CTkFont(size=11, weight="bold"))
        credit.grid(row=0, column=1, sticky="e", padx=12)
        credit.bind("<Button-1>", lambda _e: self._open_url(themes.CREDIT_GITHUB_URL))

    # ----------------------------------------------------------- option handlers
    def _load_options_into_widgets(self) -> None:
        s, g, b, a = self.cfg.scan, self.cfg.gps, self.cfg.bluetooth, self.cfg.adsb
        if s.band_2g and s.band_5g:
            self.menu_band.set(BAND_CHOICES[0])
        else:
            self.menu_band.set(BAND_CHOICES[1] if s.band_2g else BAND_CHOICES[2])
        self.menu_scan.set(SCAN_CHOICES[1] if s.passive else SCAN_CHOICES[0])
        self.switch_dfs_var.set(bool(s.skip_dfs))
        self.menu_max.set(str(s.max_adapters))
        self.menu_mode.set(mode_label(self.cfg.drive.mode))
        self.switch_privacy_var.set(bool(self.cfg.ui.privacy))
        self.menu_auto.set(next((k for k, v in AUTO_UPLOAD.items() if v == self.cfg.upload.auto), "Off"))
        self.switch_ble_random_var.set(bool(b.log_random))
        self.menu_adsb_mode.set(ADSB_MODES[1] if a.mode == "external" else ADSB_MODES[0])
        self.menu_adsb_decoder.set(a.decoder if a.decoder in ADSB_DECODERS else "auto")
        self.entry_adsb_devices.delete(0, "end")
        self.entry_adsb_devices.insert(0, ",".join(a.devices))
        self.menu_gps.set(g.source if g.source in GPS_SOURCES else "auto")
        self.menu_fix.set(FIX_CHOICES[1] if g.min_fix_mode >= 3 else FIX_CHOICES[0])
        self._update_outdir_label()
        for label, port in self._port_map.items():
            if port and port == g.serial_port:
                self.menu_port.set(label)

    def _on_theme(self, label: str) -> None:
        theme = themes.set_active(label)
        self.cfg.ui.theme = theme.key
        settings.save_theme(theme.key)
        self._build_all()
        self._set_status(f"Theme: {theme.label}", self.t.dim)

    def _on_band(self, choice: str) -> None:
        self.cfg.scan.band_2g = choice != BAND_CHOICES[2]
        self.cfg.scan.band_5g = choice != BAND_CHOICES[1]

    def _on_scan_type(self, choice: str) -> None:
        self.cfg.scan.passive = choice == SCAN_CHOICES[1]

    def _on_max(self, choice: str) -> None:
        self.cfg.scan.max_adapters = int(choice)

    def _on_dfs(self) -> None:
        self.cfg.scan.skip_dfs = bool(self.switch_dfs_var.get())

    def _on_mode(self, label: str) -> None:
        mode = MODE_BY_LABEL.get(label)
        if mode is None:
            return
        if self.app.state != IDLE:
            self.menu_mode.set(mode_label(self.cfg.drive.mode))
            return
        if "adsb" in mode or mode == "all":
            self._on_adsb_opts()
        self.app.set_mode(mode)
        settings.save("mode", self.cfg.drive.mode)
        first = {"wifi": "Wi-Fi adapters", "ble": "Bluetooth LE", "adsb": "ADS-B aircraft"}
        self.tabs.set(first.get(mode.split("+")[0], "Wi-Fi adapters") if mode != "all" else "Wi-Fi adapters")
        self._set_status(f"Mode: {label}", self.t.dim)

    def _on_privacy(self) -> None:
        self.cfg.ui.privacy = bool(self.switch_privacy_var.get())
        settings.save("privacy", self.cfg.ui.privacy)
        self._redraw_feed()
        self._set_status("Privacy mode on: SSIDs and device names are hidden in the feed (the CSV keeps them)."
                         if self.cfg.ui.privacy else "Privacy mode off.", self.t.dim)

    def _on_auto_upload(self, label: str) -> None:
        self.cfg.upload.auto = AUTO_UPLOAD.get(label, "off")
        settings.save("upload_auto", self.cfg.upload.auto)

    # ------------------------------------------------------------------- upload
    def _refresh_keys_label(self) -> None:
        keys = up.load_keys()
        parts = [("WiGLE", keys.has("wigle")), ("WDGWars", keys.has("wdgwars"))]
        self.lbl_keys.configure(text="   ".join(f"{n}: {'key set ✓' if ok else 'no key'}" for n, ok in parts),
                                text_color=self.t.ok if all(ok for _, ok in parts) else self.t.dim)

    def _upload(self, target: str) -> None:
        if self.app.state != IDLE:
            self._set_status("Stop the drive first, then upload.", self.t.warn)
            return
        if not up.load_keys().has(target):
            self._open_keys()
            return
        if self.app.upload(None, target):
            self._set_status(f"Uploading to {'WiGLE' if target == 'wigle' else 'WDGWars'} - see the live feed.",
                             self.t.warn)

    def _open_keys(self) -> None:
        KeysDialog(self)

    def _on_ble_random(self) -> None:
        self.cfg.bluetooth.log_random = bool(self.switch_ble_random_var.get())

    def _on_adsb_opts(self, _choice: str = "") -> None:
        a = self.cfg.adsb
        a.mode = "external" if self.menu_adsb_mode.get() == ADSB_MODES[1] else "managed"
        a.decoder = self.menu_adsb_decoder.get()
        devices = [d.strip() for d in self.entry_adsb_devices.get().split(",") if d.strip().isdigit()]
        a.devices = devices or ["0"]

    def _on_fix(self, choice: str) -> None:
        self.cfg.gps.min_fix_mode = 3 if choice == FIX_CHOICES[1] else 2

    def _on_gps_changed(self, _choice: str = "") -> None:
        self.cfg.gps.source = self.menu_gps.get()
        self.cfg.gps.serial_port = self._port_map.get(self.menu_port.get(), "")
        if self._gps_apply_job is not None:
            self.after_cancel(self._gps_apply_job)
        self._gps_apply_job = self.after(500, self._apply_gps)

    def _apply_gps(self) -> None:
        self._gps_apply_job = None
        if self.app.state == IDLE:
            self.app.restart_gps()
            self._set_status("GPS settings applied.", self.t.dim)

    def _refresh_ports(self) -> None:
        self._port_map = {AUTO_DEVICE: ""}
        for p in list_serial_ports():
            desc = str(p["description"])
            tag = "  [u-blox]" if p["ublox"] else ""
            self._port_map[f"{p['port']} - {desc}{tag}" if desc else f"{p['port']}{tag}"] = str(p["port"])
        current = self.menu_port.get() if hasattr(self, "menu_port") else AUTO_DEVICE
        self.menu_port.configure(values=list(self._port_map))
        self.menu_port.set(current if current in self._port_map else AUTO_DEVICE)

    def _update_outdir_label(self) -> None:
        self.lbl_outdir.configure(text=str(self.cfg.output_path))

    def _browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.cfg.output_path), title="Choose export folder")
        if chosen:
            self.cfg.log.output_dir = chosen
            self._update_outdir_label()

    def _open_folder(self) -> None:
        path = self.cfg.output_path
        path.mkdir(parents=True, exist_ok=True)
        try:
            cmd = ["xdg-open", str(path)]
            user = os.environ.get("SUDO_USER")
            if hasattr(os, "geteuid") and os.geteuid() == 0 and user:
                cmd = ["runuser", "-u", user, "--"] + cmd  # open it as the desktop user, not root
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            messagebox.showinfo("Export folder", str(path))

    # ------------------------------------------------------------------ session
    def _toggle_session(self) -> None:
        if self.app.state == IDLE:
            self.app.start_session()
        elif self.app.state == RUNNING:
            self.app.stop_session()

    def _on_close(self) -> None:
        if self.app.state == RUNNING:
            if not messagebox.askyesno("Spejari", "A session is running.\nStop it, export the files and quit?"):
                return
            self.app.stop_session(then_quit=True)
            self._set_status("Stopping and exporting before quit...", self.t.warn)
            return
        self.app.request_quit()

    def _set_status(self, text: str, color: Optional[str] = None) -> None:
        self.status.configure(text="  " + text, text_color=color or self.t.dim)

    # --------------------------------------------------------------------- tick
    @staticmethod
    def _cfg(widget, **kw) -> None:
        """configure() only when a value actually changed (avoids redraw flicker)."""
        last = getattr(widget, "_sp_kw", None)
        if last is None:
            last = widget._sp_kw = {}
        changed = {k: v for k, v in kw.items() if last.get(k) != v}
        if changed:
            last.update(changed)
            widget.configure(**changed)

    def _set(self, widget, text: str, color: Optional[str] = None) -> None:
        if color is None:
            self._cfg(widget, text=text)
        else:
            self._cfg(widget, text=text, text_color=color)

    def _tick(self) -> None:
        try:
            self._update()
        except tk.TclError:
            return  # window is being destroyed
        if self.app._quit_requested:
            if self.app.state == RUNNING:
                self.app.stop_session(then_quit=True)
            elif self.app.state == IDLE:
                self.destroy()
                return
        self.after(250, self._tick)

    def _update(self) -> None:
        app, t = self.app, self.t
        v = app.view()
        gps = v.gps

        # cards
        if v.state == RUNNING:
            self._set(self._cards["session"], f"● {fmt_duration(v.elapsed_s)}", t.ok)
        elif v.state == STOPPING:
            self._set(self._cards["session"], "exporting…", t.warn)
        else:
            self._set(self._cards["session"], "○ idle", t.text)
        wifi_col = t.text if v.wifi_enabled else t.dim
        self._set(self._cards["ssids"], str(v.unique_ssids) if v.wifi_enabled else "off",
                  t.accent if v.wifi_enabled else t.dim)
        self._set(self._cards["bssids"], str(v.unique_bssids) if v.wifi_enabled else "off", wifi_col)
        self._set(self._cards["logged"], str(v.logged))
        self._set(self._cards["ble"], str(v.ble_devices) if v.ble_enabled else "off", t.text if v.ble_enabled else t.dim)
        self._set(self._cards["air"], str(len(v.aircraft)) if v.adsb_enabled else "off",
                  t.text if v.adsb_enabled else t.dim)
        self._set(self._cards["km"], f"{v.session_km:.3f}")
        gps_color = t.ok if gps.valid else (t.warn if gps.connected else t.bad)
        self._set(self._cards["gps"], gps.status.split(" - ")[0], gps_color)

        # GPS detail line
        if gps.valid and gps.lat is not None and gps.lon is not None:
            alt = f"{gps.alt:.0f} m" if gps.alt is not None else "-"
            spd = f"{gps.speed_mps * 3.6:.0f} km/h" if gps.speed_mps is not None else "-"
            hdop = f"  ·  HDOP {gps.hdop:.1f}" if gps.hdop is not None else ""
            detail = (f"{gps.lat:+.6f}, {gps.lon:+.6f}   ·   alt {alt}   ·   {spd}   ·   "
                      f"sats {gps.sats_used}/{gps.sats_seen}{hdop}   ·   {gps.device or gps.source}")
        else:
            detail = f"GPS: {gps.status}"
        self._set(self.lbl_gps, detail)

        # buttons / locked controls
        if v.state == IDLE:
            self._cfg(self.btn_start, text="▶  Start session", state="normal", fg_color=t.go,
                      hover_color=t.go_hover, text_color=t.go_text)
        elif v.state == RUNNING:
            self._cfg(self.btn_start, text="■  Stop & export", state="normal", fg_color=t.stop,
                      hover_color=t.stop_hover, text_color=t.text)
        else:
            self._cfg(self.btn_start, text="Exporting…", state="disabled")
        lock = "normal" if v.state == IDLE else "disabled"
        for w in (self.menu_gps, self.menu_port, self.btn_ports, self.btn_browse, self.menu_mode,
                  self.menu_adsb_mode, self.menu_adsb_decoder, self.entry_adsb_devices):
            self._cfg(w, state=lock)
        up_lock = "normal" if v.state == IDLE and not v.uploading else "disabled"
        for w in (self.btn_up_wigle, self.btn_up_wdg):
            self._cfg(w, state=up_lock)
        if self._was_uploading and not v.uploading and v.last_upload:
            self._set_status(f"Upload finished: {v.last_upload}", t.ok if "failed" not in v.last_upload else t.warn)
        self._was_uploading = v.uploading

        # state transitions -> status bar
        if self._was_state != IDLE and v.state == IDLE and app.last_export:
            self._set_status(f"Exported: {app.last_export}", t.ok)
        elif self._was_state == IDLE and v.state == RUNNING:
            self._set_status("Session running - scanning and logging.", t.ok)
        self._was_state = v.state

        # one-shot auto start
        if app.auto_start and v.state == IDLE and (v.adapters or v.ble_enabled or v.adsb_enabled):
            app.auto_start = False
            app.start_session()

        self._update_adapters(v)
        self._update_ble(v)
        self._update_aircraft(v)
        self._update_feed()

    # ----------------------------------------------------------------- adapters
    def _token_selected(self, tag: str, name: str) -> bool:
        for token in self.cfg.scan.include_interfaces:
            tok = token.strip().lower()
            if tok and (tok == tag.lower() or tok in name.lower()):
                return True
        return False

    def _update_adapters(self, v) -> None:
        t = self.t
        active = {a.tag: a for a in v.adapters}
        engine = self.app.engine
        if engine is None:
            self._set(self.lbl_adapters_title, "WI-FI  ·  not part of this mode (pick a mode with Wi-Fi in the sidebar)")
            if self._adapter_sig != ("off",):
                for child in self.adapter_frame.winfo_children():
                    child.destroy()
                self._adapter_rows.clear()
                self._adapter_sig = ("off",)
            return
        skipped = {s["tag"]: s for s in engine.skipped_adapters()}
        scan = self.cfg.scan

        def name_of(tag: str) -> str:
            return active[tag].name if tag in active else skipped[tag].get("name", "")

        if not self._include_converted and scan.include_interfaces and (active or skipped):
            names = set(active) | set(skipped)
            keep = {tg for tg in names if self._token_selected(tg, name_of(tg))}
            scan.exclude_interfaces = sorted(set(scan.exclude_interfaces) | (names - keep))
            scan.include_interfaces = []
            self._include_converted = True

        tags = sorted(set(active) | set(skipped), key=lambda tg: (natural_key(name_of(tg)), tg))
        total = len({f for a in v.adapters for f in a.channels})
        title = (f"ADAPTERS  {len(active)} active of {scan.max_adapters}   ·   {total} channels shared   ·   "
                 "tick \"Use\" to include an adapter (hot-plug is automatic)")
        self._set(self.lbl_adapters_title, title)

        sig = tuple(tags)
        if sig != self._adapter_sig:
            self._rebuild_adapter_rows(tags, name_of)
            self._adapter_sig = sig
        for tag in tags:
            row = self._adapter_rows.get(tag)
            if not row:
                continue
            a = active.get(tag)
            if a is not None:
                state_color = t.ok if a.state == "scanning" else (t.dim if a.state in ("idle", "waiting") else t.warn)
                now = ",".join(str(freq_to_channel(f)) for f in a.current) or "-"
                assigned = describe_channels(a.channels)
                if len(assigned) > 70:
                    assigned = assigned[:69] + "…"
                self._set(row["ch"], str(len(a.channels)))
                self._set(row["assigned"], assigned)
                self._set(row["now"], now)
                self._set(row["driver"], a.driver)
                self._set(row["hits"], str(a.hits))
                self._set(row["state"], a.state, state_color)
            else:
                s = skipped.get(tag, {})
                for key, widget in row.items():
                    if key == "state":
                        self._set(widget, s.get("reason", "unused"), t.dim)
                    elif key in ("name", "driver"):
                        self._set(widget, s.get("name" if key == "name" else "driver", "?"))
                    else:
                        self._set(widget, "-")

    def _rebuild_adapter_rows(self, tags: List[str], name_of) -> None:
        t = self.t
        for child in self.adapter_frame.winfo_children():
            child.destroy()
        self._adapter_rows.clear()
        self._adapter_vars.clear()
        headers = ["Use", "Iface", "Driver", "Ch", "Assigned channels", "Now", "Hits", "State"]
        keys = ("driver", "ch", "assigned", "now", "hits", "state")
        empty = "No scan-capable wireless adapter detected - plug one in, Spejari hot-plugs."
        for col, h in enumerate(headers):
            ctk.CTkLabel(self.adapter_frame, text=h, text_color=t.accent2, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=col, sticky="w", padx=8)
        if not tags:
            ctk.CTkLabel(self.adapter_frame, text=empty, text_color=t.warn).grid(
                row=1, column=0, columnspan=len(headers), sticky="w", padx=8, pady=10)
            return
        for r, tag in enumerate(tags, start=1):
            var = tk.BooleanVar(value=tag not in self.cfg.scan.exclude_interfaces)
            self._adapter_vars[tag] = var
            ctk.CTkCheckBox(self.adapter_frame, text="", variable=var, width=24, checkbox_width=18,
                            checkbox_height=18, fg_color=t.accent2, hover_color=t.accent, border_color=t.dim,
                            command=lambda n=tag: self._on_adapter_toggle(n)).grid(row=r, column=0, padx=(8, 0), pady=2)
            ctk.CTkLabel(self.adapter_frame, text=tag, text_color=t.accent, anchor="w",
                         font=ctk.CTkFont(weight="bold")).grid(row=r, column=1, sticky="w", padx=8)
            widgets: Dict[str, ctk.CTkBaseClass] = {}
            for col, key in enumerate(keys, start=2):
                lbl = ctk.CTkLabel(self.adapter_frame, text="", anchor="w", text_color=t.text)
                lbl.grid(row=r, column=col, sticky="w", padx=8)
                widgets[key] = lbl
            self._adapter_rows[tag] = widgets

    def _on_adapter_toggle(self, tag: str) -> None:
        excl = set(self.cfg.scan.exclude_interfaces)
        if self._adapter_vars[tag].get():
            excl.discard(tag)
        else:
            excl.add(tag)
        self.cfg.scan.exclude_interfaces = sorted(excl)

    # ---------------------------------------------------------------- bluetooth
    def _update_ble(self, v) -> None:
        t = self.t
        if not v.ble_enabled:
            self._set(self.lbl_ble_title, "BLUETOOTH LE  ·  not part of this mode (pick a mode with BLE in the sidebar)")
        else:
            self._set(self.lbl_ble_title,
                      f"BLUETOOTH LE  {len(v.ble_adapters)} adapter(s)   ·   {v.ble_devices} unique devices   ·   "
                      f"{v.ble_logged} logged as BLE")
        sig = tuple((a.name, a.address, a.state, a.devices, a.hits) for a in v.ble_adapters) + (v.ble_note,)
        if sig == self._ble_sig:
            return
        self._ble_sig = sig
        for child in self.ble_frame.winfo_children():
            child.destroy()
        for col, h in enumerate(("Adapter", "Address", "Devices", "Hits", "State")):
            ctk.CTkLabel(self.ble_frame, text=h, text_color=t.accent2, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=col, sticky="w", padx=8)
        if not v.ble_adapters:
            note = v.ble_note or ("Waiting for a Bluetooth adapter..." if v.ble_enabled else "")
            ctk.CTkLabel(self.ble_frame, text=note, text_color=t.warn, wraplength=700, justify="left",
                         anchor="w").grid(row=1, column=0, columnspan=5, sticky="w", padx=8, pady=10)
            return
        for r, a in enumerate(v.ble_adapters, start=1):
            color = t.ok if a.state == "scanning" else (t.dim if a.state == "idle" else t.warn)
            vals = [(a.name, t.accent), (a.address or "-", t.dim), (str(a.devices), t.text), (str(a.hits), t.text),
                    (a.state, color)]
            for col, (text, fg) in enumerate(vals):
                ctk.CTkLabel(self.ble_frame, text=text, text_color=fg, anchor="w").grid(
                    row=r, column=col, sticky="w", padx=8)

    # --------------------------------------------------------------------- adsb
    def _update_aircraft(self, v) -> None:
        if not v.adsb_enabled:
            self._set(self.lbl_air_title, "ADS-B  ·  not part of this mode (pick a mode with ADS-B in the sidebar)")
        else:
            dongles = "  ·  ".join(f"{d.name}: {d.state}, {d.msgs} msgs" for d in v.adsb_dongles) or "no dongle running"
            self._set(self.lbl_air_title,
                      f"ADS-B  {len(v.aircraft)} in view   ·   {v.adsb_total} seen   ·   max range "
                      f"{v.adsb_max_range_km:.0f} km   ·   {dongles}" + (f"   ·   {v.adsb_note}" if v.adsb_note else ""))
        rows = []
        for ac in v.aircraft[:200]:
            rows.append((
                ac.icao, ac.callsign, "-" if ac.alt_ft is None else f"{ac.alt_ft:,}",
                "-" if ac.speed_kt is None else f"{ac.speed_kt:.0f}", "-" if ac.track is None else f"{ac.track:.0f}",
                "-" if ac.range_km is None else f"{ac.range_km:.0f}", f"{ac.age_s:.0f} s", ac.source,
            ))
        sig = tuple(rows)
        if sig == self._air_sig:
            return
        self._air_sig = sig
        self.air_tree.delete(*self.air_tree.get_children())
        for r in rows:
            self.air_tree.insert("", "end", values=r)

    # --------------------------------------------------------------------- feed
    def _redraw_feed(self) -> None:
        """Re-render the whole feed (privacy mode was switched)."""
        self.tree.delete(*self.tree.get_children())
        self._insert_feed(self.app.feed.tail(FEED_LIMIT))
        self.tree.yview_moveto(1.0)

    def _update_feed(self) -> None:
        entries, self._feed_total = self.app.feed.since(self._feed_total)
        if not entries:
            return
        at_bottom = self.tree.yview()[1] >= 0.97
        self._insert_feed(entries[-300:])
        children = self.tree.get_children()
        if len(children) > FEED_LIMIT:
            self.tree.delete(*children[: len(children) - FEED_LIMIT])
        if at_bottom:
            self.tree.yview_moveto(1.0)

    def _insert_feed(self, entries) -> None:
        private = self.cfg.ui.privacy
        for e in entries:
            ts = datetime.fromtimestamp(e.ts).strftime("%H:%M:%S")
            if e.kind in ("SYS", "WARN"):
                self.tree.insert("", "end", values=(ts, e.kind, _clean(e.message), "", "", "", "", "", ""),
                                 tags=(e.kind,))
                continue
            tag = "OPEN" if e.security in ("Open", "WEP") else e.kind
            if e.band == "ADSB":
                self.tree.insert("", "end", values=(ts, e.kind, _clean(e.ssid) or "(aircraft)", e.bssid, "", "ADSB",
                                                    "", e.security, e.adapter), tags=(tag,))
                continue
            ble = e.band == "BLE"
            if private:
                ssid = PRIVATE
            else:
                ssid = "<hidden>" if (e.ssid == "" and not ble) else _clean(e.ssid)
            self.tree.insert("", "end", values=(ts, e.kind, ssid, e.bssid, "" if ble else e.channel,
                                                "BLE" if ble else f"{e.band}G", e.rssi, e.security, e.adapter),
                             tags=(tag,))


class KeysDialog(ctk.CTkToplevel):
    """Enter, test and save the WiGLE and WDGWars API keys (stored in config/.spejari_keys.json, mode 600)."""

    def __init__(self, gui: SpejariGUI) -> None:
        super().__init__(gui)
        self.gui = gui
        t = gui.t
        self.title("Spejari - API keys")
        self.geometry("560x520")
        self.configure(fg_color=t.bg)
        self.transient(gui)
        keys = up.load_keys()
        self.entries: Dict[str, ctk.CTkEntry] = {}

        def head(text: str) -> None:
            ctk.CTkLabel(self, text=text, text_color=t.accent2, anchor="w",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", padx=18, pady=(16, 2))

        def hint(text: str) -> None:
            ctk.CTkLabel(self, text=text, text_color=t.dim, anchor="w", justify="left", wraplength=520,
                         font=ctk.CTkFont(size=11)).pack(fill="x", padx=18)

        def entry(key: str, label: str, value: str, secret: bool) -> None:
            ctk.CTkLabel(self, text=label, text_color=t.text, anchor="w").pack(fill="x", padx=18, pady=(6, 0))
            e = ctk.CTkEntry(self, fg_color=t.card2, text_color=t.text, border_color=t.border,
                             show="•" if secret else "")
            e.insert(0, value)
            e.pack(fill="x", padx=18)
            self.entries[key] = e

        head("WiGLE  (wigle.net)")
        hint("Log in at wigle.net → Account → API Token. Copy the API Name and the API Token.")
        entry("wigle_name", "API Name", keys.wigle_name, False)
        entry("wigle_token", "API Token", keys.wigle_token, True)
        head("WDGWars  (wdgwars.pl)")
        hint("Log in at wdgwars.pl → your profile → create an API key for Spejari.")
        entry("wdgwars_key", "API key", keys.wdgwars_key, True)
        self._wigle_encoded = keys.wigle_encoded

        self.result = ctk.CTkLabel(self, text="Keys are stored only on this machine (config/.spejari_keys.json).",
                                   text_color=t.dim, anchor="w", justify="left", wraplength=520)
        self.result.pack(fill="x", padx=18, pady=(14, 0))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(12, 16))
        row.grid_columnconfigure((0, 1, 2, 3), weight=1)
        gui._button(row, "Show", self._toggle_show).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        gui._button(row, "Test keys", self._test).grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkButton(row, text="Save", fg_color=t.go, hover_color=t.go_hover, text_color=t.go_text,
                      command=self._save).grid(row=0, column=2, sticky="ew", padx=4)
        gui._button(row, "Close", self.destroy).grid(row=0, column=3, sticky="ew", padx=(4, 0))
        self.after(150, self.lift)

    def _keys(self) -> up.Keys:
        k = up.Keys(**{name: e.get().strip() for name, e in self.entries.items()})
        if not (k.wigle_name or k.wigle_token):
            k.wigle_encoded = self._wigle_encoded
        return k

    def _toggle_show(self) -> None:
        for name in ("wigle_token", "wdgwars_key"):
            e = self.entries[name]
            e.configure(show="" if e.cget("show") else "•")

    def _save(self) -> None:
        try:
            path = up.save_keys(self._keys())
        except OSError as exc:
            self.result.configure(text=f"Could not save: {exc}", text_color=self.gui.t.bad)
            return
        self.result.configure(text=f"Saved to {path} (only readable by you).", text_color=self.gui.t.ok)
        self.gui._refresh_keys_label()

    def _test(self) -> None:
        keys = self._keys()
        self.result.configure(text="Testing…", text_color=self.gui.t.warn)

        def work() -> None:
            lines = []
            for target, check in (("wigle", up.check_wigle), ("wdgwars", up.check_wdgwars)):
                if keys.has(target):
                    ok, msg = check(keys)
                    lines.append(("✓ " if ok else "✗ ") + msg)
                else:
                    lines.append(f"- {'WiGLE' if target == 'wigle' else 'WDGWars'}: no key entered")
            text = "\n".join(lines)
            try:
                self.after(0, lambda: self.result.configure(
                    text=text, text_color=self.gui.t.ok if "✗" not in text else self.gui.t.warn))
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()


def run_gui(app: App) -> int:
    signal.signal(signal.SIGTERM, app.request_quit)
    signal.signal(signal.SIGINT, app.request_quit)
    try:
        gui = SpejariGUI(app)
    except tk.TclError as exc:
        print(f"Cannot open the GUI: {exc}", file=sys.stderr)
        print("Run from a desktop session. With sudo, keep DISPLAY/XAUTHORITY (./run.sh --gui does this) or run:\n"
              "  xhost +SI:localuser:root", file=sys.stderr)
        return 1
    app.startup()
    try:
        gui.mainloop()
    finally:
        app.shutdown()
    for path in app.exported_paths:
        print(f"Exported: {path}")
    return 0
