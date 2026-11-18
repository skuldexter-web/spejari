"""Wi-Fi scanning: drives ``iw`` and splits the channels across up to 10 adapters (clustering).

``--simulate`` plugs simulated adapters into the very same engine (no hardware, no root).
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Optional


def make_engine(cfg, gps, feed):
    """Create the Wi-Fi scan engine (real or simulated)."""
    from .scanner import ScanEngine

    sim = None
    if cfg.simulate_adapters:
        from .sim import SimWifi

        sim = SimWifi(cfg.simulate_adapters)
    return ScanEngine(cfg, gps, feed, sim=sim)


def preflight(cfg) -> Optional[str]:
    """Return an error message when Wi-Fi scanning cannot work on this machine, else None."""
    if cfg.simulate_adapters:
        return None
    if not sys.platform.startswith("linux"):
        return f"Spejari runs on Linux (Debian, Kali, Ubuntu, Raspberry Pi OS); this is {sys.platform}."
    if shutil.which("iw") is None:
        return "'iw' is required. Install it with: sudo apt install iw"
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return "Wi-Fi scanning needs root privileges. Run:  sudo ./run.sh   (or sudo python3 main.py)"
    return None
