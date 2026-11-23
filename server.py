#!/usr/bin/env python3
"""
LanDesk - remote desktop ringan untuk jaringan lokal (Windows & Linux/X11).

Jalankan di komputer yang mau DIKONTROL (host):
    python server.py --password rahasia

Lalu dari komputer lain di LAN buka browser ke:
    http://<ip-host>:8765

Cara kerja singkat:
  * Layar di-capture dengan `mss`, dibagi tile 64x64, hanya tile yang berubah
    yang di-encode JPEG (libjpeg-turbo via `simplejpeg`, fallback Pillow).
  * Dikirim lewat WebSocket biner. Client mengirim ACK setelah frame tergambar,
    server maksimal punya 2 frame "in flight" -> latency tidak menumpuk.
  * Input mouse/keyboard dari browser diinject dengan `pynput`.
"""

import argparse
import asyncio
import hmac
import itertools
import json
import logging
import os
import platform
import secrets
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

__version__ = "1.3.0"


def _run_overlay() -> bool:
    """Jalankan versi hasil auto-update (folder data user) kalau lebih baru dari versi bawaan.
    Return True kalau versi update sudah dijalankan sampai selesai."""
    if os.environ.get("LANDESK_OVERLAY_ACTIVE") or os.environ.get("LANDESK_NO_UPDATE"):
        return False
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "LanDesk" / "updates"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "landesk" / "updates"
    try:
        ver = (root / "current.txt").read_text(encoding="utf-8").strip()
        bad = (root / "bad.txt").read_text(encoding="utf-8").strip() if (root / "bad.txt").exists() else ""
    except OSError:
        return False

    def vt(v):
        return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split("."))

    target = root / ver
    if not ver or ver == bad or vt(ver) <= vt(__version__) or not (target / "server.py").exists():
        return False
    os.environ["LANDESK_OVERLAY_ACTIVE"] = "1"
    os.environ["LANDESK_BASE_DIR"] = str(Path(__file__).resolve().parent)
    sys.path.insert(0, str(target))
    started = time.time()
    try:
        import runpy

        runpy.run_path(str(target / "server.py"), run_name="__main__")
        return True
    except SystemExit:
        return True
    except Exception:
        # versi update gagal start -> tandai rusak, kembali ke versi bawaan
        if time.time() - started < 60:
            try:
                (root / "bad.txt").write_text(ver, encoding="utf-8")
            except OSError:
                pass
        sys.path.remove(str(target))
        for k in ("LANDESK_OVERLAY_ACTIVE", "LANDESK_BASE_DIR"):
            os.environ.pop(k, None)
        for mod in ("server", "gui", "dashboard", "remote", "clipboard", "updater"):
            sys.modules.pop(mod, None)
        return False


if __name__ == "__main__" and _run_overlay():
    sys.exit(0)

# --- DPI awareness di Windows: harus sebelum import mss/pynput supaya koordinat
#     capture dan koordinat mouse sama-sama dalam pixel fisik.
if sys.platform == "win32":
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import mss
import numpy as np
from aiohttp import WSMsgType, web
from PIL import Image
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

import clipboard

try:
    import simplejpeg  # libjpeg-turbo, jauh lebih cepat dari Pillow

    HAVE_SIMPLEJPEG = True
except Exception:  # pragma: no cover
    HAVE_SIMPLEJPEG = False

log = logging.getLogger("landesk")
# PyInstaller menaruh data di sys._MEIPASS
STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "static"
TILE = 64
MAX_IN_FLIGHT = 2


# ----------------------------------------------------------------------------
# Encoding
# ----------------------------------------------------------------------------
def encode_jpeg(arr: np.ndarray, colorspace: str, quality: int) -> bytes:
    """arr: (h, w, 4) uint8, C-contiguous. colorspace: 'BGRA' atau 'RGBX'."""
    if HAVE_SIMPLEJPEG:
        return simplejpeg.encode_jpeg(
            arr, quality=quality, colorspace=colorspace, colorsubsampling="420", fastdct=True
        )
    h, w = arr.shape[:2]
    raw_mode = "BGRX" if colorspace == "BGRA" else "RGBX"
    img = Image.frombuffer("RGB", (w, h), arr.tobytes(), "raw", raw_mode, 0, 1)
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, subsampling=2)
    return buf.getvalue()


