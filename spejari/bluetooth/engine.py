"""BLE scan engine: adapter discovery, one worker per adapter, hot-plug, session hand-off."""

from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..config import Config
from ..gps import GpsMonitor
from ..models import BleAdapterView, BleSighting, FeedLog
from ..session import Session

_RETRY_S = 10.0
HARD_MAX_BLE = 5

BLUEZ_FIX = ("BlueZ (the Linux Bluetooth service) is not running. Fix:  sudo apt install bluez  and  "
             "sudo systemctl enable --now bluetooth")


class SourceError(Exception):
    """The Bluetooth stack is missing, switched off or busy."""


# ------------------------------------------------------------------------------ addresses
def classify_address(address: str, address_type: Optional[str]) -> bool:
    """True when the address is a *rotating* random private address (not worth logging).

    ``address_type`` is "public", "random" or None (unknown). Random addresses come in three kinds, told apart by
    the two most significant bits: 11 = static random (stable, fine to log), 01 = resolvable private and
    00 = non-resolvable private (both rotate every few minutes).
    """
    if address_type != "random":
        return False
    try:
        top = int(address.split(":")[0], 16) >> 6
    except (ValueError, IndexError):
        return False
    return top in (0b00, 0b01)


def sighting_from_bleak(device, adv) -> BleSighting:
    """Translate a bleak (device, advertisement) pair into a BleSighting. Never raises."""
    address = str(getattr(device, "address", "")).upper()
    name = (getattr(adv, "local_name", None) or getattr(device, "name", None) or "").strip()
    addr_type: Optional[str] = None
    try:  # BlueZ: the advertisement carries (dbus path, device properties) incl. "AddressType"
        pd = getattr(adv, "platform_data", None)
        if isinstance(pd, (tuple, list)) and len(pd) > 1 and isinstance(pd[1], dict):
            addr_type = pd[1].get("AddressType")
    except Exception:
        addr_type = None
    if addr_type is None:  # older bleak: device.details["props"]["AddressType"]
        try:
            details = getattr(device, "details", None)
            if isinstance(details, dict):
                addr_type = (details.get("props") or {}).get("AddressType")
        except Exception:
            addr_type = None
    if addr_type is None:  # other bleak back-ends: BluetoothLEAdvertisementReceivedEventArgs.bluetooth_address_type
        try:
            args = getattr(adv, "platform_data", None)[1]
            raw = int(getattr(args, "bluetooth_address_type", 2))
            addr_type = {0: "public", 1: "random"}.get(raw)
        except Exception:
            addr_type = None
    company = ""
    try:
        keys = list((getattr(adv, "manufacturer_data", None) or {}).keys())
        if keys:
            company = f"0x{int(keys[0]):04X}"
    except Exception:
        company = ""
    rssi = getattr(adv, "rssi", None)
    if rssi is None:
        rssi = getattr(device, "rssi", -100)
    uuids = len(getattr(adv, "service_uuids", None) or [])
    return BleSighting(
        address=address,
        name=name,
        rssi=int(rssi if rssi is not None else -100),
        random=classify_address(address, addr_type),
        company=company,
        uuids=uuids,
    )


# --------------------------------------------------------------------------- adapter listing
def _parse_hciconfig(out: str) -> Dict[str, Dict[str, str]]:
    """{hci0: {address, powered}} from `hciconfig -a` output (powered = yes when the line says UP)."""
    found: Dict[str, Dict[str, str]] = {}
    current = ""
    for line in out.splitlines():
        m = re.match(r"^(hci\d+):", line)
        if m:
            current = m.group(1)
            found[current] = {"address": "", "powered": ""}
        if not current:
            continue
        m = re.search(r"BD Address:\s*([0-9A-Fa-f:]{17})", line)
        if m:
            found[current]["address"] = m.group(1).upper()
        m = re.match(r"^\s+((?:UP|DOWN)\b.*)$", line)
        if m:
            found[current]["powered"] = "yes" if m.group(1).startswith("UP") else "no"
    return found


def _hciconfig_info() -> Dict[str, Dict[str, str]]:
    exe = shutil.which("hciconfig")
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "-a"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return _parse_hciconfig(out)


