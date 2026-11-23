#!/usr/bin/env python3
"""
Buat paket update kecil + latest.json untuk GitHub Releases.

    python packaging/make_update.py [--repo owner/nama] [--notes "catatan rilis"]

Hasil di dist/:
    landesk-app-<versi>.zip   kode aplikasi saja (server.py, gui.py, ..., static/)
    latest.json               dibaca LanDesk di tiap PC untuk cek update
"""
import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import updater  # noqa: E402

VERSION = re.search(r'^__version__ = "(.+)"', (ROOT / "server.py").read_text(encoding="utf-8"), re.M).group(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or updater.UPDATE_REPO)
    ap.add_argument("--notes", default=os.environ.get("RELEASE_NOTES", ""))
    ap.add_argument("--base-url", default="",
                    help="URL folder tempat zip di-hosting (untuk mirror LAN). Default: GitHub Releases")
    args = ap.parse_args()

    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    name = f"landesk-app-{VERSION}.zip"
    zpath = out / name
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in updater.APP_ZIP_FILES:
            if f == "VERSION":
                z.writestr("VERSION", VERSION + "\n")
                continue
            data = (ROOT / f).read_bytes().replace(b"\r\n", b"\n")
            z.writestr(f, data)
        for f in sorted((ROOT / "static").rglob("*")):
            if f.is_file():
                z.write(f, f"static/{f.relative_to(ROOT / 'static').as_posix()}")
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    req_hash = updater.requirements_hash((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    latest = {
        "version": VERSION,
        "notes": args.notes,
        "app_zip": name,
        "app_url": (args.base_url.rstrip("/") + "/" + name) if args.base_url
        else f"https://github.com/{args.repo}/releases/download/v{VERSION}/{name}",
        "sha256": sha,
        "req_hash": req_hash,
        "windows_zip": f"LanDesk-{VERSION}-win64-portable.zip",
        "windows_setup": f"LanDesk-Setup-{VERSION}-win64.exe",
        "deb": f"landesk_{VERSION}_all.deb",
    }
    (out / "latest.json").write_text(json.dumps(latest, indent=2), encoding="utf-8")
    print(f"{zpath.name} ({zpath.stat().st_size // 1024} KB) sha256={sha[:12]}..  req={req_hash}")


if __name__ == "__main__":
    main()
