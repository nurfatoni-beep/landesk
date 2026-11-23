#!/usr/bin/env python3
"""
Build paket Debian/Ubuntu LanDesk (landesk_<versi>_all.deb).

Bisa dijalankan di Windows maupun Linux (tidak butuh dpkg-deb):
    python packaging/linux/build_deb.py

Isi paket:
    /opt/landesk/server.py, static/, vendor/{mss,pynput}   (mss & pynput murni Python)
    /usr/bin/landesk                                         launcher
    /usr/share/applications/landesk.desktop                  menu aplikasi
    /usr/share/icons/hicolor/256x256/apps/landesk.png
    /usr/lib/systemd/user/landesk.service                    opsional autostart
Dependency lain (numpy, Pillow, aiohttp, Xlib) diambil dari repo apt Ubuntu.
"""
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PKG = HERE.parent
VERSION = re.search(r'^__version__ = "(.+)"', (ROOT / "server.py").read_text(encoding="utf-8"), re.M).group(1)
MTIME = int(time.time())

DEPENDS = ", ".join([
    "python3 (>= 3.9)", "python3-numpy", "python3-pil", "python3-pil.imagetk", "python3-aiohttp",
    "python3-xlib", "python3-six", "python3-tk",
    "libxcb1", "libxcb-randr0", "libxcb-shm0", "libxcb-xfixes0", "libxcb-render0",
])

CONTROL = f"""Package: landesk
Version: {VERSION}
Section: net
Priority: optional
Architecture: all
Depends: {DEPENDS}
Recommends: xclip, python3-gi, gir1.2-ayatanaappindicator3-0.1
Suggests: chromium | google-chrome-stable | microsoft-edge-stable
Maintainer: LanDesk <noreply@localhost>
Homepage: http://localhost/
Description: Remote desktop ringan untuk jaringan lokal
 LanDesk bisa mengontrol komputer lain di LAN sekaligus membuat komputer
 ini bisa dikontrol (ID + password, pencarian otomatis di LAN, transfer
 file, sinkron clipboard). Hanya bagian layar yang berubah yang dikirim
 (tile JPEG lewat WebSocket) sehingga latensi rendah.
 Membutuhkan sesi X11 (Xorg); Wayland belum didukung.
"""

POSTINST = """#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    python3 -m compileall -q /opt/landesk >/dev/null 2>&1 || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
    echo ""
    echo "LanDesk terpasang. Buka 'LanDesk' dari menu aplikasi (atau perintah: landesk)."
    echo "Autostart            :  centang 'Jalankan otomatis saat login' di aplikasi"
    echo "Tanpa GUI (headless) :  landesk --cli   /  systemctl --user enable --now landesk"
    echo "Firewall (ufw)       :  sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp"
    echo "Viewer remote memakai Chromium/Chrome/Edge kalau ada (fallback: browser default)."
    echo "Catatan: butuh sesi Xorg (bukan Wayland)."
fi
exit 0
"""

PRERM = """#!/bin/sh
set -e
find /opt/landesk -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
"""

POSTRM = """#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
exit 0
"""

COPYRIGHT = """LanDesk
Bundled third-party (pure Python) in /opt/landesk/vendor:
 - mss     (MIT License)            https://github.com/BoboTiG/python-mss
 - pynput  (LGPL-3.0)               https://github.com/moses-palmer/pynput
 - pystray (LGPL-3.0)               https://github.com/moses-palmer/pystray
License texts are included in /opt/landesk/vendor/*-LICENSE*.
"""


class Tree:
    """Kumpulan file (path -> (bytes, mode)) untuk dimasukkan ke tar."""

    def __init__(self):
        self.files = {}

    def add(self, path: str, data: bytes, mode: int = 0o644):
        self.files[path.lstrip("/")] = (data, mode)

    def tar_gz(self) -> bytes:
        dirs = set()
        for p in self.files:
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:i]))
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=MTIME) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
                root = tarfile.TarInfo("./")
                root.type, root.mode, root.mtime = tarfile.DIRTYPE, 0o755, MTIME
                tar.addfile(root)
                for d in sorted(dirs):
                    ti = tarfile.TarInfo(f"./{d}/")
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, MTIME
                    ti.uname = ti.gname = "root"
                    tar.addfile(ti)
                for p in sorted(self.files):
                    data, mode = self.files[p]
                    ti = tarfile.TarInfo(f"./{p}")
                    ti.size, ti.mode, ti.mtime = len(data), mode, MTIME
                    ti.uname = ti.gname = "root"
                    tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()


