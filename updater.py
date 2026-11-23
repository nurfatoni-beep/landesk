"""
Update LanDesk dari GitHub Releases.

Alur:
  1. Cek https://github.com/<repo>/releases/latest/download/latest.json
     (URL download aset, tidak kena rate-limit API GitHub walau banyak PC di balik satu IP).
  2. Kalau versinya lebih baru dan dependency-nya sama dengan yang terpasang, download
     "paket aplikasi" (zip kecil berisi kode), verifikasi SHA-256, ekstrak ke folder data user:
         Windows: %LOCALAPPDATA%\\LanDesk\\updates\\<versi>\\
         Linux  : ~/.local/share/landesk/updates/<versi>/
  3. Tulis updates/current.txt lalu restart. server.py bawaan menjalankan kode di folder update
     kalau versinya lebih baru (lihat _run_overlay di server.py). Tidak butuh admin/sudo.
  4. Kalau dependency berubah, aplikasi minta user memasang paket lengkap dari halaman rilis.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Repo sumber update (owner/nama). Bisa ditimpa lewat config: "update_repo".
UPDATE_REPO = "nurfatoni-beep/landesk"
APP_ZIP_FILES = ("server.py", "gui.py", "dashboard.py", "remote.py", "clipboard.py", "updater.py",
                 "requirements.txt", "VERSION")
USER_AGENT = "LanDesk-Updater"


def vtuple(v: str):
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def overlay_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "LanDesk"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "landesk"
    return base / "updates"


def base_dir() -> Path:
    """Folder aplikasi bawaan installer (bukan folder update)."""
    return Path(os.environ.get("LANDESK_BASE_DIR") or Path(__file__).resolve().parent)


def running_from_update() -> bool:
    return bool(os.environ.get("LANDESK_OVERLAY_ACTIVE"))


def _req_lines(text: str):
    lines = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().replace(" ", "")
        if line:
            lines.append(line.lower())
    return sorted(lines)


def requirements_hash(text: str) -> str:
    return hashlib.sha256("\n".join(_req_lines(text)).encode()).hexdigest()[:16]


def base_requirements_hash():
    for p in (base_dir() / "requirements.txt", base_dir().parent / "requirements.txt"):
        if p.exists():
            return requirements_hash(p.read_text(encoding="utf-8"))
    return None


def repo(conf=None) -> str:
    return (conf or {}).get("update_repo") or UPDATE_REPO


def release_page(conf=None, version=None) -> str:
    r = repo(conf)
    return f"https://github.com/{r}/releases/tag/v{version}" if version else f"https://github.com/{r}/releases/latest"


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


class UpdateError(Exception):
    pass


def check(current_version: str, conf=None, timeout=10):
    """Return dict info rilis kalau ada versi lebih baru, None kalau sudah terbaru.
    Tambahan field: 'compatible' (bisa dipasang sebagai update kecil) dan 'page'."""
    # "update_feed" di config: URL latest.json alternatif (mis. mirror di server LAN warnet)
    url = (conf or {}).get("update_feed") or f"https://github.com/{repo(conf)}/releases/latest/download/latest.json"
    try:
        with _get(url, timeout) as r:
            info = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None  # belum ada rilis
        raise UpdateError(f"Gagal cek update (HTTP {e.code})") from e
    except Exception as e:
        raise UpdateError(f"Gagal cek update: {getattr(e, 'reason', e)}") from e
    if vtuple(info.get("version", "0")) <= vtuple(current_version):
        return None
    base_hash = base_requirements_hash()
    info["compatible"] = bool(info.get("app_zip")) and (base_hash is None or base_hash == info.get("req_hash"))
    info["page"] = release_page(conf, info["version"])
    return info


def download_and_stage(info: dict, conf=None, progress=None) -> Path:
    """Download paket aplikasi, verifikasi, ekstrak, dan tandai sebagai versi aktif."""
    ver = info["version"]
    url = info.get("app_url") or f"https://github.com/{repo(conf)}/releases/download/v{ver}/{info['app_zip']}"
    root = overlay_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp_zip = root / f"landesk-app-{ver}.zip.part"
    h = hashlib.sha256()
    try:
        with _get(url, 60) as r, open(tmp_zip, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    except Exception as e:
        tmp_zip.unlink(missing_ok=True)
        raise UpdateError(f"Download update gagal: {getattr(e, 'reason', e)}") from e
    if info.get("sha256") and h.hexdigest() != info["sha256"]:
        tmp_zip.unlink(missing_ok=True)
        raise UpdateError("File update rusak (checksum tidak cocok). Coba lagi nanti.")

    target = root / ver
    staging = root / f"{ver}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(tmp_zip) as z:
            for m in z.infolist():
                p = Path(m.filename)
                if p.is_absolute() or ".." in p.parts:  # zip-slip
                    raise UpdateError("Isi paket update tidak valid.")
            z.extractall(staging)
        if not (staging / "server.py").exists():
            raise UpdateError("Paket update tidak berisi server.py.")
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    finally:
        tmp_zip.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    (root / "current.txt").write_text(ver, encoding="utf-8")
    (root / "bad.txt").unlink(missing_ok=True)
    cleanup(keep=ver)
    return target


def cleanup(keep: str):
    """Hapus versi update lama (sisakan yang aktif)."""
    root = overlay_root()
    for d in root.iterdir() if root.exists() else []:
        if d.is_dir() and d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


def restart_args(extra=()):
    """Perintah untuk menjalankan ulang LanDesk lewat server.py bawaan (yang memilih versi update)."""
    exe = sys.executable
    if sys.platform == "win32" and Path(exe).name.lower() == "python.exe":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            exe = str(pyw)
    return [exe, str(base_dir() / "server.py"), *extra]


def spawn_restart(extra=()):
    env = dict(os.environ)
    for k in ("LANDESK_OVERLAY_ACTIVE", "LANDESK_BASE_DIR"):
        env.pop(k, None)
    env["LANDESK_RESTART_WAIT"] = "1"  # instance baru menunggu instance lama selesai menutup
    kw = {"env": env, "close_fds": True, "cwd": str(base_dir())}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(restart_args(extra), **kw)
