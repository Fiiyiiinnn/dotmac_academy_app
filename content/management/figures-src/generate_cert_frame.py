#!/usr/bin/env python3
"""Build the certificate background: static/img/cert-frame.jpg.

render_certificate_pdf() (app/services/certificates.py) draws this as a
full-page background on an A4 landscape page (297x210mm) and then writes the
dynamic text (name, course, date, serial) on top of it.

Third design for this asset (2026-09-09), replacing a code-drawn brand-panel
layout that Michael reviewed against a real reference certificate photo
(banknote-style guilloche border, script title, ribbon seal, "CEO's
Signature" line, faint watermark) and asked to match. Two kinds of content,
two different tools, same rule as the earlier designs:

1. The guilloche border is an abstract repeating engraved pattern with no
   text and no brand identity of its own -- exactly what nano-banana is good
   at, so it's generated with Gemini (fetch_border()).
2. The watermark ("Dotmac Academy" + a node icon) and the ribbon seal (the
   real static/img/dotmac-favicon.png "D") assert actual brand identity, so
   they are built in code with PIL and composited on top (build_watermark(),
   build_ribbon_seal()) -- never asked from the image model. See
   `[[nano-banana-branded-elements-build-dont-generate]]` in the Knowledge
   server for the general lesson this follows.

Output is JPEG, not PNG: the guilloche pattern is fine engraved linework at
full-page size, and photographic/textured content like that deflates poorly
under PNG (~6.5MB) -- JPEG q92 with no chroma subsampling (subsampling=0,
keeps the thin coloured line edges crisp) is ~1.6MB with no visible
artifacting.

Usage:  python3 content/management/figures-src/generate_cert_frame.py
"""

from __future__ import annotations

import base64
import json
import math
import pathlib
import urllib.error
import urllib.request
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

REPO = pathlib.Path(__file__).resolve().parents[3]
OUT = REPO / "static" / "img" / "cert-frame.jpg"
FAVICON = REPO / "static" / "img" / "dotmac-favicon.png"
FONTS = REPO / "static" / "fonts" / "certificate"
KEY = (pathlib.Path.home() / ".gemini_key").read_text().strip()
URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)

# A4 landscape at 300dpi: 297mm x 210mm.
W, H = 3508, 2480

EMERALD = (47, 122, 82)
EMERALD_DARK = (32, 90, 60)
CREAM = (250, 246, 238)
TERRACOTTA = (194, 104, 60)
WHITE = (255, 255, 255)

BORDER_PROMPT = (
    "A classic guilloche banknote-style ornamental certificate border, "
    "rendered as FULL-BLEED edge-to-edge artwork filling the entire canvas "
    "corner to corner -- not a card, not a mockup, no drop shadow, no "
    "background distinct from the border itself. "
    "Design: a wide scalloped outer band made of intricate fine engraved "
    "guilloche linework (the kind of interwoven wavy security-printing "
    "pattern seen on currency and formal diplomas), in a deep emerald green "
    "(#2f7a52), running continuously around all four edges of the page. "
    "Just inside that band, a thin single terracotta-orange rule "
    "(#c2683c) separates it from a plain, completely empty, flat cream "
    "interior (#faf6ee) that fills the rest of the page. "
    "In each of the four corners, a small delicate quarter-circle flourish "
    "line ornament sits just inside the terracotta rule, echoing the "
    "guilloche engraving style, simple and not busy. "
    "The interior center is entirely empty and uncluttered -- no seal, no "
    "emblem, no watermark, no logo, no ribbon, nothing but the plain cream "
    "field; all of that is added separately. "
    "CRITICAL: absolutely no text, no letters, no numbers, no words, no "
    "logos, no watermark, no signage anywhere in the image."
)


def fetch_border() -> Image.Image:
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": BORDER_PROMPT}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "4:3"},
            },
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 -- URL is a constant https literal
        URL, data=body, headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
        data = json.load(r)
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            raw = base64.b64decode(part["inlineData"]["data"])
            im = Image.open(BytesIO(raw)).convert("RGB")
            return im.resize((W, H), Image.LANCZOS)
    raise RuntimeError(f"no image in response -- {str(data)[:300]}")


def _extract_white_glyph(path: pathlib.Path, size: int) -> Image.Image:
    """Pull just the white "D" out of the real favicon (a D on its own green
    rounded square), discarding that square's green -- a different shade
    from our certificate emerald. Upscale the smooth grayscale FIRST (Lanczos
    interpolates a soft edge), then threshold at the target resolution, or
    the low-res (192px) source's curves get quantised to that pixel grid and
    upscaling afterwards only enlarges the jaggies."""
    im = Image.open(path).convert("L")
    im = im.resize((size, size), Image.LANCZOS)
    mask = im.point(lambda p: max(0, min(255, int((p - 150) * 255 / 60))))
    clip = Image.new("L", (size, size), 0)
    r = int(size * 0.47)
    ImageDraw.Draw(clip).ellipse(
        (size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r), fill=255
    )
    mask = Image.composite(mask, Image.new("L", (size, size), 0), clip)
    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glyph.paste(white, (0, 0), mask)
    return glyph


def _scalloped_circle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
                       n_scallops: int, amp: float, fill) -> None:
    pts = []
    steps = 360
    for i in range(steps):
        th = 2 * math.pi * i / steps
        rad = r + amp * math.cos(n_scallops * th)
        pts.append((cx + rad * math.sin(th), cy - rad * math.cos(th)))
    draw.polygon(pts, fill=fill)


