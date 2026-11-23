"""
Sisi client LanDesk: menemukan komputer lain di LAN, login, dan membuka viewer.

Viewer memakai jendela "app mode" Microsoft Edge / Chrome / Chromium dengan profil
terpisah per host (tidak mengganggu browser utama). Origin host ditandai "secure" supaya
clipboard API dan Keyboard Lock bisa dipakai walau lewat http di LAN.
"""
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

DISCOVERY_PORT = 8766
DEFAULT_PORT = 8765


def _broadcast_addrs():
    addrs = {"255.255.255.255", "127.0.0.1"}  # 127.0.0.1: LanDesk di komputer ini sendiri
    try:
        import server

        for ip in server.local_ips():
            try:
                net = ipaddress.ip_network(f"{ip}/24", strict=False)  # asumsi /24 (umum di LAN)
                addrs.add(str(net.broadcast_address))
            except ValueError:
                pass
    except Exception:
        pass
    return addrs


def discover(timeout=1.5, port=DISCOVERY_PORT):
    """Broadcast ke LAN, kembalikan list dict {id,name,os,version,port,ip}."""
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        s.settimeout(0.2)
        msg = json.dumps({"t": "discover"}).encode()
        for attempt in range(2):
            for addr in _broadcast_addrs():
                try:
                    s.sendto(msg, (addr, port))
                except OSError:
                    pass
            end = time.time() + timeout / 2
            while time.time() < end:
                try:
                    data, (ip, _) = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    info = json.loads(data.decode())
                except Exception:
                    continue
                if info.get("t") != "here" or not info.get("id"):
                    continue
                info["ip"] = ip
                prev = found.get(info["id"])
                # satu komputer bisa menjawab dari beberapa interface -> pilih IP LAN pribadi
                if not prev or (_prefer(ip) and not _prefer(prev["ip"])):
                    found[info["id"]] = info
    finally:
        s.close()
    return sorted(found.values(), key=lambda d: d.get("name", "").lower())


