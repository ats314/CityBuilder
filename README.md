# NOCTIS-7 — a walkable ASCII megacity

One HTML file. No images, no fonts, no libraries, no build step. Open it and
walk around a procedurally generated cyberpunk city that is drawn entirely with
text characters.

**Play:** <https://ats314.github.io/CityBuilder/> — or open `index.html`
locally. There is no server, no build and nothing to install; the file is the
whole program.

```
WASD / arrows  walk          SHIFT  run            F  drone mode
mouse          look          V      switch alphabet [ ]  glyph size
C  copy the current frame to the clipboard as literal text
G  save PNG   R  new city   M  sound   B  bloom   L  reflections   N  rain
P  hide HUD   H  help
O  open source files   U  walk this page's own source
   …or drop a file or folder anywhere on the page
```

Touch devices get a virtual stick, a look-drag zone, and RUN / FLY / SET
buttons. Gamepads work too. `#s=<hex>` in the URL pins the city seed, so a
city you like has a shareable address.

`docs/MASTER.md` says how this fits with the other four repositories: it is a
*target* for a compiled World Seed, and the only one that is not a 3D engine.
**AMSTERDAM CANAL BELT** is the first city to arrive that way: a real 400 m
block of the Amstel bend, 23 pts/m² of CC0 airborne lidar compiled to a 285 KB
seed and baked to a 39 KB texture, with no rendering code changed.

---

## How it works

Four GPU passes. The important idea is that **the raycast runs once per
character cell, not once per pixel** — a 1280x720 window is only ~10,000 rays,
which is why this is cheap enough to run on a phone.

```
1  SCENE      → at cell resolution (x sub-sampling, see below)
                3-D DDA raycast through a height field, one ray per cell.
                MRT out: (lit colour, emissive mask) and (ink, kind, depth).

2  BLOOM      → separable blur of the emissive-masked colour, half res.

3  GLYPH      → per cell, choose which character to print.
                Ramp alphabets: Sobel over ink+depth picks a line glyph on
                silhouettes, otherwise a per-material luminance ramp.
                Sub-cell alphabets: threshold the SXxSY sub-samples against a
                Bayer matrix and print the glyph whose dots match.
                Out: (foreground colour, glyph index).

4  COMPOSITE  → at full device resolution. One glyph-atlas lookup per pixel,
                plus bloom, scanlines, chromatic aberration, vignette.
```

The glyph atlas is rasterised at runtime from the system monospace font into a
canvas at exactly the current cell size, so glyphs stay crisp at any DPI and
the file stays asset-free.

### The world

A torus of grid cells baked into one RGBA8 texture — 256 KB for an endless
city. Generated and source cities are 256 cells across; measured ones are 512,
so a real place gets 2 km instead of 1 km at the same detail. The size is a
uniform rather than a compile-time constant, so both load into one shader:

| channel | meaning |
|---|---|
| `R` | height in 1.6-unit steps (0 = ground) |
| `G` | low nibble palette index, high nibble facade style |
| `B` | lit-window density |
| `A` | flag bits: road / road-axis / park / water / sign strip / roof beacon / plaza |

Generation, all deterministic from the seed: a torus-wrapped Voronoi diagram
assigns districts (each with its own height profile, palette family, window
density and signage rate); an irregular street grid of alleys, streets and
boulevards is cut through it; a canal is carved with bridges where roads cross;
the remaining blocks are BSP-subdivided into lots; each lot gets one building,
a park, or a plaza. A megaspire is planted at the centre. The spawn point is
chosen by scoring every road cell near the centre for carriageway width and
clear run length, then standing you mid-carriageway facing down the longest
view.

Nothing is stored per building — 1,900-odd buildings are a side effect of the
rules. The window pattern, signage, traffic and rooftop beacons are evaluated
analytically in the shader from world position, so they cost no memory at all.

### Ink is a separate channel from colour

The first version derived the character from pixel brightness, which is the
obvious approach and it looks wrong: dark blue concrete is dim, so towers came
out as black holes with windows floating in them.

So the scene pass emits **ink** — how densely a material should be typed —
independently of how bright it is. Concrete is dark navy but typed at ~0.35
density, so a tower reads as a solid mass of characters that happens to be
dark. Lit windows are typed at ~0.9. Distance multiplies ink, so the far city
thins out into blank paper rather than fading to grey. Art-directing coverage
and colour separately is what makes it look like the medium instead of like a
photo run through a filter.

---

## The alphabet switch (press `V`)

