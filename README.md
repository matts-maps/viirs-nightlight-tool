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
| `pct_dark` | percent of valid AOI pixels below `--dark-threshold` that period — a simple blackout/darkness indicator (see below) |
| `valid_pixel_count` | number of AOI pixels that passed quality masking and contributed to the stats |
| `scene_count` | number of satellite images available for that period |
| `qa_flag` | `ok`, `no_data`, or `low_valid_pixels` — check this before trusting a row |

Two more columns appear with `--include-yoy` (see [Year-over-year change](#year-over-year-change---include-yoy)):

| Column | Meaning |
|---|---|
| `<stat>_yoy_abs`, `<stat>_yoy_pct` | for `mean_radiance`/`sum_radiance`/`median_radiance`: this row vs. the same period one year back |

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
    --out-dir out --geoextent crm \
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

### GUI mode

Prefer forms and dropdowns to a terminal? `nightlight_gui.py` is a Tkinter
window with the same options as the wizard (AOI source, granularity,
attribute/unit-ID columns), organized into sections: a **Timeframe** section
(start/end date, frequency, dark-pixel threshold); an **Outputs** section —
a geoextent code (always shown; automatic from an ISO3 AOI unless you
override it, required otherwise), one shared output folder for everything,
and checkboxes for which outputs to produce (CSV is always on; Vector as
GeoJSON and/or Shapefile checkboxes, both untickable for no vector output;
Rasters as independent whole-AOI-radiance and year-over-year-diff
checkboxes, sharing one resolution field; Chart) — and a separate
**Earth Engine** section for the cloud project ID — plus a folder-browse
dialog and a live log panel instead of typed prompts:

```bash
python nightlight_gui.py
```

It needs the same environment as the CLI (steps 1–2 above), plus `tkinter`
itself, which ships with most Python installs but is a separate OS package on
some Linux distros:

```bash
sudo apt-get install python3-tk   # Debian/Ubuntu, if `import tkinter` fails
```

For calendar-style date pickers on the Start/End date fields, also install
the optional `tkcalendar` package:

```bash
pip install tkcalendar
```

This is GUI-only and intentionally not in `requirements.txt` (the CLI/wizard
have no GUI dependency to begin with). Without it, the date fields fall back
to plain typed entry, exactly as before — nothing else changes.

Like the wizard, the GUI is a thin front end over the exact same
`build_arg_parser()`/`main()` path the CLI uses underneath — it just collects
your choices into a form instead of prompts, then runs the query in a
background thread so the window doesn't freeze while Earth Engine works,
streaming progress into the log panel at the bottom. Choosing your own
boundary file unlocks Admin 3–5 granularity and the unit-name/unit-ID/simplify
fields, same as in the wizard, and auto-loads its columns into the
unit-name/unit-ID dropdowns and a scrollable, tickable checklist of extra
`--attributes` columns as soon as you pick the file — no separate "Load
available columns" click needed. For a GAUL country/name lookup, where
there's no file to auto-trigger from, the "Load available columns" button
(which needs Earth Engine set up already) still does the same job.

When you choose to break down by admin unit, the wizard looks up and prints
the actual fields available before asking which to include as
`--attributes` columns — the columns in your file for `--aoi-file`, or the
GAUL properties (e.g. `ADM0_NAME`, `ADM1_CODE`) for `--aoi-name` (this needs
Earth Engine set up already, per step 2 above, since it queries GAUL live).

Or by admin-unit name instead of a boundary file (looked up against
Earth Engine's FAO GAUL admin boundaries, country → admin-1 → admin-2):

```bash
python nightlight_tool.py --aoi-name "Ukraine" \
    --start 2021-01-01 --end 2026-09-01 --freq annual \
    --out-dir out --geoextent UKR
```

Or by ISO 3166-1 alpha-3 country code, which sidesteps having to know GAUL's
exact country-name spelling:

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2021-01-01 --end 2026-09-01 --freq annual --out-dir out
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
| `--out-dir` | yes | output folder for every output this run produces (CSV, vector, rasters, chart) — filenames are built from `--geoextent`/`--aoi-iso3` |
| `--geoextent` | no | geoextent code used in every output filename — required with `--aoi-file`/`--aoi-name`; automatic from `--aoi-iso3` |
| `--vector-out` | no | `geojson` or `shapefile` — also write spatial output joined to the boundary geometry by the unit's unique ID/pcode, in addition to the CSV — one file per period plus one combined file with every period (see below) |
| `--chart` | no | also write a PNG chart next to the CSV — one line chart for a single AOI, or a small-multiples grid (one mini chart per unit) with `--breakdown` (see below) |
| `--chart-units` | no | comma-separated exact values from the output's unit-name column to chart, when using `--chart` with `--breakdown` (see below) |
| `--ee-project` | no | your Earth Engine cloud project ID, if required (see step 2) |
| `--breakdown` | no | `admin1`–`admin5` — output one row per sub-unit per period instead of one row per period (see below). With `--aoi-name`/`--aoi-iso3`, only `admin1`/`admin2` are available (that's as far down as FAO GAUL goes); `admin3`–`admin5` need `--aoi-file` |
| `--unit-name-field` | no | required alongside `--breakdown` when using `--aoi-file` (see below) |
| `--unit-id-field` | no | column in `--aoi-file` holding each unit's unique ID, typically a pcode (see below) |
| `--attributes` | no | comma-separated field/column names to include as extra columns in `--breakdown` output (see below) |
| `--simplify-tolerance` | no | simplify `--aoi-file` geometries by this many degrees before sending to Earth Engine — only relevant to `--breakdown --aoi-file` with a detailed layer (see below) |
| `--include-yoy` | no | add `<stat>_yoy_abs`/`<stat>_yoy_pct` columns — each row vs. the same period one year back (see below) |
| `--dark-threshold` | no | radiance (nW/cm²/sr) below which a pixel counts as "dark" for the `pct_dark` column — default `0.5` (see below) |
| `--raster-whole-aoi` | no | also export a whole-AOI radiance GeoTIFF per period, written into `--out-dir` — independent of `--raster-yoy` (see below) |
| `--raster-yoy` | no | also export a year-over-year diff GeoTIFF per period, written into `--out-dir` — independent of `--raster-whole-aoi` **and** of `--include-yoy`'s tabular columns (see below) |
| `--raster-scale` | no | pixel resolution in meters for `--raster-whole-aoi`/`--raster-yoy` GeoTIFFs — default `500` |
| `--wizard` | no | run the interactive prompt instead of using flags (also runs automatically with no arguments) |

### Breaking a country down by admin unit

Instead of one AOI-wide number per period, `--breakdown` gives you one row per
admin unit per period — e.g. every governorate or district in a country, each
with its own radiance trend:

```bash
python nightlight_tool.py --aoi-name "Yemen" \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out-dir out --geoextent yem --breakdown admin1 --ee-project ee-masims
```

This looks up FAO GAUL admin1 (governorate/oblast-level) or admin2
(district/raion-level) units within that country, and queries all of them for
each period in a single Earth Engine call (not one call per unit — that
matters once you're at admin2 scale, which can be hundreds of units).
`--aoi-name`/`--aoi-iso3` only go down to admin2, since that's as far as FAO
GAUL carries boundaries — for admin3 (commune/ward), admin4, or admin5, supply
your own boundary file with `--aoi-file` instead (see below); there the admin
level you pass is just a label, since every feature in the file is already
its own unit regardless of which government tier it represents.

Output columns add the unit's name and ID (named after whichever GAUL field
actually produced them — `ADM1_NAME`/`ADM1_CODE` for `--breakdown admin1`,
`ADM2_NAME`/`ADM2_CODE` for `admin2` — filled in automatically, so they're
always populated with `--aoi-name`), plus `admin0_name`/`admin1_name`/
`admin2_name` (blank where not applicable) alongside the usual radiance/QA
columns — so you can pivot or join straight into a spreadsheet or GIS.
`--chart` in this mode
writes a small-multiples PNG (one mini chart per unit) instead of a single
shared chart, since one line per district isn't legible once there are more
than a handful:

```bash
python nightlight_tool.py --aoi-name "Yemen" --breakdown admin1 \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out-dir out --geoextent yem --chart --ee-project ee-masims
```

If there are more than 30 units, `--chart` charts the first 30 and warns you
— use `--chart-units` to pick specific ones instead (exact `unit_name`
values, comma-separated):

```bash
python nightlight_tool.py --aoi-name "Yemen" --breakdown admin1 \
    --start 2014-01-01 --end 2023-01-01 --freq annual \
    --out-dir out --geoextent yem --chart --chart-units "Sana'a,Aden,Ta'izz" \
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
    --out-dir out --geoextent crm --breakdown admin2 --ee-project ee-masims
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
    --out-dir out --geoextent crm --breakdown admin2 --ee-project ee-masims
```

This adds an ID column to the output alongside the name column — both named
after the actual column you pointed them at (`raion_pcode` and `raion_name`
in the example above, not a generic `unit_id`/`unit_name`), so you can join
back onto other datasets (or your GIS layer) by the stable code rather than a
name that might not match exactly. It's optional but recommended whenever
names might collide; if you skip it, the ID column just comes back as
`unit_id` with every value blank. With `--aoi-name` (GAUL), you don't need
this at all — the ID column is filled in for you automatically from GAUL's
`ADM1_CODE`/`ADM2_CODE`, and named accordingly. The interactive `--wizard`
asks for this right after the unit-name column.

#### Choosing which attributes end up in the output: `--attributes`

By default, `--breakdown` output includes `admin0_name`/`admin1_name`/
`admin2_name` when using `--aoi-name` (GAUL), and just the unit-name column
when using `--aoi-file`. That's not always enough — e.g. Venezuela has
several municipios that share the same name across different states, so the
unit name alone can't tell them apart in the CSV.

`--attributes` lets you pick exactly which fields from the admin/boundary
data become columns instead:

```bash
# GAUL: use GAUL's own property names
python nightlight_tool.py --aoi-name "Venezuela" --breakdown admin2 \
    --attributes ADM0_NAME,ADM1_NAME \
    --start 2024-09-01 --end 2026-09-01 --freq monthly \
    --out-dir out --geoextent ven --ee-project ee-masims

# --aoi-file: use column names from your own file
python nightlight_tool.py --aoi-file ven_admin2.geojson --unit-name-field adm2_name \
    --attributes adm1_name,adm1_pcode --breakdown admin2 \
    --start 2024-09-01 --end 2026-09-01 --freq monthly \
    --out-dir out --geoextent ven --ee-project ee-masims --simplify-tolerance 0.001
```

When `--attributes` is given, it fully replaces the default columns — you get
exactly the fields you named (plus your unit-name/unit-id columns), so include
whatever parent name/code field disambiguates your units. With `--aoi-file`, an unknown
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
    --out-dir out --geoextent ven --breakdown admin2 --ee-project ee-masims \
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

#### Getting a spatial file out: `--vector-out`

The CSV is great for spreadsheets and charting, but if you want the results
in a GIS (QGIS, ArcGIS) joined straight to the boundary geometry, add
`--vector-out geojson` or `--vector-out shapefile`:

```bash
python nightlight_tool.py --aoi-file crimea_raions.geojson \
    --unit-name-field raion_name --unit-id-field raion_pcode \
    --start 2021-01-01 --end 2023-01-01 --freq monthly \
    --out-dir out --geoextent crm --breakdown admin2 --ee-project ee-masims \
    --vector-out geojson
```

The geometry join uses `--unit-id-field` (pcode) when you've given one,
falling back to the unit-name column otherwise — the same matching logic
`--chart-units` uses. If a row's ID/name doesn't match any fetched geometry,
that row is dropped from the spatial output and a warning is printed (the
CSV still has it).

This writes **one spatial file per period** — one per frequency step
(month/week/year/day, whichever `--freq` is) — named
`{geoextent}_nightlights_{period}.{ext}`, e.g. `crm_nightlights_2021-01.geojson`,
`crm_nightlights_2021-02.geojson`, and so on. Each file has one feature per
unit, with:

- the unit's name/ID columns (and any `--attributes` columns) you selected, and
- `mean_radiance`, `sum_radiance`, `median_radiance`, `valid_pixel_count`,
  `scene_count`, `qa_flag`

Splitting by period this way makes it straightforward to step through or
animate months one at a time in a GIS, or load a single period's file to
symbolize on its own. The period itself isn't repeated as a column in these
files, since it's already at the end of every filename.

Alongside those, `--vector-out` also writes **one combined file with every
period in it** — `{geoextent}_nightlights_all_periods.{ext}` (e.g.
`crm_nightlights_all_periods.geojson`) — a one-to-many join of unit →
periods: one feature per unit *per period* (so the same unit's geometry
repeats once per period), matching the CSV's row shape. This one keeps a
`period` column, since that's the only thing telling rows for the same unit
apart. Use this file when you want to symbolize, filter, or chart by period
inside a single layer (e.g. a time-slider) rather than switching between
per-period files.

`--vector-out` works with or without `--breakdown` — without it, there's
just one "unit" (the whole AOI), so each period's file has a single feature
(and the combined file has one feature per period).

Shapefile field names are capped at 10 characters by the format itself. If
any of your column names are longer (or two get truncated down to the same
name), `--vector-out shapefile` truncates and de-duplicates them
automatically (appending `_2`, `_3`, etc. on a collision) and prints a
warning listing exactly which names got renamed to what — GeoJSON output
isn't affected by this limit.

### Year-over-year change: `--include-yoy`

`--include-yoy` adds `<stat>_yoy_abs`/`<stat>_yoy_pct` columns for
`mean_radiance`, `sum_radiance`, and `median_radiance` — each row compared
against the same period exactly one year back (March 2022 vs. March 2021;
the same ISO week number a year earlier for `--freq weekly`; February 29
and ISO week 53 are skipped when there's no matching date a year back):

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2022-01-01 --end 2023-01-01 --freq monthly \
    --out-dir out --include-yoy --ee-project ee-masims
```

The year-ago period doesn't have to fall inside `--start`/`--end` — the tool
fetches whichever year-ago periods aren't already covered by your date range
as extra Earth Engine calls, the same way `--breakdown` fetches every unit in
one call per period.

**Lead with `_yoy_abs`, not `_yoy_pct`, when judging whether something real
happened.** VIIRS radiance sits near zero across a lot of area/time (a quiet
rural district on an ordinary night), and percent change gets wild there — a
tiny, meaningless absolute change against a near-zero denominator can read as
a huge percentage even though nothing real changed, while the same absolute
change against a bright unit barely moves the percentage at all. This isn't
hypothetical: an earlier raster-based change layer built for a Crimea/Ukraine
analysis hit a 99.9th-percentile percent-change of 17,349% (max 4.75 million
percent) driven entirely by near-zero-radiance pixels, and one rayon's mean
*absolute* change was slightly negative (−0.10 nW/cm²/sr — essentially flat)
while its mean *percent* change read +157%, purely from averaging ratios with
near-zero denominators. Use `_yoy_pct` as a secondary check, and be skeptical
of a large `_yoy_pct` value paired with a small `_yoy_abs` value.

### Blackout/darkness indicator: `pct_dark` and `--dark-threshold`

Every row also includes `pct_dark` — the percent of valid AOI pixels whose
radiance fell below `--dark-threshold` (default `0.5` nW/cm²/sr) that period.
It's computed alongside the other stats at no extra Earth Engine call, and is
a simple, threshold-based way to track outages/blackouts: a rising `pct_dark`
means more of the area went dark, independent of how bright the lit portion
was. There's no single "correct" threshold — it depends on your sensor,
region, and what counts as "dark" for your analysis — so pick a value that
matches your own work and set it explicitly:

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2022-01-01 --end 2023-01-01 --freq monthly \
    --out-dir out --dark-threshold 0.3 --ee-project ee-masims
```

### Raster export: `--raster-whole-aoi` and `--raster-yoy`

`--raster-whole-aoi` exports a whole-AOI radiance GeoTIFF for each period,
written into `--out-dir` — there's no separate output location to set:

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2022-01-01 --end 2022-04-01 --freq monthly \
    --out-dir out --raster-whole-aoi --ee-project ee-masims
```

`--raster-yoy` exports a 2-band year-over-year diff GeoTIFF per period that
has a year-ago period available — band 1 absolute change (later minus
earlier, nW/cm²/sr), band 2 percent change (masked out where the earlier
period was ≤ 0, for the same near-zero-denominator reason described above).
It's **independent of `--raster-whole-aoi` and of `--include-yoy`'s tabular
columns** — turn on either raster flag, both, or neither, regardless of
whether `--include-yoy` is set:

```bash
python nightlight_tool.py --aoi-iso3 UKR \
    --start 2022-01-01 --end 2022-04-01 --freq monthly \
    --out-dir out --raster-yoy --ee-project ee-masims
```

Filenames follow a fixed, all-underscore template:

```
{geoextent}_evnt_lit_ras_s0_viirs_pp_{freetext}.tif
```

`{geoextent}` is a short code identifying the AOI, used in every output
filename (CSV, vector, and raster alike) — automatic from `--aoi-iso3` (e.g.
`ukr`), but **required via `--geoextent`** when using `--aoi-file` or
`--aoi-name`, since those have no ISO3 code of their own:

```bash
python nightlight_tool.py --aoi-file crimea_raions.geojson \
    --start 2022-01-01 --end 2022-04-01 --freq monthly \
    --out-dir out --geoextent crm --raster-whole-aoi --raster-yoy \
    --ee-project ee-masims
```

`{freetext}` carries the period (e.g. `ukr_evnt_lit_ras_s0_viirs_pp_2022_01.tif`
for the raw January 2022 raster) or, for a YoY diff, both periods being
compared (e.g. `..._diff_yoy_2022_01_minus_2021_01.tif`). Every component is
lowercased and run through the same sanitizer, so a period label like
`2022-01` or `2022-W05` always comes out hyphen-free (`2022_01`/`2022_w05`).

`--raster-scale` sets the pixel resolution in meters (default `500`,
matching the tabular stats' default `scale`). Small AOIs at a reasonable
resolution download directly; an AOI/resolution combination too large for a
direct download automatically falls back to a Google Drive export task
(printed to the console) — this tool doesn't wait for that task to finish,
so check Google Drive or the Earth Engine Task Manager for its completion.

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

- Change detection is limited to year-over-year (`--include-yoy`) and a
  simple dark-pixel-percent indicator (`pct_dark`) — there's no baseline
  (e.g. pre-war average) comparison or month-over-month change yet.
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

## Roadmap

Towards a tool anyone can pick up without reading this whole README first:

- **Fieldmaps.io/global-dataset AOI picker** — `--aoi-iso3` (see above) covers
  unambiguous country selection; a fuller country dropdown backed by
  fieldmaps.io and/or the latest GAUL release, plus admin1/2-level lookups
  by code rather than name, is the next step.
- **Geodatabase (`.gdb`) input** — `--aoi-file`/`--unit-name-field` currently
  read via `geopandas`, which supports `.gdb`, but this hasn't been tested or
  documented as a supported input format yet.
- **Baseline-period change detection** — `--include-yoy` covers
  year-over-year; a fixed, user-chosen baseline period (e.g. a pre-war
  average) to compare every row against is still open.
Already delivered towards the "anyone can use it" goal: `--wizard` interactive
mode (which also shows the actual admin-data fields available before you pick
`--attributes`), `weekly` frequency, `--attributes` for choosing output
columns, `--aoi-iso3` for unambiguous country selection, small-multiples
`--chart` support in `--breakdown` mode (`--chart-units` to pick specific
units), `--vector-out` for a joined spatial output (GeoJSON/shapefile),
year-over-year change columns (`--include-yoy`), a dark-pixel/blackout
indicator (`pct_dark`, `--dark-threshold`), whole-AOI and year-over-year-diff
raster export (`--raster-whole-aoi`, `--raster-yoy`), and a GUI Outputs
section (one shared output folder, a geoextent/ISO3 code, and checkboxes for
which outputs to produce).
