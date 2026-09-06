# NOCTIS-7's place in the master idea

Internal. This repository is one of five. The other four are `lidargame-`
(lidarworld, the compiler), `Kalasatama` (Helsinki photogrammetry),
`GODOT-GAME` and `react-native-game-engine`. The full note lives in
`lidargame-/docs/MASTER.md`; this is the part that concerns NOCTIS-7.

## The idea, in one paragraph

Measured reality is compiled once, into a small theme-independent and
engine-independent description called a **World Seed**. Renderers and engines
are targets that expand it. Nothing crosses a repository boundary except the
seed file — partly because it is the clean architecture, and partly because it
is the only join that is legally clean: this repository is MIT and the compiler
is proprietary, so vendoring one into the other breaks something either way.

## Why this repository matters more than it looks

NOCTIS-7 is the only target in the estate that is **not a 3D engine**. No
triangles, no meshes, no materials, no scene graph — a world arrives as four
bytes per 4 m cell and gets raycast once per character cell.

That is what makes it useful beyond being a nice thing to walk around in. The
compiler's other backends emit glTF, CityJSON and a web bundle: three ways of
writing down the same triangles. Between them they cannot tell you whether the
intermediate representation is genuinely engine-independent or merely
glTF-shaped. This one can, because there is nothing here for a glTF-shaped
assumption to hide in. If a seed drives an ASCII raycaster as well as it drives
a mesh exporter, the claim holds.

So the invariant survives the whole way down: the renderer picks its own
palette, its own glyph and its own window pattern from a style index and a
height byte. No material name is ever transmitted.

## The city texture is now a published format

It has a second producer. `lidarworld` grew a `noctis` backend that writes
exactly this layout, so what was an internal encoding between
`tools/bake_lidar_city.py` and `cityFromLidar()` is now an interface. Written
down here so it can be depended on.

**An N-wide, 3N-tall, fully opaque RGBA8 PNG.** Three stacked planes, row-major,
cell `i = y * N + x`, with `y` increasing northward (so image row 0 is the
southern edge).

| rows | channel | meaning |
|---|---|---|
| `0 .. N` | `R` | height above ground, in 1.6 m steps (0 = ground) |
| | `G` | low nibble palette index, high nibble facade style |
| | `B` | lit-window density |
| `N .. 2N` | `R` | flag bits (below) |
| `2N .. 3N` | `R` | terrain elevation, in `terrStep` metre steps above `groundMinZ` |
| `3N .. 4N` | `R`,`B` | optional model plane (residual / predicted), 4-band bakes only |

Flag bits: `1` road, `2` road-axis-X, `4` road-axis-Z, `8` park, `16` water,
`32` sign strip, `64` roof beacon, `128` plaza.

Two rules that are not obvious and cost real time when broken:

- **Alpha must be 255 everywhere.** Canvas backing stores are premultiplied, so
  a data byte in the alpha channel silently destroys RGB wherever it is small —
  and flag bits are mostly small. This is why the planes stack vertically
  instead of using the fourth channel.
- **The image declares its own size.** `N = img.width`, `bands = height / N`.
  A 512-cell 2 km bake and a 256-cell 1 km bake both load with nothing to
  configure. Do not hard-code either.

The accompanying meta carries what the pixels cannot: `name`, `credit`,
`cellM`, `extentM`, `terrStep`, `relief`, `grid`. `terrStep` is required for any
bake with a terrain plane — without it, relief is unitless.

## Getting a compiled place in here

```bash
# in lidargame-
lidarworld fetch amsterdam_grachtengordel -o data/real
lidarworld compile data/real --area x,y,size -o build/ams \
    --footprints amsterdam --streets amsterdam --seed
lidarworld noctis build/ams/ams.seed.json -o build/ams/city.png \
    --meta build/ams/city.json
```

Then add an entry to `LIDAR_CITIES` in `index.html` with the PNG as a data URI
and the fields from `city.json`. `cellM` defaults to 4 m in the baker because
the renderer's world cell is a fixed unit: bake the same building at 1 m and it
comes out four times smaller standing next to a 3DEP city.

## What this repository owes the master idea

**`tools/bake_lidar_city.py` should become a thin wrapper over that pipeline.**
Right now it reads 3DEP from S3 itself and reimplements ingest, ground
modelling, height-above-ground, vegetation discrimination and classification —
all of which the compiler already does, and does with tests and forward
validation against the returns. More importantly it is the *second* place in
the estate where "what is a building" and "where is the ground" get decided.
Two answers to those questions is one too many, and the two paths will drift
until the ASCII city and the glTF city disagree about the same block.

The height-model work (`bake_city_model.py`, `docs/CITY_MODEL_FINDINGS.md`) is
the opposite case and should stay here: it is a property of the *renderer's*
readouts, not of the compiler, and its honest result — a fit of R² = 0.328 at
8.2 km transferring to **R² = −0.065** on a 2 km tile, worse than predicting
the mean — belongs next to the thing that displays it.

## What stays exactly as it is

The single file, no build step, no assets, no dependencies. That constraint is
why this target is worth having: it is the cheapest possible proof that the
compiler's output is not secretly shaped like one engine. Anything that
introduces a bundler here costs more than it buys.