def build_ribbon_seal(diam: int) -> Image.Image:
    """A rosette-and-ribbon award seal: the real Dotmac "D" inside a scalloped
    medallion with two swallow-tail ribbon tails below -- the same
    composition family as the reference certificate's generic checkmark
    ribbon, but carrying the actual brand mark instead of clip art."""
    ss = 3
    d = diam * ss
    # Short tails, deliberately: at 0.85 (the first cut) the ribbon hung well
    # past the footer rule/labels it sits beside, so the medal looked like it
    # was sinking below the row instead of sitting level with it.
    tail_h = int(d * 0.42)
    canvas = Image.new("RGBA", (d, d + tail_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    cx, cy = d // 2, d // 2

    tail_top = cy + int(d * 0.30)
    tw = d * 0.20
    draw.polygon([
        (cx - tw * 1.6, tail_top), (cx - tw * 0.2, tail_top),
        (cx - tw * 0.2, tail_top + tail_h * 0.85), (cx - tw * 0.9, tail_top + tail_h),
        (cx - tw * 1.6, tail_top + tail_h * 0.85),
    ], fill=EMERALD_DARK)
    draw.polygon([
        (cx + tw * 0.2, tail_top), (cx + tw * 1.6, tail_top),
        (cx + tw * 1.6, tail_top + tail_h * 0.85), (cx + tw * 0.9, tail_top + tail_h),
        (cx + tw * 0.2, tail_top + tail_h * 0.85),
    ], fill=EMERALD)

    _scalloped_circle(draw, cx, cy, d * 0.40, 22, d * 0.018, EMERALD)
    draw.ellipse((cx - d*0.34, cy - d*0.34, cx + d*0.34, cy + d*0.34), fill=EMERALD_DARK)
    draw.ellipse((cx - d*0.34, cy - d*0.34, cx + d*0.34, cy + d*0.34),
                 outline=(*CREAM, 255), width=int(d * 0.008))
    draw.ellipse((cx - d*0.30, cy - d*0.30, cx + d*0.30, cy + d*0.30),
                 outline=(*CREAM, 255), width=int(d * 0.004))

    glyph = _extract_white_glyph(FAVICON, int(d * 0.40))
    canvas.alpha_composite(glyph, (cx - glyph.width // 2, cy - glyph.height // 2))

    return canvas.resize((diam, int((d + tail_h) / ss)), Image.LANCZOS)


def build_watermark(max_width: int) -> Image.Image:
    """A faint node-network icon + "Dotmac Academy" wordmark, sized to fit
    max_width -- sits behind the dynamic text, like the reference's
    watermark."""
    h = int(max_width * 0.16)
    f_word = ImageFont.truetype(str(FONTS / "Fraunces-SemiBold.ttf"), int(h * 0.62))
    text = "Dotmac Academy"
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    tw = probe.textlength(text, font=f_word)
    icon_r = h * 0.42
    gap = h * 0.35
    total_w = int(icon_r * 2 + gap + tw) + 20
    if total_w > max_width:
        scale = max_width / total_w
        h = int(h * scale)
        f_word = ImageFont.truetype(str(FONTS / "Fraunces-SemiBold.ttf"), int(h * 0.62))
        tw = probe.textlength(text, font=f_word)
        icon_r = h * 0.42
        gap = h * 0.35
        total_w = int(icon_r * 2 + gap + tw) + 20

    canvas = Image.new("RGBA", (total_w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    col = (*EMERALD, 26)

    icon_cx, icon_cy = icon_r + 4, h * 0.5
    sat = icon_r * 0.62
    pts = [(icon_cx, icon_cy - sat), (icon_cx + sat, icon_cy),
           (icon_cx, icon_cy + sat), (icon_cx - sat, icon_cy)]
    for px, py in pts:
        draw.line((icon_cx, icon_cy, px, py), fill=col, width=int(h * 0.035))
    draw.ellipse((icon_cx - h*0.10, icon_cy - h*0.10, icon_cx + h*0.10, icon_cy + h*0.10), fill=col)
    for px, py in pts:
        rr = h * 0.075
        draw.ellipse((px - rr, py - rr, px + rr, py + rr), fill=col)

    draw.text((icon_cx + icon_r + gap, h * 0.5), text, font=f_word, fill=col, anchor="lm")
    return canvas


def compose(border: Image.Image) -> Image.Image:
    out = border.convert("RGBA")

    # 0.28*H centers the watermark's ink (vertically centered within its own
    # band, see build_watermark's anchor="lm" at h*0.5) on ~76mm -- where the
    # recipient name's cell in certificates.py actually puts its ink -- so
    # the name sits over the wordmark, not the divider/caption below it
    # (the original 0.36 centered the watermark on ~93mm instead, behind
    # "has completed the course" rather than the name).
    wm = build_watermark(int(W * 0.72))
    out.alpha_composite(wm, ((W - wm.width) // 2, int(H * 0.28)))

    # Top of the seal graphic at 0.566*H puts the ribbon tail tips at
    # ~141mm -- just past the footer rule certificates.py draws at 140mm,
    # like a real hanging medal. Keep the two in sync by hand if either the
    # seal proportions above or the footer's rule y in certificates.py
    # change; there's no shared source of truth between the two files.
    seal = build_ribbon_seal(int(W * 0.062))
    out.alpha_composite(seal, ((W - seal.width) // 2, int(H * 0.566)))

    return out.convert("RGB")


def save(im: Image.Image) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    im.save(OUT, "JPEG", quality=92, optimize=True, subsampling=0)
    print(f"wrote {OUT} at {im.size}")


def main() -> None:
    save(compose(fetch_border()))


if __name__ == "__main__":
    main()