The same scene, printed five ways. This is the interesting part of the demo,
because it makes a normally invisible trade-off visible:

| alphabet | cell sampling | what it's good at |
|---|---|---|
| **ASCII** | 1x1 | *Legibility as material.* `X` reads as girder, `0` as window, `\|` as edge. Lowest fidelity, most character. |
| **TELETEXT** | 1x1 | Block-and-shade ramp (`░▒▓█`). Chunky, poster-like, very readable at a glance. |
| **QUADRANT** | 2x2 | 4x the spatial resolution for the same one-character cost, using `▘▝▀▖▌▞▛…`. |
| **BRAILLE** | 2x4 | 8x the resolution — `⠁⠂⠄⡀…` gives 256 dot patterns per cell. Highest fidelity, and it stops looking like text. |
| **HYBRID** | 2x4 | Braille and quadrant in one alphabet: dots for texture, solid blocks for the cells that are vertically uniform. |

Sub-cell alphabets render the scene at `cols*SX x rows*SY` and dither the
sub-samples through a 4x4 Bayer matrix, so the dots carry shape while the cell
colour carries tone. Unsupported glyphs are detected at startup by rasterising
a probe character and counting ink, so an alphabet the platform can't draw is
skipped instead of printing tofu.

Fidelity and legibility pull against each other. Braille is objectively the
better image and ASCII is the better *picture* — you lose the semantic reading
of a glyph the moment the glyph is only a dot pattern.

### HYBRID — braille and quadrant

Two of the sub-cell alphabets fail in exactly complementary ways:

| | strength | weakness |
|---|---|---|
| QUADRANT | solid mass and saturated colour | 4 sub-cells, so detail blocks up |
| BRAILLE | 8 sub-cells of fine detail | washes into grey haze, no solidity |

They combine where others don't, because they are **the same kind of mark at two
granularities**. Both say *which sub-cells are lit*, and a quadrant is exactly a
2x2 pooling of braille's 2x4 — they nest. Switching between them changes the
grain, not the medium.

So HYBRID never chooses a family, and has no renderer of its own: it is the
BRAILLE path unmodified, down to the dither, with one glyph table swapped in.
Of the 256 dot masks, **16 have both rows of each half agreeing**. Those
describe a cell that is vertically uniform, so they are drawn as the solid block
with the same silhouette rather than as four loose dots:

```
00001111 -> ▀      01010101 -> ▌      11111111 -> █      01100011 -> ⡩
```

A cell that is flat, saturated or empty gains mass; a cell with texture in it
keeps braille's resolution. Nothing is tested against a threshold, so nothing
flickers as the camera moves — which is what sinks the obvious implementation,
where per-cell contrast picks a family and every cell crossing the cutoff swaps
its entire mark type.

It also costs no glyph budget. The index travels through the cell buffer as
**one byte**, and 240 dot patterns plus 16 blocks is exactly 256 — so braille
keeps all four of its rows here rather than being cut to a 2x3 subset to make
room.

Measured on Midtown at 183x62, ~17% of cells land on blocks. Forcing more of
them — by holding the dither constant within a row pair, so rows agree far more
often — reaches 80% and simply reproduces QUADRANT's mid-distance mush.

## Press `C`

Copies the current frame to the clipboard as literal text — the glyph index is
read straight back out of the GPU cell buffer, so what you paste is exactly
what is on screen. It is genuinely characters all the way down.

---

## SOURCE CITY — drop a file on it and walk that

The renderer doesn't care where the height field came from, so text can be the
input too. Drop a source file or a folder anywhere on the page (or press `O`,
or `U` to read this page's own source, or load `#repo=owner/name` for a public
GitHub repository) and the city becomes a rendering of that code.

| in the text | in the city |
|---|---|
| top-level block (function, class, CSS rule, markdown section) | one building |
| lines in it | **height** — your longest block is the megaspire |
| max indent depth | footprint |
| comment ratio | lit windows — and above 30%, the facade becomes a vertical garden |
| hash of its name | palette, so the same block is the same colour every time |
| exported / public | a neon strip up the facade |
| over 80 lines | a red aircraft beacon on the roof |
| file | district |
| **blank lines between blocks** | **the width of the street between them** |
| the block's name | the sign running up its wall |

Blocks are placed in source order, left to right and top to bottom, so walking
east along a row is reading down the file. Parsing is deliberately
language-agnostic — a new block starts on an unindented line that follows blank
space, which is what a function, a class, a CSS rule and a markdown heading all
look like from the outside. It works on JS, Python, GDScript, C, CSS, Markdown
and most things shaped like them.

