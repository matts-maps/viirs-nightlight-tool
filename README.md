# VIIRS Nightlight Tool

A reusable command-line tool that extracts a VIIRS nighttime-lights radiance
time series for any area of interest — a country, a sub-national unit, or a
custom boundary you supply — over any date range and at daily, weekly,
monthly, or annual frequency.

Built to be run by anyone, on any machine: it's plain Python with open-source
dependencies, no ArcGIS/arcpy requirement, and no hardcoded paths. If you'd
rather be walked through the options than remember flags, run it with no
arguments (or add `--wizard`) for an interactive prompt — see
[Interactive mode](#interactive-mode-wizard) below.

## What it does

Given an AOI, a start/end date, and a frequency, it queries
[Google Earth Engine](https://earthengine.google.com/) for VIIRS Day/Night
Band radiance and writes one row per period to a CSV, with QA columns so you
can judge how much to trust each value — plus an optional chart.

| Column | Meaning |
|---|---|
| `period` | e.g. `2022-03` (monthly), `2022` (annual), `2022-03-14` (daily) |
| `mean_radiance`, `sum_radiance`, `median_radiance` | zonal stats over the AOI, in the source product's native radiance units (nW·cm⁻²·sr⁻¹) |
| `valid_pixel_count` | number of AOI pixels that passed quality masking and contributed to the stats |
| `scene_count` | number of satellite images available for that period |
| `qa_flag` | `ok`, `no_data`, or `low_valid_pixels` — check this before trusting a row |

## 1. Install

Requires Python 3.9+.

```bash
git clone <this-repo-url>
cd viirs-nightlight-tool
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Authenticate with Google Earth Engine (one-time, per user)

The tool queries Earth Engine directly (no downloads of huge global rasters),
which needs a free Google account registered for Earth Engine access:

1. Sign up at https://code.earthengine.google.com/register if you haven't
   used Earth Engine before (free, usually approved instantly for
   non-commercial use).
2. Run:
   ```bash
   earthengine authenticate
   ```
   This opens a browser window to sign in and stores a credential on your
   machine — you only need to do this once.
3. If your Google Cloud setup requires a project ID for Earth Engine (newer
   accounts sometimes do), pass it with `--ee-project your-project-id` on
   every run, or set it as the default with
   `earthengine set_project your-project-id`.

## 3. Run it

```bash
python nightlight_tool.py \
    --aoi-file sample_aoi/toy_bbox.geojson \
    --start 2021-01-01 --end 2023-01-01 \
    --freq monthly \
    --out out/toy_area_nightlights.csv \
    --chart
```

### Interactive mode (`--wizard`)

If you don't want to look up flag names, run the tool with no arguments at
all (or add `--wizard` to any invocation) and it will prompt you through each
choice — AOI, granularity, attributes, date range, frequency, and output path
— then run exactly as if you'd typed the equivalent flags:

```bash
python nightlight_tool.py
# or
python nightlight_tool.py --wizard
```

This is the same code path as the flag-based CLI underneath — the wizard just
builds the flags for you, so anything documented below applies either way.

When you choose to break down by admin unit, the wizard looks up and prints
the actual fields available before asking which to include as
`--attributes` columns — the columns in your file for `--aoi-file`, or the
GAUL properties (e.g. `ADM0_NAME`, `ADM1_CODE`) for `--aoi-name` (this needs
Earth Engine set up already, per step 2 above, since it queries GAUL live).

Or by admin-unit name instead of a boundary file (looked up against
Earth Engine's FAO GAUL admin boundaries, country → admin-1 → admin-2):

```bash
python nightlight_tool.py --aoi-name "Ukraine" \
    --start 2021-01-01 --end 2026-09-01 --freq annual --out ukraine_annual.csv
```

Or by ISO 3166-1 alpha-3 country code, which sidesteps having to know GAUL's
exact country-name spelling:

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2021-01-01 --end 2026-09-01 --freq annual --out ukraine_annual.csv
```

`--aoi-iso3` resolves the code to a country name and matches it against GAUL
automatically (GAUL boundaries don't carry ISO codes themselves) — it prints
which GAUL country name it matched to, so you can confirm it got the right
one. If no confident match is found (rare, but possible for a country whose
GAUL name is unusual), it errors out and tells you to use `--aoi-name` with
the exact GAUL name instead.

### Arguments

| Flag | Required | Notes |
|---|---|---|
| `--aoi-file` | one of `--aoi-file` / `--aoi-name` / `--aoi-iso3` | path to a GeoJSON/shapefile/etc. — for a specific sub-unit (e.g. one raion), get a proper boundary from a source like [fieldmaps.io](https://fieldmaps.io), [HDX COD](https://data.humdata.org/), or [GADM](https://gadm.org) |
| `--aoi-name` | — | admin name to look up, e.g. `"Ukraine"`, or an admin-1/2 name |
| `--aoi-iso3` | — | ISO 3166-1 alpha-3 country code, e.g. `UKR` — resolved to a GAUL country name automatically |
| `--start`, `--end` | yes | ISO dates, `--end` is exclusive |
| `--freq` | yes | `daily`, `weekly`, `monthly`, or `annual` |
| `--out` | yes | output CSV path |
| `--chart` | no | also write a PNG chart next to the CSV — one line chart for a single AOI, or a small-multiples grid (one mini chart per unit) with `--breakdown` (see below) |
| `--chart-units` | no | comma-separated exact `unit_name` values to chart, when using `--chart` with `--breakdown` (see below) |
| `--ee-project` | no | your Earth Engine cloud project ID, if required (see step 2) |
| `--breakdown` | no | `admin1` or `admin2` — output one row per sub-unit per period instead of one row per period (see below) |
| `--unit-name-field` | no | required alongside `--breakdown` when using `--aoi-file` (see below) |
| `--unit-id-field` | no | column in `--aoi-file` holding each unit's unique ID, typically a pcode (see below) |
| `--attributes` | no | comma-separated field/column names to include as extra columns in `--breakdown` output (see below) |
| `--simplify-tolerance` | no | simplify `--aoi-file` geometries by this many degrees before sending to Earth Engine — only relevant to `--breakdown --aoi-file` with a detailed layer (see below) |
| `--wizard` | no | run the interactive prompt instead of using flags (also runs automatically with no arguments) |

### Breaking a country down by admin unit

Instead of one AOI-wide number per period, `--breakdown` gives you one row per
admin unit per period — e.g. every governorate or district in a country, each
with its own radiance trend:

```bash
python nightlight_tool.py --aoi-name "Yemen" \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out yemen_by_governorate.csv --breakdown admin1 --ee-project ee-masims
```

This looks up FAO GAUL admin1 (governorate/oblast-level) or admin2
(district/raion-level) units within that country, and queries all of them for
each period in a single Earth Engine call (not one call per unit — that
matters once you're at admin2 scale, which can be hundreds of units).

Output columns add `unit_name`, `unit_id`, `admin0_name`, `admin1_name`,
`admin2_name` (blank where not applicable) alongside the usual radiance/QA
columns — so you can pivot or join straight into a spreadsheet or GIS.
`unit_id` is filled in automatically here from GAUL's own `ADM1_CODE`/
`ADM2_CODE`, so it's always populated with `--aoi-name`. `--chart` in this mode
writes a small-multiples PNG (one mini chart per unit) instead of a single
shared chart, since one line per district isn't legible once there are more
than a handful:

```bash
python nightlight_tool.py --aoi-name "Yemen" --breakdown admin1 \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out yemen_by_governorate.csv --chart --ee-project ee-masims
```

If there are more than 30 units, `--chart` charts the first 30 and warns you
— use `--chart-units` to pick specific ones instead (exact `unit_name`
values, comma-separated):

```bash
python nightlight_tool.py --aoi-name "Yemen" --breakdown admin1 \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out yemen_by_governorate.csv --chart --chart-units "Sana'a,Aden,Ta'izz" \
    --ee-project ee-masims
```

(A deliberate `--chart-units` selection is never truncated, however many you
list — the 30-panel cap only applies to the "chart everything" default.)

You can also break down a boundary file you supply yourself instead of a GAUL
lookup, by adding `--unit-name-field` to say which column/property in the file
holds each unit's name:

```bash
python nightlight_tool.py --aoi-file crimea_raions.geojson --unit-name-field raion_name \
    --start 2021-01-01 --end 2023-01-01 --freq monthly \
    --out crimea_by_raion.csv --breakdown admin2 --ee-project ee-masims
```

(Here `--breakdown admin2` is just a label for the output — with `--aoi-file`,
every feature in the file is kept separate regardless of which admin level
you name.)

#### Giving each unit a stable ID: `--unit-id-field`

`--unit-name-field` picks a human-readable label, but names aren't always
unique — two districts in different states can share a name, and even where
they don't, names get renamed/respelled in ways a stable code never does.
Whenever your boundary file has a unique-ID column (most admin boundary
sources — HDX COD, fieldmaps.io — ship one, usually called something like
`ADM2_PCODE`), point `--unit-id-field` at it:

```bash
python nightlight_tool.py --aoi-file crimea_raions.geojson \
    --unit-name-field raion_name --unit-id-field raion_pcode \
    --start 2021-01-01 --end 2023-01-01 --freq monthly \
    --out crimea_by_raion.csv --breakdown admin2 --ee-project ee-masims
```

This adds a `unit_id` column to the output alongside `unit_name`, so you can
join back onto other datasets (or your GIS layer) by the stable code rather
than a name that might not match exactly. It's optional but recommended
whenever names might collide; if you skip it, `unit_id` is just blank. With
`--aoi-name` (GAUL), you don't need this at all — `unit_id` is filled in for
you automatically from GAUL's `ADM1_CODE`/`ADM2_CODE`. The interactive
`--wizard` asks for this right after the unit-name column.

#### Choosing which attributes end up in the output: `--attributes`

By default, `--breakdown` output includes `admin0_name`/`admin1_name`/
`admin2_name` when using `--aoi-name` (GAUL), and just `unit_name` when using
`--aoi-file`. That's not always enough — e.g. Venezuela has several municipios
that share the same name across different states, so `unit_name` alone can't
tell them apart in the CSV.

`--attributes` lets you pick exactly which fields from the admin/boundary
data become columns instead:

```bash
# GAUL: use GAUL's own property names
python nightlight_tool.py --aoi-name "Venezuela" --breakdown admin2 \
    --attributes ADM0_NAME,ADM1_NAME \
    --start 2024-09-01 --end 2026-09-01 --freq monthly \
    --out venezuela_admin2.csv --ee-project ee-masims

# --aoi-file: use column names from your own file
python nightlight_tool.py --aoi-file ven_admin2.geojson --unit-name-field adm2_name \
    --attributes adm1_name,adm1_pcode --breakdown admin2 \
    --start 2024-09-01 --end 2026-09-01 --freq monthly \
    --out venezuela_admin2.csv --ee-project ee-masims --simplify-tolerance 0.001
```

When `--attributes` is given, it fully replaces the default columns — you get
exactly the fields you named (plus `unit_name`), so include whatever parent
name/code field disambiguates your units. With `--aoi-file`, an unknown
column name fails fast with the list of columns actually in your file; with
`--aoi-name`, an unrecognised GAUL property name just comes back blank rather
than erroring (GAUL's property names vary slightly by asset — check a sample
feature if a column you expect isn't showing up).

#### Large/detailed boundary files: `--simplify-tolerance`

Earth Engine rejects a client-side `FeatureCollection` (which is what
`--breakdown --aoi-file` builds from your file) once its payload exceeds
**10MB** — every polygon's full vertex list gets embedded in every API call
that touches it. A detailed admin2/raion-level file with a few hundred
units, or one digitized at high resolution, can blow past that limit even
though the file itself looks modest on disk. You'll see an error like:

```
googleapiclient.errors.HttpError: 400 ... "Request payload size exceeds the limit: 10485760 bytes."
```

If you hit this, add `--simplify-tolerance` to simplify each unit's geometry
before it's sent, e.g.:

```bash
python nightlight_tool.py --aoi-file ven_admin2.geojson --unit-name-field adm2_name \
    --start 2024-09-01 --end 2026-09-01 --freq monthly \
    --out venezuela_admin2.csv --breakdown admin2 --ee-project ee-masims \
    --simplify-tolerance 0.001
```

The value is in decimal degrees (roughly `0.001` ≈ 100m at the equator).
Since VIIRS itself only resolves to ~500m pixels, simplifying well below
that scale won't meaningfully change the zonal stats for reasonably-sized
units — start at `0.001` and increase it only if you still hit the payload
limit. Very small or thin units (e.g. a narrow coastal strip) could be
distorted more than larger ones at the same tolerance, so it's worth
sanity-checking a simplified layer's shape before trusting results for
tiny units.

### Choosing a frequency

- **monthly** (recommended default) uses NOAA's pre-composited, cloud-free
  monthly product (`VCMSLCFG`) — the most reliable option for a trend.
- **annual** averages the monthly composites over each calendar year.
- **weekly** mosaics NASA's gap-filled daily product (`VNP46A2`) over each
  ISO calendar week (Monday–Sunday) — there's no native VIIRS weekly
  composite, so this is built the same way `annual` is built from monthly
  data, just from the daily product instead. Useful for tracking short-term
  change (e.g. a blackout event) at a finer grain than monthly without the
  full noise and call-count cost of `daily`. A period that starts mid-week is
  snapped back to that week's Monday, the same way a mid-month `--start`
  snaps back to the 1st for `monthly`.
- **daily** uses NASA's gap-filled daily product (`VNP46A2`) with basic
  quality masking — more granular (useful for pinpointing a specific
  blackout night) but noisier, and much slower for long date ranges since
  it's one Earth Engine call per day.

The sample AOI in `sample_aoi/toy_bbox.geojson` is a rough illustrative box
around part of Crimea — good for confirming the tool works end-to-end, but
**not** an authoritative or political boundary. Use a real boundary source
for anything you'll actually report on.

## Testing without an Earth Engine account

The core logic (period construction, statistics, QA flagging, CSV writing)
is unit-tested offline, with no Earth Engine call or account needed:

```bash
python -m pytest tests/
# or, without pytest installed:
python tests/test_nightlight_tool.py
```

Run this after installing to confirm your Python environment is set up
correctly, before spending an Earth Engine call on a real AOI.

## Known limitations (v1)

- Time series only — no built-in change/anomaly detection against a
  baseline period yet (e.g. flagging a blackout as a % drop vs. a pre-war
  average). That's a natural v2 addition once the base tool is validated
  against real areas.
- The daily product's cloud/quality-flag bit layout in
  `daily_pixel_quality_mask()` reflects VNP46A2's documentation as of when
  this was written — NASA/NOAA have revised VIIRS product band layouts
  before, so if daily numbers look implausible, check the current VNP46A2
  User Guide's `QF_Cloud_Mask` bit table against the constants in
  `nightlight_tool.py`.
- GAUL admin boundaries (used for `--aoi-name` lookups) are a general-purpose
  reference dataset and may not reflect current or contested administrative
  boundaries precisely — for anything Crimea/Ukraine-specific, supply your
  own `--aoi-file` from a source you trust instead.
- GAUL's admin2 coverage is incomplete for some countries, **including
  Ukraine**: `--aoi-name "Ukraine" --breakdown admin2` returns one row per
  oblast (admin1) with `admin2_name` set to the literal placeholder
  `"Administrative unit not available"` and stats identical to the parent
  oblast — GAUL simply has no real raion-level (admin2) data for Ukraine to
  return. This is a data-source gap, not a bug in this tool. For genuine
  raion-level breakdowns, use `--aoi-file` with a real boundary source (e.g.
  [fieldmaps.io](https://fieldmaps.io) or [HDX COD](https://data.humdata.org/))
  and `--unit-name-field`, combined with `--simplify-tolerance` if needed
  (see above).
- Output is tabular (CSV) only, even in `--breakdown` mode — a
  `unit_name`/`admin1_name`/`admin2_name` column lets you join it back onto a
  boundary file yourself, but the tool doesn't write a joined
  shapefile/GeoJSON directly. See Roadmap below.

## Roadmap

Towards a tool anyone can pick up without reading this whole README first:

- **Fieldmaps.io/global-dataset AOI picker** — `--aoi-iso3` (see above) covers
  unambiguous country selection; a fuller country dropdown backed by
  fieldmaps.io and/or the latest GAUL release, plus admin1/2-level lookups
  by code rather than name, is the next step.
- **Geodatabase (`.gdb`) input** — `--aoi-file`/`--unit-name-field` currently
  read via `geopandas`, which supports `.gdb`, but this hasn't been tested or
  documented as a supported input format yet.
- **Baseline/change detection** — flag a period as a % drop vs. a
  user-defined baseline (e.g. pre-war average), for spotting likely
  blackouts/damage rather than just reading a trend line by eye.
- **Joined spatial output** — write the `--breakdown` results as a
  GeoJSON/shapefile with each unit's stats as attributes (geometry + data in
  one file), rather than a CSV the user joins onto a boundary file themselves.
- **Clipped raster export** — an `--export-clipped-raster` style flag that
  also writes the reduced VIIRS image for an AOI as a small GeoTIFF, for
  visual sanity-checking of the mask/clip in a GIS.
Already delivered towards the "anyone can use it" goal: `--wizard` interactive
mode (which also shows the actual admin-data fields available before you pick
`--attributes`), `weekly` frequency, `--attributes` for choosing output
columns, `--aoi-iso3` for unambiguous country selection, and small-multiples
`--chart` support in `--breakdown` mode (`--chart-units` to pick specific
units).
