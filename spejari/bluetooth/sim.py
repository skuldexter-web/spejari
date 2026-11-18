"""Simulated Bluetooth LE devices for --simulate and the tests (no hardware needed)."""

from __future__ import annotations

import random
import time
from typing import Callable, List

from ..models import BleSighting
from .engine import BleSource

_NAMES = [
    ("Galaxy Buds Pro", "0x0075"), ("Tile", "0x067C"), ("Mi Band 7", "0x0157"), ("JBL Flip 6", "0x0057"),
    ("Fitbit Charge", "0x0010"), ("Govee H5075", "0xEC88"), ("", "0x004C"), ("AirPods", "0x004C"),
    ("Garmin fenix", "0x0087"), ("ELK-BLEDOM", ""), ("[TV] Samsung", "0x0075"), ("LE-Bose QC", "0x009E"),
]


class SimBleSource(BleSource):
    """A handful of devices around the 'car' plus rotating phone addresses (random=True)."""

    def __init__(self, seed: int = 1, count: int = 24) -> None:
        self.rng = random.Random(seed)
        self.devices: List[BleSighting] = []
        for i in range(count):
            name, company = _NAMES[i % len(_NAMES)]
            random_addr = i % 3 == 0
            first = self.rng.choice([0xC0, 0xD4, 0xF1]) if not random_addr else self.rng.choice([0x42, 0x61, 0x1A])
            addr = ":".join([f"{first:02X}"] + [f"{self.rng.randrange(256):02X}" for _ in range(5)])
            self.devices.append(
                BleSighting(addr, f"{name}" if name else "", self.rng.randint(-95, -45), random_addr, company, 1)
            )

    def run(self, sink: Callable[[BleSighting], None], active: Callable[[], bool]) -> None:
        while active():
            time.sleep(0.25)
            for d in self.rng.sample(self.devices, k=min(6, len(self.devices))):
                rssi = max(-100, min(-35, d.rssi + self.rng.randint(-6, 6)))
                sink(BleSighting(d.address, d.name, rssi, d.random, d.company, d.uuids))
