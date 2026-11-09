"""The Slopetanium mark: a soft-serve swirl (the poop emoji's shape) cut like a gem. Each tier is a rounded band in
front of the one below it (a dark seam between them); inside a tier, a bright crown facet on top and slanted side
facets shaded from the light at the top left. Drawn on a pixel grid of any size."""
import math, os, sys
from PIL import Image

MINT = [(8, 36, 44), (14, 78, 84), (22, 128, 122), (40, 182, 152), (90, 226, 190), (170, 250, 226), (240, 255, 250)]
VIOL = [(30, 22, 60), (62, 44, 120), (104, 80, 190), (150, 126, 236), (200, 186, 255), (232, 226, 255), (250, 248, 255)]
OUT = (5, 12, 18)

# tiers from the top: (centre y, half width, half height); the tip above them
TIERS = [(0.40, 0.26, 0.115), (0.59, 0.37, 0.13), (0.795, 0.48, 0.145)]
TIP = (0.12, 0.34)   # v range of the tip

def region(u, v):
    """(-1 tip / 0.. tier index, local x -1..1, local y -1..1) of the frontmost part at (u, v), or None.
    Upper tiers are in front of lower ones."""
    if TIP[0] <= v <= TIP[1]:
        t = (v - TIP[0]) / (TIP[1] - TIP[0])
        cx = 0.5 + 0.11 * (1 - t) ** 2.2
        hw = 0.025 + 0.17 * t ** 1.2
        if abs(u - cx) <= hw:
            return (-1, (u - cx) / hw, t * 2 - 1)
    for i, (cy, hw, hh) in enumerate(TIERS):
        dx, dy = (u - 0.5) / hw, (v - cy) / hh
        # a band rounder at the bottom than at the top (soft-serve)
        e = dx * dx + (dy * dy if dy > 0 else dy * dy * 1.35)
        if e <= 1:
            return (i, dx, dy)
    return None

def facet(reg, size):
    """The facet a point is on: crossing cuts (diamonds), turned a little per tier (the swirl)."""
    i, lx, ly = reg
    k = 1.7 if size >= 64 else (1.25 if size >= 28 else 0.9)
    tw = 0.45 * (i + 1)
    a = math.floor((lx + ly * 0.9 + tw) * k)
    b = math.floor((-lx + ly * 0.9 + tw) * k)
    return (i, a, b)

def shade(reg, size, f):
    i, lx, ly = reg
    seed = (f[1] * 92837111) ^ (f[2] * 689287499) ^ ((f[0] + 2) * 283923481)
    jitter = ((seed >> 5) % 5) - 2            # each facet its own tilt: -2..2 levels
    light = 3.3 - lx * 2.1 - ly * 0.9         # from the top left: tiers read round
    if ly < -0.45 and i >= 0:
        light += 1.2                           # the crown catches the light
    if i == -1:
        light += 0.6 - lx * 0.8
    level = max(0, min(6, int(round(light + jitter * 0.5))))
    pal = VIOL if (i == 1 and ((seed >> 11) % 3 == 0)) or (i == 2 and ((seed >> 11) % 5 == 0)) else MINT
    return pal, level

