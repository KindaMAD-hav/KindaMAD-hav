#!/usr/bin/env python3
"""Builds every SVG in ../assets for the GitHub profile README.

GitHub renders README images through <img>, and an SVG loaded that way cannot
fetch web fonts or external files. So every word here is converted to vector
outlines with fontTools and every picture is embedded as a data URI. The art
looks the same on every machine and leans on no third-party service.

    pip install fonttools pillow
    python _build/build.py                  # everything
    python _build/build.py contributions    # only the contribution heatmap (the daily refresh)

Art comes from the KindaMAD Studios site: the local checkout when it exists,
kindamad.pages.dev otherwise. Fonts download once into _build/fonts.
"""

import base64
import html
import io
import itertools
import math
import os
import re
import sys
import urllib.request
from functools import lru_cache

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
FONT_DIR = os.path.join(HERE, "fonts")
ICON_DIR = os.path.join(HERE, "icons")
SITE_LOCAL = r"C:\UnityProjects\MoneyIsBalls\kindamadballs-site\public"
SITE_URL = "https://kindamad.pages.dev"

# Palette lifted from the site's globals.css ("pulp comic", dark).
BG, PANEL, INK, DIM = "#0c0908", "#120d0b", "#f4ead2", "#9a8f78"
GOLD, EMBER, RED, DRED, OUTLINE = "#ffce45", "#ff6a1f", "#e5352b", "#a81d16", "#1a1206"
TEAL = "#27c2b0"
# Radiation Maxxing keeps its own hazard identity, same as on the site.
RAD_BG, LIME, PALE, HAZARD = "#080a04", "#c6ff2e", "#dfe7c8", "#ffd400"

GF = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONT_URLS = {
    "AlfaSlabOne-Regular.ttf": f"{GF}/alfaslabone/AlfaSlabOne-Regular.ttf",
    "SpaceMono-Regular.ttf": f"{GF}/spacemono/SpaceMono-Regular.ttf",
    "SpaceMono-Bold.ttf": f"{GF}/spacemono/SpaceMono-Bold.ttf",
    "Oswald-VF.ttf": f"{GF}/oswald/Oswald%5Bwght%5D.ttf",
    "Archivo-VF.ttf": f"{GF}/archivo/Archivo%5Bwdth,wght%5D.ttf",
}