def dirty_rects(cur: np.ndarray, prev: np.ndarray):
    """Kembalikan list (x, y, w, h) area yang berubah, digabung per tile."""
    h, w = cur.shape[:2]
    c32 = cur.view(np.uint32).reshape(h, w)
    p32 = prev.view(np.uint32).reshape(h, w)
    ne = c32 != p32
    if not ne.any():
        return []
    rows = np.logical_or.reduceat(ne, np.arange(0, h, TILE), axis=0)
    grid = np.logical_or.reduceat(rows, np.arange(0, w, TILE), axis=1)
    th, tw = grid.shape

    # Kalau perubahan > 55% layar, kirim satu frame penuh saja (lebih efisien).
    if grid.sum() > 0.55 * th * tw:
        return [(0, 0, w, h)]

    # Gabungkan tile kotor jadi run horizontal, lalu gabungkan run yang sama
    # persis di baris berikutnya menjadi persegi panjang.
    open_rects = {}  # (c0, c1) -> [r0, r1]
    out = []
    for r in range(th):
        row = grid[r]
        runs = set()
        c = 0
        while c < tw:
            if row[c]:
                c0 = c
                while c < tw and row[c]:
                    c += 1
                runs.add((c0, c))
            else:
                c += 1
        for key in list(open_rects):
            if key in runs:
                open_rects[key][1] = r + 1
                runs.discard(key)
            else:
                out.append((key, open_rects.pop(key)))
        for key in runs:
            open_rects[key] = [r, r + 1]
    out.extend(open_rects.items())

    rects = []
    for (c0, c1), (r0, r1) in out:
        x, y = c0 * TILE, r0 * TILE
        rects.append((x, y, min(c1 * TILE, w) - x, min(r1 * TILE, h) - y))
    return rects


# ----------------------------------------------------------------------------
# Capture
#   ScreenSource dipakai bersama oleh semua sesi. Di Windows memakai DXGI Desktop
#   Duplication (dxcam, ~2-3 ms/frame & tahu kapan layar tidak berubah), selain
#   itu memakai mss (GDI / X11).
# ----------------------------------------------------------------------------
class ScreenSource:
    def __init__(self, monitor_index, use_dxgi=True):
        self.monitor_index = monitor_index  # None = monitor primary
        self._local = threading.local()
        self._lock = threading.Lock()
        self._seq = 0
        self._frame = None
        self._cam = None
        self._mon = None
        self.backend = "mss"
        if use_dxgi and sys.platform == "win32":
            try:
                self._cam = self._open_dxcam(self.monitor())
                if self._cam is not None:
                    self.backend = "dxgi"
            except Exception as e:
                log.info("DXGI tidak tersedia (%s), pakai mss", e)

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
            self._local.sct = sct
        return sct

    def monitor(self):
        if self._mon is None:
            mons = self._sct().monitors
            idx = self.monitor_index
            if idx is None:
                prim = [i for i, m in enumerate(mons) if i > 0 and m.get("is_primary")]
                idx = prim[0] if prim else 1
            if not 0 <= idx < len(mons):
                idx = 1
            self._mon = {k: mons[idx][k] for k in ("left", "top", "width", "height")}
        return self._mon

    @staticmethod
    def _open_dxcam(mon):
        import dxcam

        factory = getattr(dxcam, "__factory", None) or dxcam.DXFactory()
        for di, outputs in enumerate(factory.outputs):
            for oi, out in enumerate(outputs):
                out.update_desc()
                r = out.desc.DesktopCoordinates
                if (r.left, r.top, r.right - r.left, r.bottom - r.top) == (
                    mon["left"], mon["top"], mon["width"], mon["height"]
                ):
                    try:
                        return dxcam.create(device_idx=di, output_idx=oi, output_color="BGRA",
                                            processor_backend="numpy")
                    except TypeError:  # versi dxcam lama
                        return dxcam.create(device_idx=di, output_idx=oi, output_color="BGRA")
        return None

    def _grab_mss(self):
        shot = self._sct().grab(self.monitor())
        return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4).copy()

    def get(self):
        """Return (seq, frame BGRA). seq naik setiap ada frame baru."""
        with self._lock:
            if self._cam is not None:
                try:
                    f = self._cam.grab()
                except Exception as e:
                    log.debug("dxcam grab error: %s", e)
                    f = None
                if f is not None:
                    self._frame = np.ascontiguousarray(f)
                    self._seq += 1
                elif self._frame is None:  # belum ada frame dari DXGI
                    self._frame = self._grab_mss()
                    self._seq += 1
            else:
                self._frame = self._grab_mss()
                self._seq += 1
            return self._seq, self._frame


class Capturer:
    """State per sesi (frame sebelumnya untuk diff) di atas ScreenSource bersama."""

    def __init__(self, source: ScreenSource):
        self.source = source
        self.prev = None
        self.last_seq = -1
        self.force_full = True

    def monitor(self):
        return self.source.monitor()

    def grab_frame(self, quality: int, scale: float):
        """Return bytes payload siap kirim, atau None kalau tidak ada perubahan."""
        seq, arr = self.source.get()
        if seq == self.last_seq and not self.force_full:
            return None
        self.last_seq = seq
        h, w = arr.shape[:2]
        colorspace = "BGRA"

        if scale < 0.999:
            sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
            img = Image.frombuffer("RGBX", (w, h), arr.tobytes(), "raw", "BGRX", 0, 1)
            img = img.resize((sw, sh), Image.BILINEAR)
            arr = np.asarray(img)
            colorspace = "RGBX"
            w, h = sw, sh

        if self.force_full or self.prev is None or self.prev.shape != arr.shape:
            rects = [(0, 0, w, h)]
            self.force_full = False
        else:
            rects = dirty_rects(arr, self.prev)
        self.prev = arr  # frame dari source tidak pernah diubah in-place

        if not rects:
            return None

        parts = [struct.pack("<BHHH", 1, w, h, len(rects))]
        for x, y, rw, rh in rects:
            sub = np.ascontiguousarray(arr[y : y + rh, x : x + rw])
            jpg = encode_jpeg(sub, colorspace, quality)
            parts.append(struct.pack("<HHHHI", x, y, rw, rh, len(jpg)))
            parts.append(jpg)
        return b"".join(parts)