def mark(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    regs = [[region((x + 0.5) / size, (y + 0.5) / size) for x in range(size)] for y in range(size)]
    facets = [[facet(regs[y][x], size) if regs[y][x] else None for x in range(size)] for y in range(size)]
    for y in range(size):
        for x in range(size):
            r = regs[y][x]
            if r is None:
                continue
            pal, level = shade(r, size, facets[y][x])
            # the glints: a facet's edge that faces the light (the pixel up or left is another facet)
            up = facets[y - 1][x] if y > 0 else None
            left = facets[y][x - 1] if x > 0 else None
            if size >= 28 and r[1] < 0.2 and ((up is not None and up != facets[y][x] and up[0] == r[0]) or
                                              (left is not None and left != facets[y][x] and left[0] == r[0])):
                level = min(6, level + 1)
            c = pal[level]
            # the seam where the tier above sits on this one
            if y > 0 and regs[y - 1][x] is not None and regs[y - 1][x][0] != r[0] and r[0] > regs[y - 1][x][0]:
                c = MINT[0]
            px[x, y] = c + (255,)
    for y in range(size):
        for x in range(size):
            if regs[y][x] is None and any(0 <= x + dx < size and 0 <= y + dy < size and regs[y + dy][x + dx] is not None
                                          for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                px[x, y] = OUT + (255,)
    spots = [(0.40, 0.355), (0.30, 0.56), (0.24, 0.78), (0.49, 0.20)]
    for (u, v) in spots[: 2 if size < 24 else 4]:
        x, y = int(u * size), int(v * size)
        if regs[y][x] is not None:
            px[x, y] = (255, 255, 255, 255)
            if size >= 40:
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    if regs[y + dy][x + dx] is not None:
                        px[x + dx, y + dy] = MINT[6] + (255,)
    return img

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icon.png")


def icon_mark(size):
    """The mark from icon.png (the crystal swirl drawn for Slopetanium): the artwork cropped from its canvas, the
    generator's faint alpha speckle cleared, padded square and scaled to size x size."""
    im = Image.open(ICON).convert("RGBA")
    a = im.getchannel("A").point(lambda v: 0 if v <= 12 else v)
    im.putalpha(a)
    box = a.point(lambda v: 255 if v > 12 else 0).getbbox()
    im = im.crop(box)
    side = max(im.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    # down in two steps: a smooth shrink keeps the facets' colours, a last nearest-ish step keeps edges crisp
    mid = sq.resize((size * 4, size * 4), Image.LANCZOS)
    return mid.resize((size, size), Image.BOX)


def star(img, cx, cy, r, color=(255, 255, 255, 255)):
    """A four-point pixel sparkle."""
    px = img.load()
    for d in range(-r, r + 1):
        a = 255 if abs(d) < r else 150
        for (x, y) in ((cx + d, cy), (cx, cy + d)):
            if 0 <= x < img.width and 0 <= y < img.height:
                px[x, y] = color[:3] + (a,)
    for (x, y) in ((cx - 1, cy - 1), (cx + 1, cy - 1), (cx - 1, cy + 1), (cx + 1, cy + 1)):
        if r >= 3 and 0 <= x < img.width and 0 <= y < img.height:
            px[x, y] = (170, 250, 226, 200)


def logo(scale, path, font_path):
    """The status bar's logo: the mark and the word, 134 x 20 CSS px at this scale."""
    from PIL import ImageDraw, ImageFont
    W, H = round(134 * scale), round(20 * scale)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    m = icon_mark(H) if os.path.isfile(ICON) else mark(H)
    im.alpha_composite(m, (0, 0))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(font_path, round(17 * scale))
    tx = H + round(4 * scale)
    d.text((tx, H // 2 + round(scale)), "slope", font=f, fill=(62, 217, 176, 255), anchor="lm")
    d.text((tx + d.textlength("slope", font=f), H // 2 + round(scale)), "tanium", font=f, fill=(232, 238, 247, 255), anchor="lm")
    im.save(path)


def cover(path, font_path, font_regular):
    """Slopetanium's own box art (portrait), for its case on the shelf and its cover on the top screen."""
    from PIL import ImageDraw, ImageFont
    W, H = 300, 420
    im = Image.new("RGBA", (W, H), (20, 24, 33, 255))
    d = ImageDraw.Draw(im)
    for y in range(H):
        k = 1 - y / H
        d.line([(0, y), (W, y)], fill=(int(18 + 22 * k), int(22 + 28 * k), int(32 + 36 * k), 255))
    for x in range(-H, W, 14):
        d.line([(x, H), (x + H, 0)], fill=(32, 40, 56, 255), width=1)
    if os.path.isfile(ICON):
        m = icon_mark(210)          # (the icon has its own sparkle)
        im.alpha_composite(m, ((W - 210) // 2, 44))
    else:
        m = mark(64).resize((192, 192), Image.NEAREST)   # pixel art, whole multiples
        im.alpha_composite(m, ((W - 192) // 2, 52))
        for (x, y, r) in ((70, 70, 5), (236, 96, 4), (226, 212, 3), (58, 196, 3)):
            star(im, x, y, r)
    f = ImageFont.truetype(font_path, 44)
    d.text((W // 2, 296), "slope", font=f, fill=(62, 217, 176, 255), anchor="mm")
    d.text((W // 2, 341), "tanium", font=f, fill=(232, 238, 247, 255), anchor="mm")
    f2 = ImageFont.truetype(font_regular, 18)
    d.text((W // 2, 392), "PortMaster updater", font=f2, fill=(142, 154, 176, 255), anchor="mm")
    im.convert("RGB").save(path, optimize=True)


if __name__ == "__main__":
    # tools/make_mark.py <assets dir>: the logos, the cover and the web icon from the mark
    import base64, io, os
    A = sys.argv[1] if len(sys.argv) > 1 else "port/slopetanium/assets"
    fm, fr = os.path.join(A, "fonts", "PixelifySans-Medium.ttf"), os.path.join(A, "fonts", "PixelifySans-Regular.ttf")
    logo(1, os.path.join(A, "logo@1x.png"), fm)
    logo(1.6, os.path.join(A, "logo@1.6x.png"), fm)
    cover(os.path.join(A, "icons", "slopetanium-cover.png"), fm, fr)
    small = icon_mark(32) if os.path.isfile(ICON) else mark(32)
    small.save(os.path.join(A, "icons", "slopetanium-mark.png"))
    if os.path.isfile(ICON):
        icon_mark(256).save(os.path.join(os.path.dirname(ICON), "docs", "icon-256.png"))
    buf = io.BytesIO()
    small.save(buf, "PNG")
    print("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
