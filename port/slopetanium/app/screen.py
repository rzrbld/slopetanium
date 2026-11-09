# SPDX-License-Identifier: MIT
"""Handheld UI: a pixel-styled console home menu (in the spirit of the 3DS menu
and BananaOS's ROCKNIXDS Pixel look; nothing of either is needed at run time:
fonts and icons ship in slopetanium/).

Everything is laid out on a 640x480 grid and scaled to the screen (letterboxed
when it isn't 4:3).

Two screens (SDL sees 2 displays, e.g. RG DS): the top one shows the selected
port in detail (cover, versions, progress; the release notes while its menu is
open; the QR code for Connect), the bottom one the shelf: a bubble with the
name, the row of slots on its plate, the rail and the face buttons.

One screen (H700 and most handhelds): status band, an info card pointing at the
shelf, the shelf, rail and face buttons; Connect and release notes open as
pages of their own (B goes back).
"""

import ctypes
import math
import os
import re
import time

import sdl2
import sdl2.sdlttf as ttf

try:
    import sdl2.sdlimage as sdlimage
except Exception:  # pragma: no cover
    sdlimage = None

try:
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None

from core import human_size


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(round(a * 255)))


def mix(a, t, b):
    """t of a, the rest b (CSS color-mix)."""
    return tuple(int(round(a[i] * t + b[i] * (1 - t))) for i in range(3)) + (255,)


# Dark palette.
P = {
    "ink": hexc("f1ede2"), "muted": hexc("a39d8e"), "line": hexc("8a8374"), "edge": hexc("e8e2d3"),
    "red": hexc("ff3b4a"), "blackish": hexc("0b0a0d"),
    "shade40": (0, 0, 0, 102), "shade45": (0, 0, 0, 115), "shade35": (0, 0, 0, 89),
    "screenBg": hexc("17161c"), "screenFrame": hexc("0b0a0d"), "screenBand": hexc("2e2b33"),
    "ditherA": (255, 255, 255, 36), "ditherB": (255, 255, 255, 18),
    "sheetBase": hexc("15141a"), "sheetHi": (255, 255, 255, 26),
    "panelBand": hexc("3a3540"), "panelBody": hexc("17161c"), "panelLow": hexc("0b0a0d"),
    "chipBand": hexc("2e2b33"), "chipBody": hexc("121117"),
    "frameBg": hexc("17161c"), "meterBg": hexc("15141a"), "brandBg": hexc("262219"),
    "kbdBg": hexc("f1ede2"), "kbdInk": hexc("17161c"), "rule": hexc("45403d"),
    "bubbleBand": hexc("38333b"), "bubbleBody": hexc("191820"), "bubbleLow": hexc("0f0e13"),
    "trackBg": hexc("100f14"), "trackHi": hexc("353039"), "railBg": hexc("100f14"),
    "slotOn": hexc("ffffff"), "faceMuted": hexc("45403d"), "floor": (0, 0, 0, 89),
    "ok": hexc("5fd38d"), "warn": hexc("ffcf5c"),
}
ACCENT = {"base": hexc("f0b429"), "light": hexc("ffe68a"), "dark": hexc("8a5a00")}
ACCENT_INK = hexc("2a2008")  # text on the accent
PORT_ACCENTS = ["e08a4e", "2eae62", "4a9bff", "c25bd6", "e05a6a", "2bb3c0", "d4a72c", "8f7cf0"]
CONNECT_ACCENT = hexc("4a9bff")
SLOT_STEP = 122.0
WIFI = [
    "....######....",
    "..##......##..",
    ".#..........#.",
    "#....####....#",
    "...##....##...",
    "..#........#..",
    "......##......",
    ".....#..#.....",
    "..............",
    "......##......",
    "......##......",
]
FACE_MAX = 220.0


def port_accent(name):
    return hexc(PORT_ACCENTS[sum(map(ord, name or "?")) % len(PORT_ACCENTS)])


def plain_notes(body):
    out = []
    for line in (body or "").splitlines():
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"[*_`>]+", "", line).strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if line.startswith("- "):
            line = "· " + line[2:]
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def ease_back(p):
    """cubic-bezier(0.2, 1.4, 0.32, 1): the shelf's slide, with a little overshoot."""
    if p >= 1:
        return 1.0
    lo, hi, t = 0.0, 1.0, p
    for _ in range(18):
        x = 3 * (1 - t) ** 2 * t * 0.2 + 3 * (1 - t) * t * t * 0.32 + t ** 3
        if x < p:
            lo = t
        else:
            hi = t
        t = (lo + hi) / 2
    return 3 * (1 - t) ** 2 * t * 1.4 + 3 * (1 - t) * t * t * 1.0 + t ** 3


class Box(object):
    """A box in CSS px: rounded, bordered, with bands, dots and shadows."""

    def __init__(self, key, radius=0, border=0, border_color=None, bg=None, bands=(), dither=False,
                 shadows=()):
        self.key, self.radius, self.border, self.border_color = key, radius, border, border_color
        self.bg, self.bands, self.dither, self.shadows = bg, bands, dither, shadows


def B_hero():
    return Box("hero", 18, 3, P["edge"], P["panelBody"], [(0, 8, P["panelBand"])], False,
               [("in", 0, -6, P["panelLow"]), ("out", 4, 4, P["shade45"])])


def B_cover():
    return Box("cover", 4, 0, None, P["frameBg"], (), False, [("out", 4, 4, P["shade40"])])


def B_meter():
    return Box("meter", 2, 3, P["edge"], P["meterBg"], (), False, [("in", 2, 2, P["shade45"])])


def B_brand():
    return Box("brand", 4, 0, None, P["brandBg"], (), False, [("out", 2, 2, P["blackish"])])


def B_kbd():
    return Box("kbd", 4, 0, None, P["kbdBg"])


def B_rule():
    return Box("rule", 2, 0, None, P["rule"])


def B_bubble():
    return Box("bubble", 12, 3, P["line"], P["bubbleBody"], [(0, 10, P["bubbleBand"])], False,
               [("in", 0, -6, P["bubbleLow"]), ("out", 4, 4, P["blackish"])])


def B_track():
    return Box("track", 18, 0, None, P["trackBg"], (), True, [("in", 0, 4, P["shade45"]), ("in", 0, -4, P["trackHi"])])


def B_plate(acc):
    return Box("plate", 18, 3, P["edge"], acc["base"], [(0, 14, acc["light"]), (150, None, acc["dark"])], False,
               [("in", 0, -8, (0, 0, 0, 89)), ("out", 4, 4, P["shade40"])])


def B_slot(on):
    return Box("slot%d" % on, 14, 3, P["slotOn"] if on else P["edge"], P["panelBody"], [(0, 8, P["panelBand"])],
               False, [("in", 0, -6, P["panelLow"]), ("out", 3, 3, P["shade45"])])


def B_badge():
    return Box("badge", 2, 2, hexc("ffffff"), P["red"])


def B_rail():
    return Box("rail", 2, 2, P["edge"], P["railBg"], (), True, [("in", 2, 2, P["shade45"])])


def B_thumb():
    return Box("thumb", 2, 2, P["edge"], ACCENT["base"], [(0, 4, ACCENT["light"])])


def B_face(kind):
    if kind == 2:
        return Box("face2", 10, 3, P["faceMuted"], P["panelBody"], [(0, 8, P["bubbleBand"])])
    return Box("face0", 10, 3, P["edge"], P["panelBody"], [(0, 8, P["bubbleBand"])], False,
               [("in", 0, -4, P["panelLow"]), ("out", 3, 3, P["blackish"])])


def B_sheet(tint):
    return Box("sheet%02x%02x%02x" % tint[:3], 18, 3, P["edge"], tint, (), True,
               [("in", 0, 6, P["sheetHi"]), ("in", 0, -8, P["shade35"]), ("out", 4, 4, P["shade40"])])


def B_pick():
    return Box("pick", 8, 3, P["edge"], ACCENT["base"], [(0, 5, ACCENT["light"])], False, [("in", 0, -4, ACCENT["dark"])])