def _rfkill_blocked(name: str) -> bool:
    for p in glob.glob("/sys/class/bluetooth/%s/rfkill*/soft" % name):
        try:
            with open(p) as fh:
                if fh.read().strip() == "1":
                    return True
        except OSError:
            pass
    return False


def _hciconfig_addresses() -> Dict[str, str]:
    return {k: v["address"] for k, v in _hciconfig_info().items()}


def list_ble_adapters() -> Tuple[List[Dict[str, str]], str]:
    """(adapters, note). Each adapter: {name, address, powered}. The note explains an empty list."""
    try:
        import bleak  # noqa: F401
    except ImportError:
        return [], "The Python package 'bleak' is not installed (pip install bleak)."
    if not os.path.isdir("/sys/class/bluetooth"):
        return [], "No Bluetooth adapter found (/sys/class/bluetooth is missing). Plug in a USB Bluetooth adapter."
    names = sorted(
        (os.path.basename(p) for p in glob.glob("/sys/class/bluetooth/hci[0-9]*") if re.fullmatch(r"hci\d+", os.path.basename(p))),
        key=lambda n: int(n[3:]),
    )
    if not names:
        return [], "No Bluetooth adapter found. Plug in a USB Bluetooth adapter (check `hciconfig` / `rfkill list`)."
    info = _hciconfig_info()
    rows = []
    for n in names:
        i = info.get(n, {})
        powered = i.get("powered", "")
        if _rfkill_blocked(n):
            powered = "blocked (rfkill)"
        rows.append({"name": n, "address": i.get("address", ""), "powered": powered or "?"})
    return rows, ""


