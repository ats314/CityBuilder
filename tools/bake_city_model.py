#!/usr/bin/env python3
"""Predict a skyline from access and activity, then subtract the measured one.

The lidar bake answers "how tall is it". This answers "how tall should it be",
and the difference is the artifact.

The argument is standard monocentric bid-rent: land rent falls with distance
from activity, and developers substitute capital for land by building upward
where rent is high. So height should track a potential built from where the
activity is and how reachable it is.

SCALE MATTERS, and getting it wrong inverts the answer. Fitted inside a single
1 km box in Midtown, POI density correlates with height at r=+0.13 and
major-road proximity at r=-0.11 — the model concludes that being near a big
street makes buildings shorter. Widen to 8 km and the same predictors give
r=+0.58 and r=+0.23. Manhattan's 1811 grid deliberately flattened local access
variation, so below about a kilometre there is no gradient left to measure; you
are sampling inside one bump. This tool therefore FITS on a wide coarse tile and
EVALUATES on a narrow fine one.

    residual << 0   economics wants height here and something stopped it
                    (zoning envelope, landmark protection, air rights sold,
                     assembly failure, bedrock)
    residual >> 0   taller than access and activity alone explain

Data, all public, all read straight from S3:
    heights   USGS 3DEP lidar          (public domain)
    streets   Overture transportation  (see THIRD_PARTY_LICENSES.md)
    activity  Overture places          (same)
"""

import argparse
import importlib.util
import json
import math
import os

import numpy as np

HSTEP = 1.6
RELEASE = "2026-07-22.0"
MAJOR = {"motorway", "trunk", "primary", "secondary"}


