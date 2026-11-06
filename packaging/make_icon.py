"""Draws the FortiGate Upgrader app icon and writes the macOS .icns and Windows .ico.

Design: a charcoal macOS-style rounded square; a red shield built from firewall bricks;
a white upgrade arrow rising out of it. Original artwork (no vendor logos).

    .venv/bin/python packaging/make_icon.py      # -> packaging/icon.png, icon.icns, icon.ico
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

OUT = Path(__file__).parent
S = 1024          # final size
K = 4             # supersampling factor
N = S * K


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vgradient(size, top, bottom):
    w, h = size
    g = Image.new("RGBA", (1, h))
    for y in range(h):
        g.putpixel((0, y), lerp(top, bottom, y / (h - 1)))
    return g.resize((w, h))


def rounded_mask(size, box, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


def shield_path(cx, top, w, h):
    """Shield outline: flat top with rounded shoulders, sides curving into a point."""
    pts = []
    hw = w / 2
    r = w * 0.10
    # top edge with shoulders
    pts += [(cx - hw + r, top), (cx + hw - r, top)]
    for i in range(0, 11):  # right shoulder
        import math
        a = -math.pi / 2 + (math.pi / 2) * i / 10
        pts.append((cx + hw - r + r * math.cos(a), top + r + r * math.sin(a)))
    side = top + h * 0.50
    pts.append((cx + hw, side))
    qx, qy = cx + hw, top + h * 0.86          # control point keeps the curve convex
    for i in range(1, 31):                   # right side down to the tip
        t = i / 30
        x = (1 - t) ** 2 * (cx + hw) + 2 * (1 - t) * t * qx + t * t * cx
        y = (1 - t) ** 2 * side + 2 * (1 - t) * t * qy + t * t * (top + h)
        pts.append((x, y))
    for i in range(29, 0, -1):               # mirror back up the left side
        t = i / 30
        x = (1 - t) ** 2 * (cx - hw) + 2 * (1 - t) * t * (cx - hw) + t * t * cx
        y = (1 - t) ** 2 * side + 2 * (1 - t) * t * qy + t * t * (top + h)
        pts.append((x, y))
    pts.append((cx - hw, side))
    for i in range(0, 11):  # left shoulder
        import math
        a = math.pi + (math.pi / 2) * i / 10
        pts.append((cx - hw + r + r * math.cos(a), top + r + r * math.sin(a)))
    return pts


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # --- tile: macOS grid (824 of 1024, ~185 radius), soft drop shadow
    m = int(N * 0.0977)
    tile_box = (m, m, N - m, N - m)
    radius = int(N * 0.185)
    shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((m, m + N * 0.012, N - m, N - m + N * 0.012), radius=radius,
                                             fill=(0, 0, 0, 120))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(N * 0.012)))
    tile = vgradient((N, N), (58, 64, 72, 255), (22, 25, 29, 255))
    img.paste(tile, (0, 0), rounded_mask((N, N), tile_box, radius))
    # top sheen
    sheen = vgradient((N, N), (255, 255, 255, 34), (255, 255, 255, 0))
    sm = rounded_mask((N, N), tile_box, radius)
    sm = ImageChops.multiply(sm, rounded_mask((N, N), (m, m, N - m, int(N * 0.5)), radius)
                             .filter(ImageFilter.GaussianBlur(N * 0.06)))
    shine = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    shine.paste(sheen, (0, 0), sm)
    img = Image.alpha_composite(img, shine)

    # --- shield of firewall bricks
    cx, top, sw, sh = N / 2, N * 0.215, N * 0.56, N * 0.60
    poly = shield_path(cx, top, sw, sh)
    smask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(smask).polygon(poly, fill=255)
    red = vgradient((N, N), (232, 64, 52, 255), (168, 26, 18, 255))
    bricks = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    bricks.paste(red, (0, 0), smask)
    # mortar lines (brick courses), drawn darker inside the shield
    mortar = Image.new("L", (N, N), 0)
    md = ImageDraw.Draw(mortar)
    rows, lw = 6, int(N * 0.012)
    bh = sh * 0.80 / rows
    bw = sw / 3.0
    y0 = top + sh * 0.03
    for r_ in range(rows + 1):
        y = y0 + r_ * bh
        md.line((cx - sw, y, cx + sw, y), fill=255, width=lw)
        if r_ == rows:
            break
        off = (bw / 2) if r_ % 2 else 0
        x = cx - sw / 2 - bw + off
        while x < cx + sw / 2 + bw:
            md.line((x, y, x, y + bh), fill=255, width=lw)
            x += bw
    mortar = ImageChops.multiply(mortar, smask)
    dark = Image.new("RGBA", (N, N), (110, 14, 10, 255))
    bricks.paste(dark, (0, 0), mortar)
    # shield rim + inner highlight
    rim = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(rim).line(poly + [poly[0]], fill=(255, 196, 186, 150), width=int(N * 0.008), joint="curve")
    img = Image.alpha_composite(img, bricks)
    img = Image.alpha_composite(img, rim)

    # --- upgrade arrow (white, with a soft shadow), rising out of the shield
    aw, shaft_w = N * 0.27, N * 0.095
    tip_y, head_base = N * 0.285, N * 0.455
    shaft_bottom = N * 0.665
    arrow = [(cx, tip_y), (cx + aw / 2, head_base), (cx + shaft_w / 2, head_base),
             (cx + shaft_w / 2, shaft_bottom), (cx - shaft_w / 2, shaft_bottom),
             (cx - shaft_w / 2, head_base), (cx - aw / 2, head_base)]
    ash = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    ImageDraw.Draw(ash).polygon([(x, y + N * 0.012) for x, y in arrow], fill=(40, 0, 0, 150))
    img = Image.alpha_composite(img, ash.filter(ImageFilter.GaussianBlur(N * 0.012)))
    am = Image.new("L", (N, N), 0)
    ImageDraw.Draw(am).polygon(arrow, fill=255)
    am = am.filter(ImageFilter.GaussianBlur(K * 0.6))
    white = vgradient((N, N), (255, 255, 255, 255), (232, 236, 240, 255))
    aimg = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    aimg.paste(white, (0, 0), am)
    img = Image.alpha_composite(img, aimg)

    # --- three progress dots under the shield tip (an upgrade in steps)
    dots = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    r = N * 0.016
    for i, col in enumerate([(95, 208, 138, 255), (95, 208, 138, 255), (95, 208, 138, 110)]):
        x = cx + (i - 1) * N * 0.06
        y = N * 0.865
        dd.ellipse((x - r, y - r, x + r, y + r), fill=col)
    img = Image.alpha_composite(img, dots)

    return img.resize((S, S), Image.LANCZOS)


def main():
    icon = draw_icon()
    png = OUT / "icon.png"
    icon.save(png)
    # Windows .ico (multi-size)
    icon.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # macOS .icns via iconutil (macOS only)
    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as td:
            iset = Path(td) / "icon.iconset"
            iset.mkdir()
            for size in (16, 32, 128, 256, 512):
                icon.resize((size, size), Image.LANCZOS).save(iset / f"icon_{size}x{size}.png")
                icon.resize((size * 2, size * 2), Image.LANCZOS).save(iset / f"icon_{size}x{size}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(OUT / "icon.icns")], check=True)
    print("wrote", png, OUT / "icon.ico", OUT / "icon.icns")


if __name__ == "__main__":
    main()