# --------------------------------------------------------------------------- BlueZ service
def _run(args: List[str], timeout: float = 6.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def bluez_running() -> bool:
    """True when bluetoothd (BlueZ) is up. bleak talks to it over D-Bus; without it every scan fails."""
    if shutil.which("systemctl"):
        rc, out = _run(["systemctl", "is-active", "bluetooth"])
        if out.strip() == "active":
            return True
    if shutil.which("pgrep"):
        rc, _ = _run(["pgrep", "-x", "bluetoothd"])
        return rc == 0
    return False


def ensure_bluez(notify: Optional[Callable[[str, bool], None]] = None) -> bool:
    """Start BlueZ and power the adapters on when possible (root only). Returns True when BlueZ runs."""
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    ok = bluez_running()
    if not ok and is_root and shutil.which("systemctl"):
        if shutil.which("rfkill"):
            _run(["rfkill", "unblock", "bluetooth"])
        rc, out = _run(["systemctl", "start", "bluetooth"], timeout=15)
        for _ in range(15):
            if bluez_running():
                ok = True
                break
            time.sleep(0.2)
        if notify:
            if ok:
                notify("Started the bluetooth service (BlueZ). Make it permanent: sudo systemctl enable bluetooth", False)
            else:
                notify(BLUEZ_FIX + (f"  [{out[:80]}]" if out else ""), True)
    elif not ok and notify:
        notify(BLUEZ_FIX, True)
    if ok and is_root:
        power_on_adapters()
    return ok


def power_on_adapters() -> List[str]:
    """Power on adapters that are down or soft-blocked. Returns the names switched on."""
    switched = []
    if shutil.which("rfkill"):
        _run(["rfkill", "unblock", "bluetooth"])
    for name, info in _hciconfig_info().items():
        if info.get("powered") != "no":
            continue
        idx = name[3:]
        if shutil.which("btmgmt"):
            rc, _ = _run(["btmgmt", "--index", idx, "power", "on"])
        else:
            rc = 1
        if rc != 0 and shutil.which("hciconfig"):
            rc, _ = _run(["hciconfig", name, "up"])
        if rc == 0:
            switched.append(name)
    return switched


def friendly_error(text: str) -> str:
    low = text.lower()
    if "nosuchunit" in low or "dbus-org.bluez" in low or "serviceunknown" in low or "org.bluez was not provided" in low:
        return BLUEZ_FIX
    if "notready" in low or "not ready" in low or "powered" in low:
        return "the adapter is switched off (Spejari powers it on when run as root; or: bluetoothctl power on)"
    if "rfkill" in low or "blocked" in low:
        return "the adapter is blocked by rfkill (sudo rfkill unblock bluetooth)"
    if "inprogress" in low or "in progress" in low:
        return "another program is already scanning with this adapter"
    return text[:120]


# ------------------------------------------------------------------------------- sources
class BleSource:
    """Something that produces sightings. ``run`` blocks until ``active()`` turns false."""

    def run(self, sink: Callable[[BleSighting], None], active: Callable[[], bool]) -> None:
        raise NotImplementedError


def _adapter_kwargs(adapter: str) -> dict:
    """bleak >= 3 selects the BlueZ adapter with bluez={"adapter": ...}; older versions use adapter=..."""
    try:
        from bleak.args.bluez import BlueZScannerArgs

        if "adapter" in getattr(BlueZScannerArgs, "__annotations__", {}):
            return {"bluez": {"adapter": adapter}}
    except Exception:
        pass
    return {"adapter": adapter}


class BleakSource(BleSource):
    def __init__(self, adapter: str) -> None:
        self.adapter = adapter  # hci0, hci1, ...

    def run(self, sink, active) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError as exc:
            raise SourceError("bleak is not installed (pip install bleak)") from exc
        try:
            asyncio.run(self._arun(sink, active))
        except SourceError:
            raise
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise SourceError(BLUEZ_FIX) from exc
        except Exception as exc:  # BleakError, DBus errors, adapter unplugged, Bluetooth switched off ...
            raise SourceError(friendly_error(str(exc) or exc.__class__.__name__)) from exc

    async def _arun(self, sink, active) -> None:
        from bleak import BleakScanner

        def callback(device, adv) -> None:
            try:
                sink(sighting_from_bleak(device, adv))
            except Exception:
                pass

        kwargs = _adapter_kwargs(self.adapter) if self.adapter else {}
        scanner = BleakScanner(detection_callback=callback, **kwargs)
        await scanner.start()
        try:
            while active():
                await asyncio.sleep(0.25)
        finally:
            try:
                await scanner.stop()
            except Exception:
                pass


# -------------------------------------------------------------------------------- workers
class BleWorker(threading.Thread):
    def __init__(self, engine: "BleEngine", name: str, address: str, source: BleSource) -> None:
        super().__init__(name=f"spejari-ble-{name}", daemon=True)
        self.engine = engine
        self.adapter = name
        self.address = address
        self.source = source
        self.state = "starting"
        self.hits = 0
        self.devices: set = set()
        self._stop_evt = threading.Event()
        self._last_forward: Dict[str, float] = {}

    def stop(self) -> None:
        self._stop_evt.set()

    def _active(self) -> bool:
        return not self._stop_evt.is_set() and self.engine.scanning.is_set()

    def _sink(self, s: BleSighting) -> None:
        if s.rssi < self.engine.cfg.bluetooth.min_rssi or not s.address:
            return
        now = time.monotonic()
        last = self._last_forward.get(s.address, 0.0)
        if now - last < 1.0:
            return
        self._last_forward[s.address] = now
        self.hits += 1
        self.devices.add(s.address)
        if len(self._last_forward) > 20000:  # keep memory bounded on long drives
            self._last_forward.clear()
        self.engine.ingest(s, self.adapter)

    def run(self) -> None:
        while not self._stop_evt.is_set():
            if not self.engine.scanning.is_set():
                self.state = "idle"
                self._stop_evt.wait(0.3)
                continue
            self.state = "scanning"
            try:
                self.source.run(self._sink, self._active)
            except SourceError as exc:
                self.state = "error"
                self.engine.report_error(self.adapter, str(exc))
                self._stop_evt.wait(_RETRY_S)


# --------------------------------------------------------------------------------- engine
class BleEngine:
    """Starts one worker per Bluetooth adapter while a session runs; re-checks for new adapters every 5 s."""

    def __init__(self, cfg: Config, gps: GpsMonitor, feed: FeedLog, simulate: bool = False) -> None:
        self.cfg = cfg
        self.gps = gps
        self.feed = feed
        self.simulate = simulate
        self.scanning = threading.Event()
        self.session: Optional[Session] = None
        self.note = ""
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._workers: Dict[str, BleWorker] = {}
        self._addresses: Dict[str, str] = {}
        self._last_errors: Dict[str, float] = {}
        self._bluez_checked = 0.0
        self._manager = threading.Thread(target=self._manage, name="spejari-ble-manager", daemon=True)

    # -- lifecycle
    def start(self) -> None:
        self._manager.start()

    def shutdown(self) -> None:
        self._stop_evt.set()
        self.scanning.clear()
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=4.0)

    def begin_session(self, session: Session) -> None:
        with self._lock:
            self.session = session
            for w in self._workers.values():
                w.hits = 0
                w.devices.clear()
        self.scanning.set()

    def end_session(self) -> None:
        self.scanning.clear()
        time.sleep(0.4)  # let the scanners wind down
        with self._lock:
            self.session = None

    def report_error(self, adapter: str, text: str) -> None:
        """Show an adapter error once a minute at most (two adapters failing the same way = one line)."""
        now = time.monotonic()
        self.note = f"{adapter}: {text}"
        if now - self._last_errors.get(text, -1e9) >= 60.0:
            self._last_errors[text] = now
            others = [w.adapter for w in list(self._workers.values()) if w.adapter != adapter and w.state == "error"]
            who = ", ".join([adapter] + others)
            self.feed.system(f"Bluetooth {who}: {text} - retrying", warn=True)
        if text == BLUEZ_FIX and now - self._bluez_checked > 30.0 and not self.simulate:
            self._bluez_checked = now
            ensure_bluez(lambda msg, warn: self.feed.system(msg, warn=warn))

    # -- data
    def ingest(self, s: BleSighting, adapter: str) -> None:
        session = self.session
        if session is not None and self.scanning.is_set():
            session.ingest_ble([s], adapter, self.gps.snapshot(), self.gps.utc_now())

    def adapter_views(self) -> List[BleAdapterView]:
        with self._lock:
            return [
                BleAdapterView(name=n, address=w.address, state=w.state, devices=len(w.devices), hits=w.hits)
                for n, w in sorted(self._workers.items())
            ]

    # -- adapter manager (hot-plug aware)
    def _wanted(self) -> Tuple[List[Dict[str, str]], str]:
        if self.simulate:
            return [{"name": "sim0", "address": "00:11:22:33:44:55"}, {"name": "sim1", "address": "00:11:22:33:44:66"}], ""
        adapters, note = list_ble_adapters()
        b = self.cfg.bluetooth
        out = []
        for a in adapters:
            if b.include_adapters and a["name"] not in b.include_adapters:
                continue
            if a["name"] in b.exclude_adapters:
                continue
            out.append(a)
        return out[: max(1, min(HARD_MAX_BLE, b.max_adapters))], note

    def _make_source(self, name: str) -> BleSource:
        if self.simulate:
            from .sim import SimBleSource

            return SimBleSource(seed=sum(map(ord, name)))
        return BleakSource(name)

    def _manage(self) -> None:
        if not self.simulate:
            self._bluez_checked = time.monotonic()
            ensure_bluez(lambda msg, warn: self.feed.system(msg, warn=warn))
        while not self._stop_evt.is_set():
            try:
                adapters, note = self._wanted()
            except Exception as exc:  # never let the manager die
                adapters, note = [], f"adapter scan failed: {exc}"
            with self._lock:
                self.note = note if not adapters else self.note if self.note.endswith("retrying") else ""
                names = {a["name"] for a in adapters}
                for a in adapters:
                    if a["name"] not in self._workers:
                        w = BleWorker(self, a["name"], a.get("address", ""), self._make_source(a["name"]))
                        self._workers[a["name"]] = w
                        w.start()
                        self.feed.system(f"Bluetooth adapter {a['name']} added")
                for name in list(self._workers):
                    if name not in names:
                        self._workers.pop(name).stop()
                        self.feed.system(f"Bluetooth adapter {name} removed", warn=True)
            self._stop_evt.wait(5.0)