class Pane(object):
    """A window and its renderer; draws in 640x480 CSS px, scaled and centred."""

    def __init__(self, title, display, flags):
        x = y = sdl2.SDL_WINDOWPOS_CENTERED_DISPLAY(display)
        if flags & sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP:
            b = sdl2.SDL_Rect()
            sdl2.SDL_GetDisplayBounds(display, ctypes.byref(b))
            x, y = b.x, b.y
        size = [int(v) for v in os.environ.get("SLOP_WINDOW", "640x480").split("x")]
        self.win = sdl2.SDL_CreateWindow(title, x, y, size[0], size[1], flags)
        if not self.win:
            raise RuntimeError("SDL_CreateWindow: %s" % sdl2.SDL_GetError().decode())
        self.ren = sdl2.SDL_CreateRenderer(self.win, -1, sdl2.SDL_RENDERER_ACCELERATED | sdl2.SDL_RENDERER_TARGETTEXTURE)
        if not self.ren:
            self.ren = sdl2.SDL_CreateRenderer(self.win, -1, sdl2.SDL_RENDERER_SOFTWARE)
        if not self.ren:
            raise RuntimeError("SDL_CreateRenderer: %s" % sdl2.SDL_GetError().decode())
        sdl2.SDL_SetRenderDrawBlendMode(self.ren, sdl2.SDL_BLENDMODE_BLEND)
        w, h = ctypes.c_int(), ctypes.c_int()
        sdl2.SDL_GetRendererOutputSize(self.ren, ctypes.byref(w), ctypes.byref(h))
        self.W, self.H = w.value, h.value
        self.s = min(self.W / 640.0, self.H / 480.0)
        self.ox = (self.W - 640 * self.s) / 2.0
        self.oy = (self.H - 480 * self.s) / 2.0
        self.fonts = {}
        self.text_cache = {}
        self.box_cache = {}
        self.img_cache = {}
        self.qr_cache = None
        self.hits = []

    def X(self, v):
        return int(round(self.ox + v * self.s))

    def Y(self, v):
        return int(round(self.oy + v * self.s))

    def L(self, v):
        return max(1, int(round(v * self.s)))

    def to_css(self, px, py):
        return (px - self.ox) / self.s, (py - self.oy) / self.s

    def destroy(self):
        for cache in (self.text_cache, self.box_cache, self.img_cache):
            for v in cache.values():
                if v:
                    sdl2.SDL_DestroyTexture(v[0])
        for f in self.fonts.values():
            ttf.TTF_CloseFont(f)
        sdl2.SDL_DestroyRenderer(self.ren)
        sdl2.SDL_DestroyWindow(self.win)


def _set_nearest(tex, nearest):
    try:
        sdl2.SDL_SetTextureScaleMode(tex, sdl2.SDL_ScaleModeNearest if nearest else sdl2.SDL_ScaleModeLinear)
    except Exception:
        pass


def _rects(lst):
    return (sdl2.SDL_Rect * len(lst))(*lst)


