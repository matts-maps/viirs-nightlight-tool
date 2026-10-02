#!/usr/bin/env python3
"""
nightlight_tool.py — VIIRS nighttime-lights time series for any area of interest.

Given an area of interest (your own boundary file), a date range, and a frequency,
this pulls VIIRS Day/Night Band radiance from Google Earth Engine and writes a
per-period time series (CSV + a quick chart PNG).

Quick start
-----------
    pip install -r requirements.txt
    earthengine authenticate      # one-time, needs a free Google account
    python nightlight_tool.py --aoi-file sample_aoi/crimea.geojson \\
        --start 2021-01-01 --end 2023-01-01 --freq monthly --out-dir out --geoextent crm

See README.md for full setup and usage details.

Design notes
------------
The "pure" logic (period construction, pixel-array statistics, QA flagging, output
writing) lives in plain functions with no Earth Engine dependency, so it can be
unit-tested offline (see tests/) without an authenticated EE session. Only
`resolve_aoi_geometry`, `fetch_period_stats`, and `main` touch the `ee` module.

Known limitation: the VNP46A2 cloud/quality bit-flag layout used in
`daily_pixel_quality_mask` reflects the product documentation at the time this was
written. NASA/NOAA have changed VIIRS product band layouts before — if daily results
look implausible, check the current VNP46A2 User Guide's QF_Cloud_Mask bit table
against the constants below before trusting the numbers.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Pure logic — no `ee` import here, so this half is unit-testable offline.
# ---------------------------------------------------------------------------

VALID_FREQS = ("daily", "weekly", "monthly", "annual")

# VNP46A2 Mandatory_Quality_Flag values (per the product's QA band):
#   0 = high-quality, persistent nighttime lights, main algorithm
#   1 = other retrieval quality (e.g. lower-quality VIIRS retrieval)
#   2 = poor quality / gap-filled / snow-covered
# We keep 0 and 1 by default, drop 2. See module docstring caveat above.
MANDATORY_QF_KEEP = (0, 1)

# Minimum fraction of AOI pixels that must have passed the quality mask for a
# daily/period value to be trusted without a QA flag.
MIN_VALID_PIXEL_FRACTION = 0.5
MIN_SCENE_COUNT = 1

# Default --dark-threshold: a pixel below this radiance (nW/cm2/sr) counts as
# "dark" for the pct_dark column. Not derived from any particular published
# study -- just a low-but-nonzero cutoff that distinguishes "no detectable
# lighting" from VIIRS's own noise floor. Override with --dark-threshold if
# you have a value that matches your own analysis.
DEFAULT_DARK_THRESHOLD_NW = 0.5


@dataclass(frozen=True)
class Period:
    label: str          # e.g. "2022-03", "2022", "2022-03-14"
    start: date          # inclusive
    end: date            # exclusive


def _month_add(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def build_periods(start: str, end: str, freq: str) -> list[Period]:
    """Split [start, end) into a list of Periods at the requested frequency.

    `start`/`end` are ISO date strings (YYYY-MM-DD). `end` is exclusive, matching
    Earth Engine's filterDate convention. Raises ValueError on bad input.
    """
    if freq not in VALID_FREQS:
        raise ValueError(f"freq must be one of {VALID_FREQS}, got {freq!r}")

    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)
    if end_d <= start_d:
        raise ValueError(f"--end ({end}) must be after --start ({start})")

    periods: list[Period] = []

    if freq == "daily":
        cur = start_d
        while cur < end_d:
            nxt = cur + timedelta(days=1)
            periods.append(Period(cur.isoformat(), cur, nxt))
            cur = nxt

    elif freq == "monthly":
        cur = date(start_d.year, start_d.month, 1)
        while cur < end_d:
            nxt = _month_add(cur, 1)
            periods.append(Period(f"{cur.year:04d}-{cur.month:02d}", cur, nxt))
            cur = nxt

    elif freq == "weekly":
        # Snap to the Monday of the start date's ISO week, same spirit as
        # monthly snapping to the 1st -- a partial first week is still
        # reported as a full calendar week rather than a ragged stub.
        cur = start_d - timedelta(days=start_d.weekday())
        while cur < end_d:
            nxt = cur + timedelta(days=7)
            iso_year, iso_week, _ = cur.isocalendar()
            periods.append(Period(f"{iso_year:04d}-W{iso_week:02d}", cur, nxt))
            cur = nxt

    elif freq == "annual":
        cur = date(start_d.year, 1, 1)
        while cur < end_d:
            nxt = date(cur.year + 1, 1, 1)
            periods.append(Period(f"{cur.year:04d}", cur, nxt))
            cur = nxt

    return periods


_PERIOD_LABEL_EXAMPLES = {
    "daily": "2021-01-15",
    "weekly": "2021-W05",
    "monthly": "2021-01",
    "annual": "2021",
}


def parse_period_label(label: str, freq: str) -> Period:
    """Pure function: the inverse of build_periods() -- given one period
    label in the same format build_periods() would have produced for
    `freq` (e.g. "2021-01" for monthly, "2021" for annual, "2021-W05" for
    weekly, "2021-01-15" for daily), reconstruct the matching Period
    (start/end dates). Used by year_ago_period_label() to turn a row's
    'period' string back into dates it can do calendar math on. Raises
    ValueError on a label that doesn't match `freq`'s format.
    """
    if freq not in VALID_FREQS:
        raise ValueError(f"freq must be one of {VALID_FREQS}, got {freq!r}")
    try:
        if freq == "daily":
            start_d = date.fromisoformat(label)
            return Period(label, start_d, start_d + timedelta(days=1))
        if freq == "monthly":
            year_s, month_s = label.split("-")
            start_d = date(int(year_s), int(month_s), 1)
            return Period(label, start_d, _month_add(start_d, 1))
        if freq == "annual":
            start_d = date(int(label), 1, 1)
            return Period(label, start_d, date(start_d.year + 1, 1, 1))
        # weekly
        year_s, week_s = label.split("-W")
        start_d = date.fromisocalendar(int(year_s), int(week_s), 1)
        return Period(label, start_d, start_d + timedelta(days=7))
    except (ValueError, IndexError) as e:
        raise ValueError(
            f"{label!r} doesn't look like a {freq} period label "
            f"(expected e.g. {_PERIOD_LABEL_EXAMPLES[freq]!r}): {e}"
        ) from e


def year_ago_period(period: Period, freq: str) -> Optional[Period]:
    """Pure function (--include-yoy): the Period exactly one year before
    `period`, at the same point in the calendar/ISO-week cycle -- or None
    when that period doesn't exist. That's only possible for daily (a
    Feb 29 has no year-ago Feb 29 in a non-leap year) and weekly (ISO
    week 53 doesn't occur in every year), so those are skipped rather
    than approximated.
    """
    if freq not in VALID_FREQS:
        raise ValueError(f"freq must be one of {VALID_FREQS}, got {freq!r}")

    if freq == "daily":
        try:
            start_d = date(period.start.year - 1, period.start.month, period.start.day)
        except ValueError:
            return None
        return Period(start_d.isoformat(), start_d, start_d + timedelta(days=1))

    if freq == "monthly":
        year_s, month_s = period.label.split("-")
        year, month = int(year_s) - 1, int(month_s)
        start_d = date(year, month, 1)
        return Period(f"{year:04d}-{month:02d}", start_d, _month_add(start_d, 1))

    if freq == "annual":
        year = int(period.label) - 1
        return Period(f"{year:04d}", date(year, 1, 1), date(year + 1, 1, 1))

    # weekly -- go by the actual ISO calendar of period.start (already
    # snapped to that week's Monday) rather than string-splitting the
    # label, so this stays correct regardless of how the label was built.
    iso_year, iso_week, _ = period.start.isocalendar()
    try:
        start_d = date.fromisocalendar(iso_year - 1, iso_week, 1)
    except ValueError:
        return None  # this ISO week doesn't exist a year back (e.g. week 53)
    return Period(f"{iso_year - 1:04d}-W{iso_week:02d}", start_d, start_d + timedelta(days=7))


def year_ago_period_label(label: str, freq: str) -> Optional[str]:
    """Pure function (--include-yoy): the period label exactly one year
    before `label` (a period label in build_periods()'s format for
    `freq`), or None when that period doesn't exist (see year_ago_period())
    or `label` itself doesn't parse.
    """
    try:
        period = parse_period_label(label, freq)
    except ValueError:
        return None
    year_ago = year_ago_period(period, freq)
    return year_ago.label if year_ago else None


DEFAULT_CHANGE_STAT_COLS = ("mean_radiance", "sum_radiance", "median_radiance")


def _stat_diff(current, previous):
    """Pure helper: (absolute, percent) change from `previous` to `current`.
    Either value missing -> (None, None). `previous` is zero -> (absolute
    change, None), since percent change from zero is undefined.
    """
    if current is None or previous is None:
        return None, None
    abs_change = current - previous
    pct_change = (abs_change / previous) * 100 if previous != 0 else None
    return abs_change, pct_change


def compute_year_over_year_change(
    rows: list[dict],
    freq: str,
    lookup: dict,
    stat_cols: tuple[str, ...] = DEFAULT_CHANGE_STAT_COLS,
    group_key=None,
) -> list[dict]:
    """Pure function (--include-yoy): return a copy of `rows` with
    "<stat>_yoy_abs"/"<stat>_yoy_pct" columns added for each of
    `stat_cols`, comparing each row to the same period one year back
    (see year_ago_period_label()) *within its own group*.

    `lookup` maps (group_key(row) if group_key else None, period_label) ->
    row, and must cover every period a year-ago comparison might land on --
    typically all of `rows` plus any extra year-ago-only periods the
    caller fetched separately (see year_ago_period()), for rows near the
    start of the requested range whose year-ago period falls outside it.
    A row with no matching entry in `lookup` (data wasn't fetched, or the
    year-ago period doesn't exist at all) gets None for both new columns.

    Doesn't touch `ee` -- pure list/dict/date manipulation -- so it's
    testable offline like every other row-shaping helper here.
    """
    out = []
    for row in rows:
        group = group_key(row) if group_key else None
        year_ago_label = year_ago_period_label(row.get("period"), freq)
        year_ago_row = lookup.get((group, year_ago_label)) if year_ago_label else None
        new_row = dict(row)
        for stat in stat_cols:
            abs_change, pct_change = _stat_diff(
                row.get(stat), year_ago_row.get(stat) if year_ago_row else None
            )
            new_row[f"{stat}_yoy_abs"] = abs_change
            new_row[f"{stat}_yoy_pct"] = pct_change
        out.append(new_row)
    return out


def _sanitize_filename_token(s: str) -> str:
    """Pure helper (--raster-whole-aoi/--raster-yoy): turn `s` into a filesystem-safe, all-
    underscore token -- lowercased, every run of non-alphanumeric characters
    (hyphens, spaces, slashes, etc.) collapsed to a single underscore, with
    leading/trailing underscores stripped. Used for every component of a
    raster filename, so a period label like "2022-01" or "2022-W05"
    becomes "2022_01"/"2022_w05" rather than carrying a hyphen through --
    filenames stay entirely underscore-separated.
    """
    token = re.sub(r"[^0-9a-zA-Z]+", "_", s.lower()).strip("_")
    return token or "na"


def build_raster_filename(geoextent: str, freetext: str) -> str:
    """Pure function (--raster-whole-aoi/--raster-yoy): the filename for one raster output
    file, following the naming template
    "{geoextent}_evnt_lit_ras_s0_viirs_pp_{freetext}.tif" -- category
    "evnt" (event), subcategory "lit" (nighttime lights), scale code "s0"
    (fixed -- this tool doesn't carry a scale-code convention of its own),
    geometry type "ras" (raster), source "viirs", "pp" (post-processed),
    then free text identifying the period/comparison. Every component is
    run through _sanitize_filename_token() so the whole filename is
    lowercase and underscore-separated, never hyphenated.
    """
    return (
        f"{_sanitize_filename_token(geoextent)}_evnt_lit_ras_s0_viirs_pp_"
        f"{_sanitize_filename_token(freetext)}.tif"
    )


def resolve_geoextent(geoextent: Optional[str]) -> str:
    """Pure function: the geoextent code to use in every output filename
    (CSV, vector, raster). Every AOI comes from --aoi-file now, which has
    no code of its own to default to, so --geoextent is always required.
    Raises ValueError with a message fit to print directly when it's
    missing.
    """
    if geoextent:
        return geoextent
    raise ValueError(
        "A geoextent code is needed for output filenames -- pass --geoextent "
        "(e.g. --geoextent crm)."
    )


def build_output_filename(geoextent: str, suffix: str) -> str:
    """Pure function: the filename for a CSV or vector output file, following
    the naming template "{geoextent}_nightlights.{suffix}" (e.g.
    "npl_nightlights.csv", "npl_nightlights.geojson"). Run through
    _sanitize_filename_token() so it's lowercase and underscore-separated,
    matching the raster filename convention in build_raster_filename().
    """
    return f"{_sanitize_filename_token(geoextent)}_nightlights.{suffix}"


def _get_stat(feature_properties: dict, band: str, stat: str):
    """Look up one reducer output, tolerant of two different Earth Engine naming
    conventions we've observed in practice: `Image.reduceRegion` (single AOI)
    prefixes outputs with the band name (e.g. "avg_rad_mean"), while
    `Image.reduceRegions` (per-feature, used by --breakdown) has returned the
    bare reducer output name (e.g. "mean") with a single selected band. Rather
    than assume one or the other, check both so this survives an EE API/version
    difference either way.
    """
    for key in (stat, f"{band}_{stat}"):
        if key in feature_properties:
            return feature_properties[key]
    return None


def _extract_pct_dark(feature_properties: dict) -> Optional[float]:
    """Pure helper: the pct_dark column value (0-100) from one reduceRegion/
    reduceRegions output -- the percent of valid pixels below --dark-threshold.

    Reads the 'dark_mean' reducer output specifically (never the bare 'mean'
    fallback _get_stat() uses) -- the dark band is always added alongside the
    real radiance band (see _add_dark_band()), so the image always has 2+
    bands and Earth Engine always prefixes reducer outputs with the band
    name; the bare-name convention _get_stat() tolerates only occurs with a
    single selected band.
    """
    frac = feature_properties.get("dark_mean")
    return frac * 100 if frac is not None else None


def build_breakdown_row(
    period_label: str,
    band: str,
    feature_properties: dict,
    scene_count: int,
    attribute_fields: Optional[list[str]] = None,
) -> dict:
    """Pure function: turn one reduceRegions-output feature's properties into a row.

    `feature_properties` is a plain dict (as returned by ee's getInfo(), or a fake
    one in tests) — this function makes no Earth Engine calls itself, which is what
    keeps it unit-testable without an EE session.

    `attribute_fields`, when given, names the exact properties/columns (from
    --aoi-file, via --attributes) to carry into the output as their own
    columns. If omitted, no extra columns are added beyond unit_name/unit_id.

    `unit_id` (in `feature_properties`) is always surfaced as its own column,
    separate from `attribute_fields` -- it's the stable, unique identifier
    for the unit (whichever column the user pointed at as a unique key, e.g.
    a pcode) as opposed to `unit_name`, which is human-readable but not
    guaranteed unique (two municipios can share a name). It's None when the
    caller didn't have one to set.
    """
    row: dict = {
        "period": period_label,
        "unit_name": feature_properties.get("unit_name"),
        "unit_id": feature_properties.get("unit_id"),
    }
    if attribute_fields:
        for field in attribute_fields:
            row[field] = feature_properties.get(field)
    row.update(
        {
            "mean_radiance": _get_stat(feature_properties, band, "mean"),
            "sum_radiance": _get_stat(feature_properties, band, "sum"),
            "median_radiance": _get_stat(feature_properties, band, "median"),
            "pct_dark": _extract_pct_dark(feature_properties),
            "valid_pixel_count": _get_stat(feature_properties, band, "count"),
            "scene_count": scene_count,
            "qa_flag": qa_flag(scene_count, None),
        }
    )
    return row


def qa_flag(scene_count: int, valid_pixel_fraction: Optional[float]) -> str:
    """Pure QA classification, independent of how the counts were produced.

    `valid_pixel_fraction` is None when it isn't tracked for this product path
    (e.g. the pre-composited monthly product has no per-pixel quality band).
    """
    if scene_count < MIN_SCENE_COUNT:
        return "no_data"
    if valid_pixel_fraction is not None and valid_pixel_fraction < MIN_VALID_PIXEL_FRACTION:
        return "low_valid_pixels"
    return "ok"


def summarize_pixels(values, mask) -> dict:
    """Zonal-style stats over a 2D array of radiance values and a boolean mask.

    Pure numpy — used both by the offline smoke tests and as a stand-in for what
    ee.Reducer does server-side, so the two can be reasoned about the same way.
    """
    import numpy as np

    values = np.asarray(values, dtype=float)
    mask = np.asarray(mask, dtype=bool)
    total_pixels = int(values.size)
    valid = values[mask]
    valid_pixel_count = int(valid.size)
    valid_fraction = (valid_pixel_count / total_pixels) if total_pixels else 0.0

    if valid_pixel_count == 0:
        return {
            "mean_radiance": None,
            "sum_radiance": None,
            "median_radiance": None,
            "valid_pixel_count": 0,
            "total_pixel_count": total_pixels,
            "valid_pixel_fraction": 0.0,
        }

    return {
        "mean_radiance": float(np.mean(valid)),
        "sum_radiance": float(np.sum(valid)),
        "median_radiance": float(np.median(valid)),
        "valid_pixel_count": valid_pixel_count,
        "total_pixel_count": total_pixels,
        "valid_pixel_fraction": valid_fraction,
    }


def write_csv(rows: Iterable[dict], out_path: Path, fieldnames: Optional[list[str]] = None) -> None:
    rows = list(rows)
    if not rows:
        raise ValueError("no rows to write")
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


DEFAULT_MAX_BREAKDOWN_CHART_PANELS = 30


def _chart_key(row: dict):
    """Grouping key for one row's unit in a --breakdown chart.

    unit_name alone isn't a safe key -- it's only meant to be human-readable
    and two different units (e.g. two admin3 units in different admin2s)
    can share a name. When the row carries a unit_id, that's used instead;
    only rows with no unit_id at all (older runs, or --aoi-file without
    --unit-id-field) fall back to grouping by unit_name as before.
    """
    uid = row.get("unit_id")
    if uid not in (None, ""):
        return uid
    return row.get("unit_name")


def select_breakdown_chart_units(
    rows: list[dict],
    unit_filter: Optional[list[str]] = None,
    max_panels: int = DEFAULT_MAX_BREAKDOWN_CHART_PANELS,
) -> list:
    """Pure function: decide which units a --breakdown chart should draw one
    panel for, and in what order. Returns a list of chart keys (see
    _chart_key) -- plain unit_name strings when no unit_id is present, so
    this is a drop-in superset of the old name-only behaviour.

    With `unit_filter` given (exact unit_name values, as the user typed them),
    returns every distinct unit belonging to a matching name, grouped by name
    in the order given -- if that name turns out to belong to more than one
    unit (names collide), all of them are included rather than merged into
    one panel -- deliberate selection needs no cap. Without a filter, returns
    every unit that appears in `rows`, in first-seen order, unless that's
    more than `max_panels`, in which case it truncates and the caller is
    expected to warn (a chart with hundreds of tiny panels isn't useful, and
    the fix is to either narrow with --chart-units or just pivot the CSV
    yourself for a full per-unit view).
    """
    keys_by_name: dict[str, list] = {}
    seen_keys = set()
    order: list = []
    for row in rows:
        key = _chart_key(row)
        if key is None:
            continue
        if key not in seen_keys:
            seen_keys.add(key)
            order.append(key)
            keys_by_name.setdefault(row.get("unit_name"), []).append(key)

    if unit_filter:
        result = []
        for name in unit_filter:
            result.extend(keys_by_name.get(name, []))
        return result

    return order[:max_panels]


def write_breakdown_chart(
    rows: list[dict],
    chart_path: Path,
    units: list,
    value_field: str = "mean_radiance",
) -> None:
    """Write a small-multiples PNG: one mini line chart per unit in `units`,
    each showing `value_field` over `period`. Companion to write_chart() for
    --breakdown output, where a single shared chart with one line per unit
    isn't legible once there are more than a handful of units.

    `units` is a list of chart keys as returned by select_breakdown_chart_units
    (unit_id when available, else unit_name) -- rows are grouped by that same
    key so units sharing a name never get merged into one panel.
    """
    import math

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not units:
        raise ValueError("no units to chart")

    by_unit: dict = {u: [] for u in units}
    unit_titles: dict = {}
    for row in rows:
        key = _chart_key(row)
        if key in by_unit:
            by_unit[key].append(row)
            if key not in unit_titles:
                name = row.get("unit_name")
                uid = row.get("unit_id")
                unit_titles[key] = f"{name} ({uid})" if uid not in (None, "") else str(name)

    n = len(units)
    ncols = min(4, n)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.0 * nrows), squeeze=False)

    for i, unit in enumerate(units):
        ax = axes[i // ncols][i % ncols]
        unit_rows = by_unit[unit]
        labels = [r["period"] for r in unit_rows]
        values = [r.get(value_field) for r in unit_rows]
        ax.plot(labels, values, marker="o", linewidth=1.2, markersize=3)
        ax.set_title(unit_titles.get(unit, str(unit)), fontsize=9)
        ax.tick_params(axis="x", rotation=60, labelsize=7)
        ax.tick_params(axis="y", labelsize=7)
        for flag_row in unit_rows:
            if flag_row.get("qa_flag") not in (None, "ok"):
                ax.axvspan(flag_row["period"], flag_row["period"], color="red", alpha=0.15)

    # Hide any unused grid cells (n doesn't always divide evenly into the grid).
    for i in range(n, nrows * ncols):
        axes[i // ncols][i % ncols].axis("off")

    fig.suptitle(f"VIIRS nighttime-lights radiance by unit ({value_field.replace('_', ' ')})")
    fig.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)


def write_chart(rows: list[dict], chart_path: Path, value_field: str = "mean_radiance") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [r["period"] for r in rows]
    values = [r.get(value_field) for r in rows]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(labels, values, marker="o", linewidth=1.5)
    ax.set_ylabel(value_field.replace("_", " "))
    ax.set_title("VIIRS nighttime-lights radiance")
    ax.tick_params(axis="x", rotation=60)
    for flag_row in rows:
        if flag_row.get("qa_flag") not in (None, "ok"):
            ax.axvspan(flag_row["period"], flag_row["period"], color="red", alpha=0.15)
    fig.tight_layout()
    chart_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=150)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Earth Engine glue — only this half touches `ee`.
# ---------------------------------------------------------------------------

def resolve_aoi_geometry(aoi_file: str):
    """Return an ee.Geometry for the AOI, dissolved from the supplied
    boundary file. The only AOI source this tool supports -- see the module
    docstring and README for why automatic country/admin-name lookups
    (formerly --aoi-iso3/--aoi-name against FAO GAUL) were dropped.
    """
    import ee
    import geopandas as gpd

    gdf = gpd.read_file(aoi_file)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    # Dissolve to a single geometry so multi-feature files (e.g. several
    # raions) are treated as one AOI.
    geom = gdf.union_all() if hasattr(gdf, "union_all") else gdf.unary_union
    return ee.Geometry(geom.__geo_interface__)


def daily_pixel_quality_mask(image):
    """Mask a VNP46A2 image to reasonably reliable pixels only.

    See the module docstring: verify MANDATORY_QF_KEEP against the current
    VNP46A2 product documentation if results look off.
    """
    qf = image.select("Mandatory_Quality_Flag")
    keep_mask = qf.eq(MANDATORY_QF_KEEP[0])
    for v in MANDATORY_QF_KEEP[1:]:
        keep_mask = keep_mask.Or(qf.eq(v))
    return image.updateMask(keep_mask)


def _get_period_image_and_scene_count(freq: str, period: Period):
    """Shared by both single-AOI and breakdown paths: resolve the (image, band,
    scene_count) for one period, or (None, band, 0) if nothing's available.
    """
    import ee

    start_str = period.start.isoformat()
    end_str = period.end.isoformat()

    if freq in ("monthly", "annual"):
        band = "avg_rad"
        coll = (
            ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
            .filterDate(start_str, end_str)
            .select(band)
        )
        scene_count = coll.size().getInfo()
        if scene_count == 0:
            return None, band, 0
        # Annual = mean-composite the monthly images first, then reduce once.
        image = coll.mean() if freq == "annual" else coll.mosaic()
        return image, band, scene_count

    # daily or weekly: both mosaic the gap-filled daily product over the
    # period's date range (a single day for "daily", a calendar week for
    # "weekly") -- there's no native VIIRS weekly composite product, so a
    # week is built the same way "annual" is built from monthly images: by
    # combining the finer-grained product over a wider window.
    band = "Gap_Filled_DNB_BRDF_Corrected_NTL"
    coll = (
        ee.ImageCollection("NASA/VIIRS/002/VNP46A2")
        .filterDate(start_str, end_str)
        .map(daily_pixel_quality_mask)
        .select(band)
    )
    scene_count = coll.size().getInfo()
    if scene_count == 0:
        return None, band, 0
    image = coll.mosaic()
    return image, band, scene_count


def _combined_reducer():
    import ee

    return (
        ee.Reducer.mean()
        .combine(ee.Reducer.sum(), sharedInputs=True)
        .combine(ee.Reducer.median(), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
    )


def _add_dark_band(image, band: str, dark_threshold: float):
    """Add a boolean 'dark' band (1 where `band` < `dark_threshold`, else 0)
    alongside `band` itself, so one reduceRegion/reduceRegions call -- with
    the same _combined_reducer(), which Earth Engine broadcasts across every
    band of a multi-band image -- produces 'dark_mean' (the fraction of
    valid pixels below the threshold) in addition to the usual mean/sum/
    median/count on `band`, at no extra Earth Engine round trip.
    """
    dark = image.select(band).lt(dark_threshold).rename("dark")
    return image.select(band).addBands(dark)


def fetch_period_stats(
    freq: str,
    aoi_geom,
    period: Period,
    scale: int = 500,
    dark_threshold: float = DEFAULT_DARK_THRESHOLD_NW,
) -> dict:
    """Query Earth Engine for one period's zonal radiance stats + QA fields
    over a single AOI geometry."""
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)
    if image is None:
        return {
            "period": period.label,
            "mean_radiance": None,
            "sum_radiance": None,
            "median_radiance": None,
            "pct_dark": None,
            "valid_pixel_count": None,
            "total_pixel_count": None,
            "scene_count": 0,
            "qa_flag": "no_data",
        }

    stats = _add_dark_band(image, band, dark_threshold).reduceRegion(
        reducer=_combined_reducer(), geometry=aoi_geom, scale=scale, maxPixels=1e10, bestEffort=True
    ).getInfo()
    dark_mean = stats.get("dark_mean")
    return {
        "period": period.label,
        "mean_radiance": stats.get(f"{band}_mean"),
        "sum_radiance": stats.get(f"{band}_sum"),
        "median_radiance": stats.get(f"{band}_median"),
        "pct_dark": dark_mean * 100 if dark_mean is not None else None,
        "valid_pixel_count": stats.get(f"{band}_count"),
        "total_pixel_count": None,  # not tracked for the pre-composited/mosaicked product
        "scene_count": scene_count,
        "qa_flag": qa_flag(scene_count, None),
    }


def simplify_geometry(geom, tolerance: Optional[float]):
    """Pure geometry step: simplify a shapely geometry if a tolerance is given,
    otherwise return it unchanged. Kept separate from the ee-glue code below so
    it's unit-testable without an Earth Engine session.

    Earth Engine rejects a client-side literal (an ee.FeatureCollection built
    from local geometries, as --aoi-file + --breakdown does) once its payload
    exceeds 10MB — every polygon's full vertex list gets embedded in every API
    call that touches it. A detailed admin2/raion layer with a few hundred
    units can blow past that easily. Since VIIRS itself only resolves to
    ~500m, simplifying well below that scale won't meaningfully change zonal
    stats for reasonably-sized units — but very small or thin units could be
    affected, so this is opt-in via --simplify-tolerance, not automatic.
    """
    if tolerance is None or tolerance <= 0:
        return geom
    return geom.simplify(tolerance, preserve_topology=True)


def rename_unit_columns(
    rows: list[dict], unit_name_column: str, unit_id_column: Optional[str]
) -> list[dict]:
    """Pure function: rename the generic 'unit_name'/'unit_id' keys in each
    row to whichever column actually identifies each unit -- e.g. 'adm2_name'
    and 'adm2_pcode' for a --unit-name-field/--unit-id-field pair -- so the
    CSV header says what the values actually are instead of a generic label.

    This is a presentation-only rename applied right before the CSV is
    written. Every other part of the tool (charting, --chart-units matching,
    the wizard/GUI, the rest of build_breakdown_row/fetch_period_breakdown_stats)
    keeps using the generic 'unit_name'/'unit_id' keys internally -- rows
    passed in here are a fresh set of dicts, the originals are untouched, so
    callers that still need the generic keys (e.g. for charting after this
    is called) keep working from the pre-rename rows.

    unit_id_column of None (or already 'unit_id') leaves the 'unit_id' key
    as-is -- there's no more specific name to rename it to when no
    --unit-id-field was given. Key order is preserved, since csv.DictWriter
    takes its header from the first row's key order.
    """
    if not rows:
        return rows
    if unit_name_column == "unit_name" and unit_id_column in (None, "unit_id"):
        return rows  # nothing to rename -- avoid needless copies

    renamed = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            if key == "unit_name" and unit_name_column != "unit_name":
                new_row[unit_name_column] = value
            elif key == "unit_id" and unit_id_column and unit_id_column != "unit_id":
                new_row[unit_id_column] = value
            else:
                new_row[key] = value
        renamed.append(new_row)
    return renamed


def attach_geometry(rows: list[dict], geometry_for_row) -> list[dict]:
    """Pure function: return a copy of `rows` with a "geometry" key added to
    each, via `geometry_for_row(row)` -- a plain callable, not an Earth
    Engine object, so this takes no `ee` dependency and is unit-testable
    with a fake lookup. A row whose geometry can't be found gets
    geometry=None (the caller decides whether/how to warn and drop those).
    """
    out = []
    for row in rows:
        new_row = dict(row)
        new_row["geometry"] = geometry_for_row(row)
        out.append(new_row)
    return out


def shapefile_safe_field_names(names: list[str]) -> dict[str, str]:
    """Pure function: map each name to a version that fits the ESRI
    Shapefile format's 10-character field-name limit, keeping every result
    unique. Longer names are truncated to 10 characters; if that collides
    with an already-assigned name, characters are dropped off the end to
    make room for a disambiguating digit (or digits, if even more collide).

    geopandas/fiona will silently truncate+may-collide on their own when
    writing a shapefile with long field names -- doing it explicitly here
    means collisions are visible (as a warning at the call site) instead of
    quietly dropping/overwriting a column.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for name in names:
        candidate = name[:10]
        suffix_n = 1
        while candidate in used:
            suffix = str(suffix_n)
            candidate = name[: 10 - len(suffix)] + suffix
            suffix_n += 1
        used.add(candidate)
        mapping[name] = candidate
    return mapping


def _geo_out_driver(out_path: Path) -> str:
    """Pure function: infer the geopandas driver name from a --vector-out-style
    path's extension, or raise ValueError with a message naming the bad
    path -- shared by every vector write path so the accepted-extensions
    rule only lives in one place.
    """
    suffix = out_path.suffix.lower()
    if suffix in (".geojson", ".json"):
        return "GeoJSON"
    if suffix == ".shp":
        return "ESRI Shapefile"
    raise ValueError(
        f"vector output path must end in .geojson or .shp, got {out_path.suffix!r} ({out_path})"
    )


def _write_geo_file(rows: list[dict], out_path: Path, driver: str):
    """Write one GeoDataFrame built from `rows` (each carrying a "geometry"
    key, see attach_geometry) to `out_path` in `driver` format, applying the
    shapefile field-name-truncation safety net when writing a Shapefile.
    Used by write_geo_outputs_per_period for every period's file, so the
    truncation/warning behavior is consistent across all of them.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    if driver == "ESRI Shapefile":
        mapping = shapefile_safe_field_names([c for c in gdf.columns if c != "geometry"])
        renamed = {k: v for k, v in mapping.items() if k != v}
        if renamed:
            print(
                f"Note: shortened {len(renamed)} field name(s) to fit the shapefile "
                f"10-character limit: {renamed}",
                file=sys.stderr,
            )
        gdf.rename(columns=mapping, inplace=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver=driver)


def split_rows_by_period(rows: list[dict], period_col: str = "period") -> dict[str, list[dict]]:
    """Pure function: group already-geometry-attached rows (see
    attach_geometry) by their period value, preserving first-seen period
    order. This is how --vector-out builds one spatial file per period -- one
    per frequency step (month/week/year/day, whichever --freq is), matching
    what --breakdown/--freq already computed, rather than one combined
    multi-period file.
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        period = row.get(period_col)
        groups.setdefault(period, []).append(row)
    return groups


def write_geo_outputs_per_period(
    rows_by_period: dict[str, list[dict]],
    out_path: Path,
    drop_cols: tuple[str, ...] = ("period",),
) -> dict[str, Path]:
    """Write one spatial file per period from `rows_by_period` (see
    split_rows_by_period), named "<stem>_<period><ext>" next to `out_path"
    -- the period is already at the end of every filename, so `drop_cols`
    defaults to dropping the now-redundant "period" column from each
    feature's attributes (every row in a given file already shares that one
    period). Format is inferred from `out_path`'s extension. Returns
    {period: path_written}, in the same order as `rows_by_period`.

    Doesn't touch `ee` -- only geopandas file I/O -- so it's testable
    offline by writing to a temp path and reading the result back.
    """
    driver = _geo_out_driver(out_path)
    written: dict[str, Path] = {}
    for period, rows in rows_by_period.items():
        period_path = out_path.with_name(f"{out_path.stem}_{period}{out_path.suffix}")
        trimmed_rows = [{k: v for k, v in row.items() if k not in drop_cols} for row in rows]
        _write_geo_file(trimmed_rows, period_path, driver)
        written[period] = period_path
    return written


def write_geo_outputs_combined(rows: list[dict], out_path: Path) -> Path:
    """Write ONE spatial file containing every row across every period --
    the "one to many" join Matt asked for: one feature per (unit, period)
    pair, geometry repeated per period, so it has the same row shape as the
    CSV. Unlike write_geo_outputs_per_period, the "period" column is kept
    (it's the only thing distinguishing rows for the same unit here, since
    there's no per-period filename to carry that instead). Named
    "<stem>_all_periods<ext>" next to `out_path`, alongside the per-period
    files write_geo_outputs_per_period writes to that same `out_path`.

    Doesn't touch `ee` -- only geopandas file I/O -- so it's testable
    offline the same way write_geo_outputs_per_period is.
    """
    driver = _geo_out_driver(out_path)
    combined_path = out_path.with_name(f"{out_path.stem}_all_periods{out_path.suffix}")
    _write_geo_file(rows, combined_path, driver)
    return combined_path


def resolve_breakdown_collection(
    aoi_file: str,
    breakdown: str,
    unit_name_field: Optional[str],
    simplify_tolerance: Optional[float] = None,
    attribute_fields: Optional[list[str]] = None,
    unit_id_field: Optional[str] = None,
):
    """Return an ee.FeatureCollection of sub-units to break the analysis down by,
    each carrying a 'unit_name' property.

    Keeps every feature in --aoi-file separate (rather than dissolving them,
    like the single-AOI path does) and labels each from --unit-name-field.

    `attribute_fields`, when given, are extra property/column names to carry
    through onto each unit so they end up as columns in the output (see
    build_breakdown_row) -- read from the boundary file and validated here,
    the same way --unit-name-field already is.

    Every unit also gets a 'unit_id' property -- a stable, unique identifier,
    as opposed to 'unit_name' which is only meant to be human-readable and can
    collide (two municipios sharing a name in different states, say). It
    comes from `unit_id_field` -- typically a pcode column -- which is
    optional but strongly recommended whenever unit names might not be
    unique; when it's not given, 'unit_id' is left None for every unit.
    """
    import ee

    if not unit_name_field:
        raise ValueError(
            "--unit-name-field is required when using --breakdown "
            "(it names the column/property in your boundary file to label each unit with)"
        )
    import geopandas as gpd

    gdf = gpd.read_file(aoi_file)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    if unit_name_field not in gdf.columns:
        raise ValueError(
            f"{unit_name_field!r} not found in {aoi_file} columns: {list(gdf.columns)}"
        )
    if unit_id_field and unit_id_field not in gdf.columns:
        raise ValueError(
            f"--unit-id-field {unit_id_field!r} not found in {aoi_file} columns: "
            f"{list(gdf.columns)}"
        )
    if attribute_fields:
        missing = [f for f in attribute_fields if f not in gdf.columns]
        if missing:
            raise ValueError(
                f"--attributes field(s) {missing} not found in {aoi_file} columns: "
                f"{list(gdf.columns)}"
            )
    features = []
    for _, row in gdf.iterrows():
        properties = {
            "unit_name": row[unit_name_field],
            "unit_id": row[unit_id_field] if unit_id_field else None,
        }
        if attribute_fields:
            for field in attribute_fields:
                properties[field] = row[field]
        features.append(
            ee.Feature(
                ee.Geometry(
                    simplify_geometry(row.geometry, simplify_tolerance).__geo_interface__
                ),
                properties,
            )
        )
    return ee.FeatureCollection(features)


def fetch_period_breakdown_stats(
    freq: str,
    fc,
    period: Period,
    scale: int = 500,
    attribute_fields: Optional[list[str]] = None,
    dark_threshold: float = DEFAULT_DARK_THRESHOLD_NW,
) -> list[dict]:
    """Query Earth Engine for one period's zonal stats across every unit in `fc`
    in a single reduceRegions call, rather than one call per unit."""
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)

    if image is None:
        unit_names = fc.aggregate_array("unit_name").getInfo()
        try:
            unit_ids = fc.aggregate_array("unit_id").getInfo()
        except Exception:  # noqa: BLE001 -- older collections may not carry unit_id
            unit_ids = []
        if len(unit_ids) != len(unit_names):
            unit_ids = [None] * len(unit_names)

        # Attribute columns (e.g. admin0_name, admin0_pcode) live on the same
        # features -- without pulling them here too, any period that falls into
        # this no-image branch (most often the most recent month, before that
        # month's VIIRS composite has been published) would silently come back
        # with every --attributes column blank instead of just the stats.
        attribute_values: dict[str, list] = {}
        for field in attribute_fields or []:
            try:
                values = fc.aggregate_array(field).getInfo()
            except Exception:  # noqa: BLE001 -- field may not exist on this collection
                values = []
            if len(values) != len(unit_names):
                values = [None] * len(unit_names)
            attribute_values[field] = values

        rows = []
        for i, (name, uid) in enumerate(zip(unit_names, unit_ids)):
            feature_properties = {"unit_name": name, "unit_id": uid}
            for field, values in attribute_values.items():
                feature_properties[field] = values[i]
            rows.append(
                build_breakdown_row(
                    period.label,
                    band,
                    feature_properties,
                    scene_count=0,
                    attribute_fields=attribute_fields,
                )
            )
        return rows

    reduced = _add_dark_band(image, band, dark_threshold).reduceRegions(
        collection=fc, reducer=_combined_reducer(), scale=scale, tileScale=4
    )
    features = reduced.getInfo()["features"]
    return [
        build_breakdown_row(
            period.label, band, f["properties"], scene_count, attribute_fields=attribute_fields
        )
        for f in features
    ]


def _download_image_geotiff(image, aoi_geom, out_path: Path, scale: int = 500) -> bool:
    """ee-touching (--raster-whole-aoi/--raster-yoy): download `image` (clipped to `aoi_geom`)
    to `out_path` as a GeoTIFF via Image.getDownloadURL(), which works
    synchronously for AOIs/resolutions small enough for Earth Engine to
    hand back directly.

    Falls back to kicking off an asynchronous Export.image.toDrive task
    when the direct download is rejected (typically "Total request size
    exceeds the limit" for a large AOI at fine resolution) -- prints where
    the export landed (the caller's Google Drive, under the same filename)
    rather than polling for completion, since a Drive export can take
    anywhere from seconds to hours depending on size. Returns True if the
    file was downloaded directly, False if it was handed off to Drive
    instead (in which case `out_path` was NOT written by this call).
    """
    import urllib.request

    import ee

    clipped = image.clip(aoi_geom)
    try:
        url = clipped.getDownloadURL(
            {"scale": scale, "region": aoi_geom, "format": "GEO_TIFF"}
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, out_path)
        return True
    except Exception as e:  # noqa: BLE001 -- getDownloadURL's size-limit error isn't a stable type
        print(
            f"  Direct download failed ({e}) -- falling back to a Google Drive export task "
            f"for {out_path.name}.",
            file=sys.stderr,
        )
        task = ee.batch.Export.image.toDrive(
            image=clipped,
            description=out_path.stem[:100],
            fileNamePrefix=out_path.stem,
            scale=scale,
            region=aoi_geom,
            maxPixels=1e10,
        )
        task.start()
        print(
            f"  Started a Drive export task ({out_path.stem}) -- check Google Drive / the "
            "Earth Engine Task Manager for completion; this tool doesn't wait for it.",
            file=sys.stderr,
        )
        return False


def export_period_raster(
    freq: str, aoi_geom, period: Period, out_path: Path, scale: int = 500
) -> bool:
    """ee-touching (--raster-whole-aoi): export one period's whole-AOI radiance
    raster as a GeoTIFF. Returns False (with a printed note, no file
    written) when the period has no VIIRS scenes available at all -- the
    same "nothing to fetch" case fetch_period_stats() reports as qa_flag
    'no_data'.
    """
    image, band, scene_count = _get_period_image_and_scene_count(freq, period)
    if image is None:
        print(f"  Skipping raster for {period.label}: no VIIRS scenes available.", file=sys.stderr)
        return False
    return _download_image_geotiff(image.select(band), aoi_geom, out_path, scale=scale)


def export_yoy_diff_raster(
    freq: str,
    aoi_geom,
    later_period: Period,
    earlier_period: Period,
    out_path: Path,
    scale: int = 500,
) -> bool:
    """ee-touching (--raster-yoy): export a 2-band
    year-over-year diff GeoTIFF -- band 1 absolute change (later minus
    earlier, nW/cm2/sr), band 2 percent change -- for one period vs. its
    year-ago period, same absolute-then-percent layout as the older
    Crimea/Ukraine change-layer rasters this mirrors. Percent change is
    left masked out where the earlier period is <= 0 nW/cm2/sr, since
    percent change from zero/negative is undefined (see README's note on
    why percent change is noisy near zero -- this is the same instability,
    just at the raster level instead of the zonal-stats level).

    Returns False (with a printed note, no file written) when either
    period has no VIIRS scenes available.
    """
    later_image, later_band, later_count = _get_period_image_and_scene_count(freq, later_period)
    earlier_image, earlier_band, earlier_count = _get_period_image_and_scene_count(
        freq, earlier_period
    )
    if later_image is None or earlier_image is None:
        print(
            f"  Skipping YoY diff raster for {later_period.label} vs {earlier_period.label}: "
            "no VIIRS scenes available for one or both periods.",
            file=sys.stderr,
        )
        return False

    later_band_img = later_image.select(later_band)
    earlier_band_img = earlier_image.select(earlier_band)
    abs_change = later_band_img.subtract(earlier_band_img).rename("radiance_change_abs")
    pct_change = (
        abs_change.divide(earlier_band_img)
        .multiply(100)
        .updateMask(earlier_band_img.gt(0))
        .rename("radiance_change_pct")
    )
    diff_image = abs_change.addBands(pct_change)
    return _download_image_geotiff(diff_image, aoi_geom, out_path, scale=scale)


def fetch_unit_geometries(fc) -> list[dict]:
    """For --vector-out: pull each --breakdown unit's geometry (plus its raw
    unit_name/unit_id) from Earth Engine, in one call -- separate from the
    per-period stats query, since geometry doesn't change across periods.

    Returns plain dicts keyed like a raw (pre-rename_unit_columns) row --
    "unit_name", "unit_id", "geometry" (a shapely geometry) -- so
    _chart_key() can be reused as-is to build the join-key lookup main()
    uses to attach each geometry to its matching CSV rows.
    """
    from shapely.geometry import shape

    features = fc.getInfo()["features"]
    return [
        {
            "unit_name": f.get("properties", {}).get("unit_name"),
            "unit_id": f.get("properties", {}).get("unit_id"),
            "geometry": shape(f["geometry"]),
        }
        for f in features
    ]


def fetch_whole_aoi_geometry(aoi_geom):
    """For --vector-out on a non-breakdown run: pull the single whole-AOI
    geometry down from Earth Engine as a shapely geometry."""
    from shapely.geometry import shape

    return shape(aoi_geom.getInfo())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract a VIIRS nighttime-lights radiance time series for an AOI."
    )
    p.add_argument(
        "--aoi-file",
        required=True,
        help=(
            "Path to a boundary file (GeoJSON/shapefile/etc.) -- the area of interest. "
            "This is the only way to supply an AOI; there's no built-in country/admin-name "
            "lookup, so bring your own boundary."
        ),
    )
    p.add_argument("--start", required=True, help="Start date, YYYY-MM-DD (inclusive)")
    p.add_argument("--end", required=True, help="End date, YYYY-MM-DD (exclusive)")
    p.add_argument("--freq", required=True, choices=VALID_FREQS, help="Time step")
    p.add_argument(
        "--out-dir",
        required=True,
        help=(
            "Output folder for every output file this run produces (CSV, vector, rasters, "
            "chart). Filenames are built from --geoextent, e.g. "
            "'npl_nightlights.csv' -- there's no separate path to set for each output kind."
        ),
    )
    p.add_argument(
        "--geoextent",
        required=True,
        help=(
            "Geoextent code used in every output filename (e.g. 'UKR', 'crm' for a Crimea "
            "AOI). Always required -- --aoi-file has no code of its own to default to."
        ),
    )
    p.add_argument(
        "--vector-out",
        choices=("geojson", "shapefile"),
        default=None,
        help=(
            "Also write spatial output joined to the boundary geometry by the unit's unique "
            "identifier/pcode, in addition to the CSV, in the given format. Writes one file "
            "per period (month/week/year/day, whichever --freq is), named "
            "'<geoextent>_nightlights_<period><ext>' -- one feature per unit, with the unit's "
            "name/ID/attributes columns plus mean_radiance, sum_radiance, median_radiance, "
            "valid_pixel_count, scene_count, and qa_flag -- plus one combined file with every "
            "period in it (one feature per unit per period, same row shape as the CSV, "
            "geometry repeated per period), named '<geoextent>_nightlights_all_periods<ext>'. "
            "Works with or without --breakdown -- without it, there's just one implicit "
            "'unit' (the whole AOI)."
        ),
    )
    p.add_argument(
        "--chart",
        action="store_true",
        help=(
            "Also write a PNG chart next to the CSV. Without --breakdown, one line "
            "chart for the whole AOI. With --breakdown, a small-multiples grid with "
            "one mini chart per unit (see --chart-units to limit which ones)."
        ),
    )
    p.add_argument(
        "--chart-units",
        default=None,
        help=(
            "Comma-separated exact unit_name values to chart, when using --chart with "
            "--breakdown. If omitted, every unit is charted, up to "
            f"{DEFAULT_MAX_BREAKDOWN_CHART_PANELS} -- beyond that the chart is truncated "
            "with a warning, since a grid of hundreds of tiny panels isn't useful."
        ),
    )
    p.add_argument(
        "--ee-project",
        default=None,
        help="Earth Engine cloud project ID, if your account needs one (see README)",
    )
    p.add_argument(
        "--breakdown",
        choices=("admin1", "admin2", "admin3", "admin4", "admin5"),
        default=None,
        help=(
            "Instead of one AOI-wide row per period, output one row per admin unit "
            "per period, keeping each feature in --aoi-file separate (needs "
            "--unit-name-field). The admin level is just a label for the output -- "
            "any of admin1-5 works, however your boundary file is actually organized."
        ),
    )
    p.add_argument(
        "--unit-name-field",
        default=None,
        help="Property/column in --aoi-file to label each unit with, when using --breakdown",
    )
    p.add_argument(
        "--unit-id-field",
        default=None,
        help=(
            "Property/column in --aoi-file that uniquely identifies each unit (typically a "
            "pcode), when using --breakdown. Included as its own 'unit_id' column in the "
            "output -- unlike --unit-name-field, which is only guaranteed to be "
            "human-readable, not unique (two municipios can share a name)."
        ),
    )
    p.add_argument(
        "--attributes",
        default=None,
        help=(
            "Comma-separated list of extra property/column names from --aoi-file to "
            "include as their own columns in --breakdown output. If omitted, output has "
            "no extra columns beyond unit_name -- use this to disambiguate units that "
            "share a name (e.g. two municipios called the same thing in different states) "
            "by including their parent unit's name/code."
        ),
    )
    p.add_argument(
        "--wizard",
        action="store_true",
        help=(
            "Run an interactive prompt that walks through every option instead of passing "
            "flags. Also runs automatically if the tool is started with no arguments at all."
        ),
    )
    p.add_argument(
        "--simplify-tolerance",
        type=float,
        default=None,
        help=(
            "Simplify --aoi-file geometries by this many degrees before sending to Earth "
            "Engine (only used with --breakdown). Needed for detailed admin2/raion "
            "layers with many units, which can exceed Earth Engine's 10MB request-payload limit "
            "otherwise. Try 0.001 (~100m) as a starting point if you hit a "
            "'Request payload size exceeds the limit' error."
        ),
    )
    p.add_argument(
        "--include-yoy",
        action="store_true",
        help=(
            "Add <stat>_yoy_abs/<stat>_yoy_pct columns to mean_radiance/sum_radiance/"
            "median_radiance -- each row vs. the same period one year back (e.g. March "
            "2022 vs. March 2021; the same ISO week number a year earlier for --freq "
            "weekly). Fetches whichever year-ago periods aren't already covered by "
            "--start/--end as extra Earth Engine calls. Lead with the _abs column when "
            "judging whether something real changed -- see README for why _pct gets "
            "noisy near zero."
        ),
    )
    p.add_argument(
        "--dark-threshold",
        type=float,
        default=DEFAULT_DARK_THRESHOLD_NW,
        help=(
            "Radiance (nW/cm2/sr) below which a pixel counts as 'dark' for the "
            "pct_dark output column (percent of valid pixels below this threshold "
            f"in that period/unit). Default: {DEFAULT_DARK_THRESHOLD_NW}."
        ),
    )
    p.add_argument(
        "--raster-whole-aoi",
        action="store_true",
        help=(
            "Also export a whole-AOI radiance GeoTIFF for each period, written into "
            "--out-dir. Independent of --raster-yoy -- turn on either, both, or neither. "
            "Filenames follow '{geoextent}_evnt_lit_ras_s0_viirs_pp_{period}.tif' -- see "
            "--geoextent for where {geoextent} comes from. Downloads directly when Earth "
            "Engine allows it; falls back to a Google Drive export task (not waited on) "
            "for an AOI/resolution too large for a direct download."
        ),
    )
    p.add_argument(
        "--raster-yoy",
        action="store_true",
        help=(
            "Also export a 2-band year-over-year diff GeoTIFF (band 1 absolute change, "
            "band 2 percent change) for each period that has a year-ago period available, "
            "written into --out-dir. Independent of --include-yoy (the CSV's tabular "
            "year-over-year columns) and of --raster-whole-aoi -- turn on either, both, or "
            "neither raster flag regardless of --include-yoy. Filenames follow "
            "'{geoextent}_evnt_lit_ras_s0_viirs_pp_diff_yoy_{period}_minus_{prior}.tif'."
        ),
    )
    p.add_argument(
        "--raster-scale",
        type=int,
        default=500,
        help=(
            "Pixel resolution in meters for --raster-whole-aoi/--raster-yoy GeoTIFFs. "
            "Default: 500."
        ),
    )
    return p


def _prompt_text(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{label}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        print("  (required -- please enter a value)")


def _prompt_choice(label: str, options: list[str]) -> int:
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        val = input(f"Choose 1-{len(options)}: ").strip()
        if val.isdigit() and 1 <= int(val) <= len(options):
            return int(val) - 1
        print(f"  Please enter a number from 1 to {len(options)}.")


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    val = input(f"{label} {suffix}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def _prompt_single_field(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_text, but when `available_fields` is a known, non-empty
    list (from list_file_fields), re-prompts until the answer is exactly one
    of them -- catching a typo, or someone pasting a comma-separated list
    where one field name was expected, right where it happened instead of
    deep inside a later Earth Engine call.
    """
    while True:
        val = _prompt_text(label)
        if not available_fields or val in available_fields:
            return val
        print(f"  '{val}' isn't one of the columns/fields listed above -- enter exactly one.")


def _prompt_optional_field(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_single_field, but blank is an accepted answer (for an
    optional field like a unique-ID column that not every boundary file has).
    """
    while True:
        val = _prompt_text(label, default="").strip()
        if not val or not available_fields or val in available_fields:
            return val
        print(f"  '{val}' isn't one of the columns/fields listed above -- enter exactly one, or leave blank.")


def _prompt_field_list(label: str, available_fields: Optional[list[str]]) -> str:
    """Like _prompt_text with an empty default, but when `available_fields` is
    known, validates every comma-separated entry against it and re-prompts
    (listing which ones didn't match) rather than passing bad names through
    to fail later.

    On a bad entry, the retry prompt is pre-filled with just the valid names
    from the rejected attempt (as its default), so fixing one typo in a long
    comma list doesn't mean retyping the whole thing from scratch -- pressing
    Enter accepts the trimmed list, or the user can type a fresh full list.
    Without this, a single mistyped name in a six-column request would
    otherwise tempt the user into re-entering a short list "for now" and
    silently losing the columns they actually wanted.
    """
    default = ""
    while True:
        val = _prompt_text(label, default=default)
        if not val.strip() or not available_fields:
            return val
        requested = [f.strip() for f in val.split(",") if f.strip()]
        unknown = [f for f in requested if f not in available_fields]
        if not unknown:
            return val
        known = [f for f in requested if f not in unknown]
        default = ", ".join(known)
        print(
            f"  {unknown} not found in the columns/fields listed above -- try again "
            f"(the rest looked fine -- press Enter to keep just {default or 'none'}, "
            "or type the full corrected list)."
        )


def list_file_fields(aoi_file: str) -> list[str]:
    """Return the non-geometry column names in a boundary file, for showing the
    wizard user what's actually available before asking them to name a field.
    Raises whatever geopandas raises on a bad path/format -- callers decide how
    to handle that (the wizard treats it as "couldn't look it up", not fatal).
    """
    import geopandas as gpd

    gdf = gpd.read_file(aoi_file)
    return [c for c in gdf.columns if c != "geometry"]


def run_wizard() -> list[str]:
    """Interactively ask for each option and return the equivalent argv list.

    This deliberately doesn't duplicate any validation or execution logic --
    it just builds the same flags a command-line invocation would pass, which
    then go through the normal build_arg_parser().parse_args() + main() path
    below. That keeps there being exactly one code path that actually runs
    the tool, whether it was configured via flags or via this wizard.
    """
    print("VIIRS Nightlight Tool -- interactive setup")
    print("(Ctrl+C at any point to cancel)\n")

    argv: list[str] = []
    ee_project: Optional[str] = None  # asked for once, as soon as it's actually needed
    ee_project_asked = False  # distinguishes "asked, left blank" from "not asked yet"

    aoi_file = _prompt_text("Path to your boundary file (shapefile, GeoJSON, or geodatabase)")
    argv += ["--aoi-file", aoi_file]

    admin_level_labels = [
        "Admin 0 -- whole AOI as a single unit (one time series)",
        "Admin 1 (e.g. oblast/governorate/state)",
        "Admin 2 (e.g. raion/district/municipio)",
        "Admin 3 (e.g. commune/ward)",
        "Admin 4 (e.g. village/sub-ward)",
        "Admin 5 (finest level your data has)",
    ]
    breakdown_choice = _prompt_choice("Granularity", admin_level_labels)
    available_fields: list[str] = []
    if breakdown_choice != 0:
        breakdown = f"admin{breakdown_choice}"
        argv += ["--breakdown", breakdown]

        try:
            available_fields = list_file_fields(aoi_file)
        except Exception as e:  # noqa: BLE001
            print(f"  (Couldn't read {aoi_file} to list its columns: {e})")
        if available_fields:
            print(f"\nColumns found in {aoi_file}:")
            for f in available_fields:
                print(f"  - {f}")

        argv += [
            "--unit-name-field",
            _prompt_single_field("Which column names each unit", available_fields),
        ]

        unit_id_field = _prompt_optional_field(
            "Which column uniquely identifies each unit, e.g. a pcode "
            "(recommended, especially if unit names might repeat -- blank to skip)",
            available_fields,
        )
        if unit_id_field:
            argv += ["--unit-id-field", unit_id_field]

        file_size_mb = None
        try:
            file_size_mb = Path(aoi_file).stat().st_size / (1024 * 1024)
        except OSError:
            pass
        tolerance_prompt = (
            "Simplify geometries by this many degrees before sending to Earth Engine "
            "(blank to skip; try 0.001 for ~100m if you hit a "
            "'Request payload size exceeds the limit' error)"
        )
        if file_size_mb and file_size_mb > 2:
            print(
                f"\n{aoi_file} is {file_size_mb:.1f} MB -- a detailed boundary file this "
                "size can exceed Earth Engine's 10MB request-payload limit once every "
                "unit's full geometry is sent as part of a --breakdown query."
            )
        while True:
            tolerance = _prompt_text(tolerance_prompt, default="").strip()
            if not tolerance:
                break
            try:
                float(tolerance)
                argv += ["--simplify-tolerance", tolerance]
                break
            except ValueError:
                print(f"  '{tolerance}' isn't a number -- enter a decimal-degree value or leave blank.")

        attrs = _prompt_field_list(
            "Extra attribute columns to include, comma-separated "
            "(blank for defaults -- see README)",
            available_fields,
        )
        if attrs.strip():
            argv += ["--attributes", attrs.strip()]

    argv += ["--start", _prompt_text("Start date (YYYY-MM-DD, inclusive)")]
    argv += ["--end", _prompt_text("End date (YYYY-MM-DD, exclusive)")]

    freq_options = list(VALID_FREQS)
    freq_choice = _prompt_choice("Frequency", freq_options)
    argv += ["--freq", freq_options[freq_choice]]

    argv += ["--out-dir", _prompt_text("Output folder for all outputs", default="out")]
    geoextent = _prompt_text("Geoextent code for output filenames (e.g. 'UKR', 'crm')").strip()
    if geoextent:
        argv += ["--geoextent", geoextent]

    if _prompt_yes_no(
        "Also write spatial output (GeoJSON/Shapefile), joined by the unit's unique "
        "ID/pcode -- one file per period plus one combined file with every period?",
        default=False,
    ):
        vector_choice = _prompt_choice("Vector format", ["GeoJSON", "Shapefile"])
        argv += ["--vector-out", "geojson" if vector_choice == 0 else "shapefile"]

    if _prompt_yes_no(
        "Add year-over-year change columns (each row vs. the same period one year "
        "back)?",
        default=False,
    ):
        argv.append("--include-yoy")

    dark_threshold = _prompt_text(
        "Dark-pixel threshold for the pct_dark column, nW/cm2/sr "
        "(percent of valid pixels below this counts as 'dark' each period)",
        default=str(DEFAULT_DARK_THRESHOLD_NW),
    ).strip()
    if dark_threshold and dark_threshold != str(DEFAULT_DARK_THRESHOLD_NW):
        argv += ["--dark-threshold", dark_threshold]

    if _prompt_yes_no(
        "Also export a whole-AOI radiance GeoTIFF per period (written into the output "
        "folder)?",
        default=False,
    ):
        argv.append("--raster-whole-aoi")

    if _prompt_yes_no(
        "Also export a year-over-year diff GeoTIFF per period (written into the output "
        "folder) -- independent of the CSV's year-over-year columns above?",
        default=False,
    ):
        argv.append("--raster-yoy")

    if breakdown_choice == 0:
        if _prompt_yes_no("Also write a chart PNG next to the CSV?", default=True):
            argv.append("--chart")
    else:
        if _prompt_yes_no(
            "Also write a chart PNG (one mini chart per unit) next to the CSV?",
            default=True,
        ):
            argv.append("--chart")
            chart_units = _prompt_text(
                "Which units to chart, comma-separated exact unit_name values "
                f"(blank to chart all, up to {DEFAULT_MAX_BREAKDOWN_CHART_PANELS})",
                default="",
            )
            if chart_units.strip():
                argv += ["--chart-units", chart_units.strip()]

    if not ee_project_asked:
        ee_project = _prompt_text(
            "Earth Engine cloud project ID (blank if your account doesn't need one)", default=""
        ).strip() or None
    if ee_project:
        argv += ["--ee-project", ee_project]

    print()
    return argv


def build_argv_from_form(fields: dict) -> list[str]:
    """Pure function: turn a plain dict of form values into the argv list
    build_arg_parser().parse_args() expects.

    This is the GUI's equivalent of run_wizard() -- same one-code-path
    principle (build an argv list, then let build_arg_parser()/main() do the
    actual validation and work), but driven by a plain dict of already-chosen
    values instead of interactive input() prompts, so it's usable from a
    Tkinter form (or tested directly, with no widgets involved at all).

    Expected keys (all optional except as noted; unmentioned/None/empty
    values are treated as "not set"):
        aoi_file: str -- path to the boundary file (required; this tool only
            supports a supplied boundary file as the area of interest, not a
            country/admin-name lookup)
        breakdown_level: int 0-5 (0 or omitted = no --breakdown, whole AOI)
        unit_name_field: str -- required when breakdown_level > 0
        unit_id_field: str
        simplify_tolerance: str/float
        attributes: str (comma-separated) or list[str]
        start, end: str, YYYY-MM-DD (required)
        freq: one of VALID_FREQS (required)
        out_dir: str, output folder for every output this run produces (required)
        geoextent: str -- geoextent code used in every output filename (required --
            the boundary file has no code of its own to default to)
        vector_out: "geojson" | "shapefile" | None -- also write spatial output
            joined to the boundary geometry, in addition to the CSV, in this format
        include_yoy: bool -- add <stat>_yoy_abs/_pct columns vs. the same period one
            year back
        dark_threshold: str/float -- radiance (nW/cm2/sr) below which a pixel counts
            as 'dark' for the pct_dark column; blank/omitted uses the tool's default
        raster_whole_aoi: bool -- also export a whole-AOI radiance GeoTIFF per period,
            written into out_dir. Independent of include_yoy and raster_yoy.
        raster_yoy: bool -- also export a year-over-year diff GeoTIFF per period,
            written into out_dir. Independent of include_yoy and raster_whole_aoi.
        raster_scale: str/int -- pixel resolution in meters for raster GeoTIFFs;
            blank/omitted uses the tool's default (500)
        chart: bool
        chart_units: str (comma-separated) or list[str] -- only used if chart
            and breakdown_level are both set
        ee_project: str

    Raises ValueError with a message fit to show the user directly (e.g. in
    a message box) when something required is missing or inconsistent --
    the same checks build_arg_parser()/main() would eventually surface, just
    caught earlier with a friendlier message than an argparse error or a
    raw exception partway through a run.
    """
    argv: list[str] = []

    path = (fields.get("aoi_file") or "").strip()
    if not path:
        raise ValueError("Choose a boundary file.")
    argv += ["--aoi-file", path]

    breakdown_level = fields.get("breakdown_level") or 0
    if breakdown_level:
        if breakdown_level not in (1, 2, 3, 4, 5):
            raise ValueError("Granularity must be Admin 0-5.")
        argv += ["--breakdown", f"admin{breakdown_level}"]

        unit_name_field = (fields.get("unit_name_field") or "").strip()
        if not unit_name_field:
            raise ValueError("Choose which column names each unit.")
        argv += ["--unit-name-field", unit_name_field]

        unit_id_field = (fields.get("unit_id_field") or "").strip()
        if unit_id_field:
            argv += ["--unit-id-field", unit_id_field]

        tolerance = fields.get("simplify_tolerance")
        if tolerance not in (None, ""):
            try:
                float(tolerance)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Simplify tolerance {tolerance!r} isn't a number -- enter a "
                    "decimal-degree value (e.g. 0.001) or leave it blank."
                )
            argv += ["--simplify-tolerance", str(tolerance)]

        attributes = fields.get("attributes")
        if attributes:
            if isinstance(attributes, (list, tuple)):
                attributes = ",".join(a for a in attributes if a)
            attributes = attributes.strip()
            if attributes:
                argv += ["--attributes", attributes]

    start = (fields.get("start") or "").strip()
    end = (fields.get("end") or "").strip()
    if not start or not end:
        raise ValueError("Enter both a start and end date (YYYY-MM-DD).")
    argv += ["--start", start, "--end", end]

    freq = fields.get("freq")
    if freq not in VALID_FREQS:
        raise ValueError(f"Choose a frequency ({', '.join(VALID_FREQS)}).")
    argv += ["--freq", freq]

    out_dir = (fields.get("out_dir") or "").strip()
    if not out_dir:
        raise ValueError("Choose an output folder.")
    argv += ["--out-dir", out_dir]

    geoextent = (fields.get("geoextent") or "").strip()
    if not geoextent:
        raise ValueError(
            "Enter a geoextent code for output filenames (e.g. 'crm') -- the boundary "
            "file has no code of its own to default to."
        )
    argv += ["--geoextent", geoextent]

    vector_out = fields.get("vector_out")
    if vector_out:
        if vector_out not in ("geojson", "shapefile"):
            raise ValueError("Vector format must be 'geojson' or 'shapefile'.")
        argv += ["--vector-out", vector_out]

    if fields.get("include_yoy"):
        argv.append("--include-yoy")

    dark_threshold = fields.get("dark_threshold")
    if dark_threshold not in (None, ""):
        try:
            float(dark_threshold)
        except (TypeError, ValueError):
            raise ValueError(
                f"Dark-pixel threshold {dark_threshold!r} isn't a number -- enter a "
                "radiance value in nW/cm2/sr (e.g. 0.5) or leave it blank."
            )
        argv += ["--dark-threshold", str(dark_threshold)]

    if fields.get("raster_whole_aoi"):
        argv.append("--raster-whole-aoi")
    if fields.get("raster_yoy"):
        argv.append("--raster-yoy")

    if fields.get("raster_whole_aoi") or fields.get("raster_yoy"):
        raster_scale = fields.get("raster_scale")
        if raster_scale not in (None, ""):
            try:
                int(raster_scale)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Raster scale {raster_scale!r} isn't a whole number -- enter meters "
                    "per pixel (e.g. 500) or leave it blank."
                )
            argv += ["--raster-scale", str(raster_scale)]

    if fields.get("chart"):
        argv.append("--chart")
        if breakdown_level:
            chart_units = fields.get("chart_units")
            if isinstance(chart_units, (list, tuple)):
                chart_units = ",".join(c for c in chart_units if c)
            if chart_units:
                chart_units = chart_units.strip()
                if chart_units:
                    argv += ["--chart-units", chart_units]

    ee_project = (fields.get("ee_project") or "").strip()
    if ee_project:
        argv += ["--ee-project", ee_project]

    return argv


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if not raw_argv or "--wizard" in raw_argv:
        try:
            argv = run_wizard()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 1

    args = build_arg_parser().parse_args(argv)
    attribute_fields = (
        [f.strip() for f in args.attributes.split(",") if f.strip()] if args.attributes else None
    )

    periods = build_periods(args.start, args.end, args.freq)
    if not periods:
        print("No periods to process — check --start/--end/--freq.", file=sys.stderr)
        return 1
    if args.freq in ("daily", "weekly") and len(periods) > 366:
        print(
            f"Warning: {len(periods)} {args.freq} periods requested — this will make "
            f"{len(periods)} separate Earth Engine calls and may be slow.",
            file=sys.stderr,
        )

    try:
        geoextent = resolve_geoextent(args.geoextent)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    import ee

    try:
        if args.ee_project:
            ee.Initialize(project=args.ee_project)
        else:
            ee.Initialize()
    except Exception as e:  # noqa: BLE001
        print(
            "Earth Engine initialization failed. Have you run "
            "`earthengine authenticate`? See README.md.\n"
            f"Original error: {e}",
            file=sys.stderr,
        )
        return 1

    rows = []
    unit_name_column = "unit_name"
    unit_id_column: Optional[str] = "unit_id"

    if args.breakdown:
        fc = resolve_breakdown_collection(
            args.aoi_file,
            args.breakdown,
            args.unit_name_field,
            simplify_tolerance=args.simplify_tolerance,
            attribute_fields=attribute_fields,
            unit_id_field=args.unit_id_field,
        )
        # Output columns should say what they actually are -- the column the
        # user picked as --unit-name-field/--unit-id-field -- rather than a
        # generic 'unit_name'/'unit_id' label. See rename_unit_columns() for
        # where this is applied.
        unit_name_column = args.unit_name_field
        unit_id_column = args.unit_id_field or "unit_id"
        unit_count = fc.size().getInfo()
        print(f"Breaking down into {unit_count} {args.breakdown} units.", file=sys.stderr)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.extend(
                fetch_period_breakdown_stats(
                    args.freq,
                    fc,
                    period,
                    attribute_fields=attribute_fields,
                    dark_threshold=args.dark_threshold,
                )
            )
    else:
        aoi_geom = resolve_aoi_geometry(args.aoi_file)
        for i, period in enumerate(periods, 1):
            print(f"[{i}/{len(periods)}] {period.label} ...", file=sys.stderr)
            rows.append(
                fetch_period_stats(
                    args.freq, aoi_geom, period, dark_threshold=args.dark_threshold
                )
            )

    change_group_key = _chart_key if args.breakdown else None

    if args.include_yoy:
        # Pool every period's rows (fetched already) by (group, period_label),
        # then fetch only the year-ago periods that aren't already in that
        # pool -- e.g. a two-year --start/--end already contains each later
        # period's year-ago row, so only the first year's periods need a
        # separate fetch.
        pool: dict = {}
        for row in rows:
            key = change_group_key(row) if change_group_key else None
            pool[(key, row.get("period"))] = row

        existing_labels = {period.label for period in periods}
        year_ago_periods = []
        seen_labels = set()
        for period in periods:
            ya = year_ago_period(period, args.freq)
            if ya is None or ya.label in existing_labels or ya.label in seen_labels:
                continue
            seen_labels.add(ya.label)
            year_ago_periods.append(ya)

        for i, ya_period in enumerate(year_ago_periods, 1):
            print(
                f"[yoy {i}/{len(year_ago_periods)}] fetching year-ago period "
                f"{ya_period.label} ...",
                file=sys.stderr,
            )
            if args.breakdown:
                ya_rows = fetch_period_breakdown_stats(
                    args.freq,
                    fc,
                    ya_period,
                    attribute_fields=attribute_fields,
                    dark_threshold=args.dark_threshold,
                )
            else:
                ya_rows = [
                    fetch_period_stats(
                        args.freq, aoi_geom, ya_period, dark_threshold=args.dark_threshold
                    )
                ]
            for row in ya_rows:
                key = change_group_key(row) if change_group_key else None
                pool[(key, row.get("period"))] = row

        rows = compute_year_over_year_change(
            rows, args.freq, pool, group_key=change_group_key
        )

    out_dir = Path(args.out_dir)
    out_path = out_dir / build_output_filename(geoextent, "csv")
    csv_rows = (
        rename_unit_columns(rows, unit_name_column, unit_id_column) if args.breakdown else rows
    )
    write_csv(csv_rows, out_path)
    print(f"Wrote {len(rows)} rows to {out_path}")

    if args.vector_out:
        vector_ext = "geojson" if args.vector_out == "geojson" else "shp"
        geo_out_path = out_dir / build_output_filename(geoextent, vector_ext)
        missing_keys: set = set()

        if args.breakdown:
            unit_geoms = fetch_unit_geometries(fc)
            # _chart_key() (unit_id when present, else unit_name) doubles as
            # the join key here -- same "which unit is this really" logic
            # --chart-units matching already relies on, applied to the raw
            # (pre-rename) properties fetch_unit_geometries() returns.
            geom_by_key = {_chart_key(g): g["geometry"] for g in unit_geoms}

            def geometry_for_row(row):
                key = _chart_key(
                    {
                        "unit_id": row.get(unit_id_column) if unit_id_column else None,
                        "unit_name": row.get(unit_name_column),
                    }
                )
                geom = geom_by_key.get(key)
                if geom is None:
                    missing_keys.add(key)
                return geom
        else:
            whole_aoi_geometry = fetch_whole_aoi_geometry(aoi_geom)

            def geometry_for_row(row):
                return whole_aoi_geometry

        geo_rows = attach_geometry(csv_rows, geometry_for_row)
        if missing_keys:
            print(
                f"Warning: {len(missing_keys)} unit(s) had no matching geometry and were "
                f"left out of the spatial output: {sorted(missing_keys, key=str)}",
                file=sys.stderr,
            )
            geo_rows = [r for r in geo_rows if r["geometry"] is not None]

        rows_by_period = split_rows_by_period(geo_rows)
        try:
            per_period_paths = write_geo_outputs_per_period(rows_by_period, geo_out_path)
            combined_path = write_geo_outputs_combined(geo_rows, geo_out_path)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(
            f"Wrote {len(per_period_paths)} spatial file(s), one per period: "
            f"{', '.join(str(p) for p in per_period_paths.values())}"
        )
        print(f"Wrote 1 combined spatial file with every period (one unit-period per row): {combined_path}")

    if args.chart:
        chart_path = out_path.with_suffix(".png")
        if args.breakdown:
            chart_units = (
                [u.strip() for u in args.chart_units.split(",") if u.strip()]
                if args.chart_units
                else None
            )
            all_units = select_breakdown_chart_units(rows, unit_filter=chart_units, max_panels=len(rows) + 1)
            units = all_units if chart_units else all_units[:DEFAULT_MAX_BREAKDOWN_CHART_PANELS]
            if not units:
                print(
                    "Note: --chart-units matched no units in the output -- skipping chart. "
                    "Check the spelling against the unit_name column.",
                    file=sys.stderr,
                )
            else:
                if chart_units is None and len(all_units) > len(units):
                    print(
                        f"Note: charting the first {len(units)} of {len(all_units)} units "
                        "-- use --chart-units to pick specific ones instead.",
                        file=sys.stderr,
                    )
                write_breakdown_chart(rows, chart_path, units)
                print(f"Wrote chart ({len(units)} unit panels) to {chart_path}")
        else:
            write_chart(rows, chart_path)
            print(f"Wrote chart to {chart_path}")

    if args.raster_whole_aoi or args.raster_yoy:
        # Always the whole AOI, regardless of --breakdown -- raster export
        # isn't per-unit, so resolve the AOI geometry directly rather than
        # via the --breakdown feature collection.
        raster_aoi_geom = aoi_geom if not args.breakdown else resolve_aoi_geometry(args.aoi_file)
        raster_dir = out_dir

        if args.raster_whole_aoi:
            written, skipped = 0, 0
            for i, period in enumerate(periods, 1):
                filename = build_raster_filename(geoextent, period.label)
                print(f"[raster {i}/{len(periods)}] {filename} ...", file=sys.stderr)
                if export_period_raster(
                    args.freq, raster_aoi_geom, period, raster_dir / filename, scale=args.raster_scale
                ):
                    written += 1
                else:
                    skipped += 1
            print(f"Wrote {written} raster file(s) to {raster_dir} ({skipped} skipped -- see notes above)")

        if args.raster_yoy:
            yoy_written, yoy_skipped = 0, 0
            yoy_pairs = [
                (period, year_ago_period(period, args.freq))
                for period in periods
            ]
            yoy_pairs = [(p, ya) for p, ya in yoy_pairs if ya is not None]
            for i, (period, ya_period) in enumerate(yoy_pairs, 1):
                freetext = f"diff_yoy_{period.label}_minus_{ya_period.label}"
                filename = build_raster_filename(geoextent, freetext)
                print(f"[raster yoy {i}/{len(yoy_pairs)}] {filename} ...", file=sys.stderr)
                if export_yoy_diff_raster(
                    args.freq,
                    raster_aoi_geom,
                    period,
                    ya_period,
                    raster_dir / filename,
                    scale=args.raster_scale,
                ):
                    yoy_written += 1
                else:
                    yoy_skipped += 1
            print(
                f"Wrote {yoy_written} YoY diff raster file(s) to {raster_dir} "
                f"({yoy_skipped} skipped -- see notes above)"
            )

    flagged = [r for r in rows if r.get("qa_flag") not in (None, "ok")]
    if flagged:
        print(
            f"Note: {len(flagged)}/{len(rows)} periods flagged (see qa_flag column) "
            "— treat those values with caution.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