# ----------------------------------------------------------------------------
# Input injection
# ----------------------------------------------------------------------------
def _k(name):
    return getattr(Key, name, None)


SPECIAL_KEYS = {
    "Enter": _k("enter"), "Backspace": _k("backspace"), "Tab": _k("tab"),
    "Escape": _k("esc"), " ": _k("space"), "Spacebar": _k("space"),
    "ArrowUp": _k("up"), "ArrowDown": _k("down"), "ArrowLeft": _k("left"), "ArrowRight": _k("right"),
    "Shift": _k("shift"), "Control": _k("ctrl"), "Alt": _k("alt"), "AltGraph": _k("alt_gr"),
    "Meta": _k("cmd"), "OS": _k("cmd"), "Super": _k("cmd"),
    "Delete": _k("delete"), "Home": _k("home"), "End": _k("end"),
    "PageUp": _k("page_up"), "PageDown": _k("page_down"), "Insert": _k("insert"),
    "CapsLock": _k("caps_lock"), "NumLock": _k("num_lock"), "ScrollLock": _k("scroll_lock"),
    "ContextMenu": _k("menu"), "PrintScreen": _k("print_screen"), "Pause": _k("pause"),
    "AudioVolumeUp": _k("media_volume_up"), "AudioVolumeDown": _k("media_volume_down"),
    "AudioVolumeMute": _k("media_volume_mute"), "MediaPlayPause": _k("media_play_pause"),
}
for i in range(1, 21):
    SPECIAL_KEYS[f"F{i}"] = _k(f"f{i}")

MOUSE_BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}


class InputInjector:
    def __init__(self, capturer: Capturer):
        self.cap = capturer
        self.mouse = MouseController()
        self.kb = KeyboardController()
        self.mon = None

    def _pos(self, nx, ny):
        if self.mon is None:
            self.mon = self.cap.monitor()
        m = self.mon
        x = m["left"] + min(max(float(nx), 0.0), 1.0) * (m["width"] - 1)
        y = m["top"] + min(max(float(ny), 0.0), 1.0) * (m["height"] - 1)
        return int(round(x)), int(round(y))

    @staticmethod
    def _key(name: str):
        if name in SPECIAL_KEYS and SPECIAL_KEYS[name] is not None:
            return SPECIAL_KEYS[name]
        if len(name) == 1:
            return KeyCode.from_char(name)
        return None

    def handle(self, msg: dict):
        t = msg.get("t")
        try:
            if t == "mm":
                self.mouse.position = self._pos(msg["x"], msg["y"])
            elif t in ("md", "mu"):
                self.mouse.position = self._pos(msg["x"], msg["y"])
                btn = MOUSE_BUTTONS.get(int(msg.get("b", 0)), Button.left)
                (self.mouse.press if t == "md" else self.mouse.release)(btn)
            elif t == "wh":
                dx, dy = int(msg.get("dx", 0)), int(msg.get("dy", 0))
                if dx or dy:
                    self.mouse.scroll(dx, dy)
            elif t in ("kd", "ku"):
                key = self._key(str(msg.get("k", "")))
                if key is not None:
                    (self.kb.press if t == "kd" else self.kb.release)(key)
            elif t == "type":
                text = str(msg.get("text", ""))[:20000]
                self.kb.type(text)
            elif t == "release_all":
                for name in ("shift", "ctrl", "alt", "cmd", "alt_gr"):
                    k = _k(name)
                    if k is not None:
                        try:
                            self.kb.release(k)
                        except Exception:
                            pass
        except Exception as e:  # jangan sampai satu event rusak mematikan sesi
            log.debug("input error %s: %s", msg, e)


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
DISCOVERY_PORT = 8766
_SESSION_IDS = itertools.count(1)


class Session:
    def __init__(self, app_cfg):
        self.cfg = app_cfg
        self.id = next(_SESSION_IDS)
        self.peer = None
        self.monitor = app_cfg["source"].monitor_index
        self.capturer = Capturer(app_cfg["source"])
        self.injector = InputInjector(self.capturer)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self.inflight = asyncio.Semaphore(MAX_IN_FLIGHT)
        self.quality = app_cfg["quality"]
        self.scale = app_cfg["scale"]
        self.fps = app_cfg["fps"]
        self.view_only = app_cfg["view_only"]
        self.closed = False
        self.frames = 0
        self.bytes = 0
        self.ftoken = secrets.token_urlsafe(24)  # akses API file selama sesi hidup
        self.last_clip = None

    def set_monitor(self, idx):
        src = get_source(idx, use_dxgi=self.cfg.get("use_dxgi", True))
        self.capturer = Capturer(src)
        self.injector.cap = self.capturer
        self.injector.mon = None
        self.monitor = idx


