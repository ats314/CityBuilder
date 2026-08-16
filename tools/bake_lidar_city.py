#!/usr/bin/env python3
"""Bake a USGS 3DEP lidar tile into a NOCTIS-7 city texture.

The renderer consumes a 256x256 RGBA8 grid (height, palette|style, density,
flags). Procedural generation fills that grid from a seed; the source-city
parser fills it from text. This fills it from a laser survey, so the same
renderer walks a real place with no rendering code changed.

The points themselves are never shipped. What matters is the subtraction:

    DSM (highest return per cell) - DTM (ground surface) = height above ground

which is exactly the height field the renderer already eats. 4.7 billion points
collapse to a 256 KB texture.

Data: https://registry.opendata.aws/usgs-lidar/ (public domain)
Usage: python3 tools/bake_lidar_city.py --lat 40.7530 --lon -73.9860 --out city.png
"""

import argparse
import io
import json
import math
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

BASE = "https://s3-us-west-2.amazonaws.com/usgs-lidar-public"
R_EARTH = 6378137.0
GRID = 256                 # renderer grid, cells per side (--grid overrides)
CELL_M = 4.0               # real metres per cell -> 256 cells is 1024 m across
HSTEP = 1.6                # renderer metres per height byte
MAX_DEPTH = 9

# renderer flag bits
ROAD, ROADX, ROADZ, PARK, WATER, SIGNSTRIP, BEACON, PLAZA = 1, 2, 4, 8, 16, 32, 64, 128

# LAS classification codes we care about
C_GROUND, C_LOWVEG, C_MEDVEG, C_HIGHVEG, C_BUILDING = 2, 3, 4, 5, 6
C_NOISE, C_WATER, C_BRIDGE, C_NOISE2 = 7, 9, 17, 18


