"""Cetak catatan rilis satu versi dari CHANGELOG.md (dipakai GitHub Actions).

    python packaging/release_notes.py 1.3.1
"""
import re
import sys
from pathlib import Path

ver = sys.argv[1].lstrip("v")
text = Path(__file__).resolve().parent.parent.joinpath("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## \[{re.escape(ver)}\][^\n]*\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text, re.S | re.M)
print(m.group(1).strip() if m else "")
