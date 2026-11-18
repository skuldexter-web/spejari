"""Simulated Wi-Fi adapters (``--simulate``): try Spejari without hardware or root.

The simulator answers the same ``iw dev <if> scan ... freq ...`` commands the real
scanner sends, with genuine ``iw`` text output, so the real parser, the channel
clustering and the whole logging pipeline run exactly as they do on a real drive.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Dict, List, Tuple

from ..channels import freq_to_channel
from .adapters import Adapter

_FREQS_2G = [2407 + 5 * c for c in range(1, 14)]
_FREQS_5G = [5000 + 5 * c for c in (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
                                     132, 136, 140, 149, 153, 157, 161, 165)]
_COMMON_2G = [2412, 2437, 2462, 2412, 2437, 2462, 2422, 2447, 2472]
_NAMES = ["HomeNet", "CoffeeShop", "FreeWifi", "Hotel_Guest", "Office", "Linksys", "TP-Link", "Skald", "Odin",
          "Valhalla", "Bifrost", "Mjolnir", "Yggdrasil", "Fenrir", "NETGEAR", "FRITZ!Box", "iPhone", "AndroidAP"]
# (capability text, RSN/WPA block) - iw formats these exactly like this
_SECURITY = [
    ("ESS Privacy (0x0411)", "\tRSN:\t * Version: 1\n\t\t * Group cipher: CCMP\n\t\t * Pairwise ciphers: CCMP\n"
                             "\t\t * Authentication suites: PSK\n"),
    ("ESS Privacy (0x0411)", "\tRSN:\t * Version: 1\n\t\t * Group cipher: CCMP\n\t\t * Pairwise ciphers: CCMP\n"
                             "\t\t * Authentication suites: PSK SAE\n"),
    ("ESS Privacy (0x0411)", "\tRSN:\t * Version: 1\n\t\t * Group cipher: CCMP\n\t\t * Pairwise ciphers: CCMP\n"
                             "\t\t * Authentication suites: SAE\n"),
    ("ESS Privacy (0x0411)", "\tRSN:\t * Version: 1\n\t\t * Group cipher: CCMP\n\t\t * Pairwise ciphers: CCMP\n"
                             "\t\t * Authentication suites: IEEE 802.1X\n"),
    ("ESS Privacy (0x0411)", "\tWPA:\t * Version: 1\n\t\t * Group cipher: TKIP\n\t\t * Pairwise ciphers: TKIP CCMP\n"
                             "\t\t * Authentication suites: PSK\n"),
    ("ESS (0x0401)", ""),
    ("ESS Privacy (0x0411)", ""),  # WEP
    ("ESS Privacy (0x0411)", "\tRSN:\t * Version: 1\n\t\t * Group cipher: CCMP\n\t\t * Pairwise ciphers: CCMP\n"
                             "\t\t * Authentication suites: PSK\n"),
]


class SimWifi:
    """A neighbourhood of simulated access points plus N simulated adapters (hot-plug capable)."""

    def __init__(self, adapters: int = 3, networks: int = 180, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._count = max(0, adapters)
        self.aps: List[Dict] = []
        for n in range(networks):
            if self._rng.random() < 0.45:
                freq = self._rng.choice(_FREQS_5G)
            else:
                freq = self._rng.choice(_COMMON_2G)
            hidden = n % 23 == 0
            ssid = "" if hidden else f"{self._rng.choice(_NAMES)}-{n:03d}"
            mac = ":".join(f"{self._rng.randrange(256):02x}" for _ in range(6))
            cap, sec = _SECURITY[n % len(_SECURITY)]
            self.aps.append({"bssid": mac, "ssid": ssid, "freq": freq, "cap": cap, "sec": sec,
                             "base": self._rng.randint(-88, -38)})

    # -- adapters -------------------------------------------------------------------
    def set_adapter_count(self, n: int) -> None:
        with self._lock:
            self._count = max(0, n)

    def adapters(self) -> List[Adapter]:
        with self._lock:
            n = self._count
        out = []
        for i in range(n):
            dual = i % 3 != 2  # every third simulated adapter is a 2.4 GHz-only dongle
            chans = _FREQS_2G + (_FREQS_5G if dual else [])
            out.append(Adapter(f"simwlan{i}", f"simphy{i}", f"02:5e:1a:00:00:{i:02x}",
                               "sim-mt76" if dual else "sim-rtl8xxxu", list(chans),
                               executor=SimExecutor(self, f"simwlan{i}")))
        return out

    # -- scanning -------------------------------------------------------------------
    def scan_text(self, ifname: str, freqs: List[int]) -> str:
        wanted = set(freqs)
        window = int(time.monotonic() / 6)
        blocks = []
        for n, ap in enumerate(self.aps):
            if ap["freq"] not in wanted:
                continue
            if random.Random(hash((ifname, n, window))).random() > 0.55:
                continue  # out of range for this adapter right now
            rssi = max(-95, min(-30, ap["base"] + random.randint(-6, 6)))
            ssid = ap["ssid"]
            ch = freq_to_channel(ap["freq"])
            block = (
                f"BSS {ap['bssid']}(on {ifname})\n"
                f"\tfreq: {ap['freq']}\n"
                f"\tcapability: {ap['cap']}\n"
                f"\tsignal: {rssi}.00 dBm\n"
                f"\tSSID: {ssid}\n"
                f"\tDS Parameter set: channel {ch}\n"
                f"{ap['sec']}"
            )
            blocks.append(block)
        return "".join(blocks)


class SimExecutor:
    """Stands in for the local command runner of one simulated adapter."""

    label = "simulate"

    def __init__(self, world: SimWifi, ifname: str) -> None:
        self.world = world
        self.ifname = ifname

    def run(self, args: List[str], timeout: float = 10.0) -> Tuple[int, str, str]:
        if len(args) >= 4 and args[0] == "iw" and args[3] == "scan":
            if self.ifname not in {a.ifname for a in self.world.adapters()}:
                return 237, "", "command failed: No such device (-19)"
            freqs = [int(a) for a in args[4:] if a.isdigit()]
            time.sleep(min(0.25, 0.06 * max(1, len(freqs))))  # a real dwell takes ~100 ms per channel
            return 0, self.world.scan_text(self.ifname, freqs), ""
        return 0, "", ""  # `ip link set ... up` and friends
