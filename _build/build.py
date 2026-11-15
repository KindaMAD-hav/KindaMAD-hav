#!/usr/bin/env python3
"""Builds every SVG in ../assets for the GitHub profile README.

The look is the KindaMAD Studios site's v2 theme (src/v2/styles/v2.css): saturated colour
fields with rays and corner halftone, Kanit Black Italic two-tone lettering on an ink
outline with a hard shadow, and slanted ink-bordered panels.

GitHub renders README images through <img>, and an SVG loaded that way cannot
fetch web fonts or external files. So every word here is converted to vector
outlines with fontTools and every picture is embedded as a data URI. The art
looks the same on every machine and leans on no third-party service.

    pip install fonttools pillow
    python _build/build.py                  # everything
    python _build/build.py contributions    # only the contribution heatmap (the daily refresh)

Art comes from the KindaMAD Studios site. For a full build, point KINDAMAD_SITE at a local
checkout of it; without one the script fetches kindamad.com, whose CDN may throttle it.
Hatch & Mash art lives in _build/art (the game is on RUN, not on the site).
The contact button says EMAIL ME; set KINDAMAD_EMAIL to print the address on it instead.
The contributions refresh needs none of this. Fonts download once into _build/fonts.
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
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
FONT_DIR = os.path.join(HERE, "fonts")
ICON_DIR = os.path.join(HERE, "icons")
ART_DIR = os.path.join(HERE, "art")
SITE_URL = "https://kindamad.com"
# Optional: a local checkout of the studio site, to build offline.
SITE_CHECKOUT = os.environ.get("KINDAMAD_SITE", "")
CONTACT = os.environ.get("KINDAMAD_EMAIL", "")

# Palette: the site's v2 tokens.
INK, INK2, CREAM = "#140d1f", "#22163a", "#fff3d6"
GOLD, ORANGE, RED = "#ffc93c", "#ff7a1a", "#d9442f"
TEAL, INDIGO, BLUE = "#20b2a2", "#2d1c66", "#1b6fe0"
LIVE = "#3dff7a"
# Each colour field: base, ray tint, halftone dot tint (the site's .f-* classes).
FIELDS = {
    "teal": (TEAL, "#35c7b6", "#0f8478"),
    "red": (RED, "#e3533b", "#b5341f"),
    "indigo": (INDIGO, "#3a2680", "#1b1145"),
    "blue": (BLUE, "#2f84f0", "#0f4fb0"),
    "gold": (GOLD, "#ffd864", "#e8a919"),
}
# The studio mark (BangLogo.astro) keeps its own colours everywhere.
BANG_GOLD, BANG_RED, BANG_DRED, BANG_LINE = "#ffce45", "#e5352b", "#a81d16", "#1a1206"

GF = "https://raw.githubusercontent.com/google/fonts/main/ofl/kanit"


def num(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def esc(s):
    return html.escape(s, quote=True)


# ── Type ────────────────────────────────────────────────────────────────────


class Font:
    """A font that lays text out as SVG path data, with GPOS pair kerning."""

    def __init__(self, file):
        path = os.path.join(FONT_DIR, file)
        if not os.path.exists(path):
            os.makedirs(FONT_DIR, exist_ok=True)
            urllib.request.urlretrieve(f"{GF}/{file}", path)
        font = TTFont(path)
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


BLACK = Font("Kanit-BlackItalic.ttf")      # lettering
XBOLD = Font("Kanit-ExtraBoldItalic.ttf")  # buttons, chips, captions
BOLD = Font("Kanit-Bold.ttf")              # tags
SEMI = Font("Kanit-SemiBold.ttf")
MED = Font("Kanit-Medium.ttf")             # body copy

_ids = itertools.count()


def uid(prefix="t"):
    return f"{prefix}{next(_ids)}"


def text(font, s, size, x, y, fill, anchor="start", tracking=0.0, opacity=None):
    op = f' fill-opacity="{opacity}"' if opacity is not None else ""
    return f'<path d="{font.path(s, size, x, y, anchor, tracking)}" fill="{fill}"{op}/>'


def shout(defs, font, s, size, x, y, fill=CREAM, shadow=INK, off=(2, 3), anchor="start", tracking=0.0, shadow_opacity=1.0):
    """Copy on a colour field: the site's hard 2px/3px ink text-shadow."""
    ident = uid()
    defs.append(f'<path id="{ident}" d="{font.path(s, size, x, y, anchor, tracking)}"/>')
    op = f' fill-opacity="{shadow_opacity}"' if shadow_opacity != 1 else ""
    return f'<use href="#{ident}" x="{off[0]}" y="{off[1]}" fill="{shadow}"{op}/><use href="#{ident}" fill="{fill}"/>'


def letter(defs, s, size, x, y, anchor="start", top=CREAM, bot=GOLD, line=INK, shadow=INK, weight=None, split=0.5):
    """The site's .lt lettering: a two-tone fill split across the caps, over an ink
    outline whose hard offset shadow carries the outline with it."""
    ident = uid()
    defs.append(f'<path id="{ident}" d="{BLACK.path(s, size, x, y, anchor, -0.01)}"/>')
    w = weight or 3.2 + 0.067 * size
    dx, dy = w * 0.5, w * 0.643
    if top == bot:
        fill = top
    else:
        gid = uid("g")
        cap = BLACK.cap * size
        defs.append(
            f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" x1="0" y1="{num(y - cap)}" x2="0" y2="{num(y)}">'
            f'<stop offset="{split}" stop-color="{top}"/><stop offset="{split}" stop-color="{bot}"/></linearGradient>'
        )
        fill = f"url(#{gid})"
    stroke = f'stroke-width="{num(w)}" stroke-linejoin="round"'
    return (
        f'<use href="#{ident}" x="{num(dx)}" y="{num(dy)}" fill="{shadow}" stroke="{shadow}" {stroke}/>'
        f'<use href="#{ident}" fill="{line}" stroke="{line}" {stroke}/>'
        f'<use href="#{ident}" fill="{fill}"/>'
    )


# ── Pictures and marks ──────────────────────────────────────────────────────


