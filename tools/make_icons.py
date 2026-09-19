#!/usr/bin/env python3
"""Generate the PWA icons and print them as base64 constants for server.py.

Run on the host (needs Pillow), not in the container:
    python3 tools/make_icons.py >> /tmp/icons.txt

Kept as a script so the icons can be regenerated, but server.py carries the
bytes inline to preserve its single-file, stdlib-only property.
"""
import base64
import io
import textwrap

from PIL import Image, ImageDraw

BASE = "#1e1e2e"    # Catppuccin base, matches the app background
MAUVE = "#cba6f7"   # the accent INDEX_HTML uses for its title


def render(size: int, safe: float) -> bytes:
    """A rounded-square mark. `safe` is the fraction of the canvas kept clear
    so Android's maskable crop cannot clip the glyph."""
    scale = 4  # supersample, then downscale, for clean edges without antialias flags
    px = size * scale
    img = Image.new("RGBA", (px, px), BASE)
    d = ImageDraw.Draw(img)

    inset = px * safe
    box = (inset, inset, px - inset, px - inset)
    d.rounded_rectangle(box, radius=px * 0.16, fill=MAUVE)

    # A serif "T" with a descender bar, reading as TeX without needing a font file.
    w = px - 2 * inset
    stem = w * 0.12
    cx = px / 2
    top = inset + w * 0.24
    d.rectangle((cx - w * 0.28, top, cx + w * 0.28, top + stem), fill=BASE)
    d.rectangle((cx - stem / 2, top, cx + stem / 2, inset + w * 0.76), fill=BASE)
    d.rectangle((cx - stem / 2, inset + w * 0.64, cx + w * 0.30, inset + w * 0.76), fill=BASE)

    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


for name, size, safe in (("ICON_180_B64", 180, 0.02), ("ICON_512_B64", 512, 0.10)):
    b64 = base64.b64encode(render(size, safe)).decode()
    print(f'{name} = (\n' + "\n".join(f'    "{c}"' for c in textwrap.wrap(b64, 76)) + "\n)\n")
