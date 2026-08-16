#!/usr/bin/env python3
"""Boot the renderer headlessly and assert it actually draws.

There is no build step and no unit-testable module here — the whole program is
one HTML file whose output is a WebGL frame — so the only check worth having is
the one that opens it. A GLSL compile error does not fail a build; it fails at
runtime, and the page keeps serving. That is exactly the class of breakage this
catches, because a failed compile shows up as a console error.

Usage:  python3 tools/smoke_test.py [--headed] [--shots DIR]
Exit code is non-zero on the first failed assertion, so it works as a CI gate.
"""

import argparse
import os
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "index.html"

# Every alphabet the page offers. Each must pass its own support probe and then
# draw a frame; QUADRANT and BRAILLE are the two families HYBRID is built from,
# so a regression in either shows up here before it shows up in HYBRID.
ALPHABETS = ["ASCII", "TELETEXT", "QUADRANT", "BRAILLE", "HYBRID"]

# Chromium is preinstalled in some environments and installed by playwright in
# others; honour an explicit path when one is set rather than guessing.
CHROME = os.environ.get("SMOKE_CHROME") or None


class Failed(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Failed(msg)
    print("  ok   " + msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", default=None, help="write a PNG per alphabet here")
    ap.add_argument("--page", default=str(PAGE), help="page to test; defaults to index.html")
    args = ap.parse_args()
    page_path = pathlib.Path(args.page).resolve()

    if args.shots:
        pathlib.Path(args.shots).mkdir(parents=True, exist_ok=True)

    errors = []
    with sync_playwright() as p:
        launch = {"headless": not args.headed, "args": [
            "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
            "--no-sandbox", "--disable-dev-shm-usage"]}
        if CHROME:
            launch["executable_path"] = CHROME
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append("pageerror: %s" % str(e)[:300]))
        page.on("console", lambda m: errors.append("console: %s" % m.text[:300])
                if m.type == "error" else None)

        page.goto(page_path.as_uri())
        page.wait_for_timeout(3000)

        # Start on a measured city rather than a procedural one: it exercises the
        # tile decode, the flag plane and the terrain band as well as the renderer.
        page.click("#goLidar")
        page.wait_for_timeout(8000)

        print("renderer")
        check(page.evaluate("typeof CITY === 'object' && CITY !== null"), "city installed")
        check(page.evaluate("CITY.measured === true"), "measured (lidar) city loaded")
        n = page.evaluate("CITY.n")
        check(n in (256, 512), "tile grid is %s" % n)
        check(page.evaluate("CITY.hasModel === true"), "model plane band present")

        print("glyph tables")
        table = page.evaluate(
            "ALPHABETS.map(a => ({n: a.name, ok: a.ok !== false, g: a.glyphs.length}))")
        for want, got in zip(ALPHABETS, table):
            check(got["n"] == want, "alphabet %d is %s" % (table.index(got), want))
            check(got["ok"], "%s passes its support probe" % want)

        # HYBRID is braille's 256 dot masks with the 16 row-paired ones swapped for
        # solid blocks. Both counts are load-bearing: 256 because the glyph index is
        # a single byte, and 16 because that is what makes the blocks appear at all.
        hy = page.evaluate("""(() => {
          const q = new Set(ALPHABETS.find(a => a.name === 'QUADRANT').glyphs);
          const t = ALPHABETS.find(a => a.name === 'HYBRID').glyphs;
          return {len: t.length, blocks: t.filter(g => q.has(g)).length,
                  full: t[255], empty: t[0], topHalf: t[15], leftHalf: t[85]};
        })()""")
        check(hy["len"] == 256, "HYBRID table is 256 glyphs (one-byte index)")
        check(hy["blocks"] == 16, "HYBRID has exactly 16 block glyphs")
        check(hy["full"] == "█", "mask 11111111 draws as full block")
        check(hy["topHalf"] == "▀", "mask 00001111 draws as top half")
        check(hy["leftHalf"] == "▌", "mask 01010101 draws as left half")

        print("frames")
        for name in ALPHABETS:
            i = page.evaluate("ALPHABETS.findIndex(a => a.name === %r)" % name)
            page.evaluate("setAlphabet(%d)" % i)
            page.wait_for_timeout(2000)
            check(page.evaluate("A().name") == name, "%s selected" % name)
            hud = page.evaluate("document.getElementById('tl').textContent")
            fps = re.search(r"FPS\s+(\d+)", hud or "")
            check(fps is not None and int(fps.group(1)) > 0,
                  "%s is drawing (%s)" % (name, fps.group(0) if fps else "no FPS"))
            if args.shots:
                page.screenshot(path=os.path.join(args.shots, "%s.png" % name.lower()))

        print("second city")
        page.evaluate("loadLidarCity(1)")
        page.wait_for_timeout(6000)
        check(page.evaluate("CITY.measured === true"), "second lidar tile decodes")

        browser.close()

    if errors:
        print("\nconsole/page errors (a GLSL compile failure lands here):")
        for e in errors[:10]:
            print("  " + e)
        raise Failed("%d console/page error(s)" % len(errors))
    print("\nall checks passed")


if __name__ == "__main__":
    try:
        main()
    except Failed as e:
        print("\nFAILED: %s" % e)
        sys.exit(1)