def site_image(rel):
    local = os.path.join(SITE_CHECKOUT, "public", *rel.split("/"))
    if SITE_CHECKOUT and os.path.exists(local):
        return Image.open(local).convert("RGBA")
    # The site's CDN turns away Python's default user agent, and it can still throttle a full
    # build's worth of requests. A local checkout (KINDAMAD_SITE) is the dependable way.
    req = urllib.request.Request(f"{SITE_URL}/{rel}", headers={"User-Agent": "Mozilla/5.0 (kindamad-profile-build)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return Image.open(io.BytesIO(res.read())).convert("RGBA")
    except OSError as err:
        sys.exit(f"could not fetch {SITE_URL}/{rel} ({err}). Set KINDAMAD_SITE to a local checkout of the site and rerun.")


def local_art(name):
    return Image.open(os.path.join(ART_DIR, name)).convert("RGBA")


def cover(im, w, h, box=None, focus=(0.5, 0.5)):
    if box:
        im = im.crop(box)
    scale = max(w / im.width, h / im.height)
    rw, rh = math.ceil(im.width * scale), math.ceil(im.height * scale)
    im = im.resize((rw, rh), Image.LANCZOS)
    left, top = int((rw - w) * focus[0]), int((rh - h) * focus[1])
    return im.crop((left, top, left + w, top + h))


def fit_width(im, w):
    return im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)


def data_uri(im, fmt="JPEG", quality=84):
    buf = io.BytesIO()
    if fmt == "JPEG":
        flat = Image.new("RGB", im.size, INK)
        flat.paste(im, mask=im.getchannel("A"))
        flat.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    else:
        im.save(buf, "WEBP", quality=quality, method=6)
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"


def rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def tint(im, color):
    """Recolor a silhouette, keeping its alpha."""
    solid = Image.new("RGBA", im.size, rgb(color) + (255,))
    solid.putalpha(im.getchannel("A"))
    return solid


@lru_cache(maxsize=None)
def icon_path(slug):
    with open(os.path.join(ICON_DIR, f"{slug}.svg"), encoding="utf-8") as f:
        return re.search(r'<path d="([^"]+)"', f.read()).group(1)


# 24x24 glyphs that simple-icons doesn't carry.
GLYPHS = {
    "play": ("fill", "M6 3.5 20.5 12 6 20.5Z"),
    "mail": ("stroke", "M3 5.5h18v13H3zM3.5 6l8.5 7 8.5-7"),
    "note": ("fill", "M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"),
}


def glyph(kind, x, y, size, color, stroke_width=2.4):
    if kind == "bang":
        return bang(x - size * 0.2, y - size * 0.2, size * 1.4, cls="")
    mode, d = GLYPHS.get(kind) or ("fill", icon_path(kind))
    place = f'transform="translate({num(x)} {num(y)}) scale({size / 24:.4f})"'
    if mode == "stroke":
        return (
            f'<path d="{d}" {place} fill="none" stroke="{color}" stroke-width="{stroke_width}" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    return f'<path d="{d}" {place} fill="{color}"/>'


BANG_BURST = "52,8 60,30 82,18 73,40 98,46 77,57 92,80 67,71 59,96 50,75 41,96 34,70 9,79 24,55 4,44 30,39 21,15 43,28"


def bang(x, y, size, cls="wob"):
    """The studio mark from the site's BangLogo.astro: gold burst, red bang."""
    c = f' class="{cls}"' if cls else ""
    return (
        f'<g transform="translate({num(x)} {num(y)}) scale({size / 100:.4f})"><g{c}>'
        f'<polygon points="{BANG_BURST}" transform="translate(2 2.5)" fill="{BANG_DRED}"/>'
        f'<polygon points="{BANG_BURST}" fill="{BANG_GOLD}" stroke="{BANG_LINE}" stroke-width="2"/>'
        f'<g fill="{BANG_RED}" stroke="{BANG_LINE}" stroke-width="1.6" stroke-linejoin="round">'
        '<path d="M45 31 55 31 52.5 58 47.5 58Z"/><circle cx="50" cy="65.5" r="4.6"/></g></g></g>'
    )


def star_points(n, r_out, r_in):
    """The site's burstpoly: a 16-point comic burst with a hand-cut wobble (deterministic)."""
    pts = []
    for i in range(n * 2):
        a = math.pi * i / n - math.pi / 2
        r = r_out * (1 + 0.06 * (((i * 7) % 5) - 2) / 2) if i % 2 == 0 else r_in
        pts.append(f"{num(r * math.cos(a))},{num(r * math.sin(a))}")
    return " ".join(pts)


def burst(cx, cy, r, fill, label=None, label_fill=INK, label_size=None, rotate=0, cls="wob", n=16):
    pts = star_points(n, r, r * 0.8)
    c = f' class="{cls}"' if cls else ""
    out = (
        f'<g transform="translate({num(cx)} {num(cy)}) rotate({rotate})"><g{c}>'
        f'<polygon points="{pts}" transform="translate({num(r * 0.07)} {num(r * 0.09)})" fill="{INK}"/>'
        f'<polygon points="{pts}" fill="{fill}" stroke="{INK}" stroke-width="{num(max(2.5, r * 0.05))}" stroke-linejoin="round"/>'
    )
    if label:
        size = label_size or r * 0.62
        out += text(BLACK, label, size, 0, BLACK.cap * size / 2, label_fill, anchor="middle")
    return out + "</g></g>"


# ── Components ──────────────────────────────────────────────────────────────

SKEW = math.tan(math.radians(8))


def slant(x, y, w, h):
    """A skewX(-8deg) slab, as the site's buttons and tags: the top leans right."""
    k = h * SKEW
    return f"{num(x + k)},{num(y)} {num(x + k + w)},{num(y)} {num(x + w)},{num(y + h)} {num(x)},{num(y + h)}"


def tag(x, y, label, size=15, bg=INK, fg=CREAM, dot=False, rotate=0, font=BOLD):
    """The site's .tag: an ink slab, uppercase Kanit Bold. Returns markup and width."""
    label = label.upper()
    tw = font.width(label, size, 0.06)
    h = round(size * 2)
    padx = size * 0.8
    dw = size * 0.95 if dot else 0
    w = padx * 2 + dw + tw
    k = h * SKEW
    parts = [f'<polygon points="{slant(0, 0, w, h)}" fill="{bg}"/>']
    if dot:
        r = size * 0.27
        parts.append(f'<circle class="blink" cx="{num(k / 2 + padx + r)}" cy="{num(h / 2)}" r="{num(r)}" fill="{LIVE}" stroke="{INK}" stroke-width="2"/>')
    parts.append(text(font, label, size, k / 2 + padx + dw, h / 2 + font.cap * size / 2, fg, tracking=0.06))
    return f'<g transform="translate({num(x)} {num(y)}) rotate({rotate})">{"".join(parts)}</g>', w + k


def slab(x, y, label, size=19, bg=RED, fg=CREAM, kind=None, glyph_color=None, h=None, sw=4, drop=(5, 6), font=XBOLD, upper=True):
    """The site's .btn: a slanted ink-outlined slab with a hard shadow. Returns markup and size."""
    label = label.upper() if upper else label
    h = h or round(size * 2.45)
    tw = font.width(label, size, 0.01)
    gsz = size * 1.05 if kind else 0
    gap = size * 0.5 if kind else 0
    pad = size * 1.15
    w = pad * 2 + gsz + gap + tw
    k = h * SKEW
    cx0 = x + k / 2 + pad
    by = y + h / 2 + font.cap * size / 2
    out = [
        f'<polygon points="{slant(x + drop[0], y + drop[1], w, h)}" fill="{INK}"/>',
        f'<polygon points="{slant(x, y, w, h)}" fill="{bg}" stroke="{INK}" stroke-width="{sw}" stroke-linejoin="round"/>',
    ]
    if kind:
        out.append(glyph(kind, cx0, y + (h - gsz) / 2, gsz, glyph_color or fg))
    out.append(text(font, label, size, cx0 + gsz + gap, by, fg, tracking=0.01))
    return "".join(out), (w + k + drop[0], h + drop[1])


def chip(x, y, label, size=15, rotate=-1.5):
    """The site's .facts chips: cream, ink border, small hard shadow, a little crooked."""
    label = label.upper()
    tw = XBOLD.width(label, size)
    w, h = tw + size * 1.6, size * 2.05
    cx, cy = x + w / 2, y + h / 2
    return (
        f'<g transform="rotate({rotate} {num(cx)} {num(cy)})">'
        f'<rect x="{num(x + 3)}" y="{num(y + 4)}" width="{num(w)}" height="{num(h)}" fill="{INK}"/>'
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" fill="{CREAM}" stroke="{INK}" stroke-width="3"/>'
        f'{text(XBOLD, label, size, x + size * 0.8, cy + XBOLD.cap * size / 2, INK)}</g>'
    ), w


def chips(x, y, labels, size=15, gap=12):
    out, rots = [], (-1.5, 1.5, -1)
    for i, label in enumerate(labels):
        markup, w = chip(x, y, label, size, rots[i % 3])
        out.append(markup)
        x += w + gap
    return "".join(out), x - gap


def frame(defs, x, y, w, h, rotate, href, sw=5, drop=(10, 12), fill=INK, inner=""):
    """A slanted art panel: ink border, hard ink shadow, picture clipped inside."""
    cid = uid("c")
    cx, cy = x + w / 2, y + h / 2
    defs.append(f'<clipPath id="{cid}"><rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}"/></clipPath>')
    img = (f'<image href="{href}" x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" '
           'preserveAspectRatio="xMidYMid slice"/>') if href else ""
    return (
        f'<g transform="rotate({rotate} {num(cx)} {num(cy)})">'
        f'<rect x="{num(x + drop[0])}" y="{num(y + drop[1])}" width="{num(w)}" height="{num(h)}" fill="{INK}"/>'
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" fill="{fill}"/>'
        f'<g clip-path="url(#{cid})">{img}{inner}</g>'
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" fill="none" stroke="{INK}" stroke-width="{sw}"/></g>'
    )


def pop(href, x, y, w, h, cls="bob", delay=None):
    """Art that breaks out of its frame, with the site's hard drop shadow."""
    style = f' style="animation-delay:{delay}"' if delay else ""
    return (
        f'<g class="{cls}"{style}><image href="{href}" x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" '
        'filter="url(#popdrop)"/></g>'
    )


def ray_wedges(cx, cy, r, step=15, width=6):
    wedges = []
    for i in range(int(360 / step)):
        a0 = math.radians(i * step - 90)
        a1 = a0 + math.radians(width)
        wedges.append(
            f"M{num(cx)} {num(cy)}L{num(cx + r * math.cos(a0))} {num(cy + r * math.sin(a0))}"
            f"L{num(cx + r * math.cos(a1))} {num(cy + r * math.sin(a1))}Z"
        )
    return "".join(wedges)


def rays_and_dots(defs, w, h, kind, at, dot):
    """The .field backdrop: conic rays from one point, faded out radially, and halftone
    dots in a deeper tint masked into the top-left and bottom-right corners."""
    _, ray, dotc = FIELDS[kind]
    cx, cy = w * at[0], h * at[1]
    hh = max(h, 0.45 * w)
    reach = 0.62 * math.hypot(0.9 * w, 1.08 * hh)
    defs += [
        f'<radialGradient id="rayfade" cx="{num(cx)}" cy="{num(cy)}" r="{num(reach)}" gradientUnits="userSpaceOnUse">'
        '<stop offset=".48" stop-color="#fff"/><stop offset="1" stop-color="#000"/></radialGradient>',
        '<mask id="raym"><rect width="100%" height="100%" fill="url(#rayfade)"/></mask>',
        f'<pattern id="dots" width="{dot}" height="{dot}" patternUnits="userSpaceOnUse">'
        f'<circle cx="{num(dot / 2)}" cy="{num(dot / 2)}" r="{num(dot * 0.2)}" fill="{dotc}"/></pattern>',
        '<linearGradient id="dotfade" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff"/>'
        '<stop offset=".26" stop-color="#000"/><stop offset=".74" stop-color="#000"/><stop offset="1" stop-color="#fff"/></linearGradient>',
        '<mask id="dotm"><rect width="100%" height="100%" fill="url(#dotfade)"/></mask>',
    ]
    return (
        f'<path d="{ray_wedges(cx, cy, 2.2 * math.hypot(w, h))}" fill="{ray}" mask="url(#raym)"/>'
        '<rect width="100%" height="100%" fill="url(#dots)" mask="url(#dotm)"/>'
    )


def field(defs, w, h, kind, cut=(0, 14, 0, 14), at=(0.5, 0.4), dot=16, drop=8, sw=5):
    """One comic panel per image: a colour field with slanted, hand-cut edges, an ink
    border and a hard ink drop. Content goes between back and front."""
    base = FIELDS[kind][0]
    tl, tr, br, bl = cut
    m = sw / 2
    x0, x1 = m, w - m - drop * 0.8
    y0, y1 = m, h - m - drop
    pts = [(x0, y0 + tl), (x1, y0 + tr), (x1, y1 - br), (x0, y1 - bl)]
    poly = " ".join(f"{num(x)},{num(y)}" for x, y in pts)
    shadow = " ".join(f"{num(x + drop * 0.8)},{num(y + drop)}" for x, y in pts)
    defs.append(f'<clipPath id="fld"><polygon points="{poly}"/></clipPath>')
    back = (
        f'<polygon points="{shadow}" fill="{INK}"/><polygon points="{poly}" fill="{base}"/>'
        f'<g clip-path="url(#fld)">{rays_and_dots(defs, w, h, kind, at, dot)}'
    )
    front = f'</g><polygon points="{poly}" fill="none" stroke="{INK}" stroke-width="{sw}" stroke-linejoin="round"/>'
    return back, front


# ── Documents ───────────────────────────────────────────────────────────────

# Every loop starts from its resting pose, so a renderer that only paints the
# first frame still shows the finished art.
BASE_CSS = (
    ".wob,.bob,.roll,.spin{transform-box:fill-box;transform-origin:center}"
    ".wob{animation:wob 3.4s ease-in-out infinite}"
    "@keyframes wob{0%,100%{transform:none}30%{transform:rotate(-6deg) scale(.97)}70%{transform:rotate(5deg) scale(1.03)}}"
    ".bob{animation:bob 3.2s ease-in-out infinite}"
    "@keyframes bob{0%,100%{transform:none}50%{transform:translate(0,-10px) rotate(-4deg)}}"
    ".roll{animation:roll 2.6s ease-in-out infinite}"
    "@keyframes roll{0%,100%{transform:none}33%{transform:rotate(-12deg)}66%{transform:translate(0,-8px) rotate(10deg)}}"
    ".spin{animation:spin 14s linear infinite}"
    "@keyframes spin{to{transform:rotate(360deg)}}"
    ".blink{animation:blink 1.1s steps(2) infinite}"
    "@keyframes blink{50%{opacity:.25}}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
)

COMMON_DEFS = (
    f'<filter id="popdrop" x="-20%" y="-20%" width="150%" height="150%">'
    f'<feDropShadow dx="5" dy="7" stdDeviation="0" flood-color="{INK}" flood-opacity=".55"/></filter>'
)


def svg(w, h, label, body, defs, css=""):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="{esc(label)}"><title>{esc(label)}</title>'
        f"<style>{BASE_CSS}{css}</style><defs>{COMMON_DEFS}{''.join(defs)}</defs>{''.join(body)}</svg>"
    )


def fresh():
    # Restart ids per file, so an asset comes out byte-identical whether it
    # was built alone or as part of the full run (no empty daily commits).
    global _ids
    _ids = itertools.count()
    return [], []


def write(name, markup):
    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(markup)
    print(f"  {name:26} {len(markup.encode()) / 1024:7.1f} KB")


def section_title(defs, title, note, x, y, size, maxw, w, note_fill=CREAM, note_shadow=True, **style):
    """The site's .shead: big lettering on the left, a short note bottom-right."""
    size = BLACK.fit(title, maxw, size)
    out = [letter(defs, title, size, x, y, **style)]
    if note:
        nw = 330
        lines = MED.wrap(note, 20, nw)
        for i, line in enumerate(lines):
            ly = y - (len(lines) - 1 - i) * 28
            nx = w - 60 - nw
            out.append(shout(defs, MED, line, 20, nx, ly, note_fill, off=(1, 2), shadow_opacity=0.45) if note_shadow
                       else text(MED, line, 20, nx, ly, note_fill))
    return "".join(out)


# ── Hero ────────────────────────────────────────────────────────────────────


def hero():
    W, H = 1200, 590
    defs, body = fresh()
    back, front = field(defs, W, H, "teal", cut=(0, 0, 20, 0), at=(0.25, 0.5), dot=18)
    px, py, ps = 288, 292, 340
    face = data_uri(cover(site_image("madhav.jpg"), 680, 680), quality=86)
    founder, fw = tag(0, 0, "Founder, KindaMAD Studios", 19, GOLD, INK)
    body += [
        back,
        frame(defs, px - ps / 2, py - ps / 2, ps, ps, -3, face, sw=6, drop=(13, 15), fill="#1d1210"),
        f'<g transform="translate({num(px + ps / 2 - fw + 26)} {num(py + ps / 2 - 14)}) rotate(-2)">{founder}</g>',
        bang(46, 34, 140),
    ]
    x0 = 552
    size = BLACK.fit("GANGNEJA", W - x0 - 90, 136)
    y1 = 96 + BLACK.cap * size
    y2 = y1 + size * 0.88
    body += [
        letter(defs, "MADHAV", size, x0, y1),
        letter(defs, "GANGNEJA", size, x0 + 30, y2, top=ORANGE, bot=RED),
    ]
    t = y2 + 78
    body += [
        letter(defs, "CREATOR. GAME DEV.", 40, x0 + 36, t, top=CREAM, bot=CREAM),
        letter(defs, "PROFESSIONALLY KINDA MAD.", 40, x0 + 36, t + 48, top=GOLD, bot=GOLD),
        text(MED, "Content creator turned game developer, from India.", 22, x0 + 40, t + 104, INK),
        front,
    ]
    write("hero.svg", svg(W, H, "Madhav Gangneja, founder of KindaMAD Studios. Creator. Game dev. Professionally kinda mad.", body, defs))


# ── Link buttons ────────────────────────────────────────────────────────────


def button(name, label, kind, bg, fg, glyph_color, size=20, upper=True):
    defs, body = fresh()
    markup, (w, h) = slab(3, 3, label, size, bg, fg, kind, glyph_color, upper=upper)
    write(f"btn-{name}.svg", svg(math.ceil(w + 6), math.ceil(h + 6), label, [markup], defs))


def buttons():
    button("play", "Play free on Android", "play", RED, CREAM, CREAM)
    button("site", "kindamad.com", "bang", GOLD, INK, INK)
    if CONTACT:
        button("email", CONTACT, "mail", CREAM, INK, INK, upper=False)
    else:
        button("email", "Email me", "mail", CREAM, INK, INK)
    for name, label, slug, color in (
        ("youtube", "YouTube", "youtube", "#ff0033"),
        ("instagram", "Instagram", "instagram", "#e1306c"),
        ("discord", "Discord", "discord", "#5865f2"),
        ("x", "X", "x", INK),
        ("itch", "itch.io", "itchdotio", "#fa5c5c"),
        ("linkedin", "LinkedIn", "linkedin", "#0a66c2"),
    ):
        button(name, label, slug, CREAM, INK, color, size=18)


# ── Right now ───────────────────────────────────────────────────────────────


def now():
    """What's on the bench: three cream cards on a red field, art as the markers."""
    W = 1200
    pad, gap, top, card_h = 50, 30, 150, 262
    colw = (W - 8 - pad * 2 - gap * 2) / 3
    H = top + card_h + 58
    defs, body = fresh()
    back, front = field(defs, W, H, "red", cut=(16, 0, 0, 12), at=(0.15, 0.2))
    icon = data_uri(site_image("v2/game-icon.webp").resize((176, 176), Image.LANCZOS), fmt="WEBP", quality=90)
    die = data_uri(site_image("ludo/el/dice-5.webp").resize((152, 160), Image.LANCZOS), fmt="WEBP", quality=90)
    hammer = site_image("renders/BONKER.png").resize((176, 176), Image.LANCZOS)
    hammer_ink = data_uri(tint(hammer, INK), fmt="WEBP", quality=90)
    hammer_red = data_uri(tint(hammer, RED), fmt="WEBP", quality=90)
    rows = [
        ("Shipping", RED, CREAM, True, "BALL SURVIVORS",
         "A survivor roguelike, live on Google Play and still getting new weapons every update."),
        ("Live", BLUE, CREAM, True, "PLAY LUDO",
         "Ludo where every capture is a 10-second duel. Out now on Google Play."),
        ("Tinkering", INK, GOLD, False, "THE WORKSHOP",
         "Custom 2D physics, Unity editor tools, and MCP pipelines for Unity, Unreal, Blender and FL Studio."),
    ]
    body += [back, section_title(defs, "RIGHT NOW", "What's on the bench, and what's already out.", pad, 112, 78, 600, W)]
    for i, (status, bg, fg, dot, title, copy) in enumerate(rows):
        x = pad + i * (colw + gap)
        y = top
        rot = (-1.4, 1, -0.8)[i]
        cx, cy = x + colw / 2, y + card_h / 2
        mx, my, ms = x + 22, y + 20, 84
        if i == 0:
            marker = f'<image href="{icon}" x="{num(mx)}" y="{num(my)}" width="{ms}" height="{ms}" filter="url(#popdrop)"/>'
        elif i == 1:
            marker = f'<g class="roll"><image href="{die}" x="{num(mx + 4)}" y="{num(my)}" width="76" height="80" filter="url(#popdrop)"/></g>'
        else:
            marker = (f'<g class="wob"><image href="{hammer_red}" x="{num(mx + 4)}" y="{num(my + 5)}" width="{ms}" height="{ms}"/>'
                      f'<image href="{hammer_ink}" x="{num(mx)}" y="{num(my)}" width="{ms}" height="{ms}"/></g>')
        status_tag, _ = tag(x + 122, y + 44, status, 19, bg, fg, dot=dot)
        tw = colw - 48
        lines = MED.wrap(copy, 19, tw)
        assert len(lines) <= 3, lines
        body += [
            f'<g transform="rotate({rot} {num(cx)} {num(cy)})">'
            f'<rect x="{num(x + 8)}" y="{num(y + 10)}" width="{num(colw)}" height="{card_h}" fill="{INK}"/>'
            f'<rect x="{num(x)}" y="{y}" width="{num(colw)}" height="{card_h}" fill="{CREAM}" stroke="{INK}" stroke-width="4"/>',
            marker,
            status_tag,
            text(BLACK, title, BLACK.fit(title, tw, 36), x + 24, y + 148, INK),
            *[text(MED, line, 19, x + 24, y + 182 + j * 25, INK, opacity=0.86) for j, line in enumerate(lines)],
            "</g>",
        ]
    body.append(front)
    label = "Right now. " + " ".join(f"{s}: {t.title()}. {c}" for s, _, _, _, t, c in rows)
    write("now.svg", svg(W, H, label, body, defs))


# ── Stats band ──────────────────────────────────────────────────────────────


def stats():
    """The site's proof band: three big numbers on blue."""
    W, H = 1200, 288
    defs, body = fresh()
    back, front = field(defs, W, H, "blue", cut=(0, 14, 12, 0), at=(0.5, 0.55))
    items = [("200M+", "VIEWS IN THE LAST YEAR"), ("4", "GAME JAM PODIUMS"), ("40+", "BALL WEAPONS DESIGNED")]
    inset = 50
    col = (W - 8 - inset * 2) / 3
    body.append(back)
    for i, (n, cap) in enumerate(items):
        cx = inset + col * (i + 0.5)
        body.append(letter(defs, n, 116, cx, 64 + BLACK.cap * 116, anchor="middle"))
        body.append(shout(defs, XBOLD, cap, 23, cx, 222, anchor="middle"))
    body.append(front)
    write("stats.svg", svg(W, H, "200M+ views in the last year. 4 game jam podiums. 40+ ball weapons designed.", body, defs))


# ── Section heads ───────────────────────────────────────────────────────────


def head(name, kind, title, note, label, cut, at):
    W, H = 1200, 214
    defs, body = fresh()
    back, front = field(defs, W, H, kind, cut=cut, at=at)
    body += [back, section_title(defs, title, note, 50, 150, 104, 700, W), front]
    write(f"head-{name}.svg", svg(W, H, label, body, defs))


# ── Project cards ───────────────────────────────────────────────────────────

CARD_W, CARD_H = 600, 712
ART = (42, 40, 500, 272)  # x, y, w, h of the art panel


def card(name, kind, cut, at, art, tags, title, blurb, facts, cta, label, rot=-1.5):
    """One title per card, laid out like the site's lineup rows: slanted art panel with
    pop-outs, tags, lettering, blurb, fact chips, a slab CTA."""
    defs, body = fresh()
    back, front = field(defs, CARD_W, CARD_H, kind, cut=cut, at=at, dot=14)
    x, maxw = 46, CARD_W - 96
    body += [back, art(defs, rot)]
    tx = x
    for t in tags:
        markup, tw = t(tx, 348)
        body.append(markup)
        tx += tw + 10
    assert tx - 10 - x <= maxw, f"tags run long: {name}"
    size = BLACK.fit(title, maxw, 56)
    body.append(letter(defs, title, size, x, 446))
    lines = MED.wrap(blurb, 19, maxw - 10)
    assert len(lines) <= 3, f"copy runs long: {lines}"
    body += [shout(defs, MED, line, 19, x, 486 + i * 26, off=(1, 2), shadow_opacity=0.45) for i, line in enumerate(lines)]
    markup, end = chips(x, 566, facts, 19)
    assert end - x <= maxw, f"facts run long: {name}"
    body.append(markup)
    label_cta, bg, fg = cta
    markup, _ = slab(x, 630, label_cta, 19, bg, fg)
    body += [markup, front]
    write(f"card-{name}.svg", svg(CARD_W, CARD_H, label, body, defs))


def art_survivors(defs, rot):
    x, y, w, h = ART
    plate = data_uri(cover(site_image("v2/plates/04_swarm-lightning.webp"), w * 2, h * 2), quality=82)
    minigun = fit_width(site_image("v2/heroes/minigun.webp"), 460)
    swarm = fit_width(site_image("v2/heroes/swarm.webp"), 300)
    return (
        frame(defs, x, y, w, h, rot, plate)
        + pop(data_uri(minigun, fmt="WEBP", quality=88), 10, 204, 230, 230 * minigun.height / minigun.width)
        + pop(data_uri(swarm, fmt="WEBP", quality=88), 432, 10, 150, 150 * swarm.height / swarm.width, delay="-1.2s")
    )


def art_ludo(defs, rot):
    x, y, w, h = ART
    board = data_uri(cover(site_image("ludo/key/hero-clean-cross-1280.webp"), w * 2, h * 2), quality=84)
    die = site_image("ludo/el/dice-5.webp").resize((192, 202), Image.LANCZOS)
    pawn = site_image("ludo/el/pawn-token-red.webp").resize((160, 158), Image.LANCZOS)
    return (
        frame(defs, x, y, w, h, rot, board)
        + f'<g class="roll"><image href="{data_uri(die, fmt="WEBP", quality=90)}" x="476" y="10" width="96" height="101" filter="url(#popdrop)"/></g>'
        + pop(data_uri(pawn, fmt="WEBP", quality=90), 18, 258, 80, 79, delay="-.8s")
    )


def art_hatch(defs, rot):
    x, y, w, h = ART
    play = data_uri(cover(local_art("hatch-and-mash-play.jpg"), w * 2, h * 2, focus=(0.5, 0.5)), quality=84)
    thumb = data_uri(local_art("hatch-and-mash-thumb.jpg").resize((420, 420), Image.LANCZOS), quality=88)
    ts = 196
    tx, ty = CARD_W - ts - 40, 12
    return frame(defs, x, y, w, h, rot, play) + (
        f'<g class="wob" style="animation-duration:4.2s">{frame(defs, tx, ty, ts, ts, 6, thumb, sw=5, drop=(8, 10))}</g>'
    )


def art_rayban(defs, rot):
    x, y, w, h = ART
    a = data_uri(cover(site_image("case/rayban/tour-blueprint.webp"), 300, 400, box=(0, 0, 1200, 1600)), quality=84)
    b = data_uri(cover(site_image("case/rayban/tour-institute.webp"), 300, 400, box=(0, 0, 1200, 1600)), quality=84)
    sw_, sh_ = 150, 200
    inner = (
        f'<path d="{ray_wedges(x + w / 2, y + h * 0.55, w * 1.2)}" fill="#3a2680"/>'
        + frame(defs, x + w / 2 - sw_ - 6, y + 40, sw_, sh_, -5, a, sw=4, drop=(7, 9))
        + frame(defs, x + w / 2 + 12, y + 30, sw_, sh_, 4, b, sw=4, drop=(7, 9))
    )
    return frame(defs, x, y, w, h, rot, None, fill=INDIGO, inner=inner)


def cards():
    def icon_tag(x, y):
        icon = data_uri(site_image("v2/game-icon.webp").resize((96, 96), Image.LANCZOS), fmt="WEBP", quality=90)
        return f'<image href="{icon}" x="{x}" y="{y - 6}" width="44" height="44"/>', 44

    card("survivors", "indigo", (0, 12, 0, 6), (0.85, 0.1),
         art_survivors,
         [icon_tag, lambda x, y: tag(x, y, "Live on Google Play", 19, RED, CREAM, dot=True)],
         "BALL SURVIVORS",
         "Pick a ball, outlast the crowd, and every few seconds pick something that makes it worse for them.",
         ["2 arenas", "a boss every 5 min", "free"],
         ("See the game →", GOLD, INK),
         "Ball Survivors: a survivor roguelike, live on Google Play")
    card("ludo", "blue", (10, 0, 8, 0), (0.7, 0.5),
         art_ludo,
         [lambda x, y: tag(x, y, "Live on Google Play", 19, RED, CREAM, dot=True)],
         "PLAY LUDO",
         "Classic Ludo, except every capture is a 10-second duel. Eight mini-games, two boards, online or on one phone.",
         ["8 duels", "2 boards", "by MMA Studios"],
         ("See the game →", GOLD, INK),
         "Play Ludo: Ludo where every capture is a 10-second duel, live on Google Play", rot=1.5)
    card("hatchmash", "red", (0, 8, 0, 14), (0.3, 0.3),
         art_hatch,
         [lambda x, y: tag(x, y, "RUN October Jam 2026", 19, INK, CREAM),
          lambda x, y: tag(x, y, "Plays in browser", 19, GOLD, INK)],
         "HATCH & MASH",
         "Hatch a monster, slam a town, and watch it all fall down. Drag to stomp, let go to SLAM, and towers tip into towers.",
         ["180 monsters", "let go to slam", "free"],
         ("Play it free →", GOLD, INK),
         "Hatch & Mash: hatch a monster, slam a town. A RUN October Jam 2026 game, free in your browser")
    card("rayban", "teal", (12, 0, 10, 0), (0.6, 0.35),
         art_rayban,
         [lambda x, y: tag(x, y, "Client build · Shipped", 19, RED, CREAM)],
         "RAY-BAN CAMPUS TOUR",
         "A hands-free audio tour on Ray-Ban Meta smart glasses. Look at a room, hear its story. Beta SDK, no display, two weeks, delivered.",
         ["solo build", "2 weeks", "beta SDK"],
         ("Read the field report →", CREAM, INK),
         "Ray-Ban Campus Tour: a hands-free audio tour on Ray-Ban Meta smart glasses", rot=1.5)


# ── Jam cards ───────────────────────────────────────────────────────────────

JAM_W, JAM_H = 400, 560


def jam_card(name, title, cover_rel, focus, place, award, extra, label, rot, cut, badge=(322, 58)):
    defs, body = fresh()
    back, front = field(defs, JAM_W, JAM_H, "blue", cut=cut, at=(0.5, 0.3), dot=13)
    x, y, w, h = 36, 34, 316, 214
    art = data_uri(cover(site_image(cover_rel), w * 2, h * 2, focus=focus), quality=84)
    first = place == "1ST"
    body += [
        back,
        frame(defs, x, y, w, h, rot, art, sw=5, drop=(8, 10)),
        burst(badge[0], badge[1], 50, GOLD if first else CREAM, place, RED if first else BLUE, 28, rotate=8),
    ]
    maxw = JAM_W - 80
    upper = title.upper()
    size = BLACK.fit(upper, maxw, 40)
    body.append(letter(defs, upper, size, 40, 330))
    body.append(shout(defs, XBOLD, award, 19, 42, 372, GOLD))
    yy = 404
    if extra:
        lines = MED.wrap(extra, 19, maxw)
        assert len(lines) <= 2, lines
        for line in lines:
            body.append(shout(defs, MED, line, 19, 42, yy, off=(1, 2), shadow_opacity=0.45))
            yy += 27
    markup, _ = slab(40, JAM_H - 104, "Play on itch.io →", 19, GOLD, INK)
    body += [markup, front]
    write(f"jam-{name}.svg", svg(JAM_W, JAM_H, label, body, defs))


def jams():
    jam_card("leftbroken", "Left Broken", "jams/leftbroken.png", (0.5, 0.78), "1ST",
             "1ST · IIIT DELHI JAM", "Also 2nd at IIT Ropar, as its earlier build 'Broken'.",
             "Left Broken: 1st at IIIT Delhi Jam, also 2nd at IIT Ropar", -2, (0, 10, 0, 6))
    jam_card("shadows", "Shadows of Bhangarh", "jams/shadows.jpg", (0.5, 0.35), "1ST",
             "1ST · IIT JODHPUR JAM", "Prometeo 2025.",
             "Shadows of Bhangarh: 1st at IIT Jodhpur Jam, Prometeo 2025", 1.5, (8, 0, 10, 0))
    # The title is painted into the top of this cover, so its badge sits low, over the planet.
    jam_card("thirdlaw", "Third Law", "jams/thirdlaw.png", (0.5, 0.5), "2ND",
             "2ND · IIIT NAGPUR JAM", None,
             "Third Law: 2nd at IIIT Nagpur Jam", -1, (0, 6, 0, 12), badge=(318, 214))


# ── Building in public ──────────────────────────────────────────────────────

GITHUB_USER = "KindaMAD-hav"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# GitHub's five contribution levels, in the greens the site's About page uses on cream.
HEAT = ["#d8cfb4", "#6fb479", "#4f9b5e", "#3a8049", "#29612f"]
LABEL = "#54452b"


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
    """The About page's 'Building in public': a taped cream clipping of the last year on gold."""
    total, days = fetch_contributions(GITHUB_USER)
    active = sum(1 for d in days if d["count"])
    longest = run = 0
    for d in days:  # one cell per calendar day, no gaps
        run = run + 1 if d["count"] else 0
        longest = max(longest, run)
    best = max(days, key=lambda d: d["count"])
    latest = days[-1]["date"]

    W, H = 1200, 712
    defs, body = fresh()
    back, front = field(defs, W, H, "gold", cut=(0, 16, 0, 10), at=(0.15, 0.1))
    body += [
        back,
        section_title(defs, "BUILDING IN PUBLIC", "Most of the work ships in the open. This is the last year of commits, straight off GitHub.",
                      50, 136, 100, 700, W, note_fill=INK, note_shadow=False, top=INK, bot=INK2, line=GOLD, shadow=RED),
    ]

    # GitHub shows this 1200-wide image at about 750px, so a size here comes out
    # at ~0.62 of itself: nothing below 19 (about 12px on screen).
    cx0, cy0, cw, ch = 50, 196, W - 120, 448
    clip = [
        f'<rect x="{cx0 + 10}" y="{cy0 + 12}" width="{cw}" height="{ch}" fill="{INK}"/>',
        f'<rect x="{cx0}" y="{cy0}" width="{cw}" height="{ch}" fill="{CREAM}" stroke="{INK}" stroke-width="4"/>',
        text(XBOLD, "@KINDAMAD-HAV", 22, cx0 + 30, cy0 + 46, INK),
        text(XBOLD, "LAST 12 MONTHS", 20, cx0 + cw - 30, cy0 + 46, INK, anchor="end"),
    ]
    items = [
        (f"{total:,}", "CONTRIBUTIONS", "in the last year"),
        (str(active), "ACTIVE DAYS", f"out of {len(days)}"),
        (str(longest), "LONGEST STREAK", "days in a row"),
        (str(best["count"]), "BEST DAY", pretty(best["date"])),
    ]
    col = cw / 4
    for i, (n, label, sub) in enumerate(items):
        cx = cx0 + col * (i + 0.5)
        clip += [
            shout(defs, BLACK, n, 62, cx, cy0 + 122, INK, RED, off=(4, 5), anchor="middle"),
            text(BOLD, label, 21, cx, cy0 + 156, INK, anchor="middle", tracking=0.05),
            text(SEMI, sub, 20, cx, cy0 + 184, LABEL, anchor="middle"),
        ]
    clip.append(f'<rect x="{cx0 + 30}" y="{cy0 + 204}" width="{cw - 60}" height="3" fill="{INK}" fill-opacity=".12"/>')

    pitch, cell, small = 18, 14, 20
    days_w = max(SEMI.width(n, small) for n in ("Mon", "Wed", "Fri")) + 12
    ncols = max(d["col"] for d in days) + 1
    x0 = cx0 + (cw - (days_w + ncols * pitch - 4)) / 2 + days_w
    y0 = cy0 + 250
    columns = {}
    for d in days:
        columns.setdefault(d["col"], []).append(d)
    prev, last_x = None, -1e9
    for c in sorted(columns):
        month = int(min(d["date"] for d in columns[c])[5:7])
        x = x0 + c * pitch
        if month != prev and x - last_x >= pitch * 3:
            clip.append(text(SEMI, MONTHS[month - 1], small, x, y0 - 12, LABEL))
            last_x = x
        prev = month
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        clip.append(text(SEMI, name, small, x0 - 12, y0 + row * pitch + cell / 2 + SEMI.cap * small / 2, LABEL, anchor="end"))
    for d in days:
        ring = f' stroke="{INK}" stroke-width="2.5"' if d is best else ""
        clip.append(
            f'<rect x="{num(x0 + d["col"] * pitch)}" y="{num(y0 + d["row"] * pitch)}" width="{cell}" height="{cell}" '
            f'rx="2" fill="{HEAT[d["level"]]}"{ring}/>'
        )
    ly = y0 + 7 * pitch + 38
    clip.append(text(SEMI, f"Includes private work · data through {pretty(latest)}", small, x0, ly, LABEL))
    more_x = x0 + ncols * pitch - 4 - SEMI.width("More", small)
    clip.append(text(SEMI, "More", small, more_x, ly, LABEL))
    swatch_x = more_x - 10 - (len(HEAT) * 20 - 4)
    for i, color in enumerate(HEAT):
        clip.append(f'<rect x="{num(swatch_x + i * 20)}" y="{num(ly - 15)}" width="16" height="16" rx="2" fill="{color}"/>')
    clip.append(text(SEMI, "Less", small, swatch_x - 10, ly, LABEL, anchor="end"))
    # The strip of tape holding the clipping down.
    tape = (f'<g transform="rotate(-4 {num(cx0 + cw * 0.46 + 64)} {cy0})">'
            f'<rect x="{num(cx0 + cw * 0.46)}" y="{cy0 - 18}" width="128" height="34" fill="{CREAM}" fill-opacity=".82" '
            f'stroke="{INK}" stroke-opacity=".2" stroke-width="2"/></g>')
    body += [f'<g transform="rotate(-.6 {num(cx0 + cw / 2)} {num(cy0 + ch / 2)})">{"".join(clip)}{tape}</g>', front]

    label = (f"Building in public: {total:,} GitHub contributions in the last year, {active} active days, "
             f"longest streak {longest} days, best day {best['count']} on {pretty(best['date'])}")
    write("contributions.svg", svg(W, H, label, body, defs))
    print(f"  contributions: {total:,} total, {active} active days, streak {longest}, best {best['count']} ({best['date']}), through {latest}")


# ── Toolbox ─────────────────────────────────────────────────────────────────


def toolbox():
    W = 1200
    # Only tools Madhav has confirmed he uses (2026-09-13). Nothing aspirational.
    columns = [
        ("Engines", [("unity", "Unity"), ("unrealengine", "Unreal Engine"), ("android", "Android")]),
        ("Code", [("csharp", "C#"), ("python", "Python"), ("typescript", "TypeScript"), ("javascript", "JavaScript")]),
        ("Web & backend", [("astro", "Astro"), ("cloudflare", "Cloudflare"), ("git", "Git")]),
        ("Craft", [("blender", "Blender"), ("adobepremierepro", "Premiere Pro"), ("adobeaftereffects", "After Effects"),
                   ("adobephotoshop", "Photoshop"), ("ffmpeg", "FFmpeg"), ("note", "FL Studio")]),
    ]
    pad, gap = 50, 22
    colw = (W - 8 - pad * 2 - gap * 3) / 4
    rows = max(len(tools) for _, tools in columns)
    top, step, chip_h = 232, 58, 44
    spec_y = top + (rows - 1) * step + chip_h + 50
    H = spec_y + 72
    defs, body = fresh()
    back, front = field(defs, W, H, "teal", cut=(12, 0, 16, 0), at=(0.85, 0.15))
    body += [back, section_title(defs, "THE TOOLBOX", "Engines, code and craft. Only what I actually use.", pad, 138, 104, 700, W)]
    for i, (heading, tools) in enumerate(columns):
        x = pad + i * (colw + gap)
        markup, _ = tag(x, 168, heading, 19, INK, GOLD)
        body.append(markup)
        for j, (slug, label) in enumerate(tools):
            y = top + j * step
            rot = (-0.8, 0.6, -0.4, 0.8)[(i + j) % 4]
            body.append(
                f'<g transform="rotate({rot} {num(x + colw / 2)} {num(y + chip_h / 2)})">'
                f'<rect x="{num(x + 4)}" y="{y + 5}" width="{num(colw)}" height="{chip_h}" fill="{INK}"/>'
                f'<rect x="{num(x)}" y="{y}" width="{num(colw)}" height="{chip_h}" fill="{CREAM}" stroke="{INK}" stroke-width="3"/>'
                f'{glyph(slug, x + 14, y + (chip_h - 22) / 2, 22, INK)}'
                f'{text(SEMI, label, 19, x + 48, y + chip_h / 2 + SEMI.cap * 19 / 2, INK)}</g>'
            )
    spec, sw_ = tag(pad, spec_y, "Specialties", 19, GOLD, INK)
    body += [
        spec,
        shout(defs, XBOLD, "CUSTOM 2D PHYSICS · ECS/DOTS · UNITY EDITOR TOOLING · MCP AUTOMATION", 19,
              pad + sw_ + 16, spec_y + 19 + XBOLD.cap * 19 / 2 + 1),
        front,
    ]
    label = "The toolbox: " + ", ".join(name for _, tools in columns for _, name in tools)
    write("toolbox.svg", svg(W, H, label, body, defs))


# ── Footer ──────────────────────────────────────────────────────────────────


def footer():
    W, H = 1200, 560 if not CONTACT else 600
    defs, body = fresh()
    back, front = field(defs, W, H, "red", cut=(0, 16, 0, 0), at=(0.5, 0.5))
    services = ["full game builds", "prototypes & slices", "mechanics & systems", "polish & game feel"]
    size = BLACK.fit("SOMETHING", 700, 118)
    y1 = 142 + BLACK.cap * size
    y2 = y1 + size * 0.9
    saw = site_image("v2/heroes/ripsaw.webp").resize((242, 250), Image.LANCZOS)
    rpg = fit_width(site_image("v2/heroes/rpg.webp"), 320)
    body += [
        back,
        f'<g class="spin"><image href="{data_uri(saw, fmt="WEBP", quality=88)}" x="30" y="236" width="200" height="207" filter="url(#popdrop)"/></g>',
        pop(data_uri(rpg, fmt="WEBP", quality=88), 980, 60, 180, 180 * rpg.height / rpg.width, delay="-1.4s"),
    ]
    widths = [chip(0, 0, s, 19)[1] for s in services]
    total = sum(widths) + 14 * (len(widths) - 1)
    markup, _ = chips((W - total) / 2, 50, services, 19, 14)
    body += [
        markup,
        letter(defs, "LET’S BUILD", size, W / 2, y1, anchor="middle"),
        letter(defs, "SOMETHING", size, W / 2, y2, anchor="middle"),
    ]
    sub = MED.wrap("Got a game, a prototype, or a half-finished build that needs a push? Tell us about it. "
                   "Based in India, working with clients everywhere.", 22, 700)
    sy = y2 + 58
    body += [shout(defs, MED, line, 22, W / 2, sy + i * 30, anchor="middle", off=(1, 2), shadow_opacity=0.45) for i, line in enumerate(sub)]
    by = sy + (len(sub) - 1) * 30 + 30
    label_cta = "Start a project →"
    _, (bw, _) = slab(0, 0, label_cta, 22, GOLD, INK)
    markup, (bw, bh) = slab((W - bw) / 2, by, label_cta, 22, GOLD, INK)
    body.append(markup)
    if CONTACT:
        lead = "or email "
        ml, mw = MED.width(lead, 19), XBOLD.width(CONTACT, 19)
        mx, my = (W - ml - mw) / 2, by + bh + 42
        body += [shout(defs, MED, lead, 19, mx, my, off=(1, 2), shadow_opacity=0.45), shout(defs, XBOLD, CONTACT, 19, mx + ml, my, GOLD)]
    body.append(front)
    write("footer.svg", svg(W, H, "Let's build something. Full game builds, prototypes and slices, mechanics and systems, polish and game feel.", body, defs))


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
    now()
    stats()
    head("making", "indigo", "THE LINEUP", "Two games live on Google Play, one jam game you can play in a browser, and one client build.",
         "The lineup", (0, 14, 0, 0), (0.85, 0.2))
    cards()
    head("jams", "blue", "BEFORE THE STUDIO, THE JAMS", "Four podiums in 2025. Two 1sts, two 2nds, all team efforts.",
         "Before the studio, the jams", (14, 0, 0, 0), (0.15, 0.3))
    jams()
    contributions()
    toolbox()
    footer()


if __name__ == "__main__":
    main(sys.argv[1:])
