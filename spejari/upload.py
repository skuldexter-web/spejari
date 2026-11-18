"""Upload finished drives to WiGLE (wigle.net) and WDGWars (wdgwars.pl) with your own API keys.

* WiGLE:   POST https://api.wigle.net/api/v2/file/upload  (multipart field ``file``, HTTP Basic with the
           "API Name" + "API Token" from https://wigle.net/account, or the "Encoded for use" string)
* WDGWars: POST https://wdgwars.pl/endpoint/upload-csv    (multipart field ``file``, header ``X-API-Key``)
           POST https://wdgwars.pl/endpoint/upload/        (aircraft: HMAC-signed JSON envelope, same key)

WDGWars has no official API documentation; the endpoints follow the community clients that use it
(wigle-to-wdgwars / gungnir). Only the Python standard library is used.

Keys are stored in ``config/.spejari_keys.json`` (permissions 600, git-ignored) or read from the environment
variables ``WIGLE_API_NAME`` / ``WIGLE_API_TOKEN`` / ``WIGLE_API_ENCODED`` / ``WDGWARS_API_KEY``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import APP_NAME, __version__
from .config import PROJECT_ROOT, UploadConfig

WIGLE_UPLOAD_URL = "https://api.wigle.net/api/v2/file/upload"
WIGLE_PROFILE_URL = "https://api.wigle.net/api/v2/profile/user"
WDGWARS_CSV_URL = "https://wdgwars.pl/endpoint/upload-csv"
WDGWARS_SIGNED_URL = "https://wdgwars.pl/endpoint/upload/"
WDGWARS_ME_URL = "https://wdgwars.pl/endpoint/me"

KEYS_PATH = PROJECT_ROOT / "config" / ".spejari_keys.json"
LEDGER_NAME = ".spejari_uploads.json"
USER_AGENT = f"{APP_NAME}/{__version__} (+https://github.com/skuldexter-web)"
TARGETS = ("wigle", "wdgwars")

Log = Callable[[str, bool], None]  # (message, is_warning)


def _nolog(_msg: str, _warn: bool = False) -> None:
    pass


# ------------------------------------------------------------------------------------ keys
@dataclass
class Keys:
    wigle_name: str = ""  # "API Name"  (looks like AID0123...)
    wigle_token: str = ""  # "API Token"
    wigle_encoded: str = ""  # "Encoded for use" (base64 of name:token) - either this or name + token
    wdgwars_key: str = ""

    def has(self, target: str) -> bool:
        if target == "wigle":
            return bool(self.wigle_encoded or (self.wigle_name and self.wigle_token))
        if target == "wdgwars":
            return bool(self.wdgwars_key)
        return False

    def wigle_auth(self) -> str:
        if self.wigle_encoded:
            enc = self.wigle_encoded.strip()
            return enc if enc.lower().startswith("basic ") else "Basic " + enc
        raw = f"{self.wigle_name.strip()}:{self.wigle_token.strip()}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def mask(secret: str) -> str:
    secret = secret or ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "…" + secret[-4:]


def load_keys(path: Optional[Path] = None) -> Keys:
    keys = Keys()
    try:
        with open(path or KEYS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        for k, v in data.items() if isinstance(data, dict) else []:
            if hasattr(keys, k) and isinstance(v, str):
                setattr(keys, k, v.strip())
    except (OSError, ValueError):
        pass
    env = {
        "wigle_name": "WIGLE_API_NAME",
        "wigle_token": "WIGLE_API_TOKEN",
        "wigle_encoded": "WIGLE_API_ENCODED",
        "wdgwars_key": "WDGWARS_API_KEY",
    }
    for attr, var in env.items():
        if os.environ.get(var):
            setattr(keys, attr, os.environ[var].strip())
    return keys


def save_keys(keys: Keys, path: Optional[Path] = None) -> Path:
    target = Path(path or KEYS_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(keys), fh, indent=2)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    from .exporter import _chown_to_sudo_user

    _chown_to_sudo_user(target)
    return target


# ------------------------------------------------------------------------------------ http
def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers, **{"User-Agent": USER_AGENT}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return exc.code, text
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return 0, f"network error: {reason}"


def _multipart(field_name: str, filename: str, payload: bytes, extra: Optional[Dict[str, str]] = None,
               content_type: str = "text/csv") -> Tuple[bytes, str]:
    boundary = f"----spejari{uuid.uuid4().hex}"
    out = io.BytesIO()
    for k, v in (extra or {}).items():
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
              f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
    out.write(payload)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _short(text: str, n: int = 140) -> str:
    return " ".join(str(text).split())[:n]


# ---------------------------------------------------------------------------------- result
@dataclass
class UploadResult:
    target: str
    path: str
    ok: bool
    message: str
    details: Dict[str, object] = field(default_factory=dict)


# ----------------------------------------------------------------------------- key checks
def check_wigle(keys: Keys, timeout: float = 30.0) -> Tuple[bool, str]:
    if not keys.has("wigle"):
        return False, "no WiGLE key set"
    status, text = _http("GET", WIGLE_PROFILE_URL, {"Authorization": keys.wigle_auth(), "Accept": "application/json"},
                         None, timeout)
    data = _json(text)
    if status == 200 and data.get("success", True):
        who = data.get("userid") or data.get("user") or "?"
        return True, f"WiGLE key OK (user {who})"
    if status in (401, 403):
        return False, "WiGLE rejected the key: use the API Name + API Token from https://wigle.net/account"
    return False, f"WiGLE answered HTTP {status}: {_short(data.get('message') or text)}"


def check_wdgwars(keys: Keys, timeout: float = 30.0) -> Tuple[bool, str]:
    if not keys.has("wdgwars"):
        return False, "no WDGWars key set"
    status, text = _http("GET", WDGWARS_ME_URL, {"X-API-Key": keys.wdgwars_key, "Accept": "application/json"},
                         None, timeout)
    data = _json(text)
    if status == 200 and data.get("ok", True):
        return True, (f"WDGWars key OK (user {data.get('username', '?')}, wifi {data.get('wifi', '?')}, "
                      f"ble {data.get('ble', '?')}, aircraft {data.get('aircraft', '?')})")
    if status in (401, 403):
        return False, "WDGWars rejected the key: create one on your profile page at https://wdgwars.pl"
    return False, f"WDGWars answered HTTP {status}: {_short(data.get('error') or data.get('message') or text)}"


# ---------------------------------------------------------------------------------- WiGLE
def upload_wigle(path: Path, keys: Keys, cfg: UploadConfig) -> UploadResult:
    path = Path(path)
    if not keys.has("wigle"):
        return UploadResult("wigle", str(path), False, "no WiGLE API key (spejari --keys)")
    body, ctype = _multipart("file", path.name, path.read_bytes(),
                             {"donate": "on" if cfg.wigle_donate else "off"})
    status, text = _http("POST", WIGLE_UPLOAD_URL, {"Authorization": keys.wigle_auth(), "Content-Type": ctype,
                                                    "Accept": "application/json"}, body, cfg.timeout_s)
    data = _json(text)
    if status == 200 and data.get("success", False):
        results = data.get("results") or {}
        trans = [t.get("transId") for t in results.get("transids", []) if isinstance(t, dict)] if isinstance(
            results, dict) else []
        msg = "uploaded to WiGLE" + (f" (transaction {', '.join(filter(None, trans))})" if any(trans) else "")
        if data.get("warning"):
            msg += f" - {_short(data['warning'], 80)}"
        return UploadResult("wigle", str(path), True, msg, {"transids": trans})
    if status in (401, 403):
        return UploadResult("wigle", str(path), False,
                            "WiGLE rejected the key: use the API Name + API Token from https://wigle.net/account")
    reason = data.get("message") or data.get("error") or text
    return UploadResult("wigle", str(path), False, f"WiGLE upload failed (HTTP {status}): {_short(reason)}")


# -------------------------------------------------------------------------------- WDGWars
def _split_csv(raw: bytes, rows: int) -> List[bytes]:
    """Chunks of ``rows`` data rows, each keeping BOTH WiGLE header lines (the importer needs them)."""
    lines = raw.decode("utf-8", "replace").splitlines()
    if len(lines) < 3:
        return []
    h1, h2, data = lines[0], lines[1], [ln for ln in lines[2:] if ln.strip()]
    return [(h1 + "\n" + h2 + "\n" + "\n".join(data[i:i + rows]) + "\n").encode("utf-8")
            for i in range(0, len(data), rows)]


def _halve(chunk: bytes) -> Optional[Tuple[bytes, bytes]]:
    lines = chunk.decode("utf-8").splitlines()
    if len(lines) < 4:
        return None
    h1, h2, data = lines[0], lines[1], lines[2:]
    mid = len(data) // 2
    make = lambda part: (h1 + "\n" + h2 + "\n" + "\n".join(part) + "\n").encode("utf-8")  # noqa: E731
    return make(data[:mid]), make(data[mid:])


_COUNTERS = ("imported", "captured", "updated", "duplicates", "no_gps", "bad_rows", "merged_samples")


def upload_wdgwars_csv(path: Path, keys: Keys, cfg: UploadConfig, log: Log = _nolog,
                       sleep: Callable[[float], None] = time.sleep) -> UploadResult:
    path = Path(path)
    if not keys.has("wdgwars"):
        return UploadResult("wdgwars", str(path), False, "no WDGWars API key (spejari --keys)")
    queue = _split_csv(path.read_bytes(), cfg.chunk_rows)
    if not queue:
        return UploadResult("wdgwars", str(path), True, "nothing to upload (no data rows)")
    totals: Dict[str, int] = {k: 0 for k in _COUNTERS}
    sent = 0
    part = 0
    retries = 0
    while queue:
        chunk = queue.pop(0)
        part += 1
        body, ctype = _multipart("file", path.name, chunk)
        status, text = _http("POST", WDGWARS_CSV_URL, {"X-API-Key": keys.wdgwars_key, "Content-Type": ctype,
                                                       "Accept": "application/json"}, body, cfg.timeout_s)
        data = _json(text)
        if status == 200 and data.get("ok", True):
            for k in _COUNTERS:
                try:
                    totals[k] += int(data.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            sent += 1
            retries = 0
        elif status == 409 and data.get("error") == "duplicate_upload":
            sent += 1  # the server already has exactly this file: that is a success
            totals["duplicates"] += chunk.count(b"\n") - 2
        elif status == 429 and retries < 4:
            wait = min(180.0, float(data.get("retry_after") or 20))
            log(f"WDGWars is busy with your previous upload, waiting {wait:.0f} s", False)
            sleep(wait)
            queue.insert(0, chunk)
            retries += 1
            continue
        elif status == 413:
            halves = _halve(chunk)
            if halves is None:
                return UploadResult("wdgwars", str(path), False, "WDGWars refused a single row as too large")
            queue[0:0] = list(halves)
            continue
        elif status in (401, 403):
            return UploadResult("wdgwars", str(path), False,
                                "WDGWars rejected the key: create one on your profile page at https://wdgwars.pl")
        elif status == 524:
            return UploadResult("wdgwars", str(path), False,
                                "WDGWars timed out (HTTP 524). The rows may still be imported; lower "
                                "[upload] chunk_rows and try again later", dict(totals))
        else:
            reason = data.get("error") or data.get("message") or text
            return UploadResult("wdgwars", str(path), False,
                                f"WDGWars upload failed on part {part} (HTTP {status}): {_short(reason)}", dict(totals))
        if queue:
            sleep(1.0)  # one request at a time per account
    msg = (f"uploaded to WDGWars: {totals['imported']} imported, {totals['updated']} updated, "
           f"{totals['duplicates']} duplicates")
    if totals["no_gps"] or totals["bad_rows"]:
        msg += f", {totals['no_gps']} without GPS, {totals['bad_rows']} rejected"
    return UploadResult("wdgwars", str(path), True, msg, dict(totals))


def aircraft_records(adsb_csv: Path) -> List[dict]:
    """One record per aircraft (first heard position) from a Spejari_ADSB_*.csv, in the WDGWars schema."""
    first: Dict[str, dict] = {}
    with open(adsb_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            icao = (row.get("ICAO") or "").strip().upper()
            if not icao or icao in first:
                continue
            try:
                lat, lon = float(row["Latitude"]), float(row["Longitude"])
            except (KeyError, TypeError, ValueError):
                continue

            def num(key: str):
                try:
                    return round(float(row.get(key) or ""))
                except ValueError:
                    return None

            first[icao] = {
                "icao": icao,
                "callsign": (row.get("Callsign") or "").strip(),
                "lat": lat,
                "lon": lon,
                "alt_ft": num("AltitudeFt"),
                "speed_kt": num("SpeedKt"),
                "heading": num("TrackDeg"),
                "first_seen": (row.get("TimeUTC") or "").strip(),
                "type": "ADSB",
            }
    return list(first.values())


def signed_envelope(payload: dict, api_key: str, nonce: Optional[str] = None) -> dict:
    data_b64 = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    nonce = nonce or secrets.token_hex(8)
    sig = hmac.new(api_key.encode(), (nonce + data_b64).encode(), hashlib.sha256).hexdigest()
    return {"data": data_b64, "nonce": nonce, "sig": sig}


def upload_wdgwars_adsb(path: Path, keys: Keys, cfg: UploadConfig, log: Log = _nolog,
                        sleep: Callable[[float], None] = time.sleep, batch: int = 500) -> UploadResult:
    path = Path(path)
    if not keys.has("wdgwars"):
        return UploadResult("wdgwars", str(path), False, "no WDGWars API key (spejari --keys)")
    records = aircraft_records(path)
    if not records:
        return UploadResult("wdgwars", str(path), True, "no aircraft positions to upload")
    imported = seen = 0
    for i in range(0, len(records), batch):
        env = signed_envelope({"networks": [], "aircraft": records[i:i + batch], "meshcore_nodes": []},
                              keys.wdgwars_key)
        for attempt in range(4):
            status, text = _http("POST", WDGWARS_SIGNED_URL, {"X-API-Key": keys.wdgwars_key,
                                                             "Content-Type": "application/json",
                                                             "Accept": "application/json"},
                                 json.dumps(env).encode(), cfg.timeout_s)
            data = _json(text)
            if status == 429 and attempt < 3:
                wait = min(180.0, float(data.get("retry_after") or 20))
                log(f"WDGWars is busy, waiting {wait:.0f} s", False)
                sleep(wait)
                continue
            break
        if status in (401, 403):
            return UploadResult("wdgwars", str(path), False,
                                "WDGWars rejected the key: create one on your profile page at https://wdgwars.pl")
        if status != 200 or not data.get("ok", True):
            reason = data.get("error") or data.get("message") or text
            return UploadResult("wdgwars", str(path), False, f"WDGWars aircraft upload failed (HTTP {status}): "
                                                             f"{_short(reason)}")
        imported += int(data.get("aircraft_imported") or 0)
        seen += int(data.get("aircraft_already_seen") or 0)
        if i + batch < len(records):
            sleep(1.0)
    return UploadResult("wdgwars", str(path), True,
                        f"uploaded {len(records)} aircraft to WDGWars: {imported} new, {seen} already seen",
                        {"aircraft": len(records), "aircraft_imported": imported, "aircraft_already_seen": seen})


# --------------------------------------------------------------------------------- ledger
class Ledger:
    """Remembers which export went where (exports/.spejari_uploads.json) so nothing is sent twice."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / LEDGER_NAME
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.data: Dict[str, Dict[str, str]] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def done(self, path: Path, target: str) -> bool:
        return target in self.data.get(Path(path).name, {})

    def mark(self, path: Path, target: str) -> None:
        self.data.setdefault(Path(path).name, {})[target] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            from .exporter import _chown_to_sudo_user

            _chown_to_sudo_user(self.path)
        except OSError:
            pass