async def stream_loop(ws: web.WebSocketResponse, s: Session):
    loop = asyncio.get_running_loop()
    stat_t = time.monotonic()
    while not s.closed and not ws.closed:
        t0 = time.monotonic()
        await s.inflight.acquire()
        cap = s.capturer
        try:
            payload = await loop.run_in_executor(s.executor, cap.grab_frame, s.quality, s.scale)
        except Exception as e:
            s.inflight.release()
            log.warning("capture gagal: %s", e)
            await asyncio.sleep(0.5)
            continue
        if payload is None:
            s.inflight.release()
        else:
            try:
                await ws.send_bytes(payload)
            except Exception:
                break
            s.frames += 1
            s.bytes += len(payload)

        now = time.monotonic()
        if now - stat_t >= 1.0:
            fps = round(s.frames / (now - stat_t), 1)
            kbps = round(s.bytes * 8 / 1000 / (now - stat_t))
            emit(s.cfg, "stats", id=s.id, fps=fps, kbps=kbps)
            try:
                await ws.send_str(json.dumps({"t": "stats", "fps": fps, "kbps": kbps}))
            except Exception:
                break
            s.frames = s.bytes = 0
            stat_t = now

        delay = (1.0 / max(1, s.fps)) - (time.monotonic() - t0)
        if delay > 0:
            await asyncio.sleep(delay)


async def clipboard_loop(ws: web.WebSocketResponse, s: Session):
    """Kirim perubahan clipboard host ke client."""
    loop = asyncio.get_running_loop()
    first = True
    while not s.closed and not ws.closed:
        await asyncio.sleep(1.0)
        if not s.cfg.get("clipboard", True):
            continue
        text = await loop.run_in_executor(None, clipboard.get_text)
        if text is None:
            continue
        if first:  # jangan kirim isi clipboard lama saat baru konek
            s.last_clip, first = text, False
            continue
        if text and text != s.last_clip:
            s.last_clip = text
            try:
                await ws.send_str(json.dumps({"t": "clip", "text": text}))
            except Exception:
                break


def issue_token(cfg, ttl=60) -> str:
    tok = secrets.token_urlsafe(24)
    tokens = cfg.setdefault("tokens", {})
    now = time.time()
    for k in [k for k, exp in tokens.items() if exp < now]:
        tokens.pop(k, None)
    tokens[tok] = now + ttl
    return tok


def check_auth(cfg, data) -> bool:
    if data.get("t") != "auth":
        return False
    tok = data.get("token")
    if tok:
        exp = cfg.get("tokens", {}).pop(str(tok), None)  # sekali pakai
        return bool(exp and exp >= time.time())
    pw = str(data.get("password", ""))
    return hmac.compare_digest(pw.encode(), cfg["password"].encode())