def _prefer(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private and not a.is_loopback and not str(ip).startswith("100.")
    except ValueError:
        return False


def normalize_id(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def parse_target(text: str):
    """'123 456 789' -> ('id', '123456789'); '192.168.1.5[:8765]' / 'nama-pc' -> ('host', host, port)."""
    text = text.strip()
    digits = normalize_id(text)
    if digits and len(digits) == 9 and all(ch.isdigit() or ch.isspace() or ch == "-" for ch in text):
        return ("id", digits)
    host, _, port = text.partition(":")
    return ("host", host, int(port) if port.isdigit() else DEFAULT_PORT)


class RemoteError(Exception):
    """kind: 'auth' (password salah), 'offline', 'disabled' (pratinjau dimatikan), 'notfound', 'other'."""

    def __init__(self, msg, kind="other"):
        super().__init__(msg)
        self.kind = kind


def get_info(host, port=DEFAULT_PORT, timeout=3):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/info", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise RemoteError(f"Tidak bisa terhubung ke {host}:{port} ({_reason(e)})", "offline") from e


def fetch_thumb(host, port, password, width=320, timeout=4) -> bytes:
    """JPEG pratinjau layar host. Raise RemoteError(kind=auth/disabled/offline)."""
    req = urllib.request.Request(
        f"http://{host}:{port}/api/thumb", method="POST",
        data=json.dumps({"password": password, "w": width}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RemoteError("Password salah", "auth") from e
        if e.code == 409:
            raise RemoteError("Pratinjau dimatikan", "disabled") from e
        if e.code == 404:
            raise RemoteError("Versi LanDesk lama (belum ada pratinjau)", "old") from e
        raise RemoteError(f"HTTP {e.code}", "other") from e
    except Exception as e:
        raise RemoteError("Offline", "offline") from e


def request_token(host, port, password, timeout=8):
    req = urllib.request.Request(
        f"http://{host}:{port}/api/token", method="POST",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RemoteError("Password salah.", "auth") from e
        raise RemoteError(f"Host menolak permintaan (HTTP {e.code}).") from e
    except Exception as e:
        raise RemoteError(f"Tidak bisa terhubung ke {host}:{port} ({_reason(e)})", "offline") from e


def _reason(e):
    r = getattr(e, "reason", None) or e
    return str(getattr(r, "strerror", None) or r)


# ----------------------------------------------------------------------------
# Viewer
# ----------------------------------------------------------------------------
def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "LanDesk"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "landesk"
    base.mkdir(parents=True, exist_ok=True)
    return base


def find_browser():
    """Path browser berbasis Chromium yang mendukung --app, atau None."""
    if sys.platform == "win32":
        cands = []
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe",
                      Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
        for c in cands:
            if c.exists():
                return str(c)
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "microsoft-edge-stable", "brave-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _grant_clipboard(profile: Path, origin: str):
    """Izinkan clipboard untuk origin host di profil viewer (supaya tidak ada prompt izin)."""
    prefs_path = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except Exception:
        prefs = {}
    exc = (prefs.setdefault("profile", {}).setdefault("content_settings", {})
           .setdefault("exceptions", {}).setdefault("clipboard", {}))
    key = f"{origin},*"
    if exc.get(key, {}).get("setting") == 1:
        return
    exc[key] = {"setting": 1, "last_modified": str(int(time.time() * 1_000_000))}
    prefs.setdefault("translate", {})["enabled"] = False
    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError:
        pass


def open_viewer(host, port, token, title=""):
    url = f"http://{host}:{port}/?t={token}"
    origin = f"http://{host}:{port}"
    browser = find_browser()
    if not browser:
        webbrowser.open(url)  # fallback: browser default (clipboard otomatis mungkin tidak jalan)
        return "default"
    safe = "".join(ch if ch.isalnum() else "_" for ch in f"{host}_{port}")
    profile = data_dir() / "viewer" / safe
    _grant_clipboard(profile, origin)
    args = [
        browser, f"--app={url}", f"--user-data-dir={profile}",
        f"--unsafely-treat-insecure-origin-as-secure={origin}",
        "--test-type",  # sembunyikan bar "unsupported command-line flag"
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,msUndersideButton,msEdgeTranslate",
        "--disable-sync", "--window-size=1366,820",
    ]
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    return Path(browser).stem


def connect(target_text, password, discovered=None):
    """Resolve ID/IP -> token -> buka viewer. Return dict info host. Raise RemoteError."""
    kind = parse_target(target_text)
    if kind[0] == "id":
        match = next((d for d in (discovered or []) if d.get("id") == kind[1]), None)
        if not match:
            match = next((d for d in discover() if d.get("id") == kind[1]), None)
        if not match:
            raise RemoteError("ID tidak ditemukan di jaringan ini. Pastikan LanDesk di komputer tujuan "
                              "aktif dan berada di LAN yang sama, atau coba pakai alamat IP.", "notfound")
        host, port = match["ip"], int(match.get("port", DEFAULT_PORT))
    else:
        _, host, port = kind
        if not host:
            raise RemoteError("Isi ID atau alamat IP komputer tujuan.")
    return connect_host(host, port, password)


def connect_host(host, port, password):
    """Login ke host yang alamatnya sudah diketahui lalu buka viewer."""
    res = request_token(host, port, password)
    info = dict(res.get("info") or {}, ip=host, port=port)
    info["browser"] = open_viewer(host, port, res["token"], info.get("name", host))
    return info


def connect_device(dev: dict, password: str):
    """Konek ke perangkat dari dashboard: coba IP terakhir, kalau gagal cari ulang lewat ID (IP DHCP berubah)."""
    host, port = dev.get("ip"), int(dev.get("port") or DEFAULT_PORT)
    try:
        if host:
            return connect_host(host, port, password)
    except RemoteError as e:
        if e.kind != "offline" or not dev.get("id"):
            raise
    if dev.get("id"):
        match = next((d for d in discover() if d.get("id") == dev["id"]), None)
        if match:
            return connect_host(match["ip"], int(match.get("port", DEFAULT_PORT)), password)
    raise RemoteError(f"{dev.get('alias') or dev.get('name') or host} sedang offline atau tidak ditemukan di LAN.",
                      "offline")