def is_adsb_file(path: Path) -> bool:
    return Path(path).name.startswith(f"{APP_NAME}_ADSB_")


def export_files(directory: Path) -> List[Path]:
    """All finished Spejari exports (drive CSVs and ADS-B CSVs), oldest first."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{APP_NAME}_*.csv") if p.is_file())


def pending(directory: Path, target: str, cfg: UploadConfig) -> List[Path]:
    ledger = Ledger(directory)
    out = []
    for p in export_files(directory):
        if is_adsb_file(p) and (target != "wdgwars" or not cfg.wdgwars_adsb):
            continue  # WiGLE does not take aircraft
        if not ledger.done(p, target):
            out.append(p)
    return out


def targets_of(choice: str) -> List[str]:
    choice = (choice or "").lower()
    if choice in ("both", "all"):
        return list(TARGETS)
    return [choice] if choice in TARGETS else []


def upload_files(files: List[Path], target: str, cfg: UploadConfig, keys: Keys, ledger_dir: Optional[Path] = None,
                 log: Log = _nolog, force: bool = False) -> List[UploadResult]:
    """Upload each file to one target (wigle | wdgwars) and record successes in the ledger."""
    results: List[UploadResult] = []
    ledgers: Dict[Path, Ledger] = {}
    for f in files:
        f = Path(f)
        ledger = ledgers.setdefault(Path(ledger_dir or f.parent), Ledger(Path(ledger_dir or f.parent)))
        if not force and ledger.done(f, target):
            results.append(UploadResult(target, str(f), True, "already uploaded (skipped)", {"skipped": True}))
            continue
        if is_adsb_file(f):
            if target != "wdgwars":
                results.append(UploadResult(target, str(f), True, "skipped: WiGLE does not take ADS-B files",
                                            {"skipped": True}))
                continue
            res = upload_wdgwars_adsb(f, keys, cfg, log)
        elif target == "wigle":
            res = upload_wigle(f, keys, cfg)
        else:
            res = upload_wdgwars_csv(f, keys, cfg, log)
        if res.ok and not res.details.get("skipped"):
            ledger.mark(f, target)
        log(f"{f.name}: {res.message}", not res.ok)
        results.append(res)
    return results
