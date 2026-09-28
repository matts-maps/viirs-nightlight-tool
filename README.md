# VIIRS Nightlight Tool

A reusable command-line tool that extracts a VIIRS nighttime-lights radiance
time series for any area of interest — a country, a sub-national unit, or a
custom boundary you supply — over any date range and at daily, monthly, or
annual frequency.

Built to be run by anyone, on any machine: it's plain Python with open-source
dependencies, no ArcGIS/arcpy requirement, and no hardcoded paths.

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

Or by admin-unit name instead of a boundary file (looked up against
Earth Engine's FAO GAUL admin boundaries, country → admin-1 → admin-2):

```bash
python nightlight_tool.py --aoi-name "Ukraine" \
    --start 2021-01-01 --end 2026-09-01 --freq annual --out ukraine_annual.csv
```

### Arguments

| Flag | Required | Notes |
|---|---|---|
| `--aoi-file` | one of `--aoi-file` / `--aoi-name` | path to a GeoJSON/shapefile/etc. — for a specific sub-unit (e.g. one raion), get a proper boundary from a source like [fieldmaps.io](https://fieldmaps.io), [HDX COD](https://data.humdata.org/), or [GADM](https://gadm.org) |
| `--aoi-name` | — | admin name to look up, e.g. `"Ukraine"`, or an admin-1/2 name |
| `--start`, `--end` | yes | ISO dates, `--end` is exclusive |
| `--freq` | yes | `daily`, `monthly`, or `annual` |
| `--out` | yes | output CSV path |
| `--chart` | no | also write a PNG line chart next to the CSV |
| `--ee-project` | no | your Earth Engine cloud project ID, if required (see step 2) |

### Choosing a frequency

- **monthly** (recommended default) uses NOAA's pre-composited, cloud-free
  monthly product (`VCMSLCFG`) — the most reliable option for a trend.
- **annual** averages the monthly composites over each calendar year.
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
