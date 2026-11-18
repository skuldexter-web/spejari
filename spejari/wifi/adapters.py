"""Wireless adapter discovery (via `iw`) and capability parsing."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ..channels import select_channels
from ..iwtools import (  # noqa: F401  (re-exported for callers)
    LOCAL,
    ToolMissing,
    parse_iw_dev,
    parse_phy_freqs,
    run_cmd,
)


def natural_key(name: str):
    """Sort wlan2 before wlan10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


class AdapterError(Exception):
    pass


@dataclass
class Adapter:
    ifname: str  # interface name, shown everywhere
    phy: str
    mac: str
    driver: str
    channels: List[int] = field(default_factory=list)  # usable frequencies (MHz)
    dev: str = ""  # real interface name (defaults to ifname)
    executor: Any = None  # None = run `iw` on this machine; the simulator plugs in its own executor

    @property
    def device(self) -> str:
        return self.dev or self.ifname

    @property
    def remote(self) -> bool:
        return self.executor is not None

    def run(self, args: List[str], timeout: float = 10.0):
        return (self.executor or LOCAL).run(args, timeout)


def driver_of(ifname: str) -> str:
    try:
        return os.path.basename(os.path.realpath(f"/sys/class/net/{ifname}/device/driver")) or "?"
    except OSError:
        return "?"


class AdapterDetector:
    """Finds scan-capable adapters and caches PHY capabilities (or simulated ones with ``sim``)."""

    PHY_CACHE_TTL = 300.0

    def __init__(self, scan_cfg, sim=None) -> None:
        self.cfg = scan_cfg
        self.sim = sim  # a wifi.sim.SimWifi for --simulate
        self.truncated = 0  # adapters ignored because of the max_adapters limit
        self.skipped: List[Dict[str, str]] = []  # detected but unused: {ifname, driver, reason}
        self._phy_cache: Dict[str, Tuple[float, List[int]]] = {}

    def _phy_freqs(self, phy: str) -> List[int]:
        now = time.monotonic()
        cached = self._phy_cache.get(phy)
        if cached and now - cached[0] < self.PHY_CACHE_TTL:
            return cached[1]
        try:
            rc, out, err = run_cmd(["iw", "phy", phy, "info"])
        except ToolMissing as exc:
            raise AdapterError(str(exc)) from exc
        if rc != 0:
            raise AdapterError(f"iw phy {phy} info failed: {err.strip()}")
        freqs = parse_phy_freqs(out)
        self._phy_cache[phy] = (now, freqs)
        return freqs

    def _consider(self, adapter: Adapter, found: List[Adapter], skipped: List[Dict[str, str]]) -> bool:
        """Apply include / exclude / limit rules. True when the adapter joins the cluster."""
        name = adapter.ifname
        reason = ""
        if self.cfg.include_interfaces and name not in self.cfg.include_interfaces:
            reason = "not selected"
        elif name in self.cfg.exclude_interfaces:
            reason = "excluded"
        elif not adapter.channels:
            return False
        elif len(found) >= self.cfg.max_adapters:
            reason = "over limit"
            self.truncated += 1
        if reason:
            skipped.append({"tag": name, "ifname": name, "name": name, "driver": adapter.driver, "reason": reason})
            return False
        found.append(adapter)
        return True

    def _detect_local(self, found: List[Adapter], skipped: List[Dict[str, str]], seen_phys: set) -> str:
        """Add local adapters. Returns an error text ('' = fine)."""
        try:
            rc, out, err = run_cmd(["iw", "dev"])
        except ToolMissing as exc:
            return str(exc)
        if rc != 0:
            return f"iw dev failed: {err.strip() or out.strip()}"
        accepted = set()
        for item in sorted(parse_iw_dev(out), key=lambda i: natural_key(i["ifname"])):
            if item["type"] != "managed":
                continue
            seen_phys.add(item["phy"])
            if item["phy"] in accepted:  # several virtual ifaces on one radio
                continue
            try:
                chans = select_channels(self._phy_freqs(item["phy"]), self.cfg)
            except AdapterError:
                continue
            name = item["ifname"]
            adapter = Adapter(name, item["phy"], item["addr"], driver_of(name), chans)
            if self._consider(adapter, found, skipped):
                accepted.add(item["phy"])
        return ""

    def detect(self) -> List[Adapter]:
        found: List[Adapter] = []
        skipped: List[Dict[str, str]] = []
        seen_phys: set = set()
        self.truncated = 0
        if self.sim is not None:
            local_error = ""
            for adapter in self.sim.adapters():
                adapter.channels = select_channels(adapter.channels, self.cfg)
                self._consider(adapter, found, skipped)
        else:
            local_error = self._detect_local(found, skipped, seen_phys)
        if local_error and not found and not skipped:
            raise AdapterError(local_error)
        self.skipped = skipped
        for phy in list(self._phy_cache):
            if phy not in seen_phys:
                del self._phy_cache[phy]
        return found