The HUD names whatever you are looking at: point at a tower and it reads back
`shadeCore · 74 lines · depth 6 · 12% comment · index.html`.

Signage is rasterised into a label atlas — one row per block name — and the
scene shader runs it up the wall at a fixed 4.6 world units per letter. Letters
are therefore only a few character cells tall, which means **you often have to
switch to braille (`V`) to actually read a sign**. That is the fidelity
trade-off from the alphabet table, made load-bearing.

### The self-portrait

Press `U` on the hosted page and NOCTIS-7 reads its own source. You get 84
blocks and ~1,900 lines: a street made of its own blank lines, `frame` at 100
lines, `cityFromSources` at 115, `makeCity` at 152 — and the megaspire visible
from anywhere in the city turns out to be the 210-line block of shading code
that draws it.

---

## MEASURED CITY — real places from lidar (press `K`, again to switch)

A third input to the same unchanged middle layer. A seed gives a plausible
city; source code gives a legible one; a laser survey gives an actual one.

The data is [USGS 3DEP](https://registry.opendata.aws/usgs-lidar/) — public
domain, 4,755,025,996 points over New York City, served as Entwine Point Tiles
from AWS Open Data. `tools/bake_lidar_city.py` walks the octree over a chosen
extent, decodes the LAZ, and reduces it with one subtraction:

```
DSM (highest return per cell) - DTM (ground surface) = height above ground
```

which is exactly the height field the renderer already consumes. 18.4 million
sampled points over 2048 m of Midtown collapse to a 600 KB image. **The points
never reach the browser.**

The tiles are 512 cells across rather than 256, which doubles the extent to
2048 m at the same 4 m detail. That costs no raycast work — a ray crosses the
same number of world cells either way, because the cell is still 4 m — only
texture memory and download. What it buys is the difference between a tile and
a place: at 1024 m Midtown held no landmark and no water, and the wrap came up
every thirty seconds of walking.

Sanity against the real place: Manhattan's tallest cell reads 400 m, and San
Francisco's 330 m against Salesforce Tower's real 326 m.

Two things worth knowing about the data:

- **NYC 3DEP is essentially unclassified** — 2.3 M unclassified points against
  zero tagged as buildings. So buildings are derived from geometry, not from
  the classification channel. What classification cannot do here is reject
  noise, so a despike pass replaces any cell more than 45 m above its
  neighbours' median; real towers are many contiguous cells, a bird is one.
- **The grid is stored as a fully opaque PNG of stacked planes** — 512 wide by
  512 per plane, grid on top, then flags, terrain and the model — rather than
  packing flags into alpha. Canvas backing stores are premultiplied, so a small
  data byte in the alpha channel silently destroys RGB. Verified byte-exact on
  round-trip. Planes 2-4 use only their red channel, which looks wasteful;
  repacking them into one RGB plane was measured at 592 KB -> 543 KB, so the
  waste is 8% and the simpler layout was kept.

Colour carries height rather than appearance. **An earlier version of this file
said the dataset has RGB. It does not.** LAS point format 3 reserves red, green
and blue fields and the NYC survey leaves all three at zero — checked across
sampled tiles, `nonzero = 0.00%`. San Francisco is point format 1 and has no
colour fields at all. Nothing in either survey knows what anything looks like,
which is why the material channels below had to be measured rather than read.

### Terrain

The first version computed `DSM - DTM` and then threw the DTM away. On Manhattan
that costs nothing — 19 m of relief across the whole tile. On San Francisco it
flattens the thing that makes the place itself.

So the bake now carries a third band, the ground surface, and a cell is a solid
column from its terrain up to `terrain + height` rather than from zero up. The
raycaster reads a ground elevation per cell; facade coordinates are measured
relative to that ground so window rows line up per building instead of by
absolute elevation; the side of an unbuilt column is an exposed hillside rather
than a wall; and the player walks the terrain, eased so kerbs and hill crests do
not snap the camera. Generated cities carry a zero terrain plane, which makes
the terrain path collapse back to the flat one exactly.

Two cities ship, both real, both 2048 m across:

| | relief | tallest | water | vegetation | points sampled |
|---|---|---|---|---|---|
| Midtown Manhattan | 30 m | 400 m | 13,560 cells | 16,923 | 18.4 M |
| San Francisco | **102 m** | 330 m | **77,343 cells** | 28,174 | 442 M |

Colour still carries height, but ranked *within the tile* rather than against an
absolute scale — a fixed mapping means a low-rise city only ever uses the dark
end of the ramp, so San Francisco came out muddy while Manhattan glowed.

Limits: the tile is 2048 m and the world wraps, so walking far enough still
repeats it.

### Water, and why the label was not enough

Both cities used to ship with exactly zero water cells, for two reasons that
had to be fixed in order.

The first was that **the extents contained no water to find.** The old 1024 m
Midtown tile was entirely inland — the Hudson is 1.5 km outside it — so "no
water in these extents" was true rather than broken. Doubling the extent and
shifting Midtown 700 m east reaches the East River; San Francisco's already
touched the Bay and now contains 1.4 km of it.

The second was that the bake believed the classification channel. LAS class 9
is the documented answer for water, and over the East River this survey tags
4,204 cells of a body that is really 13,461 — it can only tag cells that
returned something, and a river is mostly cells that did not.

So water is measured off the beam, the way vegetation already was. Three
channels were tested against the cells the survey *did* tag:

| channel | water | other flat ground | verdict |
|---|---|---|---|
| points/cell | 15 | 46 | real, but only 70% recall |
| intensity | 69 | 43 | real, and **inverted** |
| multi-return | 0.00 | 0.00 | nothing |

Near-infrared is absorbed by water, so the expectation was a dropout and
darkness. Density falls only threefold rather than to nothing, and intensity
goes *up*: a flat surface returns specularly, so the few beams that come back
come back hard. One prediction had the wrong magnitude and the other had the
wrong sign, which is a good argument for measuring before choosing a threshold.

What actually identifies water is that **its top surface is a plane.** The East
River's DSM spans 0.20 m across 13,461 contiguous cells. No land does that — the
next candidate down, a pier at the same elevation, spans 1.34 m and returns
points *more* densely than ordinary ground. So the rule is waterline plus
flatness plus size, and density is reported as corroboration rather than gated
on. San Francisco Bay comes out at 0.13 m spread over 71,127 cells.

One consequence worth knowing: the ground-surface fill propagates outward from
cells that have ground returns, and open water has none. Twenty-four blur passes
reach about twenty-four cells, so mid-channel cells kept the tile median — 14.6
metres, identical to the streets. Left alone the East River rendered as a lagoon
perched at street level. Each body is now levelled onto its measured surface,
which puts the river 19 m below Midtown where it belongs.

Bake a different place:

```bash
python3 tools/bake_lidar_city.py --lat 37.7940 --lon -122.3960 \
    --dataset CA_SanFrancisco_1_B23 --depth 10 --out sf.png
```

2,277 datasets are available; the index lives in the `usgs-lidar` boundaries
GeoJSON.

### Readouts — press `X` (or the VIEW button)

Colour normally encodes height. It can encode something else measured about the
same cell instead, which turns the city into a chart you are standing inside
without it ceasing to be a place.

| readout | what colour means |
|---|---|
| **HEIGHT** | height, ranked within the tile |
| **RESIDUAL** | measured height minus what access and activity predict. **Red = shorter than the economics alone would put here. Blue = taller.** |
| **ACTIVITY** | the point-of-interest potential the prediction was built from |
| **TERRAIN** | ground elevation, which the height field normally hides |

The residual is the interesting one. Over Midtown, 111,990 cells come out
shorter than predicted and 59,360 taller. Where it runs red, something that is not
economics is holding the building down — a zoning envelope, a landmark
designation, air rights already sold, a lot nobody could assemble. See
`docs/CITY_MODEL_FINDINGS.md` for how far that model can and cannot be trusted
(Manhattan R² = 0.328 at 8 km and **negative** transferred to 2 km; San
Francisco 0.114 and +0.047, for reasons the findings doc is careful not to
overclaim).

### Vegetation, from the beam and not from a guess

Earlier bakes reported zero vegetation because this dataset barely uses the
classification channel. Return structure works instead: a beam that comes back
twice went through something, and on a roof the only thing it goes through is
foliage. Measured over Midtown with silhouette edges excluded, streets return
multiple echoes 0.7% of the time and roofs 5-16%.

Two other features were measured and **rejected**, which is worth recording:

- **roughness** — street 0.41 vs roofs 0.47-0.50. No signal. A roof is flat and
  4 m cells are larger than roof texture.
- **intensity** — street 46 vs roofs 60-70, 0.48 sigma. Real but weak; it only
  nudges facade style, it does not decide it.

Manhattan finds 3,499 vegetated cells and San Francisco 7,922.

---

MIT licensed, like everything outside `third_party/`.