async def ws_handler(request: web.Request):
    app_cfg = request.app["cfg"]
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    peer = request.remote
    ua = request.headers.get("User-Agent", "")

    # --- Autentikasi: pesan pertama wajib {"t":"auth","password":".."} atau {"t":"auth","token":".."}
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=15)
        data = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
    except Exception:
        await ws.close()
        return ws
    if not check_auth(app_cfg, data):
        log.warning("login gagal dari %s", peer)
        emit(app_cfg, "auth_fail", peer=peer)
        await asyncio.sleep(1.5)  # perlambat brute force
        await ws.send_str(json.dumps({"t": "auth_fail"}))
        await ws.close()
        return ws

    # --- Konfirmasi manual di host (opsional)
    confirm = app_cfg.get("confirm_cb")
    if confirm:
        await ws.send_str(json.dumps({"t": "waiting"}))
        try:
            ok = await asyncio.wait_for(asyncio.wrap_future(confirm(peer, ua)), timeout=45)
        except Exception:
            ok = False
        if not ok:
            await ws.send_str(json.dumps({"t": "rejected"}))
            await ws.close()
            return ws

    s = Session(app_cfg)
    s.peer = peer
    mon = await asyncio.get_running_loop().run_in_executor(s.executor, s.capturer.monitor)
    try:
        monitors = await asyncio.get_running_loop().run_in_executor(s.executor, list_monitors)
    except Exception:
        monitors = []
    app_cfg.setdefault("ftokens", {})[s.ftoken] = s
    await ws.send_str(json.dumps({
        "t": "hello", "host": socket.gethostname(), "os": platform.system(),
        "id": app_cfg.get("info", {}).get("id", ""),
        "width": mon["width"], "height": mon["height"],
        "quality": s.quality, "scale": s.scale, "fps": s.fps, "view_only": s.view_only,
        "encoder": "simplejpeg" if HAVE_SIMPLEJPEG else "pillow",
        "capture": app_cfg["source"].backend,
        "monitors": [{"index": i, "label": lbl} for i, lbl in monitors], "monitor": s.monitor,
        "ftoken": s.ftoken, "files": bool(app_cfg.get("allow_files", True)),
        "clipboard": bool(app_cfg.get("clipboard", True)) and clipboard.available(),
    }))
    log.info("client terhubung: %s", peer)
    sessions = app_cfg.setdefault("sessions", {})
    sessions[s.id] = ws
    emit(app_cfg, "connect", id=s.id, peer=peer, ua=ua)

    streamer = asyncio.create_task(stream_loop(ws, s))
    clipper = asyncio.create_task(clipboard_loop(ws, s))
    loop = asyncio.get_running_loop()
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except Exception:
                continue
            t = m.get("t")
            if t == "ack":
                if s.inflight._value < MAX_IN_FLIGHT:  # noqa: SLF001
                    s.inflight.release()
            elif t == "cfg":
                if "quality" in m:
                    s.quality = int(min(max(int(m["quality"]), 10), 95))
                if "fps" in m:
                    s.fps = int(min(max(int(m["fps"]), 1), 120))
                if "scale" in m:
                    s.scale = float(min(max(float(m["scale"]), 0.25), 1.0))
                if "monitor" in m:
                    idx = m["monitor"]
                    idx = None if idx in (None, "", "null") else int(idx)
                    await loop.run_in_executor(s.executor, s.set_monitor, idx)
                    mon = await loop.run_in_executor(s.executor, s.capturer.monitor)
                    await ws.send_str(json.dumps({"t": "screen", "width": mon["width"],
                                                  "height": mon["height"], "monitor": idx}))
                s.capturer.force_full = True
            elif t == "refresh":
                s.capturer.force_full = True
            elif t == "clip":
                text = str(m.get("text", ""))
                if app_cfg.get("clipboard", True) and not s.view_only and text and text != s.last_clip:
                    s.last_clip = text
                    await loop.run_in_executor(None, clipboard.set_text, text)
            elif not s.view_only:
                s.injector.handle(m)
    finally:
        s.closed = True
        streamer.cancel()
        clipper.cancel()
        s.injector.handle({"t": "release_all"})
        s.executor.shutdown(wait=False)
        app_cfg.get("ftokens", {}).pop(s.ftoken, None)
        sessions.pop(s.id, None)
        emit(app_cfg, "disconnect", id=s.id, peer=peer)
        log.info("client putus: %s", peer)
    return ws


# --- API HTTP --------------------------------------------------------------
async def api_info(request):
    return web.json_response(request.app["cfg"].get("info", {}))


async def api_token(request):
    """Client LanDesk menukar password dengan token sekali pakai untuk membuka viewer."""
    cfg = request.app["cfg"]
    try:
        data = await request.json()
    except Exception:
        data = {}
    pw = str(data.get("password", ""))
    if not hmac.compare_digest(pw.encode(), cfg["password"].encode()):
        emit(cfg, "auth_fail", peer=request.remote)
        await asyncio.sleep(1.5)
        return web.json_response({"error": "password salah"}, status=403)
    return web.json_response({"token": issue_token(cfg), "info": cfg.get("info", {})})


def preview_allowed(cfg) -> bool:
    """Pratinjau dashboard ditolak kalau host minta persetujuan manual atau mematikannya."""
    return bool(cfg.get("allow_preview", True)) and not cfg.get("confirm_cb")


_THUMB_CACHE = {}
_THUMB_LOCK = threading.Lock()


def make_thumb(source, width: int) -> bytes:
    """JPEG kecil layar host untuk kartu dashboard (di-cache 1 detik)."""
    from io import BytesIO

    key = (id(source), width)
    with _THUMB_LOCK:
        hit = _THUMB_CACHE.get(key)
        if hit and time.monotonic() - hit[0] < 1.0:
            return hit[1]
    _, arr = source.get()
    h0, w0 = arr.shape[:2]
    img = Image.frombuffer("RGB", (w0, h0), arr.tobytes(), "raw", "BGRX", 0, 1)
    img = img.resize((width, max(1, round(h0 * width / w0))), Image.BILINEAR, reducing_gap=2.0)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=60)
    jpg = buf.getvalue()
    with _THUMB_LOCK:
        _THUMB_CACHE[key] = (time.monotonic(), jpg)
    return jpg