def lonlat_to_mercator(lon, lat):
    x = lon * R_EARTH * math.pi / 180.0
    y = R_EARTH * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def fetch(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
    return None


def node_bounds(ept_bounds, d, x, y, z):
    size = (ept_bounds[3] - ept_bounds[0]) / (2 ** d)
    return (ept_bounds[0] + x * size, ept_bounds[1] + y * size,
            ept_bounds[0] + (x + 1) * size, ept_bounds[1] + (y + 1) * size)


def overlaps(nb, box):
    return not (nb[2] <= box[0] or nb[0] >= box[2] or nb[3] <= box[1] or nb[1] >= box[3])


def collect_nodes(dataset, ept, box, max_depth):
    """Walk the EPT hierarchy, keeping nodes whose XY footprint hits the box.

    A count of -1 means the subtree is described in its own hierarchy file. That
    has to be followed inline: re-queueing the same key against a visited-set
    silently truncates the walk at the first chunk boundary, which caps the whole
    fetch at the root's handful of nodes no matter what depth is requested.
    """
    out = []

    def walk(key, table):
        d, x, y, z = (int(v) for v in key.split("-"))
        if d > max_depth:
            return
        if not overlaps(node_bounds(ept["bounds"], d, x, y, z), box):
            return
        count = table.get(key)
        if count is None or count == 0:
            return
        if count == -1:
            walk(key, json.loads(fetch(f"{BASE}/{dataset}/ept-hierarchy/{key}.json")))
            return
        out.append((key, count))
        if d < max_depth:
            for dx in (0, 1):
                for dy in (0, 1):
                    for dz in (0, 1):
                        walk(f"{d+1}-{2*x+dx}-{2*y+dy}-{2*z+dz}", table)

    walk("0-0-0-0", json.loads(fetch(f"{BASE}/{dataset}/ept-hierarchy/0-0-0-0.json")))
    return out


# Road-vs-other-ground, fitted on Midtown against Overture carriageways buffered
# to their real class widths. See docs/SURFACE_CLASSIFICATION.md — the headline
# is that the survey does carry this, once the labels are not centrelines:
# AUC 0.885, AP 0.926 held out on a spatial split, against a 0.680 base rate.
#
# Features are z-scored against the TILE's own distribution, not Manhattan's,
# because intensity is 8-bit in New York and 16-bit in San Francisco and nothing
# else would transfer at all. That makes the transfer plausible, not verified;
# San Francisco has no class 17 to speak of and is not a grid.
ROAD_BASE = ("mean_int", "int_norm", "int_sd", "f_hi", "f_vhi", "f_brg",
             "multi", "single", "rough", "angle", "dens", "height")
ROAD_TERMS = (
    ("mean_int", -2.07158), ("int_norm", -0.85173), ("int_sd", +1.88868),
    ("f_hi", +0.56879), ("f_vhi", -0.74993), ("f_brg", -0.22129),
    ("multi", -0.25697), ("single", +0.25697), ("rough", -0.21209),
    ("angle", -0.11784), ("dens", +0.65757), ("height", -0.36675),
    ("mean_int^2", +0.49602), ("int_norm^2", -0.12277), ("int_sd^2", -0.30139),
    ("f_hi^2", +0.04056), ("f_vhi^2", -0.03471), ("f_brg^2", -0.19024),
    ("multi^2", +0.01426), ("single^2", +0.01426), ("rough^2", +0.00072),
    ("angle^2", -0.12100), ("dens^2", -0.08480), ("height^2", +0.33783),
    ("mean_int*int_norm", +0.54877), ("mean_int*int_sd", +0.09679),
    ("mean_int*f_hi", -0.56720), ("mean_int*f_vhi", +0.09351),
    ("mean_int*f_brg", -0.37176), ("mean_int*multi", +0.00623),
    ("mean_int*single", -0.00623), ("mean_int*rough", +0.03353),
    ("mean_int*angle", +0.18246), ("mean_int*dens", -0.04269),
    ("mean_int*height", +0.16021),
)
ROAD_BIAS = +1.55736


def road_probability(feats, mask):
    """Per-cell probability that a ground cell is carriageway.

    Plain logistic with squares and intensity interactions rather than the
    gradient-boosted model that scores 0.903, because 35 numbers embed in this
    file and a forest does not. The linear model alone only reaches 0.811, so
    the interactions are carrying the non-linearity that matters.
    """
    z = {}
    for k in ROAD_BASE:
        v = feats[k].astype(np.float64)
        mu, sd = float(v[mask].mean()), float(v[mask].std())
        z[k] = (v - mu) / (sd if sd > 1e-9 else 1.0)
    logit = np.full(feats["height"].shape, ROAD_BIAS)
    for name, w in ROAD_TERMS:
        if name.endswith("^2"):
            t = z[name[:-2]] ** 2
        elif "*" in name:
            a, b = name.split("*")
            t = z[a] * z[b]
        else:
            t = z[name]
        logit += w * t
    return 1.0 / (1.0 + np.exp(-np.clip(logit, -30, 30)))


def regions(mask):
    """4-connected components of a boolean grid, largest first.

    Yields index arrays, so a caller can test each candidate blob on its own
    rather than on the mask as a whole. Iterative on purpose: a river fills a
    third of the tile and recursion would blow the stack.
    """
    h, w = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    found = []
    for sy in range(h):
        for sx in range(w):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            stack, cells = [(sy, sx)], []
            seen[sy, sx] = True
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            found.append(np.array(cells))
    found.sort(key=len, reverse=True)
    return found


def roof_planes(building, height, band_m=6.0):
    """Label roof planes: connected building cells inside one height band.

    Plain connected components merge a whole Manhattan block into one 8,102-cell
    mass, which makes any per-building statistic meaningless — the vertical
    spread of a block containing a tower is the tower, not the roof. Banding by
    height first separates the tower from the podium it stands on.
    """
    from scipy import ndimage
    band = np.floor(height / band_m).astype(np.int64)
    out = np.zeros(building.shape, dtype=np.int64)
    nxt = 0
    for b in np.unique(band[building]):
        lab, n = ndimage.label(building & (band == b))
        hit = lab > 0
        out[hit] = lab[hit] + nxt
        nxt += n
    return out


def plane_median(a, plane):
    """Broadcast each roof plane's median back over its own cells."""
    from scipy import ndimage
    n = int(plane.max())
    if n == 0:
        return np.zeros_like(a, dtype=np.float64)
    med = np.asarray(ndimage.median(a, plane, np.arange(1, n + 1)), dtype=np.float64)
    return np.where(plane > 0, med[np.clip(plane - 1, 0, n - 1)], 0.0)


def boxmean(a, r):
    """Mean over a (2r+1) square, edge-clamped, via an integral image."""
    p = np.pad(a.astype(np.float64), r, mode="edge")
    c = np.pad(np.cumsum(np.cumsum(p, axis=0), axis=1), ((1, 0), (1, 0)))
    n = 2 * r + 1
    return (c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]) / (n * n)


def close_speckle(mask, max_hole):
    """Fill pinholes in a mask without drowning islands.

    Thresholding return density cell by cell leaves a river full of holes, since
    plenty of individual water cells do return a normal number of points. Only
    holes smaller than max_hole and not touching the tile edge are filled, so
    speckle closes but a genuine island in the channel survives.
    """
    out = mask.copy()
    for cells in regions(~mask):
        if len(cells) > max_hole:
            continue
        ys, xs = cells[:, 0], cells[:, 1]
        if (ys.min() == 0 or xs.min() == 0
                or ys.max() == mask.shape[0] - 1 or xs.max() == mask.shape[1] - 1):
            continue
        out[ys, xs] = True
    return out


