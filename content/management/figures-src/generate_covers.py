#!/usr/bin/env python3
"""Generate catalogue cover images for courses that lack one.

Covers are a different artefact from the in-chapter figures and want a
different look. The existing covers are softer and more peopled — muted
desaturated palette, stylised human figures with faces and business attire,
gentle drop shadows — where the figures are bolder and more geometric. This
script matches the covers, so a new course does not stand out in the
catalogue as obviously generated later.

Output is 1344x768 WebP at `static/img/cover-<slug>.webp`, matching what is
already there.

Usage:  python3 figures-src/generate_covers.py [SLUG ...]
        no arguments = every course in COVERS without a cover
"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[3]
OUT = REPO / "static" / "img"
KEY = (pathlib.Path.home() / ".gemini_key").read_text().strip()
URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)

STYLE = (
    "Soft flat editorial illustration for a course catalogue cover, wide format. "
    "Warm cream paper background. Muted desaturated palette: soft teal-green, "
    "gentle terracotta and salmon, warm grey, pale sage. Stylised human figures "
    "with simple friendly faces and business attire, subtle soft drop shadows, "
    "clean uncluttered shapes, calm and approachable, generous negative space. "
    "Professional and warm rather than corporate or clinical. "
    "CRITICAL: absolutely no text, no letters, no numbers, no words, no labels, "
    "no signage, no writing of any kind anywhere in the image. "
    "Scene: "
)

COVERS = {
    "mgmt-people-hr": (
        "A manager sitting across a small table welcoming a new person, with a "
        "few other figures nearby at different stages of joining a team — one "
        "arriving, one settled at work, one being handed a folder. Warm, "
        "human, about people joining and being looked after."
    ),
    "mgmt-health-safety": (
        "A calm supervisor observing a work site where physical guards, rails "
        "and clear markings do the protecting, while a worker in simple "
        "protective gear works comfortably. Orderly and unalarming — safety as "
        "something built into the place rather than a warning."
    ),
    "mgmt-personal-effectiveness": (
        "One focused person at an uncluttered desk within a calm protected "
        "block of space, while outside that space small competing demands wait "
        "in an orderly queue rather than crowding in. About attention and "
        "deliberate boundaries."
    ),
    "mgmt-operations-process": (
        "A team watching work flow smoothly along a simple conveyor of "
        "connected stages, one person adjusting a single stage that is "
        "narrower than the rest. Orderly, mechanical, about flow and the "
        "constraint that governs it."
    ),
}


def generate(slug: str, scene: str) -> bool:
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": STYLE + scene}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "16:9"},
            },
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 — URL is a constant https literal
        URL,
        data=body,
        headers={"x-goog-api-key": KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  {slug}: HTTP {e.code} {e.read()[:180]!r}")
        return False
    except Exception as e:
        print(f"  {slug}: {type(e).__name__} {e}")
        return False

    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            from PIL import Image

            raw = base64.b64decode(part["inlineData"]["data"])
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            if im.size != (1344, 768):
                im = im.resize((1344, 768), Image.LANCZOS)
            OUT.mkdir(parents=True, exist_ok=True)
            im.save(OUT / f"cover-{slug}.webp", "WEBP", quality=88, method=6)
            return True
    print(f"  {slug}: no image in response — {str(data)[:180]}")
    return False


def main() -> None:
    wanted = sys.argv[1:] or [
        s for s in COVERS if not (OUT / f"cover-{s}.webp").exists()
    ]
    ok = 0
    for slug in wanted:
        if slug not in COVERS:
            print(f"  {slug}: no scene defined")
            continue
        if generate(slug, COVERS[slug]):
            ok += 1
            print(f"  {slug}: ok")
    print(f"{ok}/{len(wanted)} generated")


if __name__ == "__main__":
    main()