async def api_thumb(request):
    """Pratinjau layar untuk dashboard LanDesk lain. Body: {"password": "...", "w": 320}."""
    cfg = request.app["cfg"]
    try:
        data = await request.json()
    except Exception:
        data = {}
    pw = str(data.get("password", ""))
    if not hmac.compare_digest(pw.encode(), cfg["password"].encode()):
        await asyncio.sleep(1.5)
        return web.json_response({"error": "password salah"}, status=403)
    if not preview_allowed(cfg):
        return web.json_response({"error": "pratinjau dimatikan di komputer ini"}, status=409)
    try:
        width = min(max(int(data.get("w", 320)), 64), 800)
    except (TypeError, ValueError):
        width = 320
    jpg = await asyncio.get_running_loop().run_in_executor(None, make_thumb, cfg["source"], width)
    return web.Response(body=jpg, content_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-LanDesk-Clients": str(len(cfg.get("sessions", {})))})


def _file_session(request):
    cfg = request.app["cfg"]
    tok = request.headers.get("X-LanDesk-Token") or request.query.get("ft", "")
    s = cfg.get("ftokens", {}).get(tok)
    if not s or not cfg.get("allow_files", True):
        raise web.HTTPForbidden(text="akses file tidak diizinkan")
    return s


def _roots():
    if sys.platform == "win32":
        import string

        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return ["/"]


def default_dir() -> Path:
    for p in (Path.home() / "Downloads", Path.home()):
        if p.is_dir():
            return p
    return Path.home()


async def api_files(request):
    _file_session(request)
    raw = request.query.get("path") or str(default_dir())
    p = Path(raw).expanduser()

    def listing():
        entries = []
        with os.scandir(p) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                    if e.name.startswith(".") or (sys.platform == "win32" and st.st_file_attributes & 2):
                        continue  # file tersembunyi
                    entries.append({"name": e.name, "dir": is_dir,
                                    "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)})
                except OSError:
                    continue
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return entries

    try:
        entries = await asyncio.get_running_loop().run_in_executor(None, listing)
    except OSError as e:
        return web.json_response({"error": str(e.strerror or e)}, status=400)
    parent = str(p.parent) if p.parent != p else ""
    return web.json_response({"path": str(p), "parent": parent, "entries": entries,
                              "roots": _roots(), "home": str(Path.home()), "sep": os.sep})


async def api_download(request):
    _file_session(request)
    p = Path(request.query.get("path", ""))
    if not p.is_file():
        raise web.HTTPNotFound(text="file tidak ditemukan")
    from urllib.parse import quote

    return web.FileResponse(p, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(p.name)}"})


async def api_upload(request):
    s = _file_session(request)
    if s.view_only:
        raise web.HTTPForbidden(text="mode hanya lihat")
    dest = Path(request.query.get("path") or default_dir())
    if not dest.is_dir():
        return web.json_response({"error": "folder tujuan tidak ada"}, status=400)
    reader = await request.multipart()
    saved = []
    async for part in reader:
        if not part.filename:
            continue
        name = Path(part.filename).name  # buang path dari client
        target = dest / name
        stem, suf, n = target.stem, target.suffix, 1
        while target.exists():
            target = dest / f"{stem} ({n}){suf}"
            n += 1
        with open(target, "wb") as f:
            while True:
                chunk = await part.read_chunk(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        saved.append(target.name)
        emit(request.app["cfg"], "file", peer=request.remote, name=target.name, dir=str(dest))
    return web.json_response({"saved": saved, "dir": str(dest)})


def emit(cfg, kind, **data):
    cb = cfg.get("on_event")
    if cb:
        try:
            cb(kind, data)
        except Exception:
            pass


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def make_app(cfg) -> web.Application:
    app = web.Application(client_max_size=1024 ** 4)  # upload file besar
    app["cfg"] = cfg
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/info", api_info)
    app.router.add_post("/api/token", api_token)
    app.router.add_post("/api/thumb", api_thumb)
    app.router.add_get("/api/files", api_files)
    app.router.add_get("/api/download", api_download)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_static("/static", STATIC_DIR)
    return app


# --- Discovery UDP ---------------------------------------------------------
class DiscoveryResponder:
    """Menjawab broadcast {"t":"discover"} dari LanDesk lain di LAN."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sock = None
        self.thread = None

    def start(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", DISCOVERY_PORT))
            s.settimeout(0.5)
        except OSError as e:
            log.info("discovery tidak aktif: %s", e)
            return
        self.sock = s
        self.thread = threading.Thread(target=self._run, name="landesk-discovery", daemon=True)
        self.thread.start()

    def _run(self):
        while self.sock:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                if json.loads(data.decode()).get("t") != "discover":
                    continue
                reply = dict(self.cfg.get("info", {}), t="here")
                self.sock.sendto(json.dumps(reply).encode(), addr)
            except Exception:
                continue

    def stop(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass


_SOURCES = {}


def get_source(monitor, use_dxgi=True) -> "ScreenSource":
    """ScreenSource di-cache per monitor (kamera DXGI tidak boleh dibuat dua kali)."""
    key = (monitor, use_dxgi)
    if key not in _SOURCES:
        _SOURCES[key] = ScreenSource(monitor, use_dxgi=use_dxgi)
    return _SOURCES[key]


def list_monitors():
    """[(index, label)] untuk pilihan di GUI. index None = monitor primary."""
    with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as sct:
        mons = sct.monitors
    out = [(None, "Monitor utama (otomatis)")]
    for i, m in enumerate(mons):
        if i == 0:
            if len(mons) > 2:
                out.append((0, f"Semua monitor ({m['width']}x{m['height']})"))
            continue
        tag = " - utama" if m.get("is_primary") else ""
        out.append((i, f"Monitor {i}: {m['width']}x{m['height']}{tag}"))
    return out


def device_id(conf: dict) -> str:
    """ID 9 digit permanen per komputer (seperti ID RustDesk), disimpan di config."""
    if not conf.get("device_id"):
        conf["device_id"] = str(secrets.randbelow(900_000_000) + 100_000_000)
    return conf["device_id"]


def format_id(i: str) -> str:
    i = "".join(ch for ch in str(i) if ch.isdigit())
    return " ".join(i[k:k + 3] for k in range(0, len(i), 3))


def host_info(conf: dict, port: int) -> dict:
    return {"id": device_id(conf), "name": socket.gethostname(), "os": platform.system(),
            "version": __version__, "port": port,
            "preview": bool(conf.get("allow_preview", True)) and not conf.get("confirm")}


class HostServer:
    """Menjalankan server aiohttp (+ discovery UDP) di thread terpisah (dipakai GUI)."""

    def __init__(self, cfg: dict, host="0.0.0.0", port=8765):
        self.cfg, self.host, self.port = cfg, host, port
        self.loop = None
        self.thread = None
        self._stop = None
        self.error = None
        self.discovery = DiscoveryResponder(cfg)

    def start(self, timeout=10.0):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            runner = web.AppRunner(make_app(self.cfg), access_log=None)
            try:
                self.loop.run_until_complete(runner.setup())
                site = web.TCPSite(runner, self.host, self.port)
                self.loop.run_until_complete(site.start())
            except Exception as e:  # port dipakai dll
                self.error = e
                ready.set()
                self.loop.run_until_complete(runner.cleanup())
                self.loop.close()
                return
            self._stop = asyncio.Event()
            ready.set()
            self.loop.run_until_complete(self._stop.wait())
            self.loop.run_until_complete(self._close_all())
            self.loop.run_until_complete(runner.cleanup())
            self.loop.close()

        self.thread = threading.Thread(target=run, name="landesk-server", daemon=True)
        self.thread.start()
        ready.wait(timeout)
        if self.error:
            raise self.error
        self.discovery.start()

    async def _close_all(self):
        for ws in list(self.cfg.get("sessions", {}).values()):
            try:
                await ws.close()
            except Exception:
                pass

    def disconnect_all(self):
        if self.loop and self._stop and not self._stop.is_set():
            asyncio.run_coroutine_threadsafe(self._close_all(), self.loop)

    def stop(self):
        self.discovery.stop()
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        if self.thread:
            self.thread.join(timeout=5)
        self.thread = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # tidak benar-benar mengirim paket
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "LanDesk"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "landesk"
    return base / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(conf: dict):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(conf, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass


def ensure_console():
    """Exe GUI (PyInstaller --windowed) tidak punya console; sambungkan/buat satu untuk mode CLI."""
    if sys.platform != "win32" or sys.stdout is not None:
        return
    import ctypes

    k32 = ctypes.windll.kernel32
    if not k32.AttachConsole(-1):  # console milik terminal yang memanggil
        k32.AllocConsole()
    sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
    sys.stdin = open("CONIN$", "r")


def hide_console():
    """Exe dibangun sebagai console app (lolos Smart App Control); di mode GUI console dilepas."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes

    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    hwnd = k32.GetConsoleWindow()
    if hwnd:
        u32.ShowWindow(hwnd, 0)  # SW_HIDE
    k32.FreeConsole()
    sys.stdout = sys.stderr = open(os.devnull, "w")
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler):
            h.setStream(sys.stderr)


def windows_launch_command(extra=()):
    """Perintah untuk shortcut/autostart: jalankan exe tanpa jendela console."""
    exe = sys.executable
    conhost = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "conhost.exe"
    if getattr(sys, "frozen", False) and conhost.exists():
        return [str(conhost), "--headless", exe, *extra]
    return [exe, *extra]


DEFAULTS = {"port": 8765, "quality": 70, "fps": 30, "scale": 1.0, "monitor": None, "view_only": False}


def main():
    ap = argparse.ArgumentParser(description=f"LanDesk {__version__} - remote desktop host")
    ap.add_argument("--version", action="version", version=f"LanDesk {__version__}")
    ap.add_argument("--host", default="0.0.0.0", help="alamat bind (default 0.0.0.0)")
    ap.add_argument("--port", type=int, help="port (default 8765)")
    ap.add_argument("--password", default=os.environ.get("LANDESK_PASSWORD"),
                    help="password akses (default: dari config, dibuat acak saat pertama jalan)")
    ap.add_argument("--monitor", type=int,
                    help="default: monitor primary; 1,2,.. = monitor ke-n; 0 = semua monitor")
    ap.add_argument("--no-dxgi", action="store_true", help="Windows: paksa capture GDI (mss)")
    ap.add_argument("--quality", type=int, help="kualitas JPEG 10-95 (default 70)")
    ap.add_argument("--fps", type=int, help="batas FPS (default 30)")
    ap.add_argument("--scale", type=float, help="skala resolusi 0.25-1.0 (default 1.0)")
    ap.add_argument("--view-only", action="store_true", default=None, help="hanya lihat, tanpa kontrol")
    ap.add_argument("--save", action="store_true", help="simpan opsi di atas ke file config")
    ap.add_argument("--reset-password", action="store_true", help="buat password acak baru")
    ap.add_argument("--show-config", action="store_true", help="tampilkan lokasi & isi config lalu keluar")
    ap.add_argument("--cli", action="store_true", help="mode console tanpa GUI (server/headless/systemd)")
    ap.add_argument("--minimized", action="store_true", help="GUI: mulai tersembunyi di system tray")
    ap.add_argument("-v", "--verbose", action="store_true")
    if any(a in sys.argv for a in ("--cli", "--show-config", "--version", "-h", "--help")):
        ensure_console()
    elif sys.stdout is None:  # exe GUI (windowed) tanpa console
        sys.stdout = sys.stderr = open(os.devnull, "w")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):  # supaya banner langsung tampil walau di-redirect
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    # --- config permanen: CLI > file config > default
    stored = load_config()
    changed = False
    if args.reset_password or not stored.get("password"):
        stored["password"] = secrets.token_hex(4)
        changed = True
    if args.save:
        for key in ("port", "quality", "fps", "scale", "monitor", "view_only", "password"):
            val = getattr(args, key)
            if val is not None:
                stored[key] = val
                changed = True
    if changed:
        save_config(stored)
    if args.show_config:
        print(config_path())
        print(json.dumps(stored, indent=2))
        return

    def opt(key):
        val = getattr(args, key, None)
        if val is not None:
            return val
        return stored.get(key, DEFAULTS.get(key))

    if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
        log.warning("Sesi Wayland terdeteksi. LanDesk butuh X11 (Xorg). "
                    "Pilih 'Ubuntu on Xorg' / 'GNOME on Xorg' di layar login.")

    if not args.cli:
        try:
            import gui  # tkinter; bisa tidak ada di mesin tanpa layar
        except ImportError as e:
            gui, reason = None, e
        else:
            reason = gui.display_problem()
        if gui and not reason:
            hide_console()
            gui.run(start_minimized=args.minimized, use_dxgi=not args.no_dxgi)
            return
        ensure_console()
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler):
                h.setStream(sys.stderr)
        log.warning("GUI tidak tersedia (%s), pakai mode console", reason)

    password = str(opt("password"))
    port = int(opt("port"))
    source = get_source(opt("monitor"), use_dxgi=not args.no_dxgi)
    had_id = bool(stored.get("device_id"))
    info = host_info(stored, port)
    if not had_id:
        save_config(stored)
    cfg = {
        "password": password, "source": source, "use_dxgi": not args.no_dxgi, "info": info,
        "quality": min(max(int(opt("quality")), 10), 95), "fps": min(max(int(opt("fps")), 1), 120),
        "scale": min(max(float(opt("scale")), 0.25), 1.0), "view_only": bool(opt("view_only")),
        "allow_files": bool(stored.get("allow_files", True)), "clipboard": bool(stored.get("clipboard", True)),
        "allow_preview": bool(stored.get("allow_preview", True)),
    }
    app = make_app(cfg)
    discovery = DiscoveryResponder(cfg)
    discovery.start()

    print("=" * 56)
    print(f" LanDesk {__version__} host aktif")
    print(f"   ID              :  {format_id(info['id'])}  ({info['name']})")
    for ip in local_ips():
        print(f"   buka di browser :  http://{ip}:{port}")
    print(f"   password        :  {password}")
    print(f"   config          :  {config_path()}")
    mon = source.monitor()
    print(f"   layar           :  {mon['width']}x{mon['height']} @ ({mon['left']},{mon['top']})")
    print(f"   capture         :  {source.backend}")
    print(f"   encoder         :  {'simplejpeg' if HAVE_SIMPLEJPEG else 'Pillow'}")
    print("   Ctrl+C / tutup jendela ini untuk berhenti")
    print("=" * 56)
    try:
        web.run_app(app, host=args.host, port=port, print=None, access_log=None)
    except OSError as e:
        log.error("Tidak bisa membuka port %s: %s (LanDesk sudah jalan?)", port, e)
        if getattr(sys, "frozen", False):
            input("Tekan Enter untuk keluar...")
        sys.exit(1)


if __name__ == "__main__":
    sys.modules.setdefault("server", sys.modules["__main__"])  # gui.py `import server` -> modul yang sama
    main()