def ar_archive(members) -> bytes:
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{MTIME:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        out.write(header.encode("ascii"))
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def fetch_wheels(dest: Path):
    subprocess.check_call([
        sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--only-binary=:all:",
        "--platform", "manylinux2014_x86_64", "--python-version", "3.10",
        "-d", str(dest), "mss>=10", "pynput>=1.7.6", "pystray>=0.19",
    ])
    return sorted(dest.glob("*.whl"))


def main():
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    data = Tree()

    # aplikasi
    data.add("opt/landesk/server.py", (ROOT / "server.py").read_bytes().replace(b"\r\n", b"\n"), 0o755)
    for mod in ("gui.py", "dashboard.py", "remote.py", "clipboard.py", "updater.py", "requirements.txt"):
        data.add(f"opt/landesk/{mod}", (ROOT / mod).read_bytes().replace(b"\r\n", b"\n"))
    for f in (ROOT / "static").rglob("*"):
        if f.is_file():
            data.add(f"opt/landesk/static/{f.relative_to(ROOT / 'static').as_posix()}", f.read_bytes())

    # vendor: mss & pynput (murni Python)
    with tempfile.TemporaryDirectory() as tmp:
        for whl in fetch_wheels(Path(tmp)):
            pkgname = whl.name.split("-")[0]
            with zipfile.ZipFile(whl) as z:
                for info in z.infolist():
                    n = info.filename
                    if info.is_dir():
                        continue
                    if n.startswith(f"{pkgname}/"):
                        data.add(f"opt/landesk/vendor/{n}", z.read(n))
                    elif ".dist-info/" in n and ("LICENSE" in n.upper() or "COPYING" in n.upper()):
                        data.add(f"opt/landesk/vendor/{pkgname}-{Path(n).name}", z.read(n))
            print("  vendor:", whl.name)

    # integrasi desktop
    def lf(p: Path) -> bytes:  # file dari Windows bisa ber-CRLF
        return p.read_bytes().replace(b"\r\n", b"\n")

    data.add("usr/bin/landesk", lf(HERE / "landesk"), 0o755)
    data.add("usr/share/applications/landesk.desktop", lf(HERE / "landesk.desktop"))
    data.add("usr/lib/systemd/user/landesk.service", lf(HERE / "landesk.service"))
    icon = PKG / "icon.png"
    if not icon.exists():
        subprocess.check_call([sys.executable, str(PKG / "make_icon.py"), str(PKG)])
    from PIL import Image

    b = io.BytesIO()
    Image.open(icon).resize((256, 256), Image.LANCZOS).save(b, "PNG")
    data.add("usr/share/icons/hicolor/256x256/apps/landesk.png", b.getvalue())
    data.add("usr/share/doc/landesk/README.md", (ROOT / "README.md").read_bytes())
    data.add("usr/share/doc/landesk/copyright", COPYRIGHT.encode())

    # control
    installed_kb = sum(len(d) for d, _ in data.files.values()) // 1024 + 1
    md5sums = "".join(f"{hashlib.md5(d).hexdigest()}  {p}\n" for p, (d, _) in sorted(data.files.items()))
    control = Tree()
    control.add("control", (CONTROL.rstrip("\n") + f"\nInstalled-Size: {installed_kb}\n").encode())
    control.add("md5sums", md5sums.encode())
    control.add("postinst", POSTINST.encode(), 0o755)
    control.add("prerm", PRERM.encode(), 0o755)
    control.add("postrm", POSTRM.encode(), 0o755)

    deb = ar_archive([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", control.tar_gz()),
        ("data.tar.gz", data.tar_gz()),
    ])
    target = out_dir / f"landesk_{VERSION}_all.deb"
    target.write_bytes(deb)
    print(f"Selesai: {target} ({len(deb) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