def num(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def esc(s):
    return html.escape(s, quote=True)


# ── Type ────────────────────────────────────────────────────────────────────


class Font:
    """A font that lays text out as SVG path data, with GPOS pair kerning."""

    def __init__(self, file, axes=None):
        path = os.path.join(FONT_DIR, file)
        if not os.path.exists(path):
            os.makedirs(FONT_DIR, exist_ok=True)
            urllib.request.urlretrieve(FONT_URLS[file], path)
        font = TTFont(path)
        if axes:
            font = instantiateVariableFont(font, axes)
        self.font = font
        self.upm = font["head"].unitsPerEm
        self.cmap = font.getBestCmap()
        self.hmtx = font["hmtx"]
        self.glyphs = font.getGlyphSet()
        self.cap = font["OS/2"].sCapHeight / self.upm
        self._pairs = self._kern_subtables(font)
        self._kern = {}

    @staticmethod
    def _kern_subtables(font):
        if "GPOS" not in font or not font["GPOS"].table.FeatureList:
            return []
        table = font["GPOS"].table
        indices = sorted(
            {i for rec in table.FeatureList.FeatureRecord if rec.FeatureTag == "kern" for i in rec.Feature.LookupListIndex}
        )
        subtables = []
        for i in indices:
            lookup = table.LookupList.Lookup[i]
            for sub in lookup.SubTable:
                if lookup.LookupType == 9:
                    if sub.ExtensionLookupType != 2:
                        continue
                    sub = sub.ExtSubTable
                elif lookup.LookupType != 2:
                    continue
                subtables.append(sub)
        return subtables

    def kern(self, left, right):
        key = (left, right)
        if key not in self._kern:
            self._kern[key] = self._lookup_kern(left, right)
        return self._kern[key]

    def _lookup_kern(self, left, right):
        for sub in self._pairs:
            covered = sub.Coverage.glyphs
            if left not in covered:
                continue
            if sub.Format == 1:
                for rec in sub.PairSet[covered.index(left)].PairValueRecord:
                    if rec.SecondGlyph == right:
                        return getattr(rec.Value1, "XAdvance", 0) or 0
            elif sub.Format == 2:
                c1 = sub.ClassDef1.classDefs.get(left, 0)
                c2 = sub.ClassDef2.classDefs.get(right, 0)
                return getattr(sub.Class1Record[c1].Class2Record[c2].Value1, "XAdvance", 0) or 0
        return 0

    def layout(self, text, size, tracking=0.0):
        """Glyph names with x offsets in font units, plus the total width in px."""
        placed, x, prev = [], 0.0, None
        for ch in text:
            name = self.cmap.get(ord(ch))
            if name is None:
                raise KeyError(f"font has no glyph for {ch!r} (in {text!r})")
            if prev is not None:
                x += self.kern(prev, name) + tracking * self.upm
            placed.append((name, x))
            x += self.hmtx[name][0]
            prev = name
        return placed, x * size / self.upm

    def width(self, text, size, tracking=0.0):
        return self.layout(text, size, tracking)[1]

    def path(self, text, size, x, y, anchor="start", tracking=0.0):
        placed, width = self.layout(text, size, tracking)
        x -= {"start": 0, "middle": width / 2, "end": width}[anchor]
        s = size / self.upm
        pen = SVGPathPen(self.glyphs, ntos=num)
        for name, gx in placed:
            self.glyphs[name].draw(TransformPen(pen, (s, 0, 0, -s, x + gx * s, y)))
        return pen.getCommands()

    def fit(self, text, max_width, max_size, tracking=0.0):
        return min(max_size, max_size * max_width / self.width(text, max_size, tracking))

    def wrap(self, text, size, max_width, tracking=0.0):
        lines = []
        for word in text.split():
            if lines and self.width(f"{lines[-1]} {word}", size, tracking) <= max_width:
                lines[-1] += f" {word}"
            else:
                lines.append(word)
        return lines


ALFA = Font("AlfaSlabOne-Regular.ttf")
MONO = Font("SpaceMono-Regular.ttf")
MONO_B = Font("SpaceMono-Bold.ttf")
OSWALD = Font("Oswald-VF.ttf", {"wght": 600})
OSWALD_R = Font("Oswald-VF.ttf", {"wght": 400})
ARCHIVO = Font("Archivo-VF.ttf", {"wght": 700, "wdth": 100})
ARCHIVO_M = Font("Archivo-VF.ttf", {"wght": 600, "wdth": 100})

_ids = itertools.count()


def text(font, s, size, x, y, fill, anchor="start", tracking=0.0, opacity=None):
    op = f' fill-opacity="{opacity}"' if opacity is not None else ""
    return f'<path d="{font.path(s, size, x, y, anchor, tracking)}" fill="{fill}"{op}/>'


def mast(defs, font, s, size, x, y, anchor="start", face=GOLD, mid=EMBER, low=RED):
    """The site's .pulp-mast text-shadow stack (ember, red, soft drop) as geometry."""
    ident = f"t{next(_ids)}"
    defs.append(f'<path id="{ident}" d="{font.path(s, size, x, y, anchor)}"/>')
    k = size / 80
    return (
        f'<use href="#{ident}" x="{num(6 * k)}" y="{num(8 * k)}" fill="#000" fill-opacity=".6" filter="url(#soft)"/>'
        f'<use href="#{ident}" x="{num(4 * k)}" y="{num(4 * k)}" fill="{low}"/>'
        f'<use href="#{ident}" x="{num(2 * k)}" y="{num(2 * k)}" fill="{mid}"/>'
        f'<use href="#{ident}" fill="{face}"/>'
    )


def head(defs, font, s, size, x, y, face=INK):
    """The site's quieter .pulp-head: cream type on a hard offset."""
    ident = f"t{next(_ids)}"
    defs.append(f'<path id="{ident}" d="{font.path(s, size, x, y)}"/>')
    return f'<use href="#{ident}" x="3" y="3" fill="#000" fill-opacity=".6"/><use href="#{ident}" fill="{face}"/>'


# ── Pictures and marks ──────────────────────────────────────────────────────


def site_image(rel):
    local = os.path.join(SITE_LOCAL, *rel.split("/"))
    if os.path.exists(local):
        return Image.open(local).convert("RGBA")
    with urllib.request.urlopen(f"{SITE_URL}/{rel}") as res:
        return Image.open(io.BytesIO(res.read())).convert("RGBA")


def cover(im, w, h, box=None, focus=(0.5, 0.5)):
    if box:
        im = im.crop(box)
    scale = max(w / im.width, h / im.height)
    rw, rh = math.ceil(im.width * scale), math.ceil(im.height * scale)
    im = im.resize((rw, rh), Image.LANCZOS)
    left, top = int((rw - w) * focus[0]), int((rh - h) * focus[1])
    return im.crop((left, top, left + w, top + h))


def data_uri(im, fmt="JPEG", quality=84):
    buf = io.BytesIO()
    if fmt == "JPEG":
        flat = Image.new("RGB", im.size, BG)
        flat.paste(im, mask=im.getchannel("A"))
        flat.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    else:
        im.save(buf, "WEBP", quality=quality, method=6)
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


@lru_cache(maxsize=None)
def icon_path(slug):
    with open(os.path.join(ICON_DIR, f"{slug}.svg"), encoding="utf-8") as f:
        return re.search(r'<path d="([^"]+)"', f.read()).group(1)


# 24x24 glyphs that simple-icons doesn't carry.
GLYPHS = {
    "play": ("fill", "M6 3.5 20.5 12 6 20.5Z"),
    "mail": ("stroke", "M3 5.5h18v13H3zM3.5 6l8.5 7 8.5-7"),
    "note": ("fill", "M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"),
    "check": ("stroke", "M4 12.5 9.5 18 20 6.5"),
}


def glyph(kind, x, y, size, color, stroke_width=2.2):
    if kind == "bang":
        return bang(x - size * 0.2, y - size * 0.2, size * 1.4)
    mode, d = GLYPHS.get(kind) or ("fill", icon_path(kind))
    place = f'transform="translate({num(x)} {num(y)}) scale({size / 24:.4f})"'
    if mode == "stroke":
        return (
            f'<path d="{d}" {place} fill="none" stroke="{color}" stroke-width="{stroke_width}" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    return f'<path d="{d}" {place} fill="{color}"/>'


BURST = "52,8 60,30 82,18 73,40 98,46 77,57 92,80 67,71 59,96 50,75 41,96 34,70 9,79 24,55 4,44 30,39 21,15 43,28"


def bang(x, y, size, cls="wobble"):
    """The studio mark from the site's BangLogo.astro: gold burst, red bang."""
    return (
        f'<g transform="translate({num(x)} {num(y)}) scale({size / 100:.4f})"><g class="{cls}">'
        f'<polygon points="{BURST}" transform="translate(2 2.5)" fill="{DRED}"/>'
        f'<polygon points="{BURST}" fill="{GOLD}" stroke="{OUTLINE}" stroke-width="2"/>'
        f'<g fill="{RED}" stroke="{OUTLINE}" stroke-width="1.6" stroke-linejoin="round">'
        '<path d="M45 31 55 31 52.5 58 47.5 58Z"/><circle cx="50" cy="65.5" r="4.6"/></g></g></g>'
    )


def burst_badge(cx, cy, size, fill, label):
    return (
        f'<g transform="translate({num(cx - size / 2)} {num(cy - size / 2)}) scale({size / 100:.4f})">'
        '<g transform="rotate(10 51 52)">'
        f'<polygon points="{BURST}" transform="translate(3 4)" fill="#000" fill-opacity=".6"/>'
        f'<polygon points="{BURST}" fill="{fill}" stroke="{OUTLINE}" stroke-width="2.5"/>'
        f'{text(ALFA, label, 19, 51, 53 + ALFA.cap * 19 / 2, OUTLINE, anchor="middle")}'
        "</g></g>"
    )


def rays(cx, cy, r, color=GOLD, opacity=0.05, n=28):
    """The comic spin-line halo the site puts behind hero balls, slowly turning."""
    wedges = []
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = a0 + math.pi / n
        wedges.append(
            f"M{num(cx)} {num(cy)}L{num(cx + r * math.cos(a0))} {num(cy + r * math.sin(a0))}"
            f"L{num(cx + r * math.cos(a1))} {num(cy + r * math.sin(a1))}Z"
        )
    return f'<g class="spin"><path d="{"".join(wedges)}" fill="{color}" fill-opacity="{opacity}"/></g>'


def radial(defs, ident, cx, cy, r, color, peak, cls=None):
    defs.append(
        f'<radialGradient id="{ident}" cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{color}" stop-opacity="{peak}"/>'
        f'<stop offset=".6" stop-color="{color}" stop-opacity="{peak / 4:.3f}"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></radialGradient>'
    )
    css = f' class="{cls}"' if cls else ""
    return f'<rect width="100%" height="100%" fill="url(#{ident})"{css}/>'


def trefoil(cx, cy, r):
    def at(radius, angle):
        return f"{num(cx + radius * math.cos(angle))} {num(cy + radius * math.sin(angle))}"

    inner = r * 0.24
    blades = []
    for mid in (-150, -30, 90):
        a0, a1 = math.radians(mid - 30), math.radians(mid + 30)
        blades.append(
            f"M{at(inner, a0)}L{at(r, a0)}A{num(r)} {num(r)} 0 0 1 {at(r, a1)}"
            f"L{at(inner, a1)}A{num(inner)} {num(inner)} 0 0 0 {at(inner, a0)}Z"
        )
    return "".join(blades)


def label_box(x, y, label, font, size, fg, fill, stroke, tracking=0.08, dot=None, rotate=0,
              stroke_opacity=1, fill_opacity=1, shadow=True, pad=14):
    tw = font.width(label, size, tracking)
    dw = 18 if dot else 0
    w, h = pad * 2 + dw + tw, size + 22
    parts = []
    if shadow:
        parts.append(f'<rect x="4" y="4" width="{num(w)}" height="{num(h)}" fill="#000" fill-opacity=".6"/>')
    parts.append(
        f'<rect width="{num(w)}" height="{num(h)}" fill="{fill}" fill-opacity="{fill_opacity}" '
        f'stroke="{stroke}" stroke-width="2.5" stroke-opacity="{stroke_opacity}"/>'
    )
    if dot:
        parts.append(f'<circle class="pulse" cx="{pad + 5}" cy="{num(h / 2)}" r="5" fill="{dot}"/>')
    parts.append(text(font, label, size, pad + dw, h / 2 + font.cap * size / 2, fg, tracking=tracking))
    return f'<g transform="translate({num(x)} {num(y)}) rotate({rotate})">{"".join(parts)}</g>'


# ── Documents ───────────────────────────────────────────────────────────────

# Every loop starts from its resting pose, so a renderer that only paints the
# first frame still shows the finished art.
BASE_CSS = (
    ".wobble{animation:wobble 3.2s ease-in-out infinite;transform-box:fill-box;transform-origin:center}"
    "@keyframes wobble{0%,100%{transform:rotate(0) scale(1)}30%{transform:rotate(-7deg) scale(1.05)}"
    "70%{transform:rotate(6deg) scale(1.03)}}"
    ".pulse{animation:pulse 1.6s ease-in-out infinite}"
    "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}"
    ".glow{animation:glow 5s ease-in-out infinite}"
    "@keyframes glow{0%,100%{opacity:1}50%{opacity:.65}}"
    ".spin{animation:spin 60s linear infinite;transform-box:fill-box;transform-origin:center}"
    "@keyframes spin{to{transform:rotate(360deg)}}"
    ".s24{animation-duration:24s}.s8{animation-duration:8s}.s11{animation-duration:11s}"
    ".rev{animation-direction:reverse}.slow{animation-duration:5s}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
)

COMMON_DEFS = (
    '<filter id="soft" x="-20%" y="-40%" width="140%" height="180%"><feGaussianBlur stdDeviation="4"/></filter>'
    '<pattern id="halftone" width="10" height="10" patternUnits="userSpaceOnUse">'
    '<circle cx="5" cy="5" r="1.4" fill="#fff" fill-opacity=".07"/></pattern>'
    f'<linearGradient id="bar" x1="0" x2="1"><stop offset="0" stop-color="{RED}"/>'
    f'<stop offset=".5" stop-color="{EMBER}"/><stop offset="1" stop-color="{GOLD}"/></linearGradient>'
)

HALFTONE = '<rect width="100%" height="100%" fill="url(#halftone)"/>'


def svg(w, h, label, body, defs):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="{esc(label)}"><title>{esc(label)}</title>'
        f"<style>{BASE_CSS}</style><defs>{COMMON_DEFS}{''.join(defs)}</defs>{''.join(body)}</svg>"
    )


def panel(defs, w, h, fill=PANEL, rx=18, stroke=INK, sw=4, stroke_opacity=1.0, drop=8):
    """A pulp panel: fill, ink border, hard black drop. Content goes between back and front."""
    x = sw / 2
    rect = f'x="{num(x)}" y="{num(x)}" width="{num(w - sw)}" height="{num(h - sw - drop)}" rx="{rx}"'
    defs.append(f'<clipPath id="panel"><rect {rect}/></clipPath>')
    back = f'<rect {rect} fill="{fill}"/>'
    if drop:
        back = f'<rect x="{num(x)}" y="{num(x + drop)}" width="{num(w - sw)}" height="{num(h - sw - drop)}" rx="{rx}" fill="#000"/>' + back
    front = f'<rect {rect} fill="none" stroke="{stroke}" stroke-width="{sw}" stroke-opacity="{stroke_opacity}"/>'
    return back + '<g clip-path="url(#panel)">', "</g>" + front


def write(name, markup):
    # Restart path ids per file, so an asset comes out byte-identical whether it
    # was built alone or as part of the full run (no empty daily commits).
    global _ids
    _ids = itertools.count()
    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(markup)
    print(f"  {name:26} {len(markup.encode()) / 1024:7.1f} KB")


# ── Hero ────────────────────────────────────────────────────────────────────


def hero():
    W, H = 1200, 540
    defs, body = [], []
    back, front = panel(defs, W, H, fill=BG, rx=26)
    px, py, ps = 300, 284, 330
    fw = ps + 12
    defs.append(f'<clipPath id="face"><rect x="{px - ps / 2}" y="{py - ps / 2}" width="{ps}" height="{ps}" rx="16"/></clipPath>')
    face = data_uri(cover(site_image("madhav.jpg"), 660, 660), quality=86)

    body += [
        back,
        radial(defs, "ember", px, py, 560, EMBER, 0.36, cls="glow"),
        rays(px, py, 900, GOLD, 0.035),
        HALFTONE,
        f'<rect width="{W}" height="10" fill="url(#bar)"/>',
        f'<g transform="rotate(-3 {px} {py})">'
        f'<rect x="{px - fw / 2 + 10}" y="{py - fw / 2 + 14}" width="{fw}" height="{fw}" rx="22" fill="#000" fill-opacity=".75"/>'
        f'<rect x="{px - fw / 2}" y="{py - fw / 2}" width="{fw}" height="{fw}" rx="22" fill="{INK}"/>'
        f'<image href="{face}" x="{px - ps / 2}" y="{py - ps / 2}" width="{ps}" height="{ps}" '
        'clip-path="url(#face)" preserveAspectRatio="xMidYMid slice"/></g>',
        label_box(150, 430, "FOUNDER", ARCHIVO, 18, OUTLINE, GOLD, INK, rotate=-4),
        bang(78, 56, 126),
    ]

    x0 = 548
    maxw = W - x0 - 58
    body.append(text(MONO, "FOUNDER · KINDAMAD STUDIOS", 19, x0, 128, EMBER, tracking=0.22))
    size = ALFA.fit("GANGNEJA", maxw, 122)
    y1 = 128 + 32 + ALFA.cap * size
    y2 = y1 + size * 0.94
    body.append(mast(defs, ALFA, "MADHAV", size, x0, y1))
    body.append(mast(defs, ALFA, "GANGNEJA", size, x0, y2, face=RED, mid=EMBER, low=DRED))
    t1 = y2 + 64
    for i, line in enumerate(("CREATOR. GAME DEV.", "PROFESSIONALLY KINDA MAD.")):
        body.append(f'<g transform="translate({x0} {num(t1 + i * 44)}) skewX(-8)">{text(OSWALD, line, 36, 0, 0, GOLD)}</g>')
    body.append(text(MONO, "Content creator turned game developer · India", 17, x0, t1 + 44 + 54, DIM))
    body.append(front)
    write("hero.svg", svg(W, H, "Madhav Gangneja, founder of KindaMAD Studios. Creator. Game dev. Professionally kinda mad.", body, defs))


# ── Link buttons ────────────────────────────────────────────────────────────


def button(name, label, kind, fill, fg, glyph_color, font=None, size=17, tracking=0.06):
    font = font or ARCHIVO
    h, pad, gsz, gap, drop = 46, 16, 22, 11, 5
    w = pad + gsz + gap + font.width(label, size, tracking) + pad + 2
    W, H = math.ceil(2 + w + drop + 1), 2 + h + drop + 1
    body = [
        f'<rect x="{2 + drop}" y="{2 + drop}" width="{num(w)}" height="{h}" fill="#000" fill-opacity=".55"/>',
        f'<rect x="2" y="2" width="{num(w)}" height="{h}" fill="{fill}" stroke="{INK}" stroke-width="3"/>',
        glyph(kind, 2 + pad, 2 + (h - gsz) / 2, gsz, glyph_color),
        text(font, label, size, 2 + pad + gsz + gap, 2 + h / 2 + font.cap * size / 2, fg, tracking=tracking),
    ]
    write(f"btn-{name}.svg", svg(W, H, label, body, []))


def buttons():
    button("play", "PLAY FREE ON ANDROID", "play", RED, "#fff", "#fff")
    button("site", "KINDAMAD.PAGES.DEV", "bang", GOLD, OUTLINE, OUTLINE)
    button("email", "kindamad.hav@gmail.com", "mail", PANEL, GOLD, INK, font=MONO_B, size=16, tracking=0)
    for name, label, slug, color in (
        ("youtube", "YOUTUBE", "youtube", "#ff0033"),
        ("instagram", "INSTAGRAM", "instagram", "#e1306c"),
        ("discord", "DISCORD", "discord", "#5865f2"),
        ("x", "X", "x", INK),
        ("itch", "ITCH.IO", "itchdotio", "#fa5c5c"),
        ("linkedin", "LINKEDIN", "linkedin", INK),
    ):
        button(name, label, slug, PANEL, INK, color)


# ── Stats band ──────────────────────────────────────────────────────────────


def stats():
    W, H = 1200, 248
    items = [
        ("200M+", "VIEWS IN THE LAST YEAR", "across all channels"),
        ("4", "JAM PODIUMS", "2025 · two 1st places"),
        ("40+", "WEAPONS DESIGNED", "across KindaMADballs modes"),
        ("2026", "STUDIO FOUNDED", "built in public, ever since"),
    ]
    defs, body = [], []
    back, front = panel(defs, W, H, sw=3)
    col = (W - 3) / 4
    label_size = min(ARCHIVO.fit(label, col - 32, 20, 0.08) for _, label, _ in items)
    body += [back, HALFTONE]
    for i, (n, label, sub) in enumerate(items):
        cx = 1.5 + col * (i + 0.5)
        body.append(mast(defs, ALFA, n, 72, cx, 112, anchor="middle"))
        body.append(text(ARCHIVO, label, label_size, cx, 160, INK, anchor="middle", tracking=0.08))
        body.append(text(MONO, sub, 16, cx, 196, DIM, anchor="middle"))
        if i:
            x = num(1.5 + col * i)
            body.append(
                f'<line x1="{x}" y1="44" x2="{x}" y2="206" stroke="{INK}" stroke-opacity=".14" '
                'stroke-width="2" stroke-dasharray="3 7"/>'
            )
    body.append(front)
    write("stats.svg", svg(W, H, "200M+ views in the last year. 4 jam podiums. 40+ weapons designed. Studio founded 2026.", body, defs))


# ── Section heads ───────────────────────────────────────────────────────────


def section(name, title, kicker, label):
    W, H = 1200, 138
    defs, body = [], []
    back, front = panel(defs, W, H, fill=BG, rx=18, sw=2, stroke_opacity=0.22, drop=0)
    body += [
        back,
        radial(defs, "ember", 70, 69, 420, EMBER, 0.22),
        HALFTONE,
        f'<rect y="{H - 8}" width="{W}" height="8" fill="url(#bar)"/>',
        bang(22, 22, 92),
        text(MONO, kicker, 15, 132, 52, EMBER, tracking=0.24),
        mast(defs, ALFA, title, ALFA.fit(title, W - 132 - 44, 60), 132, 112),
        front,
    ]
    write(f"head-{name}.svg", svg(W, H, label, body, defs))


# ── Project cards ───────────────────────────────────────────────────────────

CARD_W, CARD_H, ART_H = 600, 600, 312


def card_start(defs, fill):
    back, front = panel(defs, CARD_W, CARD_H, fill=fill)
    defs.append(f'<clipPath id="art"><rect width="{CARD_W}" height="{ART_H}"/></clipPath>')
    divider = f'<rect y="{ART_H}" width="{CARD_W}" height="4" fill="{INK}"/>'
    return back, front, divider


def pulp_copy(defs, kicker, title, copy, cta):
    x, maxw = 32, CARD_W - 64
    out = [
        text(MONO, kicker, 14, x, 360, EMBER, tracking=0.2),
        mast(defs, ALFA, title, ALFA.fit(title, maxw, 46), x, 414),
    ]
    lines = OSWALD_R.wrap(copy, 21, maxw)
    assert len(lines) <= 3, f"copy runs long: {lines}"
    out += [text(OSWALD_R, line, 21, x, 456 + i * 30, INK, opacity=0.82) for i, line in enumerate(lines)]
    out.append(text(ARCHIVO, cta, 16, x, 562, GOLD, tracking=0.05))
    return out


def card_balls():
    defs, body = [], []
    back, front, divider = card_start(defs, PANEL)
    # The Ball Survivors key art. Measured on the 1600x900 source: the logo runs
    # x 718-1447 and the dated "LAST WEEK" sticker starts at y 664, so the crop
    # keeps the whole logo and stops just above the sticker.
    art = cover(site_image("trailer/playtest-poster.webp"), 1192, 624, box=(212, 0, 1482, 662))
    defs.append(
        '<linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset=".5" stop-color="#000" stop-opacity="0"/>'
        '<stop offset="1" stop-color="#000" stop-opacity=".55"/></linearGradient>'
    )
    body += [
        back,
        '<g clip-path="url(#art)">',
        f'<image href="{data_uri(art, quality=82)}" width="{CARD_W}" height="{ART_H}" preserveAspectRatio="xMidYMid slice"/>',
        f'<rect width="{CARD_W}" height="{ART_H}" fill="url(#fade)"/>',
        label_box(24, 24, "FREE PLAYTEST · ANDROID", ARCHIVO, 14, "#fff", RED, INK, dot="#fff"),
        "</g>",
        divider,
    ]
    body += pulp_copy(
        defs,
        "TITLE NO. 01 · SURVIVOR ROGUELIKE",
        "KINDAMADBALLS",
        "Survive endless swarms. Stack wild ball weapons. Climb the leaderboards. Free on Android.",
        "ENTER THE ARSENAL →",
    )
    body.append(front)
    write("card-kindamadballs.svg", svg(CARD_W, CARD_H, "KindaMADballs: survivor roguelike, free playtest on Android", body, defs))


def card_radiation():
    defs, body = [], []
    back, front, divider = card_start(defs, RAD_BG)
    defs += [
        f'<pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" stroke="{LIME}" stroke-opacity=".08"/></pattern>',
        f'<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="{LIME}" fill-opacity=".05"/></pattern>',
        f'<pattern id="hazard" width="28" height="28" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="14" height="28" fill="{HAZARD}"/><rect x="14" width="14" height="28" fill="#0b0b07"/></pattern>',
        '<filter id="limeglow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="9" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
    ]
    cx, cy, r = 300, 150, 90
    body += [
        back,
        '<g clip-path="url(#art)">',
        '<rect width="100%" height="100%" fill="url(#grid)"/>',
        radial(defs, "lime", cx, cy, 300, LIME, 0.2, cls="glow"),
        f'<g filter="url(#limeglow)">'
        f'<circle cx="{cx}" cy="{cy}" r="{r + 22}" fill="none" stroke="{LIME}" stroke-width="12"/>'
        f'<g class="spin s24"><circle cx="{cx}" cy="{cy}" r="{r}" fill="none"/><path d="{trefoil(cx, cy, r)}" fill="{LIME}"/></g>'
        f'<circle cx="{cx}" cy="{cy}" r="{num(r * 0.15)}" fill="{LIME}"/></g>',
        '<rect width="100%" height="100%" fill="url(#scan)"/>',
        f'<rect y="{ART_H - 16}" width="{CARD_W}" height="16" fill="url(#hazard)"/>',
        label_box(24, 24, "IN DEVELOPMENT · STEAM", MONO_B, 12, LIME, RAD_BG, LIME, tracking=0.14, dot=LIME,
                  stroke_opacity=0.5, fill_opacity=0.7, shadow=False),
        "</g>",
        divider,
    ]
    x, maxw = 32, CARD_W - 64
    title = "RADIATION MAXXING"
    title_d = MONO_B.path(title, MONO_B.fit(title, maxw, 40), x, 404)
    copy = "An idle game where you are the radiation. Irradiate unstable bits past critical and chain the cascade."
    lines = MONO.wrap(copy, 16, maxw)
    assert len(lines) <= 3, lines
    body += [
        text(MONO, "TITLE NO. 02 · IDLE GAME", 14, x, 360, LIME, tracking=0.2, opacity=0.75),
        f'<path d="{title_d}" fill="{LIME}" fill-opacity=".45" filter="url(#soft)"/><path d="{title_d}" fill="{PALE}"/>',
        text(MONO_B, "PUSH PAST CRITICAL", 15, x, 440, LIME, tracking=0.2),
        *[text(MONO, line, 16, x, 482 + i * 26, PALE, opacity=0.75) for i, line in enumerate(lines)],
        text(MONO_B, "SEE THE PROJECT →", 15, x, 562, LIME, tracking=0.06),
        front,
    ]
    write("card-radiation.svg", svg(CARD_W, CARD_H, "Radiation Maxxing: push past critical. An idle game in development.", body, defs))


def card_rayban():
    defs, body = [], []
    back, front, divider = card_start(defs, PANEL)
    pw, ph = 270, 300
    cx, cy = 322, 26 + ph / 2
    shot = cover(site_image("case/rayban/tour-institute.webp"), pw * 2, ph * 2, box=(0, 0, 1200, 1333))
    defs.append(f'<clipPath id="screen"><rect x="{-pw / 2}" y="{-ph / 2}" width="{pw}" height="{ph}" rx="14"/></clipPath>')
    stamp_w = MONO_B.width("FIELD-TESTED", 14, 0.16) + 66
    body += [
        back,
        '<g clip-path="url(#art)">',
        radial(defs, "ember", 300, 160, 460, EMBER, 0.3),
        rays(300, 160, 700, GOLD, 0.04),
        HALFTONE,
        f'<g transform="translate({cx} {cy}) rotate(4)">'
        f'<rect x="{-pw / 2 + 4}" y="{-ph / 2 + 6}" width="{pw + 12}" height="{ph + 12}" rx="20" fill="#000" fill-opacity=".7"/>'
        f'<rect x="{-pw / 2 - 6}" y="{-ph / 2 - 6}" width="{pw + 12}" height="{ph + 12}" rx="20" fill="{INK}"/>'
        f'<image href="{data_uri(shot, quality=84)}" x="{-pw / 2}" y="{-ph / 2}" width="{pw}" height="{ph}" '
        'clip-path="url(#screen)" preserveAspectRatio="xMidYMid slice"/></g>',
        f'<g transform="translate(488 78) rotate(-12)">'
        f'<rect x="{num(-stamp_w / 2 + 4)}" y="-18" width="{num(stamp_w)}" height="44" fill="#000" fill-opacity=".6"/>'
        f'<rect x="{num(-stamp_w / 2)}" y="-22" width="{num(stamp_w)}" height="44" fill="{OUTLINE}" stroke="{RED}" stroke-width="3"/>'
        f'{text(MONO_B, "FIELD-TESTED", 14, -stamp_w / 2 + 16, MONO_B.cap * 14 / 2, RED, tracking=0.16)}'
        f'{glyph("check", stamp_w / 2 - 40, -11, 22, RED, 3)}</g>',
        label_box(24, 24, "SOLO · 2 WEEKS", ARCHIVO, 13, INK, BG, INK, stroke_opacity=0.5, fill_opacity=0.85, shadow=False),
        "</g>",
        divider,
    ]
    body += pulp_copy(
        defs,
        "CLIENT WORK · FIELD REPORT Nº 03",
        "RAY-BAN CAMPUS TOUR",
        "A hands-free audio tour on Ray-Ban Meta smart glasses. Look at a room, hear its story.",
        "READ THE FIELD REPORT →",
    )
    body.append(front)
    write("card-rayban.svg", svg(CARD_W, CARD_H, "Ray-Ban Campus Tour: a hands-free audio tour on Ray-Ban Meta smart glasses", body, defs))


def card_title03():
    defs, body = [], []
    back, front, divider = card_start(defs, PANEL)
    q = 210
    body += [
        back,
        '<g clip-path="url(#art)">',
        radial(defs, "ember", 300, 160, 420, EMBER, 0.26, cls="glow"),
        rays(300, 160, 700, GOLD, 0.05),
        HALFTONE,
        f'<g class="wobble slow">{mast(defs, ALFA, "?", q, 300, 160 + ALFA.cap * q / 2, anchor="middle")}</g>',
        label_box(24, 24, "COMING SOON", ARCHIVO, 13, DIM, BG, INK, stroke_opacity=0.5, shadow=False),
        "</g>",
        divider,
    ]
    body += pulp_copy(
        defs,
        "STILL IN THE LAB",
        "TITLE NO. 03",
        "More are in the lab. The next one gets built in public, devlog by devlog, rough edges and all.",
        "FOLLOW TO FIND OUT →",
    )
    body.append(front)
    write("card-title03.svg", svg(CARD_W, CARD_H, "Title No. 03: still in the lab. Follow to find out.", body, defs))


# ── Jam cards ───────────────────────────────────────────────────────────────

JAM_W, JAM_H, JAM_ART = 400, 530, 262


def jam_card(name, title, cover_rel, focus, place, awards, extra, label, badge_y=64):
    defs, body = [], []
    back, front = panel(defs, JAM_W, JAM_H)
    defs.append(f'<clipPath id="art"><rect width="{JAM_W}" height="{JAM_ART}"/></clipPath>')
    art = cover(site_image(cover_rel), 800, 524, focus=focus)
    body += [
        back,
        f'<g clip-path="url(#art)"><image href="{data_uri(art, quality=84)}" width="{JAM_W}" height="{JAM_ART}" preserveAspectRatio="xMidYMid slice"/></g>',
        f'<rect y="{JAM_ART}" width="{JAM_W}" height="4" fill="{INK}"/>',
        burst_badge(334, badge_y, 120, GOLD if place == "1ST" else INK, place),
    ]
    x, maxw = 28, JAM_W - 56
    upper = title.upper()
    body += [
        text(MONO, "2025 · TEAM ENTRY", 13, x, 306, EMBER, tracking=0.2),
        head(defs, ALFA, upper, ALFA.fit(upper, maxw, 34), x, 350),
    ]
    y = 390
    for award in awards:
        body.append(text(ARCHIVO, award, 17, x, y, GOLD, tracking=0.04))
        y += 28
    if extra:
        for line in MONO.wrap(extra, 13, maxw):
            body.append(text(MONO, line, 13, x, y + 2, DIM))
            y += 20
    body += [text(ARCHIVO, "PLAY ON ITCH.IO →", 15, x, JAM_H - 38, GOLD, tracking=0.05), front]
    write(f"jam-{name}.svg", svg(JAM_W, JAM_H, label, body, defs))


def jams():
    jam_card("leftbroken", "Left Broken", "jams/leftbroken.png", (0.5, 0.78), "1ST",
             ["1ST · IIIT DELHI JAM"], "also 2nd at IIT Ropar, as its earlier build 'Broken'",
             "Left Broken: 1st at IIIT Delhi Jam, also 2nd at IIT Ropar")
    jam_card("shadows", "Shadows of Bhangarh", "jams/shadows.jpg", (0.5, 0.35), "1ST",
             ["1ST · IIT JODHPUR JAM"], "Prometeo 2025",
             "Shadows of Bhangarh: 1st at IIT Jodhpur Jam, Prometeo 2025")
    # The title is painted into the top of this cover, so its badge sits low, over the planet.
    jam_card("thirdlaw", "Third Law", "jams/thirdlaw.png", (0.5, 0.5), "2ND",
             ["2ND · IIIT NAGPUR JAM"], None,
             "Third Law: 2nd at IIIT Nagpur Jam", badge_y=212)


# ── Toolbox ─────────────────────────────────────────────────────────────────


def toolbox():
    W = 1200
    # Only tools Madhav has confirmed he uses (2026-09-13). Nothing aspirational.
    columns = [
        ("ENGINES", [("unity", "Unity"), ("unrealengine", "Unreal Engine"), ("android", "Android")]),
        ("CODE", [("csharp", "C#"), ("python", "Python"), ("typescript", "TypeScript"), ("javascript", "JavaScript")]),
        ("WEB & BACKEND", [("astro", "Astro"), ("cloudflare", "Cloudflare"), ("git", "Git")]),
        ("CRAFT", [("blender", "Blender"), ("adobepremierepro", "Premiere Pro"), ("adobeaftereffects", "After Effects"),
                   ("adobephotoshop", "Photoshop"), ("ffmpeg", "FFmpeg"), ("note", "FL Studio")]),
    ]
    pad, gap = 34, 20
    colw = (W - 4 - pad * 2 - gap * 3) / 4
    rows = max(len(tools) for _, tools in columns)
    top, step, chip_h = 100, 54, 44
    rule_y = top + (rows - 1) * step + chip_h + 34
    H = rule_y + 84
    defs, body = [], []
    back, front = panel(defs, W, H, sw=3)
    body += [back, HALFTONE]
    for i, (heading, tools) in enumerate(columns):
        x = 2 + pad + i * (colw + gap)
        body.append(text(ARCHIVO, heading, 15, x, 62, EMBER, tracking=0.22))
        body.append(f'<rect x="{num(x)}" y="76" width="{num(colw)}" height="2" fill="{INK}" fill-opacity=".14"/>')
        for j, (slug, label) in enumerate(tools):
            y = top + j * step
            body += [
                f'<rect x="{num(x)}" y="{y}" width="{num(colw)}" height="{chip_h}" rx="10" fill="{BG}" '
                f'stroke="{INK}" stroke-opacity=".16" stroke-width="2"/>',
                glyph(slug, x + 14, y + (chip_h - 22) / 2, 22, INK),
                text(ARCHIVO_M, label, 18, x + 50, y + chip_h / 2 + ARCHIVO_M.cap * 18 / 2, INK),
            ]
    body.append(f'<rect x="{2 + pad}" y="{rule_y}" width="{num(W - 4 - pad * 2)}" height="2" fill="{INK}" fill-opacity=".12"/>')
    lead, rest = "SPECIALTIES  ", "custom 2D physics · ECS/DOTS · Unity editor tooling · MCP automation"
    lw, rw = MONO_B.width(lead, 15, 0.12), MONO.width(rest, 15)
    sx, ty = (W - lw - rw) / 2, rule_y + 46
    body += [text(MONO_B, lead, 15, sx, ty, GOLD, tracking=0.12), text(MONO, rest, 15, sx + lw, ty, DIM), front]
    label = "Toolbox: " + ", ".join(name for _, tools in columns for _, name in tools)
    write("toolbox.svg", svg(W, H, label, body, defs))


# ── Footer ──────────────────────────────────────────────────────────────────


def footer():
    W = 1200
    kicker = "FULL GAME BUILDS · PROTOTYPES & SLICES · MECHANICS & SYSTEMS · POLISH & GAME FEEL"
    ksize = MONO.fit(kicker, W - 180, 14, 0.2)
    size = ALFA.fit("SOMETHING", 720, 96)
    y1 = 72 + 36 + ALFA.cap * size
    y2 = y1 + size * 0.94
    sub = OSWALD_R.wrap(
        "Got a game, a prototype, or a half-finished build that needs a push? Tell us about it. "
        "Based in India, working with clients everywhere.", 22, 760)
    sy = y2 + 60
    btn_label = "START A PROJECT →"
    bw, bh = ARCHIVO.width(btn_label, 20, 0.04) + 64, 58
    by = sy + (len(sub) - 1) * 32 + 36
    mail_y = by + bh + 50
    H = math.ceil(mail_y + 44)

    defs, body = [], []
    back, front = panel(defs, W, H, fill=BG, rx=26)
    defs += [
        f'<filter id="emberglow" x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="0" stdDeviation="14" flood-color="{EMBER}" flood-opacity=".7"/></filter>',
        f'<filter id="tealglow" x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="0" stdDeviation="14" flood-color="{TEAL}" flood-opacity=".6"/></filter>',
    ]
    saw = data_uri(site_image("renders/RIPSAW.png").resize((300, 300), Image.LANCZOS), fmt="WEBP", quality=90)
    shredder = data_uri(site_image("renders/SHREDDER.png").resize((260, 260), Image.LANCZOS), fmt="WEBP", quality=90)
    mail_lead, mail = "or email  ", "kindamad.hav@gmail.com"
    ml, mw = MONO.width(mail_lead, 16), MONO_B.width(mail, 16)
    mx = (W - ml - mw) / 2
    body += [
        back,
        radial(defs, "ember", 600, y1, 700, EMBER, 0.28, cls="glow"),
        rays(600, (y1 + y2) / 2, 1100, GOLD, 0.04),
        HALFTONE,
        f'<rect width="{W}" height="10" fill="url(#bar)"/>',
        f'<g transform="translate(58 {num(H * 0.2)})"><g class="spin s8"><image href="{saw}" width="150" height="150" filter="url(#emberglow)"/></g></g>',
        f'<g transform="translate(1000 {num(H * 0.52)})"><g class="spin s11 rev"><image href="{shredder}" width="130" height="130" filter="url(#tealglow)"/></g></g>',
        text(MONO, kicker, ksize, W / 2, 72, EMBER, anchor="middle", tracking=0.2),
        mast(defs, ALFA, "LET’S BUILD", size, W / 2, y1, anchor="middle"),
        mast(defs, ALFA, "SOMETHING", size, W / 2, y2, anchor="middle", face=RED, mid=EMBER, low=DRED),
        *[text(OSWALD_R, line, 22, W / 2, sy + i * 32, INK, anchor="middle", opacity=0.85) for i, line in enumerate(sub)],
        f'<rect x="{num((W - bw) / 2 + 5)}" y="{num(by + 5)}" width="{num(bw)}" height="{bh}" fill="#000" fill-opacity=".6"/>',
        f'<rect x="{num((W - bw) / 2)}" y="{num(by)}" width="{num(bw)}" height="{bh}" fill="{RED}" stroke="{INK}" stroke-width="3"/>',
        text(ARCHIVO, btn_label, 20, W / 2, by + bh / 2 + ARCHIVO.cap * 20 / 2, "#fff", anchor="middle", tracking=0.04),
        text(MONO, mail_lead, 16, mx, mail_y, DIM),
        text(MONO_B, mail, 16, mx + ml, mail_y, GOLD),
        front,
    ]
    write("footer.svg", svg(W, H, "Let's build something. Full game builds, prototypes and slices, mechanics and systems, polish and game feel.", body, defs))


# ── Right-now board ─────────────────────────────────────────────────────────


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def tint(im, color):
    """Recolor a white weapon silhouette, keeping its alpha."""
    solid = Image.new("RGBA", im.size, rgb(color) + (255,))
    solid.putalpha(im.getchannel("A"))
    return solid


def status_board():
    """What's on the bench, with brand art as the markers instead of emoji."""
    W = 1200
    pad, gap, top, col_h = 28, 20, 84, 262
    colw = (W - 3 - pad * 2 - gap * 2) / 3
    H = top + col_h + 30 + 8
    defs, body = [], []
    back, front = panel(defs, W, H, sw=3)
    defs.append(
        '<filter id="limeglow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="5" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
    )
    fireball = data_uri(site_image("renders/FIREBALL.png").resize((200, 200), Image.LANCZOS), fmt="WEBP", quality=90)
    hammer = site_image("renders/BONKER.png").resize((170, 170), Image.LANCZOS)
    hammer_face = data_uri(tint(hammer, GOLD), fmt="WEBP", quality=90)
    hammer_drop = data_uri(tint(hammer, DRED), fmt="WEBP", quality=90)
    rows = [
        ("SHIPPING", EMBER, "KINDAMADBALLS",
         "A survivor roguelike. The free playtest is open on Android."),
        ("BUILDING", LIME, "RADIATION MAXXING",
         "An idle game where you are the radiation. In development for Steam."),
        ("TINKERING", GOLD, "THE WORKSHOP",
         "Custom 2D physics, Unity editor tooling, and MCP pipelines for Unity, Unreal, Blender and FL Studio."),
    ]
    body += [back, HALFTONE, label_box(26, 22, "RIGHT NOW", ARCHIVO, 16, OUTLINE, GOLD, INK, rotate=-2)]
    for i, (kicker, accent, title, copy) in enumerate(rows):
        x = 1.5 + pad + i * (colw + gap)
        mx, my, mr = x + 64, top + 66, 42
        body += [
            f'<rect x="{num(x)}" y="{top}" width="{num(colw)}" height="{col_h}" rx="14" fill="{BG}" '
            f'stroke="{INK}" stroke-opacity=".16" stroke-width="2"/>',
            f'<rect x="{num(x)}" y="{top + 18}" width="4" height="{col_h - 36}" fill="{accent}"/>',
        ]
        if kicker == "BUILDING":
            body.append(
                f'<circle cx="{num(mx)}" cy="{my}" r="{mr}" fill="{RAD_BG}" stroke="{LIME}" stroke-opacity=".55" stroke-width="3"/>'
                f'<g filter="url(#limeglow)"><g class="spin s24"><circle cx="{num(mx)}" cy="{my}" r="27" fill="none"/>'
                f'<path d="{trefoil(mx, my, 27)}" fill="{LIME}"/></g>'
                f'<circle cx="{num(mx)}" cy="{my}" r="4" fill="{LIME}"/></g>'
            )
        else:
            defs.append(
                f'<radialGradient id="halo{i}" cx="{num(mx)}" cy="{my}" r="{mr + 22}" gradientUnits="userSpaceOnUse">'
                f'<stop offset=".45" stop-color="{accent}" stop-opacity=".45"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient>'
            )
            body.append(
                f'<circle class="glow" cx="{num(mx)}" cy="{my}" r="{mr + 22}" fill="url(#halo{i})"/>'
                f'<circle cx="{num(mx)}" cy="{my}" r="{mr}" fill="#000" fill-opacity=".45" stroke="{accent}" stroke-opacity=".5" stroke-width="2"/>'
            )
            if kicker == "SHIPPING":
                body.append(f'<image href="{fireball}" x="{num(mx - 50)}" y="{my - 56}" width="100" height="100"/>')
            else:
                body.append(
                    f'<g class="wobble"><image href="{hammer_drop}" x="{num(mx - 37)}" y="{my - 36}" width="80" height="80"/>'
                    f'<image href="{hammer_face}" x="{num(mx - 40)}" y="{my - 40}" width="80" height="80"/></g>'
                )
        dot = ' class="pulse"' if kicker == "SHIPPING" else ""
        body += [
            f'<circle{dot} cx="{num(x + 126)}" cy="{my}" r="5" fill="{accent}"/>',
            text(MONO_B, kicker, 14, x + 140, my + MONO_B.cap * 14 / 2, accent, tracking=0.22),
        ]
        tx, ty, maxw = x + 24, top + 150, colw - 48
        if kicker == "BUILDING":
            d = MONO_B.path(title, MONO_B.fit(title, maxw, 30), tx, ty)
            body.append(f'<path d="{d}" fill="{LIME}" fill-opacity=".45" filter="url(#soft)"/><path d="{d}" fill="{PALE}"/>')
        else:
            body.append(mast(defs, ALFA, title, ALFA.fit(title, maxw, 34), tx, ty))
        lines = OSWALD_R.wrap(copy, 20, maxw)
        assert len(lines) <= 3, lines
        body += [text(OSWALD_R, line, 20, tx, top + 190 + j * 27, INK, opacity=0.82) for j, line in enumerate(lines)]
    body.append(front)
    label = "Right now. " + " ".join(f"{k.title()}: {t.title()}. {c}" for k, _, t, c in rows)
    write("now.svg", svg(W, H, label, body, defs))


# ── Building in public ──────────────────────────────────────────────────────

GITHUB_USER = "KindaMAD-hav"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# GitHub's five contribution levels, recolored from green to the pulp ember ramp.
HEAT = ["#231b17", "#5b2a14", "#a3441b", EMBER, GOLD]


def fetch_contributions(user):
    """Daily counts from GitHub's public contribution calendar.

    It is the same fragment the profile page loads, so it already includes
    private contributions whenever the profile is set to show them.
    """
    req = urllib.request.Request(f"https://github.com/users/{user}/contributions", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as res:
        page = res.read().decode("utf-8")
    total = int(re.search(r"([\d,]+)\s+contributions?\s+in the last year", page).group(1).replace(",", ""))
    tips = dict(re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]+)</tool-tip>', page))
    days = []
    for date, cid, level in re.findall(r'<td[^>]*data-date="([\d-]+)"[^>]*id="([^"]+)"[^>]*data-level="(\d)"', page):
        row, col = map(int, cid.rsplit("-", 2)[1:])
        count = re.match(r"\d+", tips.get(cid, ""))
        days.append({"date": date, "row": row, "col": col, "level": int(level), "count": int(count.group()) if count else 0})
    assert len(days) > 300, "GitHub's contribution calendar markup changed"
    return total, sorted(days, key=lambda d: d["date"])


def pretty(date):
    y, m, d = map(int, date.split("-"))
    return f"{d} {MONTHS[m - 1]} {y}"


def contributions():
    total, days = fetch_contributions(GITHUB_USER)
    active = sum(1 for d in days if d["count"])
    longest = run = 0
    for d in days:  # one cell per calendar day, no gaps
        run = run + 1 if d["count"] else 0
        longest = max(longest, run)
    best = max(days, key=lambda d: d["count"])
    latest = days[-1]["date"]

    W, H = 1200, 452
    defs, body = [], []
    back, front = panel(defs, W, H, sw=3)
    body += [back, HALFTONE]

    items = [
        (f"{total:,}", "CONTRIBUTIONS", "in the last year"),
        (str(active), "ACTIVE DAYS", f"out of {len(days)}"),
        (str(longest), "LONGEST STREAK", "days in a row"),
        (str(best["count"]), "BEST DAY", pretty(best["date"])),
    ]
    col = (W - 3) / 4
    for i, (n, label, sub) in enumerate(items):
        cx = 1.5 + col * (i + 0.5)
        body += [
            mast(defs, ALFA, n, 54, cx, 92, anchor="middle"),
            text(ARCHIVO, label, 16, cx, 128, INK, anchor="middle", tracking=0.08),
            text(MONO, sub, 14, cx, 154, DIM, anchor="middle"),
        ]
        if i:
            x = num(1.5 + col * i)
            body.append(f'<line x1="{x}" y1="40" x2="{x}" y2="160" stroke="{INK}" stroke-opacity=".14" stroke-width="2" stroke-dasharray="3 7"/>')
    body.append(f'<rect x="40" y="184" width="{W - 80}" height="2" fill="{INK}" fill-opacity=".12"/>')

    x0, y0, pitch, cell = 90, 236, 20, 16
    columns = {}
    for d in days:
        columns.setdefault(d["col"], []).append(d)
    prev, last_x = None, -1e9
    for c in sorted(columns):
        month = int(min(d["date"] for d in columns[c])[5:7])
        x = x0 + c * pitch
        if month != prev and x - last_x >= pitch * 3:
            body.append(text(MONO, MONTHS[month - 1].upper(), 13, x, y0 - 14, DIM, tracking=0.08))
            last_x = x
        prev = month
    for row, name in ((1, "MON"), (3, "WED"), (5, "FRI")):
        body.append(text(MONO, name, 12, x0 - 12, y0 + row * pitch + cell / 2 + MONO.cap * 12 / 2, DIM, anchor="end"))
    for d in days:
        ring = f' stroke="{INK}" stroke-width="2"' if d is best else ""
        body.append(
            f'<rect x="{x0 + d["col"] * pitch}" y="{y0 + d["row"] * pitch}" width="{cell}" height="{cell}" '
            f'rx="3" fill="{HEAT[d["level"]]}"{ring}/>'
        )

    ly = 414
    body.append(text(MONO, f"includes private work · data through {pretty(latest)}", 13, x0, ly, DIM))
    more_x = W - 54 - MONO.width("MORE", 12, 0.08)
    body.append(text(MONO, "MORE", 12, more_x, ly, DIM, tracking=0.08))
    swatch_x = more_x - 10 - (len(HEAT) * 18 - 4)
    for i, color in enumerate(HEAT):
        body.append(f'<rect x="{num(swatch_x + i * 18)}" y="{ly - 12}" width="14" height="14" rx="3" fill="{color}"/>')
    body.append(text(MONO, "LESS", 12, swatch_x - 10, ly, DIM, anchor="end", tracking=0.08))
    body.append(front)

    label = (f"{total:,} GitHub contributions in the last year, {active} active days, "
             f"longest streak {longest} days, best day {best['count']} on {pretty(best['date'])}")
    write("contributions.svg", svg(W, H, label, body, defs))
    print(f"  contributions: {total:,} total, {active} active days, streak {longest}, best {best['count']} ({best['date']}), through {latest}")


def main(args):
    if args == ["contributions"]:
        print("Refreshing the contribution heatmap")
        contributions()
        return
    if args:
        sys.exit(f"unknown arguments: {' '.join(args)} (expected nothing, or 'contributions')")
    print("Building profile art into assets/")
    hero()
    buttons()
    status_board()
    stats()
    section("public", "BUILDING IN PUBLIC", "@KINDAMAD-HAV ON GITHUB", "Building in public")
    contributions()
    section("making", "WHAT I’M MAKING", "NOW SHOWING", "What I'm making")
    section("jams", "BEFORE THE STUDIO, THE JAMS", "2025 · TWO 1STS · TWO 2NDS", "Before the studio, the jams")
    section("toolbox", "THE TOOLBOX", "ENGINES · CODE · CRAFT", "The toolbox")
    card_balls()
    card_radiation()
    card_rayban()
    card_title03()
    jams()
    toolbox()
    footer()


if __name__ == "__main__":
    main(sys.argv[1:])