def load_overture():
    spec = importlib.util.spec_from_file_location(
        "ov", os.path.join(os.path.dirname(os.path.abspath(__file__)), "overture_fetch.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def gaussian(a, sigma_cells):
    """Separable Gaussian blur; sigma given in cells so callers think in metres."""
    s = max(sigma_cells, 0.5)
    # numpy's 'same' returns max(len(signal), len(kernel)), so a kernel wider
    # than the grid silently grows the array instead of blurring it
    k = min(int(s * 3) | 1, (a.shape[0] - 1) | 1)
    xs = np.arange(k) - k // 2
    g = np.exp(-xs ** 2 / (2 * s * s))
    g /= g.sum()
    o = np.apply_along_axis(lambda r: np.convolve(r, g, "same"), 1, a)
    return np.apply_along_axis(lambda r: np.convolve(r, g, "same"), 0, o)


class Tile:
    """One baked extent plus the Overture layers rasterised onto it."""

    def __init__(self, png, lat, lon, cell_m, ov, label=""):
        from PIL import Image
        img = np.asarray(Image.open(png))
        # The tile declares its own grid size by its width, so a 512-cell tile
        # covering 2 km can borrow predictors from a 256-cell fit tile.
        G = self.g = img.shape[1]
        self.h = img[:G, :, 0].astype(np.float64) * HSTEP
        self.flags = img[G:G * 2, :, 0].astype(np.uint8)
        self.built = self.h > 6.0
        self.cell_m = cell_m
        dlat = (G * cell_m / 2) / 111320.0
        dlon = dlat / math.cos(math.radians(lat))
        self.bbox = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

        major = np.zeros((G, G))
        poi = np.zeros((G, G))
        self.n_seg = self.n_poi = 0
        quiet = lambda *a: None

        def cell(lo, la):
            return ((lo - self.bbox[0]) / (self.bbox[2] - self.bbox[0]) * G,
                    (la - self.bbox[1]) / (self.bbox[3] - self.bbox[1]) * G)

        def line(grid, x0, y0, x1, y1):
            n = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
            for i in range(n + 1):
                t = i / n
                x, y = int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t)
                if 0 <= x < G and 0 <= y < G:
                    grid[y, x] += 1.0

        from shapely import wkb
        for t in ov.read_bbox(RELEASE, "transportation", "segment", self.bbox,
                              columns=["subtype", "class", "geometry"], progress=quiet):
            d = t.to_pydict()
            for sub, cls, geo in zip(d["subtype"], d["class"], d["geometry"]):
                if sub != "road" or cls not in MAJOR:
                    continue
                g = wkb.loads(bytes(geo))
                if g.geom_type != "LineString":
                    continue
                cs = list(g.coords)
                self.n_seg += 1
                for (a0, b0), (a1, b1) in zip(cs, cs[1:]):
                    line(major, *cell(a0, b0), *cell(a1, b1))
        for t in ov.read_bbox(RELEASE, "places", "place", self.bbox,
                              columns=["geometry"], progress=quiet):
            for geo in t.to_pydict()["geometry"]:
                g = wkb.loads(bytes(geo))
                x, y = cell(g.x, g.y)
                if 0 <= int(x) < G and 0 <= int(y) < G:
                    poi[int(y), int(x)] += 1.0
                    self.n_poi += 1

        # Predictors defined in METRES so the two tiles are comparable even
        # though their cells differ by 8x.
        self.activity = gaussian(poi, 640.0 / cell_m)
        self.roads = gaussian((major > 0).astype(float), 128.0 / cell_m)
        ys, xs = np.nonzero(poi)
        if len(ys):
            w = poi[ys, xs]
            cy, cx = (ys * w).sum() / w.sum(), (xs * w).sum() / w.sum()
        else:
            cy = cx = G / 2
        Y, X = np.mgrid[0:G, 0:G]
        self.centre = 1.0 / (1.0 + np.hypot(Y - cy, X - cx) * cell_m / 1000.0)
        print(f"{label}: {self.built.sum():,} building cells, {self.n_seg:,} major segments, "
              f"{self.n_poi:,} places, {cell_m:g} m cells")

    def design(self, mask=None):
        m = self.built if mask is None else mask
        return np.stack([np.log(self.activity[m] + 1e-6),
                         np.log(self.centre[m] + 1e-6),
                         np.log(self.roads[m] + 1e-6),
                         np.ones(int(m.sum()))], axis=1)

    def sample_into(self, other):
        """Resample this tile's predictor fields onto another tile's grid.

        The predictors are only meaningful at the scale they were built at, so a
        fine tile must borrow them from the wide fit rather than recompute a
        640 m blur inside a 1 km box, where it degenerates to a constant.
        """
        lon = np.linspace(other.bbox[0], other.bbox[2], other.g)
        lat = np.linspace(other.bbox[1], other.bbox[3], other.g)
        gx = (lon - self.bbox[0]) / (self.bbox[2] - self.bbox[0]) * (self.g - 1)
        gy = (lat - self.bbox[1]) / (self.bbox[3] - self.bbox[1]) * (self.g - 1)
        gx = np.clip(gx, 0, self.g - 1)
        gy = np.clip(gy, 0, self.g - 1)
        X, Y = np.meshgrid(gx, gy)
        x0, y0 = np.floor(X).astype(int), np.floor(Y).astype(int)
        x1, y1 = np.minimum(x0 + 1, self.g - 1), np.minimum(y0 + 1, self.g - 1)
        fx, fy = X - x0, Y - y0

        def bilerp(a):
            return (a[y0, x0] * (1 - fx) * (1 - fy) + a[y0, x1] * fx * (1 - fy) +
                    a[y1, x0] * (1 - fx) * fy + a[y1, x1] * fx * fy)

        return bilerp(self.activity), bilerp(self.centre), bilerp(self.roads)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-png", required=True, help="wide coarse bake, where the gradient exists")
    ap.add_argument("--fit-lat", type=float, required=True)
    ap.add_argument("--fit-lon", type=float, required=True)
    ap.add_argument("--fit-cell", type=float, default=32.0)
    ap.add_argument("--eval-png", required=True, help="narrow fine bake, the one you walk")
    ap.add_argument("--eval-lat", type=float, required=True)
    ap.add_argument("--eval-lon", type=float, required=True)
    ap.add_argument("--eval-cell", type=float, default=4.0)
    ap.add_argument("--out", default="city-model.png")
    ap.add_argument("--report", default=None)
    ap.add_argument("--append-to", default=None,
                    help="walkable bake to append the model band onto")
    args = ap.parse_args()

    from PIL import Image
    ov = load_overture()

    fit = Tile(args.fit_png, args.fit_lat, args.fit_lon, args.fit_cell, ov, "fit tile ")
    ev = Tile(args.eval_png, args.eval_lat, args.eval_lon, args.eval_cell, ov, "eval tile")

    # ---- fit where the gradient is measurable -----------------------------
    X = fit.design()
    y = np.log(fit.h[fit.built] + 1.0)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    r2_fit = 1 - float(((y - X @ coef) ** 2).sum()) / float(((y - y.mean()) ** 2).sum())
    names = ["log activity", "log centrality", "log major-road density", "intercept"]
    print("\nfit on the wide tile:")
    for n, c in zip(names, coef):
        print(f"    {n:<26} {c:+.4f}")
    print(f"    R^2 = {r2_fit:.3f} over {fit.built.sum():,} cells")

    # ---- evaluate at walking resolution, using the wide tile's fields ------
    act, cen, rds = fit.sample_into(ev)
    D = np.stack([np.log(act + 1e-6), np.log(cen + 1e-6),
                  np.log(rds + 1e-6), np.ones((ev.g, ev.g))], axis=-1)
    pred = np.clip(np.exp(D @ coef) - 1.0, 0, 400)
    resid = np.where(ev.built, ev.h - pred, 0.0)
    rs = resid[ev.built]
    ye = np.log(ev.h[ev.built] + 1.0)
    pe = np.log(pred[ev.built] + 1.0)
    r2_eval = 1 - float(((ye - pe) ** 2).sum()) / float(((ye - ye.mean()) ** 2).sum())
    print(f"\nevaluated on the fine tile: transferred R^2 = {r2_eval:.3f}")
    print(f"residual  mean {rs.mean():+.1f} m  sd {rs.std():.1f} m"
          f"  p5 {np.percentile(rs,5):+.0f}  p95 {np.percentile(rs,95):+.0f}")
    print(f"unexplained variance: {(1-max(r2_eval,0))*100:.0f}% of the skyline")

    scale = max(1.0, float(np.percentile(np.abs(rs), 95)))
    model = np.zeros((ev.g, ev.g, 4), dtype=np.uint8)
    model[..., 0] = np.clip(128 + resid / scale * 127, 0, 255).astype(np.uint8)
    model[..., 1] = np.clip(pred / HSTEP, 0, 255).astype(np.uint8)
    model[..., 2] = np.clip(act / (act.max() + 1e-9) * 255, 0, 255).astype(np.uint8)
    model[..., 3] = 255
    Image.fromarray(model, "RGBA").save(args.out, optimize=True)

    # Append the model as a fourth band on the walkable bake, so the renderer can
    # switch what colour means without loading a second file.
    if args.append_to:
        base = np.asarray(Image.open(args.append_to))
        G = ev.g
        bands = base.shape[0] // G
        merged = np.zeros((G * 4, G, 4), dtype=np.uint8)
        merged[:G * bands] = base[:G * bands]
        merged[G * 3:, :, 0] = model[..., 0]         # residual, 128 = as predicted
        merged[G * 3:, :, 1] = model[..., 1]         # predicted height
        merged[G * 3:, :, 2] = model[..., 2]         # activity potential
        merged[..., 3] = 255
        Image.fromarray(merged, "RGBA").save(args.append_to, optimize=True)
        print(f"appended model band to {args.append_to} ({bands} -> 4 bands)")

    rep = {
        "fit": {n: round(float(c), 4) for n, c in zip(names, coef)},
        "r2_fit_wide": round(r2_fit, 4),
        "r2_eval_fine": round(r2_eval, 4),
        "fit_extent_m": fit.g * args.fit_cell,
        "eval_extent_m": ev.g * args.eval_cell,
        "residual_m": {"mean": round(float(rs.mean()), 2), "sd": round(float(rs.std()), 2),
                       "p5": round(float(np.percentile(rs, 5)), 1),
                       "p95": round(float(np.percentile(rs, 95)), 1),
                       "byte_scale": round(scale, 1)},
        "inputs": {"fit_segments": fit.n_seg, "fit_places": fit.n_poi,
                   "eval_segments": ev.n_seg, "eval_places": ev.n_poi},
        "sources": ["USGS 3DEP lidar (public domain)",
                    f"Overture Maps {RELEASE} transportation + places"],
    }
    if args.report:
        json.dump(rep, open(args.report, "w"), indent=1)
    print(json.dumps(rep, indent=1))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