class Painter(object):
    """Primitives on a Pane: boxes (painted once into textures), text, images."""

    def __init__(self, fonts_dir, icons_dir):
        self.font_paths = {
            "R": os.path.join(fonts_dir, "PixelifySans-Regular.ttf"),
            "M": os.path.join(fonts_dir, "PixelifySans-Medium.ttf"),
        }
        self.icons_dir = icons_dir

    @staticmethod
    def spans(w, h, r):
        """Per row (x0, x1) of a rounded rect w x h, radius r (pixel-stepped corners)."""
        rows = []
        r = max(0, min(r, w // 2, h // 2))
        for y in range(h):
            if y < r:
                dy = r - y - 0.5
            elif y >= h - r:
                dy = y - (h - r) + 0.5
            else:
                rows.append((0, w))
                continue
            dx = r - int(math.sqrt(max(0.0, r * r - dy * dy)) + 0.5)
            rows.append((dx, w - dx))
        return rows

    def _fill_rows(self, ren, x, y, rows, color, y0=0, y1=None):
        rects = []
        for i, (a, b) in enumerate(rows):
            if i < y0 or (y1 is not None and i >= y1) or b <= a:
                continue
            rects.append(sdl2.SDL_Rect(x + a, y + i, b - a, 1))
        if rects:
            sdl2.SDL_SetRenderDrawColor(ren, *color)
            sdl2.SDL_RenderFillRects(ren, _rects(rects), len(rects))

    def _paint(self, pane, box, w, h):
        """The box as a texture (device px), with room for its outer shadows."""
        ren, s = pane.ren, pane.s
        W, H = max(1, int(round(w * s))), max(1, int(round(h * s)))
        pad = 0
        for sh in box.shadows:
            if sh[0] == "out":
                pad = max(pad, int(math.ceil(max(abs(sh[1]), abs(sh[2])) * s)))
        tex = sdl2.SDL_CreateTexture(ren, sdl2.SDL_PIXELFORMAT_RGBA8888, sdl2.SDL_TEXTUREACCESS_TARGET,
                                     W + pad, H + pad)
        if not tex:
            return None
        sdl2.SDL_SetTextureBlendMode(tex, sdl2.SDL_BLENDMODE_BLEND)
        sdl2.SDL_SetRenderTarget(ren, tex)
        sdl2.SDL_SetRenderDrawBlendMode(ren, sdl2.SDL_BLENDMODE_NONE)
        sdl2.SDL_SetRenderDrawColor(ren, 0, 0, 0, 0)
        sdl2.SDL_RenderClear(ren)
        r = int(round(box.radius * s))
        outer = self.spans(W, H, r)
        for sh in box.shadows:
            if sh[0] == "out":
                self._fill_rows(ren, int(round(sh[1] * s)), int(round(sh[2] * s)), outer, sh[3])
        bw = int(round(box.border * s)) if box.border else 0
        if bw:
            self._fill_rows(ren, 0, 0, outer, box.border_color)
        IW, IH = W - 2 * bw, H - 2 * bw
        inner = self.spans(IW, IH, max(0, r - bw))
        self._fill_rows(ren, bw, bw, inner, box.bg or (0, 0, 0, 0))
        for (b0, b1, c) in box.bands:
            self._fill_rows(ren, bw, bw, inner, c, int(round(b0 * s)), IH if b1 is None else int(round(b1 * s)))
        sdl2.SDL_SetRenderDrawBlendMode(ren, sdl2.SDL_BLENDMODE_BLEND)
        if box.dither:
            d = max(1, int(round(s)))
            step = 4 * s
            ra, rb = [], []
            yy = 0.0
            while yy < IH:
                xx = 0.0
                while xx < IW:
                    for (o, lst) in ((0.0, ra), (2 * s, rb)):
                        px, py = int(xx + o), int(yy + o)
                        if py < IH and px < IW:
                            a, b = inner[py]
                            if a <= px < b:
                                lst.append(sdl2.SDL_Rect(bw + px, bw + py, d, d))
                    xx += step
                yy += step
            for lst, c in ((ra, P["ditherA"]), (rb, P["ditherB"])):
                if lst:
                    sdl2.SDL_SetRenderDrawColor(ren, *c)
                    sdl2.SDL_RenderFillRects(ren, _rects(lst), len(lst))
        for sh in box.shadows:
            if sh[0] != "in":
                continue
            dx, dy = int(round(sh[1] * s)), int(round(sh[2] * s))
            rects = []
            for y in range(IH):
                a, b = inner[y]
                hy = y - dy
                if 0 <= hy < IH:
                    ha, hb = inner[hy][0] + dx, inner[hy][1] + dx
                else:
                    ha = hb = 0
                parts = ((a, min(b, ha)), (max(a, hb), b)) if hb > ha else ((a, b),)
                for (p0, p1) in parts:
                    if p1 > p0:
                        rects.append(sdl2.SDL_Rect(bw + p0, bw + y, p1 - p0, 1))
            if rects:
                sdl2.SDL_SetRenderDrawColor(ren, *sh[3])
                sdl2.SDL_RenderFillRects(ren, _rects(rects), len(rects))
        sdl2.SDL_SetRenderTarget(ren, None)
        return (tex, W + pad, H + pad)

    def box(self, pane, box, x, y, w, h, alpha=255, scale=1.0, cx=None, cy=None):
        key = (box.key, round(w, 2), round(h, 2))
        v = pane.box_cache.get(key)
        if v is None:
            v = self._paint(pane, box, w, h)
            pane.box_cache[key] = v
        if not v:
            return
        tex, tw, th = v
        sdl2.SDL_SetTextureAlphaMod(tex, alpha)
        if scale != 1.0:
            x = cx + (x - cx) * scale
            y = cy + (y - cy) * scale
        sdl2.SDL_RenderCopy(pane.ren, tex, None, sdl2.SDL_Rect(pane.X(x), pane.Y(y),
                                                              int(round(tw * scale)), int(round(th * scale))))

    def fill(self, pane, x, y, w, h, c):
        sdl2.SDL_SetRenderDrawColor(pane.ren, *c)
        sdl2.SDL_RenderFillRect(pane.ren, sdl2.SDL_Rect(pane.X(x), pane.Y(y), pane.L(w), pane.L(h)))

    def triangle(self, pane, x0, x1, xm, ytop, h, c):
        """A pointer down: top edge x0..x1 at ytop, the tip at (xm, ytop + h)."""
        rows = int(round(h * pane.s))
        X0, X1, XM, YT = pane.X(x0), pane.X(x1), pane.X(xm), pane.Y(ytop)
        rects = []
        for i in range(rows):
            t = i / float(max(1, rows))
            a, b = int(X0 + (XM - X0) * t), int(X1 + (XM - X1) * t)
            if b > a:
                rects.append(sdl2.SDL_Rect(a, YT + i, b - a, 1))
        if rects:
            sdl2.SDL_SetRenderDrawColor(pane.ren, *c)
            sdl2.SDL_RenderFillRects(pane.ren, _rects(rects), len(rects))

    # ------------------------------------------------------------ text
    def font(self, pane, w, size):
        px = max(6, int(round(size * pane.s)))
        f = pane.fonts.get((w, px))
        if f is None:
            f = ttf.TTF_OpenFont(self.font_paths[w].encode(), px)
            if not f:
                raise RuntimeError("font %s" % self.font_paths[w])
            pane.fonts[(w, px)] = f
        return f

    def _tex(self, pane, s, w, size, c):
        key = (s, w, size, c)
        if key in pane.text_cache:
            return pane.text_cache[key]
        if len(pane.text_cache) > 900:
            for t in pane.text_cache.values():
                if t:
                    sdl2.SDL_DestroyTexture(t[0])
            pane.text_cache.clear()
        surf = ttf.TTF_RenderUTF8_Blended(self.font(pane, w, size), s.encode("utf-8"), sdl2.SDL_Color(*c))
        v = None
        if surf:
            tex = sdl2.SDL_CreateTextureFromSurface(pane.ren, surf)
            v = (tex, surf.contents.w, surf.contents.h)
            sdl2.SDL_FreeSurface(surf)
        pane.text_cache[key] = v
        return v

    def width(self, pane, s, w, size, ls=0.0, up=False):
        if not s:
            return 0.0
        if up:
            s = s.upper()
        a, b = ctypes.c_int(), ctypes.c_int()
        ttf.TTF_SizeUTF8(self.font(pane, w, size), s.encode("utf-8"), ctypes.byref(a), ctypes.byref(b))
        return a.value / pane.s + ls * len(s)

    def fit(self, pane, s, w, size, maxw, ls=0.0, up=False):
        if self.width(pane, s, w, size, ls, up) <= maxw:
            return s
        while len(s) > 1 and self.width(pane, s + "…", w, size, ls, up) > maxw:
            s = s[:-1]
        return s.rstrip() + "…"

    def text(self, pane, s, x, y, size, c=None, w="R", ls=0.0, up=False, align="left", maxw=None, alpha=255):
        """y: the top of the line (CSS px); returns the width drawn."""
        if not s:
            return 0.0
        c = c or P["ink"]
        if up:
            s = s.upper()
        if maxw:
            s = self.fit(pane, s, w, size, maxw, ls)
        tw = self.width(pane, s, w, size, ls)
        if align == "center":
            x -= tw / 2.0
        elif align == "right":
            x -= tw
        if ls:
            cx = x
            for ch in s:
                v = self._tex(pane, ch, w, size, c)
                if v:
                    sdl2.SDL_SetTextureAlphaMod(v[0], alpha)
                    sdl2.SDL_RenderCopy(pane.ren, v[0], None, sdl2.SDL_Rect(pane.X(cx), pane.Y(y), v[1], v[2]))
                cx += self.width(pane, ch, w, size) + ls
            return tw
        v = self._tex(pane, s, w, size, c)
        if v:
            sdl2.SDL_SetTextureAlphaMod(v[0], alpha)
            sdl2.SDL_RenderCopy(pane.ren, v[0], None, sdl2.SDL_Rect(pane.X(x), pane.Y(y), v[1], v[2]))
        return tw

    def wrap(self, pane, s, w, size, maxw, maxlines, ls=0.0):
        words, lines, cur = (s or "").split(), [], ""
        for i, word in enumerate(words):
            t = (cur + " " + word).strip()
            if self.width(pane, t, w, size, ls) <= maxw or not cur:
                cur = t
            else:
                lines.append(cur)
                cur = word
                if len(lines) == maxlines:
                    lines[-1] = self.fit(pane, lines[-1] + " " + " ".join(words[i:]), w, size, maxw, ls)
                    return lines
        if cur:
            lines.append(self.fit(pane, cur, w, size, maxw, ls))
        return lines[:maxlines]

    # ------------------------------------------------------------ images
    def image(self, pane, path, nearest=False):
        if not path or sdlimage is None:
            return None
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            return None
        if key in pane.img_cache:
            return pane.img_cache[key]
        surf = sdlimage.IMG_Load(path.encode())
        v = None
        if surf:
            tex = sdl2.SDL_CreateTextureFromSurface(pane.ren, surf)
            _set_nearest(tex, nearest)
            v = (tex, surf.contents.w, surf.contents.h)
            sdl2.SDL_FreeSurface(surf)
        pane.img_cache[key] = v
        return v

    def icon(self, pane, name):
        return self.image(pane, os.path.join(self.icons_dir, name + ".png"), nearest=True)

    def draw_image(self, pane, img, x, y, w, h, crop=True, alpha=255):
        tex, iw, ih = img
        src = None
        if crop:
            sa, da = float(iw) / ih, float(w) / h
            if sa > da:
                cw = int(ih * da)
                src = sdl2.SDL_Rect((iw - cw) // 2, 0, cw, ih)
            else:
                ch = int(iw / da)
                src = sdl2.SDL_Rect(0, (ih - ch) // 2, iw, ch)
        sdl2.SDL_SetTextureAlphaMod(tex, alpha)
        sdl2.SDL_RenderCopy(pane.ren, tex, src, sdl2.SDL_Rect(pane.X(x), pane.Y(y), pane.L(w), pane.L(h)))

    def qr(self, pane, data, x, y, size):
        if qrcode is None or not data:
            return
        if not pane.qr_cache or pane.qr_cache[0] != data:
            q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
            q.add_data(data)
            q.make(fit=True)
            pane.qr_cache = (data, q.get_matrix())
        m = pane.qr_cache[1]
        n = len(m)
        cell = max(1, pane.L(size) // n)
        X0 = pane.X(x) + (pane.L(size) - cell * n) // 2
        Y0 = pane.Y(y) + (pane.L(size) - cell * n) // 2
        rects = [sdl2.SDL_Rect(X0 + i * cell, Y0 + j * cell, cell, cell)
                 for j, row in enumerate(m) for i, v in enumerate(row) if v]
        if rects:
            sdl2.SDL_SetRenderDrawColor(pane.ren, 23, 22, 28, 255)
            sdl2.SDL_RenderFillRects(pane.ren, _rects(rects), len(rects))


class Screen(object):
    def __init__(self, core, web, controlfolder, root):
        self.core = core
        self.web = web
        self.root = root
        self.sel = 0
        self.anim = (0.0, 0.0, 0.0)  # from, to, start time
        self.menu = None
        self.page = None   # one screen: "connect" | "notes"
        self.notes_tag = None
        self.msg = None
        self.msg_until = 0
        self.held = set()
        self.pressed = None
        self.quit = False
        self.t0 = time.time()

        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK) != 0:
            raise RuntimeError("SDL_Init: %s" % sdl2.SDL_GetError().decode())
        if ttf.TTF_Init() != 0:
            raise RuntimeError("TTF_Init failed")
        if sdlimage is not None:
            try:
                sdlimage.IMG_Init(sdlimage.IMG_INIT_PNG | sdlimage.IMG_INIT_JPG)
            except Exception:
                pass
        self.pt = Painter(os.path.join(root, "assets", "fonts"), os.path.join(root, "assets", "icons"))
        windowed = os.environ.get("SLOP_WINDOWED") == "1"
        flags = sdl2.SDL_WINDOW_SHOWN | (0 if windowed else sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP)
        ndisp = sdl2.SDL_GetNumVideoDisplays()
        mode = os.environ.get("SLOP_SCREENS", "auto")
        self.dual = mode == "2" or (mode == "auto" and ndisp >= 2)
        if self.dual:
            self.top = Pane(b"Slopetanium", 0, flags)
            self.bottom = Pane(b"Slopetanium shelf", 1 if ndisp >= 2 else 0, flags)
        else:
            self.top = self.bottom = Pane(b"Slopetanium", 0, flags)
        sdl2.SDL_ShowCursor(0)
        self.pads = []
        for i in range(sdl2.SDL_NumJoysticks()):
            self._open_pad(i)
        self.touch_log = 0

    def _open_pad(self, i):
        if sdl2.SDL_IsGameController(i):
            p = sdl2.SDL_GameControllerOpen(i)
            if p:
                self.pads.append(p)

    # ================================================================ model
    def items(self, snap):
        out = [{"kind": "port", "r": r} for r in snap["repos"]]
        out.append({"kind": "connect"})
        return out

    def accent_of(self, it):
        return CONNECT_ACCENT if it["kind"] == "connect" else port_accent(it["r"]["repo"])

    def cur_f(self):
        a, b, t0 = self.anim
        p = min(1.0, (time.time() - t0) / 0.42)
        return a + (b - a) * ease_back(p)

    def select(self, i, n):
        i = max(0, min(n - 1, i))
        if i != self.sel:
            self.anim = (self.cur_f(), float(i), time.time())
            self.sel = i

    def plate_label(self, it):
        if it["kind"] == "connect":
            return "OPEN"
        r = it["r"]
        job = r.get("job")
        if job and job["state"] in ("running", "queued"):
            return "WORKING"
        if r["status"] in ("update", "unknown_version"):
            return "UPDATE"
        if not r["installed"]:
            return "GET"
        return "OPEN"

    def faces(self, snap, it):
        """(left, mids, right): (key, label, kind), kind 2 = inert."""
        if self.menu:
            return ("B", "Back", 0), [], ("A", "OK", 0)
        if self.page:
            return ("B", "Back", 0), [], None
        ups = sum(1 for r in snap["repos"] if r["status"] in ("update", "unknown_version"))
        mids = [("X", "Check", 0)]
        if it["kind"] == "port" and (it["r"].get("originals") or {}).get("files"):
            mids.append(("Y", "Files", 0))
        right = ("START", "Update all", 0) if ups else ("", "Up to date", 2)
        return ("B", "Exit", 0), mids, right

    def title_of(self, it):
        return "Connect" if it["kind"] == "connect" else it["r"]["title"]

    def sub_of(self, it):
        if it["kind"] == "connect":
            if self.web.running:
                us = self.web.urls()
                return us[0].replace("http://", "") if us else "No network"
            return "Manage ports from a browser"
        r = it["r"]
        job = r.get("job")
        if job and job["state"] in ("running", "queued", "failed"):
            return job["message"] or job["state"]
        st = r["status"]
        if st == "update":
            return "Update: %s -> %s" % (r.get("installed_tag"), r.get("latest_tag"))
        if st == "unknown_version":
            return "Installed · latest %s" % r.get("latest_tag")
        if st == "not_installed":
            return "Not installed · %s" % (r.get("latest_tag") or "no release")
        if st == "error":
            return r.get("error") or "Error"
        return "Up to date · %s" % (r.get("installed_tag") or "")

    # ================================================================ shared pieces
    def draw_screen_bg(self, p):
        sdl2.SDL_SetRenderDrawColor(p.ren, *P["screenBg"])
        sdl2.SDL_RenderClear(p.ren)
        self.pt.box(p, Box("screen", 2, 0, None, P["screenBg"], [(0, 8, P["screenBand"])], True,
                           [("in", 0, -4, P["screenFrame"])]), -p.ox / p.s, -p.oy / p.s, p.W / p.s, p.H / p.s)

    def draw_status(self, p):
        pt = self.pt
        col = P["ink"] if self.web.local_ips() else P["line"]
        for j, row in enumerate(WIFI):
            i = 0
            while i < len(row):
                if row[i] == "#":
                    k = i
                    while k < len(row) and row[k] == "#":
                        k += 1
                    pt.fill(p, 17 + i, 24 + j, k - i, 1, col)
                    i = k
                else:
                    i += 1
        x = 44
        bat = battery()
        if bat is not None:
            pct = "%d%%" % bat
            x += pt.text(p, pct, x, 19, 20) + 6
            for (a, b, c, d) in ((0, 0, 24, 2), (0, 12, 24, 2), (0, 0, 2, 14), (22, 0, 2, 14), (24, 4, 2, 6)):
                pt.fill(p, x + a, 25 + b, c, d, P["ink"])
            pt.fill(p, x + 3, 28, max(1, 18 * bat / 100.0), 8, P["red"] if bat <= 15 else P["ink"])
            x += 36
        w1 = pt.width(p, "SLOPE", "M", 17, 1.5)
        w2 = pt.width(p, "TANIUM", "M", 17, 1.5)
        bw = 16 + w1 + w2
        pt.box(p, B_brand(), x, 19, bw, 26)
        pt.text(p, "SLOPE", x + 8, 21, 17, ACCENT["base"], "M", 1.5)
        pt.text(p, "TANIUM", x + 8 + w1, 21, 17, P["ink"], "M", 1.5)
        x += bw + 10
        if self.web.running:
            pt.text(p, "WEB ON", x, 23, 15, CONNECT_ACCENT, "M", 1.2)
        clock = time.strftime("%H:%M")
        cw = pt.width(p, clock, "M", 20)
        pt.text(p, clock, 622 - cw, 19, 20, P["ink"], "M")
        pt.text(p, time.strftime("%m/%d"), 622 - cw - 8, 19, 20, P["ink"], align="right")

    def draw_meter(self, p, x, y, w, frac, color):
        self.pt.box(p, B_meter(), x, y, w, 14)
        if frac is None:
            t = (time.time() * 0.8) % 1.0
            seg = (w - 6) * 0.25
            self.pt.fill(p, x + 3 + (w - 6 - seg) * t, y + 3, seg, 8, color)
        elif frac > 0:
            fw = (w - 6) * min(1.0, frac)
            self.pt.fill(p, x + 3, y + 3, fw, 8, color)
            off = (time.time() * 24) % 8
            sx = x + 3 + off - 8
            while sx < x + 3 + fw:
                a0, a1 = max(x + 3, sx), min(x + 3 + fw, sx + 2)
                if a1 > a0:
                    self.pt.fill(p, a0, y + 3, a1 - a0, 8, (255, 255, 255, 115))
                sx += 8

    def face_w(self, p, f):
        key, label, kind = f
        lw = self.pt.width(p, label, "M", 19)
        if kind == 2 or not key:
            return min(FACE_MAX, 3 + 12 + lw + 12 + 3)
        return min(FACE_MAX, 3 + 12 + 12 + self.pt.width(p, key, "M", 16) + 8 + lw + 12 + 3)

    def draw_face(self, p, f, x, w, pressed=False):
        key, label, kind = f
        dx = 2 if pressed else 0
        self.pt.box(p, B_face(kind), x + dx, 422 + dx, w, 40)
        lx = x + 15 + dx
        if kind != 2 and key:
            kw = 12 + self.pt.width(p, key, "M", 16)
            self.pt.box(p, B_kbd(), lx, 430.5 + dx, kw, 23)
            self.pt.text(p, key, lx + 6, 432 + dx, 16, P["kbdInk"], "M")
            lx += kw + 8
        self.pt.text(p, label, lx, 430 + dx, 19, P["muted"] if kind == 2 else P["ink"], "M",
                     maxw=x + dx + w - 15 - lx)
        p.hits.append(((x, 422, w, 40), ("face", key)))

    def draw_faces(self, p, snap, it):
        left, mids, right = self.faces(snap, it)
        lw = self.face_w(p, left)
        self.draw_face(p, left, 18, lw, self.pressed == left[0])
        rw = 0
        if right:
            rw = self.face_w(p, right)
            self.draw_face(p, right, 622 - rw, rw, self.pressed == right[0])
        if self.msg and time.time() < self.msg_until:
            self.pt.text(p, self.msg, 18 + lw + (604 - lw - rw) / 2.0, 431, 18, P["warn"], "M", align="center",
                         maxw=604 - lw - rw - 20)
            return
        ws = [self.face_w(p, m) for m in mids]
        total = sum(ws) + 10 * (len(ws) - 1) if ws else 0
        x = 18 + lw + (604 - lw - rw - total) / 2.0
        for m, w in zip(mids, ws):
            self.draw_face(p, m, x, w, self.pressed == m[0])
            x += w + 10

    def draw_shelf(self, p, snap, items, ry):
        """Track, plate, slots and the plate's word; ry: the reel's top."""
        pt = self.pt
        n = len(items)
        pt.box(p, B_track(), 26, ry + 34, 588, 80)
        pt.box(p, B_plate(ACCENT), 258, ry + 4, 124, 162)
        cf = self.cur_f()
        sdl2.SDL_RenderSetClipRect(p.ren, sdl2.SDL_Rect(p.X(10), p.Y(ry), p.L(620), p.L(170)))
        for i in sorted(range(n), key=lambda k: -abs(k - cf)):
            d = i - cf
            tx = d * SLOT_STEP
            if abs(tx) > 3.3 * SLOT_STEP:
                continue
            sc = 1.0 - 0.16 * min(1.0, abs(d))
            cx, cy = 320.0 + tx, ry + 16
            on = i == self.sel and abs(d) < 0.5
            pt.box(p, B_slot(on), 272 + tx, ry + 16, 96, 96, 255, sc, cx, cy)
            ix, iy, isz = cx - 40 * sc, cy + 8 * sc, 80 * sc
            it = items[i]
            if it["kind"] == "connect":
                ic = pt.icon(p, "connect")
                if ic:
                    pt.draw_image(p, ic, ix, iy, isz, isz, crop=False)
            else:
                r = it["r"]
                img = pt.image(p, r.get("art"))
                alpha = 255 if r["installed"] else 120
                if img:
                    pt.draw_image(p, img, ix, iy, isz, isz, alpha=alpha)
                else:
                    ic = pt.icon(p, "ports")
                    if ic:
                        pt.draw_image(p, ic, ix, iy, isz, isz, crop=False, alpha=alpha)
                job = r.get("job")
                if job and job["state"] == "running":
                    self.draw_meter(p, ix, iy + isz - 14, isz, job.get("progress"), ACCENT["base"])
                elif r["status"] in ("update", "unknown_version") or (job and job["state"] == "failed"):
                    pt.box(p, B_badge(), cx + 29 * sc, cy + 9 * sc, 10 * sc, 10 * sc)
            p.hits.append(((272 + tx, ry + 16, 96, 96), ("slot", i)))
        sdl2.SDL_RenderSetClipRect(p.ren, None)
        pt.text(p, self.plate_label(items[self.sel]), 320, ry + 124, 20, ACCENT_INK, "M", 2.4, align="center",
                maxw=110)
        p.hits.append(((258, ry + 4, 124, 162), ("slot", self.sel)))

    def draw_rail(self, p, n):
        pt = self.pt
        a, b = "%02d" % (self.sel + 1), " / %02d" % n
        cw = pt.width(p, a, "M", 18) + pt.width(p, b, "R", 18)
        rw = 604 - cw - 16
        pt.box(p, B_rail(), 18, 396, rw, 10)
        span = rw - 4 - 42
        tx = 20 + (span * self.cur_f() / (n - 1) if n > 1 else 0)
        pt.box(p, B_thumb(), max(20, min(20 + span, tx)), 394, 42, 14)
        x = 622 - cw
        x += pt.text(p, a, x, 389, 18, P["ink"], "M")
        pt.text(p, b, x, 389, 18, P["muted"])

    # ================================================================ two screens: bottom
    def draw_bottom_dual(self, p, snap, items):
        pt = self.pt
        it = items[self.sel]
        n = len(items)
        pt.box(p, B_rule(), 284, 31.5, 72, 3)
        if n > 1:
            prev, nxt = self.title_of(items[(self.sel - 1) % n]), self.title_of(items[(self.sel + 1) % n])
            kl = 12 + pt.width(p, "L", "M", 16)
            pt.box(p, B_kbd(), 18, 21.5, kl, 23)
            pt.text(p, "L", 24, 23, 16, P["kbdInk"], "M")
            pt.text(p, prev, 18 + kl + 8, 21, 20, maxw=180)
            kr = 12 + pt.width(p, "R", "M", 16)
            pt.box(p, B_kbd(), 622 - kr, 21.5, kr, 23)
            pt.text(p, "R", 622 - kr + 6, 23, 16, P["kbdInk"], "M")
            pt.text(p, pt.fit(p, nxt, "R", 20, 180), 622 - kr - 8, 21, 20, align="right")
        bh = 2 * 32.5 + 3 + 24 + 18 + 6
        by = 60
        pt.box(p, B_bubble(), 66, by, 508, bh)
        tl = pt.wrap(p, self.title_of(it), "M", 26, 470, 2, 0.52)
        ch = len(tl) * 32.5 + 3 + 24
        cy = by + 3 + 8 + ((bh - 24) - ch) / 2.0
        for i, l in enumerate(tl):
            pt.text(p, l, 320, cy + 32.5 * i, 26, P["ink"], "M", 0.52, align="center")
        job = it["r"].get("job") if it["kind"] == "port" else None
        pt.text(p, self.sub_of(it), 320, cy + len(tl) * 32.5 + 3, 20,
                P["red"] if job and job["state"] == "failed" else P["muted"], align="center", maxw=470)
        pb = by + bh - 3
        pt.triangle(p, 306, 334, 320, pb, 16, P["line"])
        pt.triangle(p, 309, 331, 320, pb, 13, P["bubbleBody"])
        self.draw_shelf(p, snap, items, by + bh + 8)
        self.draw_rail(p, n)
        self.draw_faces(p, snap, it)

    # ================================================================ two screens: top
    def draw_sheet(self, p, tint_accent):
        self.pt.box(p, B_sheet(mix(tint_accent, 0.34, P["sheetBase"])), 18, 60, 604, 406)

    def draw_cover(self, p, it, cx=163, cy=263, maxw=214, maxh=214):
        pt = self.pt
        img = pt.image(p, it["r"].get("art")) if it["kind"] == "port" else None
        if img:
            k = min(maxw / float(img[1]), maxh / float(img[2]))
            w, h = round(img[1] * k), round(img[2] * k)
        else:
            w = h = min(maxw, maxh)
        x, y = round(cx - w / 2.0), round(cy - h / 2.0)
        fw, fy = round(w * 0.78), round(cy + h / 2.0 + 18)
        for r in range(3):
            rw = fw - r * r * 10
            pt.fill(p, cx - rw / 2.0, fy - 1 + r * 2, rw, 2, P["floor"])
        pt.box(p, B_cover(), x, y, w, h)
        if img:
            pt.draw_image(p, img, x, y, w, h, crop=False)
            t = (time.time() - self.t0) % 5.0
            if t < 0.56:  # the shine sweeping over the cover every 5 s
                gx = x - 40 + (w + 40) * (t / 0.56)
                sdl2.SDL_RenderSetClipRect(p.ren, sdl2.SDL_Rect(p.X(x), p.Y(y), p.L(w), p.L(h)))
                pt.fill(p, gx, y, 14, h, (255, 255, 255, 60))
                pt.fill(p, gx + 18, y, 6, h, (255, 255, 255, 40))
                sdl2.SDL_RenderSetClipRect(p.ren, None)
        else:
            ic = pt.icon(p, "connect" if it["kind"] == "connect" else "ports")
            if ic:
                pt.draw_image(p, ic, cx - 48, cy - 48, 96, 96, crop=False)

    def draw_top(self, p, snap, items):
        it = items[self.sel]
        self.draw_status(p)
        self.draw_sheet(p, self.accent_of(it))
        if it["kind"] == "connect":
            return self.draw_connect_sheet(p)
        r = it["r"]
        if self.menu and self.menu.get("notes"):
            return self.draw_notes_sheet(p, r, self.menu["notes"])
        self.draw_cover(p, it)
        self.draw_port_info(p, r, 300, 296, 230)

    def draw_port_info(self, p, r, L, W, SUB_Y):
        pt = self.pt
        tl = pt.wrap(p, r["title"], "M", 26, W, 4, 0.52)
        y = SUB_Y - 4 - len(tl) * 32.5
        for i, l in enumerate(tl):
            pt.text(p, l, L, y + 32.5 * i, 26, P["ink"], "M", 0.52)
        y = SUB_Y
        job = r.get("job")
        sx = L
        if job and job["state"] == "running":  # the live dot
            pt.fill(p, sx - 2, y + 7, 12, 12, P["edge"])
            pt.fill(p, sx, y + 9, 8, 8, P["red"])
            sx += 16
        pt.text(p, r["repo"], sx, y, 20, P["muted"], maxw=W - (sx - L))
        y += 44
        pt.fill(p, L, y - 10, W, 2, P["line"][:3] + (128,))
        vals = [r.get("installed_tag") or ("?" if r["installed"] else "-"), r.get("latest_tag") or "-",
                human_size(r["size"]) if r.get("size") else "-"]
        labs = ["Installed", "Latest", "Size"]
        size = 24
        while True:
            cols = [max(pt.width(p, v, "M", size), pt.width(p, l, "R", 15, 1.2, True)) for v, l in zip(vals, labs)]
            if sum(cols) + 32 <= W or size <= 14:
                break
            size -= 2
        gap = min(28.0, (W - sum(cols)) / 2.0)
        x = L
        for v, l, cw in zip(vals, labs, cols):
            pt.text(p, v, x, y + (24 - size) / 2.0, size, P["ink"], "M", maxw=cw + 2)
            pt.text(p, l, x, y + 28, 15, P["muted"], "R", 1.2, True)
            x += cw + gap
        y += 70
        o = r.get("originals") or {}
        active = job and job["state"] in ("running", "queued", "failed")
        if active:
            label = job["message"] or job["state"]
            frac = job.get("progress") if job["state"] == "running" else (0 if job["state"] == "queued" else 1)
            col = P["red"] if job["state"] == "failed" else ACCENT["base"]
        elif r["status"] in ("update", "unknown_version"):
            label, frac, col = "Update available", 1.0, ACCENT["base"]
        elif o.get("total"):
            label, frac, col = "Original files: %s to free" % human_size(o["total"]), 0, P["muted"]
        elif r["status"] == "current":
            label, frac, col = "Up to date", 1.0, P["ok"]
        else:
            label, frac, col = "Not installed", 0, P["muted"]
        pt.text(p, label, L, y, 16, P["red"] if active and job["state"] == "failed" else P["muted"], "R", 1.0, True,
                maxw=W)
        self.draw_meter(p, L, y + 28, W, frac, col)

    def draw_notes_sheet(self, p, r, tag):
        pt = self.pt
        rel = None
        for x in r.get("releases", []):
            if x["tag"] == tag:
                rel = x
        pt.text(p, "What's new in %s" % tag, 44, 82, 22, ACCENT["base"], "M", maxw=552)
        y = 116
        lines = plain_notes(rel.get("body") if rel else "") or ["No release notes."]
        for line in lines:
            if not line:
                y += 8
                continue
            for l2 in pt.wrap(p, line, "R", 18, 552, 6):
                if y > 430:
                    pt.text(p, "…", 44, y - 6, 18, P["muted"])
                    return
                pt.text(p, l2, 44, y, 18, P["ink"])
                y += 22

    def draw_connect_sheet(self, p):
        pt = self.pt
        L, W = 300, 296
        if not self.web.running:
            self.draw_cover(p, {"kind": "connect"})
            pt.text(p, "Web interface", L, 160, 26, P["ink"], "M", 0.52)
            y = 202
            for l in pt.wrap(p, "Add GitHub repos, update, roll back and clean up from a PC, phone or tablet.",
                             "R", 20, W, 4):
                pt.text(p, l, L, y, 20, P["muted"])
                y += 24
            pt.text(p, "Press A to start it", L, y + 16, 16, ACCENT["base"], "R", 1.0, True)
            return
        urls = self.web.urls()
        pin = self.core.settings().get("pin")
        pt.box(p, Box("qrcard", 6, 3, P["edge"], hexc("ffffff"), (), False, [("out", 4, 4, P["shade40"])]),
               56, 156, 214, 214)
        if urls:
            pt.qr(p, "%s/?pin=%s" % (urls[0], pin), 68, 168, 190)
        else:
            pt.text(p, "No network", 163, 250, 22, P["red"], "M", align="center")
        pt.text(p, "Open in a browser", L, 96, 16, P["muted"], "R", 1.0, True)
        y = 122
        for u in (urls[:2] or ["Connect Wi-Fi first"]):
            pt.text(p, u.replace("http://", ""), L, y, 22, P["ink"], "M", maxw=W)
            y += 30
        pt.fill(p, L, y + 8, W, 2, P["line"][:3] + (128,))
        pt.text(p, "PIN", L, y + 22, 16, P["muted"], "R", 1.0, True)
        pt.text(p, pin, L, y + 44, 56, ACCENT["base"], "M", 8)
        y += 124
        for l in pt.wrap(p, "Same Wi-Fi as this handheld. The QR code fills in the PIN.", "R", 18, W, 3):
            pt.text(p, l, L, y, 18, P["muted"])
            y += 22

    # ================================================================ one screen
    def draw_single(self, p, snap, items):
        pt = self.pt
        it = items[self.sel]
        self.draw_status(p)
        if self.page == "connect":
            self.draw_sheet(p, CONNECT_ACCENT)
            self.draw_connect_sheet(p)
            return self.draw_faces(p, snap, it)
        if self.page == "notes" and it["kind"] == "port":
            self.draw_sheet(p, self.accent_of(it))
            self.draw_notes_sheet(p, it["r"], self.notes_tag)
            return self.draw_faces(p, snap, it)
        # the info card: a bubble with the cover, pointing at the plate
        by, bh = 56, 140
        pt.box(p, B_bubble(), 18, by, 604, bh)
        cx, cy = 102, by + 6 + (bh - 12) / 2.0
        img = pt.image(p, it["r"].get("art")) if it["kind"] == "port" else None
        if img:
            k = min(140 / float(img[1]), 106 / float(img[2]))
            w, h = round(img[1] * k), round(img[2] * k)
            pt.box(p, B_cover(), cx - w / 2.0, cy - h / 2.0, w, h)
            pt.draw_image(p, img, cx - w / 2.0, cy - h / 2.0, w, h, crop=False)
        else:
            ic = pt.icon(p, "connect" if it["kind"] == "connect" else "ports")
            if ic:
                pt.draw_image(p, ic, cx - 40, cy - 40, 80, 80, crop=False)
        L, W = 190, 412
        y = by + 14
        for l in pt.wrap(p, self.title_of(it), "M", 24, W, 2, 0.4):
            pt.text(p, l, L, y, 24, P["ink"], "M", 0.4)
            y += 29
        job = it["r"].get("job") if it["kind"] == "port" else None
        pt.text(p, self.sub_of(it), L, y + 2, 18, P["red"] if job and job["state"] == "failed" else P["muted"],
                maxw=W)
        if it["kind"] == "port":
            r = it["r"]
            o = r.get("originals") or {}
            meta = r["repo"]
            if r.get("size"):
                meta += "  ·  " + human_size(r["size"])
            if o.get("total"):
                meta += "  ·  files " + human_size(o["total"])
            if job and job["state"] == "running":
                self.draw_meter(p, L, by + bh - 36, W, job.get("progress"), ACCENT["base"])
            else:
                pt.text(p, meta, L, by + bh - 38, 16, P["line"], maxw=W)
        else:
            pin = self.core.settings().get("pin")
            pt.text(p, ("PIN %s  ·  A shows the QR code" % pin) if self.web.running
                    else "A starts the web interface", L, by + bh - 38, 16, P["line"], maxw=W)
        pb = by + bh - 3
        pt.triangle(p, 306, 334, 320, pb, 14, P["line"])
        pt.triangle(p, 309, 331, 320, pb, 11, P["bubbleBody"])
        self.draw_shelf(p, snap, items, by + bh + 10)
        self.draw_rail(p, len(items))
        self.draw_faces(p, snap, it)

    # ================================================================ menu
    def draw_menu(self, p):
        pt = self.pt
        m = self.menu
        sdl2.SDL_SetRenderDrawColor(p.ren, 0, 0, 0, 150)
        sdl2.SDL_RenderFillRect(p.ren, None)
        items, lines = m["items"], m.get("lines", [])
        row = 34
        visible = max(1, min(len(items), int((330 - len(lines) * 22) // row)))
        mw = 508
        mh = 16 + 34 + len(lines) * 22 + 8 + visible * row + 12
        mx, my = 66, max(24, (414 - mh) / 2.0)
        pt.box(p, B_hero(), mx, my, mw, mh)
        y = my + 16
        pt.text(p, m["title"], mx + 20, y, 24, P["ink"], "M", 0.4, maxw=mw - 40)
        y += 34
        for s, c in lines:
            pt.text(p, s, mx + 20, y, 17, c, maxw=mw - 40)
            y += 22
        y += 8
        top = m["sel"] - visible + 1 if m["sel"] >= visible else 0
        for i, (label, fn, color) in enumerate(items[top:top + visible]):
            idx = top + i
            ry = y + i * row
            if idx == m["sel"]:
                pt.box(p, B_pick(), mx + 12, ry, mw - 24, row - 4)
                pt.text(p, label, mx + 26, ry + 4, 20, ACCENT_INK, "M", maxw=mw - 52)
            else:
                pt.text(p, label, mx + 26, ry + 4, 20, color if (fn or label == "Cancel") else P["line"], "M",
                        maxw=mw - 52)
            p.hits.append(((mx, ry, mw, row), ("menu", idx)))
        if len(items) > visible:
            pt.text(p, "%d/%d" % (m["sel"] + 1, len(items)), mx + mw - 20, my + 20, 16, P["muted"], align="right")

    # ================================================================ frame
    def draw(self, snap):
        items = self.items(snap)
        if self.sel >= len(items):
            self.sel = len(items) - 1
            self.anim = (float(self.sel), float(self.sel), 0.0)
        if self.dual:
            P1, P2 = self.top, self.bottom
            P2.hits = []
            self.draw_screen_bg(P1)
            self.draw_top(P1, snap, items)
            self.draw_screen_bg(P2)
            self.draw_bottom_dual(P2, snap, items)
            if self.menu:
                self.draw_menu(P2)
                self.draw_faces(P2, snap, items[self.sel])
            self._maybe_capture()
            sdl2.SDL_RenderPresent(P1.ren)
            sdl2.SDL_RenderPresent(P2.ren)
        else:
            p = self.top
            p.hits = []
            self.draw_screen_bg(p)
            self.draw_single(p, snap, items)
            if self.menu:
                self.draw_menu(p)
                self.draw_faces(p, snap, items[self.sel])
            self._maybe_capture()
            sdl2.SDL_RenderPresent(p.ren)

    def flash(self, msg, secs=4):
        self.msg = msg
        self.msg_until = time.time() + secs

    # ================================================================ menus / actions
    def open_menu(self, title, items, lines=None, notes=None):
        self.menu = {"title": title, "items": [(a, b, c if c is not None else P["ink"]) for a, b, c in items],
                     "sel": 0, "lines": lines or [], "notes": notes}

    def port_menu(self, r):
        items = []
        rid = r["id"]
        latest = r.get("latest_tag")
        busy = r.get("job") and r["job"]["state"] in ("running", "queued")
        if busy:
            items.append(("Working: " + (r["job"]["message"] or ""), None, P["line"]))
        elif latest and r["status"] in ("update", "unknown_version"):
            items.append(("Update to " + latest, lambda: self.install(rid, latest), None))
        elif latest and not r["installed"]:
            items.append(("Install " + latest, lambda: self.install(rid, latest), None))
        if r["installed"] and r.get("previous_tag") and not busy:
            prev = r["previous_tag"]
            items.append(("Roll back to " + prev, lambda: self.ask_install(r, prev, "Roll back"), None))
        if len(r.get("releases", [])) > 1 and not busy:
            items.append(("Other versions…", lambda: self.versions_menu(r), None))
        if latest and not self.dual:
            items.append(("Release notes", lambda: self.show_notes(latest), None))
        o = r.get("originals") or {}
        if o.get("files"):
            items.append(("Original files (%s)…" % human_size(o["total"]), lambda: self.originals_menu(r), None))
        items.append(("Check for updates", lambda: (self.core.queue_check(rid), self.flash("Checking…")), None))
        if r["installed"] and not busy:
            items.append(("Uninstall…", lambda: self.uninstall_menu(r), P["red"]))
        self.open_menu(r["title"], items, [(r["repo"], P["muted"])], notes=latest if self.dual else None)

    def show_notes(self, tag):
        self.notes_tag = tag
        self.page = "notes"

    def versions_menu(self, r):
        items = []
        for rel in r.get("releases", []):
            if not rel.get("asset"):
                continue
            label = rel["tag"] + ("  pre" if rel["prerelease"] else "") + \
                ("  · installed" if rel["installed"] else "") + ("  · on device" if rel["cached"] else "")
            tag = rel["tag"]
            fn = (lambda tag=tag: self.ask_install(r, tag, "Install")) if not rel["installed"] else None
            items.append((label, fn, P["ok"] if rel["installed"] else None))
        self.open_menu("Versions", items or [("No versions", None, P["line"])], [(r["title"], P["muted"])],
                       notes=r.get("latest_tag") if self.dual else None)

    def ask_install(self, r, tag, verb):
        rid = r["id"]
        self.open_menu("%s %s?" % (verb, tag), [(verb, lambda: self.install(rid, tag), None),
                                                ("Cancel", None, P["muted"])],
                       [("%s -> %s" % (r.get("installed_tag") or "-", tag), ACCENT["base"]),
                        ("Imported game files, settings and saves are kept.", P["muted"])],
                       notes=tag if self.dual else None)

    def install(self, rid, tag):
        try:
            self.core.queue_install(rid, tag)
            self.flash("Queued %s" % tag)
        except Exception as e:
            self.flash(str(e))

    def originals_menu(self, r):
        o = r.get("originals") or {}
        rid = r["id"]
        if o.get("imported_hint"):
            hint = ("Imported already (%s/): safe to delete." % o["imported_hint"], P["ok"])
        else:
            hint = ("Nothing imported yet: start the port once first!", P["red"])
        lines = [("%d item%s in ports/%s" % (len(o["files"]), "" if len(o["files"]) == 1 else "s", o.get("dir")),
                  P["muted"]), hint]
        for f in o["files"][:3]:
            lines.append(("  %s  %s" % (f["name"], human_size(f["size"])), P["line"]))
        if len(o["files"]) > 3:
            lines.append(("  …and %d more" % (len(o["files"]) - 3), P["line"]))
        self.open_menu("Delete original files?", [("Delete %s" % human_size(o["total"]),
                                                   lambda: self._delete_originals(rid), P["red"]),
                                                  ("Cancel", None, P["muted"])], lines)

    def _delete_originals(self, rid):
        try:
            self.flash("Freed %s" % human_size(self.core.delete_originals(rid)))
        except Exception as e:
            self.flash(str(e))

    def uninstall_menu(self, r):
        rid = r["id"]
        keep = ", ".join(r.get("keep_on_uninstall") or []) or "nothing"
        self.open_menu("Uninstall?", [
            ("Uninstall, back up %s" % keep, lambda: self._uninstall(rid, True), P["red"]),
            ("Uninstall, no backup", lambda: self._uninstall(rid, False), P["red"]),
            ("Cancel", None, P["muted"])],
            [(r["title"], P["muted"]), ("Removes ports/%s with its data and saves." % r.get("port_dir"), P["warn"])])

    def _uninstall(self, rid, backup):
        try:
            self.core.queue_uninstall(rid, backup)
            self.flash("Uninstalling…")
        except Exception as e:
            self.flash(str(e))

    def open_item(self, it):
        if it["kind"] == "connect":
            try:
                self.web.start()
            except Exception as e:
                self.flash("Web interface failed: %s" % e)
                return
            if not self.dual:
                self.page = "connect"
        else:
            self.port_menu(it["r"])

    def update_all(self):
        n = len(self.core.queue_update_all())
        self.flash("%d update%s queued" % (n, "" if n == 1 else "s") if n else "Nothing to update")

    def try_quit(self):
        if self.core.busy():
            self.flash("Still installing: exit when it's done")
        else:
            self.quit = True

    # ================================================================ input
    def act(self, b, snap):
        items = self.items(snap)
        n = len(items)
        if self.menu:
            m = self.menu
            if b == "up":
                m["sel"] = (m["sel"] - 1) % len(m["items"])
            elif b == "down":
                m["sel"] = (m["sel"] + 1) % len(m["items"])
            elif b == "a":
                label, fn = m["items"][m["sel"]][0], m["items"][m["sel"]][1]
                if fn is None and label != "Cancel":
                    return
                self.menu = None
                if fn:
                    fn()
            elif b == "b":
                self.menu = None
            return
        if self.page:
            if b in ("b", "a"):
                self.page = None
            return
        if b == "left":
            self.select(self.sel - 1, n)
        elif b == "right":
            self.select(self.sel + 1, n)
        elif b == "l":
            self.select(max(0, self.sel - 4), n)
        elif b == "r":
            self.select(min(n - 1, self.sel + 4), n)
        elif b == "a":
            self.open_item(items[self.sel])
        elif b == "b":
            self.try_quit()
        elif b == "x":
            self.core.queue_check()
            self.flash("Checking GitHub…")
        elif b == "start":
            self.update_all()
        elif b == "y":
            it = items[self.sel]
            if it["kind"] == "port" and (it["r"].get("originals") or {}).get("files"):
                self.originals_menu(it["r"])
            else:
                self.flash("No original files here")

    FACE_KEYS = {"A": "a", "B": "b", "X": "x", "Y": "y", "START": "start"}

    def touch(self, ev, snap):
        f = ev.tfinger
        p = self.bottom
        if os.environ.get("SLOP_TOUCH_LOG") and self.touch_log < 40:
            self.touch_log += 1
            devs = [sdl2.SDL_GetTouchDevice(i) for i in range(sdl2.SDL_GetNumTouchDevices())]
            print("[slopetanium] touch id=%s devices=%s x=%.3f y=%.3f" % (f.touchId, devs, f.x, f.y), flush=True)
        if self.dual:
            want = os.environ.get("SLOP_TOUCH_NAV")
            if want is not None:
                for i in range(sdl2.SDL_GetNumTouchDevices()):
                    if sdl2.SDL_GetTouchDevice(i) == f.touchId and str(i) != want:
                        return
        x, y = p.to_css(f.x * p.W, f.y * p.H)
        for (hx, hy, hw, hh), what in reversed(p.hits):
            if hx <= x <= hx + hw and hy <= y <= hy + hh:
                kind, v = what
                if kind == "menu" and self.menu:
                    if self.menu["sel"] == v:
                        self.act("a", snap)
                    else:
                        self.menu["sel"] = v
                elif kind == "slot" and not self.menu and not self.page:
                    if v == self.sel:
                        self.act("a", snap)
                    else:
                        self.select(v, len(self.items(snap)))
                elif kind == "face":
                    k = self.FACE_KEYS.get(v)
                    if k:
                        self.act(k, snap)
                return
        if self.menu:
            self.menu = None

    def run(self):
        ev = sdl2.SDL_Event()
        snap = self.core.snapshot()
        if not snap["repos"]:
            self.open_item({"kind": "connect"})  # nothing watched yet: how to connect, at once
        last_snap = 0
        keymap = {
            sdl2.SDLK_UP: "up", sdl2.SDLK_DOWN: "down", sdl2.SDLK_LEFT: "left", sdl2.SDLK_RIGHT: "right",
            sdl2.SDLK_RETURN: "a", sdl2.SDLK_a: "a", sdl2.SDLK_ESCAPE: "b", sdl2.SDLK_BACKSPACE: "b",
            sdl2.SDLK_b: "b", sdl2.SDLK_x: "x", sdl2.SDLK_y: "y", sdl2.SDLK_s: "start",
            sdl2.SDLK_PAGEUP: "l", sdl2.SDLK_PAGEDOWN: "r",
        }
        padmap = {
            sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP: "up", sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN: "down",
            sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT: "left", sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT: "right",
            sdl2.SDL_CONTROLLER_BUTTON_A: "a", sdl2.SDL_CONTROLLER_BUTTON_B: "b",
            sdl2.SDL_CONTROLLER_BUTTON_X: "x", sdl2.SDL_CONTROLLER_BUTTON_Y: "y",
            sdl2.SDL_CONTROLLER_BUTTON_START: "start", sdl2.SDL_CONTROLLER_BUTTON_BACK: "select",
            sdl2.SDL_CONTROLLER_BUTTON_GUIDE: "guide",
            sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER: "l", sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: "r",
        }
        btn_face = {"a": "A", "b": "B", "x": "X", "y": "Y", "start": "START"}
        axes = {}
        repeat = {"b": None, "t": 0}
        while not self.quit:
            while sdl2.SDL_PollEvent(ctypes.byref(ev)):
                t = ev.type
                b = None
                if t == sdl2.SDL_QUIT:
                    self.quit = True
                elif t == sdl2.SDL_KEYDOWN and not ev.key.repeat:
                    b = keymap.get(ev.key.keysym.sym)
                elif t == sdl2.SDL_CONTROLLERBUTTONDOWN:
                    b = padmap.get(ev.cbutton.button)
                    if b:
                        self.held.add(b)
                        self.pressed = btn_face.get(b)
                        if b in ("up", "down", "left", "right"):
                            repeat["b"], repeat["t"] = b, time.time() + 0.4
                elif t == sdl2.SDL_CONTROLLERBUTTONUP:
                    nb = padmap.get(ev.cbutton.button)
                    self.held.discard(nb)
                    self.pressed = None
                    if repeat["b"] == nb:
                        repeat["b"] = None
                elif t == sdl2.SDL_CONTROLLERAXISMOTION and ev.caxis.axis in (
                        sdl2.SDL_CONTROLLER_AXIS_LEFTX, sdl2.SDL_CONTROLLER_AXIS_LEFTY):
                    v = ev.caxis.value
                    nv = -1 if v < -16000 else (1 if v > 16000 else 0)
                    ax = ev.caxis.axis
                    if nv and nv != axes.get(ax, 0):
                        if ax == sdl2.SDL_CONTROLLER_AXIS_LEFTX:
                            b = "left" if nv < 0 else "right"
                        else:
                            b = "up" if nv < 0 else "down"
                    axes[ax] = nv
                elif t == sdl2.SDL_CONTROLLERDEVICEADDED:
                    self._open_pad(ev.cdevice.which)
                elif t == sdl2.SDL_FINGERDOWN:
                    self.touch(ev, snap)
                if b:
                    if ("select" in self.held and "start" in self.held) or b == "guide":
                        self.try_quit()
                        continue
                    if b == "select":
                        continue
                    self.act(b, snap)
            if repeat["b"] and time.time() > repeat["t"]:
                self.act(repeat["b"], snap)
                repeat["t"] = time.time() + 0.12
            nowt = time.time()
            if nowt - last_snap > 1.0 or self.core.changed != getattr(self, "_changed", -1):
                snap = self.core.snapshot()
                self._changed = self.core.changed
                last_snap = nowt
            self.draw(snap)
            shot = os.environ.get("SLOP_SCREENSHOT")
            if shot:
                script = os.environ.get("SLOP_SCRIPT")
                if script and not getattr(self, "_scripted", False) and nowt - self.t0 > 1.0:
                    self._scripted = True
                    for k in script.split(","):
                        self.act(k.strip(), snap)
                if nowt - self.t0 > float(os.environ.get("SLOP_SCREENSHOT_AFTER", "3")):
                    self._capture_next = shot
            sdl2.SDL_Delay(16)

    # ================================================================ debug captures
    def _maybe_capture(self):
        path = getattr(self, "_capture_next", None)
        if path:
            self._capture_next = None
            self.save_bmp(path)
            self.quit = True

    def save_bmp(self, path):
        panes = [self.top] if not self.dual else [self.top, self.bottom]
        for i, p in enumerate(panes):
            out = path if i == 0 else path[:-4] + "-bottom.bmp"
            surf = sdl2.SDL_CreateRGBSurfaceWithFormat(0, p.W, p.H, 32, sdl2.SDL_PIXELFORMAT_ARGB8888)
            sdl2.SDL_RenderReadPixels(p.ren, None, sdl2.SDL_PIXELFORMAT_ARGB8888,
                                      surf.contents.pixels, surf.contents.pitch)
            sdl2.SDL_SaveBMP(surf, out.encode())
            sdl2.SDL_FreeSurface(surf)

    def close(self):
        for p in self.pads:
            sdl2.SDL_GameControllerClose(p)
        self.top.destroy()
        if self.bottom is not self.top:
            self.bottom.destroy()
        ttf.TTF_Quit()
        sdl2.SDL_Quit()


def battery():
    base = "/sys/class/power_supply"
    try:
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            try:
                with open(os.path.join(p, "type")) as fh:
                    if fh.read().strip() != "Battery":
                        continue
                with open(os.path.join(p, "capacity")) as fh:
                    return max(0, min(100, int(fh.read().strip())))
            except (IOError, OSError, ValueError):
                continue
    except OSError:
        pass
    return None
