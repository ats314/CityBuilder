# CLAUDE.md

Guidance for agents working in this repo.

## What this is

A walkable cyberpunk city rendered entirely as coloured terminal glyphs, in
**one file with no build step**: `index.html`. There is no package manager, no
bundler, no framework. Open the file and it runs. The `tools/` scripts are
offline bakers that produce data which is then embedded *into* that file as
base64; they are not part of the runtime.

Live: https://ats314.github.io/CityBuilder/ (GitHub Pages, served from `main`).
The `BUILD` constant near the top of the script is shown in the HUD — bump it
when deploying so it is obvious which build is live.

## Commands

```bash
# verify the renderer actually draws (the only real test here)
pip install -r requirements-dev.txt
python3 tools/smoke_test.py                    # exits non-zero on failure
python3 tools/smoke_test.py --shots /tmp/shots # also write a PNG per alphabet
python3 tools/smoke_test.py --page some.html   # test a modified copy

# bake a city tile (slow, network-heavy — see "Baking" below)
pip install -r requirements.txt
python3 tools/bake_lidar_city.py --lat 40.7530 --lon -73.9770 --grid 512 \
    --depth 11 --out mh.png --dump mh_fields.npz
```

`SMOKE_CHROME=/path/to/chrome` overrides the browser if one is preinstalled.

Validate the workflow file before pushing it — an invalid one fails the run
*instantly*, with no job and no log to read, which looks like Actions being
switched off rather than like a syntax error:

```bash
python3 -c "import yaml;yaml.safe_load(open('.github/workflows/ci.yml'))"
```

## Verify visual claims by rendering them

This is the most important habit in this repo, and it is not optional. The
output is an image, and **eyeballing it misleads**. A recent change to the
HYBRID alphabet looked "too dotty" by eye; a false-colour debug build showed it
was in fact 85% solid blocks — the opposite of the visual read. The fix was
chosen from a measured sweep, not from an opinion.

So: when you change anything that affects pixels, render it with
`tools/smoke_test.py --shots`, look at the PNG, and where the claim is
quantitative, measure it — a throwaway debug build that false-colours the
decision you are making is cheap and settles the question.

## Architecture

Three inputs feed **one unchanged renderer**:

1. a seed → a procedurally generated city
2. dropped source code → a city whose massing encodes the files
3. a baked lidar tile → a real place

They all produce the same thing: an `N x N` grid of cells, four bytes each.

### The cell contract

| byte | carries |
|---|---|
| R | height (building height / `HSTEP`, 0 = not a building) |
| G | `palette \| (facade_style << 4)` — two nibbles |
| B | window density |
| A | flag bits: `ROAD 1, ROADX 2, ROADZ 4, PARK 8, WATER 16, SIGNSTRIP 32, BEACON 64, PLAZA 128` |

### The tile PNG format — read this before touching a baker

A tile is **`N` wide by `N * bands` tall**, stacked vertically, and every pixel
is fully opaque:

| band | contents |
|---|---|
| 0 | R height, G palette\|style, B density |
| 1 | R flag bits |
| 2 | R terrain elevation |
| 3 | model plane (see `bake_city_model.py`) |

Two traps live here:

- **Flags are in band 1's red channel, not in alpha.** Canvas backing stores are
  premultiplied, so a data byte in the alpha channel silently destroys RGB
  wherever alpha is small — and flag bits are mostly small. Never move them back.
- **`bake_lidar_city.py` writes 3 bands; the shipped tiles have 4.** The fourth
  is appended afterwards by `bake_city_model.py`. Running the lidar baker alone
  does not reproduce a shipped tile, and the renderer reads `bands >= 4` to
  decide whether a model plane exists.

### The glyph index is one byte

Alphabets are tables of glyphs indexed by a single byte through the cell buffer,
so **no alphabet may exceed 256 glyphs**. This is the constraint that shapes
HYBRID: it is braille's 256 dot masks with the 16 row-paired ones (both rows of
each half agreeing) swapped for the solid block of the same silhouette. 240 + 16
= 256 exactly. `tools/smoke_test.py` asserts both counts.

HYBRID has no renderer of its own — it is `mode:'bits'` with `sub:[2,4]`, the
BRAILLE path unmodified down to the dither, with only the glyph table swapped.

## Baking

Data is USGS 3DEP lidar via the public AWS EPT store, plus Overture Maps for
road geometry. Both are read over plain HTTP range requests; there is no SDK
dependency.

Bakes are **expensive**: San Francisco is ~458M points and takes ~40 minutes.
Always pass `--dump` — it writes the per-cell fields to an `.npz` so a
classifier can be retuned without re-fetching the tile.

Dataset names are EPT prefixes, e.g. `NY_NewYorkCity`,
`CA_SanFrancisco_1_B23`. Node counts do not change past the tree's real depth,
so a larger `--depth` is free but does nothing once saturated.

## Known open issues

- **The San Francisco tile is stale.** It carries facade styles `0, 1, 4, 7`,
  and the current baker can only emit `0, 1, 2, 3, 7` — style 4 is unreachable.
  Its facade re-bake never landed, so the two cities are inconsistent.
- **The road classifier does not transfer to San Francisco.** It is fitted on
  Manhattan and has never been scored on SF. On the shipped SF tile it calls
  99.3% of ground "carriageway" (35,399 road vs 242 other paved), against 84% on
  Midtown and a 76.9% ground-truth base rate. Treat it as collapsed, not merely
  unverified. See `docs/SURFACE_CLASSIFICATION.md`.

## Conventions

- Comments explain *why*, especially where a subtlety already cost someone a
  day. Match that density; do not strip them.
- `docs/` records what was measured, **including the wrong turns**, because
  knowing what was already tried and failed is the point.
- Quote AUC and average precision for the classifier, never accuracy — the base
  rate is 76.9%, so "always say road" already scores 0.769.
- `.gitignore` excludes `*.png` and `*.npz`: bake outputs are intermediates, and
  the tiles that ship are base64 inside `index.html`. Use `git add -f` if you
  ever genuinely need to commit one.
