"""Uploader tests against a local fake of the WiGLE and WDGWars APIs (no network needed)."""

import base64
import hashlib
import hmac
import json
import os
import stat
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from spejari import upload as up
from spejari.config import UploadConfig
from spejari.exporter import write_wigle_csv
from spejari.models import Observation


class FakeApi(BaseHTTPRequestHandler):
    log: list = []
    script: dict = {}  # path -> list of (status, json) answers, consumed in order

    def log_message(self, *a):  # silence
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        FakeApi.log.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        queue = FakeApi.script.get(self.path) or [(200, {"ok": True, "success": True})]
        status, data = queue.pop(0) if len(queue) > 1 else queue[0]
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _answer
    do_POST = _answer


@pytest.fixture
def api(monkeypatch):
    FakeApi.log, FakeApi.script = [], {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(up, "WIGLE_UPLOAD_URL", base + "/wigle/upload")
    monkeypatch.setattr(up, "WIGLE_PROFILE_URL", base + "/wigle/profile")
    monkeypatch.setattr(up, "WDGWARS_CSV_URL", base + "/wdg/upload-csv")
    monkeypatch.setattr(up, "WDGWARS_SIGNED_URL", base + "/wdg/upload/")
    monkeypatch.setattr(up, "WDGWARS_ME_URL", base + "/wdg/me")
    yield FakeApi
    srv.shutdown()


def _drive(tmp_path: Path, rows: int = 3, name: str = "Spejari_2026-11-17_20-00-00.csv") -> Path:
    obs = [Observation(bssid=f"AA:BB:CC:00:{i // 256:02X}:{i % 256:02X}", ssid=f"net{i}", auth="[WPA2-PSK-CCMP][ESS]",
                       seen_utc=datetime(2026, 11, 17, 19, 0, i % 60, tzinfo=timezone.utc), channel=6, rssi=-60,
                       lat=52.1, lon=4.3, alt=1.0, accuracy=5.0, freq=2437) for i in range(rows)]
    path = tmp_path / name
    write_wigle_csv(obs, path)
    return path


KEYS = up.Keys(wigle_name="AIDtest", wigle_token="tok123", wdgwars_key="wdg-secret-key")
NOSLEEP = lambda _s: None  # noqa: E731


def test_wigle_upload_sends_basic_auth_and_file(api, tmp_path):
    api.script["/wigle/upload"] = [(200, {"success": True, "results": {"transids": [{"transId": "20261117-00042"}]}})]
    res = up.upload_wigle(_drive(tmp_path), KEYS, UploadConfig())
    assert res.ok and "20261117-00042" in res.message
    req = api.log[-1]
    assert req["headers"]["authorization"] == "Basic " + base64.b64encode(b"AIDtest:tok123").decode()
    assert b'name="file"; filename="Spejari_2026-11-17_20-00-00.csv"' in req["body"]
    assert b"WigleWifi-1.6" in req["body"] and b'name="donate"' in req["body"]


def test_wigle_encoded_token_is_used_verbatim():
    k = up.Keys(wigle_encoded="QUlEOnRvaw==")
    assert k.has("wigle") and k.wigle_auth() == "Basic QUlEOnRvaw=="


def test_wigle_bad_key_is_explained(api, tmp_path):
    api.script["/wigle/upload"] = [(401, {"success": False, "message": "unauthorized"})]
    res = up.upload_wigle(_drive(tmp_path), KEYS, UploadConfig())
    assert not res.ok and "wigle.net/account" in res.message


def test_wdgwars_csv_is_chunked_with_both_header_lines(api, tmp_path):
    api.script["/wdg/upload-csv"] = [(200, {"ok": True, "imported": 10, "updated": 0, "duplicates": 0})]
    cfg = UploadConfig(chunk_rows=10)
    res = up.upload_wdgwars_csv(_drive(tmp_path, rows=25), KEYS, cfg, sleep=NOSLEEP)
    posts = [r for r in api.log if r["path"] == "/wdg/upload-csv"]
    assert res.ok and len(posts) == 3
    for p in posts:
        assert p["headers"]["x-api-key"] == "wdg-secret-key"
        payload = p["body"].split(b"\r\n\r\n", 1)[1]
        lines = payload.decode().splitlines()
        assert lines[0].startswith("WigleWifi-1.6") and lines[1].startswith("MAC,SSID,AuthMode")
    assert "30 imported" in res.message  # 3 answers x 10


def test_wdgwars_waits_on_429_and_accepts_409_duplicate(api, tmp_path):
    api.script["/wdg/upload-csv"] = [(429, {"retry_after": 3}), (409, {"error": "duplicate_upload"})]
    waited = []
    res = up.upload_wdgwars_csv(_drive(tmp_path), KEYS, UploadConfig(), sleep=waited.append)
    assert res.ok and waited == [3.0]


def test_wdgwars_413_splits_the_chunk(api, tmp_path):
    api.script["/wdg/upload-csv"] = [(413, {"error": "too large"}), (200, {"ok": True, "imported": 2}),
                                     (200, {"ok": True, "imported": 2})]
    res = up.upload_wdgwars_csv(_drive(tmp_path, rows=4), KEYS, UploadConfig(), sleep=NOSLEEP)
    assert res.ok and res.details["imported"] == 4
    assert len([r for r in api.log if r["path"] == "/wdg/upload-csv"]) == 3


def test_wdgwars_aircraft_envelope_is_signed(api, tmp_path):
    adsb = tmp_path / "Spejari_ADSB_2026-11-17_20-00-00.csv"
    adsb.write_text(
        "TimeUTC,ICAO,Callsign,Latitude,Longitude,AltitudeFt,SpeedKt,TrackDeg,Squawk,ReceiverLatitude,"
        "ReceiverLongitude,RangeKm,Dongle\n"
        "2026-11-17 19:00:00,4840d6,KLM1234,52.3,4.7,36000,450,87,1000,52.1,4.3,30.1,rtl0\n"
        "2026-11-17 19:00:05,4840d6,KLM1234,52.31,4.72,36000,450,87,1000,52.1,4.3,31.0,rtl0\n"
        "2026-11-17 19:00:06,3c6444,DLH9,52.0,4.0,,,,,52.1,4.3,20,rtl0\n")
    api.script["/wdg/upload/"] = [(200, {"ok": True, "aircraft_imported": 2, "aircraft_already_seen": 0})]
    res = up.upload_wdgwars_adsb(adsb, KEYS, UploadConfig(), sleep=NOSLEEP)
    assert res.ok and res.details["aircraft"] == 2
    env = json.loads(api.log[-1]["body"])
    expected = hmac.new(b"wdg-secret-key", (env["nonce"] + env["data"]).encode(), hashlib.sha256).hexdigest()
    assert env["sig"] == expected
    payload = json.loads(base64.b64decode(env["data"]))
    first = payload["aircraft"][0]
    assert first["icao"] == "4840D6" and first["callsign"] == "KLM1234" and first["alt_ft"] == 36000
    assert first["type"] == "ADSB" and first["first_seen"] == "2026-11-17 19:00:00"
    assert payload["aircraft"][1]["alt_ft"] is None


def test_ledger_skips_files_already_uploaded(api, tmp_path):
    api.script["/wigle/upload"] = [(200, {"success": True})]
    f = _drive(tmp_path)
    cfg = UploadConfig()
    assert [p.name for p in up.pending(tmp_path, "wigle", cfg)] == [f.name]
    first = up.upload_files([f], "wigle", cfg, KEYS, tmp_path)
    assert first[0].ok
    assert up.pending(tmp_path, "wigle", cfg) == []
    assert up.pending(tmp_path, "wdgwars", cfg) == [f]  # the other site still needs it
    again = up.upload_files([f], "wigle", cfg, KEYS, tmp_path)
    assert again[0].details.get("skipped")
    assert len([r for r in api.log if r["path"] == "/wigle/upload"]) == 1


def test_adsb_files_only_go_to_wdgwars(tmp_path):
    (tmp_path / "Spejari_ADSB_2026-11-17_20-00-00.csv").write_text("TimeUTC,ICAO\n")
    _drive(tmp_path)
    cfg = UploadConfig()
    assert all(not up.is_adsb_file(p) for p in up.pending(tmp_path, "wigle", cfg))
    assert any(up.is_adsb_file(p) for p in up.pending(tmp_path, "wdgwars", cfg))


def test_key_checks(api):
    api.script["/wigle/profile"] = [(200, {"success": True, "userid": "skald"})]
    api.script["/wdg/me"] = [(401, {"error": "bad key"})]
    ok, msg = up.check_wigle(KEYS)
    assert ok and "skald" in msg
    ok, msg = up.check_wdgwars(KEYS)
    assert not ok and "wdgwars.pl" in msg


def test_network_errors_do_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "WIGLE_UPLOAD_URL", "http://127.0.0.1:9/nothing")
    res = up.upload_wigle(_drive(tmp_path), KEYS, UploadConfig(timeout_s=3))
    assert not res.ok and "HTTP 0" in res.message


def test_keys_file_is_private_and_env_wins(tmp_path, monkeypatch):
    path = tmp_path / "keys.json"
    up.save_keys(up.Keys(wigle_name="A", wigle_token="B", wdgwars_key="C"), path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    monkeypatch.setenv("WDGWARS_API_KEY", "from-env")
    k = up.load_keys(path)
    assert (k.wigle_name, k.wigle_token, k.wdgwars_key) == ("A", "B", "from-env")
    assert up.mask("abcdefghijkl") == "abcd…ijkl"