def water_from_returns(n_pts, height, dsm, valid, min_cells):
    """Find water from return structure rather than from the class channel.

    Class 9 is the documented answer and this survey barely populates it: over
    the East River it tags 4,204 cells of a body three times that size, because
    it can only tag cells that returned something. So water is measured the same
    way the vegetation detector measures foliage — off the beam.

    Three channels were measured against the cells this survey did tag, on
    Midtown at 4 m cells, and only one of them is worth anything:

        points/cell   water 15    other flat ground 46   weak: 70% recall
        intensity     water 69    other flat ground 43   real but *inverted*
        multi-return  water 0.00  other flat ground 0.00 nothing

    Near-infrared is absorbed by water, so the expectation was a dropout and
    darkness. Density does fall, but only threefold rather than to nothing, and
    intensity goes *up*: a flat surface returns specularly, so the few beams
    that do come back come back hard. Both predictions had the wrong magnitude
    and one had the wrong sign, which is why the rule below is not either of
    them.

    What actually identifies water is that its top surface is a plane. Over
    13,461 contiguous cells the East River's DSM spans 0.20 m. No land does
    that: the next candidate down, a pier at the same elevation, spans 1.34 m
    and returns points more densely than typical ground rather than less. So
    the rule is waterline plus flatness, with density kept only as corroboration
    — it is reported, not gated on, because on its own it recalls 70%.

    Flatness is judged on the DSM, which over water is the water surface itself.
    The interpolated ground surface cannot be used, because it never reaches the
    middle of a river at all (see the caller).
    """
    lit = n_pts[valid]
    if lit.size == 0:
        return np.zeros(n_pts.shape, dtype=bool), []
    typical = float(np.median(lit))
    # The waterline: if this tile has water at all, its lowest surfaces are it.
    base = float(np.nanpercentile(dsm[valid], 2))
    surf = np.where(valid, dsm, base)      # a void reads as being at the waterline
    cand = close_speckle((surf <= base + 2.5) & (height < 2.0),
                         max_hole=max(32, min_cells // 4))
    water = np.zeros(n_pts.shape, dtype=bool)
    kept = []
    for cells in regions(cand):
        if len(cells) < min_cells:
            break                          # sorted by size: nothing later qualifies
        ys, xs = cells[:, 0], cells[:, 1]
        seen = valid[ys, xs]
        dens = float(np.median(n_pts[ys, xs]))
        if seen.sum() < 8:                 # a true void: large and empty is water
            water[ys, xs] = True
            kept.append((len(cells), base, 0.0, dens))
            continue
        s = dsm[ys[seen], xs[seen]]
        lo, hi = np.nanpercentile(s, [10, 90])
        if hi - lo > 0.75 or dens > typical:
            continue                       # not a plane, or denser than ground
        water[ys, xs] = True
        kept.append((len(cells), float(np.nanmedian(s)), float(hi - lo), dens))
    return water, kept


def main():
    global CELL_M, GRID
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="NY_NewYorkCity")
    ap.add_argument("--lat", type=float, default=40.7530)
    ap.add_argument("--lon", type=float, default=-73.9860)
    ap.add_argument("--depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--cell", type=float, default=CELL_M,
                    help="real metres per cell; 4 m covers 1 km, 32 m covers 8 km")
    ap.add_argument("--grid", type=int, default=GRID,
                    help="cells per side; must match the renderer's N. Doubling it "
                         "doubles the extent at unchanged detail and costs no extra "
                         "raycast work, because world cell size is unchanged.")
    ap.add_argument("--out", default="lidar-city.png")
    ap.add_argument("--meta", default=None)
    ap.add_argument("--dump", default=None,
                    help="write the accumulated per-cell fields to an .npz, so "
                         "detectors can be tuned without re-fetching the tile")
    args = ap.parse_args()
    CELL_M = args.cell
    GRID = args.grid

    import laspy
    from PIL import Image

    ept = json.loads(fetch(f"{BASE}/{args.dataset}/ept.json"))
    print(f"dataset {args.dataset}: {ept['points']:,} points, srs {ept['srs'].get('authority')}:"
          f"{ept['srs'].get('horizontal')}")

    # Web Mercator inflates horizontal distance by 1/cos(lat); undo it so a cell
    # is 4 real metres and the city comes out at true scale.
    merc_per_m = 1.0 / math.cos(math.radians(args.lat))
    half = GRID * CELL_M * merc_per_m / 2.0
    cx, cy = lonlat_to_mercator(args.lon, args.lat)
    box = (cx - half, cy - half, cx + half, cy + half)
    print(f"centre {args.lat},{args.lon} -> mercator {cx:.0f},{cy:.0f}")
    print(f"extent {GRID * CELL_M:.0f} m across, {CELL_M} m per cell, "
          f"mercator scale {merc_per_m:.4f}")

    nodes = collect_nodes(args.dataset, ept, box, args.depth)
    total = sum(c for _, c in nodes)
    print(f"octree: {len(nodes)} nodes overlap, {total:,} points to fetch")
    if not nodes:
        sys.exit("no lidar coverage at that location")

    # accumulators
    dsm = np.full((GRID, GRID), -9e9, dtype=np.float64)       # highest return
    gnd = np.full((GRID, GRID), 9e9, dtype=np.float64)        # lowest ground return
    n_pts = np.zeros((GRID, GRID), dtype=np.int64)
    n_bld = np.zeros((GRID, GRID), dtype=np.int64)
    n_veg = np.zeros((GRID, GRID), dtype=np.int64)
    n_wat = np.zeros((GRID, GRID), dtype=np.int64)
    n_gnd = np.zeros((GRID, GRID), dtype=np.int64)
    # material evidence: near-IR reflectance and canopy penetration
    s_int = np.zeros((GRID, GRID), dtype=np.float64)
    s_int2 = np.zeros((GRID, GRID), dtype=np.float64)
    s_intn = np.zeros((GRID, GRID), dtype=np.float64)   # flight-line normalised
    n_multi = np.zeros((GRID, GRID), dtype=np.int64)
    # ...and the channels the first version of this tool threw away. See
    # docs/SURFACE_CLASSIFICATION.md: together these take road-vs-other-ground
    # from AUC 0.791 on mean intensity alone to 0.897, with no second sensor.
    s_rgb = np.zeros((GRID, GRID, 3), dtype=np.float64)   # NYC carries colour
    n_rgb = np.zeros((GRID, GRID), dtype=np.int64)
    s_ang = np.zeros((GRID, GRID), dtype=np.float64)      # incidence angle
    s_z2 = np.zeros((GRID, GRID), dtype=np.float64)       # vertical spread
    s_z = np.zeros((GRID, GRID), dtype=np.float64)
    n_first = np.zeros((GRID, GRID), dtype=np.int64)      # single-echo fraction
    # Averaging intensity destroys the most road-specific thing in the channel:
    # lane paint is retroreflective, so a painted cell has a bright TAIL rather
    # than a bright mean. Count the tail instead of smoothing it away.
    n_hi = np.zeros((GRID, GRID), dtype=np.int64)
    n_vhi = np.zeros((GRID, GRID), dtype=np.int64)
    # Class 17 is 6.9M of Midtown's 18.4M points and was being thrown away. A
    # bridge deck is a road surface, so this is free label data.
    n_brg = np.zeros((GRID, GRID), dtype=np.int64)
    n_rail = np.zeros((GRID, GRID), dtype=np.int64)
    cls_hist = {}
    have_rgb = [False]

    # Intensity is uncalibrated between flight lines — one node here carries 116
    # distinct point_source_ids, each with its own gain — so the same asphalt
    # reads differently depending on which pass caught it. Sample a spread of
    # nodes to get a per-flight median, then scale each point onto a common one.
    # Sampling rather than a full pre-pass: this costs ~7% of the fetch.
    sample = [nodes[i][0] for i in range(0, len(nodes), max(1, len(nodes) // 48))]
    si, ss = [], []
    for key in sample:
        try:
            f = laspy.read(io.BytesIO(fetch(f"{BASE}/{args.dataset}/ept-data/{key}.laz")))
        except Exception:
            continue
        si.append(np.asarray(f.intensity).astype(np.float64))
        ss.append(np.asarray(f.point_source_id))
    si, ss = np.concatenate(si), np.concatenate(ss)
    HI, VHI = np.percentile(si, [90, 99])
    GLOBAL_MED = float(np.median(si))
    SRC_ID = np.unique(ss)
    SRC_GAIN = np.ones(len(SRC_ID))
    for i, s in enumerate(SRC_ID):
        med = float(np.median(si[ss == s]))
        SRC_GAIN[i] = GLOBAL_MED / med if med > 1e-6 else 1.0
    spread = float(np.percentile(SRC_GAIN, 90) / max(np.percentile(SRC_GAIN, 10), 1e-6))
    print(f"{len(sample)} sampled nodes, {len(si):,} points, {len(SRC_ID)} flight lines")
    print(f"intensity thresholds p90 {HI:.0f}, p99 {VHI:.0f}; median {GLOBAL_MED:.0f}; "
          f"flight-line gain spread p90/p10 = {spread:.2f}x")

    def ingest(item):
        key, _ = item
        try:
            raw = fetch(f"{BASE}/{args.dataset}/ept-data/{key}.laz")
        except Exception as e:
            print(f"  skip {key}: {e}")
            return 0
        f = laspy.read(io.BytesIO(raw))
        x, y, z = np.asarray(f.x), np.asarray(f.y), np.asarray(f.z)
        cls = np.asarray(f.classification)

        keep = (cls != C_NOISE) & (cls != C_NOISE2)
        ix = ((x - box[0]) / (box[2] - box[0]) * GRID).astype(np.int64)
        iy = ((y - box[1]) / (box[3] - box[1]) * GRID).astype(np.int64)
        keep &= (ix >= 0) & (ix < GRID) & (iy >= 0) & (iy < GRID)
        if not keep.any():
            return 0
        ix, iy, z, cls = ix[keep], iy[keep], z[keep], cls[keep]
        u, c = np.unique(cls, return_counts=True)
        for k, v in zip(u.tolist(), c.tolist()):
            cls_hist[k] = cls_hist.get(k, 0) + v
        flat = iy * GRID + ix
        np.maximum.at(dsm.reshape(-1), flat, z)
        np.add.at(n_pts.reshape(-1), flat, 1)
        inten = np.asarray(f.intensity)[keep].astype(np.float64)
        np.add.at(s_int.reshape(-1), flat, inten)
        np.add.at(s_int2.reshape(-1), flat, inten * inten)
        # same asphalt, different flight, different number: scale it out
        gi = np.searchsorted(SRC_ID, np.asarray(f.point_source_id)[keep])
        np.add.at(s_intn.reshape(-1), flat,
                  inten * SRC_GAIN[np.clip(gi, 0, len(SRC_ID) - 1)])
        nret = np.asarray(f.number_of_returns)[keep]
        mm = nret > 1
        if mm.any():
            np.add.at(n_multi.reshape(-1), flat[mm], 1)
        np.add.at(s_z.reshape(-1), flat, z)
        np.add.at(s_z2.reshape(-1), flat, z * z)
        single = nret == 1
        if single.any():
            np.add.at(n_first.reshape(-1), flat[single], 1)
        # Intensity is uncalibrated: the same asphalt returns differently at
        # nadir and at the edge of the swath, which is a good part of why it
        # only reaches 0.58 sigma raw. Keep the angle so it can be regressed out.
        np.add.at(s_ang.reshape(-1), flat,
                  np.abs(np.asarray(f.scan_angle_rank)[keep].astype(np.float64)))
        dims = set(f.point_format.dimension_names)
        if {"red", "green", "blue"} <= dims:
            have_rgb[0] = True
            for i, ch in enumerate(("red", "green", "blue")):
                np.add.at(s_rgb[..., i].reshape(-1), flat,
                          np.asarray(getattr(f, ch))[keep].astype(np.float64))
            np.add.at(n_rgb.reshape(-1), flat, 1)
        for mask, acc in ((inten > HI, n_hi), (inten > VHI, n_vhi),
                          (cls == C_BRIDGE, n_brg), (cls == 10, n_rail),
                          (cls == C_BUILDING, n_bld),
                          ((cls == C_HIGHVEG) | (cls == C_MEDVEG), n_veg),
                          (cls == C_WATER, n_wat)):
            if mask.any():
                np.add.at(acc.reshape(-1), flat[mask], 1)
        g = cls == C_GROUND
        if g.any():
            np.minimum.at(gnd.reshape(-1), flat[g], z[g])
            np.add.at(n_gnd.reshape(-1), flat[g], 1)
        return len(z)

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for got in pool.map(ingest, nodes):
            done += got
            if done and done % 500000 < got:
                print(f"  ingested {done:,} points")
    print(f"ingested {done:,} points into the grid")
    NAMES = {1:'unclassified',2:'ground',3:'low veg',4:'med veg',5:'high veg',6:'BUILDING',
             7:'noise',9:'water',10:'rail',11:'road surface',17:'bridge',18:'noise'}
    for k in sorted(cls_hist, key=lambda k: -cls_hist[k]):
        print(f"   class {k:<3} {NAMES.get(k,'?'):<14} {cls_hist[k]:>10,}")
    if done == 0:
        sys.exit("no points landed in the target box")

    # ---- ground surface: sparse in a dense city, so fill it from what we have
    have_g = n_gnd > 0
    print(f"ground returns cover {100 * have_g.mean():.1f}% of cells")
    ground = np.where(have_g, gnd, np.nan)
    if have_g.any():
        # coarse median fill, then a couple of smoothing passes over the holes
        base = np.nanmedian(ground)
        filled = np.where(np.isnan(ground), base, ground)
        for _ in range(24):
            pad = np.pad(filled, 1, mode="edge")
            blur = (pad[:-2, 1:-1] + pad[2:, 1:-1] + pad[1:-1, :-2] + pad[1:-1, 2:]) / 4.0
            filled = np.where(have_g, ground, blur)
    else:
        filled = np.zeros((GRID, GRID))

    valid = n_pts > 0
    height = np.where(valid, dsm - filled, 0.0)

    # A max-per-cell DSM takes the highest return, which in an unclassified
    # survey includes birds and scan artefacts. Real towers are many contiguous
    # cells; a spike is one cell far above its neighbours.
    pad = np.pad(height, 1, mode="edge")
    stack = np.stack([pad[:-2, :-2], pad[:-2, 1:-1], pad[:-2, 2:],
                      pad[1:-1, :-2],                pad[1:-1, 2:],
                      pad[2:, :-2],  pad[2:, 1:-1],  pad[2:, 2:]])
    neigh = np.median(stack, axis=0)
    spikes = height > neigh + 45.0
    print(f"despiked {int(spikes.sum())} isolated cells")
    height = np.where(spikes, neigh, height)
    height = np.clip(height, 0.0, 250 * HSTEP)

    # ---- terrain: the ground surface is a height field too, and discarding it
    #      flattens San Francisco onto a plane
    terr = np.where(valid, filled, np.nan)
    if np.isnan(terr).all():
        terr = np.zeros((GRID, GRID))
    tmin = float(np.nanmin(terr))
    terr = np.where(np.isnan(terr), tmin, terr) - tmin
    trange = float(terr.max())
    tstep = max(trange / 250.0, 0.02)
    print(f"terrain relief {trange:.1f} m over the tile ({tstep:.3f} m per byte)")

    # ---- material evidence, and what it is actually worth ----------------
    # Measured on Midtown with silhouette edges excluded:
    #   roughness   street 0.41 vs roofs 0.47-0.50  -> no signal at 4 m cells.
    #               A roof is flat; cell size is larger than roof texture.
    #   intensity   street 46 vs roofs 60-70, 0.48 sigma. Weak but consistent:
    #               asphalt is dark in near-IR, membrane and gravel are not.
    #   multi-ret   street 0.7% vs roofs 5-16%. This one works. A beam that
    #               returns twice went through something, and the only thing on
    #               a roof it goes through is foliage.
    # So this is a vegetation detector, not a materials classifier, and it is
    # labelled as such rather than dressed up.
    mean_int = np.where(valid, s_int / np.maximum(n_pts, 1), 0.0)
    multi = np.where(valid, n_multi / np.maximum(n_pts, 1), 0.0)
    int_med = float(np.median(mean_int[valid])) if valid.any() else 0.0

    # ---- classify each cell
    classified = (n_bld.sum() + n_veg.sum() + n_wat.sum()) > 0.01 * max(done, 1)
    if not classified:
        print("classification channel is sparse; deriving from geometry alone")
    tagged = (n_wat > n_pts * 0.5) & (n_wat > 0)
    if tagged.any():
        # Calibrate against the cells the survey did tag, the same way the
        # vegetation detector was calibrated: print the separation and let the
        # numbers pick the channel rather than picking it from physics alone.
        ref = valid & ~tagged & (height < 2.0)
        for label, arr in (("points/cell", n_pts), ("intensity", mean_int),
                           ("ground elev", filled), ("multi-return", multi)):
            w = float(np.median(arr[tagged])) if tagged.any() else 0.0
            d = float(np.median(arr[ref])) if ref.any() else 0.0
            print(f"   {label:<13} water {w:8.2f}   other flat ground {d:8.2f}")
    measured, blobs = water_from_returns(n_pts, height, dsm, valid,
                                         min_cells=max(64, int(0.002 * GRID * GRID)))
    for n, lvl, spread, dens in blobs:
        print(f"water body: {n:,} cells, surface {lvl:.2f} m, {spread:.2f} m spread, "
              f"{dens:.0f} points/cell")
    # Both sources, not either. Where the survey tags water it is right; it is
    # just far from complete.
    water = tagged | measured
    print(f"water: {int(tagged.sum()):,} cells tagged class 9, "
          f"{int(measured.sum()):,} measured from returns, {int(water.sum()):,} together")
    if not water.any():
        print("no water in this extent")

    if water.any():
        # The ground fill propagates from cells that have ground returns, and a
        # river has none — 24 blur passes reach about 24 cells, and the East
        # River is wider than that, so mid-channel cells kept the tile median
        # (14.6 m, identical to the streets). Left alone the river would render
        # as a lagoon perched at street level. The DSM over water is the water
        # surface, so use that, and level each body onto it.
        for cells in regions(water):
            ys, xs = cells[:, 0], cells[:, 1]
            seen = valid[ys, xs]
            level = (float(np.median(dsm[ys[seen], xs[seen]])) if seen.sum() >= 8
                     else float(np.nanmin(filled)))
            filled[ys, xs] = level
        height = np.where(water, 0.0, height)
        terr = np.where(valid | water, filled, np.nan)
        tmin = float(np.nanmin(terr))
        terr = np.where(np.isnan(terr), tmin, terr) - tmin
        trange = float(terr.max())
        tstep = max(trange / 250.0, 0.02)
        print(f"terrain relief {trange:.1f} m after levelling water "
              f"({tstep:.3f} m per byte)")

    # Per-cell features derived from the channels above, for the classifier.
    npz = np.maximum(n_pts, 1)
    rough = np.sqrt(np.maximum(s_z2 / npz - (s_z / npz) ** 2, 0.0))   # vertical spread
    single = n_first / npz                                            # single-echo share
    angle = s_ang / npz
    int_norm = np.where(valid, s_intn / npz, 0.0)
    int_sd = np.sqrt(np.maximum(s_int2 / npz - (s_int / npz) ** 2, 0.0))
    f_hi, f_vhi = n_hi / npz, n_vhi / npz
    f_brg, f_rail = n_brg / npz, n_rail / npz
    rgb = s_rgb / np.maximum(n_rgb, 1)[..., None]
    if have_rgb[0]:
        mx = max(float(rgb.max()), 1.0)
        rgb = rgb / mx                                                # 8- or 16-bit
        lum = rgb @ np.array([0.299, 0.587, 0.114])
        # green excess: the standard vegetation index when there is no NIR band
        gex = np.clip(2 * rgb[..., 1] - rgb[..., 0] - rgb[..., 2], -1, 1)
        print(f"colour present: median luminance {np.median(lum[valid]):.3f}, "
              f"green excess {np.median(gex[valid]):+.3f}")
    else:
        lum = np.zeros((GRID, GRID))
        gex = np.zeros((GRID, GRID))
        print("no colour in this survey; classifier runs without it")

    if args.dump:
        np.savez_compressed(args.dump, n_pts=n_pts, dsm=np.where(valid, dsm, np.nan),
                            filled=filled, valid=valid, height=height, n_wat=n_wat,
                            mean_int=mean_int, multi=multi, water=water, rough=rough,
                            single=single, angle=angle, lum=lum, gex=gex, rgb=rgb,
                            n_veg=n_veg, n_bld=n_bld, has_rgb=np.array(have_rgb),
                            int_sd=int_sd, int_norm=int_norm, f_hi=f_hi, f_vhi=f_vhi,
                            f_brg=f_brg, f_rail=f_rail)
        print(f"dumped per-cell fields to {args.dump}")

    veg = valid & (multi > 0.35) & (height > 2.0)
    if classified:
        veg |= (n_veg > n_bld) & (n_veg > n_pts * 0.25) & (height > 2)
    building = height > 6.0

    # street level: flat, dry, and actually surveyed
    street = valid & ~water & (height < 2.0)
    # Everything below 2 m used to be flagged ROAD, which made Fifth Avenue, a
    # courtyard and a plaza the same material. Ask the survey instead.
    p_road = road_probability(
        {"mean_int": mean_int, "int_norm": int_norm, "int_sd": int_sd,
         "f_hi": f_hi, "f_vhi": f_vhi, "f_brg": f_brg, "multi": multi,
         "single": single, "rough": rough, "angle": angle,
         "dens": n_pts.astype(np.float64), "height": height}, street)
    carriage = street & (p_road > 0.5)
    paved = street & ~carriage
    print(f"surface: {int(carriage.sum()):,} carriageway, {int(paved.sum()):,} other "
          f"paved ground ({100 * carriage.sum() / max(street.sum(), 1):.0f}% road)")

    tex = np.zeros((GRID, GRID, 4), dtype=np.uint8)
    hb = np.clip(np.round(height / HSTEP), 0, 250).astype(np.uint8)
    hb[~building] = 0
    tex[..., 0] = hb

    # colour carries height, not appearance: a real data channel
    bands = np.array([0, 1, 2, 15, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], dtype=np.uint8)
    idx = np.clip((height / 22.0).astype(np.int64), 0, len(bands) - 1)
    pal = bands[idx]
    pal = np.where(veg, 3, pal)

    # ---- facades, from the roof rather than from a height threshold --------
    # Style used to be two rules on height and mean intensity, applied per cell,
    # so a single tower could come out speckled with three different facades.
    # Now the tile is cut into roof planes — connected cells inside the same 6 m
    # height band, so a tower does not merge with the block it stands on — and
    # each plane is classified as a whole from the median of its returns.
    #
    # Measured over 6,577 Midtown roof planes covering 76% of the built area:
    #
    #   cells   int  bright  multi  rough   medH   reads as
    #  69,448    49   0.000   0.03    3.0    39 m  dark uniform, tar and asphalt
    #  36,385    73   0.108   0.06    4.2    47 m  mid-tone
    #   8,622   111   0.454   0.05    3.0    50 m  bright membrane or ballast
    #   7,166    64   0.064   0.07   36.7   144 m  tower tops cluttered with plant
    #   9,776    36   0.000   0.49    3.6    11 m  canopy and green roof
    #
    # The rules below are those clusters written as thresholds instead of
    # centroids, so the classes mean the same thing in a city they were not
    # fitted on. Honest caveat: lidar sees roofs, not walls, so the facade is
    # inferred from the roof as a correlate of building type and age. A bright
    # membrane roof is a modern commercial box; tar over a 39 m mass is not.
    plane = roof_planes(building, height)
    med = lambda a: plane_median(a, plane)
    p_int, p_hi, p_multi, p_rough = med(mean_int), med(f_hi), med(multi), med(rough)
    style = np.full((GRID, GRID), 0, dtype=np.uint8)
    style = np.where(p_int > int_med * 1.15, 3, style)     # mid-tone: staggered grid
    style = np.where(p_hi > 0.30, 2, style)                # bright: wide glazing
    style = np.where(p_rough > 15.0, 1, style)             # cluttered tops: ribbon
    style = np.where(p_multi > 0.35, 7, style)             # planted roofs
    style = np.where(veg, 7, style)                        # vegetation wins outright
    for s, name in ((0, "dark uniform"), (3, "mid-tone"), (2, "bright membrane"),
                    (1, "cluttered tower top"), (7, "planted")):
        k = int(((style == s) & building).sum())
        if k:
            print(f"facade {s} {name:<20} {k:>7,} cells")
    tex[..., 1] = (pal & 15) | ((style & 15) << 4)

    dens = np.clip(0.22 + (height / 260.0), 0.15, 0.72)
    tex[..., 2] = (dens * 255).astype(np.uint8)

    flags = np.zeros((GRID, GRID), dtype=np.uint8)
    flags |= np.where(water, WATER, 0).astype(np.uint8)
    flags |= np.where(carriage, ROAD | ROADX | ROADZ, 0).astype(np.uint8)
    flags |= np.where(paved & ~veg, PLAZA, 0).astype(np.uint8)
    flags |= np.where(veg & ~building, PARK, 0).astype(np.uint8)
    flags |= np.where(building & (height > 90), BEACON, 0).astype(np.uint8)
    flags |= np.where(~valid & ~water, PLAZA, 0).astype(np.uint8)
    tex[..., 3] = flags

    # Canvas backing stores are premultiplied, so a data byte in the alpha
    # channel silently destroys RGB wherever it is small — and flag bits are
    # mostly small. Emit a 256x512 fully opaque image instead: the grid on top,
    # the flag plane underneath.
    out = np.zeros((GRID * 3, GRID, 4), dtype=np.uint8)
    out[:GRID, :, 0:3] = tex[..., 0:3]
    out[GRID:GRID * 2, :, 0] = tex[..., 3]
    out[GRID * 2:, :, 0] = np.clip(terr / tstep, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    Image.fromarray(out, "RGBA").save(args.out, optimize=True)

    built = building.sum()
    stats = {
        "dataset": args.dataset, "lat": args.lat, "lon": args.lon,
        "cell_m": CELL_M, "grid": GRID, "points_ingested": int(done),
        "coverage_pct": round(100 * float(valid.mean()), 1),
        "tallest_m": round(float(height.max()), 1),
        "mean_building_m": round(float(height[building].mean()), 1) if built else 0,
        "building_cells": int(built), "water_cells": int(water.sum()),
        "street_cells": int(street.sum()), "veg_cells": int(veg.sum()),
        "terrain_relief_m": round(trange, 1), "terrain_step_m": round(tstep, 4),
        "ground_elev_min_m": round(tmin, 1),
        "intensity_median": round(int_med, 1),
        "multi_return_pct": round(100 * float(multi[valid].mean()), 2),
        "veg_from_returns": int((valid & (multi > 0.35) & (height > 2)).sum()),
    }
    print(json.dumps(stats, indent=1))
    if args.meta:
        json.dump(stats, open(args.meta, "w"), indent=1)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
