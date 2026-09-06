#!/usr/bin/env python3
"""Screenshot the megacity, so it can be looked at instead of guessed at.

Every number about a baked city can be right while the picture is broken. There
is no GPU in CI and none in most agent sandboxes, so the only way to check a
city is a headless browser over SwiftShader: 1-4 fps, useless for playing,
fine for a still.

    python3 tools/shoot_ascii.py --city 'AMSTERDAM CANAL BELT' --out build/shots

Drives the page's own globals (`player`, `S`, `CITY`) rather than synthesising
input, because a screenshot of a walk that drifted is not a screenshot of the
thing you meant to check. Console errors and failed requests are reported: a
black frame with a shader error in the log is a different problem from a black
frame without one.
"""
from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import threading
from pathlib import Path

#: Poses as (eye height above the ground under the camera, yaw degrees, pitch).
#: The city picks the spot -- its own spawn chooser stands you mid-carriageway
#: facing down the longest clear run, which is a better camera than any offset
#: guessed from the block centre.
VIEWS = {
    "spawn":    {"eye": 1.78, "yaw": None, "pitch": 0.02},
    "look_up":  {"eye": 1.78, "yaw": None, "pitch": 0.34},
    "drone":    {"eye": 90.0, "yaw": None, "pitch": -0.62},
}


def serve(root: Path, port: int):
    handler = type("Q", (http.server.SimpleHTTPRequestHandler,), {
        "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
            self, *a, directory=str(root), **k),
        "log_message": lambda *a: None,
    })
    # allow_reuse_address must be set before bind, so it goes on the class.
    # Port 0 lets the OS pick: a crashed run leaves the old port held, and a
    # fixed default turns that into a confusing EADDRINUSE on the next shot.
    reusable = type("R", (socketserver.TCPServer,), {"allow_reuse_address": True})
    server = reusable(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html")
    ap.add_argument("--root", default=".")
    ap.add_argument("--city", default=None,
                    help="name from LIDAR_CITIES; omit for a generated city")
    ap.add_argument("--out", default="build/shots")
    ap.add_argument("--port", type=int, default=0,
                    help="0 lets the OS pick a free one")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--views", default=",".join(VIEWS))
    ap.add_argument("--settle-ms", type=int, default=4000,
                    help="SwiftShader needs seconds per frame; do not rush it")
    ap.add_argument("--chrome",
                    default="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server, port = serve(Path(args.root), args.port)
    problems: list[str] = []

    with sync_playwright() as pw:
        launch = {"args": ["--use-gl=angle", "--use-angle=swiftshader",
                           "--enable-unsafe-swiftshader"]}
        if Path(args.chrome).exists():
            launch["executable_path"] = args.chrome
        browser = pw.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("requestfailed",
                lambda r: problems.append(f"request failed: {r.url}"))

        page.goto(f"http://127.0.0.1:{port}/{args.page}",
                  wait_until="load", timeout=120_000)
        page.wait_for_timeout(1500)

        # Start, then install the requested city by name so a reordered
        # LIDAR_CITIES array does not silently shoot a different place.
        page.evaluate("() => start()")
        if args.city:
            found = page.evaluate(
                """(name) => {
                    const i = LIDAR_CITIES.findIndex(c => c.name === name);
                    if (i < 0) return null;
                    loadLidarCity(i);
                    return LIDAR_CITIES[i].name;
                }""", args.city)
            if found is None:
                names = page.evaluate("() => LIDAR_CITIES.map(c => c.name)")
                print(f"no city named {args.city!r}; have {names}")
                browser.close(); server.shutdown()
                return 2
            page.wait_for_function(
                "() => CITY && CITY.measured === true", timeout=60_000)

        page.wait_for_timeout(args.settle_ms)

        info = page.evaluate(
            "() => ({ n: N, title: CITY && CITY.title, "
            "spawn: CITY && CITY.spawn, measured: !!(CITY && CITY.measured) })")
        print(json.dumps(info, indent=1))

        for name in args.views.split(","):
            view = VIEWS.get(name.strip())
            if view is None:
                continue
            page.evaluate(
                """(v) => {
                    // The ground under the camera, not the world minimum: a
                    // block with relief puts "street level" metres underground
                    // if you anchor to the bounds instead.
                    player.ground = groundAtWorld(player.x, player.z);
                    player.y = player.ground + v.eye;
                    player.fly = v.eye > 10;
                    if (v.yaw !== null) player.yaw = v.yaw;
                    player.pitch = v.pitch;
                    player.vx = player.vz = player.vy = 0;
                }""", view)
            page.wait_for_timeout(args.settle_ms)
            path = out / f"{name.strip()}.png"
            page.screenshot(path=str(path))
            print(f"  {path}  {path.stat().st_size / 1024:.0f} KB")

        browser.close()
    server.shutdown()

    if problems:
        print("\nproblems the browser reported:")
        for line in dict.fromkeys(problems):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
