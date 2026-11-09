#!/usr/bin/env python3
"""Generate the decorative figures via Gemini 2.5 Flash Image.

The explanatory figures are HTML rendered with headless Chrome (see the .html
files beside this script). The rest of the callouts are metaphors — a garden, a
scale, a revolving door — which want illustration rather than diagramming.

Two things this script exists to get right. The model garbles any text it is
asked to render, so every prompt forbids words explicitly and the captions stay
in the markdown where they belong. And the whole set shares one style block, so
forty illustrations sit together as a course rather than as forty stock images.

Usage:  python3 figures-src/generate.py [FIGURE-ID ...]
        no arguments = every figure in SUBJECTS that has no PNG yet
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "figures"
KEY = (pathlib.Path.home() / ".gemini_key").read_text().strip()
URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-image:generateContent"
)

STYLE = (
    "Flat editorial vector illustration in a wide cinematic composition. "
    "Warm cream paper background. Restrained palette: deep teal-green, "
    "terracotta orange-red, muted sage, soft warm grey. Clean geometric shapes, "
    "confident simple lines, subtle paper texture, generous negative space. "
    "Calm, intelligent, editorial — like a quality business book illustration, "
    "not corporate stock photography. "
    "CRITICAL: absolutely no text, no letters, no numbers, no words, no labels, "
    "no signage, no writing of any kind anywhere in the image. "
    "Subject: "
)

# One line per figure: the metaphor its caption already promises, described
# visually. Kept close to the caption so image and text agree.
SUBJECTS = {

    "MGT-CM-01": (
        "One central figure speaking, with three distinct streams of shaped forms flowing away in three "
        "directions — upward to a single larger figure, downward to a group, and sideways to a peer. Each "
        "stream a different shape and colour."
    ),
    "MGT-CM-02": (
        "Two documents side by side: one with a single bold clear block at the very top and light detail "
        "beneath, glowing and being read; the other a dense uniform stack of grey lines, dull and "
        "untouched."
    ),
    "MGT-CM-03": (
        "A pyramid of stacked blocks where only the small topmost block is lifted away and glowing, "
        "travelling upward alone, while the wide base remains solid and ready beneath it."
    ),
    "MGT-CM-04": (
        "A round clock face, a short ordered list of three shapes, and a single decision token resting on "
        "a table between chairs. Spare, purposeful, nothing extra."
    ),
    "MGT-CM-05": (
        "Two figures seated facing each other early, with a small contained storm cloud between them "
        "shrinking into a tiny green shoot taking root on the table."
    ),

    "MGT-CX-01": (
        "A single winding path seen from above, with several distinct marked waypoints along it, ending "
        "at one glowing circular memory. Continuous line, unbroken."
    ),
    "MGT-CX-02": (
        "A single baton carried along a relay of several hands, but one continuous coloured thread runs "
        "through every hand from start to finish, never dropped."
    ),
    "MGT-CX-03": (
        "An umbrella held steady over a small figure in heavy rain, the umbrella itself formed from a "
        "folded promissory shape. Shelter that was agreed in advance."
    ),
    "MGT-CX-04": (
        "A balanced pair of scales with a documented trail of small stacked cards beneath one pan, "
        "protecting figures on both sides of the balance equally."
    ),
    "MGT-CX-05": (
        "A closed circular loop of arrows: signals entering at one point, an action taken at another, "
        "returning to a small figure at the start. A loop, emphatically not a dial."
    ),

    "MGT-DK-02": (
        "A vast wall of many small identical dials, with exactly four lifted out and glowing, connected "
        "by visible wires to a working machine below."
    ),
    "MGT-DK-03": (
        "A small measured plant in soil with a ruler beside it, and a flag planted a deliberate short "
        "distance ahead of its current height."
    ),
    "MGT-DK-05": (
        "A single pressure gauge whose needle is physically connected by linkage to a wrench that is "
        "turning a bolt. Measurement doing mechanical work."
    ),

    "MGT-DM-01": (
        "A balance scale with several small invisible-seeming weights already resting on one pan, tilting "
        "it before anything visible has been placed."
    ),
    "MGT-DM-03": (
        "An iceberg: a small sharp tip above the waterline, and a vast structured geometric mass below "
        "it, clearly the same object."
    ),
    "MGT-DM-04": (
        "A figure stepping across water on stones, able to see only the next stone clearly, with small "
        "marker flags planted on the stones already passed."
    ),
    "MGT-DM-05": (
        "Several figures around a table each with a speech form rising from them, all forms flowing into "
        "one single solid decision token held by one figure."
    ),

    "MGT-FN-05": (
        "An architectural structure of stacked blocks, transparent and open to view, with a magnifying "
        "lens examining the joints where it would be pressed."
    ),

    "MGT-HS-01": (
        "The same task shown twice: once protected by built-in machinery guards and rails that require "
        "nothing of the worker, once relying on a single tense figure concentrating hard."
    ),
    "MGT-HS-02": (
        "A dial scored once with the needle high, then a set of physical guards fitted, then the same "
        "dial scored again with the needle much lower."
    ),
    "MGT-HS-03": (
        "Five stacked steps descending, the widest and strongest at the top and the narrowest and most "
        "fragile at the bottom, with a small human figure supporting only the bottom step."
    ),
    "MGT-HS-04": (
        "Three elements resting together: a folded method card, a small competent crew of figures, and a "
        "large accessible stop button within easy reach."
    ),
    "MGT-HS-05": (
        "A rising line of many small light-coloured warning markers, with far fewer dark serious markers, "
        "drawn as a healthy wide-based pyramid on cream."
    ),

    "MGT-OP-01": (
        "Two overlaid process diagrams: a clean intended one in light grey, and the real one drawn over "
        "it in terracotta with extra loops and detours the first does not have."
    ),
    "MGT-OP-03": (
        "A flow of shapes moving through a pipe that narrows sharply at one point, with a large queue of "
        "shapes piled up before the narrowing and sparse space after it."
    ),
    "MGT-OP-04": (
        "A small controlled test plot beside a large field, the test plot fenced and measured, with a "
        "single marker showing what was predicted before it began."
    ),
    "MGT-OP-05": (
        "A short daily rhythm shown as evenly spaced identical marks along a line, each one small, "
        "keeping a mechanism visibly wound and running."
    ),

    "MGT-PE-01": (
        "Two versions of the same week side by side as blocks of time: one imagined, tidy and mostly "
        "whole; one recorded, fragmented into many small scattered pieces."
    ),
    "MGT-PE-02": (
        "A pair of scales where placing one new object onto a pan visibly lifts and displaces another "
        "object already there. Every yes displaces something."
    ),
    "MGT-PE-03": (
        "Four small separated fragments of a shape on one side, and two large whole blocks on the other, "
        "the whole blocks clearly more useful despite equal total area."
    ),
    "MGT-PE-04": (
        "A single open container holding many small varied tokens, with a calm empty head-shaped outline "
        "beside it, unburdened."
    ),
    "MGT-PE-05": (
        "A simple mechanism lying dormant, with one small crank beside it that would restart it — modest, "
        "obviously easy to pick up again."
    ),

    "MGT-PH-02": (
        "A built ramp rising gently and steadily upward with support rails, beside a bare steep cliff "
        "face a small figure is attempting to climb alone."
    ),
    "MGT-PH-03": (
        "A plain unglamorous filing card written at a desk, dated, sitting in a simple box — quiet and "
        "ordinary, waiting."
    ),
    "MGT-PH-04": (
        "Four sequential stages laid out left to right as connected stations, each one a distinct step, "
        "with the final judgement token appearing only at the end."
    ),
    "MGT-PH-05": (
        "A line of small early signals — a dimming light, a turned-away chair, an untouched cup — leading "
        "to a single envelope at the far end."
    ),

    "MGT-PM-01": (
        "A clearly marked destination point and one chosen route drawn to it, with faint tempting side- "
        "paths branching off and fading away unchosen."
    ),
    "MGT-PM-03": (
        "A table with several figures already seated around it early, each holding a small gate or key, "
        "brought together deliberately rather than encountered later."
    ),
    "MGT-PM-04": (
        "A written standard card held up beside an item being examined, the card and the item compared "
        "directly, with a gate behind them."
    ),
    "MGT-PM-05": (
        "A winding road with warning flags planted before each bend, and a single gate through which "
        "every change must pass onto the road."
    ),
    "MGT-PM-06": (
        "A set of keys, a bound record, and a compact knowledge form being handed from one pair of hands "
        "to another, with a clear closing marker behind them."
    ),

    "MGT-SF-02": "A large ornate keyhole in a door, with the key formed from a listening ear shape, about to turn.",
    "MGT-SF-03": (
        "A balance scale where the price side sits heavy and low until several distinct value blocks are "
        "loaded onto the other side, bringing it level."
    ),
    "MGT-SF-04": (
        "Two hands exchanging objects simultaneously across a table, each releasing something at the same "
        "instant as receiving."
    ),
    "MGT-SF-05": (
        "A closing handshake from which a continuous line loops back around to become the beginning of a "
        "new approach."
    ),

    "MGT-TL-01": (
        "One figure frantically juggling many tools alone at a workbench, while behind them a group of "
        "colleagues stands idle with empty hands, blocked and waiting."
    ),
    "MGT-TL-02": (
        "A baton passed from one hand to another, and the receiving figure is visibly taller and more "
        "capable than in the previous handover of the same baton."
    ),
    "MGT-TL-04": (
        "A closed loop of three connected stages cycling continuously — an observation, a practice "
        "attempt, visible growth — with no endpoint or verdict stamp."
    ),
    "MGT-TL-05": (
        "A calm central figure holding a wide shelter over a small group in a storm, with the storm "
        "clearly ending at a defined edge rather than fading."
    ),

    "MGT-VP-01": (
        "A sequence of three gates along a path, each one closed and requiring opening, with a stack of "
        "coins waiting to pass through them."
    ),
    "MGT-VP-03": (
        "A folded paper shelter over two figures on opposite sides, built while the weather was fine, now "
        "keeping rain off both equally."
    ),
    "MGT-VP-04": (
        "Goods counted at a gate, a signature mark, and shelved items each with a small name tag attached "
        "— an unbroken chain of custody."
    ),
    "MGT-VP-05": (
        "A tended garden bed where strong plants are supported with stakes and weak ones are being pruned "
        "away, deliberate and cared for."
    ),
    "MGT-VP-06": (
        "A clear glass box containing a straightforward transaction, transparent on all sides, allowing "
        "an honest exchange to be seen plainly from outside."
    ),
}


def generate(fid: str, subject: str) -> bool:
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": STYLE + subject}]}],
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
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 — constant https URL
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"  {fid}: HTTP {e.code} {e.read()[:180]!r}")
        return False
    except Exception as e:  # network, timeout
        print(f"  {fid}: {type(e).__name__} {e}")
        return False

    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            OUT.mkdir(exist_ok=True)
            (OUT / f"{fid}.png").write_bytes(base64.b64decode(part["inlineData"]["data"]))
            return True
    print(f"  {fid}: no image in response — {str(data)[:180]}")
    return False


def main() -> None:
    wanted = sys.argv[1:] or [
        f for f in SUBJECTS if not (OUT / f"{f}.png").exists()
    ]
    ok = 0
    for fid in wanted:
        if fid not in SUBJECTS:
            print(f"  {fid}: no subject defined")
            continue
        if generate(fid, SUBJECTS[fid]):
            ok += 1
            print(f"  {fid}: ok")
    print(f"{ok}/{len(wanted)} generated")


if __name__ == "__main__":
    main()
