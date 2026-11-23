"""Generate icon LanDesk (icon.ico untuk Windows, icon.png untuk Linux)."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# latar rounded
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=(30, 41, 66, 255))
# monitor
d.rounded_rectangle((96, 120, S - 96, 352), radius=26, fill=(79, 140, 255, 255))
d.rounded_rectangle((120, 144, S - 120, 328), radius=14, fill=(15, 17, 21, 255))
# kaki monitor
d.rectangle((228, 352, 284, 396), fill=(79, 140, 255, 255))
d.rounded_rectangle((176, 392, 336, 420), radius=12, fill=(79, 140, 255, 255))
# panah "remote"
d.polygon([(196, 270), (256, 200), (316, 270), (280, 270), (280, 300), (232, 300), (232, 270)],
          fill=(62, 207, 142, 255))

img.save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon ditulis ke", out)
